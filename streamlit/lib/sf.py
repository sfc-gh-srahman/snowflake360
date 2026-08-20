"""Snowflake connection, query helpers, and self-instrumentation for Snowflake360.

Runs both locally (streamlit run) and in Streamlit in Snowflake. Locally it uses
connections.toml via snowflake.connector; in SiS it uses the active session.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import date, datetime

import pandas as pd
import streamlit as st

# Presentation helpers live in lib/style.py but are re-exported here so pages
# import from a single module. See docs/a360-style-gap.md.
from lib.style import (  # noqa: F401
    ACTUAL_BLUE,
    BREACH,
    CATEGORY_SCALE,
    CUMULATIVE,
    FORECAST,
    NEUTRAL,
    chart_hint,
    currency_symbol,
    mi,
    money_column_config,
    note,
    page_header,
    range_selector,
    section,
    set_currency,
    table_with_download,
)

APP_NAME = "SNOWFLAKE360"
APP_VERSION = "0.1.0"
DB = "SF360"

# Every model carries a SCOPE column. Anything ACCOUNT-scoped must never render
# under an org header without a banner, because the premium ORGANIZATION_USAGE
# views (QUERY_HISTORY, QUERY_ATTRIBUTION_HISTORY, QUERY_INSIGHTS, CORTEX_*) are
# organization-account-only and therefore unavailable here.
SCOPE_ACCOUNT_ONLY_NOTE = (
    "This page is scoped to the installed account only. Query-level detail is not "
    "available org-wide because ORGANIZATION_USAGE query views are premium and exist "
    "only in the organization account."
)


def _has_active_session() -> bool:
    try:
        from snowflake.snowpark.context import get_active_session

        get_active_session()
        return True
    except Exception:
        return False


@st.cache_resource(show_spinner=False)
def get_conn():
    """Return a live connection. Prefers the SiS session, falls back to connections.toml."""
    if _has_active_session():
        from snowflake.snowpark.context import get_active_session

        return ("snowpark", get_active_session())

    import snowflake.connector

    # No default connection name. A missing one used to fall back to a developer
    # profile name, which on any other machine fails with "connection not found"
    # and points at the wrong problem. Local development is the only path that
    # reaches here at all -- SiS returns above.
    conn_name = os.environ.get("SNOWFLAKE_DEFAULT_CONNECTION_NAME")
    if not conn_name:
        raise RuntimeError(
            "SNOWFLAKE_DEFAULT_CONNECTION_NAME is not set. Local runs need a named "
            "connection from connections.toml, for example:\n"
            "    SNOWFLAKE_DEFAULT_CONNECTION_NAME=my_conn streamlit run Snowflake360.py"
        )
    conn = snowflake.connector.connect(
        connection_name=conn_name,
        client_session_keep_alive=True,
    )
    cur = conn.cursor()
    # Docs mandate UTC when reconciling ACCOUNT_USAGE against ORGANIZATION_USAGE.
    # Daily cost buckets are UTC; only display timestamps are converted.
    cur.execute("ALTER SESSION SET TIMEZONE = 'UTC'")
    # Deliberately no USE WAREHOUSE. In SiS the warehouse comes from the STREAMLIT
    # object's QUERY_WAREHOUSE, and locally it comes from the named connection.
    # Overriding it here pinned every local run to one hardcoded warehouse, which
    # silently ignored whatever the connection specified.
    cur.close()
    return ("connector", conn)


def _tag(page: str, query_name: str) -> str:
    return json.dumps(
        {
            "app": APP_NAME,
            "app_version": APP_VERSION,
            "page": page,
            "query_name": query_name,
            "run_id": st.session_state.get("run_id", "unknown"),
        }
    )


@st.cache_resource(show_spinner=False)
def _tagging_mode() -> str:
    """How this runtime is able to carry a query tag: "session" or "comment".

    Streamlit in Snowflake executes the app inside a stored procedure sandbox,
    which rejects ALTER SESSION outright:

        090236 (42601): Stored procedure execution error:
        Unsupported statement type 'ALTER_SESSION'

    Every query in this app is tagged, so on SiS that turned the first query on
    the first page into a fatal error and nothing below it rendered. The
    capability is probed once per session rather than assumed from whether a
    Snowpark session exists, because "local Snowpark" and "SiS Snowpark" look
    identical from inside the process but differ on exactly this point.

    When ALTER SESSION is unavailable the tag is prepended to the statement as a
    comment instead. That still lands in ACCOUNT_USAGE.QUERY_HISTORY.QUERY_TEXT,
    so the app's self-instrumentation degrades from a parsable QUERY_TAG column
    to a parsable comment rather than disappearing.
    """
    kind, handle = get_conn()
    probe = "ALTER SESSION SET QUERY_TAG = 'SF360_PROBE'"
    # Overridable so the SiS behaviour can be reproduced from a laptop, where
    # ALTER SESSION succeeds and the fallback would otherwise never be exercised.
    forced = os.environ.get("SF360_QUERY_TAG_MODE", "").strip().lower()
    if forced in ("session", "comment"):
        return forced
    try:
        if kind == "snowpark":
            handle.sql(probe).collect()
        else:
            cur = handle.cursor()
            try:
                cur.execute(probe)
            finally:
                cur.close()
        return "session"
    except Exception:
        return "comment"


def _tagged(kind, handle, sql: str, page: str, query_name: str) -> str:
    """Attach the query tag, returning the statement to actually run."""
    tag = _tag(page, query_name)
    if _tagging_mode() == "comment":
        # Single line: a newline inside a -- comment would comment out the query.
        return f"-- SF360 {tag}\n{sql}"
    stmt = "ALTER SESSION SET QUERY_TAG = '{}'".format(tag.replace("'", "''"))
    if kind == "snowpark":
        handle.sql(stmt).collect()
    else:
        cur = handle.cursor()
        try:
            cur.execute(stmt)
        finally:
            cur.close()
    return sql


def _coerce(df: pd.DataFrame) -> pd.DataFrame:
    """Make Snowflake output plottable.

    Snowflake NUMBER arrives as decimal.Decimal, which pandas holds as dtype object.
    Altair silently renders axes but no marks for object columns, so charts come out
    empty with no error. DATE arrives as datetime.date, also object. Coerce both here
    rather than per chart, so every page inherits the fix.
    """
    from decimal import Decimal

    for col in df.columns:
        if df[col].dtype != "object":
            continue
        sample = df[col].dropna()
        if sample.empty:
            continue
        first = sample.iloc[0]
        if isinstance(first, Decimal):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        elif isinstance(first, (date, datetime)):
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


@st.cache_data(ttl=900, show_spinner=False)
def q(sql: str, page: str = "unknown", query_name: str = "unnamed") -> pd.DataFrame:
    """Run a query with a structured query tag. Cached 15 minutes.

    The tag mirrors A360's instrumentation so the app can report on its own usage.
    """
    kind, handle = get_conn()
    started = time.time()
    stmt = _tagged(kind, handle, sql, page, query_name)
    if kind == "snowpark":
        df = handle.sql(stmt).to_pandas()
    else:
        cur = handle.cursor()
        try:
            cur.execute(stmt)
            df = cur.fetch_pandas_all() if cur.description else pd.DataFrame()
        finally:
            cur.close()
    st.session_state.setdefault("query_log", []).append(
        {"page": page, "query": query_name, "seconds": round(time.time() - started, 3)}
    )
    return _coerce(df)


def d(v) -> str:
    """Render a DATE as YYYY-MM-DD.

    _coerce promotes DATE to datetime64 so Altair can plot it, which makes raw
    display show a spurious 00:00:00. Always format dates through here.
    """
    if v is None or pd.isna(v):
        return "n/a"
    return pd.Timestamp(v).strftime("%Y-%m-%d")


def exec_sql(sql: str, page: str = "unknown", query_name: str = "unnamed"):
    """Run a statement for its side effect. Deliberately not cached.

    q() is cached, so it must never be used for DML, DDL or CALL -- a second
    identical write would silently return the first call's cached result and
    appear to succeed without touching the database.
    """
    kind, handle = get_conn()
    stmt = _tagged(kind, handle, sql, page, query_name)
    if kind == "snowpark":
        rows = handle.sql(stmt).collect()
        return [tuple(r) for r in rows]
    cur = handle.cursor()
    try:
        cur.execute(stmt)
        return cur.fetchall() if cur.description else []
    finally:
        cur.close()


def sql_str(v) -> str:
    """Quote a value as a SQL literal, or NULL. Escapes single quotes."""
    if v is None or v == "":
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


ORDER_FORM_STAGE = f"@{DB}.ORDERFORM.ORDER_FORMS"


def upload_to_stage(file_name: str, data: bytes) -> str:
    """Put a file on the order form stage. Works in SiS and locally.

    AUTO_COMPRESS must be off: AI_PARSE_DOCUMENT cannot read a gzipped PDF, and
    the connector compresses by default, which produces a .pdf.gz that fails to
    parse with a misleading error.
    """
    import io
    import re

    # Stage paths are awkward with spaces and quotes, and order form filenames
    # routinely contain both. Normalise but keep the name recognisable.
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", file_name).strip("_") or "order_form.pdf"

    kind, handle = get_conn()
    if kind == "snowpark":
        handle.file.put_stream(
            io.BytesIO(data), f"{ORDER_FORM_STAGE}/{safe}",
            auto_compress=False, overwrite=True,
        )
    else:
        import os
        import tempfile

        tmp = os.path.join(tempfile.mkdtemp(), safe)
        with open(tmp, "wb") as fh:
            fh.write(data)
        try:
            posix = tmp.replace("\\", "/")
            exec_sql(
                f"PUT 'file://{posix}' {ORDER_FORM_STAGE} "
                "AUTO_COMPRESS=FALSE OVERWRITE=TRUE",
                page="Order Form", query_name="put_order_form",
            )
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return safe


def init_page(title: str) -> None:
    """Standard page setup: run id, currency, then A360's title block.

    The header names the customer, which A360 does on every page. Sourcing it from
    the contract means it appears automatically once an order form is accepted,
    and degrades to a plain title before that.

    The contract's currency is resolved here, once, so every amount the page
    formats -- metric or table cell -- uses the same symbol without each call site
    having to pass one.
    """
    if "run_id" not in st.session_state:
        st.session_state["run_id"] = str(uuid.uuid4())[:8]
    set_currency(metered_currency())
    page_header(title, customer_name())


# ---------------------------------------------------------------------------
# Formatting helpers
#
# The currency symbol table lives in lib/style.py, which owns both this module's
# usd() and the table cell formatter, so the two cannot drift apart.
# ---------------------------------------------------------------------------

def usd(v, decimals: int = 2, currency: str | None = None) -> str:
    """Currency format: symbol, two decimals and a thousands separator.

    Two decimals is the default deliberately. Rounding to whole dollars in one
    place and not another makes two views of the same figure disagree by up to a
    dollar, which reads as a reconciliation bug rather than as rounding.

    A currency code may be passed for contracts that are not in USD. When it is
    omitted the contract's currency is used, resolved through the same table that
    formats table cells, so a figure cannot appear as "$1,234.56" in a metric and
    "\u20ac1,234.56" in the table beneath it.
    """
    if v is None or pd.isna(v):
        return "n/a"
    # Sign outside the symbol: "-$1,145,966.35", not "$-1,145,966.35". Negative
    # balances are the normal way this app reports an overspent contract, so the
    # sign is load-bearing and belongs where a reader expects to find it.
    sign = "-" if v < 0 else ""
    return f"{sign}{currency_symbol(currency)}{abs(v):,.{decimals}f}"


def num(v, decimals: int = 1) -> str:
    if v is None or pd.isna(v):
        return "n/a"
    return f"{v:,.{decimals}f}"


def pct(v, decimals: int = 1) -> str:
    if v is None or pd.isna(v):
        return "n/a"
    return f"{v:,.{decimals}f}%"


def display_ts(ts) -> str:
    """Render a point-in-time timestamp in the configured display timezone.

    Daily cost buckets stay UTC. Only point-in-time timestamps convert, because a
    daily bucket shifted into a local zone would move spend between days and no
    longer reconcile against ACCOUNT_USAGE.

    The zone comes from CONFIG.SETTINGS.DISPLAY_TIMEZONE. It was previously
    hardcoded to America/Chicago even though that setting already existed, so
    every deployment rendered upload and acceptance times in US Central
    regardless of where the customer was. UTC is the fallback rather than a
    guessed local zone: an unconverted timestamp labelled UTC is unambiguous,
    whereas a wrong local zone is silently misleading.
    """
    if ts is None or pd.isna(ts):
        return "n/a"
    if not isinstance(ts, (date, datetime)):
        return str(ts)
    try:
        zone = (settings().get("DISPLAY_TIMEZONE") or "UTC").strip() or "UTC"
    except Exception:
        zone = "UTC"
    s = pd.Timestamp(ts)
    if s.tzinfo is None:
        s = s.tz_localize("UTC")
    try:
        s = s.tz_convert(zone)
    except Exception:
        # An unrecognised zone must not break a page over a timestamp label.
        s = s.tz_convert("UTC")
    return f"{s:%Y-%m-%d %H:%M} {s:%Z}"


# ---------------------------------------------------------------------------
# Shared data accessors
# ---------------------------------------------------------------------------

@st.cache_data(ttl=900, show_spinner=False)
def settings() -> dict:
    df = q(
        f"SELECT SETTING_KEY, SETTING_VALUE FROM {DB}.CONFIG.SETTINGS",
        "shared",
        "settings",
    )
    return dict(zip(df["SETTING_KEY"], df["SETTING_VALUE"]))


@st.cache_data(ttl=900, show_spinner=False)
def freshness() -> pd.DataFrame:
    return q(
        f"""SELECT SOURCE_NAME, SCOPE, MEASURE, AS_OF_DATE, LAG_DAYS,
                   DOCUMENTED_LATENCY_HOURS, SOURCE_VIEW
            FROM {DB}.CURATED.FCT_FRESHNESS ORDER BY SCOPE, MEASURE""",
        "shared",
        "freshness",
    )


@st.cache_data(ttl=900, show_spinner=False)
def scope_accounts() -> pd.DataFrame:
    return q(
        f"""SELECT ACCOUNT_KEY, ACCOUNT_NAME, ACCOUNT_LOCATOR, REGION, CLOUD,
                   SERVICE_LEVEL, IS_DELETED, IS_MANAGED, IS_GOV_REGION,
                   IS_INSTALLED_ACCOUNT
            FROM {DB}.CURATED.DIM_ACCOUNT
            WHERE IS_IN_SCOPE ORDER BY ACCOUNT_NAME""",
        "shared",
        "scope_accounts",
    )


def customer_name() -> str | None:
    """Customer name for page headers, or None before a contract exists.

    Wrapped in try/except because init_page runs before anything else on every
    page: a header lookup must never be the thing that prevents a page from
    rendering its own error message.
    """
    try:
        c = active_contract()
        if c.empty:
            return None
        v = c.iloc[0].get("CUSTOMER_NAME")
        if v is None or pd.isna(v):
            return None
        return str(v).strip() or None
    except Exception:
        return None


def metered_currency() -> str:
    """The active contract's currency code, or USD before one exists.

    Wrapped like customer_name() for the same reason: init_page calls it before a
    page has rendered anything, so it must not be able to raise. USD is the right
    fallback because it is the CONFIG.CONTRACT column default.
    """
    try:
        c = active_contract()
        if c.empty:
            return "USD"
        v = c.iloc[0].get("METERED_CURRENCY")
        if v is None or pd.isna(v):
            return "USD"
        return str(v).strip().upper() or "USD"
    except Exception:
        return "USD"


@st.cache_data(ttl=900, show_spinner=False)
def active_contract() -> pd.DataFrame:
    return q(
        f"""SELECT ORGANIZATION_NAME, CONTRACT_NUMBER, CUSTOMER_NAME, AGREEMENT_TYPE,
                   CONTRACT_START_DATE, CONTRACT_END_DATE, METERED_CURRENCY,
                   CAPACITY_PURCHASED, TOTAL_FREE_USAGE, ROLLOVER, ADJUSTMENT,
                   BALANCE_TRANSFER, CURRENCY_CONVERSION_ADJUSTMENT,
                   DATA_SHARING_REBATE, BALANCE_MIGRATION, CONTRACT_SOURCE,
                   BILLING_FREQUENCY, ON_DEMAND_BILLING_FREQUENCY,
                   TERM_LENGTH_MONTHS, CAPACITY_CREDIT_PRICE,
                   ON_DEMAND_CREDIT_PRICE, CAPACITY_DISCOUNT_PCT,
                   INVOICE_PULL_FORWARD, EDITION, CLOUD_PROVIDER, REGION_NAME,
                   STORAGE_PRICE_PER_TB, STORAGE_TIER, PAYMENT_TERMS_DAYS
            FROM {DB}.CONFIG.CONTRACT
            WHERE IS_ACTIVE AND VALID_TO IS NULL""",
        "shared",
        "active_contract",
    )


def render_freshness_caption() -> None:
    """Two data-through dates: credits and dollars have different latencies."""
    f = freshness()
    if f.empty:
        # A first-run signal rather than silence. Before the first refresh there is
        # no freshness row, and returning quietly left every page with no
        # indication of whether it was showing thin data or no data.
        st.caption(
            "No usage history loaded yet. Run the refresh from the Setup & Settings "
            "page, or wait for the 11:00 UTC schedule."
        )
        return
    parts = []
    for measure in ("credits", "dollars"):
        row = f[(f["SCOPE"] == "ORG") & (f["MEASURE"] == measure)]
        if not row.empty:
            parts.append(f"{measure} through {d(row.iloc[0]['AS_OF_DATE'])}")
    if parts:
        st.caption("Data " + " | ".join(parts) + "  (all dates UTC)")


def reporting_mode() -> str:
    """"ORG" or "ACCOUNT", from CONFIG.SETTINGS.

    Written by scripts/setup.sql, which probes whether ORGANIZATION_USAGE is
    reachable. ACCOUNT means this is not an organization account, so every view
    sourced from ORGANIZATION_USAGE is legitimately empty.

    That distinction has to be surfaced, not inferred by the reader. Currency is
    the clearest case: FCT_DAILY_CURRENCY is sourced entirely from
    LND_ORG_CURRENCY_DAILY, so in ACCOUNT mode every dollar figure in the app is
    genuinely absent -- and rendering "$0.00" for it states a measurement that was
    never taken.
    """
    try:
        return (settings().get("MODE") or "ORG").strip().upper()
    except Exception:
        return "ORG"


def org_data_available() -> bool:
    return reporting_mode() == "ORG"


def render_org_unavailable(what: str = "This page") -> bool:
    """Explain an empty org-sourced panel, returning True when it is unavailable.

    Call it before rendering anything that reads ORGANIZATION_USAGE or a table
    derived from it, and skip the panel when it returns True:

        if render_org_unavailable("Consumption in currency"):
            st.stop()
    """
    if org_data_available():
        return False
    st.info(
        f"**{what} needs organization data, which is not available in this account.**\n\n"
        "SNOWFLAKE.ORGANIZATION_USAGE exists only in an organization account. Setup "
        "detected that and set the reporting mode to ACCOUNT, so currency amounts, "
        "the account inventory and org-wide rollups have no source here.\n\n"
        "This is a missing source, not a measurement of zero. Per-account credit "
        "usage, anomalies, warehouse activity and query attribution all still work.",
        icon=mi("info"),
    )
    return True


def render_config_banner() -> None:
    """Surface rate and contract provenance so invented values are never mistaken for real."""
    c = active_contract()
    if c.empty:
        st.error(
            "No active contract configured. Active Contract and projections cannot render. "
            "Add one on the Setup & Settings page, or upload your order form there."
        )
        return
    row = c.iloc[0]
    if row["CONTRACT_SOURCE"] == "CUSTOMER_ENTERED":
        st.warning(
            f"Contract **{row['CONTRACT_NUMBER']}** is customer-entered, not sourced from "
            f"Snowflake. Capacity {usd(row['CAPACITY_PURCHASED'])} and the term "
            f"{d(row['CONTRACT_START_DATE'])} to {d(row['CONTRACT_END_DATE'])} drive every "
            "projection on this page. Verify on the Setup & Settings page."
        )


def render_account_scope_banner() -> None:
    st.info(SCOPE_ACCOUNT_ONLY_NOTE, icon=mi("info"))


# Components that sum to total capacity, in A360's stated order, with the sign
# each carries in the formula. Names match CONFIG.CONTRACT columns.
_CAPACITY_PARTS = [
    ("Capacity Purchase", "CAPACITY_PURCHASED", 1),
    ("Free Usage", "TOTAL_FREE_USAGE", 1),
    ("Rollover", "ROLLOVER", 1),
    ("Adjustment", "ADJUSTMENT", 1),
    ("Balance Transfer", "BALANCE_TRANSFER", 1),
    ("Currency Conversion Adjustment", "CURRENCY_CONVERSION_ADJUSTMENT", 1),
    ("Data Sharing Rebate", "DATA_SHARING_REBATE", 1),
    ("Balance Migration", "BALANCE_MIGRATION", 1),
]


def total_capacity(row) -> float:
    """Total capacity as the sum of its components, not CAPACITY_PURCHASED alone.

    Every downstream percentage must divide by this, or a contract carrying
    rollover reports a burn rate that is too high.
    """
    total = 0.0
    for _, col, sign in _CAPACITY_PARTS:
        v = row.get(col)
        if v is not None and not pd.isna(v):
            total += sign * float(v)
    return total


def capacity_formula_note(row) -> None:
    """A360's footnote spelling out how total capacity is composed.

    This is the single most useful element on A360's contract page for a customer
    who cannot tell what their number is made of. Zero-valued components are
    omitted; A360 prints all of them, which buries the two or three that matter
    in a line of '$0'.
    """
    total = total_capacity(row)
    shown = []
    for label, col, sign in _CAPACITY_PARTS:
        v = row.get(col)
        if v is None or pd.isna(v) or float(v) == 0:
            continue
        amt = sign * float(v)
        shown.append(("+" if amt >= 0 else "-") + f" {label} {usd(abs(amt))}")

    if not shown:
        return
    body = " ".join(shown).lstrip("+ ").strip()
    note(
        f"<em>Capacity Used % is based on Total Capacity of {usd(total)} "
        f"= {body}. Components not shown are zero.</em>"
    )
