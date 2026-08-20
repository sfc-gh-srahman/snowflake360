"""Product & AI Usage - per-feature, per-model, per-user AI spend."""

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

st.set_page_config(page_title="Product & AI Usage | Snowflake360", layout="wide")
PAGE = "Product & AI Usage"
init_page("Product & AI Usage")
render_account_scope_banner()
render_freshness_caption()

_range_label, days = range_selector("product", "1Y")

# ---------------------------------------------------------------------------
# Authoritative totals come from metering, never from the feature views
# ---------------------------------------------------------------------------
totals = q(
    f"""SELECT
          SUM(CASE WHEN CREDIT_CLASS='AI'       THEN CREDITS_BILLED END)    AS AI_CREDITS,
          SUM(CASE WHEN CREDIT_CLASS='AI'       THEN DOLLARS_FROM_RATE END) AS AI_DOLLARS,
          SUM(CASE WHEN CREDIT_CLASS='PLATFORM' THEN CREDITS_BILLED END)    AS PLAT_CREDITS,
          SUM(DOLLARS_FROM_RATE)                                            AS TOTAL_DOLLARS
        FROM {DB}.CURATED.FCT_DAILY_CREDITS
        WHERE ACCOUNT_NAME = CURRENT_ACCOUNT_NAME()
          AND USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())""",
    PAGE,
    "ai_totals_metering",
)

t = totals.iloc[0]
# pd.notna AND > 0, not a bare truthiness test. SUM() over no rows returns NULL,
# which pandas may hand back as float NaN -- and NaN is truthy, so the old guard
# passed and the metric rendered the string "nan%". When it arrived as None
# instead, the guard failed and it rendered a fabricated "0.0%". Both stated a
# share that had not been measured.
_ai_total = t["TOTAL_DOLLARS"]
_ai_ok = pd.notna(_ai_total) and float(_ai_total) > 0
ai_share = 100.0 * t["AI_DOLLARS"] / _ai_total if _ai_ok else None

k = st.columns(4)
k[0].metric("AI credits", num(t["AI_CREDITS"], 2))
k[1].metric("AI spend", usd(t["AI_DOLLARS"], 2))
k[2].metric("AI share of spend",
            f"{ai_share:,.1f}%" if ai_share is not None else "n/a")
k[3].metric("Platform credits", num(t["PLAT_CREDITS"], 2))

st.caption(
    "Totals come from metering, which is the billed source of truth. The feature breakdown "
    "below comes from the Cortex usage views and differs by roughly half a percent, so it is "
    "used for proportional detail only, never as the authoritative total."
)

# ---------------------------------------------------------------------------
# Feature breakdown from the deduplicated AI views
# ---------------------------------------------------------------------------
section("By feature")

feat = q(
    f"""SELECT FEATURE, SUM(CREDITS) AS CREDITS, SUM(TOKENS) AS TOKENS,
               SUM(EVENT_COUNT) AS EVENTS
        FROM {DB}.LANDING.LND_AI_USAGE_DAILY
        WHERE USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
        GROUP BY 1 HAVING SUM(CREDITS) > 0 OR SUM(EVENT_COUNT) > 0
        ORDER BY CREDITS DESC""",
    PAGE,
    "ai_by_feature",
)

if feat.empty:
    st.info("No AI feature usage in the selected window.", icon=mi("info"))
else:
    f1, f2 = st.columns([2, 1])
    with f1:
        st.altair_chart(
            alt.Chart(feat)
            .mark_bar()
            .encode(
                x=alt.X("CREDITS:Q", title="Credits"),
                y=alt.Y("FEATURE:N", sort="-x", title=None),
                tooltip=["FEATURE", alt.Tooltip("CREDITS:Q", format=",.4f"),
                         alt.Tooltip("TOKENS:Q", format=",.0f"), "EVENTS"],
            )
            .properties(height=300),
            use_container_width=True,
        )
    with f2:
        show = feat.copy()
        show["Credits"] = show["CREDITS"].apply(lambda v: num(v, 4))
        show["Events"] = show["EVENTS"].apply(lambda v: num(v, 0))
        table_with_download(
    show[["FEATURE", "Credits", "Events"]],
    "snowflake360_product_ai_1", "product_ai_1",
)

    # Trend
    trend = q(
        f"""SELECT USAGE_DATE_UTC, FEATURE, SUM(CREDITS) AS CREDITS
            FROM {DB}.LANDING.LND_AI_USAGE_DAILY
            WHERE USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
            GROUP BY 1,2 HAVING SUM(CREDITS) > 0 ORDER BY 1""",
        PAGE,
        "ai_trend",
    )
    if not trend.empty:
        st.altair_chart(
            alt.Chart(trend)
            .mark_area()
            .encode(
                x=alt.X("USAGE_DATE_UTC:T", title="Date (UTC)"),
                y=alt.Y("CREDITS:Q", title="Credits", stack=True),
                color=alt.Color("FEATURE:N", title="Feature",
                                scale=alt.Scale(range=CATEGORY_SCALE)),
                tooltip=["USAGE_DATE_UTC:T", "FEATURE", alt.Tooltip("CREDITS:Q", format=",.4f")],
            )
            .properties(height=260),
            use_container_width=True,
        )
    else:
        st.info(
            "No AI usage trend in this window.",
            icon=mi("info"),
        )

# ---------------------------------------------------------------------------
# Model, surface, and user detail
# ---------------------------------------------------------------------------
section("Detail")
t1, t2, t3 = st.tabs(["By model", "By surface", "By user"])

with t1:
    models = q(
        f"""SELECT MODEL_NAME, FEATURE, SUM(CREDITS) AS CREDITS, SUM(TOKENS) AS TOKENS
            FROM {DB}.LANDING.LND_AI_USAGE_DAILY
            WHERE USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
              AND MODEL_NAME <> 'n/a'
            GROUP BY 1,2 ORDER BY CREDITS DESC LIMIT 40""",
        PAGE,
        "ai_by_model",
    )
    if models.empty:
        st.info("No model-attributed usage. Some features report credits without a model name.")
    else:
        show = models.copy()
        show["Credits"] = show["CREDITS"].apply(lambda v: num(v, 4))
        show["Tokens"] = show["TOKENS"].apply(lambda v: num(v, 0))
        table_with_download(
    show[["MODEL_NAME", "FEATURE", "Credits", "Tokens"]],
    "snowflake360_product_ai_2", "product_ai_2",
)

with t2:
    surface = q(
        f"""SELECT FEATURE, SUB_FEATURE, SUM(CREDITS) AS CREDITS, SUM(EVENT_COUNT) AS EVENTS
            FROM {DB}.LANDING.LND_AI_USAGE_DAILY
            WHERE USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
              AND SUB_FEATURE <> 'n/a'
            GROUP BY 1,2 ORDER BY CREDITS DESC LIMIT 40""",
        PAGE,
        "ai_by_surface",
    )
    if surface.empty:
        st.info("No sub-feature detail available.")
    else:
        show = surface.copy()
        show["Credits"] = show["CREDITS"].apply(lambda v: num(v, 4))
        table_with_download(
    show[["FEATURE", "SUB_FEATURE", "Credits", "EVENTS"]]
                     .rename(columns={"SUB_FEATURE": "Surface", "EVENTS": "Events"}),
    "snowflake360_product_ai_3", "product_ai_3",
)
        st.caption(
            "Cortex Code surfaces (Desktop, CLI, Snowsight) come from the INTERFACE column of "
            "SNOWFLAKE_COCO_USAGE_HISTORY, which spans the full history and supersedes the "
            "three legacy per-surface views."
        )

with t3:
    users = q(
        f"""SELECT USER_NAME, SUM(CREDITS) AS CREDITS, SUM(EVENT_COUNT) AS EVENTS,
                   COUNT(DISTINCT FEATURE) AS FEATURES
            FROM {DB}.LANDING.LND_AI_USAGE_DAILY
            WHERE USAGE_DATE_UTC >= DATEADD('day', -{days}, CURRENT_DATE())
              AND USER_NAME <> 'UNKNOWN'
            GROUP BY 1 ORDER BY CREDITS DESC LIMIT 40""",
        PAGE,
        "ai_by_user",
    )
    if users.empty:
        st.info(
            "No user-attributed AI usage. Several Cortex views expose only USER_ID, not "
            "USER_NAME, so those rows cannot be attributed to a person."
        )
    else:
        show = users.copy()
        show["Credits"] = show["CREDITS"].apply(lambda v: num(v, 4))
        table_with_download(
    show[["USER_NAME", "Credits", "EVENTS", "FEATURES"]]
                     .rename(columns={"EVENTS": "Events", "FEATURES": "Features"}),
    "snowflake360_product_ai_4", "product_ai_4",
)

# ---------------------------------------------------------------------------
# Deduplication disclosure: the traps that would double count
# ---------------------------------------------------------------------------
with st.expander("How AI usage is deduplicated", expanded=False):
    st.markdown(
        "Three overlapping-source traps were verified in this account and are handled:\n\n"
        "1. **`SNOWFLAKE_COCO_USAGE_HISTORY` spans the full history** and is a superset of the "
        "three legacy `CORTEX_CODE_*` views — identical row counts and 3,318 shared "
        "`REQUEST_ID`s. Only the COCO view is read.\n"
        "2. **`CORTEX_AISQL_USAGE_HISTORY` overlaps `CORTEX_AI_FUNCTIONS_USAGE_HISTORY`** on "
        "667 of 669 `QUERY_ID`s. AI functions is canonical; AISQL contributes only rows with "
        "no matching query.\n"
        "3. **`SNOWFLAKE_INTELLIGENCE_USAGE_HISTORY` and `SNOWFLAKE_COWORK_USAGE_HISTORY` are "
        "identical** — same 59 rows, same `REQUEST_ID`s. Intelligence was renamed CoWork and "
        "both views persist. Only CoWork is read.\n\n"
        "Unioning the obvious-looking views without these checks overstates AI spend by "
        "roughly 85%."
    )
