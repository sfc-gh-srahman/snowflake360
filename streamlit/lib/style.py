"""Shared visual language for Snowflake360, matched to A360.

Kept separate from sf.py so that connection/query concerns and presentation
concerns stay independent. Everything here is re-exported from sf.py, so pages
import from one place.

The gap analysis this implements is in docs/a360-style-gap.md.
"""

from __future__ import annotations

import functools

import pandas as pd
import streamlit as st

# Parsed once. Used to gate widgets and icon syntax that only exist in newer
# Streamlit releases, since the Streamlit in Snowflake runtime version is set by
# the account this app is installed into, not by us.
def _parse_version(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in raw.split(".")[:3]:
        digits = "".join(c for c in piece if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


_ST_VERSION = _parse_version(getattr(st, "__version__", "0"))

# A360's palette. Sampled from the rendered app rather than guessed, so charts
# built here look like charts built there.
SNOW_BLUE = "#29B5E8"      # Snowflake cyan. Rules, headings, primary emphasis.
ACTUAL_BLUE = "#7FD3F0"    # Light blue. Actual consumption bars and areas.
CUMULATIVE = "#11567F"     # Cumulative line. A360 uses black; this reads better
                           # on both light and dark Streamlit themes.
FORECAST = "#F5A623"       # Amber. Projected/forecast regions.
BREACH = "#D6336C"         # Red. Overage and threshold breach markers.
STORAGE = "#4DD0B1"        # Mint. Storage series.
SECONDARY = "#FF9F40"      # Orange. Marketplace and other secondary series.
NEUTRAL = "#8C9BAB"        # Gray. Context, "other", de-emphasised series.

# Ordered palette for categorical series, so repeated categories keep the same
# colour across pages.
CATEGORY_SCALE = [
    ACTUAL_BLUE, SNOW_BLUE, STORAGE, SECONDARY, CUMULATIVE,
    FORECAST, BREACH, NEUTRAL, "#A78BFA", "#6EE7B7",
]

def mi(name: str) -> str:
    """A Material Symbols icon name, or an emoji standing in for it.

    Streamlit only accepts ":material/...:" in an icon= argument from 1.36
    onward, and raises on it before that. Snowflake360 is deployed into accounts
    whose Streamlit in Snowflake runtime version we do not choose, so an icon --
    the least important thing on any panel -- must not be able to break a page.
    """
    if _ST_VERSION >= (1, 36):
        return f":material/{name}:"
    return _ICON_FALLBACK.get(name, "")


_ICON_FALLBACK = {
    "info": "ℹ️",
    "warning": "⚠️",
    "check_circle": "✅",
    "analytics": "📊",
    "cloud_download": "⬇️",
    "notifications_off": "🔔",
}


_CSS = f"""<style>
/* A360's section headings are cyan and lighter than a default subheader. */
.sf360-section {{
    color: {SNOW_BLUE};
    font-weight: 700;
    font-size: 1.15rem;
    margin: 1.4rem 0 0.35rem 0;
}}

/* The heavy cyan rule that sits under every A360 page title.
   Rendered as a div rather than an <hr>: Streamlit ships its own hr styling that
   wins on specificity, so an <hr> comes out as a thin gray line regardless of
   what is set here. */
.sf360-rule {{
    height: 4px;
    background: {SNOW_BLUE};
    border: none;
    margin: 0.1rem 0 1.1rem 0;
    border-radius: 2px;
}}

.sf360-customer {{
    font-size: 1.6rem;
    font-weight: 700;
    line-height: 1.25;
    margin-bottom: 0.1rem;
}}
.sf360-customer .sf360-page {{ color: {SNOW_BLUE}; }}

/* Formula footnote: small, gray, and allowed to wrap. */
.sf360-note {{
    color: {NEUTRAL};
    font-size: 0.78rem;
    line-height: 1.45;
    margin: 0.35rem 0 0.9rem 0;
}}

/* A360 fits ~20 nav items on screen. Streamlit's default spacing fits far
   fewer, so tighten it. Native nav is kept -- see the gap analysis. */
section[data-testid="stSidebar"] [data-testid="stSidebarNav"] li {{
    margin-bottom: 0.05rem;
}}
section[data-testid="stSidebar"] [data-testid="stSidebarNav"] a {{
    padding-top: 0.2rem;
    padding-bottom: 0.2rem;
}}
section[data-testid="stSidebar"] [data-testid="stSidebarNav"] a p {{
    font-size: 0.86rem;
}}

/* Metric captions in A360 are annotations, not just deltas, so they need to be
   readable at length rather than clipped. */
div[data-testid="stMetricDelta"] {{ font-size: 0.78rem; }}
</style>
"""


def inject_css() -> None:
    """Emit the stylesheet once per page run."""
    st.markdown(_CSS, unsafe_allow_html=True)


def page_header(page_title: str, customer: str | None = None) -> None:
    """A360's title block: page name, customer, then a heavy cyan rule.

    A360 also prints a Salesforce opportunity ID here. It is deliberately omitted:
    it is a Snowflake-internal CRM key with no meaning inside a customer account.
    """
    inject_css()
    if customer:
        st.markdown(
            f'<div class="sf360-customer">'
            f'<span class="sf360-page">{page_title}</span> &mdash; {customer}'
            f"</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div class="sf360-customer"><span class="sf360-page">{page_title}</span></div>',
            unsafe_allow_html=True,
        )
    st.markdown('<div class="sf360-rule"></div>', unsafe_allow_html=True)


def section(label: str) -> None:
    """Cyan section heading, matching A360's 'Contract Breakout' style."""
    st.markdown(f'<div class="sf360-section">{label}</div>', unsafe_allow_html=True)


def note(text: str) -> None:
    """Small gray footnote, for formula breakdowns and caveats."""
    st.markdown(f'<div class="sf360-note">{text}</div>', unsafe_allow_html=True)


def chart_hint() -> None:
    """A360 prints these affordance hints under interactive charts."""
    st.caption(
        "Click a legend entry to isolate or restore a series. "
        "Tooltips may omit categories when the chart is small; expand it for the full view."
    )


def table_with_download(
    df: pd.DataFrame,
    file_name: str,
    key: str,
    height: int | None = None,
    column_config: dict | None = None,
    hide_index: bool = True,
    money_cols: list[str] | None = None,
    currency: str | None = None,
) -> None:
    """Render a dataframe with A360's 'Download data as CSV' affordance beneath it.

    A360 has this on every table without exception, and it is clearly load-bearing
    for how the tool gets used in practice, so it is centralised rather than
    left to each page to remember.

    Currency columns are formatted here rather than at the call site. Doing it per
    table is how the app drifted into showing a per-credit price as a bare "3"
    next to a total rendered as "$1,503,506.12": every new query is one more
    chance to forget. Pass money_cols to override the detection, or [] to disable
    it for a table whose numbers are not money.

    The currency symbol comes from the active contract via style.active_currency()
    rather than being hardcoded, so a table and a metric on the same page cannot
    disagree about what currency the figure is in.
    """
    # Call sites sometimes pass a Styler for number formatting. st.dataframe
    # accepts one, but Styler has no .empty and no .to_csv, so unwrap it for the
    # download and hand the Styler itself to the display.
    frame = getattr(df, "data", df)

    # Only auto-format a plain DataFrame. A call site that already built a Styler
    # has made a deliberate presentation choice, and re-styling would discard it.
    if df is frame:
        df = money(frame, money_cols, currency)

    # height must be omitted rather than passed as None: Streamlit rejects None
    # with StreamlitInvalidHeightError, which halts the whole script since this
    # runs at module scope.
    kwargs = {"height": height} if height is not None else {}
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=hide_index,
        column_config=column_config or {},
        **kwargs,
    )
    if frame.empty:
        return
    st.download_button(
        "Download data as CSV",
        data=frame.to_csv(index=not hide_index).encode("utf-8"),
        file_name=file_name if file_name.endswith(".csv") else f"{file_name}.csv",
        mime="text/csv",
        key=f"dl_{key}",
    )


# ---------------------------------------------------------------------------
# Currency
#
# The symbol table lives here, in the lower-level module, rather than next to
# sf.usd() which is its other consumer. sf imports from style, so this is the
# only direction that gives both the table and the metric formatter one shared
# definition without a circular import.
#
# It has to be shared. When money_cell() hardcoded "$" and usd() resolved the
# contract's METERED_CURRENCY, a non-USD contract rendered "$1,234.56" in every
# table and "\u20ac1,234.56" in every metric -- on the same page, describing the
# same figure. That is precisely the failure usd()'s own docstring sets out to
# prevent, reintroduced one layer down.
# ---------------------------------------------------------------------------

CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "\u20ac", "GBP": "\u00a3", "JPY": "\u00a5",
    "CAD": "CA$", "AUD": "A$", "NZD": "NZ$", "CHF": "CHF ", "INR": "\u20b9",
    "BRL": "R$", "MXN": "MX$", "SGD": "S$", "HKD": "HK$", "SEK": "SEK ",
    "NOK": "NOK ", "DKK": "DKK ", "PLN": "PLN ", "ZAR": "R",
}

_DEFAULT_CURRENCY = "USD"


def set_currency(code: str | None) -> None:
    """Set the currency every unqualified amount on this page formats in.

    Called once per page by sf.init_page() from the active contract, so a page
    does not have to thread a currency code through every table and metric call.
    That threading is what was skipped before, and skipping it is how tables and
    metrics came to disagree: there are ~30 table call sites and only one of nine
    pages was passing a currency at all.

    Held in st.session_state rather than a module global because Streamlit reuses
    the module across reruns and across pages within a session, so a module global
    would persist a stale currency after the contract changed.
    """
    st.session_state["_sf360_currency"] = (
        (code or _DEFAULT_CURRENCY).strip().upper() or _DEFAULT_CURRENCY
    )


def active_currency() -> str:
    """The contract's currency, defaulting to USD before one is configured."""
    return st.session_state.get("_sf360_currency", _DEFAULT_CURRENCY)


def currency_symbol(code: str | None = None) -> str:
    """Symbol for a currency code, falling back to a prefixed code.

    Falling back to "SEK " rather than "$" is deliberate: a wrong symbol silently
    misstates the amount, whereas a bare code is merely plain.
    """
    resolved = (code or active_currency()).strip().upper()
    return CURRENCY_SYMBOLS.get(resolved, f"{resolved} ")


def money_cell(v, decimals: int = 2, currency: str | None = None) -> str:
    """One currency cell. Sign outside the symbol, matching sf.usd()."""
    if v is None or pd.isna(v):
        return "n/a"
    sign = "-" if v < 0 else ""
    return f"{sign}{currency_symbol(currency)}{abs(v):,.{decimals}f}"


# Column-name fragments that always denote a currency amount in this app's
# models. Used to catch money columns generically so a new query does not have to
# remember to format them. CREDIT alone is deliberately absent: CREDITS is a
# quantity, while CREDIT_PRICE is a rate, and only the latter is money.
_MONEY_HINTS = (
    "PRICE", "USD", "DOLLAR", "AMOUNT", "SPEND", "REVENUE", "INVOICED",
    "ALLOCATION", "BALANCE", "CAPACITY", "OVERAGE", "RATE_PER", "PER_DAY",
    "COST", "_FEE", "RUN_RATE",
)

# Explicitly not money, even though the name matches a hint above.
_MONEY_DENY = ("PCT", "PERCENT", "SHARE", "DAYS", "COUNT", "_ID", "CREDITS")


def money_columns(df) -> list[str]:
    """Numeric columns in df that represent currency."""
    frame = getattr(df, "data", df)
    out = []
    for col in frame.columns:
        name = str(col).upper()
        if any(bad in name for bad in _MONEY_DENY):
            continue
        if not any(hint in name for hint in _MONEY_HINTS):
            continue
        if pd.api.types.is_numeric_dtype(frame[col]):
            out.append(col)
    return out


def money(df, cols: list[str] | None = None, currency: str | None = None):
    """Format currency columns as $1,234.56 for display, leaving the data numeric.

    Applied through a pandas Styler rather than Streamlit's column_config so the
    output carries a currency symbol, two decimals AND a thousands separator on
    every Streamlit version. column_config's printf-style format has no
    separator, and the named presets that do only exist on newer releases -- and
    the Streamlit in Snowflake runtime version is set by the installing account,
    not by us.

    Styling only affects presentation: table_with_download unwraps the Styler so
    the CSV still contains raw numbers for the reader's own arithmetic.
    """
    frame = getattr(df, "data", df)
    target = [c for c in (cols if cols is not None else money_columns(frame))
              if c in frame.columns]
    if not target:
        return df
    fmt = functools.partial(money_cell, currency=currency)
    return frame.style.format({c: fmt for c in target}, na_rep="n/a")


def money_column_config(df, cols: list[str] | None = None,
                        currency: str | None = None) -> dict:
    """column_config for an EDITABLE table, where a Styler cannot be used.

    st.data_editor needs real number columns to edit, so this uses the printf
    format instead. That loses the thousands separator, which is acceptable here
    because the only editable money fields are per-credit and per-TB rates -- all
    well under four figures, so no separator would ever be shown anyway.
    """
    frame = getattr(df, "data", df)
    target = cols if cols is not None else money_columns(frame)
    # printf needs a literal symbol, and a multi-character fallback like "SEK "
    # is fine here -- these are editable rate fields, not dense figures.
    symbol = currency_symbol(currency).replace("%", "%%")
    return {
        c: st.column_config.NumberColumn(
            str(c).replace("_", " ").title(), format=f"{symbol}%.2f",
            step=0.01, min_value=0.0,
        )
        for c in target if c in frame.columns
    }


# Range options as (label, trailing days). QTD and YTD carry None because they
# resolve against the Snowflake fiscal calendar rather than a fixed window.
_RANGES: dict[str, int | None] = {
    "1M": 30,
    "6M": 183,
    "1Y": 365,
    "QTD": None,
    "YTD": None,
    "All": 3650,
}


def range_selector(key: str, default: str = "1Y") -> tuple[str, int]:
    """A360's horizontal 1M | 6M | 1Y | QTD | YTD | All control.

    Returns the chosen label and a trailing-day count. QTD and YTD are resolved
    against Snowflake's fiscal calendar, which starts February 1, so a fiscal
    quarter boundary does not coincide with a calendar one.
    """
    labels = list(_RANGES)
    # st.segmented_control arrived in Streamlit 1.40. Streamlit in Snowflake
    # resolves its own runtime version, and this app is meant to be deployed into
    # accounts we do not control, so the widget is treated as optional rather
    # than assumed. A horizontal radio is the same control with heavier chrome.
    if hasattr(st, "segmented_control"):
        choice = st.segmented_control(
            "Range", labels, default=default, key=f"range_{key}",
            label_visibility="collapsed",
        ) or default
    else:
        choice = st.radio(
            "Range", labels, index=labels.index(default), key=f"range_{key}",
            horizontal=True, label_visibility="collapsed",
        )

    days = _RANGES[choice]
    if days is not None:
        return choice, days

    from lib.sf import DB, q

    # DIM_DATE labels fiscal periods but does not store their start dates, so the
    # start is the earliest day sharing today's label. Snowflake's fiscal year
    # starts February 1, so this is not a calendar quarter boundary.
    grain = "FISCAL_QUARTER_LABEL" if choice == "QTD" else "FISCAL_YEAR_LABEL"
    df = q(
        f"""WITH today AS (
              SELECT {grain} AS L FROM {DB}.CURATED.DIM_DATE
              WHERE DATE_UTC = CURRENT_DATE
            )
            SELECT DATEDIFF(day, MIN(dd.DATE_UTC), CURRENT_DATE) + 1 AS DAYS
            FROM {DB}.CURATED.DIM_DATE dd JOIN today ON dd.{grain} = today.L""",
        "shared", f"range_{choice.lower()}",
    )
    # An empty DIM_DATE row for today would otherwise silently produce a
    # zero-day window and an empty chart.
    if df.empty or pd.isna(df.iloc[0]["DAYS"]):
        return choice, 90
    return choice, max(int(df.iloc[0]["DAYS"]), 1)
