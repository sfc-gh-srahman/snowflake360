"""Headless test harness that exercises Snowflake360 the way SiS runs it.

Streamlit in Snowflake reaches Snowflake through an *active Snowpark session*, not
through connections.toml. lib/sf.get_conn() branches on that, so the entire
snowpark code path -- handle.sql(...).to_pandas(), ALTER SESSION through Snowpark,
file.put_stream -- is never touched by a normal local `streamlit run`. That branch
is precisely where a SiS-only bug can hide.

This harness closes that gap without needing a browser:

  1. It creates a real Snowpark session before importing the app. Creating a
     Session registers it as the active one, so get_active_session() starts
     succeeding and get_conn() takes the ("snowpark", session) branch exactly as
     it would inside SiS.
  2. It runs each page script through streamlit.testing.v1.AppTest, which
     executes the whole script top to bottom in-process and collects any
     exception into at.exception rather than painting it into a browser DOM.

What it does NOT prove: the SiS runtime's Streamlit version. AppTest runs against
whatever Streamlit is installed locally, so version-gated APIs still need either a
real SiS load or the defensive fallbacks in lib/style.py.

Read-only by construction: it renders scripts and reads widget state. It never
clicks a Save/Accept/Upload/Refresh control, so no configuration is written.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The Streamlit app lives in streamlit/ so that CREATE STREAMLIT ... FROM a git
# repository can point at one directory containing the entry script, pages/ and
# lib/. Both paths are needed: ROOT to import tests._conn, APP to resolve the
# app's own "from lib.sf import ..." exactly as it resolves inside SiS.
APP = ROOT / "streamlit"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(APP))

TIMEOUT = 180


def make_active_session():
    """Create a Snowpark session so the app takes its SiS branch."""
    from tests._conn import open_snowpark

    return open_snowpark()


def scripts() -> list[Path]:
    return [APP / "Snowflake360.py"] + sorted((APP / "pages").glob("*.py"))


def describe(at) -> str:
    """Count what actually rendered, so a silently empty page is visible.

    Charts are counted as "vega_lite_chart". They are NOT available as
    at.altair_chart -- AppTest has no such accessor -- and at.get("altair_chart")
    returns an empty list rather than raising, so asking for the intuitive name
    reported charts=0 on a page full of charts and looked like a real answer.
    """
    def n(attr):
        try:
            return len(at.get(attr))
        except Exception:
            return -1
    return (
        f"metrics={n('metric')} charts={n('vega_lite_chart')} "
        f"dataframes={n('dataframe')} "
        f"tables={n('table')} errors={n('error')} warnings={n('warning')} "
        f"infos={n('info')} buttons={n('button')} radios={n('radio')} "
        f"tabs={n('tabs')} markdown={n('markdown')}"
    )


def run_one(path: Path) -> tuple[str, str, str]:
    from streamlit.testing.v1 import AppTest

    try:
        at = AppTest.from_file(str(path), default_timeout=TIMEOUT).run()
    except Exception:
        return "FAIL", "harness could not run script", traceback.format_exc()

    if at.exception:
        texts = []
        for e in at.exception:
            msg = getattr(e, "value", None) or getattr(e, "message", "") or str(e)
            stack = getattr(e, "stack_trace", None)
            if stack:
                msg = f"{msg}\n" + "\n".join(
                    stack if isinstance(stack, list) else [str(stack)]
                )
            texts.append(str(msg))
        return "FAIL", describe(at), "\n---\n".join(texts)
    return "PASS", describe(at), ""


def main() -> int:
    session = make_active_session()
    from snowflake.snowpark.context import get_active_session

    assert get_active_session() is session, "active session not registered"
    print("active Snowpark session registered -> app will take the SiS branch")
    row = session.sql(
        "SELECT CURRENT_WAREHOUSE(), CURRENT_ROLE(), CURRENT_ACCOUNT_NAME()"
    ).collect()[0]
    print(f"warehouse={row[0]} role={row[1]} account={row[2]}\n")

    failures = []
    for path in scripts():
        status, detail, err = run_one(path)
        print(f"[{status}] {path.name}")
        print(f"        {detail}")
        if err:
            failures.append((path.name, err))
            print("        " + err.replace("\n", "\n        ")[:2000])
        print()

    print("=" * 70)
    if failures:
        print(f"{len(failures)} script(s) FAILED: " + ", ".join(n for n, _ in failures))
        return 1
    print(f"all {len(scripts())} scripts rendered with no exception on the SiS path")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
