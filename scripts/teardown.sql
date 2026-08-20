/*******************************************************************************
 * SNOWFLAKE360 -- TEARDOWN
 *
 * Removes everything scripts/setup.sql created.
 *
 * READ THIS FIRST
 *   This deletes data you cannot get back from Snowflake. Specifically:
 *
 *     - the contract you entered or accepted from an order form
 *     - your negotiated per-credit and per-TB rates
 *     - any alert thresholds you set
 *     - your account scope selections
 *     - the order form PDFs you uploaded, and the extraction results
 *
 *   Everything else is derived from SNOWFLAKE.ACCOUNT_USAGE and would rebuild on
 *   the next setup run, so it does not matter. The list above does, because none
 *   of it exists anywhere else.
 *
 *   Section 0 shows you how to keep it. Do that before running section 1 unless
 *   you are certain you want it gone.
 *
 * Run as ACCOUNTADMIN.
 ******************************************************************************/

USE ROLE ACCOUNTADMIN;

/*******************************************************************************
 * SECTION 0 -- OPTIONAL: KEEP YOUR CONFIGURATION FIRST
 *
 * Uncomment and run this section to clone the customer-owned tables into a
 * database that teardown will not touch. Cloning is metadata-only, so it is
 * effectively free and instant.
 *
 * The uploaded PDFs live on an internal stage and cannot be cloned. If you want
 * them, download them first:
 *
 *     GET @SF360.ORDERFORM.ORDER_FORMS file:///tmp/sf360_order_forms/;
 ******************************************************************************/

-- CREATE DATABASE IF NOT EXISTS SF360_KEEP
--   COMMENT = 'Snowflake360 configuration preserved before teardown.';
-- CREATE SCHEMA IF NOT EXISTS SF360_KEEP.CONFIG;
-- CREATE SCHEMA IF NOT EXISTS SF360_KEEP.ORDERFORM;
--
-- CREATE TABLE SF360_KEEP.CONFIG.CONTRACT         CLONE SF360.CONFIG.CONTRACT;
-- CREATE TABLE SF360_KEEP.CONFIG.SUBSCRIPTION     CLONE SF360.CONFIG.SUBSCRIPTION;
-- CREATE TABLE SF360_KEEP.CONFIG.ACCOUNT_SCOPE    CLONE SF360.CONFIG.ACCOUNT_SCOPE;
-- CREATE TABLE SF360_KEEP.CONFIG.ALERT_THRESHOLDS CLONE SF360.CONFIG.ALERT_THRESHOLDS;
-- CREATE TABLE SF360_KEEP.CONFIG.SETTINGS         CLONE SF360.CONFIG.SETTINGS;
-- CREATE TABLE SF360_KEEP.CONFIG.BENCHMARKS       CLONE SF360.CONFIG.BENCHMARKS;
-- CREATE TABLE SF360_KEEP.ORDERFORM.RAW_UPLOAD    CLONE SF360.ORDERFORM.RAW_UPLOAD;
-- CREATE TABLE SF360_KEEP.ORDERFORM.EXTRACTED     CLONE SF360.ORDERFORM.EXTRACTED;

/*******************************************************************************
 * SECTION 1 -- STOP THE SCHEDULE
 *
 * Root first, so no new run can start while the graph is being dismantled. A task
 * graph cannot be modified while its root is resumed, so this ordering is required
 * rather than merely tidy.
 ******************************************************************************/

ALTER TASK IF EXISTS SF360.LANDING.TSK_SF360_ROOT             SUSPEND;
ALTER TASK IF EXISTS SF360.LANDING.TSK_SF360_LANDING_ACCOUNT  SUSPEND;
ALTER TASK IF EXISTS SF360.LANDING.TSK_SF360_LANDING_ACCOUNT2 SUSPEND;
ALTER TASK IF EXISTS SF360.LANDING.TSK_SF360_LANDING_AI       SUSPEND;
ALTER TASK IF EXISTS SF360.LANDING.TSK_SF360_CURATED          SUSPEND;
ALTER TASK IF EXISTS SF360.LANDING.TSK_SF360_TELEMETRY        SUSPEND;

-- Children before parents.
DROP TASK IF EXISTS SF360.LANDING.TSK_SF360_TELEMETRY;
DROP TASK IF EXISTS SF360.LANDING.TSK_SF360_CURATED;
DROP TASK IF EXISTS SF360.LANDING.TSK_SF360_LANDING_AI;
DROP TASK IF EXISTS SF360.LANDING.TSK_SF360_LANDING_ACCOUNT2;
DROP TASK IF EXISTS SF360.LANDING.TSK_SF360_LANDING_ACCOUNT;
DROP TASK IF EXISTS SF360.LANDING.TSK_SF360_ROOT;

/*******************************************************************************
 * SECTION 2 -- APP AND SOURCE
 ******************************************************************************/

DROP STREAMLIT IF EXISTS SF360.APP.SNOWFLAKE360;
DROP GIT REPOSITORY IF EXISTS SF360.APP.SF360_REPO;

/*******************************************************************************
 * SECTION 3 -- THE DATABASE
 *
 * This is the irreversible step. It takes the ORDER_FORMS stage and the PDFs on it
 * with it.
 ******************************************************************************/

DROP DATABASE IF EXISTS SF360;

/*******************************************************************************
 * SECTION 4 -- ACCOUNT-LEVEL OBJECTS
 *
 * These sit outside the database, so dropping SF360 does not remove them.
 ******************************************************************************/

DROP WAREHOUSE IF EXISTS SF360_WH;
DROP API INTEGRATION IF EXISTS SF360_GIT_API;

-- Account-level grants are revoked implicitly when the role is dropped. The
-- database roles granted to it (SNOWFLAKE.USAGE_VIEWER and the rest) are Snowflake's
-- own and are unaffected.
DROP ROLE IF EXISTS SF360_APP_ROLE;

/*******************************************************************************
 * SECTION 5 -- CONFIRM
 ******************************************************************************/

SELECT
  (SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASES
    WHERE DATABASE_NAME = 'SF360' AND DELETED IS NULL)             AS DATABASE_REMAINING,
  'Teardown complete. ACCOUNT_USAGE views are untouched -- Snowflake360 only ever read them, so re-running setup.sql rebuilds everything except the configuration you entered.' AS NOTE;

SHOW WAREHOUSES LIKE 'SF360_WH';
SHOW ROLES LIKE 'SF360_APP_ROLE';
