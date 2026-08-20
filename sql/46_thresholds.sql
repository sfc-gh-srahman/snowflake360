-- Snowflake360 :: User-defined alert thresholds
--
-- Native Budgets cannot express this contract: the limit is in credits not
-- currency, the interval is a fixed calendar month, there is no pooling across
-- periods, and a budget carries a single notification threshold. So thresholds
-- live here, owned and edited by the customer in Settings, and a Budget can be
-- attached later purely as an email transport.
--
-- Each threshold is evaluated twice: against position today, and against the
-- projected end-of-period or end-of-term position. That is what lets the app say
-- "not breached yet, but forecast to breach in 23 days" rather than only reporting
-- a breach after it has already cost money.

CREATE TABLE IF NOT EXISTS SF360.CONFIG.ALERT_THRESHOLDS (
  THRESHOLD_ID   NUMBER        NOT NULL,
  SCOPE          VARCHAR       NOT NULL COMMENT 'PERIOD evaluates against the current installment allocation. TERM evaluates against total capacity.',
  THRESHOLD_PCT  NUMBER(38,2)  NOT NULL,
  LABEL          VARCHAR,
  SEVERITY       VARCHAR       DEFAULT 'WARNING' COMMENT 'INFO, WARNING or CRITICAL. Controls banner prominence.',
  NOTIFY_EMAIL   BOOLEAN       DEFAULT FALSE COMMENT 'Opt-in. Requires a notification integration configured in Settings.',
  IS_ENABLED     BOOLEAN       DEFAULT TRUE,
  UPDATED_BY     VARCHAR       DEFAULT CURRENT_USER(),
  UPDATED_AT     TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
  CONSTRAINT PK_ALERT_THRESHOLDS PRIMARY KEY (THRESHOLD_ID)
)
COMMENT = 'Customer-defined consumption thresholds. Intentionally empty on install: what counts as "burning hot" is a business judgement the customer owns, and a seeded value would present an opinion as a finding.';

-- Deliberately NOT seeded.
--
-- Thresholds are a business judgement the customer owns, and pre-filling them
-- makes an opinion look like a finding: a fresh install would show eight
-- "Breached" rows describing limits nobody chose (four quartiles across the
-- PERIOD and TERM scopes). The Setup & Settings page
-- offers a one-click quartile preset (25 / 50 / 75 / 100) for whichever scope
-- the customer wants, which is a suggestion they accept rather than a default
-- they have to discover and undo.

-- Threshold evaluation ------------------------------------------------------

CREATE OR REPLACE VIEW SF360.CURATED.FCT_THRESHOLD_STATUS
COMMENT = 'Evaluates each enabled threshold against both current and projected position. STATUS is OK, FORECAST_BREACH or BREACHED, so a threshold can warn before it is crossed rather than only after.'
AS
WITH pos AS (
  SELECT
    MAX(IFF(IS_CURRENT_PERIOD, PERIOD_LABEL,      NULL)) AS PERIOD_LABEL,
    MAX(IFF(IS_CURRENT_PERIOD, PERIOD_END,        NULL)) AS PERIOD_END,
    MAX(IFF(IS_CURRENT_PERIOD, ALLOCATION,        NULL)) AS ALLOCATION,
    MAX(IFF(IS_CURRENT_PERIOD, CONSUMPTION,       NULL)) AS PERIOD_CONSUMPTION,
    MAX(IFF(IS_CURRENT_PERIOD, PERIOD_BURN_PCT,   NULL)) AS PERIOD_PCT,
    MAX(IFF(IS_CURRENT_PERIOD, PROJECTED_PERIOD_BURN_PCT, NULL)) AS PERIOD_PCT_PROJ,
    MAX(IFF(IS_CURRENT_PERIOD, CUM_CONSUMPTION,   NULL)) AS CUM_CONSUMPTION,
    MAX(IFF(IS_CURRENT_PERIOD, TERM_BURN_PCT,     NULL)) AS TERM_PCT,
    MAX(TOTAL_CAPACITY) AS TOTAL_CAPACITY,
    MAX(TERM_END)       AS TERM_END,
    MAX(CURRENCY)       AS CURRENCY
  FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION
),
rate AS (
  SELECT COALESCE(
           MAX(IFF(METHOD='NATIVE_FORECAST_FLOORED', PROJECTED_RATE_PER_DAY, NULL)),
           MAX(IFF(METHOD='RUN_RATE_30D',            PROJECTED_RATE_PER_DAY, NULL))
         ) AS RATE_PER_DAY
  FROM SF360.CURATED.FCT_CONTRACT_PROJECTION
),
proj AS (
  SELECT p.*, r.RATE_PER_DAY,
    -- Projected term-end position at the forecast rate.
    ROUND((p.CUM_CONSUMPTION
           + GREATEST(DATEDIFF(day, CURRENT_DATE(), p.TERM_END),0) * r.RATE_PER_DAY)
          / NULLIF(p.TOTAL_CAPACITY,0) * 100, 2) AS TERM_PCT_PROJ
  FROM pos p CROSS JOIN rate r
),
ev AS (
  SELECT
    t.THRESHOLD_ID, t.SCOPE, t.THRESHOLD_PCT, t.LABEL, t.SEVERITY, t.NOTIFY_EMAIL,
    p.CURRENCY, p.PERIOD_LABEL, p.PERIOD_END, p.TERM_END,
    CASE t.SCOPE WHEN 'PERIOD' THEN p.PERIOD_PCT      ELSE p.TERM_PCT      END AS CURRENT_PCT,
    CASE t.SCOPE WHEN 'PERIOD' THEN p.PERIOD_PCT_PROJ ELSE p.TERM_PCT_PROJ END AS PROJECTED_PCT,
    CASE t.SCOPE WHEN 'PERIOD' THEN p.ALLOCATION      ELSE p.TOTAL_CAPACITY END AS BASIS_AMOUNT,
    CASE t.SCOPE WHEN 'PERIOD' THEN p.PERIOD_CONSUMPTION ELSE p.CUM_CONSUMPTION END AS ACTUAL_AMOUNT,
    CASE t.SCOPE WHEN 'PERIOD' THEN p.PERIOD_END      ELSE p.TERM_END       END AS HORIZON_DATE,
    p.RATE_PER_DAY
  FROM SF360.CONFIG.ALERT_THRESHOLDS t
  CROSS JOIN proj p
  WHERE t.IS_ENABLED
)
SELECT
  THRESHOLD_ID, SCOPE, THRESHOLD_PCT, LABEL, SEVERITY, NOTIFY_EMAIL,
  CURRENCY, PERIOD_LABEL, BASIS_AMOUNT, ACTUAL_AMOUNT,
  CURRENT_PCT, PROJECTED_PCT, HORIZON_DATE,
  ROUND(BASIS_AMOUNT * THRESHOLD_PCT / 100, 2) AS THRESHOLD_AMOUNT,
  CASE
    WHEN CURRENT_PCT   >= THRESHOLD_PCT THEN 'BREACHED'
    WHEN PROJECTED_PCT >= THRESHOLD_PCT THEN 'FORECAST_BREACH'
    ELSE 'OK'
  END AS STATUS,
  -- Days until the threshold amount is reached at the forecast rate.
  CASE
    WHEN CURRENT_PCT >= THRESHOLD_PCT THEN 0
    WHEN RATE_PER_DAY > 0
      THEN CEIL((BASIS_AMOUNT * THRESHOLD_PCT / 100 - ACTUAL_AMOUNT) / RATE_PER_DAY)
    ELSE NULL
  END AS DAYS_UNTIL_BREACH,
  CURRENT_TIMESTAMP() AS BUILT_AT
FROM ev;
