"""Third-party news/tech-blog coverage of institution fee changes.

Supplements (never replaces) each institution's own website/press
releases/Facebook page -- the project's primary-source rule stays intact for
every source that IS reachable. This exists because some institutions' own
sites are unreachable from here at all (BDO: confirmed network/WAF-level
block -- see README "Known limitations") while their fee changes still get
covered by outlets like astig.ph, so relying solely on official-channel
scraping leaves a real gap: astig.ph and Tech Pilipinas both had a direct,
on-topic hit for BDO's InstaPay/PESONet fee waiver when checked manually.

Each configured outlet (config/news_sources.yaml) is a plain on-site search:
the institution's name plus InstaPay/PESONet/fee keywords is substituted into
that outlet's own search URL, then the results page is crawled/scored exactly
like scraper/website/crawler.py crawls an institution's own site -- same
keyword-scoring, same Playwright fallback when plain HTTP finds nothing (a
JS-rendered search-results page, e.g. GMA News). The one addition: a
candidate page is discarded unless the institution's own name literally
appears in it too, since an outlet's "related articles" widget can surface a
completely different institution's fee news right next to the real hit.

Because a single article can discuss several institutions at once (unlike an
institution's own site, which is inherently about only itself), the LLM
prompt (scraper/llm_extract.py) is told to only attribute a fee to the
institution named in the request, ignoring any other bank/e-wallet mentioned
in the same text.
"""
from __future__ import annotations

import pathlib
import re
from urllib.parse import quote_plus, urlparse

import yaml

from scraper.website.crawler import DEFAULT_KEYWORDS, SiteCrawlerScraper

CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "news_sources.yaml"
NEWS_MAX_CANDIDATE_PAGES = 3  # tighter than a site's own crawler (8) -- an
                              # outlet's own search already pre-filters

# WordPress-style archive/taxonomy listing pages (category/tag/author/page-N,
# but also arbitrary custom taxonomies -- confirmed live via BDO/astig.ph:
# /brand/realme/, /brand/asus/, etc. are sitewide nav present on nearly every
# page). These often excerpt enough of a real article's text to pass both the
# keyword score AND the entity-name filter, producing a duplicate "source"
# for the same underlying article. Two independent, cheap signals catch this
# without needing a per-outlet allow/block list: a known standard-taxonomy
# path segment, or a slug that's too short to be a real headline (a genuine
# article slug on every outlet checked so far is a multi-word phrase --
# "bdo-unibank-instapay-pesonet-free-transfer-fee-free" -- while a taxonomy
# term is one or two words -- "realme", "smartphones").
_ARCHIVE_URL_RE = re.compile(r"/(category|tag|author|page)/", re.IGNORECASE)
_MIN_SLUG_HYPHENS = 3


def _looks_like_an_article_slug(url: str) -> bool:
    path = urlparse(url).path.strip("/")
    if not path:
        return False
    last_segment = path.rsplit("/", 1)[-1]
    return last_segment.count("-") >= _MIN_SLUG_HYPHENS


def load_news_sources() -> list[dict]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["sources"]


class NewsSearchScraper(SiteCrawlerScraper):
    """Crawls one news outlet's own search-results page for a given
    institution's fee coverage, reusing SiteCrawlerScraper's keyword-scoring
    and Playwright fallback -- just seeded from a search URL instead of the
    institution's own homepage, with sitemap discovery skipped (a news
    outlet's domain-wide sitemap has nothing to do with this specific
    search) and an extra entity-name filter on top."""

    source_type = "website"  # fetched over plain HTTP like any other page;
                              # "official vs. third-party" is a config-level
                              # distinction (config/news_sources.yaml), not a
                              # database one -- see app.py's Sources tab.

    def __init__(self, entity: str, outlet_name: str, search_url_template: str, aliases: list[str] | None = None):
        # News coverage almost never uses an institution's full legal name
        # ("BDO Unibank, Inc.") -- search on its well-known short name when
        # one is configured (config/entities.yaml `aliases`), since the full
        # legal name returns poor/no results on most outlets' own search.
        search_name = aliases[0] if aliases else entity
        query = quote_plus(f"{search_name} InstaPay PESONet transfer fee")
        search_url = search_url_template.format(query=query)
        super().__init__(
            entity=entity,
            base_url=search_url,
            keywords=DEFAULT_KEYWORDS + [search_name.lower()],
            max_pages=15,
            max_depth=1,
        )
        self.outlet_name = outlet_name
        # Same reasoning applies to the entity-mention filter below: an
        # article says "BDO", not "BDO Unibank, Inc." -- match on any known
        # name for this institution, not just its full legal name.
        self._match_names = [entity.lower()] + [a.lower() for a in (aliases or [])]

    def _sitemap_candidates(self) -> list[tuple[str, str]]:
        return []

    def _discover_with_browser(self) -> list[tuple[str, str]]:
        # A real Chromium launch+crawl per (entity, outlet) combo is what a
        # site-owner's own crawl needs when its site is a genuine SPA/WAF
        # block -- but here it just means "this outlet has no coverage of
        # this institution," which is true for most institutions on most
        # outlets. Confirmed live: with 6 outlets x ~40 entities, this
        # fallback firing on every zero-hit search ran the scheduled workflow
        # past GitHub's hard 6-hour ceiling, which kills the job with no
        # grace period -- losing the entire run's data (see run_all.py /
        # scrape.yml). News coverage is supplementary/best-effort, not worth
        # that cost -- skip it and accept "not found" from plain HTTP alone.
        return []

    def _discover_candidate_pages(self) -> list[tuple[str, str]]:
        candidates = super()._discover_candidate_pages()
        # The search-results page's own URL always contains the query
        # keywords, so it routinely clears the relevance score on URL alone
        # -- exclude it explicitly rather than waste a candidate slot on a
        # listing page that was never going to state a fee figure itself.
        return [
            (url, html) for url, html in candidates
            if url != self.base_url
            and not _ARCHIVE_URL_RE.search(url)
            and _looks_like_an_article_slug(url)
            and any(name in html.lower() for name in self._match_names)
        ][:NEWS_MAX_CANDIDATE_PAGES]
