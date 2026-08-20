"""Usage - the two small-multiples grids: Platform credits and AI credits."""

from __future__ import annotations

import sys
from pathlib import Path

import altair as alt
import streamlit as st

sys.path.append(str(Path(__file__).parent.parent))
from lib.sf import (  # noqa: E402
    ACTUAL_BLUE,
    DB,
    init_page,
    mi,
    num,
    q,
    range_selector,
    render_freshness_caption,
    section,
    table_with_download,
    usd,
)

st.set_page_config(page_title="Usage | Snowflake360", layout="wide")
PAGE = "Usage"
init_page("Usage")
st.caption(
    "Credits by usage type, split into Platform and AI grids. Classification follows "
    "Snowflake's native RATING_TYPE, so it stays correct as Snowflake adds service types."
)
render_freshness_caption()

_range_label, days = range_selector("usage", "1Y")


def sparkline_grid(df, title: str, empty_note: str) -> None:
    """Render one small-multiples grid, matching A360's Usage page layout."""
    section(title)
    if df.empty:
        st.info(empty_note, icon=mi("info"))
        return

    groups = (
        df.groupby("DISPLAY_GROUP", as_index=False)
        .agg(CREDITS=("CREDITS", "sum"), DOLLARS=("DOLLARS", "sum"))
        .sort_values("CREDITS", ascending=False)
    )

    per_row = 4
    for start in range(0, len(groups), per_row):
        chunk = groups.iloc[start : start + per_row]
        cols = st.columns(per_row)
        for col, (_, g) in zip(cols, chunk.iterrows()):
            series = df[df["DISPLAY_GROUP"] == g["DISPLAY_GROUP"]]
            with col:
                st.caption(f"**{g['DISPLAY_GROUP']}**")
                st.write(f"{num(g['CREDITS'], 2)} cr · {usd(g['DOLLARS'], 2)}")
                if len(series) > 1:
                    st.altair_chart(
                        alt.Chart(series)
                        .mark_area(opacity=0.75, color=ACTUAL_BLUE)
                        .encode(
                            # A360 keeps a dated x axis on these small multiples.
                            # Without it a sparkline shows a shape with no
                            # indication of when the movement happened, which is
                            # the one thing a reader needs from it. Monthly ticks
                            # at a short label, since the panel is ~180px wide.
                            x=alt.X(
                                "USAGE_DATE_UTC:T",
                                title=None,
                                axis=alt.Axis(
                                    format="%b",
                                    tickCount={"interval": "month", "step": 1},
                                    labelFontSize=8,
                                    labelAngle=0,
                                    labelOverlap="greedy",
                                    ticks=True,
                                    domain=True,
                                    grid=False,
                                ),
                            ),
                            y=alt.Y("CREDITS:Q", axis=None),
                            tooltip=[
                                alt.Tooltip("USAGE_DATE_UTC:T", title="Date"),
                                alt.Tooltip("CREDITS:Q", title="Credits", format=",.4f"),
                            ],
                        )
                        # Taller than before to make room for the axis without
                        # squeezing the area itself flat.
                        .properties(height=72),
                        use_container_width=True,
                    )
                else:
                    st.caption("single day")


base = q(
    f"""SELECT USAGE_DATE_UTC, DISPLAY_GROUP, CREDIT_CLASS,
               SUM(CREDITS_BILLED)    AS CREDITS,
               SUM(DOLLARS_FROM_RATE) AS DOLLARS
        FROM {DB}.CURATED.FCT_DAILY_CREDITS
        WHERE USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
        GROUP BY 1,2,3 HAVING SUM(CREDITS_BILLED) > 0""",
    PAGE,
    "usage_grids",
)

sparkline_grid(
    base[base["CREDIT_CLASS"] == "PLATFORM"],
    "Credits by usage type — Platform",
    "No platform credits in the selected window.",
)

st.divider()

sparkline_grid(
    base[base["CREDIT_CLASS"] == "AI"],
    "AI credits by usage type",
    "No AI credits in the selected window.",
)

# ---------------------------------------------------------------------------
# Honest empty states: verified absent rather than assumed
# ---------------------------------------------------------------------------
st.divider()
section("Verified unavailable in this account")

gaps = q(
    f"""SELECT 'Reader accounts' AS TILE,
               (SELECT COUNT(*) FROM {DB}.CURATED.DIM_ACCOUNT WHERE IS_MANAGED AND IS_IN_SCOPE)::VARCHAR AS DETAIL,
               'No managed (reader) accounts in scope, so reader compute, storage, data transfer and serverless tiles have no source.' AS NOTE
        UNION ALL
        SELECT 'Query acceleration',
               (SELECT COUNT_IF(CREDITS_USED_QUERY_ACCELERATION IS NOT NULL)
                FROM {DB}.LANDING.LND_QUERY_ATTRIBUTION)::VARCHAR,
               'CREDITS_USED_QUERY_ACCELERATION is entirely NULL: QAS is not enabled.'
        UNION ALL
        SELECT 'Support adjustments and data sharing rebate', 'n/a',
               'No ACCOUNT_USAGE or ORGANIZATION_USAGE source. Customer-entered on the contract in Settings.'""",
    PAGE,
    "empty_state_notes",
)
table_with_download(
    gaps.rename(columns={"TILE": "Tile", "DETAIL": "Count", "NOTE": "Why empty"}),
    "snowflake360_usage_1", "usage_1",
)

# ---------------------------------------------------------------------------
# Rename-boundary continuity check, surfaced rather than hidden
# ---------------------------------------------------------------------------
with st.expander("Service type renames handled", expanded=False):
    aliases = q(
        f"""SELECT SERVICE_TYPE, RATE_SHEET_SERVICE_TYPE, RATING_TYPE, CREDIT_CLASS, DISPLAY_GROUP
            FROM {DB}.CURATED.DIM_SERVICE_TYPE
            WHERE IS_ALIASED ORDER BY DISPLAY_GROUP, SERVICE_TYPE""",
        PAGE,
        "aliased_service_types",
    )
    table_with_download(
    aliases,
    "snowflake360_usage_2", "usage_2",
)
    st.caption(
        "Snowflake renames metering service types over time and the names differ between "
        "METERING_DAILY_HISTORY and RATE_SHEET_DAILY. DISPLAY_GROUP keeps each series "
        "continuous so a rename does not look like a feature dying and a new one appearing. "
        "Cortex Code, for example, appears as CORTEX_CODE_* through 2026-06-30 and "
        "SNOWFLAKE_COCO_* from 2026-07-05."
    )

    unrated = q(
        f"""SELECT SERVICE_TYPE, RATING_TYPE, CREDIT_CLASS
            FROM {DB}.CURATED.DIM_SERVICE_TYPE WHERE IS_UNRATED""",
        PAGE,
        "unrated_service_types",
    )
    if not unrated.empty:
        st.warning(
            f"{len(unrated)} service type(s) have no rate sheet entry and therefore no price. "
            "This is expected only for types Snowflake no longer bills.",
            icon=mi("warning"),
        )
        table_with_download(
    unrated,
    "snowflake360_usage_3", "usage_3",
)
