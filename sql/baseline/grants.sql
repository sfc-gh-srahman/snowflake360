-- Snowflake360 :: privileges for SF360_APP_ROLE.
--
-- This file is hand-maintained and authoritative. It replaced a generated file of
-- 421 GRANT statements scripted from SHOW GRANTS TO ROLE, which had two problems.
--
-- First, 333 of those grants were the fallout of a dev-time GRANT ALL ON SCHEMA.
-- The role could create masking policies, network rules, secrets, authentication
-- policies, session policies, password policies and services across four of the
-- five schemas -- while ORDERFORM had only USAGE, which is what showed the rest
-- was accidental rather than considered. A Streamlit app that reads usage views
-- and writes configuration rows has no business holding those privileges, and no
-- customer security review would accept them.
--
-- Second, enumerating every object meant a newly added table was invisible to the
-- app until someone regenerated the file. Grants here are made ON ALL plus ON
-- FUTURE for each object type, so both existing and later objects are covered.
--
-- The shape is: read everything in SF360, write only what the customer owns.
--
-- Run as ACCOUNTADMIN. Safe to re-run.

USE ROLE ACCOUNTADMIN;
USE DATABASE SF360;

-- ---------------------------------------------------------------------------
-- 1. Reading Snowflake's own usage data
--
-- This is the section that makes the application work at all. Every number in
-- Snowflake360 originates in SNOWFLAKE.ACCOUNT_USAGE or
-- SNOWFLAKE.ORGANIZATION_USAGE, and access to those is granted through database
-- roles rather than object grants.
--
-- Granular viewer roles are used instead of IMPORTED PRIVILEGES ON DATABASE
-- SNOWFLAKE. IMPORTED PRIVILEGES would be one statement instead of nine, but it
-- confers read access to everything in the SNOWFLAKE database, which is far more
-- than this app reads. Each role below maps to views the app actually queries.
-- ---------------------------------------------------------------------------

-- Account-level usage: METERING_HISTORY, QUERY_HISTORY, WAREHOUSE_METERING_HISTORY,
-- STORAGE_USAGE, QUERY_ATTRIBUTION_HISTORY, WAREHOUSE_LOAD_HISTORY.
GRANT DATABASE ROLE SNOWFLAKE.USAGE_VIEWER              TO ROLE SF360_APP_ROLE;

-- Organization-level usage. These three are why the app can report on an entire
-- organization rather than one account, and they exist only in an organization
-- account -- which is why CONFIG.SETTINGS carries a MODE of ORG or ACCOUNT.
GRANT DATABASE ROLE SNOWFLAKE.ORGANIZATION_USAGE_VIEWER    TO ROLE SF360_APP_ROLE;
GRANT DATABASE ROLE SNOWFLAKE.ORGANIZATION_BILLING_VIEWER  TO ROLE SF360_APP_ROLE;
GRANT DATABASE ROLE SNOWFLAKE.ORGANIZATION_ACCOUNTS_VIEWER TO ROLE SF360_APP_ROLE;

-- ACCESS_HISTORY and object metadata, used by the optimization and attribution
-- pages to name the objects behind a cost.
GRANT DATABASE ROLE SNOWFLAKE.GOVERNANCE_VIEWER         TO ROLE SF360_APP_ROLE;
GRANT DATABASE ROLE SNOWFLAKE.OBJECT_VIEWER             TO ROLE SF360_APP_ROLE;

-- LOGIN_HISTORY / users and roles, for attributing spend to a user or role.
GRANT DATABASE ROLE SNOWFLAKE.SECURITY_VIEWER           TO ROLE SF360_APP_ROLE;

-- Data sharing and reader account consumption, for the Data Sharing page.
GRANT DATABASE ROLE SNOWFLAKE.SHARING_USAGE_VIEWER      TO ROLE SF360_APP_ROLE;
GRANT DATABASE ROLE SNOWFLAKE.READER_USAGE_VIEWER       TO ROLE SF360_APP_ROLE;

-- AI_PARSE_DOCUMENT and AI_EXTRACT, used by order form extraction.
--
-- This grant was missing until an audit caught it. Every test had been run as
-- ACCOUNTADMIN, which already has Cortex access, so extraction worked throughout
-- development and would have failed on the first customer who used it as the app
-- role. Cold-install testing is what surfaces this class of bug.
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER               TO ROLE SF360_APP_ROLE;

-- ---------------------------------------------------------------------------
-- 2. Compute
-- ---------------------------------------------------------------------------

-- USAGE to run queries; OPERATE so opening the app can resume a suspended
-- warehouse, which it will be most of the time given AUTO_SUSPEND = 60.
GRANT USAGE, OPERATE ON WAREHOUSE SF360_WH TO ROLE SF360_APP_ROLE;

-- The Refresh & alerts panel offers a "run the refresh now" action rather than
-- making the customer wait for the 11:00 UTC schedule.
GRANT EXECUTE TASK         ON ACCOUNT TO ROLE SF360_APP_ROLE;
GRANT EXECUTE MANAGED TASK ON ACCOUNT TO ROLE SF360_APP_ROLE;

-- ---------------------------------------------------------------------------
-- 3. Traversal
--
-- USAGE only. The app creates no objects, so it needs no CREATE privilege on any
-- schema. This is the single most important difference from what this file
-- replaced.
-- ---------------------------------------------------------------------------

GRANT USAGE ON DATABASE SF360 TO ROLE SF360_APP_ROLE;

GRANT USAGE ON SCHEMA SF360.CONFIG    TO ROLE SF360_APP_ROLE;
GRANT USAGE ON SCHEMA SF360.LANDING   TO ROLE SF360_APP_ROLE;
GRANT USAGE ON SCHEMA SF360.CURATED   TO ROLE SF360_APP_ROLE;
GRANT USAGE ON SCHEMA SF360.ORDERFORM TO ROLE SF360_APP_ROLE;
GRANT USAGE ON SCHEMA SF360.APP       TO ROLE SF360_APP_ROLE;

-- ---------------------------------------------------------------------------
-- 4. Read access across the model
--
-- ON ALL covers what exists now, ON FUTURE covers what gets added later. Without
-- the FUTURE half, a new dynamic table is invisible to the app until somebody
-- remembers to re-run this file -- a failure that is silent and delayed, because
-- the app keeps working until the moment it reads the new object.
-- ---------------------------------------------------------------------------

GRANT SELECT ON ALL TABLES            IN DATABASE SF360 TO ROLE SF360_APP_ROLE;
GRANT SELECT ON FUTURE TABLES         IN DATABASE SF360 TO ROLE SF360_APP_ROLE;
GRANT SELECT ON ALL VIEWS             IN DATABASE SF360 TO ROLE SF360_APP_ROLE;
GRANT SELECT ON FUTURE VIEWS          IN DATABASE SF360 TO ROLE SF360_APP_ROLE;
GRANT SELECT ON ALL DYNAMIC TABLES    IN DATABASE SF360 TO ROLE SF360_APP_ROLE;
GRANT SELECT ON FUTURE DYNAMIC TABLES IN DATABASE SF360 TO ROLE SF360_APP_ROLE;

-- ---------------------------------------------------------------------------
-- 5. Write access, confined to what the customer owns
--
-- CONFIG holds the contract, the negotiated rates, the alert thresholds, the
-- account scope and the settings -- all typed in by the customer or accepted from
-- their order form. ORDERFORM holds their uploads and the extracted values a
-- human reviews. Those are the only two schemas the app may modify.
--
-- LANDING and CURATED get no write grant. Everything in them is derived from
-- ACCOUNT_USAGE and is written by the refresh procedures under the task owner's
-- rights, never by the app.
-- ---------------------------------------------------------------------------

GRANT INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA SF360.CONFIG TO ROLE SF360_APP_ROLE;
GRANT INSERT, UPDATE, DELETE ON FUTURE TABLES IN SCHEMA SF360.CONFIG TO ROLE SF360_APP_ROLE;

GRANT INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA SF360.ORDERFORM TO ROLE SF360_APP_ROLE;
GRANT INSERT, UPDATE, DELETE ON FUTURE TABLES IN SCHEMA SF360.ORDERFORM TO ROLE SF360_APP_ROLE;

-- ---------------------------------------------------------------------------
-- 6. Order form ingestion
-- ---------------------------------------------------------------------------

-- READ to let AI_PARSE_DOCUMENT open the file, WRITE for the upload itself and
-- for ALTER STAGE ... REFRESH, which the upload runs so the directory table the
-- UI lists files from is current.
GRANT READ, WRITE ON STAGE SF360.ORDERFORM.ORDER_FORMS TO ROLE SF360_APP_ROLE;

-- The extract / check / accept procedures, called from the Order form tab.
GRANT USAGE ON ALL PROCEDURES    IN SCHEMA SF360.ORDERFORM TO ROLE SF360_APP_ROLE;
GRANT USAGE ON FUTURE PROCEDURES IN SCHEMA SF360.ORDERFORM TO ROLE SF360_APP_ROLE;
GRANT USAGE ON ALL FUNCTIONS     IN SCHEMA SF360.ORDERFORM TO ROLE SF360_APP_ROLE;
GRANT USAGE ON FUTURE FUNCTIONS  IN SCHEMA SF360.ORDERFORM TO ROLE SF360_APP_ROLE;

-- ---------------------------------------------------------------------------
-- Deliberately NOT granted
--
--   CREATE anything on any schema  -- the app creates no objects
--   Write on LANDING or CURATED    -- derived data, written by tasks
--   OWNERSHIP of any object        -- the installing role stays the owner
--   IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE -- too broad, see section 1
--   Privileges on SNOWFLAKE.LOCAL.ANOMALY_INSIGHTS -- registering alert
--     recipients needs account-level rights the app should not hold. The app
--     attempts the call, and when it fails it saves the recipients anyway and
--     shows the SQL for an administrator to run.
-- ---------------------------------------------------------------------------

SHOW GRANTS TO ROLE SF360_APP_ROLE;
