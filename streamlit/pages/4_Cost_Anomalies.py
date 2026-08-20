"""Cost Anomalies - Snowflake-native detection with native drill-down. No custom rules."""

from __future__ import annotations

import sys
from pathlib import Path

import altair as alt
import streamlit as st

sys.path.append(str(Path(__file__).parent.parent))
from lib.sf import (  # noqa: E402
    d,
    DB,
    init_page,
    mi,
    num,
    q,
    render_freshness_caption,
    section,
    table_with_download,
    usd,
    org_data_available,
    render_org_unavailable,
)

st.set_page_config(page_title="Cost Anomalies | Snowflake360", layout="wide")
PAGE = "Cost Anomalies"
init_page("Cost Anomalies")
st.caption(
    "Detection is entirely Snowflake-native: ANOMALIES_IN_CURRENCY_DAILY for the org and "
    "ANOMALIES_DAILY for the installed account. No thresholds, no models, no maintenance."
)
render_freshness_caption()

scope = st.radio(
    "Scope", ["ORG", "ACCOUNT"], horizontal=True,
    help="ORG is dollars across in-scope accounts. ACCOUNT is credits for the installed account.",
)

summary = q(
    f"""SELECT SCOPE, MEASURE_TYPE, UNIT,
               COUNT(*)                          AS DAYS,
               COUNT_IF(IS_ANOMALY)              AS ANOMALIES,
               COUNT_IF(NATIVE_FORECAST_WAS_NEGATIVE) AS NEGATIVE_FORECASTS
        FROM {DB}.CURATED.FCT_ANOMALY_DAILY GROUP BY 1,2,3""",
    PAGE,
    "anomaly_summary",
)

row = summary[summary["SCOPE"] == scope]
DAYS_EVALUATED = int(row.iloc[0]["DAYS"]) if not row.empty else 0
c1, c2, c3 = st.columns(3)
if not row.empty:
    r = row.iloc[0]
    c1.metric("Days evaluated", int(r["DAYS"]))
    c2.metric("Anomalies detected", int(r["ANOMALIES"]))
    c3.metric("Negative native forecasts", int(r["NEGATIVE_FORECASTS"]),
              "floored to zero", delta_color="off")
else:
    # Three empty columns said nothing at all. Which of the two reasons applies
    # matters: ORG scope has no source outside an organization account, whereas
    # ACCOUNT scope simply has no history yet.
    c1.metric("Days evaluated", 0)
    st.info(
        "Snowflake has not evaluated this scope yet."
        + ("  ORG scope needs ORGANIZATION_USAGE, which this account does not have."
           if scope == "ORG" and not org_data_available()
           else "  Native anomaly detection needs several days of history before it"
                " produces a baseline."),
        icon=mi("info"),
    )

series = q(
    f"""SELECT ANOMALY_DATE, ACCOUNT_NAME, ANOMALY_ID, IS_ANOMALY, UNIT,
               ACTUAL_VALUE, FORECASTED_VALUE, LOWER_BOUND, UPPER_BOUND,
               VARIANCE_VS_FORECAST
        FROM {DB}.CURATED.FCT_ANOMALY_DAILY
        WHERE SCOPE = '{scope}'
          AND ANOMALY_DATE >= DATEADD('day', -120, CURRENT_DATE())
        ORDER BY ANOMALY_DATE""",
    PAGE,
    "anomaly_series",
)

if series.empty:
    st.info("No anomaly data in the selected scope.", icon=mi("info"))
    st.stop()

unit = series["UNIT"].iloc[0]
agg = (
    series.groupby("ANOMALY_DATE", as_index=False)
    .agg(
        ACTUAL_VALUE=("ACTUAL_VALUE", "sum"),
        FORECASTED_VALUE=("FORECASTED_VALUE", "sum"),
        LOWER_BOUND=("LOWER_BOUND", "sum"),
        UPPER_BOUND=("UPPER_BOUND", "sum"),
        IS_ANOMALY=("IS_ANOMALY", "max"),
    )
)

section(f"Actual vs forecast ({unit})")

band = (
    alt.Chart(agg)
    .mark_area(opacity=0.2)
    .encode(
        x=alt.X("ANOMALY_DATE:T", title="Date (UTC)"),
        y=alt.Y("LOWER_BOUND:Q", title=unit),
        y2="UPPER_BOUND:Q",
    )
)
actual = (
    alt.Chart(agg)
    .mark_line(size=2)
    .encode(x="ANOMALY_DATE:T", y="ACTUAL_VALUE:Q",
            tooltip=[alt.Tooltip("ANOMALY_DATE:T", title="Date"),
                     alt.Tooltip("ACTUAL_VALUE:Q", title="Actual", format=",.2f"),
                     alt.Tooltip("FORECASTED_VALUE:Q", title="Forecast", format=",.2f")])
)
forecast = (
    alt.Chart(agg)
    .mark_line(strokeDash=[5, 3], opacity=0.7)
    .encode(x="ANOMALY_DATE:T", y="FORECASTED_VALUE:Q")
)
marks = (
    alt.Chart(agg[agg["IS_ANOMALY"]])
    .mark_point(size=140, color="#c00", filled=True, shape="triangle-up")
    .encode(x="ANOMALY_DATE:T", y="ACTUAL_VALUE:Q",
            tooltip=[alt.Tooltip("ANOMALY_DATE:T", title="Anomaly on")])
)
st.altair_chart((band + forecast + actual + marks).properties(height=320),
                use_container_width=True)
st.caption(
    "Shaded band is Snowflake's confidence interval with the lower bound floored at zero. "
    "Red triangles are days Snowflake flagged as anomalous."
)

# ---------------------------------------------------------------------------
# Anomaly register with native drill-down
# ---------------------------------------------------------------------------
section("Detected anomalies")

flagged = series[series["IS_ANOMALY"]].sort_values("ANOMALY_DATE", ascending=False)
if flagged.empty:
    # An all-clear is only meaningful if something was actually evaluated.
    if DAYS_EVALUATED:
        st.success(
            f"No anomalies detected across {DAYS_EVALUATED} evaluated days.",
            icon=mi("check_circle"),
        )
    else:
        st.info(
            "No anomaly history for this scope yet, so there is nothing to report "
            "either way. This is not an all-clear.",
            icon=mi("info"),
        )
else:
    show = flagged.copy()
    show["Date"] = show["ANOMALY_DATE"].apply(d)
    show["Actual"] = show["ACTUAL_VALUE"].apply(lambda v: num(v, 2))
    show["Forecast"] = show["FORECASTED_VALUE"].apply(lambda v: num(v, 2))
    show["Over forecast by"] = show["VARIANCE_VS_FORECAST"].apply(lambda v: num(v, 2))
    table_with_download(
    show[["Date", "ACCOUNT_NAME", "Actual", "Forecast", "Over forecast by", "ANOMALY_ID"]]
        .rename(columns={"ACCOUNT_NAME": "Account", "ANOMALY_ID": "Anomaly ID"}),
    "snowflake360_anomalies_1", "anomalies_1",
)
    st.caption(
        "ANOMALY_ID is stable across refreshes, so an acknowledged anomaly stays identifiable."
    )

    section("Investigate an anomaly")
    pick = st.selectbox(
        "Anomaly date",
        flagged["ANOMALY_DATE"].apply(d).unique().tolist(),
        help="Drill-down uses SNOWFLAKE.LOCAL.ANOMALY_INSIGHTS procedures for the installed account.",
    )

    if pick:
        st.info(
            "Deeper native drill-down is available via `SNOWFLAKE.LOCAL.ANOMALY_INSIGHTS!"
            "GET_HOURLY_CONSUMPTION_BY_SERVICE_TYPE`, `GET_TOP_WAREHOUSES_ON_DATE` and "
            "`GET_TOP_QUERIES_FROM_WAREHOUSE`. These are stored procedures rather than views, "
            "so they are materialized by the daily task rather than called live from the app.",
            icon=mi("info"),
        )

        wh = q(
            f"""SELECT WAREHOUSE_NAME, SUM(CREDITS_USED) AS CREDITS
                FROM {DB}.LANDING.LND_WAREHOUSE_METERING
                WHERE USAGE_DATE_UTC = '{pick}'
                GROUP BY 1 ORDER BY CREDITS DESC LIMIT 10""",
            PAGE,
            "anomaly_top_warehouses",
        )
        if not wh.empty:
            st.write("**Top warehouses that day (installed account)**")
            st.altair_chart(
                alt.Chart(wh).mark_bar().encode(
                    x=alt.X("CREDITS:Q", title="Credits"),
                    y=alt.Y("WAREHOUSE_NAME:N", sort="-x", title=None),
                    tooltip=["WAREHOUSE_NAME", alt.Tooltip("CREDITS:Q", format=",.4f")],
                ).properties(height=220),
                use_container_width=True,
            )
        else:
            st.info(
                "No warehouse-level anomalies in this scope.",
                icon=mi("info"),
            )

        svc = q(
            f"""SELECT DISPLAY_GROUP, SUM(NET_IN_CURRENCY) AS USD
                FROM {DB}.CURATED.FCT_DAILY_CURRENCY
                WHERE USAGE_DATE_UTC = '{pick}'
                GROUP BY 1 HAVING SUM(NET_IN_CURRENCY) > 0
                ORDER BY USD DESC LIMIT 12""",
            PAGE,
            "anomaly_service_breakdown",
        )
        if not svc.empty:
            st.write("**Spend by service that day (all in-scope accounts)**")
            table_with_download(
    svc.assign(Dollars=svc["USD"].apply(lambda v: usd(v, 2)))[
                    ["DISPLAY_GROUP", "Dollars"]
                ].rename(columns={"DISPLAY_GROUP": "Service"}),
    "snowflake360_anomalies_2", "anomalies_2",
)
        else:
            st.info(
                "No service-level anomaly breakdown in this scope.",
                icon=mi("info"),
            )

# ---------------------------------------------------------------------------
# Native email alerting
# ---------------------------------------------------------------------------
st.divider()
with st.expander("Native anomaly email alerts", expanded=False):
    st.markdown(
        "Snowflake sends cost anomaly notifications directly, with no alert objects or "
        "custom logic to maintain. Configure recipients once:"
    )
    st.code(
        "CALL SNOWFLAKE.LOCAL.ANOMALY_INSIGHTS!SET_ACCOUNT_NOTIFICATION_EMAILS(\n"
        "  ['you@example.com', 'finops@example.com']\n"
        ");",
        language="sql",
    )
    st.caption(
        "Requires ACCOUNTADMIN or the SNOWFLAKE.APP_USAGE_ADMIN application role. "
        "Set once at deployment; nothing to maintain afterwards."
    )
