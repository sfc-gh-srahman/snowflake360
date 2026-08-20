"""Cost Attribution - chargeback with the three-bucket split leading, not buried."""

from __future__ import annotations

import sys

import pandas as pd
from pathlib import Path

import altair as alt
import streamlit as st

sys.path.append(str(Path(__file__).parent.parent))
from lib.sf import (  # noqa: E402
    CATEGORY_SCALE,
    DB,
    init_page,
    mi,
    num,
    q,
    range_selector,
    render_account_scope_banner,
    render_freshness_caption,
    section,
    table_with_download,
    usd,
)

st.set_page_config(page_title="Cost Attribution | Snowflake360", layout="wide")
PAGE = "Cost Attribution"
init_page("Cost Attribution")
render_account_scope_banner()
render_freshness_caption()

_range_label, days = range_selector("attribution", "1Y")

# ---------------------------------------------------------------------------
# Lead with the three buckets. An attributed-only view understates cost heavily.
# ---------------------------------------------------------------------------
section("What is actually attributable")

buckets = q(
    f"""SELECT BUCKET, SUM(CREDITS) AS CREDITS, SUM(DOLLARS) AS DOLLARS
        FROM {DB}.CURATED.FCT_QUERY_ATTRIBUTION
        WHERE USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
        GROUP BY 1 ORDER BY CREDITS DESC""",
    PAGE,
    "three_buckets",
)

if buckets.empty:
    st.warning("No attribution data in the selected window.")
    st.stop()

total_cr = buckets["CREDITS"].sum()
attributed = buckets.loc[buckets["BUCKET"] == "QUERY_ATTRIBUTED", "CREDITS"].sum()
attr_share = 100.0 * attributed / total_cr if total_cr else 0

# total_cr > 0 matters: with rows present but all credits zero or NULL, attr_share
# is 0, and "Only 0.0% of credits are attributable" fired as an alarming finding
# about an account that had simply not been measured yet.
if total_cr > 0 and attr_share < 50:
    st.warning(
        f"**Only {attr_share:,.1f}% of credits are attributable to a specific query.** "
        "Chargeback by user or role covers that slice alone. The rest is warehouse idle time "
        "and serverless or AI services, which carry no query identity. Totals below are shown "
        "in all three buckets so nothing silently disappears.",
        icon=mi("warning"),
    )

cols = st.columns(len(buckets))
labels = {
    "QUERY_ATTRIBUTED": "Query attributed",
    "WAREHOUSE_IDLE": "Warehouse idle",
    "NON_WAREHOUSE": "Serverless / AI",
}
for col, (_, r) in zip(cols, buckets.iterrows()):
    col.metric(
        labels.get(r["BUCKET"], r["BUCKET"]),
        usd(r["DOLLARS"]),
        f"{100.0 * r['CREDITS'] / total_cr:,.1f}% of credits" if total_cr else "n/a",
        delta_color="off",
    )

st.altair_chart(
    alt.Chart(buckets)
    .mark_arc(innerRadius=60)
    .encode(
        theta=alt.Theta("CREDITS:Q"),
        color=alt.Color("BUCKET:N", title="Bucket",
                                scale=alt.Scale(range=CATEGORY_SCALE)),
        tooltip=["BUCKET", alt.Tooltip("CREDITS:Q", format=",.2f"),
                 alt.Tooltip("DOLLARS:Q", title="Dollars", format="$,.2f")],
    )
    .properties(height=240),
    use_container_width=True,
)

# ---------------------------------------------------------------------------
# Idle allocation toggle
# ---------------------------------------------------------------------------
st.divider()
section("Chargeback")

allocate = st.toggle(
    "Allocate warehouse idle to users pro-rata",
    value=False,
    help=(
        "Off: only query-attributed credits are charged, and idle stays unallocated. "
        "On: each warehouse's idle credits are split across its users in proportion to their "
        "attributed credits, so every dollar lands on someone."
    ),
)

dim = st.selectbox(
    "Chargeback dimension",
    ["USER_NAME", "ROLE_NAME", "WAREHOUSE_NAME", "DATABASE_NAME", "QUERY_TAG", "QUERY_TYPE"],
    format_func=lambda s: s.replace("_", " ").title(),
)

if allocate:
    charge = q(
        f"""WITH attr AS (
              SELECT WAREHOUSE_NAME, {dim} AS DIM, SUM(CREDITS) AS CR, SUM(QUERY_COUNT) AS QC
              FROM {DB}.CURATED.FCT_QUERY_ATTRIBUTION
              WHERE BUCKET = 'QUERY_ATTRIBUTED'
                AND USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
              GROUP BY 1,2
            ),
            wh_attr AS (SELECT WAREHOUSE_NAME, SUM(CR) AS WH_CR FROM attr GROUP BY 1),
            wh_idle AS (
              SELECT WAREHOUSE_NAME, SUM(CREDITS) AS IDLE_CR
              FROM {DB}.CURATED.FCT_QUERY_ATTRIBUTION
              WHERE BUCKET = 'WAREHOUSE_IDLE'
                AND USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
              GROUP BY 1
            )
            SELECT a.DIM,
                   SUM(a.CR)                                              AS ATTRIBUTED_CR,
                   SUM(COALESCE(i.IDLE_CR,0) * a.CR / NULLIF(w.WH_CR,0))  AS ALLOCATED_IDLE_CR,
                   SUM(a.CR + COALESCE(i.IDLE_CR,0) * a.CR / NULLIF(w.WH_CR,0)) AS TOTAL_CR,
                   SUM(a.QC)                                              AS QUERIES
            FROM attr a
            JOIN wh_attr w ON w.WAREHOUSE_NAME = a.WAREHOUSE_NAME
            LEFT JOIN wh_idle i ON i.WAREHOUSE_NAME = a.WAREHOUSE_NAME
            GROUP BY 1 ORDER BY TOTAL_CR DESC LIMIT 40""",
        PAGE,
        f"chargeback_allocated_{dim}",
    )
    value_col = "TOTAL_CR"
    st.caption(
        "Idle is split across each warehouse's users in proportion to attributed credits. "
        "Debatable fairness: whoever wakes a warehouse subsidizes everyone who follows."
    )
else:
    charge = q(
        f"""SELECT {dim} AS DIM, SUM(CREDITS) AS ATTRIBUTED_CR, SUM(QUERY_COUNT) AS QUERIES,
                   SUM(BYTES_SCANNED)/POWER(1024,4) AS TB_SCANNED
            FROM {DB}.CURATED.FCT_QUERY_ATTRIBUTION
            WHERE BUCKET = 'QUERY_ATTRIBUTED'
              AND USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
            GROUP BY 1 ORDER BY ATTRIBUTED_CR DESC LIMIT 40""",
        PAGE,
        f"chargeback_attributed_{dim}",
    )
    value_col = "ATTRIBUTED_CR"

if charge.empty:
    st.info("No attributed credits for that dimension in the selected window.")
else:
    rate = q(
        f"""SELECT PRICE_PER_PLATFORM_CREDIT AS R FROM {DB}.CONFIG.SUBSCRIPTION
            WHERE ACCOUNT_NAME = CURRENT_ACCOUNT_NAME() AND VALID_TO IS NULL""",
        PAGE,
        "installed_rate",
    )
    # No configured rate means dollars cannot be computed. Pricing at 0.0 silently
    # rendered every chargeback row as "$0.00", which reads as a real measurement
    # of nothing owed rather than as a missing input.
    _has_rate = (not rate.empty) and pd.notna(rate.iloc[0]["R"]) and float(rate.iloc[0]["R"]) > 0
    if not _has_rate:
        st.info(
            "No per-credit price is configured for this account, so credits cannot be "
            "converted to currency. Chargeback is shown in credits only. Set your "
            "negotiated rate on the **Rates** tab of Setup & Settings.",
            icon=mi("info"),
        )
    r = float(rate.iloc[0]["R"]) if _has_rate else None

    charge["DOLLARS"] = charge[value_col] * r if _has_rate else float("nan")
    st.altair_chart(
        alt.Chart(charge.head(20))
        .mark_bar()
        .encode(
            x=alt.X("DOLLARS:Q", title="Dollars"),
            y=alt.Y("DIM:N", sort="-x", title=None),
            tooltip=[alt.Tooltip("DIM:N", title="Dimension"), alt.Tooltip(f"{value_col}:Q", title="Credits", format=",.3f"),
                     alt.Tooltip("DOLLARS:Q", title="Dollars", format="$,.2f")],
        )
        .properties(height=460),
        use_container_width=True,
    )

    show = charge.copy()
    show["Credits"] = show[value_col].apply(lambda v: num(v, 3))
    show["Dollars"] = show["DOLLARS"].apply(lambda v: usd(v, 2))
    _denom = show[value_col].sum()
    show["Share"] = ((100.0 * show[value_col] / _denom).round(2)
                     if _denom else float("nan"))
    keep = ["DIM", "Credits", "Dollars", "Share"] if _has_rate else ["DIM", "Credits", "Share"]
    if "QUERIES" in show.columns:
        keep.insert(1, "QUERIES")
    if "ALLOCATED_IDLE_CR" in show.columns:
        show["Allocated idle"] = show["ALLOCATED_IDLE_CR"].apply(lambda v: num(v, 3))
        keep.append("Allocated idle")
    # table_with_download already renders a CSV button for this exact frame. A
    # second one sat here from before that helper existed, offering the same rows
    # under a different filename -- two adjacent download buttons where one of
    # them is the wrong one to click.
    table_with_download(
        show[keep].rename(
            columns={"DIM": dim.replace("_", " ").title(), "QUERIES": "Queries"}
        ),
        f"snowflake360_chargeback_{dim.lower()}", "attribution_1",
    )

# ---------------------------------------------------------------------------
# Non-warehouse detail: invisible to attribution entirely
# ---------------------------------------------------------------------------
st.divider()
section("Serverless and AI, by service")
st.caption(
    "These credits never appear in QUERY_ATTRIBUTION_HISTORY at all, so they cannot be "
    "charged to a user or role. They are attributable only to a service."
)

nonwh = q(
    f"""SELECT WAREHOUSE_NAME AS SERVICE, SUM(CREDITS) AS CREDITS, SUM(DOLLARS) AS DOLLARS
        FROM {DB}.CURATED.FCT_QUERY_ATTRIBUTION
        WHERE BUCKET = 'NON_WAREHOUSE'
          AND USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
        GROUP BY 1 ORDER BY CREDITS DESC""",
    PAGE,
    "non_warehouse_detail",
)
if not nonwh.empty:
    show = nonwh.copy()
    show["Credits"] = show["CREDITS"].apply(lambda v: num(v, 3))
    show["Dollars"] = show["DOLLARS"].apply(lambda v: usd(v, 2))
    show["Share"] = (100.0 * show["CREDITS"] / show["CREDITS"].sum()).round(2)
    table_with_download(
    show[["SERVICE", "Credits", "Dollars", "Share"]],
    "snowflake360_attribution_2", "attribution_2",
)
else:
    st.info(
        "No serverless or AI credits in this window.",
        icon=mi("info"),
    )
