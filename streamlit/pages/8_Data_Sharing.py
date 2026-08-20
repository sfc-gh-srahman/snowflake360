"""Data Sharing - minimal outbound inventory with honest empty states."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.append(str(Path(__file__).parent.parent))
from lib.sf import (  # noqa: E402
    DB,
    init_page,
    mi,
    num,
    q,
    render_freshness_caption,
    section,
    table_with_download,
    render_org_unavailable,
)

st.set_page_config(page_title="Data Sharing | Snowflake360", layout="wide")
PAGE = "Data Sharing"
init_page("Data Sharing")
st.caption("Outbound share and listing inventory. Deliberately minimal in scope.")
render_freshness_caption()

inv = q(
    f"""SELECT OBJECT_KIND, OBJECT_NAME, OBJECT_DETAIL, OWNER_NAME,
               TARGET_ACCOUNTS, LISTING_GLOBAL_NAME, STATE, CREATED_DATE, DELETED_DATE
        FROM {DB}.LANDING.LND_SHARING_INVENTORY
        ORDER BY OBJECT_KIND, OBJECT_NAME""",
    PAGE,
    "sharing_inventory",
)

k = st.columns(3)
k[0].metric("Shares", int((inv["OBJECT_KIND"] == "SHARE").sum()) if not inv.empty else 0)
k[1].metric("Listings", int((inv["OBJECT_KIND"] == "LISTING").sum()) if not inv.empty else 0)

readers = q(
    f"""SELECT COUNT(*) AS N FROM {DB}.CURATED.DIM_ACCOUNT
        WHERE IS_MANAGED AND IS_IN_SCOPE""",
    PAGE,
    "reader_account_count",
)
k[2].metric("Reader accounts in scope", int(readers.iloc[0]["N"]))

if inv.empty:
    st.info(
        "**No outbound shares or listings exist in this account.** This page is correct for a "
        "customer who shares data, but it cannot be validated here — there is nothing to show. "
        "It was built last for that reason.",
        icon=mi("info"),
    )
else:
    table_with_download(
    inv,
    "snowflake360_data_sharing_1", "data_sharing_1",
)

# ---------------------------------------------------------------------------
# Egress, which does have data
# ---------------------------------------------------------------------------
section("Data transfer (egress)")

xfer = q(
    f"""SELECT TRANSFER_TYPE, SOURCE_CLOUD, SOURCE_REGION, TARGET_CLOUD, TARGET_REGION,
               SUM(TB_TRANSFERRED) AS TB
        FROM {DB}.LANDING.LND_DATA_TRANSFER_DAILY
        GROUP BY 1,2,3,4,5 HAVING SUM(TB_TRANSFERRED) > 0
        ORDER BY TB DESC LIMIT 25""",
    PAGE,
    "data_transfer",
)
if xfer.empty:
    st.info("No data transfer recorded.", icon=mi("info"))
else:
    show = xfer.copy()
    show["TB"] = show["TB"].apply(lambda v: num(v, 6))
    table_with_download(
    show,
    "snowflake360_data_sharing_2", "data_sharing_2",
)

# ---------------------------------------------------------------------------
# The structural gap, stated rather than papered over
# ---------------------------------------------------------------------------
st.divider()
with st.expander("Why consumer consumption is not shown", expanded=True):
    st.markdown(
        "A360's provider view reports consumer credits, the large majority of which are "
        "**internal same-org sharing**. Those credits are spent in a *sibling account*, so a "
        "single account cannot see them at all.\n\n"
        "Org mode closes part of this gap: cross-account metering is visible for in-scope "
        "accounts. What remains invisible is consumption by accounts outside the org, and by "
        "reader accounts, whose usage bills to a parent account and cannot be separated."
    )
