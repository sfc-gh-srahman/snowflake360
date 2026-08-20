"""Capture and compare a functional-parity baseline for Snowflake360.

The point of this file is to make "the refactor did not change behaviour" a
provable statement rather than an assertion. It captures three independent
fingerprints and can diff a later capture against an earlier one:

  1. DATA      -- row count and an order-independent content hash for every
                  table, dynamic table and view in the model.
  2. CHECKS    -- the CURATED.VW_VERIFICATION suite, recorded per check rather
                  than as a pass/fail total, so a check that starts passing for
                  a new reason is still visible as a change.
  3. RENDER    -- what each page actually puts on screen, including every
                  metric's label and rendered value string.

The render census is the strongest of the three. Row counts can be identical
while a page shows the wrong number, because most of the ways this app can go
wrong are presentation-layer: a Decimal column that Altair draws as an empty
axis, a currency symbol resolved from the wrong place, a shared axis that makes
a cumulative series read three orders of magnitude off. Metric values are the
numbers a human reads, so they are what parity has to be measured against.

Usage:

    # before changing anything, twice, and confirm the two agree
    python tests/parity.py capture --out tests/baseline
    python tests/parity.py capture --out tests/baseline2
    python tests/parity.py diff tests/baseline tests/baseline2

    # after refactoring
    python tests/parity.py capture --out tests/after
    python tests/parity.py diff tests/baseline tests/after

Requires SNOWFLAKE_DEFAULT_CONNECTION_NAME. Read-only against Snowflake: it
issues SELECTs and renders pages, and never clicks a write control.
"""
from __future__ import annotations

import argparse
import json
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

DB = "SF360"
TIMEOUT = 300

# BUILT_AT is a refresh timestamp that every curated dynamic table projects. It
# legitimately changes on every refresh, so hashing it would make every capture
# differ from every other capture and the whole comparison would be worthless.
# This is also the column that forces those tables to FULL refresh mode, which is
# documented in the curated DDL -- it is load-bearing, not accidental.
VOLATILE_COLUMNS = {"BUILT_AT"}


# ---------------------------------------------------------------------------
# Snowflake side
# ---------------------------------------------------------------------------
def connect():
    from tests._conn import open_connector

    return open_connector()


def fetch(cur, sql: str) -> list[tuple]:
    cur.execute(sql)
    return cur.fetchall()


def object_list(cur) -> list[tuple[str, str, str]]:
    """Every queryable object in the model, with its type."""
    rows = fetch(cur, f"""
        SELECT TABLE_SCHEMA, TABLE_NAME,
               TABLE_TYPE || IFF(IS_DYNAMIC = 'YES', ' (DYNAMIC)', '') AS KIND
        FROM {DB}.INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA <> 'INFORMATION_SCHEMA'
        ORDER BY TABLE_SCHEMA, TABLE_NAME
    """)
    return [(r[0], r[1], r[2]) for r in rows]


def hashable_columns(cur, schema: str, table: str) -> list[str]:
    """Columns to include in the content hash, in a stable order.

    Ordered by name rather than by ordinal position so that adding a column in
    the middle of a table does not reshuffle the hash of the columns either side
    of it. A column added or removed will change the hash, which is correct --
    that IS a schema change -- but the diff then reports it as one table, not as
    every table whose ordinals shifted.
    """
    rows = fetch(cur, f"""
        SELECT COLUMN_NAME
        FROM {DB}.INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = '{schema}' AND TABLE_NAME = '{table}'
        ORDER BY COLUMN_NAME
    """)
    return [r[0] for r in rows if r[0] not in VOLATILE_COLUMNS]


def fingerprint_data(cur) -> dict:
    """Row count plus an order-independent content hash per object."""
    out: dict[str, dict] = {}
    for schema, table, kind in object_list(cur):
        fq = f"{DB}.{schema}.{table}"
        cols = hashable_columns(cur, schema, table)
        entry: dict = {"kind": kind, "columns": cols}
        if not cols:
            entry["error"] = "no hashable columns"
            out[fq] = entry
            continue
        # HASH_AGG is order-independent, so a rebuild that lands rows in a
        # different partition order still hashes the same. That matters because
        # the landing tables are rebuilt with CREATE OR REPLACE every night.
        col_list = ", ".join(f'"{c}"' for c in cols)
        try:
            row = fetch(cur, f"SELECT COUNT(*), HASH_AGG({col_list}) FROM {fq}")[0]
            entry["rows"] = int(row[0])
            # NULL when the table is empty; normalise so it compares cleanly.
            entry["hash"] = str(row[1]) if row[1] is not None else "EMPTY"
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            entry["error"] = str(exc).splitlines()[0][:200]
        out[fq] = entry
    return out


def fingerprint_checks(cur) -> dict:
    """The verification suite, per check."""
    try:
        rows = fetch(cur, f"""
            SELECT CHK, CHECK_NAME, ACTUAL, EXPECTED, RESULT
            FROM {DB}.CURATED.VW_VERIFICATION ORDER BY CHK
        """)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc).splitlines()[0][:200]}
    return {
        str(r[0]): {
            "name": str(r[1]),
            "actual": str(r[2]),
            "expected": str(r[3]),
            "result": str(r[4]),
        }
        for r in rows
    }


# ---------------------------------------------------------------------------
# Streamlit side
# ---------------------------------------------------------------------------
def make_active_session():
    """Register a Snowpark session so pages take the SiS code path.

    Mirrors tests/sis_harness.py deliberately: the census has to be taken on the
    same branch of get_conn() that SiS uses, or it would be a baseline for a code
    path customers never execute.
    """
    from tests._conn import open_snowpark

    return open_snowpark()


def scripts() -> list[Path]:
    return [APP / "Snowflake360.py"] + sorted((APP / "pages").glob("*.py"))


# Element types counted on every page.
#
# "vega_lite_chart" is the one to be careful about. Altair charts are NOT exposed
# as at.altair_chart -- AppTest has no such accessor -- and at.get("altair_chart")
# returns an empty list rather than raising. So a census asking for the intuitive
# name reports charts=0 on a page full of charts and looks like a valid answer.
# Every chart in this app goes through st.altair_chart, which lands in the element
# tree as vega_lite_chart.
CENSUS_ELEMENTS = (
    "metric", "dataframe", "table", "vega_lite_chart", "error", "warning",
    "info", "success", "button", "radio", "selectbox", "text_input",
    "checkbox", "multiselect", "tabs", "markdown", "caption", "expander",
    "download_button", "segmented_control", "number_input", "date_input",
    "subheader", "text",
)

# Element names that at.get() understands but that are not AppTest attributes.
# Anything outside this set must be a real attribute, or the name is a typo that
# would silently count zero forever.
_GET_ONLY_ELEMENTS = {"vega_lite_chart"}


def _validate_census_elements() -> None:
    """Fail loudly on a census element name that would silently count zero."""
    from streamlit.testing.v1.app_test import AppTest

    unknown = [
        name for name in CENSUS_ELEMENTS
        if not hasattr(AppTest, name) and name not in _GET_ONLY_ELEMENTS
    ]
    if unknown:
        raise SystemExit(
            "census element name(s) not resolvable by AppTest: "
            + ", ".join(unknown)
            + "\nThese would report 0 forever instead of raising."
        )


def census(at) -> dict:
    """What this page rendered.

    Element counts catch a page that silently lost a chart. The metric label and
    value pairs catch the more dangerous case: a page that renders exactly the
    same shape while showing a different number.
    """
    def count(name: str) -> int:
        try:
            return len(at.get(name))
        except Exception:
            return -1  # -1, not 0: an unreadable element type is not an absent one

    counts = {name: count(name) for name in CENSUS_ELEMENTS}

    metrics = []
    try:
        for m in at.metric:
            metrics.append({
                "label": str(getattr(m, "label", "")),
                "value": str(getattr(m, "value", "")),
                "delta": str(getattr(m, "delta", "") or ""),
            })
    except Exception:
        pass

    # Error and warning text is captured because the app renders intentional
    # capacity warnings through st.error and st.warning. Their disappearance
    # would be a regression, so the text matters as much as the count.
    def texts(attr: str) -> list[str]:
        try:
            return [str(getattr(e, "value", e))[:300] for e in getattr(at, attr)]
        except Exception:
            return []

    return {
        "counts": counts,
        "metrics": metrics,
        "errors": texts("error"),
        "warnings": texts("warning"),
    }


def fingerprint_render() -> dict:
    from streamlit.testing.v1 import AppTest

    _validate_census_elements()
    out: dict[str, dict] = {}
    for path in scripts():
        try:
            at = AppTest.from_file(str(path), default_timeout=TIMEOUT).run()
        except Exception:
            out[path.name] = {"status": "HARNESS_FAIL",
                              "detail": traceback.format_exc()[-1500:]}
            continue
        entry = census(at)
        entry["status"] = "EXCEPTION" if at.exception else "OK"
        if at.exception:
            entry["exceptions"] = [
                str(getattr(e, "value", None) or getattr(e, "message", "") or e)[:500]
                for e in at.exception
            ]
        out[path.name] = entry
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_capture(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    session = make_active_session()
    from snowflake.snowpark.context import get_active_session
    assert get_active_session() is session, "active session not registered"
    print("active Snowpark session registered -> pages take the SiS branch")

    conn = connect()
    try:
        cur = conn.cursor()
        print("fingerprinting data ...")
        data = fingerprint_data(cur)
        print(f"  {len(data)} objects")
        print("running verification suite ...")
        checks = fingerprint_checks(cur)
        print(f"  {len(checks)} checks")
        cur.close()
    finally:
        conn.close()

    print("taking render census (this runs every page) ...")
    render = fingerprint_render()
    for name, entry in render.items():
        n = entry.get("counts", {})
        print(f"  [{entry['status']:9}] {name:34} "
              f"metrics={n.get('metric', 0):>3} charts={n.get('vega_lite_chart', 0):>3} "
              f"dataframes={n.get('dataframe', 0):>3}")

    (out / "data.json").write_text(json.dumps(data, indent=2, sort_keys=True))
    (out / "checks.json").write_text(json.dumps(checks, indent=2, sort_keys=True))
    (out / "render.json").write_text(json.dumps(render, indent=2, sort_keys=True))
    print(f"\nwrote {out}/data.json, checks.json, render.json")
    return 0


def _diff_section(name: str, a: dict, b: dict, keys: tuple[str, ...]) -> list[str]:
    """Compare two dicts of dicts on selected keys."""
    lines: list[str] = []
    for missing in sorted(set(a) - set(b)):
        lines.append(f"  - {name} REMOVED: {missing}")
    for added in sorted(set(b) - set(a)):
        lines.append(f"  + {name} ADDED:   {added}")
    for k in sorted(set(a) & set(b)):
        for field in keys:
            va, vb = a[k].get(field), b[k].get(field)
            if va != vb:
                lines.append(f"  ~ {name} {k}: {field} {va!r} -> {vb!r}")
    return lines


def cmd_diff(args) -> int:
    before, after = Path(args.before), Path(args.after)
    problems: list[str] = []

    a = json.loads((before / "data.json").read_text())
    b = json.loads((after / "data.json").read_text())
    data_lines = _diff_section("data", a, b, ("rows", "hash", "error"))

    a = json.loads((before / "checks.json").read_text())
    b = json.loads((after / "checks.json").read_text())
    check_lines = _diff_section("check", a, b, ("name", "actual", "expected", "result"))

    a = json.loads((before / "render.json").read_text())
    b = json.loads((after / "render.json").read_text())
    render_lines: list[str] = []
    for missing in sorted(set(a) - set(b)):
        render_lines.append(f"  - page REMOVED: {missing}")
    for added in sorted(set(b) - set(a)):
        render_lines.append(f"  + page ADDED:   {added}")
    for page in sorted(set(a) & set(b)):
        pa, pb = a[page], b[page]
        if pa.get("status") != pb.get("status"):
            render_lines.append(
                f"  ~ {page}: status {pa.get('status')} -> {pb.get('status')}")
        for el in sorted(set(pa.get("counts", {})) | set(pb.get("counts", {}))):
            ca = pa.get("counts", {}).get(el, 0)
            cb = pb.get("counts", {}).get(el, 0)
            if ca != cb:
                render_lines.append(f"  ~ {page}: {el} count {ca} -> {cb}")
        # Metrics compared by label, since a reordering is not a behaviour change
        # but a changed value is the single clearest parity failure there is.
        ma = {m["label"]: m for m in pa.get("metrics", [])}
        mb = {m["label"]: m for m in pb.get("metrics", [])}
        for lbl in sorted(set(ma) - set(mb)):
            render_lines.append(f"  - {page}: metric gone {lbl!r}")
        for lbl in sorted(set(mb) - set(ma)):
            render_lines.append(f"  + {page}: metric new  {lbl!r}")
        for lbl in sorted(set(ma) & set(mb)):
            for field in ("value", "delta"):
                if ma[lbl].get(field) != mb[lbl].get(field):
                    render_lines.append(
                        f"  ~ {page}: metric {lbl!r} {field} "
                        f"{ma[lbl].get(field)!r} -> {mb[lbl].get(field)!r}")

    for title, lines in (("DATA", data_lines), ("CHECKS", check_lines),
                         ("RENDER", render_lines)):
        print(f"{title}: {'no change' if not lines else str(len(lines)) + ' difference(s)'}")
        for line in lines:
            print(line)
        problems += lines

    print("=" * 70)
    if problems:
        print(f"{len(problems)} difference(s) between {before} and {after}")
        print("Each must be an intended change, or it is a regression.")
        return 1
    print(f"identical: {before} and {after} agree on data, checks and render")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture", help="write a fingerprint set")
    c.add_argument("--out", required=True)
    c.set_defaults(func=cmd_capture)
    d = sub.add_parser("diff", help="compare two fingerprint sets")
    d.add_argument("before")
    d.add_argument("after")
    d.set_defaults(func=cmd_diff)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
