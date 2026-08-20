-- Snowflake360 :: contract spend projections
--
-- Trailing run-rate windows are 30 / 60 / 90 / 180 days, matching A360's
-- "Select a Run Rate period" control, so the projection basis a customer picks
-- here is the same set of choices a Snowflake seller sees. The 7-day window was
-- dropped with them: a single week is too short to project a multi-year capacity
-- contract from, and offering it invites reading noise as a trend.
--
-- The native method is Snowflake's own FORECASTED_VALUE, floored at zero because
-- it can return negatives, which are meaningless for spend. No custom forecasting
-- model is trained or maintained.
--
-- This file exists because the table was originally created ad hoc and its
-- definition lived only in the account. It is now reproducible from the repo.

CREATE OR REPLACE DYNAMIC TABLE SF360.CURATED.FCT_CONTRACT_PROJECTION(
	SCOPE,
	ORGANIZATION_NAME,
	CONTRACT_NUMBER,
	AS_OF_DATE,
	METHOD,
	METHOD_CLASS,
	PROJECTED_RATE_PER_DAY,
	CAPACITY_PURCHASED,
	CAPACITY_USED_TO_DATE,
	REMAINING_BALANCE,
	DAYS_REMAINING_IN_TERM,
	PROJECTED_TOTAL_AT_TERM_END,
	PROJECTED_OVERAGE,
	DAYS_UNTIL_OVERAGE,
	PROJECTED_OVERAGE_DATE,
	OVERAGE_BEFORE_TERM_END,
	CONTRACT_SOURCE,
	BUILT_AT
) target_lag = '1 day' refresh_mode = FULL initialize = ON_CREATE warehouse = SF360_WH
 as
WITH latest AS (SELECT MAX(USAGE_DATE_UTC) AS AS_OF FROM SF360.CURATED.FCT_CONTRACT_POSITION),
cur AS (
  SELECT p.* FROM SF360.CURATED.FCT_CONTRACT_POSITION p
  WHERE p.USAGE_DATE_UTC = (SELECT AS_OF FROM latest)
),
-- Method 1: trailing run rate over several windows. Cannot go negative.
run_rates AS (
  SELECT w.N AS WINDOW_DAYS, AVG(p.DOLLARS_DAY) AS RATE_PER_DAY
  FROM (SELECT 30 AS N UNION ALL SELECT 60 UNION ALL SELECT 90
        UNION ALL SELECT 180) w
  JOIN SF360.CURATED.FCT_CONTRACT_POSITION p
    ON p.USAGE_DATE_UTC >  DATEADD('day', -w.N, (SELECT AS_OF FROM latest))
   AND p.USAGE_DATE_UTC <= (SELECT AS_OF FROM latest)
  GROUP BY w.N
),
-- Method 2: Snowflake native forecast, floored. Native FORECASTED_VALUE can be negative.
native AS (
  SELECT AVG(GREATEST(FORECASTED_VALUE_RAW, 0)) AS RATE_PER_DAY
  FROM SF360.LANDING.LND_ORG_ANOMALIES
  WHERE ANOMALY_DATE > DATEADD('day', -30, (SELECT AS_OF FROM latest))
),
methods AS (
  SELECT 'RUN_RATE_' || WINDOW_DAYS::VARCHAR || 'D' AS METHOD,
         RATE_PER_DAY, 'TRAILING_RUN_RATE' AS METHOD_CLASS
  FROM run_rates
  UNION ALL
  SELECT 'NATIVE_FORECAST_FLOORED', RATE_PER_DAY, 'SNOWFLAKE_NATIVE' FROM native
)
SELECT
  'ORG'                            AS SCOPE,
  c.ORGANIZATION_NAME,
  c.CONTRACT_NUMBER,
  c.USAGE_DATE_UTC                 AS AS_OF_DATE,
  m.METHOD,
  m.METHOD_CLASS,
  GREATEST(ROUND(m.RATE_PER_DAY, 2), 0)         AS PROJECTED_RATE_PER_DAY,
  c.CAPACITY_PURCHASED,
  ROUND(c.TOTAL_CAPACITY_USED, 2)               AS CAPACITY_USED_TO_DATE,
  ROUND(c.REMAINING_BALANCE, 2)                 AS REMAINING_BALANCE,
  c.DAYS_REMAINING_IN_TERM,
  GREATEST(ROUND(c.TOTAL_CAPACITY_USED
    + GREATEST(m.RATE_PER_DAY,0) * c.DAYS_REMAINING_IN_TERM, 2), 0) AS PROJECTED_TOTAL_AT_TERM_END,
  GREATEST(ROUND(c.TOTAL_CAPACITY_USED
    + GREATEST(m.RATE_PER_DAY,0) * c.DAYS_REMAINING_IN_TERM - c.CAPACITY_PURCHASED, 2), 0)
                                                AS PROJECTED_OVERAGE,
  CASE WHEN m.RATE_PER_DAY > 0 AND c.REMAINING_BALANCE > 0
       THEN CEIL(c.REMAINING_BALANCE / m.RATE_PER_DAY) END AS DAYS_UNTIL_OVERAGE,
  CASE WHEN m.RATE_PER_DAY > 0 AND c.REMAINING_BALANCE > 0
       THEN DATEADD('day', CEIL(c.REMAINING_BALANCE / m.RATE_PER_DAY), c.USAGE_DATE_UTC) END
                                                AS PROJECTED_OVERAGE_DATE,
  CASE WHEN m.RATE_PER_DAY > 0 AND c.REMAINING_BALANCE > 0
        AND DATEADD('day', CEIL(c.REMAINING_BALANCE / m.RATE_PER_DAY), c.USAGE_DATE_UTC)
            <= c.CONTRACT_END_DATE
       THEN TRUE ELSE FALSE END                 AS OVERAGE_BEFORE_TERM_END,
  c.CONTRACT_SOURCE,
  CURRENT_TIMESTAMP()                           AS BUILT_AT
FROM cur c CROSS JOIN methods m;
