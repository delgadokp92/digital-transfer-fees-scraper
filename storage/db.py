"""SQLite storage layer: fee history plus the audit trail that backs every figure.

Three tables:
- audit_log: one row per scrape attempt (URL, timestamp, hashes, archived copy, status).
  This is the provenance record -- every fee number can be traced back to exactly
  the audit_log row that produced it.
- fee_snapshots: append-only fee readings, each pointing at the audit_log row it came from.
  Append-only (never updated/deleted) so historical timepoints can always be reconstructed.
- scraper_health: one row per (entity, source_type) tracking whether the last structure
  hash changed unexpectedly, so a broken/drifted scraper gets flagged instead of trusted blindly.
"""
from __future__ import annotations

import pathlib
import sqlite3
from datetime import datetime

DB_PATH = pathlib.Path(__file__).resolve().parent / "fees.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('website','facebook')),
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    structure_hash TEXT,
    snapshot_path TEXT,
    screenshot_path TEXT,
    status TEXT NOT NULL CHECK(status IN ('ok','error')),
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS fee_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL,
    network TEXT NOT NULL CHECK(network IN ('InstaPay','PESONet')),
    fee_type TEXT NOT NULL,
    amount REAL,
    conditions TEXT,
    effective_date TEXT,
    scraped_at TEXT NOT NULL,
    audit_id INTEGER NOT NULL REFERENCES audit_log(id)
);

CREATE TABLE IF NOT EXISTS scraper_health (
    entity TEXT NOT NULL,
    source_type TEXT NOT NULL,
    last_ok_at TEXT,
    last_structure_hash TEXT,
    last_source_url TEXT,
    flagged INTEGER NOT NULL DEFAULT 0,
    flagged_reason TEXT,
    PRIMARY KEY (entity, source_type)
);
"""


def get_connection(db_path: str | pathlib.Path | None = None) -> sqlite3.Connection:
    path = pathlib.Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: Streamlit caches this connection with
    # @st.cache_resource, but a script rerun can execute on a different
    # thread than the one that created it -- sqlite3 connections are
    # thread-affine by default, which raises ProgrammingError otherwise.
    # Safe here since nothing performs concurrent writes on this connection;
    # the scraper writes from a separate process, not concurrently in-app.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def insert_audit_log(
    conn: sqlite3.Connection,
    *,
    entity: str,
    source_url: str,
    source_type: str,
    fetched_at: str,
    content_hash: str,
    structure_hash: str | None,
    snapshot_path: str | None,
    screenshot_path: str | None,
    status: str,
    error_message: str | None,
) -> int:
    cur = conn.execute(
        """INSERT INTO audit_log
           (entity, source_url, source_type, fetched_at, content_hash, structure_hash,
            snapshot_path, screenshot_path, status, error_message)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            entity, source_url, source_type, fetched_at, content_hash, structure_hash,
            snapshot_path, screenshot_path, status, error_message,
        ),
    )
    conn.commit()
    return cur.lastrowid


def update_audit_screenshot(conn: sqlite3.Connection, audit_id: int, screenshot_path: str) -> None:
    conn.execute("UPDATE audit_log SET screenshot_path = ? WHERE id = ?", (screenshot_path, audit_id))
    conn.commit()


def insert_fee_snapshot(
    conn: sqlite3.Connection,
    *,
    entity: str,
    network: str,
    fee_type: str,
    amount: float | None,
    conditions: str | None,
    effective_date: str | None,
    scraped_at: str,
    audit_id: int,
) -> int:
    cur = conn.execute(
        """INSERT INTO fee_snapshots
           (entity, network, fee_type, amount, conditions, effective_date, scraped_at, audit_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        (entity, network, fee_type, amount, conditions, effective_date, scraped_at, audit_id),
    )
    conn.commit()
    return cur.lastrowid


def get_last_structure_hash(conn: sqlite3.Connection, entity: str, source_type: str) -> str | None:
    row = conn.execute(
        "SELECT last_structure_hash FROM scraper_health WHERE entity = ? AND source_type = ?",
        (entity, source_type),
    ).fetchone()
    return row["last_structure_hash"] if row else None


def get_last_fee(conn: sqlite3.Connection, entity: str, network: str) -> dict | None:
    row = conn.execute(
        """SELECT amount, conditions FROM fee_snapshots
           WHERE entity = ? AND network = ?
           ORDER BY scraped_at DESC, id DESC LIMIT 1""",
        (entity, network),
    ).fetchone()
    return dict(row) if row else None


def upsert_scraper_health(
    conn: sqlite3.Connection,
    *,
    entity: str,
    source_type: str,
    last_ok_at: str | None,
    last_structure_hash: str | None,
    flagged: bool,
    flagged_reason: str | None,
    last_source_url: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO scraper_health
           (entity, source_type, last_ok_at, last_structure_hash, last_source_url, flagged, flagged_reason)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(entity, source_type) DO UPDATE SET
             last_ok_at = COALESCE(excluded.last_ok_at, scraper_health.last_ok_at),
             last_structure_hash = excluded.last_structure_hash,
             last_source_url = COALESCE(excluded.last_source_url, scraper_health.last_source_url),
             flagged = excluded.flagged,
             flagged_reason = excluded.flagged_reason""",
        (entity, source_type, last_ok_at, last_structure_hash, last_source_url, int(flagged), flagged_reason),
    )
    conn.commit()


def query_latest_fees(conn: sqlite3.Connection):
    return conn.execute(
        """SELECT fs.* FROM fee_snapshots fs
           JOIN (
               SELECT entity, network, MAX(scraped_at) AS max_scraped_at
               FROM fee_snapshots GROUP BY entity, network
           ) latest
           ON fs.entity = latest.entity AND fs.network = latest.network
              AND fs.scraped_at = latest.max_scraped_at
           ORDER BY fs.entity, fs.network"""
    ).fetchall()


def query_fees_as_of(conn: sqlite3.Connection, as_of_iso: str):
    return conn.execute(
        """SELECT fs.* FROM fee_snapshots fs
           JOIN (
               SELECT entity, network, MAX(scraped_at) AS max_scraped_at
               FROM fee_snapshots WHERE scraped_at <= ? GROUP BY entity, network
           ) latest
           ON fs.entity = latest.entity AND fs.network = latest.network
              AND fs.scraped_at = latest.max_scraped_at
           ORDER BY fs.entity, fs.network""",
        (as_of_iso,),
    ).fetchall()


def query_latest_fees_grouped(conn: sqlite3.Connection, window_seconds: int = 3600) -> list[dict]:
    """Like query_latest_fees, but returns EVERY row from an entity's most
    recent scrape batch, not just one per (entity, network). LLM extraction
    can legitimately produce more than one concurrent condition for the same
    network (e.g. a permanent rate and a separate limited-time promo, each
    found on a different page fetched moments apart within the same run) --
    collapsing to a single row per network would silently discard one of them.
    `window_seconds` groups rows into the same "batch" if they're within that
    span of the entity's latest scrape (pages within one run_all.py run are
    fetched seconds to minutes apart, not hours)."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM fee_snapshots ORDER BY entity, scraped_at").fetchall()]
    latest_by_entity: dict[str, datetime] = {}
    for row in rows:
        ts = datetime.fromisoformat(row["scraped_at"])
        if row["entity"] not in latest_by_entity or ts > latest_by_entity[row["entity"]]:
            latest_by_entity[row["entity"]] = ts

    result = []
    for row in rows:
        ts = datetime.fromisoformat(row["scraped_at"])
        if (latest_by_entity[row["entity"]] - ts).total_seconds() <= window_seconds:
            result.append(row)
    return result


def query_fees_as_of_grouped(conn: sqlite3.Connection, as_of_iso: str, window_seconds: int = 3600) -> list[dict]:
    """As-of variant of query_latest_fees_grouped: groups each entity's rows
    into its most recent scrape batch at or before as_of_iso."""
    as_of = datetime.fromisoformat(as_of_iso)
    rows = [
        dict(r) for r in conn.execute(
            "SELECT * FROM fee_snapshots WHERE scraped_at <= ? ORDER BY entity, scraped_at", (as_of_iso,)
        ).fetchall()
    ]
    latest_by_entity: dict[str, datetime] = {}
    for row in rows:
        ts = datetime.fromisoformat(row["scraped_at"])
        if row["entity"] not in latest_by_entity or ts > latest_by_entity[row["entity"]]:
            latest_by_entity[row["entity"]] = ts

    result = []
    for row in rows:
        ts = datetime.fromisoformat(row["scraped_at"])
        if (latest_by_entity[row["entity"]] - ts).total_seconds() <= window_seconds:
            result.append(row)
    return result


def query_history(conn: sqlite3.Connection, entity: str):
    return conn.execute(
        "SELECT * FROM fee_snapshots WHERE entity = ? ORDER BY scraped_at", (entity,)
    ).fetchall()


def query_audit(conn: sqlite3.Connection, audit_id: int):
    return conn.execute("SELECT * FROM audit_log WHERE id = ?", (audit_id,)).fetchone()


def query_flagged(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM scraper_health WHERE flagged = 1").fetchall()


def query_distinct_timestamps(conn: sqlite3.Connection):
    rows = conn.execute("SELECT DISTINCT scraped_at FROM fee_snapshots ORDER BY scraped_at").fetchall()
    return [r["scraped_at"] for r in rows]
