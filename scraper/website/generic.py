"""Config-driven scraper for institution fee pages where the InstaPay/PESONet
fee text is known to live in a specific, locatable element (CSS selector).

Selectors come from config/entities.yaml per entity -- this covers straightforward
fee pages without needing a bespoke scraper per bank. Sites that render fees via
JavaScript or need custom logic should get their own module under
scraper/website/ instead of forcing this generic one to handle everything.

Since the selector already tells us which network an element belongs to, the
network on each extracted FeeRecord is forced to match the selector's key
rather than trusted from the LLM's own output -- the LLM's job here is only to
read the fee structure (amount, conditions, tiers, dates) out of the selected
text, not to (re-)decide which network it's for.
"""
from __future__ import annotations

import requests
from bs4 import BeautifulSoup

from scraper.base import BaseScraper, FeeRecord
from scraper.llm_extract import extract_fee_conditions, format_conditions_text

DEFAULT_USER_AGENT = "transfer-fees-monitoring/0.1 (+open-source fee monitor)"


class GenericWebsiteScraper(BaseScraper):
    source_type = "website"

    def __init__(
        self,
        entity: str,
        source_url: str,
        selectors: dict[str, str],
        timeout: int = 15,
    ):
        super().__init__(entity, source_url)
        self.selectors = selectors  # e.g. {"InstaPay": "#instapay-fee", "PESONet": "#pesonet-fee"}
        self.timeout = timeout

    def fetch(self) -> str:
        response = requests.get(
            self.source_url,
            timeout=self.timeout,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        response.raise_for_status()
        return response.text

    def parse(self, raw_content: str) -> list[FeeRecord]:
        soup = BeautifulSoup(raw_content, "html.parser")
        records: list[FeeRecord] = []

        for network, selector in self.selectors.items():
            node = soup.select_one(selector)
            if node is None:
                continue
            text = node.get_text(" ", strip=True)
            hinted_text = f"[This text is specifically about {network}]\n{text}"

            for condition in extract_fee_conditions(self.entity, self.source_url, hinted_text):
                records.append(
                    FeeRecord(
                        entity=self.entity,
                        network=network,  # trust the selector, not the LLM's guess
                        fee_type=condition.fee_type,
                        amount=condition.amount,
                        conditions=format_conditions_text(condition),
                        effective_date=condition.effective_date,
                    )
                )
        return records
