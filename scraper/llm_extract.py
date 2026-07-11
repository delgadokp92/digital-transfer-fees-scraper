"""LLM-based fee structure extraction.

Regex/keyword extraction (the previous approach in this project) required a
hand-rolled rule for every edge case discovered through live testing --
matching into comma-grouped transaction limits, magnitude words ("PHP 2
million"), a bare "free" false-positiving inside "Toll-Free", an
over-the-counter-vs-digital-channel state machine, and institutions with more
than one genuinely relevant fee condition at once (a permanent rate change
and a separate limited-time promo). Each fix solved one case and still left
the underlying approach fragile.

An LLM reading the actual page text handles all of those through prompt
instructions instead of one-off patterns, and -- the actual point of this
module -- captures the FULL fee structure (tiers, conditions, promo windows,
effective dates) rather than being forced to reduce everything to a single
number. Every institution can have zero, one, or several distinct fee
conditions extracted from a single page; none are discarded in favor of "the
best one".

Uses Claude Haiku: this is a classification/extraction task, not a hard
reasoning task, so the cheapest capable model is the right fit (roughly
$0.002-0.003 per page at typical article/page lengths). Requires
ANTHROPIC_API_KEY; callers should catch exceptions from this module and flag
the entity in scraper_health rather than crash the batch run, consistent with
every other scraper failure in this project.
"""
from __future__ import annotations

import re
import threading
from typing import Literal

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()  # picks up ANTHROPIC_API_KEY from a local .env file if present

MODEL = "claude-haiku-4-5"
MAX_PAGE_CHARS = 12000
KEYWORD_CONTEXT_MARGIN = 500  # chars of leading context kept before the first keyword hit

SYSTEM_PROMPT = """You extract InstaPay and PESONet digital fund-transfer fee information from text about a named Philippine bank or e-wallet -- either the institution's own web page/press release, or a third-party news/tech-blog article covering it.

Rules:
- The text may be a third-party news article that discusses SEVERAL institutions at once (e.g. "which banks now waive InstaPay fees: BDO, BPI, Metrobank..."). Only extract fee conditions clearly and explicitly attributed to the Institution named below -- ignore fees stated for any other institution mentioned in the same text, even in the same sentence or table.
- Only DIGITAL channel fees count (online banking, mobile app, mobile banking). Explicitly EXCLUDE over-the-counter (OTC) fees, ATM fees, RTGS fees, and fees for unrelated services.
- Do NOT confuse a transaction/daily limit (e.g. "up to PHP 50,000 per transaction"), a loan amount, a cashback amount, or any unrelated PHP figure with the actual transfer fee.
- Institutions can have MORE THAN ONE real, currently-relevant fee condition at once -- e.g. a permanent standing fee AND a separate limited-time promotional waiver for small transactions. Extract EACH as its own separate entry; do not merge them or discard one in favor of the other.
- fee_type is "flat" (fixed peso amount always charged), "free" (explicitly waived/zero), "tiered" (different amounts depending on transfer size or another condition), or "promo" (a limited-time offer, distinct from the institution's standing/permanent fee).
- conditions must be a clear, complete, human-readable description of exactly when this fee/condition applies (thresholds, channel, promo window, eligibility). This is the most important field -- do not leave it thin.
- effective_date: the date this fee took effect or was announced, in YYYY-MM-DD, if stated. promo_end_date: if this is a time-limited promo, its end date in YYYY-MM-DD, if stated. Use null when not stated in the text -- never guess or infer a date.
- Some pages describe a fund transfer fee to another institution's account (bank, e-wallet, or EMI) WITHOUT ever writing the words "InstaPay" or "PESONet". When that happens, deduce the network from characteristics the text itself states: InstaPay settles instantly/in real time and has a per-transaction limit of roughly PHP 50,000; PESONet is a batch rail that is NOT instant (same-day or next-banking-day credit) and allows materially higher transfer amounts. Base the deduction only on settlement-speed wording ("instant"/"real-time" vs. "next banking day"/"batch") or a stated limit that clearly matches one rail -- never on the fee amount itself, and never by assuming a default. If the text gives no such signal at all, leave that fee out rather than force a guess at which network it is.
- If the page does not actually state a genuine InstaPay or PESONet transfer fee (e.g. it's an unrelated announcement, a cashback/loan promo, or just describes what InstaPay is without giving a fee), return an empty list. Do not force an answer out of irrelevant content."""


class FeeCondition(BaseModel):
    network: Literal["InstaPay", "PESONet"]
    fee_type: Literal["flat", "free", "tiered", "promo"]
    amount: float | None = Field(
        default=None,
        description="Peso amount; null if fee_type is 'free' or the amount varies by tier and is described in conditions",
    )
    conditions: str = Field(
        description="Full human-readable description of when/how this applies, including thresholds and channel"
    )
    effective_date: str | None = Field(default=None, description="YYYY-MM-DD if stated, else null")
    promo_end_date: str | None = Field(
        default=None, description="YYYY-MM-DD if this is a time-limited promo, else null"
    )


class FeeExtractionResult(BaseModel):
    fees: list[FeeCondition]


# Safety net, not just prompting: live testing found the LLM does not reliably
# follow the "exclude OTC/ATM" instruction on pages that table multiple
# channels together (e.g. Bank of Commerce's fee schedule extracted a real PHP
# 100 OTC figure and a PHP 15 ATM figure despite the system prompt explicitly
# excluding both). Prompt instructions alone are not enough to guarantee this,
# so any condition whose own description names an excluded channel is dropped
# after extraction, regardless of what the model decided.
_EXCLUDED_CHANNEL_RE = re.compile(r"\b(otc|over-the-counter|over the counter|atm)\b", re.IGNORECASE)


def _mentions_excluded_channel(conditions: str) -> bool:
    return bool(_EXCLUDED_CHANNEL_RE.search(conditions or ""))


_client: anthropic.Anthropic | None = None
_client_lock = threading.Lock()


def _get_client() -> anthropic.Anthropic:
    # run_all.py now calls extract_fee_conditions concurrently from a thread
    # pool for news-source scraping -- guard first-call lazy init so two
    # threads racing here can't both construct a client.
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    return _client


def _trim_page_text(page_text: str) -> str:
    """A blind prefix truncation can cut off the actual fee content entirely if
    a page has a lot of nav/boilerplate text before it -- found via RCBC's
    fees-charges page, where the real fee table only started at character
    8442, past the previous fixed 8000-char cutoff. Instead, center the
    truncation window on wherever InstaPay/PESONet is first mentioned."""
    if len(page_text) <= MAX_PAGE_CHARS:
        return page_text

    lower = page_text.lower()
    positions = [p for p in (lower.find("instapay"), lower.find("pesonet")) if p != -1]
    if not positions:
        return page_text[:MAX_PAGE_CHARS]  # no keyword at all -- LLM will correctly find nothing

    start = max(0, min(positions) - KEYWORD_CONTEXT_MARGIN)
    return page_text[start:start + MAX_PAGE_CHARS]


def extract_fee_conditions(entity: str, source_url: str, page_text: str) -> list[FeeCondition]:
    trimmed = _trim_page_text(page_text)
    response = _get_client().messages.parse(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Institution: {entity}\nSource: {source_url}\n\nPage text:\n{trimmed}",
        }],
        output_format=FeeExtractionResult,
    )
    return [f for f in response.parsed_output.fees if not _mentions_excluded_channel(f.conditions)]


def format_conditions_text(condition: FeeCondition) -> str:
    if condition.promo_end_date:
        return f"{condition.conditions} (promo through {condition.promo_end_date})"
    return condition.conditions
