"""One connection bootstrap for the test and tooling scripts.

There were four near-copies of this logic -- in sis_harness.py, verification.py,
parity.py and sql/baseline/_script_grants.py -- each separately deciding a default
connection name and whether to pin a warehouse. Three read the environment
variable and one did not, so the same command could reach a different account
depending on which script was run.

Neither helper pins a warehouse. Pinning one was how the scripts came to disagree
with the app: they forced COMPUTE_WH while the deployed Streamlit object used its
own QUERY_WAREHOUSE, so a test could pass on different compute than production
used. The connection's warehouse is now authoritative, and SF360_TEST_WAREHOUSE
can override it when a specific one is needed.
"""
from __future__ import annotations

import os


def connection_name() -> str:
    """The named connection to use, or fail with an actionable message."""
    name = os.environ.get("SNOWFLAKE_DEFAULT_CONNECTION_NAME")
    if not name:
        raise SystemExit(
            "SNOWFLAKE_DEFAULT_CONNECTION_NAME is not set.\n"
            "    SNOWFLAKE_DEFAULT_CONNECTION_NAME=<your_connection> "
            ".venv/bin/python <script>"
        )
    return name


def _overrides() -> dict:
    """Role and warehouse overrides, applied at CONNECT time rather than with USE.

    They must be connection parameters, not USE ROLE / USE WAREHOUSE statements.
    This account's sessions are restricted and reject the statement form outright:

        003107 (42501): SQL execution error:
        Current session is restricted. USE ROLE not allowed.

    Passing them to connect() succeeds where the statement fails, which is also
    closer to how Streamlit in Snowflake works -- SiS hands the app a session whose
    role and warehouse are already fixed, and the app never switches either.

    Role override exists because every test up to the packaging work ran as
    ACCOUNTADMIN, which holds far more privilege than the deployed app. That hid a
    missing SNOWFLAKE.CORTEX_USER grant and two missing SELECT grants: order form
    extraction and the Rates tab worked all through development and would have
    failed for the first customer. Running the harness as SF360_APP_ROLE is the
    only way to test what a customer actually experiences.
    """
    out = {}
    role = os.environ.get("SF360_TEST_ROLE", "").strip()
    if role:
        out["role"] = role
    wh = os.environ.get("SF360_TEST_WAREHOUSE", "").strip()
    if wh:
        out["warehouse"] = wh
    return out


def open_connector():
    """A snowflake.connector connection, matching the app's local code path."""
    import snowflake.connector

    return snowflake.connector.connect(
        connection_name=connection_name(), **_overrides()
    )


def open_snowpark():
    """A Snowpark session, which registers itself as the active session.

    Creating it is what makes lib/sf.get_conn() take its ("snowpark", session)
    branch, so the app under test exercises the same code path Streamlit in
    Snowflake uses rather than the local connector path.
    """
    from snowflake.snowpark import Session

    builder = Session.builder.config("connection_name", connection_name())
    for key, value in _overrides().items():
        builder = builder.config(key, value)
    return builder.create()
