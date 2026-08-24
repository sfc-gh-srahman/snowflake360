USE DATABASE SF360;
USE SCHEMA CURATED;
create or replace view VW_VERIFICATION(
	CHK,
	CHECK_NAME,
	ACTUAL,
	EXPECTED,
	RESULT
) as
-- G1: ACCOUNT_KEY must be unique. Name has 62 dupes, locator 26; only locator+region is unique.
SELECT 1 AS CHK, 'G1 dim_account key uniqueness' AS CHECK_NAME,
       (COUNT(*) - COUNT(DISTINCT ACCOUNT_KEY))::VARCHAR AS ACTUAL, '0' AS EXPECTED,
       IFF(COUNT(*) = COUNT(DISTINCT ACCOUNT_KEY), 'PASS','FAIL') AS RESULT
FROM SF360.CURATED.DIM_ACCOUNT
UNION ALL
-- G1b: joining currency to dim must not inflate rows
SELECT 2, 'G1b no join fanout currency to dim',
       (SELECT COUNT(*) FROM SF360.LANDING.LND_ORG_CURRENCY_DAILY u
          JOIN SF360.CURATED.DIM_ACCOUNT d
            ON d.ACCOUNT_LOCATOR=u.ACCOUNT_LOCATOR AND d.REGION=u.REGION)::VARCHAR,
       (SELECT COUNT(*) FROM SF360.LANDING.LND_ORG_CURRENCY_DAILY)::VARCHAR,
       IFF((SELECT COUNT(*) FROM SF360.LANDING.LND_ORG_CURRENCY_DAILY u
              JOIN SF360.CURATED.DIM_ACCOUNT d
                ON d.ACCOUNT_LOCATOR=u.ACCOUNT_LOCATOR AND d.REGION=u.REGION)
           = (SELECT COUNT(*) FROM SF360.LANDING.LND_ORG_CURRENCY_DAILY), 'PASS','FAIL')
UNION ALL
-- G2/G3: edition label normalization must resolve a rate for every in-scope account
SELECT 3, 'G2 all in-scope accounts have rates',
       (SELECT COALESCE(COUNT_IF(PRICE_PER_PLATFORM_CREDIT IS NULL), 0) FROM SF360.CONFIG.SUBSCRIPTION)::VARCHAR, '0',
       IFF((SELECT COALESCE(COUNT_IF(PRICE_PER_PLATFORM_CREDIT IS NULL), 0) FROM SF360.CONFIG.SUBSCRIPTION)=0,'PASS','FAIL')
UNION ALL
-- G2b: different editions must resolve different platform rates
SELECT 4, 'G2b editions resolve distinct rates',
       (SELECT COUNT(DISTINCT PRICE_PER_PLATFORM_CREDIT) FROM SF360.CONFIG.SUBSCRIPTION)::VARCHAR, '>1',
       IFF((SELECT COUNT(DISTINCT PRICE_PER_PLATFORM_CREDIT) FROM SF360.CONFIG.SUBSCRIPTION)>1,'PASS','FAIL')
UNION ALL
-- G4: adjustments must never be counted as consumption
SELECT 5, 'G4 adjustments separated from usage',
       (SELECT COALESCE(COUNT_IF(USAGE_IN_CURRENCY <> 0 AND ADJUSTMENT_IN_CURRENCY <> 0), 0)
          FROM SF360.LANDING.LND_ORG_CURRENCY_DAILY)::VARCHAR, '0',
       IFF((SELECT COALESCE(COUNT_IF(USAGE_IN_CURRENCY <> 0 AND ADJUSTMENT_IN_CURRENCY <> 0), 0)
              FROM SF360.LANDING.LND_ORG_CURRENCY_DAILY)=0,'PASS','FAIL')
UNION ALL
-- G6: totalling across currencies is meaningless
SELECT 6, 'G6 single currency in scope',
       (SELECT COUNT(DISTINCT CURRENCY) FROM SF360.LANDING.LND_ORG_CURRENCY_DAILY)::VARCHAR, '1',
       IFF((SELECT COUNT(DISTINCT CURRENCY) FROM SF360.LANDING.LND_ORG_CURRENCY_DAILY)=1,'PASS','WARN')
UNION ALL
-- Classification completeness: zero unclassified credits
SELECT 7, 'CLS org metering fully classified',
       (SELECT COUNT(*) FROM SF360.LANDING.LND_ORG_METERING_DAILY m
          LEFT JOIN SF360.CURATED.DIM_SERVICE_TYPE d ON d.SERVICE_TYPE=m.SERVICE_TYPE
          WHERE d.SERVICE_TYPE IS NULL)::VARCHAR, '0',
       IFF((SELECT COUNT(*) FROM SF360.LANDING.LND_ORG_METERING_DAILY m
              LEFT JOIN SF360.CURATED.DIM_SERVICE_TYPE d ON d.SERVICE_TYPE=m.SERVICE_TYPE
              WHERE d.SERVICE_TYPE IS NULL)=0,'PASS','FAIL')
UNION ALL
-- Platform + AI must equal total credits
SELECT 8, 'CLS platform plus AI equals total',
       ROUND((SELECT SUM(CREDITS_BILLED) FROM SF360.CURATED.FCT_DAILY_CREDITS)
             - (SELECT SUM(CREDITS_BILLED) FROM SF360.CURATED.FCT_DAILY_CREDITS
                WHERE CREDIT_CLASS IN ('PLATFORM','AI')), 6)::VARCHAR, '0',
       IFF(ABS((SELECT SUM(CREDITS_BILLED) FROM SF360.CURATED.FCT_DAILY_CREDITS)
               - (SELECT SUM(CREDITS_BILLED) FROM SF360.CURATED.FCT_DAILY_CREDITS
                  WHERE CREDIT_CLASS IN ('PLATFORM','AI'))) < 0.000001,'PASS','FAIL')
UNION ALL
-- Forecast floor: no displayed bound may be negative
SELECT 9, 'FC no negative forecast or bound',
       (SELECT COALESCE(COUNT_IF(FORECASTED_VALUE < 0 OR LOWER_BOUND < 0), 0)
          FROM SF360.CURATED.FCT_ANOMALY_DAILY)::VARCHAR, '0',
       IFF((SELECT COALESCE(COUNT_IF(FORECASTED_VALUE < 0 OR LOWER_BOUND < 0), 0)
              FROM SF360.CURATED.FCT_ANOMALY_DAILY)=0,'PASS','FAIL')
UNION ALL
SELECT 10, 'FC no negative projection rate',
       (SELECT COALESCE(COUNT_IF(PROJECTED_RATE_PER_DAY < 0 OR PROJECTED_OVERAGE < 0), 0)
          FROM SF360.CURATED.FCT_CONTRACT_PROJECTION)::VARCHAR, '0',
       IFF((SELECT COALESCE(COUNT_IF(PROJECTED_RATE_PER_DAY < 0 OR PROJECTED_OVERAGE < 0), 0)
              FROM SF360.CURATED.FCT_CONTRACT_PROJECTION)=0,'PASS','FAIL')
UNION ALL
-- G7: coverage gap - in-scope accounts with no usage must be counted, not read as zero
SELECT 11, 'G7 in-scope accounts missing usage',
       (SELECT COUNT(*) FROM SF360.CURATED.DIM_ACCOUNT d
          WHERE d.IS_IN_SCOPE
            AND NOT EXISTS (SELECT 1 FROM SF360.CURATED.FCT_DAILY_CURRENCY f
                            WHERE f.ACCOUNT_KEY = d.ACCOUNT_KEY))::VARCHAR, '0',
       IFF((SELECT COUNT(*) FROM SF360.CURATED.DIM_ACCOUNT d
              WHERE d.IS_IN_SCOPE
                AND NOT EXISTS (SELECT 1 FROM SF360.CURATED.FCT_DAILY_CURRENCY f
                                WHERE f.ACCOUNT_KEY = d.ACCOUNT_KEY))=0,'PASS','WARN')
UNION ALL
-- Contract must have exactly one active row
SELECT 12, 'CT exactly one active contract',
       (SELECT COUNT(*) FROM SF360.CONFIG.CONTRACT WHERE IS_ACTIVE AND VALID_TO IS NULL)::VARCHAR, '1',
       IFF((SELECT COUNT(*) FROM SF360.CONFIG.CONTRACT WHERE IS_ACTIVE AND VALID_TO IS NULL)=1,'PASS','FAIL')
UNION ALL
-- Fiscal calendar must follow Snowflake's February-1 fiscal year. Checked as a
-- rule over every row rather than against one hardcoded date: a frozen date stops
-- testing anything the moment DIM_DATE is built over a different window, and it
-- then reports FAIL for having moved rather than for being wrong. The row-count
-- guard stops an empty DIM_DATE from passing vacuously with zero mismatches.
SELECT 13, 'FY fiscal labels follow the Feb-1 fiscal rule',
       (SELECT COALESCE(COUNT_IF(FISCAL_QUARTER_LABEL <> (CASE WHEN MONTH(DATE_UTC) >= 2 THEN YEAR(DATE_UTC) ELSE YEAR(DATE_UTC)-1 END) || '-Q' || (FLOOR(MOD(MONTH(DATE_UTC)-2+12,12)/3)+1)), 0)::VARCHAR
          || ' mismatched of ' || COUNT(*)::VARCHAR || ' dates' FROM SF360.CURATED.DIM_DATE),
       '0 mismatched, at least 1 date',
       IFF((SELECT COUNT(*) FROM SF360.CURATED.DIM_DATE) > 0
           AND COALESCE((SELECT COALESCE(COUNT_IF(FISCAL_QUARTER_LABEL <> (CASE WHEN MONTH(DATE_UTC) >= 2 THEN YEAR(DATE_UTC) ELSE YEAR(DATE_UTC)-1 END) || '-Q' || (FLOOR(MOD(MONTH(DATE_UTC)-2+12,12)/3)+1)), 0) FROM SF360.CURATED.DIM_DATE), 1) = 0,
           'PASS','FAIL')
UNION ALL
-- BP1: period allocations must sum to the total capacity entitlement. If this
-- drifts, every per-period figure on the Billing Cycles page is wrong.
SELECT 14, 'BP1 allocations sum to capacity',
       (SELECT TO_VARCHAR(ROUND(SUM(ALLOCATION),2)) FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION),
       (SELECT TO_VARCHAR(ROUND(MAX(TOTAL_CAPACITY),2)) FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION),
       IFF((SELECT ABS(SUM(ALLOCATION) - MAX(TOTAL_CAPACITY))
              FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION) <= 1.00, 'PASS', 'FAIL')
UNION ALL
-- BP2: periods must tile the term with no gaps and no overlaps.
SELECT 15, 'BP2 periods contiguous',
       (SELECT COUNT(*)::VARCHAR FROM (
          SELECT PERIOD_END, LEAD(PERIOD_START) OVER (ORDER BY PERIOD_SEQ) AS NXT
          FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION)
        WHERE NXT IS NOT NULL AND NXT <> DATEADD(day,1,PERIOD_END)), '0',
       IFF((SELECT COUNT(*) FROM (
              SELECT PERIOD_END, LEAD(PERIOD_START) OVER (ORDER BY PERIOD_SEQ) AS NXT
              FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION)
            WHERE NXT IS NOT NULL AND NXT <> DATEADD(day,1,PERIOD_END))=0,'PASS','FAIL')
UNION ALL
-- BP3: period count must equal term months divided by the billing cadence.
--
-- This restates CONFIG.BILLING_SCHEDULE's rule instead of reading its output, so the
-- two can be compared independently -- which means it has to restate the rule
-- exactly, upfront case included: cadence NULL or 0 is one period spanning the term.
-- Dividing by NULLIF(cadence,0) instead made the expected value NULL for an upfront
-- contract, and NULL never equals the actual 1, so the check failed a pipeline that
-- was right. A verification check that cannot represent a legitimate input is not
-- protecting anything; it is manufacturing an alarm.
SELECT 16, 'BP3 period count matches cadence',
       (SELECT MAX(PERIOD_COUNT)::VARCHAR FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION),
       (SELECT TO_VARCHAR(
                 CASE WHEN SF360.ORDERFORM.FN_CADENCE_MONTHS(MAX(BILLING_FREQUENCY)) IS NULL
                        OR SF360.ORDERFORM.FN_CADENCE_MONTHS(MAX(BILLING_FREQUENCY)) = 0
                      THEN 1
                      ELSE GREATEST(CEIL(MAX(TERM_LENGTH_MONTHS)
                             / SF360.ORDERFORM.FN_CADENCE_MONTHS(MAX(BILLING_FREQUENCY))), 1)
                 END)
          FROM SF360.CONFIG.CONTRACT WHERE IS_ACTIVE),
       IFF((SELECT MAX(PERIOD_COUNT) FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION)
           = (SELECT CASE WHEN SF360.ORDERFORM.FN_CADENCE_MONTHS(MAX(BILLING_FREQUENCY)) IS NULL
                            OR SF360.ORDERFORM.FN_CADENCE_MONTHS(MAX(BILLING_FREQUENCY)) = 0
                          THEN 1
                          ELSE GREATEST(CEIL(MAX(TERM_LENGTH_MONTHS)
                                 / SF360.ORDERFORM.FN_CADENCE_MONTHS(MAX(BILLING_FREQUENCY))), 1)
                     END
              FROM SF360.CONFIG.CONTRACT WHERE IS_ACTIVE), 'PASS','FAIL')
UNION ALL
-- BP4: pooled remaining must reconcile to capacity minus cumulative consumption.
-- Guards the carryover arithmetic.
SELECT 17, 'BP4 pooled remaining reconciles',
       (SELECT TO_VARCHAR(ROUND(MAX(ABS(TOTAL_CAPACITY - CUM_CONSUMPTION - POOLED_REMAINING)),2))
          FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION), '0',
       IFF((SELECT MAX(ABS(TOTAL_CAPACITY - CUM_CONSUMPTION - POOLED_REMAINING))
              FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION) <= 0.02, 'PASS','FAIL')
UNION ALL
-- BP5: exactly one period may be current.
SELECT 18, 'BP5 one current period',
       (SELECT COALESCE(COUNT_IF(IS_CURRENT_PERIOD), 0)::VARCHAR FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION), '1',
       IFF((SELECT COALESCE(COUNT_IF(IS_CURRENT_PERIOD), 0) FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION)=1,
           'PASS','WARN')
UNION ALL
-- BP6: a period outside usage retention must report NULL consumption, never 0,
-- or the UI silently understates burn.
SELECT 19, 'BP6 unmeasurable periods are null',
       (SELECT COALESCE(COUNT_IF(DATA_COVERAGE='NO_DATA' AND CONSUMPTION IS NOT NULL), 0)::VARCHAR
          FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION), '0',
       IFF((SELECT COALESCE(COUNT_IF(DATA_COVERAGE='NO_DATA' AND CONSUMPTION IS NOT NULL), 0)
              FROM SF360.CURATED.FCT_BILLING_PERIOD_POSITION)=0,'PASS','FAIL')
UNION ALL
-- TH1: every active warning must carry a days-until, or the UI cannot state how
-- much lead time remains.
SELECT 20, 'TH1 warnings have lead time',
       (SELECT COALESCE(COUNT_IF(DAYS_UNTIL IS NULL), 0)::VARCHAR FROM SF360.CURATED.FCT_CAPACITY_WARNING), '0',
       IFF((SELECT COALESCE(COUNT_IF(DAYS_UNTIL IS NULL), 0) FROM SF360.CURATED.FCT_CAPACITY_WARNING)=0,
           'PASS','FAIL')
UNION ALL
-- TH2: threshold evaluation must resolve to a known state for every threshold.
-- COALESCE is load-bearing: COUNT_IF returns NULL over an empty set, not 0,
-- so with no thresholds configured this reported NULL <> 0 and failed. No
-- thresholds means nothing unevaluated, which is a pass.
SELECT 21, 'TH2 thresholds all evaluated',
       (SELECT COALESCE(COUNT_IF(STATUS NOT IN ('OK','FORECAST_BREACH','BREACHED')),0)::VARCHAR
          FROM SF360.CURATED.FCT_THRESHOLD_STATUS), '0',
       IFF((SELECT COALESCE(COUNT_IF(STATUS NOT IN ('OK','FORECAST_BREACH','BREACHED')),0)
              FROM SF360.CURATED.FCT_THRESHOLD_STATUS)=0,'PASS','FAIL')
UNION ALL
-- OF1: an extracted contract must retain lineage back to the uploaded document.
SELECT 22, 'OF1 extracted contract has provenance',
       (SELECT COALESCE(COUNT_IF(CONTRACT_SOURCE='ORDER_FORM_EXTRACTED' AND SOURCE_UPLOAD_ID IS NULL), 0)::VARCHAR
          FROM SF360.CONFIG.CONTRACT), '0',
       IFF((SELECT COALESCE(COUNT_IF(CONTRACT_SOURCE='ORDER_FORM_EXTRACTED' AND SOURCE_UPLOAD_ID IS NULL), 0)
              FROM SF360.CONFIG.CONTRACT)=0,'PASS','FAIL')
UNION ALL
-- OF2: the derived On Demand price must be at or above the contract price. A
-- lower value would understate the exhaustion price cliff.
SELECT 23, 'OF2 on demand price >= contract price',
       (SELECT COALESCE(COUNT_IF(ON_DEMAND_CREDIT_PRICE < CAPACITY_CREDIT_PRICE), 0)::VARCHAR
          FROM SF360.CONFIG.CONTRACT WHERE IS_ACTIVE), '0',
       IFF((SELECT COALESCE(COUNT_IF(ON_DEMAND_CREDIT_PRICE < CAPACITY_CREDIT_PRICE), 0)
              FROM SF360.CONFIG.CONTRACT WHERE IS_ACTIVE)=0,'PASS','FAIL');
