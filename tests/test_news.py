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
