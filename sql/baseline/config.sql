-- Snowflake360 :: CONFIG schema, scripted from the live account.
--
-- Captured as a baseline because these objects were created ad hoc during
-- development and their definitions existed only inside the account. This file
-- is the reproducible record; the numbered files in sql/ remain the source of
-- truth for anything created after it.
--
-- Originally scripted from a live SF360 deployment, then hand-corrected.
--
-- CREATE ... IF NOT EXISTS throughout, which is the one place this baseline
-- deliberately departs from what GET_DDL emitted. GET_DDL writes CREATE OR
-- REPLACE, and CONFIG is the only schema holding data that cannot be rebuilt:
-- the contract, the subscription rates, the alert thresholds and the account
-- scope are all typed in by the customer or accepted from their order form.
-- Re-running the scripted form would have dropped the schema and silently reset
-- the app to unconfigured, which on a reinstall or an upgrade is indistinguishable
-- from data loss. LANDING and CURATED keep CREATE OR REPLACE because everything
-- in them is derived and is rebuilt nightly.
--
-- The tradeoff of IF NOT EXISTS is that a column added here does not reach an
-- existing install. Adding one therefore needs an explicit ALTER TABLE, which is
-- the right amount of friction for a schema that holds the customer's contract.

USE DATABASE SF360;

CREATE SCHEMA IF NOT EXISTS SF360.CONFIG COMMENT='Customer-entered contract, subscription, pricing, benchmarks, settings';

CREATE TABLE IF NOT EXISTS SF360.CONFIG.ACCOUNT_SCOPE (
	ACCOUNT_NAME VARCHAR(16777216) NOT NULL,
	ACCOUNT_LOCATOR VARCHAR(16777216),
	IS_INCLUDED BOOLEAN DEFAULT TRUE,
	NOTE VARCHAR(16777216),
	UPDATED_BY VARCHAR(16777216) DEFAULT CURRENT_USER(),
	UPDATED_AT TIMESTAMP_LTZ(9) DEFAULT CURRENT_TIMESTAMP()
)COMMENT='Org-mode account scope. An EMPTY table means ALL accounts. Seeded with 5 test accounts chosen for edition and region variety.'
;
CREATE TABLE IF NOT EXISTS SF360.CONFIG.ALERT_THRESHOLDS (
	THRESHOLD_ID NUMBER(38,0) NOT NULL,
	SCOPE VARCHAR(16777216) NOT NULL COMMENT 'PERIOD evaluates against the current installment allocation. TERM evaluates against total capacity.',
	THRESHOLD_PCT NUMBER(38,2) NOT NULL,
	LABEL VARCHAR(16777216),
	SEVERITY VARCHAR(16777216) DEFAULT 'WARNING' COMMENT 'INFO, WARNING or CRITICAL. Controls banner prominence.',
	NOTIFY_EMAIL BOOLEAN DEFAULT FALSE COMMENT 'Opt-in. Requires a notification integration configured in Settings.',
	IS_ENABLED BOOLEAN DEFAULT TRUE,
	UPDATED_BY VARCHAR(16777216) DEFAULT CURRENT_USER(),
	UPDATED_AT TIMESTAMP_LTZ(9) DEFAULT CURRENT_TIMESTAMP(),
	constraint PK_ALERT_THRESHOLDS primary key (THRESHOLD_ID)
)COMMENT='Customer-defined consumption thresholds. Seeded with sensible defaults but fully editable, because what counts as \"burning hot\" is a business judgement the customer owns.'
;
CREATE TABLE IF NOT EXISTS SF360.CONFIG.BENCHMARKS (
	BENCHMARK_KEY VARCHAR(16777216) NOT NULL,
	BENCHMARK_VALUE NUMBER(18,4) NOT NULL,
	LOWER_IS_BETTER BOOLEAN DEFAULT TRUE,
	SOURCE VARCHAR(16777216),
	AS_OF_DATE DATE,
	DESCRIPTION VARCHAR(16777216),
	UPDATED_BY VARCHAR(16777216) DEFAULT CURRENT_USER(),
	UPDATED_AT TIMESTAMP_LTZ(9) DEFAULT CURRENT_TIMESTAMP(),
	primary key (BENCHMARK_KEY)
)COMMENT='Published efficiency medians. Displayed with AS_OF_DATE so a stale comparison is visible rather than assumed current.'
;
CREATE TABLE IF NOT EXISTS SF360.CONFIG.CONTRACT (
	CONTRACT_SK NUMBER(38,0) NOT NULL autoincrement start 1 increment 1 noorder,
	ORGANIZATION_NAME VARCHAR(16777216),
	CONTRACT_NUMBER VARCHAR(16777216),
	AGREEMENT_TYPE VARCHAR(16777216),
	CONTRACT_START_DATE DATE NOT NULL,
	CONTRACT_END_DATE DATE NOT NULL,
	EXPIRATION_DATE DATE,
	METERED_CURRENCY VARCHAR(16777216) DEFAULT 'USD',
	CAPACITY_PURCHASED NUMBER(18,2) NOT NULL,
	ADDITIONAL_CAPACITY NUMBER(18,2) DEFAULT 0,
	TOTAL_FREE_USAGE NUMBER(18,2) DEFAULT 0,
	ROLLOVER NUMBER(18,2) DEFAULT 0,
	ADJUSTMENT NUMBER(18,2) DEFAULT 0,
	BALANCE_TRANSFER NUMBER(18,2) DEFAULT 0,
	CONTRACT_OFFSET NUMBER(18,2) DEFAULT 0,
	CURRENCY_CONVERSION_ADJUSTMENT NUMBER(18,2) DEFAULT 0,
	DATA_SHARING_REBATE NUMBER(18,2) DEFAULT 0,
	BALANCE_MIGRATION NUMBER(18,2) DEFAULT 0,
	COMPUTE_SUPPORT_ADJUSTMENT NUMBER(18,2) DEFAULT 0,
	STORAGE_SUPPORT_ADJUSTMENT NUMBER(18,2) DEFAULT 0,
	MCD_UPLIFT_PCT NUMBER(9,4),
	MCD_LIMIT NUMBER(18,2),
	CAPACITY_DISCOUNT_PCT NUMBER(9,4) DEFAULT 0,
	CONTRACT_SOURCE VARCHAR(16777216) DEFAULT 'CUSTOMER_ENTERED',
	IS_ACTIVE BOOLEAN,
	VALID_FROM DATE NOT NULL,
	VALID_TO DATE,
	UPDATED_BY VARCHAR(16777216) DEFAULT CURRENT_USER(),
	UPDATED_AT TIMESTAMP_LTZ(9) DEFAULT CURRENT_TIMESTAMP(),
	BILLING_FREQUENCY VARCHAR(16777216) COMMENT 'Capacity Fees billing frequency from the order form, e.g. Quarterly. Drives the installment schedule.',
	ON_DEMAND_BILLING_FREQUENCY VARCHAR(16777216) COMMENT 'On Demand Fees billing frequency, e.g. Monthly in Arrears. Governs invoicing after capacity is exhausted.',
	TERM_LENGTH_MONTHS NUMBER(38,0) COMMENT 'Subscription Term length in months.',
	CARRYOVER_MODE VARCHAR(16777216) DEFAULT 'POOLED' COMMENT 'POOLED means unspent allocation carries forward within the term. STRICT means each period stands alone.',
	CAPACITY_CREDIT_PRICE NUMBER(38,6) COMMENT 'Discounted credit price from the order form.',
	ON_DEMAND_CREDIT_PRICE NUMBER(38,6) COMMENT 'Undiscounted price applied after capacity is exhausted. The gap between this and CAPACITY_CREDIT_PRICE is the price cliff.',
	DISCOUNT_APPLIES_ON_DEMAND BOOLEAN DEFAULT FALSE COMMENT 'Order forms normally state the credit discount does NOT apply to On Demand.',
	INVOICE_PULL_FORWARD BOOLEAN DEFAULT TRUE COMMENT 'Whether Snowflake may pull forward subsequent invoices when a period overspends.',
	STORAGE_PRICE_PER_TB NUMBER(38,6) COMMENT 'Capacity storage price per TB per month.',
	STORAGE_TIER VARCHAR(16777216),
	EDITION VARCHAR(16777216),
	CLOUD_PROVIDER VARCHAR(16777216),
	REGION_NAME VARCHAR(16777216),
	PAYMENT_TERMS_DAYS NUMBER(38,0),
	BILLING_EMAIL VARCHAR(16777216),
	SOURCE_UPLOAD_ID VARCHAR(16777216) COMMENT 'ORDERFORM.RAW_UPLOAD lineage when terms came from an extracted order form.',
	PRIOR_CONSUMPTION_AMOUNT NUMBER(38,2) COMMENT 'Customer-entered consumption already drawn against this contract as of PRIOR_CONSUMPTION_AS_OF. Needed because ACCOUNT_USAGE retains ~365 days, so a multi-year term cannot be measured from its start. Take this from the most recent Snowflake invoice or remaining-balance statement.',
	PRIOR_CONSUMPTION_AS_OF DATE COMMENT 'The as-of date for PRIOR_CONSUMPTION_AMOUNT. Measured consumption is counted only from the day after this date to avoid double counting.',
	CUSTOMER_NAME VARCHAR(16777216) COMMENT 'Legal customer name from the order form. Drives the A360-style page header.',
	primary key (CONTRACT_SK)
)COMMENT='Org-level contract terms. Prefilled from ORGANIZATION_USAGE.CONTRACT_ITEMS where an active contract exists; customer-entered otherwise. Grain is CONTRACT_NUMBER, matching how the contract governs all in-scope accounts.'
;
CREATE TABLE IF NOT EXISTS SF360.CONFIG.SETTINGS (
	SETTING_KEY VARCHAR(16777216) NOT NULL,
	SETTING_VALUE VARCHAR(16777216),
	DESCRIPTION VARCHAR(16777216),
	UPDATED_BY VARCHAR(16777216) DEFAULT CURRENT_USER(),
	UPDATED_AT TIMESTAMP_LTZ(9) DEFAULT CURRENT_TIMESTAMP(),
	primary key (SETTING_KEY)
)COMMENT='App-wide settings. Fiscal calendar is deliberately NOT here: Snowflake fiscal year (Feb 1 start) is hardcoded in DIM_DATE.'
;
CREATE TABLE IF NOT EXISTS SF360.CONFIG.SUBSCRIPTION (
	SUBSCRIPTION_ID NUMBER(38,0) NOT NULL autoincrement start 1 increment 1 noorder,
	ACCOUNT_KEY VARCHAR(16777216),
	ACCOUNT_LOCATOR VARCHAR(16777216),
	ACCOUNT_NAME VARCHAR(16777216),
	REGION VARCHAR(16777216),
	CLOUD VARCHAR(16777216),
	ORGANIZATION_NAME VARCHAR(16777216),
	SERVICE_LEVEL VARCHAR(16777216),
	CONTRACT_NUMBER VARCHAR(16777216),
	CONTRACT_CURRENCY VARCHAR(16777216) DEFAULT 'USD',
	PRICE_PER_PLATFORM_CREDIT NUMBER(18,6),
	OVERAGE_PRICE_PER_CREDIT NUMBER(18,6),
	PRICE_PER_AI_CREDIT NUMBER(18,6),
	PRICE_PER_AI_CREDIT_GLOBAL NUMBER(18,6) DEFAULT 2,
	PRICE_PER_AI_CREDIT_REGIONAL NUMBER(18,6) DEFAULT 2.2,
	AI_ROUTING_MODE VARCHAR(16777216),
	STORAGE_PRICE_PER_TB_MONTH NUMBER(18,6),
	OVERAGE_STORAGE_PRICE NUMBER(18,6),
	RATE_SOURCE VARCHAR(16777216) DEFAULT 'PREFILLED_FROM_RATE_SHEET',
	VALID_FROM DATE NOT NULL,
	VALID_TO DATE,
	UPDATED_BY VARCHAR(16777216) DEFAULT CURRENT_USER(),
	UPDATED_AT TIMESTAMP_LTZ(9) DEFAULT CURRENT_TIMESTAMP(),
	primary key (SUBSCRIPTION_ID)
)COMMENT='Effective-dated subscription and price list. Prefilled from ORGANIZATION_USAGE.RATE_SHEET_DAILY where available; customer-entered values always override. RATE_SOURCE records provenance per row.'
;
create or replace view SF360.CONFIG.BILLING_SCHEDULE(
	CONTRACT_SK,
	CONTRACT_NUMBER,
	IS_ACTIVE,
	CURRENCY,
	BILLING_FREQUENCY,
	CARRYOVER_MODE,
	CADENCE_MONTHS,
	PERIOD_COUNT,
	PERIOD_SEQ,
	PERIOD_LABEL,
	PERIOD_START,
	PERIOD_END,
	ALLOCATION,
	CUM_ALLOCATION,
	TOTAL_CAPACITY,
	TERM_START,
	TERM_END,
	IS_CURRENT_PERIOD
) COMMENT='One row per capacity installment period for each contract, anniversary-aligned to the term start date. ALLOCATION is the per-period installment amount; CUM_ALLOCATION is what has been invoiced through the end of that period on the contractual schedule.'
 as
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


    COALESCE(CAPACITY_PURCHASED,0) + COALESCE(ADDITIONAL_CAPACITY,0)
      + COALESCE(ROLLOVER,0) + COALESCE(TOTAL_FREE_USAGE,0) AS TOTAL_CAPACITY,
    COALESCE(
      TERM_LENGTH_MONTHS,

      NULLIF(DATEDIFF(month, CONTRACT_START_DATE, DATEADD(day,1,CONTRACT_END_DATE)),0)
    ) AS TERM_MONTHS,
    SF360.ORDERFORM.FN_CADENCE_MONTHS(BILLING_FREQUENCY) AS CADENCE_MONTHS
  FROM SF360.CONFIG.CONTRACT
),
sized AS (
  SELECT c.*,
    CASE



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
