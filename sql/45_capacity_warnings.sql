-- Snowflake360 :: Forward-looking capacity warnings
--
-- The customer's complaint was not a missing report. It was that nothing told him
-- in advance. Every row here is predictive, carries a days-until, and states what
-- to do about it in plain language.
--
-- Four events, deliberately kept distinct because their remedies differ:
--
--   PERIOD_BURN_HOT      pacing problem inside one installment period
--   PULL_FORWARD_LIKELY  cash-flow problem -- next invoice arrives early
--   CAPACITY_EXHAUSTION  the capacity pool runs dry before the term ends
--   PRICE_CLIFF          post-exhaustion credits lose the negotiated discount
--
-- Forecast rate comes from FCT_CONTRACT_PROJECTION, which already wraps
-- SNOWFLAKE.ML.FORECAST. Reused rather than rebuilt so there is exactly one
-- forecast in the app and no custom detection logic to maintain. The run rate is
-- purely usage-derived and therefore valid regardless of which contract is active.

CREATE OR REPLACE VIEW SF360.CURATED.FCT_CAPACITY_WARNING
COMMENT = 'One row per active forward-looking capacity warning, with severity, days until impact, and a plain-language recommendation. Drives the warning banner at the top of the Active Contract page.'
AS
WITH rate AS (
  -- Prefer the native forecast; fall back to a trailing 30-day run rate when
  -- history is too short for the forecast to be meaningful.
  SELECT
    COALESCE(
      MAX(IFF(METHOD = 'NATIVE_FORECAST_FLOORED', PROJECTED_RATE_PER_DAY, NULL)),
      MAX(IFF(METHOD = 'RUN_RATE_30D',            PROJECTED_RATE_PER_DAY, NULL))
    ) AS RATE_PER_DAY,
    IFF(MAX(IFF(METHOD = 'NATIVE_FORECAST_FLOORED', PROJECTED_RATE_PER_DAY, NULL)) IS NOT NULL,
        'Snowflake ML forecast', 'trailing 30-day run rate') AS RATE_BASIS,
    MAX(AS_OF_DATE) AS AS_OF_DATE
  FROM SF360.CURATED.FCT_CONTRACT_PROJECTION
),
-- Enough history to trust a projection at all. Mirrors the low-denominator guard
-- already used on the Active Contract page: better to say "not enough history" than to forecast off
-- a handful of days.
hist AS (
  SELECT COUNT(DISTINCT USAGE_DATE_UTC) AS DAYS_OF_HISTORY
  FROM SF360.CURATED.FCT_DAILY_CURRENCY
),
cur AS (
  SELECT * FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION WHERE IS_CURRENT_PERIOD
),
term AS (
  SELECT
    MAX(TOTAL_CAPACITY)  AS TOTAL_CAPACITY,
    MAX(TERM_END)        AS TERM_END,
    MAX(CONTRACT_NUMBER) AS CONTRACT_NUMBER,
    MAX(CURRENCY)        AS CURRENCY,
    -- Cumulative position as of the current period.
    MAX(IFF(IS_CURRENT_PERIOD, CUM_CONSUMPTION,   NULL)) AS CUM_CONSUMPTION,
    MAX(IFF(IS_CURRENT_PERIOD, POOLED_REMAINING,  NULL)) AS POOLED_REMAINING,
    MAX(IFF(IS_CURRENT_PERIOD, TERM_BURN_PCT,     NULL)) AS TERM_BURN_PCT,
    MAX(IFF(IS_CURRENT_PERIOD, IS_IN_OVERAGE,     NULL)) AS IS_IN_OVERAGE,
    MAX(CAPACITY_CREDIT_PRICE)  AS CAPACITY_CREDIT_PRICE,
    MAX(ON_DEMAND_CREDIT_PRICE) AS ON_DEMAND_CREDIT_PRICE,
    MAX(PRICE_CLIFF_PCT)        AS PRICE_CLIFF_PCT
  FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION
),
ctx AS (
  SELECT
    t.*, r.RATE_PER_DAY, r.RATE_BASIS, r.AS_OF_DATE, h.DAYS_OF_HISTORY,
    c.PERIOD_LABEL, c.PERIOD_START, c.PERIOD_END, c.ALLOCATION,
    c.CONSUMPTION AS PERIOD_CONSUMPTION, c.PERIOD_BURN_PCT,
    c.PROJECTED_PERIOD_BURN_PCT, c.PULL_FORWARD_TRIGGERED,
    c.ELAPSED_DAYS, c.PERIOD_DAYS, c.DATA_COVERAGE,
    DATEDIFF(day, CURRENT_DATE(), t.TERM_END) AS DAYS_LEFT_IN_TERM,
    -- Days until the pooled capacity is exhausted at the projected rate.
    IFF(r.RATE_PER_DAY > 0 AND t.POOLED_REMAINING > 0,
        FLOOR(t.POOLED_REMAINING / r.RATE_PER_DAY), NULL) AS DAYS_TO_EXHAUSTION
  FROM term t CROSS JOIN rate r CROSS JOIN hist h
  LEFT JOIN cur c ON TRUE
),
enriched AS (
  SELECT ctx.*,
    IFF(DAYS_TO_EXHAUSTION IS NOT NULL,
        DATEADD(day, DAYS_TO_EXHAUSTION, CURRENT_DATE()), NULL) AS EXHAUSTION_DATE
  FROM ctx
),
w AS (
  -- 1. Period burning hot -----------------------------------------------------
  SELECT
    'PERIOD_BURN_HOT' AS WARNING_CODE,
    'Period burning hot' AS WARNING_TITLE,
    IFF(PROJECTED_PERIOD_BURN_PCT >= 150, 'CRITICAL', 'WARNING') AS SEVERITY,
    1 AS SORT_ORDER,
    GREATEST(DATEDIFF(day, CURRENT_DATE(), PERIOD_END), 0) AS DAYS_UNTIL,
    PERIOD_END AS IMPACT_DATE,
    PERIOD_LABEL || ' is pacing to '
      || TO_VARCHAR(PROJECTED_PERIOD_BURN_PCT, '999,990') || '% of its '
      || CURRENCY || ' ' || TO_VARCHAR(ALLOCATION, '999,999,990') || ' allocation.'
      AS MESSAGE,
    'Spend in this installment period is outrunning the amount invoiced for it. '
      || 'Review the largest warehouses and AI workloads before the period closes.'
      AS RECOMMENDATION,
    PROJECTED_PERIOD_BURN_PCT AS METRIC_VALUE
  FROM enriched
  WHERE PROJECTED_PERIOD_BURN_PCT > 100 AND DAYS_OF_HISTORY >= 14

  UNION ALL
  -- 2. Invoice pull-forward ---------------------------------------------------
  SELECT
    'PULL_FORWARD_LIKELY',
    'Next invoice may arrive early',
    'WARNING', 2,
    0, CURRENT_DATE(),
    PERIOD_LABEL || ' has consumed ' || CURRENCY || ' '
      || TO_VARCHAR(PERIOD_CONSUMPTION, '999,999,990') || ' against a '
      || TO_VARCHAR(ALLOCATION, '999,999,990') || ' installment, exceeding it by '
      || TO_VARCHAR(PERIOD_CONSUMPTION - ALLOCATION, '999,999,990') || '.',
    'The order form lets Snowflake invoice an installment on the earlier of its '
      || 'scheduled date or the date consumption exceeds it, and to pull forward '
      || 'later invoices. Expect the next invoice sooner than the calendar implies. '
      || 'This is accelerated billing, not an overage charge.',
    PERIOD_BURN_PCT
  FROM enriched
  WHERE PULL_FORWARD_TRIGGERED = TRUE

  UNION ALL
  -- 3. Capacity exhaustion ----------------------------------------------------
  SELECT
    'CAPACITY_EXHAUSTION',
    'Capacity projected to run out before term end',
    IFF(DAYS_TO_EXHAUSTION <= 60, 'CRITICAL', 'WARNING'), 3,
    DAYS_TO_EXHAUSTION, EXHAUSTION_DATE,
    CURRENCY || ' ' || TO_VARCHAR(POOLED_REMAINING, '999,999,990')
      || ' of capacity remains. At the ' || RATE_BASIS || ' of '
      || CURRENCY || ' ' || TO_VARCHAR(RATE_PER_DAY, '999,999,990')
      || '/day it is exhausted on ' || TO_VARCHAR(EXHAUSTION_DATE, 'YYYY-MM-DD')
      || ', ' || TO_VARCHAR(DATEDIFF(day, EXHAUSTION_DATE, TERM_END))
      || ' days before the term ends.',
    'Plan an Additional Capacity Order before this date. Bought while still within '
      || 'capacity it carries the same negotiated discount; bought after, it does not.',
    DAYS_TO_EXHAUSTION
  FROM enriched
  WHERE DAYS_TO_EXHAUSTION IS NOT NULL
    AND EXHAUSTION_DATE < TERM_END
    AND DAYS_OF_HISTORY >= 14

  UNION ALL
  -- 4. Price cliff ------------------------------------------------------------
  SELECT
    'PRICE_CLIFF',
    'Credit price rises when capacity is exhausted',
    'WARNING', 4,
    DAYS_TO_EXHAUSTION, EXHAUSTION_DATE,
    'After capacity is exhausted, usage converts to On Demand and the '
      || TO_VARCHAR(PRICE_CLIFF_PCT, '990.0') || '% credit discount no longer applies: '
      || CURRENCY || ' ' || TO_VARCHAR(CAPACITY_CREDIT_PRICE, '990.00') || ' becomes '
      || CURRENCY || ' ' || TO_VARCHAR(ON_DEMAND_CREDIT_PRICE, '990.00')
      || ' per credit. Projected exposure through term end is about '
      || CURRENCY || ' '
      || TO_VARCHAR(ROUND(GREATEST(DATEDIFF(day, EXHAUSTION_DATE, TERM_END), 0)
                          * RATE_PER_DAY * (PRICE_CLIFF_PCT / 100), 0), '999,999,990') || '.',
    'This increase is easy to miss because it appears as higher unit cost rather '
      || 'than a separate charge. Purchasing additional capacity before exhaustion '
      || 'avoids it entirely.',
    PRICE_CLIFF_PCT
  FROM enriched
  WHERE DAYS_TO_EXHAUSTION IS NOT NULL
    AND EXHAUSTION_DATE < TERM_END
    AND PRICE_CLIFF_PCT > 0
    AND DAYS_OF_HISTORY >= 14

  UNION ALL
  -- 5. Already in overage -----------------------------------------------------
  SELECT
    'IN_OVERAGE',
    'Capacity is already exhausted',
    'CRITICAL', 0,
    0, CURRENT_DATE(),
    'Cumulative consumption of ' || CURRENCY || ' '
      || TO_VARCHAR(CUM_CONSUMPTION, '999,999,990') || ' has passed the '
      || TO_VARCHAR(TOTAL_CAPACITY, '999,999,990') || ' capacity purchase by '
      || TO_VARCHAR(CUM_CONSUMPTION - TOTAL_CAPACITY, '999,999,990')
      || '. Usage is now billed On Demand at undiscounted rates, monthly in arrears.',
    'Purchasing additional capacity restores the negotiated discount on future '
      || 'consumption. Verify this against your latest invoice, and confirm the '
      || 'opening balance in Settings is correct.',
    TERM_BURN_PCT
  FROM enriched
  WHERE IS_IN_OVERAGE = TRUE
)
SELECT
  w.WARNING_CODE, w.WARNING_TITLE, w.SEVERITY, w.SORT_ORDER,
  w.DAYS_UNTIL, w.IMPACT_DATE, w.MESSAGE, w.RECOMMENDATION, w.METRIC_VALUE,
  e.CONTRACT_NUMBER, e.CURRENCY, e.RATE_BASIS, e.DAYS_OF_HISTORY,
  -- Low history makes any projection shaky; say so rather than hiding it.
  IFF(e.DAYS_OF_HISTORY < 60, TRUE, FALSE) AS IS_LOW_CONFIDENCE,
  e.DATA_COVERAGE AS CURRENT_PERIOD_COVERAGE,
  CURRENT_TIMESTAMP() AS BUILT_AT
FROM w CROSS JOIN enriched e;
