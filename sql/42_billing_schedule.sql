-- Snowflake360 :: Billing schedule generator
--
-- Anniversary-aligned installment periods derived from the contract. A view, not
-- a table, so it can never drift from CONFIG.CONTRACT and needs no refresh.
--
-- Periods align to the Subscription Term Start Date, not the calendar. For an
-- order form starting 2024-07-09 with a 36 month Quarterly term, that yields 12
-- periods running 2024-07-09 -> 2024-10-08, 2024-10-09 -> 2025-01-08,
-- and so on. Calendar-quarter alignment would be wrong and would misplace every
-- invoice boundary.

CREATE OR REPLACE VIEW SF360.CONFIG.BILLING_SCHEDULE
COMMENT = 'One row per capacity installment period for each contract, anniversary-aligned to the term start date. ALLOCATION is the per-period installment amount; CUM_ALLOCATION is what has been invoiced through the end of that period on the contractual schedule.'
AS
WITH c AS (
  SELECT
    CONTRACT_SK,
    CONTRACT_NUMBER,
    CONTRACT_START_DATE,
    CONTRACT_END_DATE,
    METERED_CURRENCY,
    IS_ACTIVE,
    BILLING_FREQUENCY,
    CARRYOVER_MODE,
    -- Total entitlement for the term. Rollover from a prior order form is applied
    -- before the capacity purchase per the contract's Billing Frequency clause.
    COALESCE(CAPACITY_PURCHASED,0) + COALESCE(ADDITIONAL_CAPACITY,0)
      + COALESCE(ROLLOVER,0) + COALESCE(TOTAL_FREE_USAGE,0) AS TOTAL_CAPACITY,
    COALESCE(
      TERM_LENGTH_MONTHS,
      -- Fall back to deriving the term from the dates when it was entered by hand.
      NULLIF(DATEDIFF(month, CONTRACT_START_DATE, DATEADD(day,1,CONTRACT_END_DATE)),0)
    ) AS TERM_MONTHS,
    SF360.ORDERFORM.FN_CADENCE_MONTHS(BILLING_FREQUENCY) AS CADENCE_MONTHS
  FROM SF360.CONFIG.CONTRACT
),
sized AS (
  SELECT c.*,
    CASE
      -- Unrecognised or upfront cadence collapses to a single period covering the
      -- whole term, which is the correct behaviour for paid-in-advance contracts
      -- and a safe default when we could not read the cadence.
      WHEN CADENCE_MONTHS IS NULL OR CADENCE_MONTHS = 0 THEN 1
      ELSE GREATEST(CEIL(TERM_MONTHS / CADENCE_MONTHS), 1)
    END AS PERIOD_COUNT,
    CASE
      WHEN CADENCE_MONTHS IS NULL OR CADENCE_MONTHS = 0 THEN TERM_MONTHS
      ELSE CADENCE_MONTHS
    END AS STEP_MONTHS
  FROM c
  WHERE TERM_MONTHS IS NOT NULL AND TERM_MONTHS > 0
),
periods AS (
  SELECT
    s.*,
    seq.SEQ AS PERIOD_SEQ
  FROM sized s
  JOIN (
    SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) AS SEQ
    FROM TABLE(GENERATOR(ROWCOUNT => 240))
  ) seq
    ON seq.SEQ <= s.PERIOD_COUNT
)
SELECT
  CONTRACT_SK,
  CONTRACT_NUMBER,
  IS_ACTIVE,
  METERED_CURRENCY                                        AS CURRENCY,
  BILLING_FREQUENCY,
  CARRYOVER_MODE,
  CADENCE_MONTHS,
  PERIOD_COUNT,
  PERIOD_SEQ,
  'P' || LPAD(PERIOD_SEQ::VARCHAR, 2, '0') || ' of ' || PERIOD_COUNT AS PERIOD_LABEL,
  DATEADD(month, (PERIOD_SEQ - 1) * STEP_MONTHS, CONTRACT_START_DATE) AS PERIOD_START,
  -- Final period is clamped to the contract end so the schedule never overruns
  -- the term, which matters when term months do not divide evenly by cadence.
  LEAST(
    DATEADD(day, -1, DATEADD(month, PERIOD_SEQ * STEP_MONTHS, CONTRACT_START_DATE)),
    CONTRACT_END_DATE
  )                                                       AS PERIOD_END,
  ROUND(TOTAL_CAPACITY / PERIOD_COUNT, 2)                 AS ALLOCATION,
  ROUND(TOTAL_CAPACITY / PERIOD_COUNT * PERIOD_SEQ, 2)    AS CUM_ALLOCATION,
  TOTAL_CAPACITY,
  CONTRACT_START_DATE                                     AS TERM_START,
  CONTRACT_END_DATE                                       AS TERM_END,
  CURRENT_DATE() BETWEEN
      DATEADD(month, (PERIOD_SEQ - 1) * STEP_MONTHS, CONTRACT_START_DATE)
      AND LEAST(DATEADD(day, -1, DATEADD(month, PERIOD_SEQ * STEP_MONTHS, CONTRACT_START_DATE)),
                CONTRACT_END_DATE)                        AS IS_CURRENT_PERIOD
FROM periods;
