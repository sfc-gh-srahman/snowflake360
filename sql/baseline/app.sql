-- Snowflake360 :: APP schema, scripted from the live account.
--
-- Captured as a baseline because these objects were created ad hoc during
-- development and their definitions existed only inside the account. This file
-- is the reproducible record; the numbered files in sql/ remain the source of
-- truth for anything created after it.
--
-- Originally scripted from a live SF360 deployment, then hand-corrected.
--
-- STREAMLIT_QUERY_TAGS uses CREATE ... IF NOT EXISTS rather than the CREATE OR
-- REPLACE that GET_DDL emitted: it accumulates the app's own query telemetry over
-- time, so replacing it on a redeploy would silently reset the history the
-- Refresh & alerts panel reports from. The procedure and the Streamlit object stay
-- CREATE OR REPLACE because they are code.

USE DATABASE SF360;

CREATE SCHEMA IF NOT EXISTS SF360.APP COMMENT='Streamlit app objects and self-instrumentation telemetry';

-- Created ad hoc during development and recorded here for reproducibility. This is
-- the stage `snow streamlit deploy` uploads the app files to.
CREATE STAGE IF NOT EXISTS SF360.APP.STREAMLIT_STAGE
  DIRECTORY = (ENABLE = TRUE)
  COMMENT = 'Snowflake360 Streamlit app files';

CREATE TABLE IF NOT EXISTS SF360.APP.STREAMLIT_QUERY_TAGS (
	DS_UTC DATE,
	START_TIME TIMESTAMP_LTZ(9),
	APP_NAME VARCHAR(16777216),
	APP_VERSION VARCHAR(16777216),
	PAGE_NAME VARCHAR(16777216),
	QUERY_NAME VARCHAR(16777216),
	RUN_ID VARCHAR(16777216),
	USER_NAME VARCHAR(16777216),
	ROLE_NAME VARCHAR(16777216),
	WAREHOUSE_NAME VARCHAR(16777216),
	QUERY_ID VARCHAR(16777216),
	TOTAL_ELAPSED_MS NUMBER(38,0),
	BYTES_SCANNED NUMBER(38,0),
	CREDITS_ATTRIBUTED NUMBER(18,9),
	EXECUTION_STATUS VARCHAR(16777216),
	HARVESTED_AT TIMESTAMP_LTZ(9)
)COMMENT='Self-instrumentation. Mirrors A360''s A360_STREAMLIT_QUERY_TAGS: which pages are used, by whom, how fast, and at what credit cost.'
;
CREATE OR REPLACE PROCEDURE SF360.APP.SP_HARVEST_QUERY_TAGS()
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS '
DECLARE
  inserted NUMBER DEFAULT 0;
BEGIN
  ALTER SESSION SET TIMEZONE = ''UTC'';

  INSERT INTO SF360.APP.STREAMLIT_QUERY_TAGS
  WITH tagged AS (
    SELECT q.START_TIME, q.QUERY_ID, q.USER_NAME, q.ROLE_NAME, q.WAREHOUSE_NAME,
           q.TOTAL_ELAPSED_TIME, q.BYTES_SCANNED, q.EXECUTION_STATUS,
           TRY_PARSE_JSON(q.QUERY_TAG) AS TAG
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
    WHERE q.START_TIME >= DATEADD(''day'', -7, CURRENT_DATE())
      AND q.QUERY_TAG ILIKE ''%SNOWFLAKE360%''
  )
  SELECT t.START_TIME::DATE, t.START_TIME,
         t.TAG:app::VARCHAR, t.TAG:app_version::VARCHAR,
         t.TAG:page::VARCHAR, t.TAG:query_name::VARCHAR, t.TAG:run_id::VARCHAR,
         t.USER_NAME, t.ROLE_NAME, t.WAREHOUSE_NAME, t.QUERY_ID,
         t.TOTAL_ELAPSED_TIME, t.BYTES_SCANNED,
         a.CREDITS_ATTRIBUTED_COMPUTE, t.EXECUTION_STATUS,
         CURRENT_TIMESTAMP()
  FROM tagged t
  LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
    ON a.QUERY_ID = t.QUERY_ID
  WHERE t.TAG IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM SF360.APP.STREAMLIT_QUERY_TAGS x
                    WHERE x.QUERY_ID = t.QUERY_ID);

  inserted := SQLROWCOUNT;
  RETURN ''harvested '' || inserted || '' tagged app queries'';
END;
';

-- The STREAMLIT object is deliberately NOT created here.
--
-- scripts/setup.sql creates it from the git repository, so Snowflake pulls the app
-- straight from GitHub and the customer needs no local tooling:
--
--   CREATE STREAMLIT SF360.APP.SNOWFLAKE360
--     FROM '@SF360.APP.SF360_REPO/branches/main/streamlit/'
--     MAIN_FILE = 'Snowflake360.py';
--
-- The definition scripted from the account also pointed MAIN_FILE at
-- 'streamlit_app.py', which does not exist in this repository -- an artefact of an
-- earlier deploy. Recreating it from here would have broken the app.
--
-- For local iteration use `snow streamlit deploy` with streamlit/snowflake.yml.
