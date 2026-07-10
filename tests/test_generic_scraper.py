from scraper.llm_extract import FeeCondition
from scraper.website.generic import GenericWebsiteScraper

SAMPLE_HTML = """
<html><body>
<div id="instapay-fee">InstaPay transfer fee: PHP 25.00 per transaction</div>
<div id="pesonet-fee">PESONet transfer fee: PHP 10.00 per transaction</div>
</body></html>
"""


def test_generic_scraper_extracts_amount_per_selector(monkeypatch):
    def fake_extract(entity, source_url, page_text):
        amount = 25.0 if "25.00" in page_text else 10.0
        return [FeeCondition(network="InstaPay", fee_type="flat", amount=amount, conditions="test")]

    monkeypatch.setattr("scraper.website.generic.extract_fee_conditions", fake_extract)

    scraper = GenericWebsiteScraper(
        entity="Test Bank",
        source_url="https://example.test/fees",
        selectors={"InstaPay": "#instapay-fee", "PESONet": "#pesonet-fee"},
    )

    records = scraper.parse(SAMPLE_HTML)
    by_network = {r.network: r for r in records}

    assert by_network["InstaPay"].amount == 25.0
    assert by_network["PESONet"].amount == 10.0


def test_generic_scraper_forces_network_from_selector_not_llm(monkeypatch):
    # Even if the LLM's own network guess is wrong/ambiguous, the selector
    # already tells us definitively which network this element is for.
    def fake_extract(entity, source_url, page_text):
        return [FeeCondition(network="InstaPay", fee_type="flat", amount=10.0, conditions="test")]

    monkeypatch.setattr("scraper.website.generic.extract_fee_conditions", fake_extract)

    scraper = GenericWebsiteScraper(
        entity="Test Bank",
        source_url="https://example.test/fees",
        selectors={"PESONet": "#pesonet-fee"},
    )

    records = scraper.parse(SAMPLE_HTML)

    assert records[0].network == "PESONet"


def test_generic_scraper_skips_missing_selector(monkeypatch):
    def fake_extract(entity, source_url, page_text):
        return [FeeCondition(network="InstaPay", fee_type="flat", amount=25.0, conditions="test")]

    monkeypatch.setattr("scraper.website.generic.extract_fee_conditions", fake_extract)

    scraper = GenericWebsiteScraper(
        entity="Test Bank",
        source_url="https://example.test/fees",
        selectors={"InstaPay": "#instapay-fee", "PESONet": "#does-not-exist"},
    )

    records = scraper.parse(SAMPLE_HTML)

    assert len(records) == 1
    assert records[0].network == "InstaPay"
