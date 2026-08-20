-- Snowflake360 :: refresh task DAG schema, scripted from the live account.
--
-- Captured as a baseline because these objects were created ad hoc during
-- development and their definitions existed only inside the account. This file
-- is the reproducible record; the numbered files in sql/ remain the source of
-- truth for anything created after it.
--
-- Originally scripted from a live SF360 deployment, then hand-corrected.

USE DATABASE SF360;
USE SCHEMA LANDING;
--
-- Task names here are FULLY QUALIFIED, and USE SCHEMA is set explicitly. Neither was
-- true in the scripted version: the CREATE statements were unqualified while every
-- `after` clause named SF360.LANDING.<task>, so with no schema in context the tasks
-- were created in PUBLIC and the children could not find their predecessors:
--
--   091085 (42601): Invalid predecessor SF360.LANDING.TSK_SF360_ROOT was specified.
--
-- Order matters and is NOT the order GET_DDL produced. GET_DDL emits objects
-- alphabetically, which put TSK_SF360_CURATED first -- and creating a task whose
-- predecessor does not exist yet fails outright:
--
--   091085 (42601): Invalid predecessor SF360.LANDING.TSK_SF360_LANDING_AI was
--   specified.
--
-- Parents must precede children. The graph is a straight chain:
--   ROOT -> LANDING_ACCOUNT -> LANDING_ACCOUNT2 -> LANDING_AI -> CURATED -> TELEMETRY
--
-- All six are created suspended. scripts/setup.sql resumes them children-first,
-- because a child cannot be resumed while its predecessor is suspended.

create or replace task SF360.LANDING.TSK_SF360_ROOT
	warehouse=SF360_WH
	schedule='USING CRON 0 11 * * * UTC'
	COMMENT='Snowflake360 daily refresh root. 11:00 UTC: after the 8h attribution latency, before 8am Central year-round with no DST logic.'
	as CALL SF360.LANDING.SP_REBUILD_LANDING();

create or replace task SF360.LANDING.TSK_SF360_LANDING_ACCOUNT
	warehouse=SF360_WH
	after SF360.LANDING.TSK_SF360_ROOT
	as CALL SF360.LANDING.SP_REBUILD_LANDING_ACCOUNT();

create or replace task SF360.LANDING.TSK_SF360_LANDING_ACCOUNT2
	warehouse=SF360_WH
	after SF360.LANDING.TSK_SF360_LANDING_ACCOUNT
	as CALL SF360.LANDING.SP_REBUILD_LANDING_ACCOUNT2();

create or replace task SF360.LANDING.TSK_SF360_LANDING_AI
	warehouse=SF360_WH
	after SF360.LANDING.TSK_SF360_LANDING_ACCOUNT2
	as CALL SF360.LANDING.SP_REBUILD_LANDING_AI();

create or replace task SF360.LANDING.TSK_SF360_CURATED
	warehouse=SF360_WH
	after SF360.LANDING.TSK_SF360_LANDING_AI
	as CALL SF360.CURATED.SP_REFRESH_CURATED();

create or replace task SF360.LANDING.TSK_SF360_TELEMETRY
	warehouse=SF360_WH
	COMMENT='Harvests the app own query tags into SF360.APP.STREAMLIT_QUERY_TAGS'
	after SF360.LANDING.TSK_SF360_CURATED
	as CALL SF360.APP.SP_HARVEST_QUERY_TAGS();
