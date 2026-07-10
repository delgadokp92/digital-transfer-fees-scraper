"""Streamlit dashboard: InstaPay/PESONet transfer fees per BSP-supervised institution.

Run locally with:  streamlit run app.py
Deploys as-is to Streamlit Community Cloud (this file is the entrypoint).
"""
from __future__ import annotations

import html
import os
import pathlib
from datetime import datetime, timezone
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
import yaml

from storage import db

VERSION = "v2026-07-10"
ROOT_DIR = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = ROOT_DIR / "config" / "entities.yaml"
NEWS_CONFIG_PATH = ROOT_DIR / "config" / "news_sources.yaml"

st.set_page_config(page_title="PH Transfer Fees Monitor", layout="wide")

# Streamlit Community Cloud secrets (st.secrets) aren't guaranteed to also
# land in os.environ -- but the LLM extraction module reads ANTHROPIC_API_KEY
# via os.environ (same as any local run using a .env file). Bridge it
# explicitly so a secret set in the Cloud UI actually reaches that code path.
# st.secrets raises (not just returns empty) when no secrets.toml exists at
# all, which is the normal case for local dev using .env instead -- guard it.
if not os.environ.get("ANTHROPIC_API_KEY"):
    try:
        os.environ["ANTHROPIC_API_KEY"] = st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        pass  # no Cloud secrets configured -- fall back to .env / local env var

FEE_TABLE_COLUMNS = ["entity", "network", "fee_type", "amount", "conditions", "effective_date", "scraped_at"]

_CATEGORY_EMOJI = {"bank": "🏦", "e-wallet": "📱", "emi": "💳"}

# Colors live entirely in CSS (custom properties + a prefers-color-scheme
# override) rather than being computed in Python and string-substituted, so
# the whole card/feed UI follows whichever theme the visitor's browser/OS
# prefers instead of being tuned for one mode. .streamlit/config.toml plus
# the #MainMenu/header/footer rule below hide Streamlit's own chrome (Share,
# GitHub/Edit, hamburger menu, "Made with Streamlit" footer) so only the
# dashboard itself is visible in the upper right.
_CARD_CSS = """
<style>
:root {
    --fee-good-bg: rgba(12, 163, 12, 0.16);
    --fee-warning-bg: rgba(250, 178, 25, 0.22);
    --fee-neutral-bg: rgba(128, 128, 128, 0.14);
    --fee-muted-text: #6b6b6b;
    --fee-border: rgba(128, 128, 128, 0.25);
    --fee-row-border: rgba(128, 128, 128, 0.12);
    --fee-accent-free: #0ca30c;
    --fee-accent-promo: #d99a1b;
    --fee-accent-neutral: rgba(128, 128, 128, 0.6);
}
@media (prefers-color-scheme: dark) {
    :root {
        --fee-good-bg: rgba(46, 204, 64, 0.30);
        --fee-warning-bg: rgba(255, 193, 7, 0.32);
        --fee-neutral-bg: rgba(170, 170, 170, 0.24);
        --fee-muted-text: #b5b5b5;
        --fee-border: rgba(200, 200, 200, 0.25);
        --fee-row-border: rgba(200, 200, 200, 0.16);
        --fee-accent-free: #35d63c;
        --fee-accent-promo: #ffc93c;
        --fee-accent-neutral: rgba(200, 200, 200, 0.6);
    }
}

#MainMenu, header, footer { visibility: hidden; }

.entity-card {
    border: 1px solid var(--fee-border); border-radius: 10px;
    padding: 16px 20px; margin-bottom: 16px;
}
.entity-card-header {
    display: flex; justify-content: space-between; align-items: baseline;
    flex-wrap: wrap; gap: 8px; margin-bottom: 6px;
}
.entity-name { font-size: 1.05rem; font-weight: 600; }
.entity-flag {
    font-size: 0.82rem; padding: 2px 10px; border-radius: 6px;
    background: var(--fee-warning-bg);
}
.network-block { margin-top: 12px; }
.network-name {
    font-weight: 600; font-size: 0.92rem; margin-bottom: 4px;
    text-transform: uppercase; letter-spacing: 0.02em; color: var(--fee-muted-text);
}
.condition-row {
    display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap;
    padding: 6px 0; border-bottom: 1px solid var(--fee-row-border);
}
.condition-row:last-child { border-bottom: none; }
.condition-badge {
    font-size: 0.78rem; font-weight: 600; padding: 2px 10px; border-radius: 10px;
    white-space: nowrap; font-variant-numeric: tabular-nums;
}
.badge-free { background: var(--fee-good-bg); }
.badge-promo { background: var(--fee-warning-bg); }
.badge-flat, .badge-tiered { background: var(--fee-neutral-bg); }
.condition-text { flex: 1 1 auto; min-width: 200px; }
.condition-date { color: var(--fee-muted-text); font-size: 0.85em; }
.condition-source { font-size: 0.82rem; white-space: nowrap; }
.no-data { color: var(--fee-muted-text); font-style: italic; font-size: 0.9rem; padding: 4px 0; }
.fee-link { color: inherit; text-decoration: none; border-bottom: 1px dashed currentColor; }
.fee-link:hover { border-bottom-style: solid; }

.feed-card {
    border: 1px solid var(--fee-border); border-left: 4px solid var(--fee-accent-neutral);
    border-radius: 10px; padding: 12px 18px; margin-bottom: 12px;
}
.feed-card.accent-free { border-left-color: var(--fee-accent-free); }
.feed-card.accent-promo { border-left-color: var(--fee-accent-promo); }
.feed-top-row {
    display: flex; justify-content: space-between; align-items: baseline;
    flex-wrap: wrap; gap: 8px;
}
.feed-tags { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
.feed-institution { font-weight: 700; font-size: 1.0rem; }
.feed-tag {
    font-size: 0.72rem; padding: 1px 8px; border-radius: 8px;
    background: var(--fee-neutral-bg); color: var(--fee-muted-text);
    text-transform: uppercase; letter-spacing: 0.03em; white-space: nowrap;
}
.feed-time { font-size: 0.78rem; color: var(--fee-muted-text); white-space: nowrap; }
.feed-text { margin-top: 6px; }
.feed-footer { margin-top: 6px; font-size: 0.82rem; }
</style>
"""


@st.cache_resource
def get_conn():
    conn = db.get_connection()
    db.init_db(conn)
    return conn


@st.cache_data(ttl=300)
def load_latest_grouped():
    return db.query_latest_fees_grouped(get_conn())


@st.cache_data(ttl=300)
def load_as_of_grouped(as_of_iso: str):
    return db.query_fees_as_of_grouped(get_conn(), as_of_iso)


@st.cache_data(ttl=300)
def load_timestamps():
    return db.query_distinct_timestamps(get_conn())


@st.cache_data(ttl=300)
def load_history(entity: str):
    return [dict(r) for r in db.query_history(get_conn(), entity)]


@st.cache_data(ttl=300)
def load_flagged():
    return [dict(r) for r in db.query_flagged(get_conn())]


@st.cache_data(ttl=300)
def load_all_entity_names():
    return sorted({r["entity"] for r in db.query_latest_fees_grouped(get_conn())})


@st.cache_data(ttl=300)
def load_feed():
    return db.query_feed(get_conn())


@st.cache_data(ttl=300)
def load_entity_categories() -> dict[str, str]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        entities = yaml.safe_load(f)["entities"]
    return {e["name"]: e.get("category", "") for e in entities}


@st.cache_data(ttl=300)
def load_sources():
    return db.query_sources(get_conn())


@st.cache_data(ttl=300)
def load_news_domains() -> set[str]:
    """Domains of configured third-party news/tech-blog outlets (see
    config/news_sources.yaml) -- used only to pick a display icon (a news
    outlet is fetched the same way as any other page, source_type='website'
    in the DB; "official vs. third-party" is a config-level distinction)."""
    with open(NEWS_CONFIG_PATH, "r", encoding="utf-8") as f:
        sources = yaml.safe_load(f)["sources"]
    return {urlparse(s["search_url_template"]).netloc for s in sources}


def _source_icon(source_type: str, source_url: str | None, news_domains: set[str]) -> str:
    if source_type == "facebook":
        return "📘"
    if source_url and urlparse(source_url).netloc in news_domains:
        return "📰"
    return "🌐"


@st.cache_data(ttl=300)
def load_entity_source_configs() -> dict[str, list[dict]]:
    """Configured source URLs per entity from entities.yaml (website +
    Facebook), skipping entities with nothing configured yet (# website: TBD)
    so the Sources tab doesn't fill up with dozens of empty placeholders."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        entities = yaml.safe_load(f)["entities"]
    configs: dict[str, list[dict]] = {}
    for e in entities:
        sources = []
        website = e.get("website")
        if website:
            url = website.get("url") or website.get("base_url")
            if url:
                sources.append({"source_type": "website", "source_url": url})
        facebook = e.get("facebook")
        if facebook and facebook.get("page_url"):
            sources.append({"source_type": "facebook", "source_url": facebook["page_url"]})
        if sources:
            configs[e["name"]] = sources
    return configs


def _badge_class(fee_type: str) -> str:
    return {"free": "badge-free", "promo": "badge-promo"}.get(fee_type, "badge-flat")


def _badge_label(fee_type: str, amount: float | None) -> str:
    if fee_type == "free":
        return "Free"
    if amount is not None:
        return f"PHP {amount:,.2f}"
    return fee_type.capitalize()


def _category_label(category: str) -> str:
    cat = (category or "").strip().lower()
    emoji = _CATEGORY_EMOJI.get(cat, "🏢")
    label = cat.replace("-", " ").title() if cat else "Institution"
    return f"{emoji} {label}"


def _relative_time(scraped_at: str) -> str:
    try:
        ts = datetime.fromisoformat(scraped_at)
    except ValueError:
        return scraped_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - ts).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    days = int(seconds // 86400)
    if days < 7:
        return f"{days}d ago"
    return ts.strftime("%Y-%m-%d")


def _render_condition(row: dict, conn) -> str:
    audit = db.query_audit(conn, row["audit_id"])
    source_url = audit["source_url"] if audit is not None else None

    badge = (
        f'<span class="condition-badge {_badge_class(row["fee_type"])}">'
        f'{html.escape(_badge_label(row["fee_type"], row["amount"]))}</span>'
    )
    text = html.escape(row["conditions"] or "")
    effective = (
        f' <span class="condition-date">Effective {html.escape(row["effective_date"])}</span>'
        if row.get("effective_date") else ""
    )
    source = (
        f' <a class="condition-source fee-link" href="{html.escape(source_url)}" '
        f'target="_blank" rel="noopener">source</a>'
        if source_url else ""
    )
    return f'<div class="condition-row">{badge}<span class="condition-text">{text}{effective}</span>{source}</div>'


def render_entity_card(entity: str, networks: dict[str, list[dict]], flagged_reason: str | None, conn) -> str:
    parts = ['<div class="entity-card">', '<div class="entity-card-header">',
             f'<span class="entity-name">{html.escape(entity)}</span>']
    if flagged_reason:
        parts.append(f'<span class="entity-flag">⚠ {html.escape(flagged_reason)}</span>')
    parts.append("</div>")

    for network in ("InstaPay", "PESONet"):
        rows = networks.get(network, [])
        parts.append(f'<div class="network-block"><div class="network-name">{network}</div>')
        if rows:
            for row in rows:
                parts.append(_render_condition(row, conn))
        else:
            parts.append('<div class="no-data">No fee data found yet</div>')
        parts.append("</div>")

    parts.append("</div>")
    return "".join(parts)


def build_entity_cards(conn, fees: list[dict], flagged: list[dict]) -> str:
    by_entity: dict[str, dict[str, list[dict]]] = {}
    for row in fees:
        by_entity.setdefault(row["entity"], {}).setdefault(row["network"], []).append(row)

    flagged_reasons: dict[str, str] = {}
    for row in flagged:
        flagged_reasons.setdefault(row["entity"], row["flagged_reason"] or "Flagged for review")

    all_entities = sorted(set(by_entity) | set(flagged_reasons))
    cards = [
        render_entity_card(entity, by_entity.get(entity, {}), flagged_reasons.get(entity), conn)
        for entity in all_entities
    ]
    return "".join(cards)


def render_feed_item(row: dict, categories: dict[str, str], news_domains: set[str], conn) -> str:
    audit = db.query_audit(conn, row["audit_id"])
    source_url = audit["source_url"] if audit is not None else None
    source_type = audit["source_type"] if audit is not None else None
    source_icon = _source_icon(source_type, source_url, news_domains)

    accent = _badge_class(row["fee_type"]).replace("badge-", "accent-")
    badge = (
        f'<span class="condition-badge {_badge_class(row["fee_type"])}">'
        f'{html.escape(_badge_label(row["fee_type"], row["amount"]))}</span>'
    )
    category_chip = f'<span class="feed-tag">{html.escape(_category_label(categories.get(row["entity"], "")))}</span>'
    network_chip = f'<span class="feed-tag">{html.escape(row["network"])}</span>'
    text = html.escape(row["conditions"] or "")
    effective = (
        f' <span class="condition-date">Effective {html.escape(row["effective_date"])}</span>'
        if row.get("effective_date") else ""
    )
    source_link = (
        f'<a class="fee-link" href="{html.escape(source_url)}" target="_blank" rel="noopener">{source_icon} source</a>'
        if source_url else '<span class="feed-time">source unavailable</span>'
    )

    return (
        f'<div class="feed-card {accent}">'
        f'<div class="feed-top-row">'
        f'<div class="feed-tags"><span class="feed-institution">{html.escape(row["entity"])}</span>'
        f'{category_chip}{network_chip}</div>'
        f'<span class="feed-time" title="{html.escape(row["scraped_at"])}">{_relative_time(row["scraped_at"])}</span>'
        f'</div>'
        f'<div class="feed-text">{badge} {text}{effective}</div>'
        f'<div class="feed-footer">{source_link}</div>'
        f'</div>'
    )


def build_feed_html(rows: list[dict], categories: dict[str, str], news_domains: set[str], conn) -> str:
    if not rows:
        return '<div class="no-data">No updates match this filter yet.</div>'
    return "".join(render_feed_item(row, categories, news_domains, conn) for row in rows)


def build_fee_comparison_table(fees: list[dict]) -> pd.DataFrame:
    """Rows = institution, columns = (network, Min/Max) using each
    institution's currently active conditions. 'Free' counts as PHP 0;
    conditions with no fixed peso amount (e.g. purely tiered/conditional
    fees described only in text) are excluded from the min/max, not
    treated as zero."""
    buckets: dict[str, dict[tuple[str, str], float]] = {}
    for row in fees:
        value = 0.0 if row["fee_type"] == "free" else row["amount"]
        if value is None:
            continue
        bucket = buckets.setdefault(row["entity"], {})
        for stat, agg in (("Min", min), ("Max", max)):
            key = (row["network"], stat)
            bucket[key] = value if key not in bucket else agg(bucket[key], value)

    columns = pd.MultiIndex.from_product([["InstaPay", "PESONet"], ["Min", "Max"]])
    table = pd.DataFrame(index=sorted(buckets), columns=columns, dtype=float)
    for entity, bucket in buckets.items():
        for key, value in bucket.items():
            table.loc[entity, key] = value
    return table


def _format_fee_cell(value: float) -> str:
    if pd.isna(value):
        return "—"
    if value == 0:
        return "Free"
    return f"PHP {value:,.2f}"


st.markdown(_CARD_CSS, unsafe_allow_html=True)

st.title("Philippine InstaPay / PESONet Transfer Fees Monitor")
st.caption(VERSION)
st.caption(
    "Fees are collected only from each institution's own public website, press releases, or "
    "Facebook page -- never from BSP reports -- so this data can be used to independently "
    "check against those reports rather than being derived from them. Each source page is read "
    "and qualified by Claude Haiku. Over-the-counter fees are excluded; institutions can have "
    "more than one concurrent fee condition (e.g. a permanent rate alongside a limited-time "
    "promo) -- all are shown, not just one."
)

timestamps = load_timestamps()

st.sidebar.header("View")
st.sidebar.caption("Applies to the \"By Institution\" and \"Fee Comparison\" tabs.")
view_mode = st.sidebar.radio("Show fees as of", ["Latest", "Choose a point in time"])

if view_mode == "Latest" or not timestamps:
    fees = load_latest_grouped()
    as_of_label = "Latest available data"
else:
    selected_ts = st.sidebar.select_slider("Timestamp", options=timestamps, value=timestamps[-1])
    fees = load_as_of_grouped(selected_ts)
    as_of_label = f"As of {selected_ts}"

flagged = load_flagged()

tab_feed, tab_institutions, tab_compare, tab_sources = st.tabs(
    ["📰 Latest Updates", "🏦 By Institution", "📊 Fee Comparison", "🔎 Sources"]
)

with tab_feed:
    st.subheader("What's new")
    st.caption(
        "Each entry is the first time this exact fee condition was detected -- later scrapes "
        "that just reconfirm an already-known fee don't repeat here. Includes both each "
        "institution's own channels (🌐/📘) and third-party news/tech-blog coverage (📰) -- "
        "always reflects the full detection history, independent of the sidebar's \"as of\" selector."
    )
    categories = load_entity_categories()
    search_col, network_col = st.columns([2, 1])
    with search_col:
        search = st.text_input("Filter by institution", "", placeholder="e.g. BPI, GCash...")
    with network_col:
        network_filter = st.selectbox("Network", ["All", "InstaPay", "PESONet"])

    feed_rows = load_feed()
    if search:
        feed_rows = [r for r in feed_rows if search.lower() in r["entity"].lower()]
    if network_filter != "All":
        feed_rows = [r for r in feed_rows if r["network"] == network_filter]

    SHOWN = 100
    if not feed_rows:
        st.info("No updates match this filter yet.")
    else:
        st.markdown(build_feed_html(feed_rows[:SHOWN], categories, load_news_domains(), get_conn()), unsafe_allow_html=True)
        if len(feed_rows) > SHOWN:
            st.caption(f"Showing the latest {SHOWN} of {len(feed_rows)} updates.")

with tab_institutions:
    st.subheader(as_of_label)

    if not fees and not flagged:
        st.info("No data collected yet. Run `python -m scraper.run_all` to populate the database.")
    else:
        st.markdown(build_entity_cards(get_conn(), fees, flagged), unsafe_allow_html=True)
        st.caption(
            "Green badge = fee currently waived/free. Amber badge = a limited-time promo "
            "(check the effective/through dates). Click \"source\" to open where a figure came from."
        )

    with st.expander("System details (audit trail, fee history, scraper health)"):
        st.subheader("Entity detail & audit trail")

        entities = load_all_entity_names()

        if entities:
            selected_entity = st.selectbox("Select an institution", entities)
            history = load_history(selected_entity)

            if history:
                hist_df = pd.DataFrame(history)
                chart_df = hist_df.pivot_table(index="scraped_at", columns="network", values="amount", aggfunc="last")
                st.line_chart(chart_df)
                st.dataframe(hist_df[FEE_TABLE_COLUMNS], width="stretch", hide_index=True)

                st.markdown("**Audit trail (source of each figure)**")
                conn = get_conn()
                for row in list(reversed(history))[:10]:
                    audit = db.query_audit(conn, row["audit_id"])
                    if audit is None:
                        continue
                    label = f"{row['network']} — {row['amount']} — scraped {row['scraped_at']}"
                    with st.expander(label):
                        st.write(f"Source: [{audit['source_url']}]({audit['source_url']}) ({audit['source_type']})")
                        st.write(f"Fetched at: {audit['fetched_at']}")
                        if audit["snapshot_path"]:
                            st.write(f"Archived raw page: `{audit['snapshot_path']}`")
                        if audit["screenshot_path"]:
                            st.image(audit["screenshot_path"], caption="Archived screenshot at time of scrape")
        else:
            st.info("No entities yet -- add some to config/entities.yaml and run the scraper.")

        st.divider()
        st.subheader("Scraper health")
        if flagged:
            st.warning(f"{len(flagged)} source(s) flagged for review")
            st.dataframe(pd.DataFrame(flagged), width="stretch", hide_index=True)
        else:
            st.success("No flagged sources.")

with tab_compare:
    st.subheader("Minimum & maximum fee by network")
    st.caption(
        "Based on each institution's currently active fee conditions (same data as the "
        "\"By Institution\" tab). \"Free\" counts as PHP 0. A blank cell means no fixed peso "
        "amount was found for that network -- often because the only fee found there is "
        "tiered/conditional and described in text rather than a single number; check that "
        "institution's card in the \"By Institution\" tab for the full description."
    )
    table = build_fee_comparison_table(fees)
    if table.empty:
        st.info("No data collected yet.")
    else:
        st.dataframe(table.map(_format_fee_cell), width="stretch")

with tab_sources:
    st.subheader("Where each institution's information comes from")
    st.caption(
        "Every source URL the scraper has actually fetched for an institution -- 🌐 the "
        "institution's own website, 📘 its Facebook page, 📰 third-party news/tech-blog "
        "coverage -- with how many times it's been checked and whether the most recent check "
        "succeeded. Configured sources not yet successfully reached show as \"not yet checked\", "
        "so coverage gaps are visible too, not just what worked."
    )

    checked = load_sources()
    configured = load_entity_source_configs()

    checked_by_entity: dict[str, list[dict]] = {}
    for row in checked:
        checked_by_entity.setdefault(row["entity"], []).append(row)

    all_source_entities = sorted(set(checked_by_entity) | set(configured))
    search_src = st.text_input("Filter by institution", "", key="sources_search", placeholder="e.g. BPI, GCash...")
    if search_src:
        all_source_entities = [e for e in all_source_entities if search_src.lower() in e.lower()]

    if not all_source_entities:
        st.info("No sources match this filter yet.")

    news_domains = load_news_domains()
    for entity in all_source_entities:
        entity_rows = checked_by_entity.get(entity, [])
        ok_count = sum(1 for r in entity_rows if r["last_status"] == "ok")
        label = f"{entity} — {ok_count}/{len(entity_rows)} source(s) currently OK" if entity_rows else f"{entity} — not yet checked"
        with st.expander(label):
            seen = set()
            for row in entity_rows:
                seen.add((row["source_type"], row["source_url"]))
                icon = _source_icon(row["source_type"], row["source_url"], news_domains)
                status_icon = "✅" if row["last_status"] == "ok" else "⚠️"
                st.markdown(
                    f"{icon} [{row['source_url']}]({row['source_url']}) — {status_icon} "
                    f"last checked {row['last_checked']} ({row['check_count']} check(s) total)"
                )
                if row["last_status"] != "ok" and row["last_error"]:
                    st.caption(f"Last error: {row['last_error']}")
            for cfg in configured.get(entity, []):
                if (cfg["source_type"], cfg["source_url"]) in seen:
                    continue
                icon = _source_icon(cfg["source_type"], cfg["source_url"], news_domains)
                st.markdown(f"{icon} [{cfg['source_url']}]({cfg['source_url']}) — not yet checked")
