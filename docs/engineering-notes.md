# Snowflake360 — engineering notes

Why the code looks the way it does, and the bugs that shaped it. `README.md` explains
how to run the app; this explains what will bite you if you change it.

Every item here was found by something breaking, not by review. They are recorded
because each one cost real time to diagnose and none is obvious from reading the code.

---

## 1. The governing principle: NULL and zero are different answers

Snowflake360 reports on a customer's real spend and ships no sample data. The
consequence is that empty is a normal state, not an error state, and the app must be
able to say *why* a panel is empty.

That gives one rule the whole codebase follows:

- **`0` means "computed, and the answer is zero."**
- **`NULL` means "cannot be computed — say so."**

Conflating them is the single most productive source of bugs in this project. It has
produced a crash, a false all-clear, and a false alarm, in three different layers.
Whenever you add a metric, decide which of the two an absent input should produce, and
make it explicit.

### 1a. `COUNT_IF` returns NULL on an empty table, not 0

This is the one to internalise. `COUNT(*)` on an empty table returns `0`.
`COUNT_IF(predicate)` returns **`NULL`**, because there are no rows for the predicate to
be evaluated against.

It has bitten twice, in two layers, and the second time only because the first sweep was
too narrow:

**In Python** — pandas stores the NULL as `NaN`, and `int(NaN)` raises
`ValueError: cannot convert float NaN to integer`. This crashed the account-scope panel
on any install where `DIM_ACCOUNT` was empty. Fixed by `COALESCE(..., 0)` in the SQL that
feeds it.

**In SQL** — every verification check shaped like this is wrong on an empty table:

```sql
IFF((SELECT COUNT_IF(bad_condition) FROM t) = 0, 'PASS', 'FAIL')
```

An empty `t` means *nothing bad was found*, which should pass. Instead `COUNT_IF` returns
`NULL`, `NULL = 0` evaluates to `NULL`, `IFF` treats that as false, and the check reports
**FAIL** with a blank actual value. Twenty sites in `VW_VERIFICATION` had this. Two of them
(`G2`, `TH1`) were actively reporting failure on a healthy pipeline.

All twenty are now wrapped in `COALESCE(COUNT_IF(...), 0)`. One bare `COUNT_IF` remains,
at `curated.sql` ~line 326, inside a `GROUP BY` — no group is ever empty, so `NULL` is
unreachable there. Leave it.

> **Lesson about the fix, not the bug.** The first time this was found, the audit covered
> the Python call sites and stopped. The identical bug sat in the SQL layer for another
> week. When you find a bug rooted in a *language or platform semantic* rather than a
> typo, the blast radius is every layer that uses that semantic — sweep all of them, and
> grep for the construct, not for the symptom.

### 1b. NaN is truthy in Python

Guards written the obvious way do not catch it:

```python
if not value or value == 0:     # NaN passes straight through:
                                #   not NaN  -> False
                                #   NaN == 0 -> False
```

Use `pd.isna(value)` explicitly, before any `int()` or comparison. Two sites had this;
both are now `pd.isna(...) or not ... or ... == 0`.

---

## 2. Magic sentinels need semantic branches, not arithmetic guards

`ORDERFORM.FN_CADENCE_MONTHS(billing_frequency)` maps an order form's billing cadence to
a period length in months. It returns two non-positive values that mean entirely
different things:

| Return | Meaning | Correct handling |
|---|---|---|
| `0` | **Upfront** — one payment covering the whole term. Legitimate and common, especially on AWS Marketplace order forms. | One period spanning the term |
| `NULL` | Unrecognized cadence | Flag it; cannot verify |

**Neither is a divisor.** This caused two separate bugs, and they are worth contrasting
because the second is the more instructive.

**The crash.** Check (e) in `SP_CHECK_EXTRACTION` evaluated `MOD(MONTHS, CAD)`. An AWS
Marketplace order form says *"Billing Frequency: Upfront and as provided below"*, so
`CAD = 0`, and `MOD(n, 0)` raises `Division by zero`. Extraction died with a nested
`STATEMENT_ERROR` and the user got no contract.

**The false alarm.** Check `BP3` in `VW_VERIFICATION` *already* guarded, with
`CEIL(term / NULLIF(cadence, 0))`. So it never crashed. It computed an expected value of
`NULL`, compared it to an actual of `1`, and — because `1 = NULL` is `NULL`, not true —
reported **FAIL on a pipeline that was producing the right answer.**

That is the important one. The guard prevented the crash and produced a worse outcome: a
verification check confidently reporting breakage that did not exist.

### Why not `DIV0`

`DIV0` / `DIV0NULL` are the obvious reach, and they are wrong here on both counts:

1. **They cannot reach the crash.** `MOD` is not a division operator. `MOD(36, 0)` raises
   regardless of what you do with `/`.
2. **Where they do apply, they launder a bad input into a plausible number.** Chained
   through check (e), `DIV0` yields *"0 installments of 0.00"* for a $252,000 contract.
   A loud crash becomes a quiet lie on a contract page, which is strictly worse in an
   app whose entire value is being trustworthy about money.

The house rule:

| Situation | Tool |
|---|---|
| Zero is the genuinely correct answer | `DIV0` / `DIV0NULL` |
| Cannot be computed, must not show a number | `/ NULLIF(d, 0)` → `NULL` → panel explains itself |
| **The zero divisor is semantically meaningful** | **A real branch. Not a guard.** |

Cadence is the third case, always.

### The convention already existed

The most uncomfortable part: `CONFIG.BILLING_SCHEDULE` had handled this correctly from
the start —

```sql
WHEN CADENCE_MONTHS IS NULL OR CADENCE_MONTHS = 0 THEN 1        -- PERIOD_COUNT
ELSE GREATEST(CEIL(TERM_MONTHS / CADENCE_MONTHS), 1)
```

— which is exactly right: upfront is one period covering the term. Of four consumers of
the function, `config.sql` handled `0`, two guarded with `NULLIF`, and one divided by it.
So this was never a missing design. It was **a convention that existed in one file and was
not propagated to a consumer written later.**

Mitigations now in place, in order of importance:

1. `FN_CADENCE_MONTHS`'s `COMMENT` states the contract explicitly — both sentinels, and
   that neither may be used as a divisor. The function is the only place all callers
   reliably look.
2. `BP3` restates `BILLING_SCHEDULE`'s rule *exactly*, including the upfront arm, so the
   check cannot drift from the implementation it verifies.
3. `CASE` arms test `CAD = 0` **before** any arithmetic. `CASE` short-circuits in
   Snowflake (verified), and `MOD` additionally carries a `NULLIF` in case a future query
   plan evaluates more eagerly than the current one.

> **Generalisation.** Any function returning a magic value that is *numerically valid but
> semantically special* creates an obligation on every caller. If you add one, document it
> on the function and add a verification check that exercises it — otherwise the fourth
> caller breaks it.

---

## 3. A verification check that cannot represent a legitimate input is not protecting anything

`BP3` is the cautionary example: a check whose expected-value expression could not
represent an upfront contract, so it manufactured an alarm instead of catching a fault.

When adding a check, ask what it does on:

- an empty source table (see §1a),
- a legitimate but unusual input — upfront billing, expired term, single account,
- a term that has not started, or has already ended.

`CHK 18 BP5 one current period` is the model of doing this right. It reports `0` and
`WARN` for a contract whose term has ended, which is honest: there really is no current
period. It does not crash, and it does not claim breakage.

---

## 4. Deployment: code lives in three places, not two

Git-based deployment means the customer needs no local tooling — Snowflake fetches the
repo itself:

```
CREATE API INTEGRATION  -> CREATE GIT REPOSITORY
                        -> EXECUTE IMMEDIATE FROM '@repo/.../file.sql'
                        -> CREATE STREAMLIT ... FROM '@repo/.../streamlit/'
```

The trap is that code then exists in **three** places, and they drift independently:

1. your local working copy,
2. GitHub,
3. **the Snowflake git repository object**, which is a cached snapshot and does *not*
   update when you push.

`ALTER GIT REPOSITORY <name> FETCH` is required. This was missed once: SQL objects on an
account were patched directly from local files and the repo was pushed, so both of those
agreed — but the Snowflake git stage sat two commits behind, and anyone re-running
`EXECUTE IMMEDIATE FROM` against it would have pulled the pre-fix SQL. Check
`SHOW GIT BRANCHES IN <repo>` and compare `commit_hash` to `git rev-parse origin/main`.

Also: `raw.githubusercontent.com` caches for roughly five minutes. A `curl` of a file you
just pushed can legitimately return the old content. Verify against
`git ls-remote origin main`, not the raw URL, when confirming a push landed.

### 4a. `EXECUTE IMMEDIATE FROM` returns only the last statement's status

A file can be half-applied and still report success. Do not treat the returned row as
proof the whole file ran; verify the objects.

### 4b. Bare `DECLARE ... END` blocks break clients that split on semicolons

Snowsight's *Run All* parses anonymous blocks correctly. Clients that split a script on
`;` — `snow sql -f`, most JDBC batch runners, most CI steps — send
`DECLARE x BOOLEAN DEFAULT FALSE;` as a statement on its own and fail with
`syntax error ... unexpected '<EOF>'`.

The failure shape is the danger. In `setup.sql` the earlier sections had already created
the database, so the account was left with the full model built, `MODE` unset, no
Streamlit, no data loaded and the DAG suspended — **a half-install that looks like a
successful one until someone opens the app.**

Wrap anonymous blocks in `EXECUTE IMMEDIATE $$ ... $$`. That is a single statement to a
naive splitter and still valid to Snowsight, so one file works on both paths.

And **keep dollar-quote delimiters out of comments.** A comment that quotes `$$`
literally puts a stray pair in the file. They happened to be balanced, so a splitter
toggling on each pair landed correctly by luck — but delimiters are exactly what a naive
splitter tracks to find statement boundaries. The stray pair also captured a regex aimed
at the real block during testing, which is the same mistake a client parser makes.

### 4c. Test the install on more than one client

The two paths parse differently, so passing on one proves little about the other:

- Snowsight worksheet paste + Run All (the primary documented path)
- `snow sql -f` (a different splitter)
- `connector.execute_string()` (a third)

Installing via the CLI is the more hostile test and found the bug above. Snowsight is what
customers actually use, so it needs its own confirmation — being green on the CLI is not
transitive.

---

## 5. Ordering rules that bite

**`GET_DDL` emits objects alphabetically, not in dependency order.** Baseline files
scripted from a live account therefore *have never run from zero*, even though they look
complete. `curated.sql` and `tasks.sql` both had to be hand-reordered:

- `curated.sql` → dependency order, matching `SP_REFRESH_CURATED`
  (`FCT_CONTRACT_POSITION` came before `FCT_DAILY_CURRENCY` alphabetically, and failed).
- `tasks.sql` → parent-first, fully qualified names, with `USE SCHEMA LANDING`
  (alphabetical order put children before parents → `091085 Invalid predecessor`).

**`CREATE OR REPLACE SCHEMA` drops everything inside it** — including the task DAG and
its history. Use `CREATE SCHEMA IF NOT EXISTS` in any file that might be re-run.

**A task graph cannot be modified while its root is resumed** (`091421`). Suspend the
root, change, resume. Teardown suspends root-first and drops children-first, for the same
reason.

**Cold-install proof requires an actual cold install.** Dropping and rebuilding in place
is good; a second, unrelated account is better. Several of these ordering bugs were
invisible until the app was installed somewhere it had never run.

---

## 6. Account-shape detection

`MODE` in `CONFIG.SETTINGS` is `ORG` or `ACCOUNT`, probed at install by querying
`ORGANIZATION_USAGE`.

**The probe tests whether the views are *readable*, not whether they have rows** — and
those come apart on a new organization account, where the views grant fine but stay empty
until Snowflake's billing pipeline populates them, typically 24–48 hours after first use.

`MODE = 'ORG'` is the correct answer there. The account genuinely is an organization
account and the org pages fill in on their own. Setting `ACCOUNT` on a row count of zero
would permanently understate what the account can do. What *was* wrong was the messaging:
"unavailable" and "not populated yet" read identically. The probe now distinguishes them,
because they call for different responses — one is a limitation, the other is patience.

This is also why `RATE_SHEET_DAILY` legitimately shows an explanatory banner on a trial
account: it is an `ORGANIZATION_USAGE` view with no data yet, not a broken panel.

Other account-shape gotchas:

- **`CORTEX_ENABLED_CROSS_REGION`** is needed outside Cortex-native regions. Order form
  extraction runs inside a task, so an unavailable model surfaces as an **empty
  `ORDERFORM.EXTRACTED` with no error** — the worst failure shape. Left commented in
  `setup.sql` because it moves inference payloads out of region, which should be a
  deliberate choice.
- **`USE ROLE` fails under a session policy** that blocks role switching
  (`003107: Current session is restricted`). Pass the role as a connect-time parameter, or
  select it from the worksheet role picker. Noted inline in both scripts.
- **`ALTER SESSION` is rejected in the SiS stored-procedure sandbox.** `lib/sf.py` probes
  once and falls back to carrying the query tag as a SQL comment.
- **Account scope defaults to everything.** With `CONFIG.ACCOUNT_SCOPE` empty, every
  account in the org is in scope — on a large org that is thousands, which makes `G7` warn
  loudly and the org pages unwieldy. Narrow scope on Setup & Settings before demoing.

---

## 7. Testing

| Harness | Catches |
|---|---|
| `tests/verification.py` | 23 SQL assertions against silently-wrong rollups |
| `tests/sis_harness.py` | Renders all 9 pages through a real Snowpark session — the code path SiS uses, where SiS-only bugs hide |
| `tests/parity.py` | Object hashes, check results, and every page's rendered metric values |
| `tools/audit_grants.py` | Least-privilege drift |

`sis_harness.py` earns its keep: `lib/sf.get_conn()` branches on whether an active
Snowpark session exists, and a normal local `streamlit run` never touches the Snowpark
branch at all. Creating a `Session` before importing the app forces that branch.

### The test-corpus gap worth remembering

Order form extraction was validated against several documents — all of them **direct**
Snowflake order forms, billed Monthly, Quarterly or Annually. AWS Marketplace order forms
bill *upfront*, and that entire document class was absent from the corpus. It crashed on
first contact.

Coverage is not just "how many documents" but **how many document *shapes***. For a
feature that parses third-party documents, enumerate the variants that exist in the world
— marketplace vs direct, upfront vs recurring, multi-currency, amendments — and get one of
each before claiming the feature works.

Most verification checks depend on a contract existing, so a fresh install with no
contract legitimately shows roughly 9 of 23 passing. That is configuration, not breakage.
Confirm *which* checks fail before concluding anything: read them, do not assume.

---

## 8. Repository layout convention

`sql/baseline/` holds the deployable definitions and is what `setup.sql` runs. The
numbered `sql/40..49_*.sql` files hold the reasoning, because `GET_DDL` strips comments.

**They must be fixed together.** The numbered files are documented as the record of *why*;
if they disagree with `baseline/` about intent, the record is actively misleading. Every
fix in this document was applied to both.
