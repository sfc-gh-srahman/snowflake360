"""Run the Snowflake360 verification suite from a laptop.

The 23 checks used to be a tab inside the app. They were pulled out because they
are a build-time concern, not a customer-facing one: each check guards a specific
way an org-wide rollup can report a silently wrong number, and a red FAIL in a
shipped app reads as "this product is broken" even when the real cause is a
config value the customer has not set yet.

The view itself still lives in the database, so this is a thin client over it.

    SNOWFLAKE_DEFAULT_CONNECTION_NAME=my_snowflake .venv/bin/python tests/verification.py

Exits non-zero if any check fails, so it can gate a deploy.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The Streamlit app lives in streamlit/ so that CREATE STREAMLIT ... FROM a git
# repository can point at one directory containing the entry script, pages/ and
# lib/. Both paths are needed: ROOT to import tests._conn, APP to resolve the
# app's own "from lib.sf import ..." exactly as it resolves inside SiS.
APP = ROOT / "streamlit"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(APP))

from lib.sf import DB  # noqa: E402
from tests._conn import open_connector  # noqa: E402

VIEW = f"{DB}.CURATED.VW_VERIFICATION"


def main() -> int:
    conn = open_connector()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT CHK, CHECK_NAME, ACTUAL, EXPECTED, RESULT FROM {VIEW} ORDER BY CHK"
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    if not rows:
        print(f"no rows from {VIEW} -- is the database built?")
        return 1

    failed = [r for r in rows if r[4] != "PASS"]
    width = max(len(str(r[1])) for r in rows)
    for chk, name_, actual, expected, result in rows:
        mark = "ok  " if result == "PASS" else "FAIL"
        line = f"[{mark}] {chk:>3}  {str(name_):<{width}}"
        if result != "PASS":
            line += f"   actual={actual!r} expected={expected!r}"
        print(line)

    print("-" * 60)
    print(f"{len(rows) - len(failed)}/{len(rows)} passing")
    if failed:
        print("FAILING: " + ", ".join(f"{r[0]} {r[1]}" for r in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
