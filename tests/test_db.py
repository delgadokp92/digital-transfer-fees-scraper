import tempfile

from storage import db


def _make_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = db.get_connection(tmp.name)
    db.init_db(conn)
    return conn


def test_insert_and_query_latest_fee():
    conn = _make_conn()
    audit_id = db.insert_audit_log(
        conn,
        entity="Test Bank",
        source_url="https://example.test/fees",
        source_type="website",
        fetched_at="2026-01-01T00:00:00+00:00",
        content_hash="abc",
        structure_hash="def",
        snapshot_path="storage/snapshots/test.html",
        screenshot_path=None,
        status="ok",
        error_message=None,
    )

    db.insert_fee_snapshot(
        conn,
        entity="Test Bank",
        network="InstaPay",
        fee_type="flat",
        amount=25.0,
        conditions="Flat fee",
        effective_date=None,
        scraped_at="2026-01-01T00:00:00+00:00",
        audit_id=audit_id,
    )

    latest = db.query_latest_fees(conn)

    assert len(latest) == 1
    assert latest[0]["amount"] == 25.0
    assert db.query_audit(conn, audit_id)["source_url"] == "https://example.test/fees"


def test_query_fees_as_of_uses_the_snapshot_at_or_before_the_given_time():
    conn = _make_conn()
    audit_1 = db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://example.test/fees", source_type="website",
        fetched_at="2026-01-01T00:00:00+00:00", content_hash="h1", structure_hash="s1",
        snapshot_path=None, screenshot_path=None, status="ok", error_message=None,
    )
    db.insert_fee_snapshot(
        conn, entity="Test Bank", network="InstaPay", fee_type="flat", amount=25.0,
        conditions=None, effective_date=None, scraped_at="2026-01-01T00:00:00+00:00", audit_id=audit_1,
    )

    audit_2 = db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://example.test/fees", source_type="website",
        fetched_at="2026-02-01T00:00:00+00:00", content_hash="h2", structure_hash="s1",
        snapshot_path=None, screenshot_path=None, status="ok", error_message=None,
    )
    db.insert_fee_snapshot(
        conn, entity="Test Bank", network="InstaPay", fee_type="flat", amount=30.0,
        conditions=None, effective_date=None, scraped_at="2026-02-01T00:00:00+00:00", audit_id=audit_2,
    )

    as_of_january = db.query_fees_as_of(conn, "2026-01-15T00:00:00+00:00")
    as_of_latest = db.query_fees_as_of(conn, "2026-03-01T00:00:00+00:00")

    assert as_of_january[0]["amount"] == 25.0
    assert as_of_latest[0]["amount"] == 30.0


def test_query_latest_fees_grouped_keeps_multiple_conditions_same_network():
    # The whole point of the grouped query: an entity/network can have more
    # than one concurrent real condition (e.g. a permanent rate on one page
    # and a promo on another, fetched moments apart in the same run) -- both
    # must survive, not just whichever has the single latest timestamp.
    conn = _make_conn()
    audit_1 = db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://example.test/permanent", source_type="website",
        fetched_at="2026-01-01T00:00:00+00:00", content_hash="h1", structure_hash="s1",
        snapshot_path=None, screenshot_path=None, status="ok", error_message=None,
    )
    db.insert_fee_snapshot(
        conn, entity="Test Bank", network="InstaPay", fee_type="flat", amount=10.0,
        conditions="Permanent rate", effective_date=None,
        scraped_at="2026-01-01T00:00:01+00:00", audit_id=audit_1,
    )

    audit_2 = db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://example.test/promo", source_type="website",
        fetched_at="2026-01-01T00:00:05+00:00", content_hash="h2", structure_hash="s1",
        snapshot_path=None, screenshot_path=None, status="ok", error_message=None,
    )
    db.insert_fee_snapshot(
        conn, entity="Test Bank", network="InstaPay", fee_type="promo", amount=0.0,
        conditions="Promo waiver", effective_date=None,
        scraped_at="2026-01-01T00:00:05+00:00", audit_id=audit_2,
    )

    grouped = db.query_latest_fees_grouped(conn)

    assert len(grouped) == 2
    assert {row["conditions"] for row in grouped} == {"Permanent rate", "Promo waiver"}


def test_query_latest_fees_grouped_excludes_a_much_older_batch():
    conn = _make_conn()
    old_audit = db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://example.test/fees", source_type="website",
        fetched_at="2026-01-01T00:00:00+00:00", content_hash="h1", structure_hash="s1",
        snapshot_path=None, screenshot_path=None, status="ok", error_message=None,
    )
    db.insert_fee_snapshot(
        conn, entity="Test Bank", network="InstaPay", fee_type="flat", amount=25.0,
        conditions="Old batch", effective_date=None, scraped_at="2026-01-01T00:00:00+00:00", audit_id=old_audit,
    )
    new_audit = db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://example.test/fees", source_type="website",
        fetched_at="2026-02-01T00:00:00+00:00", content_hash="h2", structure_hash="s1",
        snapshot_path=None, screenshot_path=None, status="ok", error_message=None,
    )
    db.insert_fee_snapshot(
        conn, entity="Test Bank", network="InstaPay", fee_type="flat", amount=10.0,
        conditions="New batch", effective_date=None, scraped_at="2026-02-01T00:00:00+00:00", audit_id=new_audit,
    )

    grouped = db.query_latest_fees_grouped(conn, window_seconds=3600)

    assert len(grouped) == 1
    assert grouped[0]["conditions"] == "New batch"


def test_query_latest_fees_grouped_excludes_an_expired_promo():
    # A promo whose promo_end_date has already passed must not display as if
    # still live just because nothing has re-scraped that page since.
    conn = _make_conn()
    audit = db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://example.test/promo", source_type="website",
        fetched_at="2020-01-01T00:00:00+00:00", content_hash="h1", structure_hash="s1",
        snapshot_path=None, screenshot_path=None, status="ok", error_message=None,
    )
    db.insert_fee_snapshot(
        conn, entity="Test Bank", network="InstaPay", fee_type="promo", amount=0.0,
        conditions="Free for a limited time (promo through 2020-06-30)", effective_date="2020-01-01",
        scraped_at="2020-01-01T00:00:00+00:00", audit_id=audit, promo_end_date="2020-06-30",
    )

    grouped = db.query_latest_fees_grouped(conn)

    assert grouped == []


def test_query_fees_as_of_grouped_keeps_a_promo_that_was_still_live_at_that_time():
    # Historical view: a promo that has since expired (relative to *today*)
    # must still show up when looking at an as-of date that fell within its
    # window -- expiry is relative to the as-of date, not today.
    conn = _make_conn()
    audit = db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://example.test/promo", source_type="website",
        fetched_at="2020-01-01T00:00:00+00:00", content_hash="h1", structure_hash="s1",
        snapshot_path=None, screenshot_path=None, status="ok", error_message=None,
    )
    db.insert_fee_snapshot(
        conn, entity="Test Bank", network="InstaPay", fee_type="promo", amount=0.0,
        conditions="Free for a limited time", effective_date="2020-01-01",
        scraped_at="2020-01-01T00:00:00+00:00", audit_id=audit, promo_end_date="2020-06-30",
    )

    as_of_during_promo = db.query_fees_as_of_grouped(conn, "2020-03-01T00:00:00+00:00")
    as_of_after_promo = db.query_fees_as_of_grouped(conn, "2020-12-01T00:00:00+00:00")

    assert len(as_of_during_promo) == 1
    assert as_of_after_promo == []


def test_query_latest_fees_grouped_supersedes_an_older_standing_rate_discovered_late():
    # A news article can be discovered well after it was published (search
    # results aren't sorted by recency) -- if it lands in the same scrape
    # batch as a genuinely current official-site rate, the one with the more
    # recent effective_date must win, not both showing side by side.
    conn = _make_conn()
    current_audit = db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://example.test/fees", source_type="website",
        fetched_at="2026-01-01T00:00:00+00:00", content_hash="h1", structure_hash="s1",
        snapshot_path=None, screenshot_path=None, status="ok", error_message=None,
    )
    db.insert_fee_snapshot(
        conn, entity="Test Bank", network="InstaPay", fee_type="flat", amount=25.0,
        conditions="Current standing rate", effective_date="2025-06-01",
        scraped_at="2026-01-01T00:00:01+00:00", audit_id=current_audit,
    )
    stale_news_audit = db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://news.example/old-article", source_type="website",
        fetched_at="2026-01-01T00:00:05+00:00", content_hash="h2", structure_hash="s1",
        snapshot_path=None, screenshot_path=None, status="ok", error_message=None,
    )
    db.insert_fee_snapshot(
        conn, entity="Test Bank", network="InstaPay", fee_type="free", amount=0.0,
        conditions="Old superseded waiver", effective_date="2022-01-01",
        scraped_at="2026-01-01T00:00:05+00:00", audit_id=stale_news_audit,
    )

    grouped = db.query_latest_fees_grouped(conn)

    assert len(grouped) == 1
    assert grouped[0]["conditions"] == "Current standing rate"


def test_query_feed_ignores_reworded_but_unchanged_fee():
    conn = _make_conn()
    audit1 = db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://example.test/fees", source_type="website",
        fetched_at="2026-01-01T00:00:00+00:00", content_hash="a", structure_hash="s",
        snapshot_path=None, screenshot_path=None, status="ok", error_message=None,
    )
    db.insert_fee_snapshot(
        conn, entity="Test Bank", network="InstaPay", fee_type="flat", amount=25.0,
        conditions="PHP 25 per transfer", effective_date=None,
        scraped_at="2026-01-01T00:00:00+00:00", audit_id=audit1,
    )
    audit2 = db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://example.test/fees", source_type="website",
        fetched_at="2026-01-02T00:00:00+00:00", content_hash="b", structure_hash="s",
        snapshot_path=None, screenshot_path=None, status="ok", error_message=None,
    )
    # Same fee_type/amount/effective_date as before -- only the wording changed,
    # which must NOT be treated as a new event (LLM rephrasing, not a real change).
    db.insert_fee_snapshot(
        conn, entity="Test Bank", network="InstaPay", fee_type="flat", amount=25.0,
        conditions="A flat PHP 25.00 fee applies to each digital transfer", effective_date=None,
        scraped_at="2026-01-02T00:00:00+00:00", audit_id=audit2,
    )

    feed = db.query_feed(conn)

    assert len(feed) == 1
    assert feed[0]["scraped_at"] == "2026-01-01T00:00:00+00:00"


def test_query_feed_flags_genuine_amount_change_as_new():
    conn = _make_conn()
    audit1 = db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://example.test/fees", source_type="website",
        fetched_at="2026-01-01T00:00:00+00:00", content_hash="a", structure_hash="s",
        snapshot_path=None, screenshot_path=None, status="ok", error_message=None,
    )
    db.insert_fee_snapshot(
        conn, entity="Test Bank", network="InstaPay", fee_type="flat", amount=25.0,
        conditions="PHP 25 per transfer", effective_date=None,
        scraped_at="2026-01-01T00:00:00+00:00", audit_id=audit1,
    )
    audit2 = db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://example.test/fees", source_type="website",
        fetched_at="2026-01-02T00:00:00+00:00", content_hash="b", structure_hash="s",
        snapshot_path=None, screenshot_path=None, status="ok", error_message=None,
    )
    db.insert_fee_snapshot(
        conn, entity="Test Bank", network="InstaPay", fee_type="flat", amount=30.0,
        conditions="PHP 30 per transfer", effective_date=None,
        scraped_at="2026-01-02T00:00:00+00:00", audit_id=audit2,
    )

    feed = db.query_feed(conn)

    assert len(feed) == 2
    assert {row["amount"] for row in feed} == {25.0, 30.0}


def test_query_sources_reports_check_count_and_latest_status():
    conn = _make_conn()
    db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://example.test/fees", source_type="website",
        fetched_at="2026-01-01T00:00:00+00:00", content_hash="a", structure_hash="s",
        snapshot_path=None, screenshot_path=None, status="ok", error_message=None,
    )
    db.insert_audit_log(
        conn, entity="Test Bank", source_url="https://example.test/fees", source_type="website",
        fetched_at="2026-01-02T00:00:00+00:00", content_hash="b", structure_hash="s",
        snapshot_path=None, screenshot_path=None, status="error", error_message="timed out",
    )

    sources = db.query_sources(conn)

    assert len(sources) == 1
    assert sources[0]["check_count"] == 2
    assert sources[0]["last_status"] == "error"
    assert sources[0]["last_error"] == "timed out"


def test_scraper_health_flagging():
    conn = _make_conn()
    db.upsert_scraper_health(
        conn, entity="Test Bank", source_type="website",
        last_ok_at="2026-01-01T00:00:00+00:00", last_structure_hash="hash1",
        flagged=False, flagged_reason=None,
    )
    assert db.get_last_structure_hash(conn, "Test Bank", "website") == "hash1"
    assert db.query_flagged(conn) == []

    db.upsert_scraper_health(
        conn, entity="Test Bank", source_type="website",
        last_ok_at=None, last_structure_hash="hash2",
        flagged=True, flagged_reason="structure changed",
    )

    flagged = db.query_flagged(conn)
    assert len(flagged) == 1
    assert flagged[0]["flagged_reason"] == "structure changed"
