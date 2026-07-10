from scraper.llm_extract import (
    MAX_PAGE_CHARS,
    FeeCondition,
    _mentions_excluded_channel,
    _trim_page_text,
    format_conditions_text,
)


def test_format_conditions_text_appends_promo_end_date():
    condition = FeeCondition(
        network="InstaPay",
        fee_type="promo",
        amount=0.0,
        conditions="Free for transfers up to PHP 1,000",
        effective_date="2025-07-05",
        promo_end_date="2025-09-30",
    )

    assert format_conditions_text(condition) == "Free for transfers up to PHP 1,000 (promo through 2025-09-30)"


def test_format_conditions_text_without_promo_end_date():
    condition = FeeCondition(
        network="PESONet",
        fee_type="flat",
        amount=10.0,
        conditions="Flat PHP 10 fee via online/mobile banking",
    )

    assert format_conditions_text(condition) == "Flat PHP 10 fee via online/mobile banking"


def test_trim_page_text_centers_on_keyword_instead_of_blind_prefix():
    # Mirrors the real bug found via RCBC: a page with a lot of leading
    # boilerplate whose real fee content only starts well past a naive
    # from-the-start truncation cutoff.
    boilerplate = "Nav item. " * 2000  # far longer than MAX_PAGE_CHARS on its own
    real_content = "InstaPay transfer fee: PHP 10.00 per transaction."
    page_text = boilerplate + real_content

    trimmed = _trim_page_text(page_text)

    assert "InstaPay" in trimmed
    assert len(trimmed) <= MAX_PAGE_CHARS


def test_trim_page_text_leaves_short_pages_untouched():
    page_text = "InstaPay transfer fee: PHP 10.00 per transaction."

    assert _trim_page_text(page_text) == page_text


def test_mentions_excluded_channel_catches_otc_and_atm():
    # Real case found in testing: Bank of Commerce's LLM output described an
    # OTC/ATM figure despite the system prompt explicitly excluding both --
    # this safety net catches what the prompt alone didn't.
    assert _mentions_excluded_channel("Over-the-counter (OTC) channel")
    assert _mentions_excluded_channel("ATM channel")
    assert _mentions_excluded_channel("Available via OTC or online banking")


def test_mentions_excluded_channel_does_not_false_positive_on_digital_channels():
    assert not _mentions_excluded_channel("Internet Banking (RIB) / Online Banking channel")
    assert not _mentions_excluded_channel("Via BPI app and BPI online banking")
