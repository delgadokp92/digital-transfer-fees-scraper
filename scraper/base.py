"""Shared scraper contract: fetch raw content, parse fees, and always compute the
provenance data (hashes) that feeds the audit trail and structure-drift detection.
"""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class FeeRecord:
    entity: str
    network: str  # "InstaPay" | "PESONet"
    fee_type: str  # "flat" | "tiered" | "percentage" | "free_threshold" | "unstructured"
    amount: float | None
    conditions: str | None = None
    effective_date: str | None = None
    promo_end_date: str | None = None


@dataclass
class ScrapeResult:
    entity: str
    source_url: str
    source_type: str  # "website" | "facebook"
    fetched_at: str
    raw_content: str
    content_hash: str
    structure_hash: str | None
    fee_records: list[FeeRecord] = field(default_factory=list)
    status: str = "ok"  # "ok" | "error"
    error_message: str | None = None


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def structure_skeleton(html: str) -> str:
    """Reduce HTML to its tag skeleton (tag names only, no text/attribute values).

    Used for structure-drift detection: fee amounts changing is expected and should
    NOT trip this, but a bank rearranging/renaming the page's markup should.
    """
    tags = re.findall(r"</?[a-zA-Z][a-zA-Z0-9]*", html)
    return "\n".join(tags)


def structure_hash(html: str) -> str:
    return hash_text(structure_skeleton(html))


class BaseScraper(ABC):
    source_type: str = "website"

    def __init__(self, entity: str, source_url: str):
        self.entity = entity
        self.source_url = source_url

    @abstractmethod
    def fetch(self) -> str:
        """Return raw content (HTML or text) fetched from source_url."""

    @abstractmethod
    def parse(self, raw_content: str) -> list[FeeRecord]:
        """Extract FeeRecords from raw content."""

    def run(self) -> ScrapeResult:
        fetched_at = utcnow_iso()
        try:
            raw = self.fetch()
        except Exception as exc:  # network/site failures must not crash the whole batch
            return ScrapeResult(
                entity=self.entity,
                source_url=self.source_url,
                source_type=self.source_type,
                fetched_at=fetched_at,
                raw_content="",
                content_hash="",
                structure_hash=None,
                status="error",
                error_message=str(exc),
            )

        content_hash = hash_text(raw)
        struct_hash = structure_hash(raw) if self.source_type == "website" else None

        try:
            records = self.parse(raw)
            status = "ok"
            error_message = None
        except Exception as exc:  # a parse failure is still archived, just flagged as an error
            records = []
            status = "error"
            error_message = str(exc)

        return ScrapeResult(
            entity=self.entity,
            source_url=self.source_url,
            source_type=self.source_type,
            fetched_at=fetched_at,
            raw_content=raw,
            content_hash=content_hash,
            structure_hash=struct_hash,
            fee_records=records,
            status=status,
            error_message=error_message,
        )

    def run_multi(self) -> list[ScrapeResult]:
        """Default: single-page scrapers just wrap run() in a list. Discovery
        scrapers that can find more than one genuinely relevant page (e.g.
        SiteCrawlerScraper) override this -- an institution can have more than
        one real, currently-relevant fee condition (a permanent rate change
        and a separate promo), each on its own page, and neither should be
        discarded in favor of "the one best page"."""
        return [self.run()]
