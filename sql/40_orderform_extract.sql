-- Snowflake360 :: Order form extraction pipeline
-- AI_PARSE_DOCUMENT (LAYOUT) -> AI_EXTRACT (FIELD_SPEC driven) -> ORDERFORM.EXTRACTED
--
-- Design notes:
--  * The stage must be SSE encrypted; AI_PARSE_DOCUMENT cannot read client-side
--    encrypted files, and it cannot read a gzipped PDF, so uploads use
--    AUTO_COMPRESS = FALSE.
--  * Prompts live in ORDERFORM.FIELD_SPEC rather than inline, so the extraction
--    schema is data and not code. Fields inside the two-column "Payment and
--    Billing Terms" table carry column-qualified prompts: an unqualified
--    "Billing Frequency" prompt returns the On Demand column ("Monthly in
--    Arrears") instead of the Capacity column ("Quarterly"), which is the field
--    the entire installment schedule depends on.
--  * Nothing here writes to CONFIG. Extraction lands in a staging table and a
--    human confirms it in the UI first.

CREATE OR REPLACE PROCEDURE SF360.ORDERFORM.SP_EXTRACT_ORDER_FORM(P_UPLOAD_ID VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
COMMENT = 'Parse and extract one uploaded order form into ORDERFORM.EXTRACTED. Idempotent: re-running replaces prior extraction rows for the upload.'
AS
$$
DECLARE
  V_FILE_NAME VARCHAR;
  V_LAYOUT    VARCHAR;
  V_FIELDS    NUMBER;
BEGIN
  SELECT FILE_NAME INTO :V_FILE_NAME
  FROM SF360.ORDERFORM.RAW_UPLOAD
  WHERE UPLOAD_ID = :P_UPLOAD_ID;

  IF (:V_FILE_NAME IS NULL) THEN
    RETURN 'ERROR: upload_id not found';
  END IF;

  -- 1. Layout-aware parse. LAYOUT preserves the table structure that the
  --    column-qualified prompts rely on; OCR mode flattens it and the
  --    Capacity/On Demand distinction is lost.
  BEGIN
    SELECT TO_VARCHAR(
             AI_PARSE_DOCUMENT(
               TO_FILE('@SF360.ORDERFORM.ORDER_FORMS', :V_FILE_NAME),
               {'mode': 'LAYOUT'}
             ):content
           )
      INTO :V_LAYOUT;
  EXCEPTION
    WHEN OTHER THEN
      UPDATE SF360.ORDERFORM.RAW_UPLOAD
         SET PARSE_STATUS = 'FAILED',
             PARSE_ERROR  = 'AI_PARSE_DOCUMENT: ' || SQLERRM,
             PARSED_AT    = CURRENT_TIMESTAMP()
       WHERE UPLOAD_ID = :P_UPLOAD_ID;
      RETURN 'ERROR: parse failed - ' || SQLERRM;
  END;

  UPDATE SF360.ORDERFORM.RAW_UPLOAD
     SET RAW_LAYOUT   = :V_LAYOUT,
         PARSE_STATUS = 'PARSED',
         PARSED_AT    = CURRENT_TIMESTAMP(),
         PARSE_ERROR  = NULL
   WHERE UPLOAD_ID = :P_UPLOAD_ID;

  -- 2. Extract, then normalize per declared data type. RAW_VALUE is kept
  --    verbatim so the review UI can show what the model actually said next to
  --    the cleaned value.
  --
  --    DELETE and INSERT are one transaction, not two statements. Autocommit made
  --    this idempotent only for *sequential* re-runs: two overlapping calls both
  --    deleted while the table was still empty, then both inserted, doubling every
  --    field. A double-clicked "Upload and extract" was enough to do it, and the
  --    damage stayed invisible until activation failed on OBJECT_AGG much later.
  --    Inside a transaction the second caller blocks on the first DELETE row lock
  --    until it commits, so the loser cleanly replaces the winner instead of
  --    stacking on top of it. Exactly one set of rows survives either ordering.
  BEGIN TRANSACTION;

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
    SELECT f.key AS FIELD_NAME, NULLIF(TRIM(TO_VARCHAR(f.value)), '') AS RAW_VALUE
    FROM x, LATERAL FLATTEN(input => R:response) f
  )
  SELECT
    :P_UPLOAD_ID,
    s.FIELD_NAME,
    fl.RAW_VALUE,
    CASE s.DATA_TYPE
      WHEN 'NUMBER' THEN
        -- Store a clean number, not raw decimal text. TO_VARCHAR on
        -- DECIMAL(38,6) yields "36.000000", which then surfaces verbatim in the
        -- review form. Integral values render as integers; fractional values keep
        -- only their significant decimals. Trailing zeros are stripped only when a
        -- decimal point is present, since stripping them from "360000" would
        -- silently turn it into "36".
        (WITH n AS (
           SELECT TRY_TO_DECIMAL(REGEXP_REPLACE(fl.RAW_VALUE, '[^0-9.-]', ''), 38, 6) AS V
         )
         SELECT CASE
                  WHEN V IS NULL           THEN NULL
                  WHEN V = TRUNC(V)        THEN TO_VARCHAR(V::BIGINT)
                  ELSE REGEXP_REPLACE(TO_VARCHAR(V), '0+$', '')
                END
         FROM n)
      WHEN 'DATE' THEN
        TO_VARCHAR(
          COALESCE(
            TRY_TO_DATE(fl.RAW_VALUE, 'YYYY-MM-DD'),
            TRY_TO_DATE(fl.RAW_VALUE, 'DD MON YYYY'),
            TRY_TO_DATE(fl.RAW_VALUE, 'MON DD, YYYY'),
            TRY_TO_DATE(fl.RAW_VALUE, 'MM/DD/YYYY')
          ), 'YYYY-MM-DD')
      WHEN 'BOOLEAN' THEN
        CASE WHEN LOWER(fl.RAW_VALUE) IN ('true','yes','y','1') THEN 'TRUE'
             WHEN LOWER(fl.RAW_VALUE) IN ('false','no','n','0') THEN 'FALSE'
             ELSE NULL END
      ELSE TRIM(fl.RAW_VALUE)
    END
  FROM SF360.ORDERFORM.FIELD_SPEC s
  LEFT JOIN flat fl ON fl.FIELD_NAME = s.FIELD_NAME;

  COMMIT;

  SELECT COUNT(*) INTO :V_FIELDS
  FROM SF360.ORDERFORM.EXTRACTED WHERE UPLOAD_ID = :P_UPLOAD_ID;

  UPDATE SF360.ORDERFORM.RAW_UPLOAD
     SET PARSE_STATUS = 'EXTRACTED'
   WHERE UPLOAD_ID = :P_UPLOAD_ID;

  CALL SF360.ORDERFORM.SP_CHECK_EXTRACTION(:P_UPLOAD_ID);

  RETURN 'OK: extracted ' || :V_FIELDS || ' fields';
END;
$$;
