import pytest

from scraper.llm_extract import FeeCondition
from scraper.website.crawler import NoFeePageFoundError, SiteCrawlerScraper

FEE_SITE = {
    "https://bank.test/": '<html><body><a href="/about">About</a><a href="/fees">Fees</a></body></html>',
    "https://bank.test/about": "<html><body>We are a bank. Nothing relevant here.</body></html>",
    "https://bank.test/fees": (
        "<html><body>Schedule of fees. InstaPay transfer fee: PHP 25.00. "
        "PESONet transfer fee: PHP 10.00.</body></html>"
    ),
}

NO_FEE_SITE = {
    "https://bank.test/": "<html><body><a href='/about'>About</a></body></html>",
    "https://bank.test/about": "<html><body>We are a bank. No fee info here.</body></html>",
}

# Two equally-relevant pages (same keywords, same score) with different fee
# amounts and datelines -- mirrors a real case found in testing (BPI): a bank
# can have more than one genuine fee-related announcement at once (a permanent
# rate change and a separate limited-time promo), and both should be kept, not
# just whichever page happens to be discovered/scored first.
DUAL_ANNOUNCEMENT_SITE = {
    "https://bank3.test/": (
        '<html><body><a href="/news/old-promo">Old Promo</a>'
        '<a href="/news/new-promo">New Promo</a></body></html>'
    ),
    "https://bank3.test/news/old-promo": (
        "<html><body>Jan 01, 2024. Schedule of fees. "
        "InstaPay transfer fee: PHP 25.00.</body></html>"
    ),
    "https://bank3.test/news/new-promo": (
        "<html><body>Jan 01, 2026. Schedule of fees. "
        "InstaPay transfer fee: PHP 5.00.</body></html>"
    ),
}

# Homepage has NO link to the fee page -- only discoverable via sitemap.xml,
# so this only passes if sitemap discovery actually ran (a plain link crawl
# would never find it).
SITEMAP_SITE = {
    "https://bank2.test/": "<html><body>Homepage with no useful links.</body></html>",
    "https://bank2.test/robots.txt": "User-agent: *\nSitemap: https://bank2.test/sitemap.xml\n",
    "https://bank2.test/sitemap.xml": (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://bank2.test/about</loc></url>"
        "<url><loc>https://bank2.test/fees-and-charges</loc></url>"
        "</urlset>"
    ),
    "https://bank2.test/about": "<html><body>About us.</body></html>",
    "https://bank2.test/fees-and-charges": (
        "<html><body>Fees and charges. InstaPay transfer fee: PHP 25.00. "
        "PESONet transfer fee: PHP 10.00.</body></html>"
    ),
}


class _FakeScraper(SiteCrawlerScraper):
    """Overrides the network call so crawl/discovery logic can be tested without HTTP."""

    def __init__(self, site: dict[str, str], **kwargs):
        super().__init__(**kwargs)
        self._site = site

    def _fetch_page(self, url: str) -> str:
        if url not in self._site:
            raise RuntimeError("404")
        return self._site[url]


# -- discovery tests (no LLM involved -- these test which pages get found) ---


def test_crawler_finds_the_fee_page_over_the_homepage():
    scraper = _FakeScraper(FEE_SITE, entity="Bank", base_url="https://bank.test/")

    candidates = scraper._discover_candidate_pages()

    assert [url for url, _ in candidates] == ["https://bank.test/fees"]


def test_crawler_raises_when_no_page_is_relevant_enough():
    scraper = _FakeScraper(NO_FEE_SITE, entity="Bank", base_url="https://bank.test/")

    with pytest.raises(NoFeePageFoundError):
        scraper.fetch()


def test_crawler_uses_sitemap_when_no_link_path_exists():
    scraper = _FakeScraper(SITEMAP_SITE, entity="Bank2", base_url="https://bank2.test/")

    candidates = scraper._discover_candidate_pages()

    assert [url for url, _ in candidates] == ["https://bank2.test/fees-and-charges"]


def test_crawler_discovers_all_relevant_pages_ordered_by_recency():
    scraper = _FakeScraper(DUAL_ANNOUNCEMENT_SITE, entity="Bank3", base_url="https://bank3.test/")

    candidates = scraper._discover_candidate_pages()
    urls = [url for url, _ in candidates]

    assert urls[0] == "https://bank3.test/news/new-promo"
    assert "https://bank3.test/news/old-promo" in urls


# -- run_multi() tests (LLM call mocked -- these test our orchestration glue) --


def test_run_multi_wires_llm_output_into_fee_records(monkeypatch):
    def fake_extract(entity, source_url, page_text):
        return [
            FeeCondition(network="InstaPay", fee_type="flat", amount=25.0, conditions="Flat fee"),
            FeeCondition(network="PESONet", fee_type="flat", amount=10.0, conditions="Flat fee"),
        ]

    monkeypatch.setattr("scraper.website.crawler.extract_fee_conditions", fake_extract)
    scraper = _FakeScraper(FEE_SITE, entity="Bank", base_url="https://bank.test/")

    results = scraper.run_multi()

    assert len(results) == 1
    assert results[0].status == "ok"
    by_network = {r.network: r for r in results[0].fee_records}
    assert by_network["InstaPay"].amount == 25.0
    assert by_network["PESONet"].amount == 10.0


def test_run_multi_keeps_facts_from_every_candidate_page_not_just_one(monkeypatch):
    # This is the core behavior change: BPI-style multi-announcement
    # institutions must not have one condition discarded in favor of another.
    def fake_extract(entity, source_url, page_text):
        if "new-promo" in source_url:
            return [FeeCondition(network="InstaPay", fee_type="flat", amount=5.0, conditions="New rate")]
        return [FeeCondition(network="InstaPay", fee_type="flat", amount=25.0, conditions="Old rate")]

    monkeypatch.setattr("scraper.website.crawler.extract_fee_conditions", fake_extract)
    scraper = _FakeScraper(DUAL_ANNOUNCEMENT_SITE, entity="Bank3", base_url="https://bank3.test/")

    results = scraper.run_multi()

    amounts_by_url = {r.source_url: r.fee_records[0].amount for r in results}
    assert amounts_by_url["https://bank3.test/news/new-promo"] == 5.0
    assert amounts_by_url["https://bank3.test/news/old-promo"] == 25.0


def test_run_multi_returns_flagged_error_result_when_no_page_found():
    scraper = _FakeScraper(NO_FEE_SITE, entity="Bank", base_url="https://bank.test/")

    results = scraper.run_multi()

    assert len(results) == 1
    assert results[0].status == "error"
    assert results[0].fee_records == []
