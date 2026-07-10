"""Best-effort scraper for public Facebook page posts (fee announcements).

Known limitation (see README): Facebook aggressively blocks unauthenticated
scraping and changes markup often -- current testing shows mbasic.facebook.com
now returns a login wall ("Log in or sign up to view") even for well-known
official pages, so this is expected to find nothing in practice most of the
time. There is no reliable open-source way to read posts from a page without a
logged-in session or the paid Graph API. Failures/empty results here are
logged to scraper_health rather than crashing the whole run.

Uses the same LLM-based extraction as the website scrapers (see
scraper/llm_extract.py) -- if Facebook's access situation ever improves and
real post content comes back, this benefits from the same fee-structure
extraction (tiers, conditions, dates) without needing separate logic, and the
LLM correctly returns nothing for the login-wall boilerplate rather than
misparsing it.
"""
from __future__ import annotations

import re

import requests

from scraper.base import BaseScraper, FeeRecord
from scraper.llm_extract import extract_fee_conditions, format_conditions_text

DEFAULT_USER_AGENT = "transfer-fees-monitoring/0.1 (+open-source fee monitor)"


class FacebookPageScraper(BaseScraper):
    source_type = "facebook"

    def __init__(self, entity: str, page_url: str, timeout: int = 15):
        super().__init__(entity, page_url)
        self.timeout = timeout

    @staticmethod
    def _to_mbasic(url: str) -> str:
        return re.sub(r"https?://(www\.)?facebook\.com", "https://mbasic.facebook.com", url)

    def fetch(self) -> str:
        response = requests.get(
            self._to_mbasic(self.source_url),
            timeout=self.timeout,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        response.raise_for_status()
        return response.text

    def parse(self, raw_content: str) -> list[FeeRecord]:
        text = re.sub(r"<[^>]+>", " ", raw_content)
        text = re.sub(r"\s+", " ", text).strip()

        return [
            FeeRecord(
                entity=self.entity,
                network=condition.network,
                fee_type=condition.fee_type,
                amount=condition.amount,
                conditions=format_conditions_text(condition),
                effective_date=condition.effective_date,
            )
            for condition in extract_fee_conditions(self.entity, self.source_url, text)
        ]
