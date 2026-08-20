"""Audit the privileges actually held by SF360_APP_ROLE.

This used to generate sql/baseline/grants.sql from SHOW GRANTS TO ROLE. That is no
longer appropriate: grants.sql is now hand-written and authoritative, expressed as
ON ALL plus ON FUTURE rather than as one statement per object. Regenerating it from
the account would flatten those rules back into a snapshot and silently drop the
FUTURE half, which is the half that keeps newly added objects visible to the app.

So this reports instead of writing. It answers two questions:

  1. What does the role actually hold, grouped by object type?
  2. Does it hold anything it should not -- specifically CREATE privileges on a
     schema, which is what a GRANT ALL leaves behind.

The second check exists because a dev-time GRANT ALL ON SCHEMA once left this role
able to create masking policies, network rules, secrets and authentication
policies across four schemas. It went unnoticed because everything was tested as
ACCOUNTADMIN. Run this after any grant change.

    SNOWFLAKE_DEFAULT_CONNECTION_NAME=<conn> .venv/bin/python tools/audit_grants.py

Exits non-zero if an over-privileged grant is found, so it can gate a release.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests._conn import open_connector  # noqa: E402

ROLE = "SF360_APP_ROLE"

# A privilege on a SCHEMA that this app has no use for. The app creates no
# objects, so any CREATE here is residue from a GRANT ALL.
FORBIDDEN_SCHEMA_PRIVILEGE_PREFIXES = ("CREATE ",)

# Schema privileges that are legitimate.
ALLOWED_SCHEMA_PRIVILEGES = {"USAGE"}


def main() -> int:
    conn = open_connector()
    try:
        cur = conn.cursor()
        cur.execute(f"SHOW GRANTS TO ROLE {ROLE}")
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        cur.close()
    finally:
        conn.close()

    i_priv = cols.index("privilege")
    i_on = cols.index("granted_on")
    i_name = cols.index("name")

    by_type: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for r in rows:
        by_type[r[i_on]].append((r[i_priv], r[i_name]))

    print(f"{ROLE}: {len(rows)} grants\n")
    for kind in sorted(by_type):
        items = by_type[kind]
        privs = Counter(p for p, _ in items)
        print(f"  {kind:15} {len(items):>4}  ({', '.join(f'{p} x{n}' for p, n in sorted(privs.items()))})")

    print("\nDatabase roles (how the app reads Snowflake's own usage data):")
    for priv, name in sorted(by_type.get("DATABASE_ROLE", [])):
        print(f"  {name}")

    # Over-privilege check
    problems = []
    for priv, name in by_type.get("SCHEMA", []):
        if priv in ALLOWED_SCHEMA_PRIVILEGES:
            continue
        if any(priv.startswith(p) for p in FORBIDDEN_SCHEMA_PRIVILEGE_PREFIXES):
            problems.append(f"{priv} on SCHEMA {name}")
        else:
            problems.append(f"{priv} on SCHEMA {name} (not in the allowed set)")

    print()
    if problems:
        print(f"OVER-PRIVILEGED: {len(problems)} schema grant(s) this app does not need")
        for p in problems[:40]:
            print(f"  {p}")
        if len(problems) > 40:
            print(f"  ... and {len(problems) - 40} more")
        print("\nRe-run sql/baseline/grants.sql after revoking:")
        print("  REVOKE ALL PRIVILEGES ON SCHEMA <schema> FROM ROLE " + ROLE + ";")
        return 1

    print("no over-privileged schema grants: the role holds USAGE only, as intended")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
