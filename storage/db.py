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
from datetime import date, datetime

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


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Added after fee_snapshots already existed in the committed storage/fees.db
    # -- CREATE TABLE IF NOT EXISTS above won't retrofit a column onto an
    # existing table, so migrate it explicitly rather than requiring a fresh DB.
    _ensure_column(conn, "fee_snapshots", "promo_end_date", "TEXT")
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
    promo_end_date: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO fee_snapshots
           (entity, network, fee_type, amount, conditions, effective_date, scraped_at, audit_id, promo_end_date)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (entity, network, fee_type, amount, conditions, effective_date, scraped_at, audit_id, promo_end_date),
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


def _is_promo_expired(row: dict, as_of: datetime) -> bool:
    if row["fee_type"] != "promo" or not row["promo_end_date"]:
        return False
    try:
        promo_end = date.fromisoformat(row["promo_end_date"])
    except ValueError:
        return False  # unparseable date -- don't hide a condition over a formatting quirk
    # promo_end_date is a calendar date with no time-of-day component, and
    # as_of can be naive (datetime.now() for the "latest" query) or
    # tz-aware (parsed from an as_of_iso string) -- compare at date
    # granularity only, which sidesteps both the tz-awareness mismatch and
    # any need to define what time-of-day a promo "ends" at.
    return promo_end < as_of.date()


def _more_current_standing_rate(candidate: dict, incumbent: dict) -> bool:
    # ISO YYYY-MM-DD strings compare correctly as plain strings.
    candidate_date, incumbent_date = candidate.get("effective_date"), incumbent.get("effective_date")
    if candidate_date and incumbent_date:
        return candidate_date > incumbent_date
    if candidate_date and not incumbent_date:
        return True  # a stated date beats an unstated one, regardless of scrape recency
    if incumbent_date and not candidate_date:
        return False
    return candidate["scraped_at"] > incumbent["scraped_at"]


def _filter_current_conditions(rows: list[dict], as_of: datetime) -> list[dict]:
    """Applies two "is this actually still applicable?" rules on top of the
    raw scrape-batch window, using each condition's own stated dates rather
    than when we happened to scrape it:

    - Drops promo conditions whose promo_end_date has already passed as of
      `as_of` -- otherwise an expired promo displays as if still live for as
      long as nothing re-scrapes that page differently.
    - For non-promo (standing-rate) conditions, keeps only the one with the
      most recent effective_date per (entity, network) -- a page or news
      article can be discovered late (a search engine doesn't sort by
      recency), which would otherwise insert an old, possibly superseded rate
      with today's scraped_at and make it look exactly as current as the
      genuinely up-to-date one. Promo conditions still legitimately stack
      alongside a standing rate (a permanent rate AND a live small-transaction
      waiver can both be true at once) -- this precedence only applies to
      standing-rate rows competing to represent "the" current rate.
    """
    rows = [r for r in rows if not _is_promo_expired(r, as_of)]

    standing_by_key: dict[tuple[str, str], dict] = {}
    other_rows = []
    for row in rows:
        if row["fee_type"] == "promo":
            other_rows.append(row)
            continue
        key = (row["entity"], row["network"])
        incumbent = standing_by_key.get(key)
        if incumbent is None or _more_current_standing_rate(row, incumbent):
            standing_by_key[key] = row
    return other_rows + list(standing_by_key.values())


def query_latest_fees_grouped(conn: sqlite3.Connection, window_seconds: int = 3600) -> list[dict]:
    """Like query_latest_fees, but returns EVERY row from an entity's most
    recent scrape batch, not just one per (entity, network). LLM extraction
    can legitimately produce more than one concurrent condition for the same
    network (e.g. a permanent rate and a separate limited-time promo, each
    found on a different page fetched moments apart within the same run) --
    collapsing to a single row per network would silently discard one of them.
    `window_seconds` groups rows into the same "batch" if they're within that
    span of the entity's latest scrape (pages within one run_all.py run are
    fetched seconds to minutes apart, not hours). See _filter_current_conditions
    for the expired-promo and superseded-standing-rate filtering on top."""
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
    return _filter_current_conditions(result, datetime.now())


def query_fees_as_of_grouped(conn: sqlite3.Connection, as_of_iso: str, window_seconds: int = 3600) -> list[dict]:
    """As-of variant of query_latest_fees_grouped: groups each entity's rows
    into its most recent scrape batch at or before as_of_iso, then applies the
    same "is this actually applicable as of that time" filtering (see
    _filter_current_conditions) using as_of_iso itself -- not today -- as the
    reference date, so a historical view correctly shows what was live then."""
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
    return _filter_current_conditions(result, as_of)


def query_feed(conn: sqlite3.Connection, limit: int = 300) -> list[dict]:
    """A "what's new" feed: one row per distinct fee fact, returning only its
    FIRST-seen scrape. fee_snapshots is append-only and the scraper re-inserts
    a row for a still-true fee on every run, so ordering by scraped_at
    directly would repeat the same fee 3x/day forever.

    "New" is judged on the SUBSTANTIVE fields only (fee_type, amount,
    effective_date) -- deliberately excluding the free-text `conditions`
    description, since Claude Haiku can phrase an unchanged fee slightly
    differently between runs, and that wording drift must not be mistaken for
    a real change. A page that states its own effective_date makes a genuine
    change straightforward to detect (the date itself differs); a page with
    no date falls back to pure value-equality against what's already
    recorded. Either way, an unchanged fact is still stored (every row stays
    in fee_snapshots for history/audit) but is not surfaced here as "new"."""
    rows = conn.execute(
        """SELECT fs.* FROM fee_snapshots fs
           WHERE fs.id IN (
               SELECT MIN(id) FROM fee_snapshots
               GROUP BY entity, network, fee_type, amount, effective_date
           )
           ORDER BY fs.scraped_at DESC, fs.id DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def query_history(conn: sqlite3.Connection, entity: str):
    return conn.execute(
        "SELECT * FROM fee_snapshots WHERE entity = ? ORDER BY scraped_at", (entity,)
    ).fetchall()


def query_audit(conn: sqlite3.Connection, audit_id: int):
    return conn.execute("SELECT * FROM audit_log WHERE id = ?", (audit_id,)).fetchone()


def query_sources(conn: sqlite3.Connection) -> list[dict]:
    """Every distinct source URL ever fetched, grouped by entity, with how
    many times it's been checked and whether the most recent check succeeded
    -- this is the per-institution "where does this data come from" index,
    covering entities even when extraction found no fee (e.g. a blocked
    site), not just ones with fee_snapshots rows."""
    rows = conn.execute(
        """SELECT entity, source_type, source_url,
                  COUNT(*) AS check_count,
                  MAX(fetched_at) AS last_checked
           FROM audit_log
           GROUP BY entity, source_type, source_url
           ORDER BY entity, source_type, source_url"""
    ).fetchall()
    result = []
    for r in rows:
        last_audit = conn.execute(
            """SELECT status, error_message FROM audit_log
               WHERE entity = ? AND source_type = ? AND source_url = ?
               ORDER BY fetched_at DESC LIMIT 1""",
            (r["entity"], r["source_type"], r["source_url"]),
        ).fetchone()
        d = dict(r)
        d["last_status"] = last_audit["status"] if last_audit else None
        d["last_error"] = last_audit["error_message"] if last_audit else None
        result.append(d)
    return result


def query_flagged(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM scraper_health WHERE flagged = 1").fetchall()


def query_distinct_timestamps(conn: sqlite3.Connection):
    rows = conn.execute("SELECT DISTINCT scraped_at FROM fee_snapshots ORDER BY scraped_at").fetchall()
    return [r["scraped_at"] for r in rows]
