"""Consumption - spend by category, account, and service type over the fiscal calendar."""

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
    q,
    render_freshness_caption,
    section,
    table_with_download,
    range_selector,
    usd,
    render_org_unavailable,
    mi,
)

st.set_page_config(page_title="Consumption | Snowflake360", layout="wide")
PAGE = "Consumption"
init_page("Consumption")
render_freshness_caption()


_range_label, days = range_selector("consumption", "1Y")

# ---------------------------------------------------------------------------
# Category KPIs with period-over-period delta
# ---------------------------------------------------------------------------
cats = q(
    f"""WITH cur AS (
          SELECT CATEGORY, SUM(NET_IN_CURRENCY) AS USD
          FROM {DB}.CURATED.FCT_DAILY_CURRENCY
          WHERE USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
          GROUP BY 1
        ),
        prev AS (
          SELECT CATEGORY, SUM(NET_IN_CURRENCY) AS USD
          FROM {DB}.CURATED.FCT_DAILY_CURRENCY
          WHERE USAGE_DATE_UTC >= DATEADD('day', -{days * 2}, CURRENT_DATE())
            AND USAGE_DATE_UTC <  DATEADD('day', -{days}, CURRENT_DATE())
          GROUP BY 1
        )
        SELECT COALESCE(c.CATEGORY, p.CATEGORY) AS CATEGORY,
               COALESCE(c.USD, 0) AS USD_CURRENT,
               COALESCE(p.USD, 0) AS USD_PRIOR
        FROM cur c FULL OUTER JOIN prev p ON p.CATEGORY = c.CATEGORY
        ORDER BY USD_CURRENT DESC""",
    PAGE,
    "category_kpis",
)

total_cur = cats["USD_CURRENT"].sum()
total_prv = cats["USD_PRIOR"].sum()

if cats.empty:
    # Distinguish a missing source from a measurement of zero. Currency comes only
    # from ORGANIZATION_USAGE, so outside an organization account this page has
    # nothing to read -- which is a different statement from "you spent nothing".
    if not render_org_unavailable("Consumption in currency"):
        st.info(
            f"No currency-rated usage in the last {days} days. Either there was no "
            "spend in this window, or usage has not been rated yet -- rating lags "
            "consumption by about a day.",
            icon=mi("info"),
        )
    st.stop()

cols = st.columns(len(cats) + 1)
delta = (
    f"{100.0 * (total_cur - total_prv) / total_prv:+,.1f}% vs prior {days}d"
    if total_prv
    else "no prior period"
)
cols[0].metric("Total", usd(total_cur), delta, delta_color="inverse")
for i, (_, r) in enumerate(cats.iterrows(), start=1):
    dlt = (
        f"{100.0 * (r['USD_CURRENT'] - r['USD_PRIOR']) / r['USD_PRIOR']:+,.1f}%"
        if r["USD_PRIOR"]
        else "new"
    )
    cols[i].metric(str(r["CATEGORY"]).title(), usd(r["USD_CURRENT"]), dlt, delta_color="inverse")

st.caption(
    f"{_range_label} ({days} days) versus the prior equal-length period. Categories follow Snowflake's "
    "RATING_TYPE: Compute, Storage, Other. Deltas are inverted so growth reads red."
)

# ---------------------------------------------------------------------------
# Monthly series by category on the Snowflake fiscal calendar
# ---------------------------------------------------------------------------
section("Monthly consumption")

monthly = q(
    f"""SELECT dd.CAL_MONTH_LABEL, dd.FISCAL_QUARTER_LABEL, f.CATEGORY,
               SUM(f.NET_IN_CURRENCY) AS USD
        FROM {DB}.CURATED.FCT_DAILY_CURRENCY f
        JOIN {DB}.CURATED.DIM_DATE dd ON dd.DATE_UTC = f.USAGE_DATE_UTC
        GROUP BY 1,2,3 ORDER BY 1""",
    PAGE,
    "monthly_by_category",
)

if not monthly.empty:
    st.altair_chart(
        alt.Chart(monthly)
        .mark_bar()
        .encode(
            x=alt.X("CAL_MONTH_LABEL:N", title="Month (UTC)", sort=None),
            y=alt.Y("USD:Q", title="Dollars", stack=True),
            color=alt.Color("CATEGORY:N", title="Category",
                                scale=alt.Scale(range=CATEGORY_SCALE)),
            tooltip=[
                "CAL_MONTH_LABEL",
                "FISCAL_QUARTER_LABEL",
                "CATEGORY",
                alt.Tooltip("USD:Q", format="$,.2f"),
            ],
        )
        .properties(height=300),
        use_container_width=True,
    )
    st.caption(
        "Fiscal quarters follow Snowflake's calendar: FY starts February 1, so Q1 is Feb-Apr."
    )
else:
    st.info(
        "No monthly consumption recorded in this window.",
        icon=mi("info"),
    )

# ---------------------------------------------------------------------------
# Consumption breakout
# ---------------------------------------------------------------------------
section("Consumption breakout")
t1, t2, t3 = st.tabs(["Over time by service", "Ranked services", "By account"])

with t1:
    stacked = q(
        f"""SELECT USAGE_DATE_UTC, DISPLAY_GROUP, SUM(NET_IN_CURRENCY) AS USD
            FROM {DB}.CURATED.FCT_DAILY_CURRENCY
            WHERE USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
              AND DISPLAY_GROUP IN (
                SELECT DISPLAY_GROUP FROM {DB}.CURATED.FCT_DAILY_CURRENCY
                WHERE USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
                GROUP BY 1 ORDER BY SUM(NET_IN_CURRENCY) DESC LIMIT 10)
            GROUP BY 1,2 ORDER BY 1""",
        PAGE,
        "stacked_service_over_time",
    )
    if not stacked.empty:
        st.altair_chart(
            alt.Chart(stacked)
            .mark_area()
            .encode(
                x=alt.X("USAGE_DATE_UTC:T", title="Date (UTC)"),
                y=alt.Y("USD:Q", title="Dollars", stack=True),
                color=alt.Color("DISPLAY_GROUP:N", title="Service",
                                scale=alt.Scale(range=CATEGORY_SCALE)),
                tooltip=["USAGE_DATE_UTC:T", "DISPLAY_GROUP", alt.Tooltip("USD:Q", format=",.2f")],
            )
            .properties(height=320),
            use_container_width=True,
        )
        st.caption("Top 10 services by spend in the selected window.")
    else:
        st.info(
            "No service-level spend in this window.",
            icon=mi("info"),
        )

with t2:
    ranked = q(
        f"""SELECT DISPLAY_GROUP, CREDIT_CLASS, CATEGORY,
                   SUM(NET_IN_CURRENCY) AS USD, SUM(USAGE_QTY) AS QTY
            FROM {DB}.CURATED.FCT_DAILY_CURRENCY
            WHERE USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
            GROUP BY 1,2,3 HAVING SUM(NET_IN_CURRENCY) <> 0
            ORDER BY USD DESC""",
        PAGE,
        "ranked_services",
    )
    if not ranked.empty:
        st.altair_chart(
            alt.Chart(ranked.head(20))
            .mark_bar()
            .encode(
                x=alt.X("USD:Q", title="Dollars"),
                y=alt.Y("DISPLAY_GROUP:N", sort="-x", title=None),
                color=alt.Color("CREDIT_CLASS:N", title="Credit class",
                                scale=alt.Scale(range=CATEGORY_SCALE)),
                tooltip=["DISPLAY_GROUP", "CATEGORY", alt.Tooltip("USD:Q", format=",.2f")],
            )
            .properties(height=420),
            use_container_width=True,
        )
        show = ranked.copy()
        show["Dollars"] = show["USD"].apply(usd, decimals=2)
        show["Share"] = (100.0 * show["USD"] / show["USD"].sum()).round(2)
        table_with_download(
    show[["DISPLAY_GROUP", "CREDIT_CLASS", "CATEGORY", "Dollars", "Share"]],
    "snowflake360_consumption_1", "consumption_1",
)
    else:
        st.info(
            "No rated services in this window.",
            icon=mi("info"),
        )

with t3:
    by_acct = q(
        f"""SELECT f.ACCOUNT_NAME, a.SERVICE_LEVEL, a.REGION,
                   SUM(f.NET_IN_CURRENCY) AS USD,
                   SUM(CASE WHEN f.CREDIT_CLASS='AI' THEN f.NET_IN_CURRENCY ELSE 0 END) AS AI_USD
            FROM {DB}.CURATED.FCT_DAILY_CURRENCY f
            JOIN {DB}.CURATED.DIM_ACCOUNT a ON a.ACCOUNT_KEY = f.ACCOUNT_KEY
            WHERE f.USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
            GROUP BY 1,2,3 ORDER BY USD DESC""",
        PAGE,
        "by_account",
    )
    if not by_acct.empty:
        show = by_acct.copy()
        show["Total"] = show["USD"].apply(usd)
        show["AI spend"] = show["AI_USD"].apply(usd)
        # NaN, not pd.NA: pd.NA makes the column object dtype and Series.round
        # then crashes on NAType, but only when some account has zero spend.
        show["AI share"] = (
            100.0 * show["AI_USD"] / show["USD"].replace(0, float("nan"))
        ).round(2)
        table_with_download(
    show[["ACCOUNT_NAME", "SERVICE_LEVEL", "REGION", "Total", "AI spend", "AI share"]],
    "snowflake360_consumption_2", "consumption_2",
)
    else:
        st.info(
            "No account-level spend in this window.",
            icon=mi("info"),
        )

# ---------------------------------------------------------------------------
# Monthly pivot with CSV export, matching A360's export affordance
# ---------------------------------------------------------------------------
section("Monthly pivot")

pivot_src = q(
    f"""SELECT dd.CAL_MONTH_LABEL AS MONTH, f.DISPLAY_GROUP,
               SUM(f.NET_IN_CURRENCY) AS USD
        FROM {DB}.CURATED.FCT_DAILY_CURRENCY f
        JOIN {DB}.CURATED.DIM_DATE dd ON dd.DATE_UTC = f.USAGE_DATE_UTC
        GROUP BY 1,2""",
    PAGE,
    "monthly_pivot",
)

if not pivot_src.empty:
    pv = pivot_src.pivot_table(
        index="DISPLAY_GROUP", columns="MONTH", values="USD", aggfunc="sum", fill_value=0
    )
    pv["Total"] = pv.sum(axis=1)
    pv = pv.sort_values("Total", ascending=False)
    # Styler formats the display only; the CSV download keeps raw numerics so the
    # file stays usable in a spreadsheet.
    table_with_download(
        pv.style.format("${:,.2f}"),
        "snowflake360_monthly_consumption", "consumption_pivot",
        hide_index=False,
    )
else:
    st.info(
        "No monthly breakdown available for this window.",
        icon=mi("info"),
    )

st.divider()
st.caption(
    "Dollars come from ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY, net of adjustments. "
    "Adjustment rows are tracked separately and never counted as consumption."
)
