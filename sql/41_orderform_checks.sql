-- Snowflake360 :: Automated cross-checks on extracted order form fields
--
-- These do not gate acceptance. The order form is the contractual source of
-- truth, so when it disagrees with RATE_SHEET_DAILY the order form wins --
-- negotiated pricing is real and routinely differs from list. The checks exist
-- to tell the reviewer *where to look*, because the one field that silently
-- failed in testing (Capacity vs On Demand billing frequency) looked entirely
-- plausible on its own.

CREATE OR REPLACE PROCEDURE SF360.ORDERFORM.SP_CHECK_EXTRACTION(P_UPLOAD_ID VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
COMMENT = 'Run automated consistency checks over ORDERFORM.EXTRACTED for one upload. Sets CHECK_STATUS to PASS/WARN/FAIL and writes a human-readable CHECK_DETAIL.'
AS
$$
DECLARE
  V_WARN NUMBER;
  V_FAIL NUMBER;
BEGIN
  -- Reset
  UPDATE SF360.ORDERFORM.EXTRACTED
     SET CHECK_STATUS = 'UNCHECKED', CHECK_DETAIL = NULL
   WHERE UPLOAD_ID = :P_UPLOAD_ID;

  -- (a) Required field missing or unparseable -> FAIL
  UPDATE SF360.ORDERFORM.EXTRACTED e
     SET CHECK_STATUS = 'FAIL',
         CHECK_DETAIL = 'Required field is missing or could not be parsed. Enter it manually.'
    FROM SF360.ORDERFORM.FIELD_SPEC s
   WHERE e.FIELD_NAME = s.FIELD_NAME
     AND e.UPLOAD_ID  = :P_UPLOAD_ID
     AND s.IS_REQUIRED
     AND e.NORMALIZED_VALUE IS NULL;

  -- (b) Optional field missing -> WARN
  UPDATE SF360.ORDERFORM.EXTRACTED e
     SET CHECK_STATUS = 'WARN',
         CHECK_DETAIL = 'Not found in the document. Optional, but confirm it is genuinely absent.'
    FROM SF360.ORDERFORM.FIELD_SPEC s
   WHERE e.FIELD_NAME = s.FIELD_NAME
     AND e.UPLOAD_ID  = :P_UPLOAD_ID
     AND NOT s.IS_REQUIRED
     AND e.NORMALIZED_VALUE IS NULL;

  -- (c) Billing frequency must be a cadence we can turn into periods.
  --     This is the field that failed extraction testing, so it gets an
  --     explicit callout rather than a generic pass.
  UPDATE SF360.ORDERFORM.EXTRACTED
     SET CHECK_STATUS = IFF(SF360.ORDERFORM.FN_CADENCE_MONTHS(NORMALIZED_VALUE) IS NULL,
                            'FAIL', 'WARN'),
         CHECK_DETAIL = IFF(SF360.ORDERFORM.FN_CADENCE_MONTHS(NORMALIZED_VALUE) IS NULL,
              'Unrecognized cadence. Expected Monthly, Quarterly, Semi-Annually or Annually.',
              'Confirm this is the Capacity Fees column, not On Demand Fees. '
           || 'These sit side by side in the same table row and are easily swapped.')
   WHERE UPLOAD_ID = :P_UPLOAD_ID
     AND FIELD_NAME = 'capacity_billing_frequency'
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
          MAX(IFF(FIELD_NAME='credit_discount_pct',   TRY_TO_DECIMAL(NORMALIZED_VALUE,38,6), NULL)) AS DISC,
          MAX(IFF(FIELD_NAME='capacity_credit_price', TRY_TO_DECIMAL(NORMALIZED_VALUE,38,6), NULL)) AS PRICE,
          MAX(IFF(FIELD_NAME='edition',               NORMALIZED_VALUE, NULL))                      AS EDITION
        FROM SF360.ORDERFORM.EXTRACTED
        WHERE UPLOAD_ID = :P_UPLOAD_ID
      ),
      lst AS (
        -- Effective list price for the extracted edition, from the org rate sheet.
        -- FCT_RATE_EFFECTIVE names the edition SERVICE_LEVEL, and platform credits
        -- are RATING_TYPE = 'COMPUTE' (Snowflake's own billing classification).
        SELECT AVG(r.EFFECTIVE_RATE) AS LIST_RATE
        FROM SF360.CURATED.FCT_RATE_EFFECTIVE r
        JOIN f ON UPPER(REPLACE(r.SERVICE_LEVEL,' ','_')) = UPPER(REPLACE(f.EDITION,' ','_'))
        WHERE r.RATING_TYPE = 'COMPUTE'
      )
      SELECT
        'capacity_credit_price' AS FIELD_NAME,
        CASE
          WHEN f.PRICE IS NULL OR f.DISC IS NULL OR lst.LIST_RATE IS NULL THEN 'WARN'
          WHEN ABS(f.PRICE - lst.LIST_RATE * (1 - f.DISC/100)) <= 0.01 THEN 'PASS'
          ELSE 'WARN'
        END AS STATUS,
        CASE
          WHEN f.PRICE IS NULL OR f.DISC IS NULL OR lst.LIST_RATE IS NULL
            THEN 'Could not cross-check: missing price, discount, or no rate sheet match for edition '
                 || COALESCE(f.EDITION,'(unknown)') || '.'
          WHEN ABS(f.PRICE - lst.LIST_RATE * (1 - f.DISC/100)) <= 0.01
            THEN 'Consistent: ' || TO_VARCHAR(lst.LIST_RATE,'999.00') || ' list x (1 - '
                 || TO_VARCHAR(f.DISC,'990.00') || '%) = ' || TO_VARCHAR(f.PRICE,'999.00') || '.'
          ELSE 'Does not match list minus discount (expected ~'
               || TO_VARCHAR(lst.LIST_RATE * (1 - f.DISC/100),'999.00')
               || ' from ' || TO_VARCHAR(lst.LIST_RATE,'999.00') || ' list). '
               || 'The order form governs, but verify the edition and discount were read correctly.'
        END AS DETAIL
      FROM f, lst
    ) v
   WHERE e.UPLOAD_ID = :P_UPLOAD_ID AND e.FIELD_NAME = v.FIELD_NAME;

  -- (e) Term length must divide evenly into the billing cadence, otherwise the
  --     installment schedule has a ragged final period.
  UPDATE SF360.ORDERFORM.EXTRACTED e
     SET CHECK_STATUS = v.STATUS,
         CHECK_DETAIL = v.DETAIL
    FROM (
      WITH f AS (
        SELECT
          MAX(IFF(FIELD_NAME='term_length_months', TRY_TO_NUMBER(NORMALIZED_VALUE), NULL)) AS MONTHS,
          MAX(IFF(FIELD_NAME='capacity_amount',    TRY_TO_DECIMAL(NORMALIZED_VALUE,38,6), NULL)) AS CAP,
          MAX(IFF(FIELD_NAME='capacity_billing_frequency', NORMALIZED_VALUE, NULL)) AS FREQ
        FROM SF360.ORDERFORM.EXTRACTED WHERE UPLOAD_ID = :P_UPLOAD_ID
      ),
      c AS (
        SELECT MONTHS, CAP, FREQ,
               SF360.ORDERFORM.FN_CADENCE_MONTHS(FREQ) AS CAD
        FROM f
      )
      SELECT 'term_length_months' AS FIELD_NAME,
        CASE WHEN MONTHS IS NULL OR CAD IS NULL THEN 'WARN'
             WHEN MOD(MONTHS, CAD) = 0 THEN 'PASS'
             ELSE 'WARN' END AS STATUS,
        CASE
          WHEN MONTHS IS NULL OR CAD IS NULL THEN 'Could not verify term against billing cadence.'
          WHEN MOD(MONTHS, CAD) = 0
            THEN MONTHS || ' months / ' || CAD || '-month cadence = '
                 || TO_VARCHAR(FLOOR(MONTHS/CAD))
                 || ' installments of '
                 || TRIM(TO_VARCHAR(CAP / FLOOR(MONTHS/CAD), '999,999,999.00')) || '.'
          ELSE MONTHS || ' months does not divide evenly by a ' || CAD
               || '-month cadence; the final installment will be a partial period.'
        END AS DETAIL
      FROM c
    ) v
   WHERE e.UPLOAD_ID = :P_UPLOAD_ID AND e.FIELD_NAME = v.FIELD_NAME;

  -- (f) Currency must match what the account actually bills in.
  UPDATE SF360.ORDERFORM.EXTRACTED e
     SET CHECK_STATUS = v.STATUS, CHECK_DETAIL = v.DETAIL
    FROM (
      SELECT 'currency' AS FIELD_NAME,
             IFF(x.OF_CUR = x.ACCT_CUR, 'PASS', 'WARN') AS STATUS,
             IFF(x.OF_CUR = x.ACCT_CUR,
                 'Matches billing currency ' || x.ACCT_CUR || '.',
                 'Order form says ' || COALESCE(x.OF_CUR,'(none)')
                 || ' but usage is billed in ' || COALESCE(x.ACCT_CUR,'(unknown)') || '.') AS DETAIL
      FROM (
        SELECT
          (SELECT UPPER(NORMALIZED_VALUE) FROM SF360.ORDERFORM.EXTRACTED
            WHERE UPLOAD_ID = :P_UPLOAD_ID AND FIELD_NAME = 'currency') AS OF_CUR,
          (SELECT MAX(CURRENCY) FROM SF360.CURATED.FCT_DAILY_CURRENCY)   AS ACCT_CUR
      ) x
    ) v
   WHERE e.UPLOAD_ID = :P_UPLOAD_ID AND e.FIELD_NAME = v.FIELD_NAME;

  -- Anything still unchecked and populated is a plain PASS.
  UPDATE SF360.ORDERFORM.EXTRACTED
     SET CHECK_STATUS = 'PASS', CHECK_DETAIL = 'Extracted.'
   WHERE UPLOAD_ID = :P_UPLOAD_ID
     AND CHECK_STATUS = 'UNCHECKED'
     AND NORMALIZED_VALUE IS NOT NULL;

  SELECT COUNT_IF(CHECK_STATUS='WARN'), COUNT_IF(CHECK_STATUS='FAIL')
    INTO :V_WARN, :V_FAIL
  FROM SF360.ORDERFORM.EXTRACTED WHERE UPLOAD_ID = :P_UPLOAD_ID;

  RETURN 'OK: ' || :V_FAIL || ' fail, ' || :V_WARN || ' warn';
END;
$$;
