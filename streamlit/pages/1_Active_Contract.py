"""Active Contract - the whole contract picture on one page.

Consolidates what were previously two pages. The contract terms and the
installment schedule they generate belong together: a customer asking "why did I
get invoiced again" and a customer asking "how much capacity is left" are asking
about the same document.

Structured around the three events the order form treats as distinct, because
conflating them is what makes quarterly invoices confusing:

  * period overspend    -> the next invoice may arrive early (cash flow)
  * capacity exhaustion -> usage converts to On Demand pricing (unit price)
  * overage             -> consumption beyond the whole capacity purchase

A period can overspend while the contract as a whole is perfectly healthy. That
is accelerated billing, not an overage charge.
"""

from __future__ import annotations

import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.append(str(Path(__file__).parent.parent))
from lib.sf import (  # noqa: E402
    active_contract,
    ACTUAL_BLUE,
    BREACH,
    capacity_formula_note,
    CATEGORY_SCALE,
    chart_hint,
    CUMULATIVE,
    d,
    DB,
    FORECAST,
    init_page,
    mi,
    NEUTRAL,
    num,
    pct,
    q,
    render_config_banner,
    render_freshness_caption,
    scope_accounts,
    section,
    table_with_download,
    total_capacity,
    usd,
)

st.set_page_config(page_title="Active Contract | Snowflake360", layout="wide")
PAGE = "Active Contract"
init_page("Active Contract")

render_config_banner()
render_freshness_caption()

c = active_contract()
if c.empty:
    st.stop()
ct = c.iloc[0]
CCY = str(ct["METERED_CURRENCY"] or "USD")

pos = q(
    f"""SELECT USAGE_DATE_UTC, DOLLARS_DAY, CAPACITY_USED, TOTAL_CAPACITY_USED,
               REMAINING_BALANCE, CAPACITY_PURCHASED, DAYS_REMAINING_IN_TERM,
               ACCOUNTS_WITH_SPEND, COMMITTED_DAY, OVERAGE_DAY
        FROM {DB}.CURATED.FCT_CONTRACT_POSITION ORDER BY USAGE_DATE_UTC""",
    PAGE,
    "contract_position_series",
)
if pos.empty:
    st.warning(
        "No contract position rows. The active contract term may not overlap available usage."
    )
    st.stop()

latest = pos.iloc[-1]

# Depth of measured history, used to decide whether a forecast or an all-clear is
# worth stating. The curated model calls anything under 60 days low confidence
# (curated.sql and 45_capacity_warnings.sql both use that threshold), so the page
# agrees with it rather than inventing a second rule. Measured here from the
# position series so it is available even when there are no warnings at all --
# which is precisely the fresh-account case where an unqualified "consumption is
# tracking within the contract" would be an all-clear nobody measured.
HISTORY_DAYS = len(pos)
LOW_CONFIDENCE = HISTORY_DAYS < 60

# Percentages divide by total capacity, not the purchase amount, so a contract
# carrying rollover is not reported as burning faster than it is.
CAPACITY = total_capacity(ct) or float(latest["CAPACITY_PURCHASED"] or 0)

warns = q(
    f"""SELECT WARNING_CODE, WARNING_TITLE, SEVERITY, DAYS_UNTIL, IMPACT_DATE,
               MESSAGE, RECOMMENDATION, RATE_BASIS, IS_LOW_CONFIDENCE
        FROM {DB}.CURATED.FCT_CAPACITY_WARNING ORDER BY SORT_ORDER""",
    PAGE, "capacity_warnings",
)

proj = q(
    f"""SELECT METHOD, METHOD_CLASS, PROJECTED_RATE_PER_DAY, PROJECTED_TOTAL_AT_TERM_END,
               PROJECTED_OVERAGE, DAYS_UNTIL_OVERAGE, PROJECTED_OVERAGE_DATE,
               OVERAGE_BEFORE_TERM_END
        FROM {DB}.CURATED.FCT_CONTRACT_PROJECTION ORDER BY PROJECTED_RATE_PER_DAY DESC""",
    PAGE, "projections",
)

# ---------------------------------------------------------------------------
# What to act on
#
# Above everything else. The failure this app exists to prevent is a customer
# learning about a problem from an invoice, so a warning that has to be scrolled
# to has already failed.
# ---------------------------------------------------------------------------
if not warns.empty:
    section("What to act on")
    for w in warns.itertuples():
        box = st.error if w.SEVERITY == "CRITICAL" else st.warning
        # NaN is truthy, so `not NaN` is False and `NaN == 0` is False --
        # both guards pass and int(NaN) raises. pd.isna catches it.
        when = (
            "now" if pd.isna(w.DAYS_UNTIL) or not w.DAYS_UNTIL or w.DAYS_UNTIL == 0
            else f"in {int(w.DAYS_UNTIL)} days ({d(w.IMPACT_DATE)})"
        )
        box(f"**{w.WARNING_TITLE} - {when}**\n\n{w.MESSAGE}\n\n{w.RECOMMENDATION}")
    if bool(warns.iloc[0].IS_LOW_CONFIDENCE):
        st.caption(
            "Projections are based on limited history and will tighten as more usage "
            "accumulates."
        )
    st.caption(f"Projections use the {warns.iloc[0].RATE_BASIS}.")
elif LOW_CONFIDENCE:
    st.info(
        f"No capacity warnings, but only {HISTORY_DAYS} day(s) of usage have been "
        "measured against this contract. That is not yet enough to project "
        "exhaustion or to say consumption is on track -- treat this as "
        "\"not enough history\" rather than as an all-clear.",
        icon=mi("info"),
    )
else:
    st.success(
        f"No capacity warnings. Consumption is tracking within the contract, "
        f"measured over {HISTORY_DAYS} days.",
        icon=mi("check_circle"),
    )

# ---------------------------------------------------------------------------
# Capacity KPI header
# ---------------------------------------------------------------------------
section("Capacity position")

consumed_pct = 100.0 * latest["TOTAL_CAPACITY_USED"] / CAPACITY if CAPACITY else 0

m = st.columns(5)
m[0].metric("Total capacity", usd(CAPACITY),
            f"Purchase {usd(latest['CAPACITY_PURCHASED'])}", delta_color="off")
m[1].metric("Total capacity used", usd(latest["TOTAL_CAPACITY_USED"]),
            f"Used {consumed_pct:,.1f}%", delta_color="off")
m[2].metric("Remaining balance", usd(latest["REMAINING_BALANCE"]),
            f"Term ends {d(ct['CONTRACT_END_DATE'])}", delta_color="off")
# int() on a NULL raises "cannot convert float NaN to integer", which on a
# fresh account is a blank page rather than a missing tile.
m[3].metric("Days left in term",
            int(latest["DAYS_REMAINING_IN_TERM"])
            if pd.notna(latest["DAYS_REMAINING_IN_TERM"]) else "n/a",
            f"Started {d(ct['CONTRACT_START_DATE'])}", delta_color="off")

_exh = warns[warns.WARNING_CODE == "CAPACITY_EXHAUSTION"] if not warns.empty else pd.DataFrame()
if not _exh.empty:
    _du = _exh.iloc[0].DAYS_UNTIL
    m[4].metric("Projected exhaustion", d(_exh.iloc[0].IMPACT_DATE),
                f"in {int(_du)} days" if pd.notna(_du) else "date projected",
                delta_color="off")
elif LOW_CONFIDENCE:
    # Distinguish "we looked and the pace holds" from "we do not have enough
    # history to say". Reporting the former when the latter is true reads as an
    # all-clear that was never actually measured.
    m[4].metric("Projected exhaustion", "not enough history",
                "Needs more usage days", delta_color="off")
else:
    m[4].metric("Projected exhaustion", "not within term",
                "Current pace holds", delta_color="off")

st.progress(min(consumed_pct / 100.0, 1.0))
capacity_formula_note(ct)

# ---------------------------------------------------------------------------
# Burn-down with forecast band and overage marker
#
# A360's treatment: actual daily spend as light blue bars on the left axis,
# cumulative as a dark line on the right, an amber band covering the projected
# remainder of the term, and a red rule at the projected overage date. The point
# of the band is that the region past today is a projection, not measurement, and
# it should not look like data.
# ---------------------------------------------------------------------------
section("Capacity burn-down and forecast")

# The run-rate basis is the user's choice, not the app's. A360 offers a period
# selector for exactly this reason: which window you trust is a judgement call,
# and short and long windows disagree precisely when it matters most.
#
# Windows are 30/60/90/180 to match A360's control, ordered shortest-first so the
# list reads like a duration scale. Snowflake's own forecast sits last as an
# alternative to a trailing average rather than another window length.
METHOD_LABELS = {
    "RUN_RATE_30D": "30 days",
    "RUN_RATE_60D": "60 days",
    "RUN_RATE_90D": "90 days",
    "RUN_RATE_180D": "180 days",
    "NATIVE_FORECAST_FLOORED": "Snowflake forecast",
}
_order = list(METHOD_LABELS)
methods = sorted(
    (m for m in proj["METHOD"]) if not proj.empty else [],
    key=lambda m: _order.index(m) if m in _order else len(_order),
)
sel_col, chart_col = st.columns([1, 5])
with sel_col:
    st.caption("Select a run rate period")
    default_i = methods.index("RUN_RATE_30D") if "RUN_RATE_30D" in methods else 0
    chosen = st.radio(
        "Run rate basis", methods, index=default_i if methods else 0,
        format_func=lambda m: METHOD_LABELS.get(m, m.replace("_", " ").title()),
        key="ac_rate_basis", label_visibility="collapsed",
    ) if methods else None
    if chosen:
        pr = proj[proj.METHOD == chosen].iloc[0]
        st.metric("Consumption run rate", usd(pr.PROJECTED_RATE_PER_DAY))
        st.caption("per day, times days remaining")

with chart_col:
    hist = pos[["USAGE_DATE_UTC", "DOLLARS_DAY", "TOTAL_CAPACITY_USED"]].copy()

    last_day = pd.Timestamp(hist["USAGE_DATE_UTC"].max())
    term_end = pd.Timestamp(ct["CONTRACT_END_DATE"])

    # Two y scales, not three. Daily spend gets the left axis; everything
    # cumulative -- measured, projected, and the capacity line -- shares the
    # right, via a nested layer that keeps one shared scale between them.
    #
    # Every cumulative mark must carry the *same* axis definition. Layering them
    # with independent scales gives three overlapping right axes whose labels
    # collide; suppressing the extras with axis=None instead removes the right
    # axis altogether, which silently leaves the cumulative and capacity marks
    # unlabelled and reads three orders of magnitude wrong against the left axis.
    # Identical axis objects on a shared scale merge into one.
    CUM_AXIS = alt.Axis(orient="right", title=f"Cumulative {CCY}")

    left = alt.Chart(hist).mark_bar(color=ACTUAL_BLUE, opacity=0.85).encode(
        x=alt.X("USAGE_DATE_UTC:T", title="Date (UTC)"),
        y=alt.Y("DOLLARS_DAY:Q", title=f"{CCY} per day"),
        tooltip=[alt.Tooltip("USAGE_DATE_UTC:T", title="Date"),
                 alt.Tooltip("DOLLARS_DAY:Q", title="Spend", format="$,.2f")],
    )

    right_layers = []

    # Amber band over the projected remainder of the term, drawn first so measured
    # data sits on top of it. x-only, so it does not participate in the y scale.
    if chosen is not None and term_end > last_day:
        right_layers.append(
            alt.Chart(pd.DataFrame({"START": [last_day], "END": [term_end]}))
            .mark_rect(color=FORECAST, opacity=0.13)
            .encode(x=alt.X("START:T"), x2="END:T")
        )

    right_layers.append(
        alt.Chart(hist).mark_line(color=CUMULATIVE, size=2).encode(
            x=alt.X("USAGE_DATE_UTC:T"),
            y=alt.Y("TOTAL_CAPACITY_USED:Q", axis=CUM_AXIS),
            tooltip=[alt.Tooltip("USAGE_DATE_UTC:T", title="Date"),
                     alt.Tooltip("TOTAL_CAPACITY_USED:Q", title="Cumulative",
                                 format="$,.2f")],
        )
    )

    # Projected cumulative path, so the forecast is a line a number can be read
    # off rather than only a shaded region.
    if chosen is not None and term_end > last_day:
        n_days = int((term_end - last_day).days)
        if n_days > 0:
            rate = float(pr.PROJECTED_RATE_PER_DAY or 0)
            base_cum = float(hist["TOTAL_CAPACITY_USED"].iloc[-1] or 0)
            fdf = pd.DataFrame(
                {"USAGE_DATE_UTC": pd.date_range(last_day, term_end, freq="D")}
            )
            # Day offset must be a numeric Series, not a range: a float times a
            # range is a TypeError.
            fdf["PROJECTED"] = base_cum + rate * pd.Series(
                range(len(fdf)), dtype="float64"
            )
            right_layers.append(
                alt.Chart(fdf)
                .mark_line(color=FORECAST, strokeDash=[4, 3], size=2)
                .encode(
                    x=alt.X("USAGE_DATE_UTC:T"),
                    y=alt.Y("PROJECTED:Q", axis=CUM_AXIS),
                    tooltip=[alt.Tooltip("USAGE_DATE_UTC:T", title="Date"),
                             alt.Tooltip("PROJECTED:Q", title="Projected",
                                         format="$,.2f")],
                )
            )

    right_layers.append(
        alt.Chart(pd.DataFrame({"Y": [CAPACITY]}))
        .mark_rule(color=BREACH, strokeDash=[6, 4], size=2)
        .encode(y=alt.Y("Y:Q", axis=CUM_AXIS))
    )

    # Overage marker. A projected date only exists while capacity is unspent; once
    # it is gone the crossing is a historical fact, so fall back to the first day
    # cumulative spend passed capacity. Reporting "no overage date" for an account
    # that is already in overage would be the wrong answer, not a missing one.
    overage_date = None
    overage_kind = ""
    if chosen is not None and pd.notna(pr.PROJECTED_OVERAGE_DATE):
        overage_date = pd.Timestamp(pr.PROJECTED_OVERAGE_DATE)
        overage_kind = "Projected overage"
    else:
        crossed = hist[hist["TOTAL_CAPACITY_USED"] >= CAPACITY]
        if not crossed.empty and CAPACITY > 0:
            overage_date = pd.Timestamp(crossed["USAGE_DATE_UTC"].iloc[0])
            overage_kind = "Capacity exhausted"

    if overage_date is not None:
        ov = pd.DataFrame({"X": [overage_date]})
        right_layers.append(
            alt.Chart(ov).mark_rule(color=BREACH, size=2).encode(x=alt.X("X:T"))
        )
        right_layers.append(
            alt.Chart(ov).mark_text(
                align="left", dx=5, dy=-6, color=BREACH,
                fontWeight="bold", fontSize=11,
            ).encode(
                x=alt.X("X:T"),
                text=alt.value(f"{overage_kind}: {d(overage_date)}"),
            )
        )

    st.altair_chart(
        alt.layer(left, alt.layer(*right_layers))
        .resolve_scale(y="independent")
        .properties(height=340),
        use_container_width=True,
    )

_cap_note = (
    f"The dashed red rule is total capacity of {usd(CAPACITY)}"
    + (f", crossed on {d(overage_date)}." if overage_date is not None else ".")
)
st.caption(
    "Light blue bars are measured daily spend (left axis). The dark line is cumulative "
    "spend against capacity (right axis). The amber region and dashed amber line are "
    "projection, not measurement. " + _cap_note
)

# ---------------------------------------------------------------------------
# Contract and subscription detail
# ---------------------------------------------------------------------------
section("Contract terms")

t1, t2, t3, t4, t5 = st.columns(5)
t1.metric("Contract number", str(ct["CONTRACT_NUMBER"]))
t1.caption(f"Source: {str(ct['CONTRACT_SOURCE'] or 'unknown').replace('_', ' ').lower()}")
t2.metric("Capacity billing", str(ct["BILLING_FREQUENCY"] or "not set"))
t2.caption(f"On Demand: {ct['ON_DEMAND_BILLING_FREQUENCY'] or 'not set'}")
t3.metric(
    "Term length",
    f"{int(ct['TERM_LENGTH_MONTHS'])} months"
    if pd.notna(ct["TERM_LENGTH_MONTHS"]) else "not set",
)
t3.caption(f"{d(ct['CONTRACT_START_DATE'])} to {d(ct['CONTRACT_END_DATE'])}")

_cp = ct["CAPACITY_CREDIT_PRICE"]
_op = ct["ON_DEMAND_CREDIT_PRICE"]
t4.metric("Contract credit price", usd(_cp, 2) if pd.notna(_cp) else "not set")
if pd.notna(_cp) and pd.notna(_op) and float(_cp) > 0 and float(_op) > float(_cp):
    _cliff = 100.0 * (float(_op) - float(_cp)) / float(_cp)
    t4.caption(
        f"Rises to {usd(_op, 2)} (+{_cliff:,.1f}%) once capacity is gone"
    )
t5.metric("Edition", str(ct["EDITION"] or "not set"))
t5.caption(f"{ct['CLOUD_PROVIDER'] or '?'} / {ct['REGION_NAME'] or '?'}")

subs = q(
    f"""SELECT ACCOUNT_NAME, ACCOUNT_LOCATOR, REGION, CLOUD, SERVICE_LEVEL,
               CONTRACT_CURRENCY, PRICE_PER_PLATFORM_CREDIT, OVERAGE_PRICE_PER_CREDIT,
               PRICE_PER_AI_CREDIT, STORAGE_PRICE_PER_TB_MONTH, AI_ROUTING_MODE, RATE_SOURCE
        FROM {DB}.CONFIG.SUBSCRIPTION ORDER BY ACCOUNT_NAME""",
    PAGE, "subscriptions",
)
table_with_download(subs, "snowflake360_subscriptions", "ac_subs")
st.caption(
    "Account locator, region, cloud, and service level are auto-detected. Rates are "
    "prefilled from ORGANIZATION_USAGE.RATE_SHEET_DAILY; RATE_SOURCE records provenance "
    "per row. Editing a rate in Setup & Settings overrides the prefill without "
    "repricing history."
)

with st.expander("How Total Capacity Used is calculated", expanded=False):
    st.code(
        "Total Capacity Used = capacity_used\n"
        "                    + free_usage + rollover + adjustment + balance_transfer\n"
        "                    + currency_conversion_adjustment + data_sharing_rebate\n"
        "                    + balance_migration\n\n"
        "Remaining Balance   = total_capacity - total_capacity_used",
        language="text",
    )
    for label, value in {
        "capacity_used (computed from usage)": latest["CAPACITY_USED"],
        "total_free_usage": ct["TOTAL_FREE_USAGE"],
        "rollover": ct["ROLLOVER"],
        "adjustment": ct["ADJUSTMENT"],
        "balance_transfer": ct["BALANCE_TRANSFER"],
        "currency_conversion_adjustment": ct["CURRENCY_CONVERSION_ADJUSTMENT"],
        "data_sharing_rebate": ct["DATA_SHARING_REBATE"],
        "balance_migration": ct["BALANCE_MIGRATION"],
    }.items():
        st.write(f"- `{label}` = **{usd(value, 2)}**")
    st.caption(
        "Only capacity_used is computed. Every other addend is customer-entered and has "
        "no single-account source, so a wrong value here silently moves the whole position."
    )

# ---------------------------------------------------------------------------
# Billing cycles
# ---------------------------------------------------------------------------
bp = q(
    f"""SELECT PERIOD_SEQ, PERIOD_LABEL, PERIOD_START, PERIOD_END, DATA_COVERAGE,
               IS_CURRENT_PERIOD, ALLOCATION, CONSUMPTION, CUM_ALLOCATION,
               CUM_CONSUMPTION, TOTAL_CAPACITY, POOLED_REMAINING, TERM_BURN_PCT,
               PERIOD_BURN_PCT, PROJECTED_PERIOD_BURN_PCT, PULL_FORWARD_TRIGGERED,
               PERIOD_OVERSPEND, OVERAGE_AMOUNT, IS_IN_OVERAGE, CURRENCY,
               BILLING_FREQUENCY, PERIOD_COUNT, TERM_START, TERM_END,
               CAPACITY_CREDIT_PRICE, ON_DEMAND_CREDIT_PRICE, PRICE_CLIFF_PCT,
               OPENING_BALANCE, ELAPSED_DAYS, PERIOD_DAYS
        FROM {DB}.CURATED.FCT_BILLING_PERIOD_POSITION ORDER BY PERIOD_SEQ""",
    PAGE, "billing_position",
)

section("Billing cycles")

if bp.empty:
    st.info(
        "No installment schedule yet. Set a term length and a capacity billing "
        "frequency on the Setup & Settings page - either by uploading your order form "
        "or on the Contract tab - and the schedule generates itself."
    )
else:
    row0 = bp.iloc[0]
    curp = bp[bp.IS_CURRENT_PERIOD]
    curp = curp.iloc[0] if not curp.empty else None

    st.caption(
        f"{usd(row0.TOTAL_CAPACITY, currency=CCY)} across "
        f"{int(row0.PERIOD_COUNT) if pd.notna(row0.PERIOD_COUNT) else '?'} "
        f"{str(row0.BILLING_FREQUENCY).lower()} installments, anniversary-aligned to "
        f"{d(row0.TERM_START)}. Installments are not calendar quarters."
    )

    if curp is not None:
        b1, b2, b3 = st.columns(3)
        b1.metric(
            f"Current period ({curp.PERIOD_LABEL})",
            usd(curp.CONSUMPTION, currency=CCY) if pd.notna(curp.CONSUMPTION) else "no data",
            f"{curp.PERIOD_BURN_PCT:,.0f}% of allocation"
            if pd.notna(curp.PERIOD_BURN_PCT) else None,
            delta_color="off",
        )
        b1.caption(
            f"Allocation {usd(curp.ALLOCATION, currency=CCY)} - "
            f"day {int(curp.ELAPSED_DAYS) if pd.notna(curp.ELAPSED_DAYS) else '?'}"
            f" of {int(curp.PERIOD_DAYS) if pd.notna(curp.PERIOD_DAYS) else '?'}"
        )
        b2.metric("Capacity remaining", usd(curp.POOLED_REMAINING, currency=CCY),
                  f"{curp.TERM_BURN_PCT:,.1f}% of contract consumed", delta_color="off")
        b3.metric(
            "Projected period burn",
            f"{curp.PROJECTED_PERIOD_BURN_PCT:,.0f}%"
            if pd.notna(curp.PROJECTED_PERIOD_BURN_PCT) else "n/a",
            "at current pace", delta_color="off",
        )

    n_nodata = int((bp.DATA_COVERAGE == "NO_DATA").sum())
    if n_nodata:
        st.info(
            f"{n_nodata} early period(s) fall outside Snowflake's usage retention (about "
            "365 days) and cannot be measured. They are shown without consumption rather "
            "than as zero. Enter the consumption already drawn against this contract on "
            "the Setup & Settings page to make the remaining-capacity figure accurate."
        )

    chart_df = bp.melt(
        id_vars=["PERIOD_LABEL", "PERIOD_SEQ", "DATA_COVERAGE"],
        value_vars=["ALLOCATION", "CONSUMPTION"],
        var_name="MEASURE", value_name="AMOUNT",
    ).dropna(subset=["AMOUNT"])
    chart_df["MEASURE"] = chart_df.MEASURE.map(
        {"ALLOCATION": "Invoiced allocation", "CONSUMPTION": "Consumption"}
    )

    st.altair_chart(
        alt.Chart(chart_df)
        .mark_bar()
        .encode(
            x=alt.X("PERIOD_LABEL:N", sort=None, title=None),
            y=alt.Y("AMOUNT:Q", title=f"{CCY} per period"),
            xOffset="MEASURE:N",
            color=alt.Color("MEASURE:N", title=None,
                            scale=alt.Scale(
                                domain=["Invoiced allocation", "Consumption"],
                                range=[NEUTRAL, ACTUAL_BLUE])),
            tooltip=["PERIOD_LABEL", "MEASURE", alt.Tooltip("AMOUNT:Q", format="$,.2f")],
        )
        .properties(height=280),
        use_container_width=True,
    )
    st.caption(
        "A consumption bar taller than its allocation bar is what permits Snowflake to "
        "invoice the next installment early. That is accelerated billing, not an "
        "overage charge."
    )
    chart_hint()

    cum = bp[["PERIOD_LABEL", "PERIOD_SEQ", "CUM_ALLOCATION", "CUM_CONSUMPTION",
              "TOTAL_CAPACITY", "DATA_COVERAGE"]].copy()
    # Cumulative consumption is only meaningful once there is data to accumulate.
    cum.loc[cum.DATA_COVERAGE == "NO_DATA", "CUM_CONSUMPTION"] = None
    cum_long = cum.melt(
        id_vars=["PERIOD_LABEL", "PERIOD_SEQ"],
        value_vars=["CUM_ALLOCATION", "CUM_CONSUMPTION", "TOTAL_CAPACITY"],
        var_name="MEASURE", value_name="AMOUNT",
    ).dropna(subset=["AMOUNT"])
    cum_long["MEASURE"] = cum_long.MEASURE.map({
        "CUM_ALLOCATION": "Invoiced to date (schedule)",
        "CUM_CONSUMPTION": "Consumed to date",
        "TOTAL_CAPACITY": "Total capacity",
    })

    st.altair_chart(
        alt.Chart(cum_long)
        .mark_line(point=True)
        .encode(
            x=alt.X("PERIOD_LABEL:N", sort=None, title=None),
            y=alt.Y("AMOUNT:Q", title=CCY),
            color=alt.Color("MEASURE:N", title=None,
                            scale=alt.Scale(
                                domain=["Invoiced to date (schedule)",
                                        "Consumed to date", "Total capacity"],
                                range=[NEUTRAL, CUMULATIVE, BREACH])),
            strokeDash=alt.condition(
                alt.datum.MEASURE == "Total capacity",
                alt.value([6, 4]), alt.value([0]),
            ),
            tooltip=["PERIOD_LABEL", "MEASURE", alt.Tooltip("AMOUNT:Q", format="$,.2f")],
        )
        .properties(height=300),
        use_container_width=True,
    )
    st.caption(
        "Where consumed crosses total capacity, usage converts to On Demand and the "
        "negotiated credit discount stops applying. Where consumed sits above "
        "invoiced-to-date, invoices are running ahead of the calendar."
    )

    with st.expander("Period detail"):
        table_with_download(
            pd.DataFrame({
                "Period": bp.PERIOD_LABEL,
                "Start": bp.PERIOD_START.map(d),
                "End": bp.PERIOD_END.map(d),
                "Coverage": bp.DATA_COVERAGE.map({
                    "COMPLETE": "measured", "PARTIAL": "partly measured",
                    "NO_DATA": "before retention", "FUTURE": "not started",
                }),
                "Allocation": bp.ALLOCATION.map(lambda v: f"{v:,.2f}"),
                "Consumption": bp.CONSUMPTION.map(
                    lambda v: f"{v:,.2f}" if pd.notna(v) else "-"),
                "Burn %": bp.PERIOD_BURN_PCT.map(
                    lambda v: f"{v:,.0f}%" if pd.notna(v) else "-"),
                "Pull-forward": bp.PULL_FORWARD_TRIGGERED.map(
                    lambda v: "yes" if v is True else ("-" if pd.isna(v) else "no")),
                "Cumulative": bp.CUM_CONSUMPTION.map(lambda v: f"{v:,.2f}"),
                "Remaining": bp.POOLED_REMAINING.map(lambda v: f"{v:,.2f}"),
            }),
            "snowflake360_billing_periods", "ac_periods",
        )

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
section("Your thresholds")

th = q(
    f"""SELECT SCOPE, LABEL, THRESHOLD_PCT, THRESHOLD_AMOUNT, CURRENT_PCT,
               PROJECTED_PCT, STATUS, DAYS_UNTIL_BREACH, NOTIFY_EMAIL, CURRENCY
        FROM {DB}.CURATED.FCT_THRESHOLD_STATUS ORDER BY SCOPE DESC, THRESHOLD_PCT""",
    PAGE, "threshold_status",
)

if th.empty:
    st.caption("No thresholds configured. Add them on the Setup & Settings page.")
else:
    table_with_download(
        pd.DataFrame({
            "Scope": th.SCOPE.str.title(),
            "Threshold": th.LABEL,
            "Trigger at": th.apply(
                lambda r: f"{r.CURRENCY} {r.THRESHOLD_AMOUNT:,.2f} ({r.THRESHOLD_PCT:,.0f}%)",
                axis=1),
            "Now": th.CURRENT_PCT.map(lambda v: f"{v:,.1f}%" if pd.notna(v) else "n/a"),
            "Projected": th.PROJECTED_PCT.map(
                lambda v: f"{v:,.1f}%" if pd.notna(v) else "n/a"),
            "Status": th.STATUS.map({
                "BREACHED": "Breached",
                "FORECAST_BREACH": "Forecast to breach",
                "OK": "OK",
            }),
            "Lead time": th.DAYS_UNTIL_BREACH.map(
                lambda v: "now" if pd.notna(v) and v <= 0
                else (f"{int(v)} days" if pd.notna(v) else "not projected")),
            "Email": th.NOTIFY_EMAIL.map({True: "on", False: "off"}),
        }),
        "snowflake360_thresholds", "ac_thresholds",
    )
    st.caption("Thresholds are yours to set. Edit them on the Setup & Settings page.")

# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------
section("Projections")

if not proj.empty:
    over = proj[proj["OVERAGE_BEFORE_TERM_END"]]
    safe = proj[~proj["OVERAGE_BEFORE_TERM_END"]]
    if len(over) and len(safe):
        st.warning(
            f"**Methods disagree.** {len(over)} of {len(proj)} project an overage before "
            "term end. Compare the short and long trailing windows below: a large gap "
            "between them means the spend rate changed recently, which is the signal "
            "worth acting on.",
            icon=mi("warning"),
        )

    disp = proj.copy()
    disp["Rate / day"] = disp["PROJECTED_RATE_PER_DAY"].apply(usd)
    disp["Projected total"] = disp["PROJECTED_TOTAL_AT_TERM_END"].apply(usd)
    disp["Projected overage"] = disp["PROJECTED_OVERAGE"].apply(usd)
    disp["Crosses capacity"] = disp["PROJECTED_OVERAGE_DATE"].apply(d)
    disp["Days to crossover"] = disp["DAYS_UNTIL_OVERAGE"]
    disp["Overage in term"] = disp["OVERAGE_BEFORE_TERM_END"].map({True: "Yes", False: "No"})
    table_with_download(
        disp[[
            "METHOD", "METHOD_CLASS", "Rate / day", "Projected total",
            "Projected overage", "Days to crossover", "Crosses capacity",
            "Overage in term",
        ]].rename(columns={"METHOD": "Method", "METHOD_CLASS": "Class"}),
        "snowflake360_projections", "ac_proj",
    )
    st.caption(
        "Trailing run rates cannot go negative. The native method is Snowflake's own "
        "FORECASTED_VALUE, floored at zero because it can return negatives that are "
        "meaningless for spend. No custom forecasting model is trained or maintained."
    )
else:
    st.info(
        "No projections yet. A run rate needs several days of measured consumption against this contract term.",
        icon=mi("info"),
    )

# ---------------------------------------------------------------------------
# Contract breakout
# ---------------------------------------------------------------------------
section("Contract breakout")

tab1, tab2 = st.tabs(["By account", "By service type"])

with tab1:
    by_acct = q(
        f"""SELECT f.ACCOUNT_NAME, a.SERVICE_LEVEL,
                   SUM(f.NET_IN_CURRENCY) AS DOLLARS
            FROM {DB}.CURATED.FCT_DAILY_CURRENCY f
            JOIN {DB}.CURATED.DIM_ACCOUNT a ON a.ACCOUNT_KEY = f.ACCOUNT_KEY
            WHERE f.USAGE_DATE_UTC BETWEEN '{d(ct['CONTRACT_START_DATE'])}'
                                       AND '{d(ct['CONTRACT_END_DATE'])}'
            GROUP BY 1,2 ORDER BY DOLLARS DESC""",
        PAGE, "breakout_by_account",
    )
    if not by_acct.empty:
        st.altair_chart(
            alt.Chart(by_acct)
            .mark_bar()
            .encode(
                x=alt.X("DOLLARS:Q", title="Dollars drawn against contract"),
                y=alt.Y("ACCOUNT_NAME:N", sort="-x", title=None),
                color=alt.Color("SERVICE_LEVEL:N", title="Service level",
                                scale=alt.Scale(range=CATEGORY_SCALE)),
                tooltip=["ACCOUNT_NAME", "SERVICE_LEVEL",
                         alt.Tooltip("DOLLARS:Q", format="$,.2f")],
            )
            .properties(height=240),
            use_container_width=True,
        )
        chart_hint()
        total = by_acct["DOLLARS"].sum()
        by_acct["Share"] = (100.0 * by_acct["DOLLARS"] / total).round(1)
        by_acct["Dollars"] = by_acct["DOLLARS"].apply(usd)
        table_with_download(
            by_acct[["ACCOUNT_NAME", "SERVICE_LEVEL", "Dollars", "Share"]],
            "snowflake360_contract_by_account", "ac_by_acct",
        )
    else:
        st.info(
            "No spend recorded against this contract term yet.",
            icon=mi("info"),
        )

with tab2:
    by_svc = q(
        f"""SELECT DISPLAY_GROUP, CREDIT_CLASS, SUM(NET_IN_CURRENCY) AS DOLLARS
            FROM {DB}.CURATED.FCT_DAILY_CURRENCY
            WHERE USAGE_DATE_UTC BETWEEN '{d(ct['CONTRACT_START_DATE'])}'
                                     AND '{d(ct['CONTRACT_END_DATE'])}'
            GROUP BY 1,2 HAVING SUM(NET_IN_CURRENCY) > 0
            ORDER BY DOLLARS DESC LIMIT 25""",
        PAGE, "breakout_by_service",
    )
    if not by_svc.empty:
        st.altair_chart(
            alt.Chart(by_svc)
            .mark_bar()
            .encode(
                x=alt.X("DOLLARS:Q", title="Dollars"),
                y=alt.Y("DISPLAY_GROUP:N", sort="-x", title=None),
                color=alt.Color("CREDIT_CLASS:N", title="Credit class",
                                scale=alt.Scale(range=CATEGORY_SCALE)),
                tooltip=["DISPLAY_GROUP", "CREDIT_CLASS",
                         alt.Tooltip("DOLLARS:Q", format="$,.2f")],
            )
            .properties(height=500),
            use_container_width=True,
        )
        chart_hint()
        table_with_download(by_svc, "snowflake360_contract_by_service", "ac_by_svc")
    else:
        st.info(
            "No service-level spend recorded against this contract term yet.",
            icon=mi("info"),
        )

st.divider()
st.caption(
    f"Contract governs {len(scope_accounts())} in-scope accounts. Omitted versus A360: "
    "per-subscription LTM revenue, which is seller-side data with no customer equivalent."
)

# ---------------------------------------------------------------------------
# Account health signals
#
# Carried over from the former Overview page, which duplicated this page's
# warnings, capacity KPIs and projections. These are the parts that were not
# duplicated: pointers into the detail pages, and efficiency against Snowflake's
# published medians. They sit last because they are account-wide operational
# signals, not contract terms.
#
# The old Platform-vs-AI credit chart was dropped rather than moved. It showed a
# 90-day area split that answered no question the other pages do not answer
# better, and the only figures on it that mattered -- the credit prices -- are
# authoritative on the Rates tab.
# ---------------------------------------------------------------------------
section("Account health signals")

sig1, sig2, sig3 = st.columns(3)

anom = q(
    f"""SELECT COUNT_IF(IS_ANOMALY) AS ANOMALIES,
               COUNT_IF(IS_ANOMALY AND ANOMALY_DATE >= DATEADD('day',-30,CURRENT_DATE())) AS ANOM_30D
        FROM {DB}.CURATED.FCT_ANOMALY_DAILY""",
    PAGE, "anomaly_counts",
)
sig1.metric(
    "Cost anomalies (all time)",
    int(anom.iloc[0]["ANOMALIES"]) if not anom.empty else 0,
    f"{int(anom.iloc[0]['ANOM_30D']) if not anom.empty else 0} in last 30d",
    delta_color="off",
)
sig1.caption("Detail on Cost Anomalies.")

opp = q(
    f"""SELECT COUNT_IF(IS_OPPORTUNITY) AS OPPS, COUNT(DISTINCT INSIGHT_TOPIC) AS TOPICS
        FROM {DB}.CURATED.FCT_OPTIMIZATION_OPPORTUNITY""",
    PAGE, "optimization_counts",
)
sig2.metric(
    "Query optimization opportunities",
    int(opp.iloc[0]["OPPS"]) if not opp.empty else 0,
    f"{int(opp.iloc[0]['TOPICS']) if not opp.empty else 0} topics",
    delta_color="off",
)
sig2.caption("Detail on Warehouse and Optimization.")

attr = q(
    f"""SELECT BUCKET, SUM(CREDITS) AS CR FROM {DB}.CURATED.FCT_QUERY_ATTRIBUTION
        GROUP BY 1""",
    PAGE, "attribution_buckets",
)
if not attr.empty:
    total_cr = attr["CR"].sum()
    attributed = attr.loc[attr["BUCKET"] == "QUERY_ATTRIBUTED", "CR"].sum()
    sig3.metric(
        "Credits attributable to a query",
        pct(100.0 * attributed / total_cr if total_cr else 0),
        "rest is idle or serverless",
        delta_color="off",
    )
    sig3.caption("Detail on Cost Attribution.")
else:
    st.info(
        "No query-attributed credits in this window. Attribution lags consumption by roughly 8 hours, so today's queries are not here yet.",
        icon=mi("info"),
    )

eff = q(
    f"""WITH bm AS (
          SELECT BENCHMARK_KEY, BENCHMARK_VALUE, AS_OF_DATE, SOURCE
          FROM {DB}.CONFIG.BENCHMARKS
        ),
        wh_credits AS (
          SELECT SUM(CREDITS_BILLED) AS CR
          FROM {DB}.CURATED.FCT_DAILY_CREDITS
          WHERE SERVICE_TYPE = 'WAREHOUSE_METERING'
            AND USAGE_DATE_UTC >= DATEADD('day',-30,CURRENT_DATE())
            AND ACCOUNT_NAME = CURRENT_ACCOUNT_NAME()
        ),
        qd AS (
          SELECT SUM(QUERY_COUNT) AS QUERIES, SUM(BYTES_SCANNED)/POWER(1024,4) AS TB
          FROM {DB}.LANDING.LND_QUERY_DAILY
          WHERE USAGE_DATE_UTC >= DATEADD('day',-30,CURRENT_DATE())
        )
        SELECT (SELECT CR FROM wh_credits)                                    AS WH_CREDITS,
               (SELECT QUERIES FROM qd)                                       AS QUERIES,
               (SELECT TB FROM qd)                                            AS TB_SCANNED,
               (SELECT CR FROM wh_credits) / NULLIF((SELECT QUERIES FROM qd)/1000.0,0) AS CR_PER_1K_QUERIES,
               (SELECT CR FROM wh_credits) / NULLIF((SELECT TB FROM qd),0)     AS CR_PER_TB,
               (SELECT BENCHMARK_VALUE FROM bm WHERE BENCHMARK_KEY='CREDITS_PER_1000_EXEC_QUERIES') AS BM_QUERIES,
               (SELECT BENCHMARK_VALUE FROM bm WHERE BENCHMARK_KEY='CREDITS_PER_TB_SCANNED')        AS BM_TB,
               (SELECT MAX(AS_OF_DATE) FROM bm)                               AS BM_AS_OF""",
    PAGE, "efficiency_tiles_30d",
)

if not eff.empty:
    e = eff.iloc[0]
    e1, e2 = st.columns(2)

    # A ratio with a near-zero denominator is arithmetically valid and analytically
    # meaningless. This account ran 650k queries scanning 0.4 TB, which makes
    # credits/TB explode. Guard rather than show a red 65x-worse number.
    MIN_TB = 1.0
    MIN_QUERIES = 1000

    def _tile(col, label, actual, benchmark, denom, min_denom, denom_label):
        if actual is None or benchmark is None or pd.isna(actual):
            col.metric(label, "n/a")
            return
        if denom is None or pd.isna(denom) or denom < min_denom:
            col.metric(label, num(actual, 2), "not comparable", delta_color="off")
            col.caption(
                f"Denominator is only {num(denom, 2)} {denom_label}, below the "
                f"{num(min_denom, 0)} needed for the median to mean anything."
            )
            return
        better = actual <= benchmark
        col.metric(
            label, num(actual, 2),
            f"median {num(benchmark, 1)} - {'better' if better else 'worse'}",
            delta_color="normal" if better else "inverse",
        )

    _tile(e1, "Credits / 1000 executed queries", e["CR_PER_1K_QUERIES"],
          e["BM_QUERIES"], e["QUERIES"], MIN_QUERIES, "queries")
    _tile(e2, "Credits / TB scanned", e["CR_PER_TB"], e["BM_TB"],
          e["TB_SCANNED"], MIN_TB, "TB scanned")
    st.caption(
        f"Installed account, trailing 30 days: {num(e['WH_CREDITS'], 1)} warehouse "
        f"compute credits, {num(e['QUERIES'], 0)} queries, "
        f"{num(e['TB_SCANNED'], 2)} TB scanned. Both ratios use warehouse compute "
        f"credits as the numerator, matching how the published medians are defined. "
        f"Benchmarks as of {d(e['BM_AS_OF'])}."
    )
else:
    st.info(
        "No efficiency comparison yet: this account has not scanned or run enough for the ratios to mean anything.",
        icon=mi("info"),
    )
