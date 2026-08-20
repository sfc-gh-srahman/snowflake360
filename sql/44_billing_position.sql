-- Snowflake360 :: Billing period position with pull-forward detection
--
-- Models three distinct things the contract treats separately, which is the crux
-- of why the customer could not explain his invoices:
--
--  1. PERIOD BURN      -- did consumption in this installment period exceed the
--                         period's allocation? Drives invoice pull-forward.
--  2. POOLED POSITION  -- cumulative consumption against total capacity. Drives
--                         capacity exhaustion.
--  3. OVERAGE          -- consumption beyond total capacity, which converts to
--                         On Demand at undiscounted rates.
--
-- Pull-forward is NOT overage. Per the order form's Billing Frequency clause an
-- installment is invoiced on "the earlier of" the scheduled date or the date
-- consumption exceeds the fees payable for that period, and Snowflake "may pull
-- forward any subsequent invoices". So a customer perfectly within his total
-- capacity can still receive two invoices in one quarter. That is the "double
-- dipping" symptom, and it has nothing to do with overage.
--
-- Data coverage honesty: ACCOUNT_USAGE retains roughly 365 days, so periods that
-- predate coverage cannot be measured. They are flagged NO_DATA rather than shown
-- as zero consumption, and the contract's customer-entered PRIOR_CONSUMPTION
-- opening balance closes the gap so the pooled position stays truthful.

CREATE OR REPLACE VIEW SF360.CURATED.FCT_BILLING_PERIOD_POSITION
COMMENT = 'One row per capacity installment period: allocation, measured consumption, pooled cumulative position, pull-forward trigger, and overage. DATA_COVERAGE flags periods that predate ACCOUNT_USAGE retention so absent history is never rendered as zero spend.'
AS
WITH cov AS (
  SELECT MIN(USAGE_DATE_UTC) AS FIRST_DAY, MAX(USAGE_DATE_UTC) AS LAST_DAY
  FROM SF360.CURATED.FCT_DAILY_CURRENCY
),
ct AS (
  SELECT CONTRACT_SK, CAPACITY_CREDIT_PRICE, ON_DEMAND_CREDIT_PRICE,
         INVOICE_PULL_FORWARD, CARRYOVER_MODE,
         COALESCE(PRIOR_CONSUMPTION_AMOUNT, 0) AS PRIOR_AMT,
         PRIOR_CONSUMPTION_AS_OF               AS PRIOR_AS_OF
  FROM SF360.CONFIG.CONTRACT WHERE IS_ACTIVE
),
-- Measured spend per period. Only counts days after the opening-balance as-of
-- date, so an entered prior balance and measured history never double count.
spend AS (
  SELECT
    b.CONTRACT_SK,
    b.PERIOD_SEQ,
    SUM(f.NET_IN_CURRENCY) AS MEASURED
  FROM SF360.CONFIG.BILLING_SCHEDULE b
  JOIN ct ON ct.CONTRACT_SK = b.CONTRACT_SK
  JOIN SF360.CURATED.FCT_DAILY_CURRENCY f
    ON f.USAGE_DATE_UTC BETWEEN b.PERIOD_START AND b.PERIOD_END
   AND (ct.PRIOR_AS_OF IS NULL OR f.USAGE_DATE_UTC > ct.PRIOR_AS_OF)
  WHERE b.IS_ACTIVE
  GROUP BY 1, 2
),
base AS (
  SELECT
    b.CONTRACT_SK, b.CONTRACT_NUMBER, b.CURRENCY,
    b.BILLING_FREQUENCY, b.CADENCE_MONTHS, b.PERIOD_COUNT,
    b.PERIOD_SEQ, b.PERIOD_LABEL, b.PERIOD_START, b.PERIOD_END,
    b.ALLOCATION, b.CUM_ALLOCATION, b.TOTAL_CAPACITY,
    b.TERM_START, b.TERM_END, b.IS_CURRENT_PERIOD,
    ct.CAPACITY_CREDIT_PRICE, ct.ON_DEMAND_CREDIT_PRICE,
    ct.INVOICE_PULL_FORWARD, ct.PRIOR_AMT, ct.PRIOR_AS_OF,
    COALESCE(s.MEASURED, 0) AS MEASURED_CONSUMPTION,
    -- Classify measurability before any arithmetic, so the UI can suppress
    -- misleading zeros.
    CASE
      WHEN b.PERIOD_START > cov.LAST_DAY                              THEN 'FUTURE'
      WHEN b.PERIOD_END   < COALESCE(ct.PRIOR_AS_OF, cov.FIRST_DAY)   THEN 'NO_DATA'
      WHEN b.PERIOD_START < COALESCE(ct.PRIOR_AS_OF, cov.FIRST_DAY)
        OR b.PERIOD_END   > cov.LAST_DAY                              THEN 'PARTIAL'
      ELSE 'COMPLETE'
    END AS DATA_COVERAGE,
    -- Elapsed and total days let the UI pace a period that is still running.
    DATEDIFF(day, b.PERIOD_START, b.PERIOD_END) + 1 AS PERIOD_DAYS,
    GREATEST(LEAST(DATEDIFF(day, b.PERIOD_START, LEAST(cov.LAST_DAY, b.PERIOD_END)) + 1,
                   DATEDIFF(day, b.PERIOD_START, b.PERIOD_END) + 1), 0) AS ELAPSED_DAYS
  FROM SF360.CONFIG.BILLING_SCHEDULE b
  JOIN ct        ON ct.CONTRACT_SK = b.CONTRACT_SK
  CROSS JOIN cov
  LEFT JOIN spend s ON s.CONTRACT_SK = b.CONTRACT_SK AND s.PERIOD_SEQ = b.PERIOD_SEQ
  WHERE b.IS_ACTIVE
),
cum AS (
  SELECT
    base.*,
    -- Pooled cumulative consumption = opening balance + everything measured to
    -- and including this period.
    PRIOR_AMT + SUM(MEASURED_CONSUMPTION) OVER (
      PARTITION BY CONTRACT_SK ORDER BY PERIOD_SEQ
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS CUM_CONSUMPTION
  FROM base
)
SELECT
  CONTRACT_SK, CONTRACT_NUMBER, CURRENCY,
  BILLING_FREQUENCY, CADENCE_MONTHS, PERIOD_COUNT,
  PERIOD_SEQ, PERIOD_LABEL, PERIOD_START, PERIOD_END,
  DATA_COVERAGE, IS_CURRENT_PERIOD, PERIOD_DAYS, ELAPSED_DAYS,

  ALLOCATION,
  -- Neither an unmeasurable past period nor a period that has not started yet
  -- has a meaningful consumption figure. Both are NULL so charts skip them
  -- instead of drawing a floor of zeros.
  IFF(DATA_COVERAGE IN ('NO_DATA','FUTURE'), NULL, MEASURED_CONSUMPTION) AS CONSUMPTION,
  CUM_ALLOCATION,
  CUM_CONSUMPTION,
  TOTAL_CAPACITY,
  PRIOR_AMT AS OPENING_BALANCE,

  -- Period pacing. Suppressed where the period cannot be measured.
  IFF(DATA_COVERAGE IN ('NO_DATA','FUTURE') OR ALLOCATION = 0, NULL,
      ROUND(MEASURED_CONSUMPTION / ALLOCATION * 100, 2))     AS PERIOD_BURN_PCT,
  -- Straight-line projection of where this period lands if the pace holds. Only
  -- meaningful for a period still in flight.
  IFF(IS_CURRENT_PERIOD AND ELAPSED_DAYS > 0 AND ALLOCATION > 0,
      ROUND(MEASURED_CONSUMPTION / ELAPSED_DAYS * PERIOD_DAYS / ALLOCATION * 100, 2),
      NULL)                                                  AS PROJECTED_PERIOD_BURN_PCT,

  -- Pull-forward: this period's consumption exceeded the fees payable for it.
  -- Contractually this permits Snowflake to invoice the next installment early.
  IFF(DATA_COVERAGE IN ('NO_DATA','FUTURE'), NULL,
      INVOICE_PULL_FORWARD AND MEASURED_CONSUMPTION > ALLOCATION)
                                                             AS PULL_FORWARD_TRIGGERED,
  IFF(DATA_COVERAGE <> 'NO_DATA' AND MEASURED_CONSUMPTION > ALLOCATION,
      ROUND(MEASURED_CONSUMPTION - ALLOCATION, 2), 0)        AS PERIOD_OVERSPEND,

  -- Pooled position against the whole capacity purchase.
  ROUND(TOTAL_CAPACITY - CUM_CONSUMPTION, 2)                 AS POOLED_REMAINING,
  ROUND(CUM_CONSUMPTION / NULLIF(TOTAL_CAPACITY,0) * 100, 2) AS TERM_BURN_PCT,

  -- True overage: cumulative consumption beyond the entire capacity purchase.
  -- This is the only condition that triggers Conversion to On Demand.
  GREATEST(ROUND(CUM_CONSUMPTION - TOTAL_CAPACITY, 2), 0)    AS OVERAGE_AMOUNT,
  CUM_CONSUMPTION > TOTAL_CAPACITY                           AS IS_IN_OVERAGE,

  CAPACITY_CREDIT_PRICE, ON_DEMAND_CREDIT_PRICE,
  ROUND((ON_DEMAND_CREDIT_PRICE / NULLIF(CAPACITY_CREDIT_PRICE,0) - 1) * 100, 2)
                                                             AS PRICE_CLIFF_PCT,
  TERM_START, TERM_END,
  CURRENT_TIMESTAMP()                                        AS BUILT_AT
FROM cum;
