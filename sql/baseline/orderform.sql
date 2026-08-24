-- Snowflake360 :: ORDERFORM schema, scripted from the live account.
--
-- Captured as a baseline because these objects were created ad hoc during
-- development and their definitions existed only inside the account. This file
-- is the reproducible record; the numbered files in sql/ remain the source of
-- truth for anything created after it.
--
-- Originally scripted from a live SF360 deployment, then hand-corrected.
--
-- The three tables use CREATE ... IF NOT EXISTS rather than the CREATE OR REPLACE
-- that GET_DDL emitted. RAW_UPLOAD and EXTRACTED hold the customer's own order
-- form and the values a human reviewed and accepted from it, so replacing them on
-- a re-run would discard the audit trail that links an accepted contract back to
-- the document it came from. FIELD_SPEC holds this app's extraction prompts and is
-- seeded by orderform_seed.sql, which MERGEs so a tuned prompt can be shipped to
-- an existing install without dropping the table.
--
-- The function, procedures and stage stay CREATE OR REPLACE: they are code and
-- configuration, not data.

USE DATABASE SF360;

CREATE SCHEMA IF NOT EXISTS SF360.ORDERFORM COMMENT='Order form ingestion: uploaded PDFs, AI extraction results, and human-accepted contract terms.';

-- The stage was created ad hoc during development and its definition existed only
-- inside the account, so it is recorded here with the two properties the feature
-- depends on. ENCRYPTION = SNOWFLAKE_SSE is required because AI_PARSE_DOCUMENT
-- cannot read a client-side encrypted file, and DIRECTORY = (ENABLE = TRUE) is
-- what the upload UI lists files from.
CREATE STAGE IF NOT EXISTS SF360.ORDERFORM.ORDER_FORMS
  DIRECTORY = (ENABLE = TRUE)
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
  COMMENT = 'Uploaded capacity order form PDFs. SSE is required by AI_PARSE_DOCUMENT; directory table lets the UI list files.';

CREATE TABLE IF NOT EXISTS SF360.ORDERFORM.EXTRACTED (
	UPLOAD_ID VARCHAR(16777216) NOT NULL,
	FIELD_NAME VARCHAR(16777216) NOT NULL,
	RAW_VALUE VARCHAR(16777216),
	NORMALIZED_VALUE VARCHAR(16777216),
	CHECK_STATUS VARCHAR(16777216) DEFAULT 'UNCHECKED',
	CHECK_DETAIL VARCHAR(16777216),
	REVIEWED_VALUE VARCHAR(16777216),
	WAS_EDITED BOOLEAN DEFAULT FALSE,
	EXTRACTED_AT TIMESTAMP_LTZ(9) DEFAULT CURRENT_TIMESTAMP(),
	constraint PK_EXTRACTED primary key (UPLOAD_ID, FIELD_NAME)
)COMMENT='One row per extracted field per upload. RAW_VALUE is what AI_EXTRACT returned; NORMALIZED_VALUE is typed/cleaned; REVIEWED_VALUE is what the human confirmed. CHECK_STATUS in PASS/WARN/FAIL/UNCHECKED from the automated cross-checks.'
;
CREATE TABLE IF NOT EXISTS SF360.ORDERFORM.FIELD_SPEC (
	FIELD_NAME VARCHAR(16777216) NOT NULL,
	FIELD_LABEL VARCHAR(16777216) NOT NULL,
	PROMPT VARCHAR(16777216) NOT NULL,
	DATA_TYPE VARCHAR(16777216) NOT NULL,
	IS_REQUIRED BOOLEAN DEFAULT TRUE,
	DISPLAY_ORDER NUMBER(38,0) NOT NULL,
	NOTES VARCHAR(16777216),
	constraint PK_FIELD_SPEC primary key (FIELD_NAME)
)COMMENT='Extraction schema for capacity order forms. Prompts are column-qualified for fields inside the two-column Payment and Billing Terms table, because an unqualified Billing Frequency prompt returns the On Demand column instead of Capacity.'
;
CREATE TABLE IF NOT EXISTS SF360.ORDERFORM.RAW_UPLOAD (
	UPLOAD_ID VARCHAR(16777216) NOT NULL DEFAULT UUID_STRING(),
	FILE_NAME VARCHAR(16777216) NOT NULL,
	STAGE_PATH VARCHAR(16777216) NOT NULL,
	FILE_SIZE_BYTES NUMBER(38,0),
	FILE_MD5 VARCHAR(16777216),
	UPLOADED_BY VARCHAR(16777216) DEFAULT CURRENT_USER(),
	UPLOADED_AT TIMESTAMP_LTZ(9) DEFAULT CURRENT_TIMESTAMP(),
	PARSE_STATUS VARCHAR(16777216) DEFAULT 'PENDING',
	PARSE_ERROR VARCHAR(16777216),
	PARSED_AT TIMESTAMP_LTZ(9),
	RAW_LAYOUT VARCHAR(16777216),
	PAGE_COUNT NUMBER(38,0),
	IS_ACCEPTED BOOLEAN DEFAULT FALSE,
	ACCEPTED_AT TIMESTAMP_LTZ(9),
	ACCEPTED_BY VARCHAR(16777216),
	constraint PK_RAW_UPLOAD primary key (UPLOAD_ID)
)COMMENT='One row per uploaded order form PDF. PARSE_STATUS in PENDING/PARSED/EXTRACTED/FAILED. FILE_MD5 lets the UI detect re-uploads of the same document.'
;
CREATE OR REPLACE FUNCTION SF360.ORDERFORM.FN_CADENCE_MONTHS("P_FREQ" VARCHAR)
RETURNS NUMBER(38,0)
LANGUAGE SQL
COMMENT='Map an order form billing frequency to a period length in months. Two distinct non-positive answers, and callers MUST tell them apart: 0 means upfront -- one payment covering the whole term, a real and common answer, especially on AWS Marketplace order forms. NULL means unrecognized. Neither may be used as a divisor: 0 raises Division by zero, and guarding with NULLIF only converts that into a NULL that compares false and reports a spurious failure. Branch on 0 explicitly and treat it as a single period spanning the term, which is what CONFIG.BILLING_SCHEDULE does.'
AS '
  CASE
    WHEN P_FREQ IS NULL THEN NULL
    WHEN REGEXP_LIKE(UPPER(P_FREQ), ''.*(MONTHLY|MONTH).*'') AND NOT REGEXP_LIKE(UPPER(P_FREQ), ''.*(6|SIX|SEMI).*'') THEN 1
    WHEN REGEXP_LIKE(UPPER(P_FREQ), ''.*(QUARTER).*'') THEN 3
    WHEN REGEXP_LIKE(UPPER(P_FREQ), ''.*(SEMI.?ANNUAL|BI.?ANNUAL|HALF.?YEAR|EVERY 6 MONTHS).*'') THEN 6
    WHEN REGEXP_LIKE(UPPER(P_FREQ), ''.*(ANNUAL|YEARLY|PER YEAR).*'') THEN 12
    WHEN REGEXP_LIKE(UPPER(P_FREQ), ''.*(UPFRONT|ADVANCE|ONE.?TIME|IN FULL).*'') THEN 0
    ELSE NULL
  END
';
CREATE OR REPLACE PROCEDURE SF360.ORDERFORM.SP_ACCEPT_ORDER_FORM("P_UPLOAD_ID" VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
COMMENT='Promote a reviewed order form extraction into CONFIG.CONTRACT as the active contract. Supersedes any previously active contract rather than deleting it.'
EXECUTE AS OWNER
AS '
DECLARE
  V_FAILS   NUMBER;
  V_SK      NUMBER;
  V_NUM     VARCHAR;
  V_PERIODS NUMBER;
BEGIN
  -- Refuse to commit while a required field is still failing. Warnings are fine;
  -- they are advisory and the reviewer has seen them.
  SELECT COUNT(*) INTO :V_FAILS
  FROM SF360.ORDERFORM.EXTRACTED e
  JOIN SF360.ORDERFORM.FIELD_SPEC s ON s.FIELD_NAME = e.FIELD_NAME
  WHERE e.UPLOAD_ID = :P_UPLOAD_ID
    AND s.IS_REQUIRED
    AND COALESCE(e.REVIEWED_VALUE, e.NORMALIZED_VALUE) IS NULL;

  IF (:V_FAILS > 0) THEN
    RETURN ''BLOCKED: '' || :V_FAILS || '' required field(s) still empty. Fill them in review first.'';
  END IF;

  LET V_VALS OBJECT := (
    SELECT OBJECT_AGG(FIELD_NAME, COALESCE(REVIEWED_VALUE, NORMALIZED_VALUE)::VARIANT)
    FROM SF360.ORDERFORM.EXTRACTED
    WHERE UPLOAD_ID = :P_UPLOAD_ID
      AND COALESCE(REVIEWED_VALUE, NORMALIZED_VALUE) IS NOT NULL
  );

  V_NUM := :V_VALS:order_form_number::VARCHAR;

  -- Retire the current active contract. Kept for history so prior positions
  -- remain reproducible.
  UPDATE SF360.CONFIG.CONTRACT
     SET IS_ACTIVE = FALSE,
         VALID_TO  = CURRENT_DATE(),
         UPDATED_AT = CURRENT_TIMESTAMP(),
         UPDATED_BY = CURRENT_USER()
   WHERE IS_ACTIVE = TRUE;

  SELECT COALESCE(MAX(CONTRACT_SK),0) + 1 INTO :V_SK FROM SF360.CONFIG.CONTRACT;

  INSERT INTO SF360.CONFIG.CONTRACT (
    CONTRACT_SK, CONTRACT_NUMBER, CUSTOMER_NAME, AGREEMENT_TYPE,
    CONTRACT_START_DATE, CONTRACT_END_DATE, EXPIRATION_DATE,
    METERED_CURRENCY, CAPACITY_PURCHASED, CAPACITY_DISCOUNT_PCT,
    BILLING_FREQUENCY, ON_DEMAND_BILLING_FREQUENCY, TERM_LENGTH_MONTHS,
    CARRYOVER_MODE, CAPACITY_CREDIT_PRICE, ON_DEMAND_CREDIT_PRICE,
    DISCOUNT_APPLIES_ON_DEMAND, INVOICE_PULL_FORWARD,
    STORAGE_PRICE_PER_TB, STORAGE_TIER, EDITION, CLOUD_PROVIDER, REGION_NAME,
    PAYMENT_TERMS_DAYS, CONTRACT_SOURCE, SOURCE_UPLOAD_ID,
    IS_ACTIVE, VALID_FROM
  )
  SELECT
    :V_SK,
    :V_VALS:order_form_number::VARCHAR,
    :V_VALS:customer_name::VARCHAR,
    ''CAPACITY_ORDER_FORM'',
    TO_DATE(:V_VALS:term_start_date::VARCHAR),
    -- Term end is start + term months - 1 day, so the term is inclusive and the
    -- next term would begin on the anniversary.
    DATEADD(day, -1, DATEADD(month, :V_VALS:term_length_months::NUMBER,
                             TO_DATE(:V_VALS:term_start_date::VARCHAR))),
    DATEADD(day, -1, DATEADD(month, :V_VALS:term_length_months::NUMBER,
                             TO_DATE(:V_VALS:term_start_date::VARCHAR))),
    COALESCE(:V_VALS:currency::VARCHAR, ''USD''),
    :V_VALS:capacity_amount::DECIMAL(38,6),
    COALESCE(:V_VALS:credit_discount_pct::DECIMAL(38,6), 0),
    :V_VALS:capacity_billing_frequency::VARCHAR,
    :V_VALS:on_demand_billing_frequency::VARCHAR,
    :V_VALS:term_length_months::NUMBER,
    ''POOLED'',
    :V_VALS:capacity_credit_price::DECIMAL(38,6),
    -- Derive undiscounted On Demand price by backing the discount out. If the
    -- discount does apply to On Demand (rare), there is no cliff and the two
    -- prices are equal.
    CASE
      WHEN COALESCE(:V_VALS:discount_applies_to_on_demand::VARCHAR,''FALSE'') = ''TRUE''
        THEN :V_VALS:capacity_credit_price::DECIMAL(38,6)
      WHEN COALESCE(:V_VALS:credit_discount_pct::DECIMAL(38,6),0) > 0
        THEN ROUND(:V_VALS:capacity_credit_price::DECIMAL(38,6)
                   / (1 - :V_VALS:credit_discount_pct::DECIMAL(38,6) / 100), 6)
      ELSE :V_VALS:capacity_credit_price::DECIMAL(38,6)
    END,
    COALESCE(:V_VALS:discount_applies_to_on_demand::VARCHAR,''FALSE'') = ''TRUE'',
    COALESCE(:V_VALS:invoice_pull_forward::VARCHAR,''TRUE'') = ''TRUE'',
    :V_VALS:storage_price_per_tb::DECIMAL(38,6),
    :V_VALS:storage_tier::VARCHAR,
    :V_VALS:edition::VARCHAR,
    :V_VALS:cloud_provider::VARCHAR,
    :V_VALS:region::VARCHAR,
    :V_VALS:payment_terms_days::NUMBER,
    ''ORDER_FORM_EXTRACTED'',
    :P_UPLOAD_ID,
    TRUE,
    CURRENT_DATE();

  UPDATE SF360.ORDERFORM.RAW_UPLOAD
     SET IS_ACCEPTED = TRUE,
         ACCEPTED_AT = CURRENT_TIMESTAMP(),
         ACCEPTED_BY = CURRENT_USER()
   WHERE UPLOAD_ID = :P_UPLOAD_ID;

  SELECT COUNT(*) INTO :V_PERIODS
  FROM SF360.CONFIG.BILLING_SCHEDULE WHERE CONTRACT_SK = :V_SK;

  RETURN ''OK: contract '' || :V_NUM || '' active with '' || :V_PERIODS || '' billing periods'';
END;
';
CREATE OR REPLACE PROCEDURE SF360.ORDERFORM.SP_CHECK_EXTRACTION("P_UPLOAD_ID" VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
COMMENT='Run automated consistency checks over ORDERFORM.EXTRACTED for one upload. Sets CHECK_STATUS to PASS/WARN/FAIL and writes a human-readable CHECK_DETAIL.'
EXECUTE AS OWNER
AS '
DECLARE
  V_WARN NUMBER;
  V_FAIL NUMBER;
BEGIN
  -- Reset
  UPDATE SF360.ORDERFORM.EXTRACTED
     SET CHECK_STATUS = ''UNCHECKED'', CHECK_DETAIL = NULL
   WHERE UPLOAD_ID = :P_UPLOAD_ID;

  -- (a) Required field missing or unparseable -> FAIL
  UPDATE SF360.ORDERFORM.EXTRACTED e
     SET CHECK_STATUS = ''FAIL'',
         CHECK_DETAIL = ''Required field is missing or could not be parsed. Enter it manually.''
    FROM SF360.ORDERFORM.FIELD_SPEC s
   WHERE e.FIELD_NAME = s.FIELD_NAME
     AND e.UPLOAD_ID  = :P_UPLOAD_ID
     AND s.IS_REQUIRED
     AND e.NORMALIZED_VALUE IS NULL;

  -- (b) Optional field missing -> WARN
  UPDATE SF360.ORDERFORM.EXTRACTED e
     SET CHECK_STATUS = ''WARN'',
         CHECK_DETAIL = ''Not found in the document. Optional, but confirm it is genuinely absent.''
    FROM SF360.ORDERFORM.FIELD_SPEC s
   WHERE e.FIELD_NAME = s.FIELD_NAME
     AND e.UPLOAD_ID  = :P_UPLOAD_ID
     AND NOT s.IS_REQUIRED
     AND e.NORMALIZED_VALUE IS NULL;

  -- (c) Billing frequency must be a cadence we can turn into periods.
  --     This is the field that failed extraction testing, so it gets an
  --     explicit callout rather than a generic pass.
  --
  --     Three outcomes, not two. Upfront (0) is a recognized, correct cadence and
  --     must not be told to double-check the column it read: an AWS Marketplace form
  --     legitimately says "Upfront and as provided below" under Capacity Fees, and
  --     advising the reader to go look for a recurring cadence sends them hunting
  --     for something that is not on the page.
  UPDATE SF360.ORDERFORM.EXTRACTED
     SET CHECK_STATUS = CASE
           WHEN SF360.ORDERFORM.FN_CADENCE_MONTHS(NORMALIZED_VALUE) IS NULL THEN ''FAIL''
           WHEN SF360.ORDERFORM.FN_CADENCE_MONTHS(NORMALIZED_VALUE) = 0    THEN ''PASS''
           ELSE ''WARN'' END,
         CHECK_DETAIL = CASE
           WHEN SF360.ORDERFORM.FN_CADENCE_MONTHS(NORMALIZED_VALUE) IS NULL
             THEN ''Unrecognized cadence. Expected Monthly, Quarterly, Semi-Annually, Annually, or an upfront/paid-in-advance term.''
           WHEN SF360.ORDERFORM.FN_CADENCE_MONTHS(NORMALIZED_VALUE) = 0
             THEN ''Billed upfront: the capacity is paid once and covers the full term, so there is no recurring installment schedule. Common on AWS Marketplace order forms.''
           ELSE ''Confirm this is the Capacity Fees column, not On Demand Fees. ''
             || ''These sit side by side in the same table row and are easily swapped.''
           END
   WHERE UPLOAD_ID = :P_UPLOAD_ID
     AND FIELD_NAME = ''capacity_billing_frequency''
     AND NORMALIZED_VALUE IS NOT NULL;

  -- (d) Credit discount vs credit price vs list price.
  --     list * (1 - discount) should equal the stated capacity credit price.
  --     For example: 3.00 * (1 - 0.13) = 2.61 exactly.
  UPDATE SF360.ORDERFORM.EXTRACTED e
     SET CHECK_STATUS = v.STATUS,
         CHECK_DETAIL = v.DETAIL
    FROM (
      WITH f AS (
        SELECT
          MAX(IFF(FIELD_NAME=''credit_discount_pct'',   TRY_TO_DECIMAL(NORMALIZED_VALUE,38,6), NULL)) AS DISC,
          MAX(IFF(FIELD_NAME=''capacity_credit_price'', TRY_TO_DECIMAL(NORMALIZED_VALUE,38,6), NULL)) AS PRICE,
          MAX(IFF(FIELD_NAME=''edition'',               NORMALIZED_VALUE, NULL))                      AS EDITION
        FROM SF360.ORDERFORM.EXTRACTED
        WHERE UPLOAD_ID = :P_UPLOAD_ID
      ),
      lst AS (
        -- Effective list price for the extracted edition, from the org rate sheet.
        -- FCT_RATE_EFFECTIVE names the edition SERVICE_LEVEL, and platform credits
        -- are RATING_TYPE = ''COMPUTE'' (Snowflake''s own billing classification).
        SELECT AVG(r.EFFECTIVE_RATE) AS LIST_RATE
        FROM SF360.CURATED.FCT_RATE_EFFECTIVE r
        JOIN f ON UPPER(REPLACE(r.SERVICE_LEVEL,'' '',''_'')) = UPPER(REPLACE(f.EDITION,'' '',''_''))
        WHERE r.RATING_TYPE = ''COMPUTE''
      )
      SELECT
        ''capacity_credit_price'' AS FIELD_NAME,
        CASE
          WHEN f.PRICE IS NULL OR f.DISC IS NULL OR lst.LIST_RATE IS NULL THEN ''WARN''
          WHEN ABS(f.PRICE - lst.LIST_RATE * (1 - f.DISC/100)) <= 0.01 THEN ''PASS''
          ELSE ''WARN''
        END AS STATUS,
        CASE
          WHEN f.PRICE IS NULL OR f.DISC IS NULL OR lst.LIST_RATE IS NULL
            THEN ''Could not cross-check: missing price, discount, or no rate sheet match for edition ''
                 || COALESCE(f.EDITION,''(unknown)'') || ''.''
          WHEN ABS(f.PRICE - lst.LIST_RATE * (1 - f.DISC/100)) <= 0.01
            THEN ''Consistent: '' || TO_VARCHAR(lst.LIST_RATE,''999.00'') || '' list x (1 - ''
                 || TO_VARCHAR(f.DISC,''990.00'') || ''%) = '' || TO_VARCHAR(f.PRICE,''999.00'') || ''.''
          ELSE ''Does not match list minus discount (expected ~''
               || TO_VARCHAR(lst.LIST_RATE * (1 - f.DISC/100),''999.00'')
               || '' from '' || TO_VARCHAR(lst.LIST_RATE,''999.00'') || '' list). ''
               || ''The order form governs, but verify the edition and discount were read correctly.''
        END AS DETAIL
      FROM f, lst
    ) v
   WHERE e.UPLOAD_ID = :P_UPLOAD_ID AND e.FIELD_NAME = v.FIELD_NAME;

  -- (e) Term length must divide evenly into the billing cadence, otherwise the
  --     installment schedule has a ragged final period.
  --
  --     CAD = 0 is upfront billing, not a missing value, and it is not a divisor.
  --     It gets its own arm before any arithmetic touches it: MOD(MONTHS, 0) raises
  --     Division by zero, which is what took this whole procedure down on an AWS
  --     Marketplace order form ("Billing Frequency: Upfront and as provided below").
  --     The answer for upfront is one installment covering the term, matching
  --     CONFIG.BILLING_SCHEDULE. CASE short-circuits, so the later arms are never
  --     evaluated when CAD is 0; MOD still carries a NULLIF in case a future plan
  --     evaluates more eagerly than the current one.
  UPDATE SF360.ORDERFORM.EXTRACTED e
     SET CHECK_STATUS = v.STATUS,
         CHECK_DETAIL = v.DETAIL
    FROM (
      WITH f AS (
        SELECT
          MAX(IFF(FIELD_NAME=''term_length_months'', TRY_TO_NUMBER(NORMALIZED_VALUE), NULL)) AS MONTHS,
          MAX(IFF(FIELD_NAME=''capacity_amount'',    TRY_TO_DECIMAL(NORMALIZED_VALUE,38,6), NULL)) AS CAP,
          MAX(IFF(FIELD_NAME=''capacity_billing_frequency'', NORMALIZED_VALUE, NULL)) AS FREQ
        FROM SF360.ORDERFORM.EXTRACTED WHERE UPLOAD_ID = :P_UPLOAD_ID
      ),
      c AS (
        SELECT MONTHS, CAP, FREQ,
               SF360.ORDERFORM.FN_CADENCE_MONTHS(FREQ) AS CAD
        FROM f
      )
      SELECT ''term_length_months'' AS FIELD_NAME,
        CASE WHEN MONTHS IS NULL OR CAD IS NULL THEN ''WARN''
             WHEN CAD = 0 THEN ''PASS''
             WHEN MOD(MONTHS, NULLIF(CAD,0)) = 0 THEN ''PASS''
             ELSE ''WARN'' END AS STATUS,
        CASE
          WHEN MONTHS IS NULL OR CAD IS NULL THEN ''Could not verify term against billing cadence.''
          WHEN CAD = 0
            THEN ''Paid upfront: a single installment of ''
                 || TRIM(TO_VARCHAR(CAP, ''999,999,999.00''))
                 || '' covering the whole '' || MONTHS
                 || ''-month term. There are no recurring installments to reconcile.''
          WHEN MOD(MONTHS, NULLIF(CAD,0)) = 0
            THEN MONTHS || '' months / '' || CAD || ''-month cadence = ''
                 || TO_VARCHAR(FLOOR(MONTHS/NULLIF(CAD,0)))
                 || '' installments of ''
                 || TRIM(TO_VARCHAR(CAP / NULLIF(FLOOR(MONTHS/NULLIF(CAD,0)),0), ''999,999,999.00'')) || ''.''
          ELSE MONTHS || '' months does not divide evenly by a '' || CAD
               || ''-month cadence; the final installment will be a partial period.''
        END AS DETAIL
      FROM c
    ) v
   WHERE e.UPLOAD_ID = :P_UPLOAD_ID AND e.FIELD_NAME = v.FIELD_NAME;

  -- (f) Currency must match what the account actually bills in.
  UPDATE SF360.ORDERFORM.EXTRACTED e
     SET CHECK_STATUS = v.STATUS, CHECK_DETAIL = v.DETAIL
    FROM (
      SELECT ''currency'' AS FIELD_NAME,
             IFF(x.OF_CUR = x.ACCT_CUR, ''PASS'', ''WARN'') AS STATUS,
             IFF(x.OF_CUR = x.ACCT_CUR,
                 ''Matches billing currency '' || x.ACCT_CUR || ''.'',
                 ''Order form says '' || COALESCE(x.OF_CUR,''(none)'')
                 || '' but usage is billed in '' || COALESCE(x.ACCT_CUR,''(unknown)'') || ''.'') AS DETAIL
      FROM (
        SELECT
          (SELECT UPPER(NORMALIZED_VALUE) FROM SF360.ORDERFORM.EXTRACTED
            WHERE UPLOAD_ID = :P_UPLOAD_ID AND FIELD_NAME = ''currency'') AS OF_CUR,
          (SELECT MAX(CURRENCY) FROM SF360.CURATED.FCT_DAILY_CURRENCY)   AS ACCT_CUR
      ) x
    ) v
   WHERE e.UPLOAD_ID = :P_UPLOAD_ID AND e.FIELD_NAME = v.FIELD_NAME;

  -- Anything still unchecked and populated is a plain PASS.
  UPDATE SF360.ORDERFORM.EXTRACTED
     SET CHECK_STATUS = ''PASS'', CHECK_DETAIL = ''Extracted.''
   WHERE UPLOAD_ID = :P_UPLOAD_ID
     AND CHECK_STATUS = ''UNCHECKED''
     AND NORMALIZED_VALUE IS NOT NULL;

  SELECT COUNT_IF(CHECK_STATUS=''WARN''), COUNT_IF(CHECK_STATUS=''FAIL'')
    INTO :V_WARN, :V_FAIL
  FROM SF360.ORDERFORM.EXTRACTED WHERE UPLOAD_ID = :P_UPLOAD_ID;

  RETURN ''OK: '' || :V_FAIL || '' fail, '' || :V_WARN || '' warn'';
END;
';
CREATE OR REPLACE PROCEDURE SF360.ORDERFORM.SP_EXTRACT_ORDER_FORM("P_UPLOAD_ID" VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
COMMENT='Parse and extract one uploaded order form into ORDERFORM.EXTRACTED. Idempotent: re-running replaces prior extraction rows for the upload.'
EXECUTE AS OWNER
AS '
DECLARE
  V_FILE_NAME VARCHAR;
  V_LAYOUT    VARCHAR;
  V_FIELDS    NUMBER;
BEGIN
  SELECT FILE_NAME INTO :V_FILE_NAME
  FROM SF360.ORDERFORM.RAW_UPLOAD
  WHERE UPLOAD_ID = :P_UPLOAD_ID;

  IF (:V_FILE_NAME IS NULL) THEN
    RETURN ''ERROR: upload_id not found'';
  END IF;

  -- 1. Layout-aware parse. LAYOUT preserves the table structure that the
  --    column-qualified prompts rely on; OCR mode flattens it and the
  --    Capacity/On Demand distinction is lost.
  BEGIN
    SELECT TO_VARCHAR(
             AI_PARSE_DOCUMENT(
               TO_FILE(''@SF360.ORDERFORM.ORDER_FORMS'', :V_FILE_NAME),
               {''mode'': ''LAYOUT''}
             ):content
           )
      INTO :V_LAYOUT;
  EXCEPTION
    WHEN OTHER THEN
      UPDATE SF360.ORDERFORM.RAW_UPLOAD
         SET PARSE_STATUS = ''FAILED'',
             PARSE_ERROR  = ''AI_PARSE_DOCUMENT: '' || SQLERRM,
             PARSED_AT    = CURRENT_TIMESTAMP()
       WHERE UPLOAD_ID = :P_UPLOAD_ID;
      RETURN ''ERROR: parse failed - '' || SQLERRM;
  END;

  UPDATE SF360.ORDERFORM.RAW_UPLOAD
     SET RAW_LAYOUT   = :V_LAYOUT,
         PARSE_STATUS = ''PARSED'',
         PARSED_AT    = CURRENT_TIMESTAMP(),
         PARSE_ERROR  = NULL
   WHERE UPLOAD_ID = :P_UPLOAD_ID;

  -- 2. Extract, then normalize per declared data type. RAW_VALUE is kept
  --    verbatim so the review UI can show what the model actually said next to
  --    the cleaned value.
  DELETE FROM SF360.ORDERFORM.EXTRACTED WHERE UPLOAD_ID = :P_UPLOAD_ID;

  INSERT INTO SF360.ORDERFORM.EXTRACTED
    (UPLOAD_ID, FIELD_NAME, RAW_VALUE, NORMALIZED_VALUE)
  WITH fmt AS (
    SELECT OBJECT_AGG(FIELD_NAME, PROMPT::VARIANT) AS F
    FROM SF360.ORDERFORM.FIELD_SPEC
  ),
  x AS (
    SELECT AI_EXTRACT(text => :V_LAYOUT, responseFormat => fmt.F) AS R
    FROM fmt
  ),
  flat AS (
    SELECT f.key AS FIELD_NAME, NULLIF(TRIM(TO_VARCHAR(f.value)), '''') AS RAW_VALUE
    FROM x, LATERAL FLATTEN(input => R:response) f
  )
  SELECT
    :P_UPLOAD_ID,
    s.FIELD_NAME,
    fl.RAW_VALUE,
    CASE s.DATA_TYPE
      WHEN ''NUMBER'' THEN
        -- Store a clean number, not raw decimal text. TO_VARCHAR on
        -- DECIMAL(38,6) yields "36.000000", which then surfaces verbatim in the
        -- review form. Integral values render as integers; fractional values keep
        -- only their significant decimals. Trailing zeros are stripped only when a
        -- decimal point is present, since stripping them from "360000" would
        -- silently turn it into "36".
        (WITH n AS (
           SELECT TRY_TO_DECIMAL(REGEXP_REPLACE(fl.RAW_VALUE, ''[^0-9.-]'', ''''), 38, 6) AS V
         )
         SELECT CASE
                  WHEN V IS NULL           THEN NULL
                  WHEN V = TRUNC(V)        THEN TO_VARCHAR(V::BIGINT)
                  ELSE REGEXP_REPLACE(TO_VARCHAR(V), ''0+$'', '''')
                END
         FROM n)
      WHEN ''DATE'' THEN
        TO_VARCHAR(
          COALESCE(
            TRY_TO_DATE(fl.RAW_VALUE, ''YYYY-MM-DD''),
            TRY_TO_DATE(fl.RAW_VALUE, ''DD MON YYYY''),
            TRY_TO_DATE(fl.RAW_VALUE, ''MON DD, YYYY''),
            TRY_TO_DATE(fl.RAW_VALUE, ''MM/DD/YYYY'')
          ), ''YYYY-MM-DD'')
      WHEN ''BOOLEAN'' THEN
        CASE WHEN LOWER(fl.RAW_VALUE) IN (''true'',''yes'',''y'',''1'') THEN ''TRUE''
             WHEN LOWER(fl.RAW_VALUE) IN (''false'',''no'',''n'',''0'') THEN ''FALSE''
             ELSE NULL END
      ELSE TRIM(fl.RAW_VALUE)
    END
  FROM SF360.ORDERFORM.FIELD_SPEC s
  LEFT JOIN flat fl ON fl.FIELD_NAME = s.FIELD_NAME;

  SELECT COUNT(*) INTO :V_FIELDS
  FROM SF360.ORDERFORM.EXTRACTED WHERE UPLOAD_ID = :P_UPLOAD_ID;

  UPDATE SF360.ORDERFORM.RAW_UPLOAD
     SET PARSE_STATUS = ''EXTRACTED''
   WHERE UPLOAD_ID = :P_UPLOAD_ID;

  CALL SF360.ORDERFORM.SP_CHECK_EXTRACTION(:P_UPLOAD_ID);

  RETURN ''OK: extracted '' || :V_FIELDS || '' fields'';
END;
';
