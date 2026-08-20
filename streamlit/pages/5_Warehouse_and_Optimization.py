"""Warehouse & Optimization - period-over-period spend plus native QUERY_INSIGHTS."""

from __future__ import annotations

import sys
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
    render_org_unavailable,
)

st.set_page_config(page_title="Warehouse & Optimization | Snowflake360", layout="wide")
PAGE = "Warehouse & Optimization"
init_page("Warehouse & Optimization")
render_freshness_caption()

_range_label, days = range_selector("warehouse", "1Y")

# ---------------------------------------------------------------------------
# Org-wide warehouse spend, period over period
# ---------------------------------------------------------------------------
section("Warehouse spend, period over period")
st.caption(f"All in-scope accounts. {_range_label} ({days} days) versus the prior {days} days.")

wh = q(
    f"""WITH cur AS (
          SELECT f.ACCOUNT_NAME, f.DISPLAY_GROUP, SUM(f.NET_IN_CURRENCY) AS USD
          FROM {DB}.CURATED.FCT_DAILY_CURRENCY f
          WHERE f.SERVICE_TYPE = 'WAREHOUSE_METERING'
            AND f.USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
          GROUP BY 1,2
        ),
        prv AS (
          SELECT f.ACCOUNT_NAME, SUM(f.NET_IN_CURRENCY) AS USD
          FROM {DB}.CURATED.FCT_DAILY_CURRENCY f
          WHERE f.SERVICE_TYPE = 'WAREHOUSE_METERING'
            AND f.USAGE_DATE_UTC >= DATEADD('day', -{days * 2}, CURRENT_DATE())
            AND f.USAGE_DATE_UTC <  DATEADD('day', -{days}, CURRENT_DATE())
          GROUP BY 1
        )
        SELECT COALESCE(c.ACCOUNT_NAME, p.ACCOUNT_NAME) AS ACCOUNT_NAME,
               COALESCE(c.USD,0) AS USD_CURRENT, COALESCE(p.USD,0) AS USD_PRIOR,
               COALESCE(c.USD,0) - COALESCE(p.USD,0) AS DIFF
        FROM cur c FULL OUTER JOIN prv p ON p.ACCOUNT_NAME = c.ACCOUNT_NAME
        ORDER BY USD_CURRENT DESC""",
    PAGE,
    "warehouse_period_over_period",
)

if not wh.empty:
    st.altair_chart(
        alt.Chart(wh)
        .mark_bar()
        .encode(
            x=alt.X("DIFF:Q", title=f"Change vs prior {days}d (dollars)"),
            y=alt.Y("ACCOUNT_NAME:N", sort="-x", title=None),
            color=alt.condition(
                alt.datum.DIFF > 0, alt.value("#c94040"), alt.value("#3a8a3a")
            ),
            tooltip=[
                "ACCOUNT_NAME",
                alt.Tooltip("USD_CURRENT:Q", title="Current", format="$,.2f"),
                alt.Tooltip("USD_PRIOR:Q", title="Prior", format="$,.2f"),
                alt.Tooltip("DIFF:Q", title="Change", format="$,.2f"),
            ],
        )
        .properties(height=220),
        use_container_width=True,
    )
    show = wh.copy()
    show["Current"] = show["USD_CURRENT"].apply(usd)
    show["Prior"] = show["USD_PRIOR"].apply(usd)
    show["Change"] = show["DIFF"].apply(usd)
    # Divide by NaN, not pd.NA. Substituting pd.NA makes the column object dtype,
    # so Series.round falls back to elementwise round() and NAType has no
    # __round__ -- which crashes only for ranges where some account happens to
    # have zero prior spend.
    show["Change %"] = (
        100.0 * show["DIFF"] / show["USD_PRIOR"].replace(0, float("nan"))
    ).round(1)
    table_with_download(
    show[["ACCOUNT_NAME", "Current", "Prior", "Change", "Change %"]],
    "snowflake360_warehouse_1", "warehouse_1",
)
else:
    st.info(
        "No warehouse spend in this window. In ACCOUNT mode this panel needs organization data, which is unavailable.",
        icon=mi("info"),
    )

# ---------------------------------------------------------------------------
# Installed-account warehouse detail
# ---------------------------------------------------------------------------
st.divider()
section("Warehouse detail")
render_account_scope_banner()

detail = q(
    f"""WITH cur AS (
          SELECT WAREHOUSE_ID, WAREHOUSE_NAME, SUM(CREDITS_USED) AS CR
          FROM {DB}.LANDING.LND_WAREHOUSE_METERING
          WHERE USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
          GROUP BY 1,2
        ),
        prv AS (
          SELECT WAREHOUSE_ID, SUM(CREDITS_USED) AS CR
          FROM {DB}.LANDING.LND_WAREHOUSE_METERING
          WHERE USAGE_DATE_UTC >= DATEADD('day', -{days * 2}, CURRENT_DATE())
            AND USAGE_DATE_UTC <  DATEADD('day', -{days}, CURRENT_DATE())
          GROUP BY 1
        )
        SELECT c.WAREHOUSE_NAME, c.WAREHOUSE_ID, c.CR AS CREDITS_CURRENT,
               COALESCE(p.CR,0) AS CREDITS_PRIOR, c.CR - COALESCE(p.CR,0) AS DIFF
        FROM cur c LEFT JOIN prv p ON p.WAREHOUSE_ID = c.WAREHOUSE_ID
        ORDER BY c.CR DESC LIMIT 20""",
    PAGE,
    "warehouse_detail_top20",
)

if detail.empty:
    st.info("No warehouse activity in the selected window.", icon=mi("info"))
else:
    show = detail.copy()
    show["Warehouse"] = show["WAREHOUSE_NAME"] + " (" + show["WAREHOUSE_ID"].astype(str) + ")"
    show["Credits"] = show["CREDITS_CURRENT"].apply(lambda v: num(v, 3))
    show["Prior"] = show["CREDITS_PRIOR"].apply(lambda v: num(v, 3))
    show["Change"] = show["DIFF"].apply(lambda v: num(v, 3))
    table_with_download(
    show[["Warehouse", "Credits", "Prior", "Change"]],
    "snowflake360_warehouse_2", "warehouse_2",
)
    st.caption(
        "Warehouse ID is shown alongside the name because ACCOUNT_USAGE retains dropped "
        "objects and names get reused, so the name alone is ambiguous."
    )

# ---------------------------------------------------------------------------
# Idle time, the dominant cost driver this app surfaces
# ---------------------------------------------------------------------------
section("Idle versus query-attributed")

idle = q(
    f"""SELECT BUCKET, SUM(CREDITS) AS CREDITS, SUM(DOLLARS) AS DOLLARS
        FROM {DB}.CURATED.FCT_QUERY_ATTRIBUTION
        WHERE USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
        GROUP BY 1 ORDER BY CREDITS DESC""",
    PAGE,
    "idle_split",
)
if not idle.empty:
    tot = idle["CREDITS"].sum()
    cols = st.columns(len(idle))
    for col, (_, r) in zip(cols, idle.iterrows()):
        col.metric(
            r["BUCKET"].replace("_", " ").title(),
            num(r["CREDITS"], 2),
            f"{100.0 * r['CREDITS'] / tot:,.1f}% of credits",
            delta_color="off",
        )
    st.caption(
        "Warehouse idle is real billed time with no query attached. Snowflake's own "
        "Optimization Insights flags warehouses idle more than 50% of active time and "
        "recommends lowering AUTO_SUSPEND."
    )
else:
    st.info(
        "No warehouse credits in this window, so there is no idle-versus-attributed split to show.",
        icon=mi("info"),
    )

# ---------------------------------------------------------------------------
# Native query optimization opportunities
# ---------------------------------------------------------------------------
st.divider()
section("Query optimization opportunities")
st.caption(
    "Sourced from ACCOUNT_USAGE.QUERY_INSIGHTS. Snowflake generates the finding and the "
    "suggested fix; this app only groups and displays them."
)

topics = q(
    f"""SELECT INSIGHT_TOPIC, INSIGHT_TYPE_ID,
               COUNT(*) AS FINDINGS, COUNT_IF(IS_OPPORTUNITY) AS OPPORTUNITIES,
               ROUND(AVG(TOTAL_ELAPSED_TIME)/1000.0, 2) AS AVG_ELAPSED_S
        FROM {DB}.CURATED.FCT_OPTIMIZATION_OPPORTUNITY
        GROUP BY 1,2 ORDER BY OPPORTUNITIES DESC""",
    PAGE,
    "insight_topics",
)

if topics.empty:
    # Snowflake's optimization insights only appear once there is enough query
    # history to analyse, so an empty result on a new account means "not evaluated",
    # not "nothing to improve".
    st.info(
        "No query optimization insights recorded yet. Snowflake generates these from "
        "observed query patterns, so they appear once the account has accumulated "
        "enough history.",
        icon=mi("info"),
    )
else:
    o1, o2 = st.columns([2, 1])
    with o1:
        st.altair_chart(
            alt.Chart(topics)
            .mark_bar()
            .encode(
                x=alt.X("OPPORTUNITIES:Q", title="Opportunities"),
                y=alt.Y("INSIGHT_TYPE_ID:N", sort="-x", title=None),
                color=alt.Color("INSIGHT_TOPIC:N", title="Topic",
                                scale=alt.Scale(range=CATEGORY_SCALE)),
                tooltip=["INSIGHT_TYPE_ID", "INSIGHT_TOPIC", "OPPORTUNITIES", "AVG_ELAPSED_S"],
            )
            .properties(height=260),
            use_container_width=True,
        )
    with o2:
        st.metric("Total opportunities", int(topics["OPPORTUNITIES"].sum()))
        st.metric("Distinct topics", int(topics["INSIGHT_TOPIC"].nunique()))

    st.write("**Snowflake's findings and suggested fixes**")
    detail_ins = q(
        f"""SELECT INSIGHT_TOPIC, INSIGHT_TYPE_ID, WAREHOUSE_NAME,
                   ROUND(TOTAL_ELAPSED_TIME/1000.0,2) AS ELAPSED_S,
                   MESSAGE_TEXT,
                   TO_VARCHAR(SUGGESTIONS_VARIANT) AS SUGGESTIONS,
                   QUERY_ID
            FROM {DB}.CURATED.FCT_OPTIMIZATION_OPPORTUNITY
            WHERE IS_OPPORTUNITY
            ORDER BY TOTAL_ELAPSED_TIME DESC LIMIT 50""",
        PAGE,
        "insight_detail",
    )
    table_with_download(
    detail_ins.rename(
            columns={
                "INSIGHT_TOPIC": "Topic",
                "INSIGHT_TYPE_ID": "Type",
                "WAREHOUSE_NAME": "Warehouse",
                "ELAPSED_S": "Elapsed (s)",
                "MESSAGE_TEXT": "Finding",
                "SUGGESTIONS": "Suggested fix",
                "QUERY_ID": "Query ID",
            }
        ),
    "snowflake360_warehouse_3", "warehouse_3",
)

# ---------------------------------------------------------------------------
# Account-level Optimization Insights: Snowsight-only, so link out
# ---------------------------------------------------------------------------
st.divider()
with st.expander("Account-level Optimization Insights (Snowsight)", expanded=False):
    st.markdown(
        "Snowflake also computes nine account-level savings insights weekly. They are surfaced "
        "in Snowsight rather than as a queryable view, so this app links out rather than "
        "reimplementing them:\n\n"
        "- Rarely used tables with automatic clustering\n"
        "- Rarely used materialized views\n"
        "- Rarely used search optimization paths\n"
        "- Large tables never queried\n"
        "- Tables over 100 GB written but not read\n"
        "- Short-lived permanent tables\n"
        "- **Active warehouses with large gaps between queries (idle > 50%)**\n"
        "- Inefficient multi-cluster warehouse configuration\n"
        "- Tables with significant cold file storage\n\n"
        "Find them under **Admin » Cost Management » Account Overview » Optimization insights**."
    )
