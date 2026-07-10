"""Streamlit dashboard: InstaPay/PESONet transfer fees per BSP-supervised institution.

Run locally with:  streamlit run app.py
Deploys as-is to Streamlit Community Cloud (this file is the entrypoint).
"""
from __future__ import annotations

import html
import os

import pandas as pd
import streamlit as st

from storage import db

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

# Fixed status palette (never themed) -- see the project's data-viz reference.
# Used as a translucent overlay so it reads reasonably on both Streamlit's
# light and dark surfaces, since plain inline styles can't media-query the theme.
_GOOD_BG = "rgba(12, 163, 12, 0.16)"
_WARNING_BG = "rgba(250, 178, 25, 0.22)"
_NEUTRAL_BG = "rgba(128, 128, 128, 0.14)"
_MUTED_TEXT = "#898781"

FEE_TABLE_COLUMNS = ["entity", "network", "fee_type", "amount", "conditions", "effective_date", "scraped_at"]

_CARD_CSS = """
<style>
.entity-card {
    border: 1px solid rgba(128,128,128,0.25); border-radius: 10px;
    padding: 16px 20px; margin-bottom: 16px;
}
.entity-card-header {
    display: flex; justify-content: space-between; align-items: baseline;
    flex-wrap: wrap; gap: 8px; margin-bottom: 6px;
}
.entity-name { font-size: 1.05rem; font-weight: 600; }
.entity-flag {
    font-size: 0.82rem; padding: 2px 10px; border-radius: 6px;
    background: __WARNING_BG__;
}
.network-block { margin-top: 12px; }
.network-name {
    font-weight: 600; font-size: 0.92rem; margin-bottom: 4px;
    text-transform: uppercase; letter-spacing: 0.02em; color: __MUTED__;
}
.condition-row {
    display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap;
    padding: 6px 0; border-bottom: 1px solid rgba(128,128,128,0.12);
}
.condition-row:last-child { border-bottom: none; }
.condition-badge {
    font-size: 0.78rem; font-weight: 600; padding: 2px 10px; border-radius: 10px;
    white-space: nowrap; font-variant-numeric: tabular-nums;
}
.badge-free { background: __GOOD_BG__; }
.badge-promo { background: __WARNING_BG__; }
.badge-flat, .badge-tiered { background: __NEUTRAL_BG__; }
.condition-text { flex: 1 1 auto; min-width: 200px; }
.condition-date { color: __MUTED__; font-size: 0.85em; }
.condition-source { font-size: 0.82rem; white-space: nowrap; }
.no-data { color: __MUTED__; font-style: italic; font-size: 0.9rem; padding: 4px 0; }
.fee-link { color: inherit; text-decoration: none; border-bottom: 1px dashed currentColor; }
.fee-link:hover { border-bottom-style: solid; }
</style>
""".replace("__WARNING_BG__", _WARNING_BG).replace("__GOOD_BG__", _GOOD_BG) \
   .replace("__NEUTRAL_BG__", _NEUTRAL_BG).replace("__MUTED__", _MUTED_TEXT)


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


def _badge_class(fee_type: str) -> str:
    return {"free": "badge-free", "promo": "badge-promo"}.get(fee_type, "badge-flat")


def _badge_label(fee_type: str, amount: float | None) -> str:
    if fee_type == "free":
        return "Free"
    if amount is not None:
        return f"PHP {amount:,.2f}"
    return fee_type.capitalize()


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
    return _CARD_CSS + "".join(cards)


st.title("Philippine InstaPay / PESONet Transfer Fees Monitor")
st.caption(
    "Fees are collected only from each institution's own public website, press releases, or "
    "Facebook page -- never from BSP reports -- so this data can be used to independently "
    "check against those reports rather than being derived from them. Over-the-counter fees "
    "are excluded; institutions can have more than one concurrent fee condition (e.g. a "
    "permanent rate alongside a limited-time promo) -- all are shown, not just one."
)

timestamps = load_timestamps()

st.sidebar.header("View")
view_mode = st.sidebar.radio("Show fees as of", ["Latest", "Choose a point in time"])

if view_mode == "Latest" or not timestamps:
    fees = load_latest_grouped()
    as_of_label = "Latest available data"
else:
    selected_ts = st.sidebar.select_slider("Timestamp", options=timestamps, value=timestamps[-1])
    fees = load_as_of_grouped(selected_ts)
    as_of_label = f"As of {selected_ts}"

st.subheader(as_of_label)

flagged = load_flagged()

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
