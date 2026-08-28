-- Snowflake360 :: Accept a reviewed order form into contract configuration
--
-- This is the human review gate's commit step. It reads REVIEWED_VALUE in
-- preference to NORMALIZED_VALUE, so whatever the reviewer confirmed or corrected
-- in the UI is what lands in CONFIG -- the model's output is never written
-- straight through.
--
-- The On Demand credit price is derived here rather than extracted, because order
-- forms state the discounted price and the discount, not the list price. Backing
-- out list from those two is exact: 2.61 / (1 - 0.13) = 3.00. That derived value
-- is what makes the price cliff quantifiable.

CREATE OR REPLACE PROCEDURE SF360.ORDERFORM.SP_ACCEPT_ORDER_FORM(P_UPLOAD_ID VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
COMMENT = 'Promote a reviewed order form extraction into CONFIG.CONTRACT as the active contract. Supersedes any previously active contract rather than deleting it.'
AS
$$
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
    RETURN 'BLOCKED: ' || :V_FAILS || ' required field(s) still empty. Fill them in review first.';
  END IF;

  -- One row per field, even if EXTRACTED somehow holds more than one.
  --
  -- OBJECT_AGG raises "Duplicate field key" on a repeated key. That surfaced in the
  -- UI as an opaque EXPRESSION_ERROR naming a line number inside this procedure,
  -- at the *end* of a long manual review -- the worst place to lose the work, and
  -- unactionable for whoever hits it. Extraction is transactional now, so the
  -- duplicates that caused it cannot recur, but activation refuses to be the step
  -- that breaks if one ever appears again: most recently extracted row wins.
  LET V_VALS OBJECT := (
    SELECT OBJECT_AGG(FIELD_NAME, VAL)
    FROM (
      SELECT FIELD_NAME,
             COALESCE(REVIEWED_VALUE, NORMALIZED_VALUE)::VARIANT AS VAL
      FROM SF360.ORDERFORM.EXTRACTED
      WHERE UPLOAD_ID = :P_UPLOAD_ID
        AND COALESCE(REVIEWED_VALUE, NORMALIZED_VALUE) IS NOT NULL
      QUALIFY ROW_NUMBER() OVER (PARTITION BY FIELD_NAME
                                 ORDER BY EXTRACTED_AT DESC) = 1
    )
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
    'CAPACITY_ORDER_FORM',
    TO_DATE(:V_VALS:term_start_date::VARCHAR),
    -- Term end is start + term months - 1 day, so the term is inclusive and the
    -- next term would begin on the anniversary.
    DATEADD(day, -1, DATEADD(month, :V_VALS:term_length_months::NUMBER,
                             TO_DATE(:V_VALS:term_start_date::VARCHAR))),
    DATEADD(day, -1, DATEADD(month, :V_VALS:term_length_months::NUMBER,
                             TO_DATE(:V_VALS:term_start_date::VARCHAR))),
    COALESCE(:V_VALS:currency::VARCHAR, 'USD'),
    :V_VALS:capacity_amount::DECIMAL(38,6),
    COALESCE(:V_VALS:credit_discount_pct::DECIMAL(38,6), 0),
    :V_VALS:capacity_billing_frequency::VARCHAR,
    :V_VALS:on_demand_billing_frequency::VARCHAR,
    :V_VALS:term_length_months::NUMBER,
    'POOLED',
    :V_VALS:capacity_credit_price::DECIMAL(38,6),
    -- Derive undiscounted On Demand price by backing the discount out. If the
    -- discount does apply to On Demand (rare), there is no cliff and the two
    -- prices are equal.
    CASE
      WHEN COALESCE(:V_VALS:discount_applies_to_on_demand::VARCHAR,'FALSE') = 'TRUE'
        THEN :V_VALS:capacity_credit_price::DECIMAL(38,6)
      WHEN COALESCE(:V_VALS:credit_discount_pct::DECIMAL(38,6),0) > 0
        THEN ROUND(:V_VALS:capacity_credit_price::DECIMAL(38,6)
                   / (1 - :V_VALS:credit_discount_pct::DECIMAL(38,6) / 100), 6)
      ELSE :V_VALS:capacity_credit_price::DECIMAL(38,6)
    END,
    COALESCE(:V_VALS:discount_applies_to_on_demand::VARCHAR,'FALSE') = 'TRUE',
    COALESCE(:V_VALS:invoice_pull_forward::VARCHAR,'TRUE') = 'TRUE',
    :V_VALS:storage_price_per_tb::DECIMAL(38,6),
    :V_VALS:storage_tier::VARCHAR,
    :V_VALS:edition::VARCHAR,
    :V_VALS:cloud_provider::VARCHAR,
    :V_VALS:region::VARCHAR,
    :V_VALS:payment_terms_days::NUMBER,
    'ORDER_FORM_EXTRACTED',
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

  -- Rebuild the curated layer before returning.
  --
  -- Every contract-keyed fact joins the term to DIM_DATE, so until curated is
  -- rebuilt none of them contain the contract that was just activated. The app
  -- reported that as "No contract position rows. The active contract term may not
  -- overlap available usage." -- a message describing a date-range problem, on an
  -- install whose term overlapped usage by two years. Activation appeared to
  -- succeed and the page was empty, which reads as the product being broken.
  --
  -- Clearing the Streamlit cache cannot fix this: the staleness is in the dynamic
  -- tables, not the client. It belongs here rather than in the UI so that any
  -- caller -- app, worksheet, task -- leaves the account in a consistent state.
  -- Activation is rare and deliberate, so paying the refresh cost inline is the
  -- right trade against shipping a page that looks broken.
  CALL SF360.CURATED.SP_REFRESH_CURATED();

  RETURN 'OK: contract ' || :V_NUM || ' active with ' || :V_PERIODS || ' billing periods';
END;
$$;
