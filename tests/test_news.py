from unittest.mock import patch

from scraper.llm_extract import FeeCondition
from scraper.news import NewsSearchScraper


class _FakeNewsScraper(NewsSearchScraper):
    """Overrides the network call so crawl/discovery logic can be tested without HTTP."""

    def __init__(self, site: dict[str, str], **kwargs):
        super().__init__(**kwargs)
        self._site = site

    def _fetch_page(self, url: str) -> str:
        if url not in self._site:
            raise RuntimeError("404")
        return self._site[url]

    def _discover_with_browser(self):
        return []  # no real Playwright in tests -- plain HTTP only


def test_search_url_is_built_from_the_template_and_entity_name():
    scraper = _FakeNewsScraper(
        {}, entity="Test Bank", outlet_name="Astig.PH", search_url_template="https://astig.ph/?s={query}",
    )

    assert scraper.base_url == "https://astig.ph/?s=Test+Bank+InstaPay+PESONet+transfer+fee"
    assert scraper.outlet_name == "Astig.PH"


def test_search_query_and_matching_use_the_alias_not_the_full_legal_name():
    # News coverage says "BDO", never "BDO Unibank, Inc." -- both the search
    # query and the entity-mention filter must use the configured alias.
    search_url = "https://astig.ph/?s=BDO+InstaPay+PESONet+transfer+fee"
    site = {
        search_url: '<html><body><a href="/bdo-instapay-fee-free/">BDO waives InstaPay fee</a></body></html>',
        "https://astig.ph/bdo-instapay-fee-free/": (
            "<html><body>BDO now waives its InstaPay and PESONet transfer fees "
            "for all digital transactions.</body></html>"
        ),
    }
    scraper = _FakeNewsScraper(
        site, entity="BDO Unibank, Inc.", outlet_name="Astig.PH",
        search_url_template="https://astig.ph/?s={query}", aliases=["BDO"],
    )

    assert scraper.base_url == search_url
    candidates = scraper._discover_candidate_pages()
    urls = [url for url, _ in candidates]
    assert urls == ["https://astig.ph/bdo-instapay-fee-free/"]


def test_wordpress_archive_pages_are_excluded_even_if_they_score():
    # Confirmed live against astig.ph/BDO: a category/tag archive page can
    # excerpt enough of the real article to pass both the keyword score and
    # the entity-name filter, producing a duplicate "source" for one article.
    search_url = "https://astig.ph/?s=BDO+InstaPay+PESONet+transfer+fee"
    site = {
        search_url: (
            '<html><body>'
            '<a href="/bdo-instapay-fee-free/">BDO waives InstaPay fee</a>'
            '<a href="/category/technology/">Technology</a>'
            '<a href="/tag/smartphones/">Smartphones</a>'
            '</body></html>'
        ),
        "https://astig.ph/bdo-instapay-fee-free/": (
            "<html><body>BDO now waives its InstaPay and PESONet transfer fees "
            "for all digital transactions.</body></html>"
        ),
        "https://astig.ph/category/technology/": (
            "<html><body>BDO InstaPay PESONet transfer fees are now free via digital "
            "channels. Read more tech news.</body></html>"
        ),
        "https://astig.ph/tag/smartphones/": (
            "<html><body>BDO InstaPay PESONet transfer fees are now free. "
            "Smartphone reviews and news.</body></html>"
        ),
    }
    scraper = _FakeNewsScraper(
        site, entity="BDO Unibank, Inc.", outlet_name="Astig.PH",
        search_url_template="https://astig.ph/?s={query}", aliases=["BDO"],
    )

    candidates = scraper._discover_candidate_pages()

    urls = [url for url, _ in candidates]
    assert urls == ["https://astig.ph/bdo-instapay-fee-free/"]


def test_short_taxonomy_slugs_are_excluded_even_outside_the_known_paths():
    # Confirmed live against astig.ph/BDO: custom taxonomies like /brand/realme/
    # aren't caught by the category/tag/author/page path check, but their
    # slug is too short to be a real article headline.
    search_url = "https://astig.ph/?s=BDO+InstaPay+PESONet+transfer+fee"
    site = {
        search_url: (
            '<html><body>'
            '<a href="/bdo-instapay-fee-free/">BDO waives InstaPay fee</a>'
            '<a href="/brand/realme/">Realme</a>'
            '</body></html>'
        ),
        "https://astig.ph/bdo-instapay-fee-free/": (
            "<html><body>BDO now waives its InstaPay and PESONet transfer fees "
            "for all digital transactions.</body></html>"
        ),
        "https://astig.ph/brand/realme/": (
            "<html><body>BDO InstaPay PESONet transfer fees are now free. "
            "Realme smartphone news and reviews.</body></html>"
        ),
    }
    scraper = _FakeNewsScraper(
        site, entity="BDO Unibank, Inc.", outlet_name="Astig.PH",
        search_url_template="https://astig.ph/?s={query}", aliases=["BDO"],
    )

    candidates = scraper._discover_candidate_pages()

    urls = [url for url, _ in candidates]
    assert urls == ["https://astig.ph/bdo-instapay-fee-free/"]


def test_sitemap_discovery_is_always_skipped():
    # Even if a sitemap exists on the outlet's domain, it's irrelevant to this
    # specific institution's search and must never be consulted.
    site = {
        "https://astig.ph/?s=Bank+InstaPay+PESONet+transfer+fee": "<html><body>No links here.</body></html>",
        "https://astig.ph/robots.txt": "User-agent: *\nSitemap: https://astig.ph/sitemap.xml\n",
        "https://astig.ph/sitemap.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://astig.ph/bank-instapay-pesonet-fee-free/</loc></url>"
            "</urlset>"
        ),
    }
    scraper = _FakeNewsScraper(
        site, entity="Bank", outlet_name="Astig.PH", search_url_template="https://astig.ph/?s={query}",
    )

    assert scraper._sitemap_candidates() == []
    assert scraper._discover_candidate_pages() == []


def test_candidate_must_mention_the_target_entity_by_name():
    search_url = "https://astig.ph/?s=Test+Bank+InstaPay+PESONet+transfer+fee"
    site = {
        search_url: (
            '<html><body>'
            '<a href="/other-bank-instapay-fee-free/">Other Bank waives InstaPay fee</a>'
            '<a href="/test-bank-instapay-fee-free/">Test Bank waives InstaPay fee</a>'
            '</body></html>'
        ),
        # A related-articles widget hit for a DIFFERENT institution -- must be
        # excluded even though it clears the generic keyword score.
        "https://astig.ph/other-bank-instapay-fee-free/": (
            "<html><body>Other Bank now waives its InstaPay and PESONet transfer fees "
            "for all digital transactions.</body></html>"
        ),
        "https://astig.ph/test-bank-instapay-fee-free/": (
            "<html><body>Test Bank now waives its InstaPay and PESONet transfer fees "
            "for all digital transactions.</body></html>"
        ),
    }
    scraper = _FakeNewsScraper(
        site, entity="Test Bank", outlet_name="Astig.PH", search_url_template="https://astig.ph/?s={query}",
    )

    candidates = scraper._discover_candidate_pages()

    urls = [url for url, _ in candidates]
    assert urls == ["https://astig.ph/test-bank-instapay-fee-free/"]


def test_parse_page_drops_a_condition_that_names_a_different_real_institution():
    # Confirmed live across 7 entities (BPI, China Bank, EastWest, LandBank,
    # Metrobank, PNB, GXI/GCash): a "related articles" sidebar or a
    # multi-bank roundup article can make an unrelated institution's name
    # appear on the page, letting it through the discovery-time entity
    # filter -- and the LLM then sometimes mislabels that page's real content
    # (about a DIFFERENT institution) as belonging to the one being searched
    # for. Here the extracted condition's own text explicitly names "LandBank"
    # -- a different configured institution's own alias -- so it must be
    # dropped regardless of what the (mocked) LLM returned.
    scraper = _FakeNewsScraper(
        {}, entity="G-Xchange, Incorporated (GXI)", outlet_name="YugaTech",
        search_url_template="https://www.yugatech.com/?s={query}", aliases=["GCash"],
    )

    with patch(
        "scraper.website.crawler.extract_fee_conditions",
        return_value=[
            FeeCondition(
                network="InstaPay", fee_type="free", amount=None,
                conditions=(
                    "InstaPay transfers sent through digital channels (LandBank and "
                    "Overseas Filipino Bank online banking/mobile app)"
                ),
                effective_date="2026-07-07", promo_end_date=None,
            )
        ],
    ):
        records = scraper._parse_page(
            "https://www.yugatech.com/landbank-to-remove-instapay-and-pesonet-transfer-fees/",
            "LandBank removes InstaPay and PESONet transfer fees. GCash cuts InstaPay fee (related article).",
        )

    assert records == []


def test_parse_page_keeps_a_condition_that_uses_an_unlisted_product_brand():
    # Confirmed live: EastWest's own real fee-waiver article got its
    # extracted condition worded as "EasyWay and Komo digital platforms" --
    # EastWest's actual app/product brands, not the word "EastWest" itself,
    # and neither is a configured alias. An earlier version of this filter
    # required the entity/alias to appear in the condition text and wrongly
    # deleted this and five other legitimate rows. The correct check is
    # whether a DIFFERENT institution is named, not whether this one is.
    scraper = _FakeNewsScraper(
        {}, entity="East West Banking Corporation", outlet_name="Astig.PH",
        search_url_template="https://astig.ph/?s={query}", aliases=["EastWest"],
    )

    with patch(
        "scraper.website.crawler.extract_fee_conditions",
        return_value=[
            FeeCondition(
                network="InstaPay", fee_type="free", amount=None,
                conditions="InstaPay transfers made through EasyWay and Komo digital platforms",
                effective_date="2026-07-15", promo_end_date=None,
            )
        ],
    ):
        records = scraper._parse_page(
            "https://astig.ph/eastwest-waives-instapay-pesonet-fees/",
            "EastWest is waiving InstaPay fees on EasyWay and Komo starting July 15.",
        )

    assert len(records) == 1
    assert records[0].entity == "East West Banking Corporation"


def test_parse_page_keeps_a_condition_that_names_the_entity_or_its_alias():
    scraper = _FakeNewsScraper(
        {}, entity="G-Xchange, Incorporated (GXI)", outlet_name="YugaTech",
        search_url_template="https://www.yugatech.com/?s={query}", aliases=["GCash"],
    )

    with patch(
        "scraper.website.crawler.extract_fee_conditions",
        return_value=[
            FeeCondition(
                network="InstaPay", fee_type="flat", amount=10.0,
                conditions="InstaPay bank transfer through the GCash mobile app",
                effective_date="2026-07-04", promo_end_date=None,
            )
        ],
    ):
        records = scraper._parse_page(
            "https://www.yugatech.com/gcash-cuts-instapay-bank-transfer-fee/",
            "GCash cuts InstaPay bank transfer fee from PHP 15 to PHP 10.",
        )

    assert len(records) == 1
    assert records[0].entity == "G-Xchange, Incorporated (GXI)"


def test_discovery_does_not_crawl_past_the_search_results_page():
    search_url = "https://astig.ph/?s=Test+Bank+InstaPay+PESONet+transfer+fee"
    site = {
        search_url: '<html><body><a href="/test-bank-instapay-fee-free/">Test Bank InstaPay fee</a></body></html>',
        "https://astig.ph/test-bank-instapay-fee-free/": (
            "<html><body>Test Bank InstaPay PESONet transfer fee waived. "
            '<a href="/unrelated-deep-link/">Read more</a></body></html>'
        ),
        # Only reachable by following a link FROM the article page (depth 2) --
        # must never be visited since max_depth=1 for news search.
        "https://astig.ph/unrelated-deep-link/": (
            "<html><body>Test Bank InstaPay PESONet transfer fee unrelated deep page.</body></html>"
        ),
    }
    scraper = _FakeNewsScraper(
        site, entity="Test Bank", outlet_name="Astig.PH", search_url_template="https://astig.ph/?s={query}",
    )

    candidates = scraper._discover_candidate_pages()

    urls = [url for url, _ in candidates]
    assert "https://astig.ph/unrelated-deep-link/" not in urls
