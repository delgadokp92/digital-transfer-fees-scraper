from scraper.base import BaseScraper, FeeRecord, hash_text, structure_hash


def test_hash_text_deterministic_and_sensitive_to_content():
    assert hash_text("abc") == hash_text("abc")
    assert hash_text("abc") != hash_text("abd")


def test_structure_hash_ignores_text_changes():
    html_a = "<div class='fee'><span>PHP 25</span></div>"
    html_b = "<div class='fee'><span>PHP 30</span></div>"
    assert structure_hash(html_a) == structure_hash(html_b)


def test_structure_hash_detects_tag_changes():
    html_a = "<div class='fee'><span>PHP 25</span></div>"
    html_b = "<section class='fee'><span>PHP 25</span></section>"
    assert structure_hash(html_a) != structure_hash(html_b)


class _DummyScraper(BaseScraper):
    source_type = "website"

    def __init__(self, entity, source_url, raw, records):
        super().__init__(entity, source_url)
        self._raw = raw
        self._records = records

    def fetch(self):
        return self._raw

    def parse(self, raw_content):
        return self._records


class _FailingFetchScraper(BaseScraper):
    source_type = "website"

    def fetch(self):
        raise RuntimeError("network down")

    def parse(self, raw_content):
        return []


def test_run_returns_ok_result_with_hashes():
    records = [FeeRecord(entity="Test Bank", network="InstaPay", fee_type="flat", amount=25.0)]
    scraper = _DummyScraper("Test Bank", "https://example.test/fees", "<div>PHP 25</div>", records)

    result = scraper.run()

    assert result.status == "ok"
    assert result.fee_records == records
    assert result.content_hash
    assert result.structure_hash


def test_run_handles_fetch_error_without_raising():
    scraper = _FailingFetchScraper("Test Bank", "https://example.test/fees")

    result = scraper.run()

    assert result.status == "error"
    assert "network down" in result.error_message
    assert result.fee_records == []
