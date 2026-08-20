-- Snowflake360 :: CONFIG.SETTINGS seed.
--
-- These five rows were inserted ad hoc during development and existed nowhere in
-- the repository, which made them a cold-install failure: SP_REBUILD_LANDING reads
-- RETENTION_DAYS out of this table in three places, so on a fresh install the
-- retention window resolved to NULL and the nightly rebuild had no bounds.
--
-- MERGE rather than INSERT so re-running is harmless and a customer's edited value
-- is not reset -- except MODE, which setup.sql deliberately overwrites after probing
-- whether ORGANIZATION_USAGE is actually reachable.

USE DATABASE SF360;
USE SCHEMA CONFIG;

MERGE INTO SF360.CONFIG.SETTINGS t
USING (
  SELECT * FROM VALUES
    ('MODE', 'ACCOUNT',
     'ORG or ACCOUNT. Set by scripts/setup.sql after probing ORGANIZATION_USAGE. ACCOUNT means this is not an organization account, so currency amounts and org-wide rollups have no source and the app says so rather than showing zeros. Seeded as ACCOUNT because it is the safe assumption: claiming ORG when the views are unreachable produces empty panels with no explanation.'),

    ('RETENTION_DAYS', '365',
     'How many days of history the landing rebuild pulls from ACCOUNT_USAGE and ORGANIZATION_USAGE. 365 matches Snowflake''s own retention, so lowering it reduces refresh cost and raising it above 365 gains nothing. Read by SP_REBUILD_LANDING and its three siblings.'),

    ('DISPLAY_TIMEZONE', 'UTC',
     'Timezone for point-in-time timestamps such as upload and acceptance times. Daily cost buckets always stay UTC: shifting a daily bucket into a local zone would move spend between days and stop it reconciling against ACCOUNT_USAGE. Set to an IANA name such as America/New_York to localise the display.'),

    ('APP_WAREHOUSE', 'SF360_WH',
     'The warehouse Snowflake360 runs on. Recorded so the Refresh and alerts panel can report which warehouse the app is actually using rather than assuming. Changing this value does not move the workload -- alter the STREAMLIT object, the tasks and the dynamic tables to do that.'),

    ('REFRESH_CRON_UTC', '0 11 * * *',
     'Schedule of the root refresh task, for display. 11:00 UTC is after Snowflake''s roughly 8 hour query attribution latency has settled, and before 8am US Central year-round with no daylight-saving logic to get wrong. The authoritative schedule is on TSK_SF360_ROOT itself.')
  AS v (SETTING_KEY, SETTING_VALUE, DESCRIPTION)
) s
  ON t.SETTING_KEY = s.SETTING_KEY
-- Only backfill a missing description; never overwrite a value the customer changed.
WHEN MATCHED THEN UPDATE SET
  t.DESCRIPTION = COALESCE(t.DESCRIPTION, s.DESCRIPTION)
WHEN NOT MATCHED THEN INSERT (SETTING_KEY, SETTING_VALUE, DESCRIPTION)
  VALUES (s.SETTING_KEY, s.SETTING_VALUE, s.DESCRIPTION);

-- Expect 5 rows, all with a value.
SELECT COUNT(*) AS SETTINGS,
       COUNT_IF(SETTING_VALUE IS NULL OR TRIM(SETTING_VALUE) = '') AS MISSING_VALUES
FROM SF360.CONFIG.SETTINGS;
