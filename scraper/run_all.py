"""Orchestrator: loads config/entities.yaml, runs the applicable scraper(s) per
entity, and persists results into storage/fees.db -- fee_snapshots, audit_log
(the provenance trail), and scraper_health (structure-drift flags).

Usage:
    python -m scraper.run_all
    ENABLE_SCREENSHOTS=1 python -m scraper.run_all   # also capture a screenshot
                                                       # when a fee value changes
                                                       # (requires: pip install playwright
                                                       # && playwright install chromium)
    DISABLE_NEWS_SOURCES=1 python -m scraper.run_all # skip config/news_sources.yaml
                                                       # (third-party news/tech-blog
                                                       # coverage) -- on by default,
                                                       # adds ~6x the LLM page-extraction
                                                       # calls (one search per entity per
                                                       # configured outlet); see README Cost
"""
from __future__ import annotations

import concurrent.futures
import os
import pathlib

import yaml

from scraper.base import ScrapeResult
from scraper.facebook import FacebookPageScraper
from scraper.news import NewsSearchScraper, load_news_sources
from scraper.website.crawler import SiteCrawlerScraper
from scraper.website.generic import GenericWebsiteScraper
from storage import db

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "entities.yaml"
SNAPSHOT_DIR = ROOT_DIR / "storage" / "snapshots"
NEWS_SOURCES = load_news_sources()
NEWS_WORKERS = int(os.environ.get("NEWS_WORKERS", "10"))


def load_entities() -> list[dict]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["entities"]


def build_scrapers(entity_cfg: dict) -> list:
    scrapers = []

    website_cfg = entity_cfg.get("website")
    if website_cfg:
        mode = website_cfg.get("mode", "fixed")
        if mode == "crawl":
            scrapers.append(
                SiteCrawlerScraper(
                    entity=entity_cfg["name"],
                    base_url=website_cfg["base_url"],
                    keywords=website_cfg.get("keywords"),
                    max_pages=website_cfg.get("max_pages", 25),
                    max_depth=website_cfg.get("max_depth", 2),
                )
            )
        elif mode == "fixed":
            scrapers.append(
                GenericWebsiteScraper(
                    entity=entity_cfg["name"],
                    source_url=website_cfg["url"],
                    selectors=website_cfg["selectors"],
                )
            )
        else:
            raise ValueError(f"Unknown website mode '{mode}' for entity '{entity_cfg['name']}'")

    facebook_cfg = entity_cfg.get("facebook")
    if facebook_cfg:
        scrapers.append(
            FacebookPageScraper(
                entity=entity_cfg["name"],
                page_url=facebook_cfg["page_url"],
            )
        )

    # Checked for every entity, not just ones whose own site is blocked -- a
    # fee change can be reported here before (or instead of) an institution
    # updating its own site (see scraper/news.py).
    if not os.environ.get("DISABLE_NEWS_SOURCES"):
        for source in NEWS_SOURCES:
            scrapers.append(
                NewsSearchScraper(
                    entity=entity_cfg["name"],
                    outlet_name=source["name"],
                    search_url_template=source["search_url_template"],
                    aliases=entity_cfg.get("aliases"),
                )
            )

    return scrapers


def _safe_path_component(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in value)


def save_snapshot(entity: str, source_type: str, fetched_at: str, raw_content: str) -> str:
    entity_dir = SNAPSHOT_DIR / _safe_path_component(entity)
    entity_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{source_type}_{_safe_path_component(fetched_at)}.html"
    path = entity_dir / filename
    path.write_text(raw_content, encoding="utf-8")
    return str(path.relative_to(ROOT_DIR))


def capture_screenshot(url: str, out_path: pathlib.Path) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  (screenshot skipped: playwright not installed)")
        return False
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url, timeout=20000)
            page.screenshot(path=str(out_path), full_page=True)
            browser.close()
        return True
    except Exception as exc:
        print(f"  (screenshot capture failed for {url}: {exc})")
        return False


def process_result(conn, result: ScrapeResult, enable_screenshots: bool) -> None:
    snapshot_path = None
    if result.raw_content:
        snapshot_path = save_snapshot(result.entity, result.source_type, result.fetched_at, result.raw_content)

    last_structure_hash = db.get_last_structure_hash(conn, result.entity, result.source_type)
    flagged = False
    flagged_reason = None

    if result.status == "error":
        flagged = True
        flagged_reason = result.error_message
    elif result.structure_hash and last_structure_hash and result.structure_hash != last_structure_hash:
        flagged = True
        flagged_reason = "Website structure changed since last successful scrape -- verify scraper still parses correctly"

    audit_id = db.insert_audit_log(
        conn,
        entity=result.entity,
        source_url=result.source_url,
        source_type=result.source_type,
        fetched_at=result.fetched_at,
        content_hash=result.content_hash,
        structure_hash=result.structure_hash,
        snapshot_path=snapshot_path,
        screenshot_path=None,
        status="ok" if result.status == "ok" else "error",
        error_message=result.error_message,
    )

    any_fee_changed = False
    for record in result.fee_records:
        previous = db.get_last_fee(conn, record.entity, record.network)
        changed = previous is None or (previous["amount"], previous["conditions"]) != (record.amount, record.conditions)
        any_fee_changed = any_fee_changed or changed

        db.insert_fee_snapshot(
            conn,
            entity=record.entity,
            network=record.network,
            fee_type=record.fee_type,
            amount=record.amount,
            conditions=record.conditions,
            effective_date=record.effective_date,
            scraped_at=result.fetched_at,
            audit_id=audit_id,
            promo_end_date=record.promo_end_date,
        )

    if enable_screenshots and any_fee_changed and result.source_type == "website":
        screenshot_path = SNAPSHOT_DIR / _safe_path_component(result.entity) / f"website_{_safe_path_component(result.fetched_at)}.png"
        if capture_screenshot(result.source_url, screenshot_path):
            db.update_audit_screenshot(conn, audit_id, str(screenshot_path.relative_to(ROOT_DIR)))

    db.upsert_scraper_health(
        conn,
        entity=result.entity,
        source_type=result.source_type,
        last_ok_at=result.fetched_at if result.status == "ok" else None,
        last_structure_hash=result.structure_hash or last_structure_hash,
        last_source_url=result.source_url if result.status == "ok" else None,
        flagged=flagged,
        flagged_reason=flagged_reason,
    )


def _print_result(result: ScrapeResult) -> None:
    print(f"[{result.status.upper()}] {result.entity} ({result.source_type}, {result.source_url}) -> {len(result.fee_records)} fee record(s)")


def main() -> None:
    enable_screenshots = bool(os.environ.get("ENABLE_SCREENSHOTS"))
    entities = load_entities()

    conn = db.get_connection()
    db.init_db(conn)

    official_scrapers = []
    news_scrapers = []
    for entity_cfg in entities:
        for scraper in build_scrapers(entity_cfg):
            (news_scrapers if isinstance(scraper, NewsSearchScraper) else official_scrapers).append(scraper)

    # Official-site/Facebook scrapers run sequentially: a handful of them can
    # fall back to a real Playwright browser (scraper/website/crawler.py),
    # whose sync API is not safe to invoke from worker threads. There are only
    # ~18-30 of these, so this isn't the part that was making runs take hours.
    for scraper in official_scrapers:
        for result in scraper.run_multi():
            process_result(conn, result, enable_screenshots)
            _print_result(result)

    # News-source scrapers never touch Playwright (browser fallback is
    # disabled for them -- see scraper/news.py), so they're pure I/O-bound
    # HTTP fetches and safe to run concurrently. This is the bulk of the work
    # (6 outlets x every entity) and was the actual reason a full run could
    # blow past GitHub's 6h job ceiling -- see git history for that incident.
    # DB writes (process_result) stay on the main thread throughout; only the
    # network fetch + LLM extraction (run_multi()) happens in worker threads.
    with concurrent.futures.ThreadPoolExecutor(max_workers=NEWS_WORKERS) as pool:
        futures = {pool.submit(scraper.run_multi): scraper for scraper in news_scrapers}
        for future in concurrent.futures.as_completed(futures):
            scraper = futures[future]
            try:
                results = future.result()
            except Exception as exc:  # a single scraper's unhandled failure must not crash the batch
                print(f"[ERROR] {scraper.entity} (news, {scraper.base_url}) -> unhandled exception: {exc}")
                continue
            for result in results:
                process_result(conn, result, enable_screenshots)
                _print_result(result)

    conn.close()


if __name__ == "__main__":
    main()
