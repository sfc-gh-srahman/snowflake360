/*******************************************************************************
 * SNOWFLAKE360 -- COMPLETE SETUP
 *
 * Contract and capacity intelligence for your own Snowflake account or
 * organization, built entirely on data every Snowflake account already has.
 *
 * HOW TO RUN
 *   1. Open a Snowsight SQL worksheet.
 *   2. Paste this entire file.
 *   3. Run All, as ACCOUNTADMIN.
 *   4. Open Projects > Streamlit > Snowflake360.
 *
 * Takes roughly 5-10 minutes, most of it the first data load.
 *
 * WHAT IT CREATES
 *   Database SF360, 5 schemas, ~47 objects
 *   Warehouse SF360_WH (STANDARD, X-Small) -- all app compute, so the cost of
 *     running Snowflake360 is one attributable line in your billing
 *   Role SF360_APP_ROLE, least privilege
 *   A 6-task DAG that refreshes daily at 11:00 UTC
 *   A Streamlit app, pulled straight from GitHub by Snowflake
 *
 * WHAT IT DOES NOT DO
 *   It ships no data. Every figure comes from your own
 *   SNOWFLAKE.ACCOUNT_USAGE and SNOWFLAKE.ORGANIZATION_USAGE. Nothing leaves
 *   your account, and there is nothing to seed or refresh from outside.
 *
 *   A consequence worth knowing before you run it: on a brand new account there
 *   is very little usage history, so the app will be honest and mostly empty.
 *   Section 4 below measures your history and tells you what to expect.
 *
 * SAFE TO RE-RUN
 *   Yes, and this is the upgrade path. Your contract, negotiated rates, alert
 *   thresholds, account scope and uploaded order forms are all created with
 *   CREATE ... IF NOT EXISTS and are never overwritten. Derived data in LANDING
 *   and CURATED is rebuilt.
 *
 * TO REMOVE EVERYTHING
 *   Run scripts/teardown.sql.
 ******************************************************************************/

USE ROLE ACCOUNTADMIN;

-- If this line errors with "003107: Current session is restricted", your account
-- has a session policy that blocks role switching. Select ACCOUNTADMIN from the
-- role picker in the worksheet header instead, and delete this line.

ALTER SESSION SET QUERY_TAG = '{"origin":"sf_sit","name":"snowflake360","version":{"major":1,"minor":0},"attributes":{"is_quickstart":0,"source":"sql"}}';

-- Docs mandate UTC when reconciling ACCOUNT_USAGE against ORGANIZATION_USAGE.
ALTER SESSION SET TIMEZONE = 'UTC';

-- OPTIONAL -- only if your account is NOT in a Cortex-native region.
--
-- Order form extraction calls AI_PARSE_DOCUMENT and AI_EXTRACT. In a region where
-- those models are not hosted, the calls fail rather than routing themselves, and
-- because the extraction runs inside a task you will see an empty EXTRACTED table
-- with no obvious error. Allowing cross-region inference avoids that.
--
-- This sends inference payloads outside your region, so it is left commented for
-- you to make that call deliberately. Nothing else in Snowflake360 needs Cortex --
-- entering the contract by hand is the primary documented path either way.
--
-- ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'ANY_REGION';

SET SF360_USER = (SELECT CURRENT_USER());

/*******************************************************************************
 * SECTION 1 -- EDIT THIS IF YOU FORKED THE REPOSITORY
 *
 * Snowflake fetches the application code from GitHub itself, which is why you do
 * not need the Snowflake CLI, Docker, or anything installed locally. If you
 * forked the repo, change the owner in BOTH places below.
 ******************************************************************************/

CREATE OR REPLACE API INTEGRATION SF360_GIT_API
  API_PROVIDER = git_https_api
  API_ALLOWED_PREFIXES = ('https://github.com/sfc-gh-srahman/')   -- <-- fork: change owner
  ENABLED = TRUE
  COMMENT = 'Snowflake360: read-only access to the public GitHub repository holding the app and its DDL.';

/*******************************************************************************
 * SECTION 2 -- DATABASE, ROLE, AND THE REPOSITORY OBJECT
 ******************************************************************************/

CREATE DATABASE IF NOT EXISTS SF360
  COMMENT = 'Snowflake360: contract and capacity intelligence over ACCOUNT_USAGE and ORGANIZATION_USAGE.';

-- APP has to exist before the git repository object, which lives inside it.
CREATE SCHEMA IF NOT EXISTS SF360.APP
  COMMENT = 'Streamlit app objects and self-instrumentation telemetry';

-- The role the Streamlit app runs as. Privileges are granted in section 3 by
-- sql/baseline/grants.sql, which is deliberately least privilege: read across
-- SF360, write only to the schemas holding customer-entered data, and no CREATE
-- privilege anywhere.
CREATE ROLE IF NOT EXISTS SF360_APP_ROLE
  COMMENT = 'Snowflake360 application role. Reads usage data, writes only customer configuration.';

GRANT ROLE SF360_APP_ROLE TO USER IDENTIFIER($SF360_USER);

CREATE OR REPLACE GIT REPOSITORY SF360.APP.SF360_REPO
  API_INTEGRATION = SF360_GIT_API
  ORIGIN = 'https://github.com/sfc-gh-srahman/snowflake360.git'   -- <-- fork: change owner
  COMMENT = 'Snowflake360 source: Streamlit app and all DDL.';

ALTER GIT REPOSITORY SF360.APP.SF360_REPO FETCH;

/*******************************************************************************
 * SECTION 3 -- BUILD THE MODEL
 *
 * Each file is run from the repository rather than pasted inline, so this script
 * stays short enough to read and every object definition stays in a reviewable
 * per-schema file with the reasoning intact. Order is dependency order.
 ******************************************************************************/

-- Order below is dependency order, established by actually installing from zero.
-- It is not the order the files were originally scripted in, and three of these
-- steps fail if moved:
--
--   * orderform.sql must precede config.sql, because CONFIG.BILLING_SCHEDULE is a
--     view over ORDERFORM.FN_CADENCE_MONTHS.
--   * config_seed.sql must precede warehouse.sql and landing.sql, because
--     SP_REBUILD_LANDING reads RETENTION_DAYS out of CONFIG.SETTINGS.
--   * tasks.sql must come last of the DDL, because every task's predecessor has to
--     exist before it does.

-- Order form ingestion: stage, tables, the cadence function, and the AI extraction
-- procedures. First because CONFIG's billing schedule view depends on the function.
EXECUTE IMMEDIATE FROM '@SF360.APP.SF360_REPO/branches/main/sql/baseline/orderform.sql';

-- The 18 extraction prompts. Several are column-qualified on purpose: an order
-- form states capacity and On Demand fees in adjacent columns, so an unqualified
-- prompt returns the wrong one while looking entirely plausible.
EXECUTE IMMEDIATE FROM '@SF360.APP.SF360_REPO/branches/main/sql/baseline/orderform_seed.sql';

-- Customer-owned configuration. CREATE ... IF NOT EXISTS throughout, so re-running
-- this script never resets a contract that has already been entered.
EXECUTE IMMEDIATE FROM '@SF360.APP.SF360_REPO/branches/main/sql/baseline/config.sql';

-- Default settings, including the retention window the landing rebuild depends on.
EXECUTE IMMEDIATE FROM '@SF360.APP.SF360_REPO/branches/main/sql/baseline/config_seed.sql';

-- Dedicated warehouse: everything from here runs on it.
EXECUTE IMMEDIATE FROM '@SF360.APP.SF360_REPO/branches/main/sql/baseline/warehouse.sql';

USE WAREHOUSE SF360_WH;

-- Landing: the only consumer of SNOWFLAKE.ACCOUNT_USAGE / ORGANIZATION_USAGE.
-- Rebuilt in full nightly, because those views back-fill late-arriving rows and a
-- trailing-window incremental load would silently miss them.
EXECUTE IMMEDIATE FROM '@SF360.APP.SF360_REPO/branches/main/sql/baseline/landing.sql';

-- Curated: 12 dynamic tables and 4 views, in dependency order. All FULL refresh,
-- which is forced rather than chosen -- every one projects CURRENT_TIMESTAMP() AS
-- BUILT_AT, and a timestamp function in a SELECT list is not incremental-safe.
EXECUTE IMMEDIATE FROM '@SF360.APP.SF360_REPO/branches/main/sql/baseline/curated.sql';

-- App telemetry table and the query-tag harvester.
EXECUTE IMMEDIATE FROM '@SF360.APP.SF360_REPO/branches/main/sql/baseline/app.sql';

-- The refresh DAG, parents before children. Created suspended; section 7 resumes it.
EXECUTE IMMEDIATE FROM '@SF360.APP.SF360_REPO/branches/main/sql/baseline/tasks.sql';

-- Least-privilege grants, including the nine SNOWFLAKE.*_VIEWER database roles
-- that are how the app reads usage data at all.
EXECUTE IMMEDIATE FROM '@SF360.APP.SF360_REPO/branches/main/sql/baseline/grants.sql';

USE ROLE ACCOUNTADMIN;
USE WAREHOUSE SF360_WH;

/*******************************************************************************
 * SECTION 4 -- MEASURE THIS ACCOUNT, THEN CONFIGURE ACCORDINGLY
 *
 * Two things differ per account and both change what the app can show. Detecting
 * them and writing the answer into CONFIG.SETTINGS is better than assuming, and
 * far better than letting pages render empty with no explanation.
 ******************************************************************************/

-- ORGANIZATION_USAGE exists only in an organization account. Without it the app
-- reports on this account alone, which is a smaller but entirely valid picture.
--
-- Note what is being tested: whether the view is READABLE, not whether it has rows.
-- Those come apart on a new organization account, where the views exist and grant
-- fine but return nothing until Snowflake's billing pipeline has populated them --
-- typically 24-48 hours after first use. MODE = 'ORG' is the correct answer there:
-- the account genuinely is an organization account, and the org pages will fill in
-- on their own. Until they do, those pages explain that they are empty. Setting
-- MODE = 'ACCOUNT' on a row count of zero would be wrong, and would leave the app
-- permanently understating what the account can do.
-- Wrapped in EXECUTE IMMEDIATE with a dollar-quoted body rather than written as a
-- bare DECLARE ... END block, so this file survives being fed to a client that
-- splits on semicolons -- `snow sql -f`, most JDBC batch runners, most CI steps.
-- Those would otherwise send `DECLARE org_ok BOOLEAN DEFAULT FALSE;` on its own
-- and fail with "syntax error ... unexpected '<EOF>'" here, taking sections 5
-- through 8 down with it. Snowsight's Run All handles either form; only this one
-- handles both.
--
-- Deliberately no dollar-quote delimiters in this comment: they are what a naive
-- splitter tracks to find statement boundaries, and a stray pair in a comment is
-- ambiguity for no gain in exactly the file that most needs none.
EXECUTE IMMEDIATE $$
DECLARE
  org_ok    BOOLEAN DEFAULT FALSE;
  org_rows  INTEGER DEFAULT 0;
  mode      VARCHAR;
BEGIN
  BEGIN
    org_rows := (
      SELECT COUNT(*) FROM SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY
       WHERE USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE())
    );
    org_ok := TRUE;
  EXCEPTION
    WHEN OTHER THEN
      org_ok := FALSE;
  END;

  mode := IFF(org_ok, 'ORG', 'ACCOUNT');

  MERGE INTO SF360.CONFIG.SETTINGS t
  USING (SELECT 'MODE' AS K, :mode AS V) s
     ON t.SETTING_KEY = s.K
  WHEN MATCHED THEN UPDATE SET SETTING_VALUE = s.V
  WHEN NOT MATCHED THEN INSERT (SETTING_KEY, SETTING_VALUE) VALUES (s.K, s.V);

  RETURN 'MODE set to ' || mode
         || IFF(NOT org_ok,
                ' -- ORGANIZATION_USAGE is not reachable from this account, so the app reports on this account only. Org-scoped panels will say so rather than showing zeros.',
                IFF(org_rows > 0,
                    ' -- organization-wide reporting is available (' || org_rows || ' rows of org usage in the last 7 days).',
                    ' -- this IS an organization account, but ORGANIZATION_USAGE returned 0 rows for the last 7 days, so org-wide and currency pages will be empty for now. That is normal on a new organization account; Snowflake populates these views 24-48 hours after first use. Nothing to fix -- re-check tomorrow.'));
END
$$;

/*******************************************************************************
 * SECTION 5 -- DEPLOY THE APP FROM GIT
 ******************************************************************************/

CREATE OR REPLACE STREAMLIT SF360.APP.SNOWFLAKE360
  FROM '@SF360.APP.SF360_REPO/branches/main/streamlit/'
  MAIN_FILE = 'Snowflake360.py'
  QUERY_WAREHOUSE = 'SF360_WH'
  TITLE = 'Snowflake360'
  COMMENT = 'Contract and capacity intelligence over your own ACCOUNT_USAGE and ORGANIZATION_USAGE.';

ALTER STREAMLIT SF360.APP.SNOWFLAKE360 ADD LIVE VERSION FROM LAST;

GRANT USAGE ON STREAMLIT SF360.APP.SNOWFLAKE360 TO ROLE SF360_APP_ROLE;

/*******************************************************************************
 * SECTION 6 -- FIRST DATA LOAD
 *
 * Run now rather than waiting for 11:00 UTC, so the app has something to show the
 * first time it is opened. This is the slow part of the script.
 ******************************************************************************/

CALL SF360.LANDING.SP_REBUILD_LANDING();
CALL SF360.LANDING.SP_REBUILD_LANDING_ACCOUNT();
CALL SF360.LANDING.SP_REBUILD_LANDING_ACCOUNT2();
CALL SF360.LANDING.SP_REBUILD_LANDING_AI();
CALL SF360.CURATED.SP_REFRESH_CURATED();

/*******************************************************************************
 * SECTION 7 -- START THE SCHEDULE
 *
 * Children first, root last: a child cannot be resumed while its predecessor is
 * suspended, and resuming the root is what arms the schedule.
 ******************************************************************************/

ALTER TASK SF360.LANDING.TSK_SF360_TELEMETRY        RESUME;
ALTER TASK SF360.LANDING.TSK_SF360_CURATED          RESUME;
ALTER TASK SF360.LANDING.TSK_SF360_LANDING_AI       RESUME;
ALTER TASK SF360.LANDING.TSK_SF360_LANDING_ACCOUNT2 RESUME;
ALTER TASK SF360.LANDING.TSK_SF360_LANDING_ACCOUNT  RESUME;
ALTER TASK SF360.LANDING.TSK_SF360_ROOT             RESUME;

/*******************************************************************************
 * SECTION 8 -- WHAT YOU GOT
 *
 * Reports what was built, how much history it found, and whether the internal
 * consistency checks pass. A verification failure here is usually configuration
 * rather than breakage: most checks depend on a contract existing, and no contract
 * exists until you enter one on the Setup & Settings page.
 ******************************************************************************/

SELECT
  (SELECT COUNT(*) FROM SF360.INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA <> 'INFORMATION_SCHEMA')                    AS OBJECTS_BUILT,
  (SELECT SETTING_VALUE FROM SF360.CONFIG.SETTINGS
    WHERE SETTING_KEY = 'MODE')                                    AS REPORTING_MODE,
  (SELECT COUNT(DISTINCT USAGE_DATE_UTC) FROM SF360.LANDING.LND_METERING_DAILY) AS DAYS_OF_HISTORY,
  (SELECT COUNT(*) FROM SF360.CURATED.VW_VERIFICATION WHERE RESULT = 'PASS')
    || ' of '
    || (SELECT COUNT(*) FROM SF360.CURATED.VW_VERIFICATION)        AS CHECKS_PASSING,
  (SELECT COUNT(*) FROM SF360.CONFIG.CONTRACT WHERE IS_ACTIVE)     AS ACTIVE_CONTRACTS,
  'Open Projects > Streamlit > Snowflake360. Start on the Setup & Settings page: nothing downstream is meaningful until a contract exists.' AS NEXT_STEP;
