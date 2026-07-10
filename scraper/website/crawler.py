"""Contextual crawler for institutions whose fee page isn't known ahead of time.

Rather than requiring an exact fee-page URL and CSS selectors per entity (which
generic.GenericWebsiteScraper needs), this locates a site's relevant fee/news
pages in two phases, staying on the same domain throughout:

1. Sitemap-first: check robots.txt for a declared sitemap (falling back to
   /sitemap.xml), collect its URLs (following one level of sitemap-index
   nesting), and fetch whichever candidates look most promising by URL. This is
   usually faster and more reliable than blind crawling when a sitemap exists.
2. Contextual link crawl (fallback): if there's no usable sitemap or nothing in
   it clears the relevance bar, crawl from base_url, greedily prioritizing
   links/pages that look fee-related (by URL path and link text keywords) over
   a plain breadth-first search.

Unlike an earlier version of this module, discovery does NOT reduce to a
single "best" page. An institution can have more than one genuinely relevant
page at once -- e.g. BPI has both a permanent-rate press release and a
separate limited-time small-transaction fee waiver promo -- so this returns
the top-scoring candidates (see MAX_CANDIDATE_PAGES) and runs LLM-based
extraction (scraper/llm_extract.py) on each, keeping every distinct fee
condition found rather than picking one page and discarding the rest. If no
page clears the minimum relevance threshold at all, this raises rather than
guessing; run_all.py turns that into a flagged scraper_health entry.
"""
from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from collections import deque
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from scraper.base import BaseScraper, FeeRecord, ScrapeResult, hash_text, structure_hash, utcnow_iso
from scraper.llm_extract import extract_fee_conditions, format_conditions_text

DEFAULT_KEYWORDS = [
    "instapay", "pesonet", "transfer fee", "schedule of fees",
    "fees and charges", "service fee", "fund transfer",
    # Several institutions post fee changes as news rather than updating a
    # dedicated fee page (BPI's current standing fee and PBCom's fee change
    # were both found this way). Kept narrow on purpose -- "advisory"/
    # "announcement" were tried and reverted: too generic, they pulled the
    # crawler toward unrelated notices (e.g. BPI: a remittance-service-
    # deactivation notice that only mentions InstaPay/PESONet in passing).
    "media center", "press release",
]
DEFAULT_USER_AGENT = "transfer-fees-monitoring/0.1 (+open-source fee monitor)"
MIN_RELEVANCE_SCORE = 2
MAX_SITEMAP_URLS = 500
MAX_NESTED_SITEMAPS = 5
MAX_CANDIDATE_PAGES = 8  # cap on how many relevant pages get LLM extraction per entity
BROWSER_FALLBACK_MAX_PAGES = 10  # browser navigation is much slower per-page than plain HTTP

_MONTH_NUMBERS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
import re  # noqa: E402  (kept local to the date-extraction helpers below)

_MONTH_NAME_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)
_META_DATE_RE = re.compile(
    r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_TIME_TAG_DATE_RE = re.compile(r'<time[^>]+datetime=["\']([^"\']+)["\']', re.IGNORECASE)


def extract_page_date(raw_html: str, text: str) -> dt.date | None:
    """Best-effort publish date, used only to rank candidate pages (most
    recent first) when several are equally relevant by keyword score."""
    for pattern in (_META_DATE_RE, _TIME_TAG_DATE_RE):
        match = pattern.search(raw_html)
        if match:
            try:
                return dt.datetime.fromisoformat(match.group(1).replace("Z", "+00:00")).date()
            except ValueError:
                continue

    # Fall back to a "Month D, YYYY" dateline near the top of the article --
    # bylines/datelines are almost always near the start, not buried in body text.
    match = _MONTH_NAME_DATE_RE.search(text[:2000])
    if match:
        month = _MONTH_NUMBERS.get(match.group(1).lower())
        try:
            return dt.date(int(match.group(3)), month, int(match.group(2)))
        except (TypeError, ValueError):
            return None
    return None


class NoFeePageFoundError(RuntimeError):
    """Raised when the discovery budget is exhausted without finding any page
    that plausibly contains InstaPay/PESONet fee info."""


class SiteCrawlerScraper(BaseScraper):
    source_type = "website"

    def __init__(
        self,
        entity: str,
        base_url: str,
        keywords: list[str] | None = None,
        max_pages: int = 25,
        max_depth: int = 2,
        timeout: int = 15,
    ):
        super().__init__(entity, base_url)
        self.base_url = base_url
        self.keywords = [k.lower() for k in (keywords or DEFAULT_KEYWORDS)]
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.timeout = timeout
        self._domain = urlparse(base_url).netloc
        self._visited_count = 0  # page-fetch budget shared across sitemap + crawl phases
        self._use_browser = False  # set True only during the Playwright fallback pass
        self._browser_page = None  # the reused Playwright page during that pass

    def _score_url(self, url: str, link_text: str = "") -> int:
        # URL slugs are almost always hyphen/underscore-separated ("transfer-
        # fees-for-small-transactions"), which wouldn't otherwise match a
        # space-separated keyword phrase like "transfer fee" -- normalizing to
        # spaces before matching catches those (found via BPI's promo page,
        # which was missed until this normalization was added).
        normalized_url = url.lower().replace("-", " ").replace("_", " ")
        haystack = f"{normalized_url} {link_text.lower()}"
        return sum(1 for kw in self.keywords if kw in haystack)

    def _score_page_text(self, text: str) -> int:
        lower = text.lower()
        return sum(1 for kw in self.keywords if kw in lower)

    def _same_domain(self, url: str) -> bool:
        return urlparse(url).netloc == self._domain

    def _fetch_page(self, url: str) -> str:
        if self._use_browser:
            self._browser_page.goto(url, timeout=self.timeout * 1000, wait_until="networkidle")
            return self._browser_page.content()
        response = requests.get(url, timeout=self.timeout, headers={"User-Agent": DEFAULT_USER_AGENT})
        response.raise_for_status()
        return response.text

    # -- discovery: find every relevant page, not just the single best one ------

    def _discover_candidate_pages(self) -> list[tuple[str, str]]:
        self._visited_count = 0
        candidates = self._sitemap_candidates()
        if not candidates:
            candidates = self._crawl_candidates()
        if not candidates:
            # Plain HTTP found nothing at all -- either the site blocked the
            # request outright (bot/WAF protection: BDO, PNB, Security Bank,
            # UnionBank all fail here) or it's a JS-rendered SPA that returns
            # an empty app shell without executing JavaScript (GCash,
            # ShopeePay, TayoCash). A real browser can help with either, so
            # retry the same discovery process through Playwright before
            # giving up. Optional dependency -- silently skipped if not
            # installed, same graceful-degradation pattern as screenshot capture.
            candidates = self._discover_with_browser()
        return candidates

    def _discover_with_browser(self) -> list[tuple[str, str]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return []

        self._visited_count = 0
        self._use_browser = True
        # Browser navigation is much slower per-page than a plain HTTP GET --
        # bound the worst-case CI time by capping this fallback pass to a
        # smaller page budget than the plain-HTTP discovery gets.
        original_max_pages = self.max_pages
        self.max_pages = min(self.max_pages, BROWSER_FALLBACK_MAX_PAGES)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    self._browser_page = browser.new_page(user_agent=DEFAULT_USER_AGENT)
                    candidates = self._sitemap_candidates()
                    if not candidates:
                        candidates = self._crawl_candidates()
                    return candidates
                finally:
                    browser.close()
        except Exception:
            return []  # browser launch/navigation failed -- fall through to "not found"
        finally:
            self.max_pages = original_max_pages
            self._use_browser = False
            self._browser_page = None

    def _locate_sitemaps(self) -> list[str]:
        sitemap_urls: list[str] = []
        try:
            robots_txt = self._fetch_page(urljoin(self.base_url, "/robots.txt"))
            sitemap_urls = [
                line.split(":", 1)[1].strip()
                for line in robots_txt.splitlines()
                if line.lower().startswith("sitemap:")
            ]
        except Exception:
            pass

        if not sitemap_urls:
            sitemap_urls = [urljoin(self.base_url, "/sitemap.xml")]
        return sitemap_urls

    def _collect_urls_from_sitemaps(self, sitemap_urls: list[str], seen: set[str] | None = None) -> list[str]:
        seen = seen if seen is not None else set()
        collected: list[str] = []

        for sitemap_url in sitemap_urls[:MAX_NESTED_SITEMAPS]:
            if sitemap_url in seen or len(collected) >= MAX_SITEMAP_URLS:
                continue
            seen.add(sitemap_url)
            try:
                root = ET.fromstring(self._fetch_page(sitemap_url))
            except Exception:
                continue

            root_tag = root.tag.split("}")[-1]
            locs = [el.text.strip() for el in root.iter() if el.tag.split("}")[-1] == "loc" and el.text]

            if root_tag == "sitemapindex":
                collected.extend(self._collect_urls_from_sitemaps(locs, seen))
            else:
                collected.extend(locs)

        return collected[:MAX_SITEMAP_URLS]

    def _sitemap_candidates(self) -> list[tuple[str, str]]:
        urls = self._collect_urls_from_sitemaps(self._locate_sitemaps())
        # Sorted by URL-keyword score first (cheap, no fetch needed), but not
        # filtered to score > 0 -- many real fee pages don't have keywords in
        # the URL itself, so ties are still worth fetching and content-scoring.
        ranked = sorted(
            (u for u in urls if self._same_domain(u)),
            key=lambda u: self._score_url(u),
            reverse=True,
        )[: self.max_pages]

        scored: list[tuple[int, dt.date, str, str]] = []
        for url in ranked:
            if self._visited_count >= self.max_pages:
                break
            try:
                html = self._fetch_page(url)
            except Exception:
                continue
            self._visited_count += 1

            text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
            score = self._score_page_text(text) + self._score_url(url)
            if score < MIN_RELEVANCE_SCORE:
                continue
            page_date = extract_page_date(html, text) or dt.date.min
            scored.append((score, page_date, url, html))

        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [(url, html) for _, _, url, html in scored[:MAX_CANDIDATE_PAGES]]

    def _crawl_candidates(self) -> list[tuple[str, str]]:
        queue: deque[tuple[str, int, int]] = deque([(self.base_url, 0, self._score_url(self.base_url))])
        seen = {self.base_url}
        scored: list[tuple[int, dt.date, str, str]] = []

        while queue and self._visited_count < self.max_pages:
            # Greedy best-first: re-sort so the most fee-looking link/page is
            # visited next, instead of exhausting a plain BFS on a large site.
            queue = deque(sorted(queue, key=lambda item: item[2], reverse=True))
            url, depth, _priority = queue.popleft()

            try:
                html = self._fetch_page(url)
            except Exception:
                continue
            self._visited_count += 1

            soup = BeautifulSoup(html, "html.parser")
            page_text = soup.get_text(" ", strip=True)
            score = self._score_page_text(page_text) + self._score_url(url)
            if score >= MIN_RELEVANCE_SCORE:
                page_date = extract_page_date(html, page_text) or dt.date.min
                scored.append((score, page_date, url, html))

            if depth < self.max_depth:
                for a in soup.find_all("a", href=True):
                    link = urljoin(url, a["href"]).split("#")[0]
                    if link in seen or not self._same_domain(link):
                        continue
                    seen.add(link)
                    queue.append((link, depth + 1, self._score_url(link, a.get_text(" ", strip=True))))

        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [(url, html) for _, _, url, html in scored[:MAX_CANDIDATE_PAGES]]

    # -- extraction ---------------------------------------------------------

    def _parse_page(self, source_url: str, page_text: str) -> list[FeeRecord]:
        conditions = extract_fee_conditions(self.entity, source_url, page_text)
        return [
            FeeRecord(
                entity=self.entity,
                network=c.network,
                fee_type=c.fee_type,
                amount=c.amount,
                conditions=format_conditions_text(c),
                effective_date=c.effective_date,
            )
            for c in conditions
        ]

    def run_multi(self) -> list[ScrapeResult]:
        candidates = self._discover_candidate_pages()
        if not candidates:
            return [ScrapeResult(
                entity=self.entity,
                source_url=self.base_url,
                source_type=self.source_type,
                fetched_at=utcnow_iso(),
                raw_content="",
                content_hash="",
                structure_hash=None,
                status="error",
                error_message=(
                    f"No fee-related page found after checking {self._visited_count} page(s) "
                    f"(sitemap + crawl) from {self.base_url} -- site may need an explicit "
                    f"URL/selectors instead of crawling"
                ),
            )]

        results = []
        for url, html in candidates:
            fetched_at = utcnow_iso()
            text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
            try:
                records = self._parse_page(url, text)
                status, error_message = "ok", None
            except Exception as exc:  # LLM/API failure must not crash the whole batch
                records, status, error_message = [], "error", str(exc)

            results.append(ScrapeResult(
                entity=self.entity,
                source_url=url,
                source_type=self.source_type,
                fetched_at=fetched_at,
                raw_content=html,
                content_hash=hash_text(html),
                structure_hash=structure_hash(html),
                fee_records=records,
                status=status,
                error_message=error_message,
            ))
        return results

    # -- BaseScraper interface compliance (run_multi() is the real entrypoint) --

    def fetch(self) -> str:
        candidates = self._discover_candidate_pages()
        if not candidates:
            raise NoFeePageFoundError(
                f"No fee-related page found after checking {self._visited_count} page(s) "
                f"from {self.base_url}"
            )
        self.source_url = candidates[0][0]
        return candidates[0][1]

    def parse(self, raw_content: str) -> list[FeeRecord]:
        text = BeautifulSoup(raw_content, "html.parser").get_text(" ", strip=True)
        return self._parse_page(self.source_url, text)
