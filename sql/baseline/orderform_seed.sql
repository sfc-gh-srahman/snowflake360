-- Snowflake360 :: ORDERFORM.FIELD_SPEC seed data.
--
-- These 18 rows are the order form extraction contract: for each field, the
-- prompt handed to AI_EXTRACT, whether a human must supply it before the contract
-- can be accepted, and the order it appears in the review UI.
--
-- This file exists because the rows were inserted ad hoc during development and
-- lived only inside the account. Nothing in the repository created them, so a
-- fresh install would have had an empty FIELD_SPEC and extraction would have
-- returned no fields at all -- with no error, because an empty spec is
-- indistinguishable from "nothing to extract".
--
-- Several prompts are column-qualified rather than plainly worded, and that is
-- load-bearing rather than verbose. An order form states capacity fees and On
-- Demand fees in adjacent columns of the same table, so an unqualified
-- "Billing Frequency" prompt returns the On Demand value while looking entirely
-- plausible -- and capacity billing frequency is the field the whole installment
-- schedule is derived from. The same trap produced the column header
-- "Cloud Provider" as the value for edition. Reword these only against a real
-- order form.
--
-- MERGE rather than INSERT so a tuned prompt reaches an existing install without
-- dropping the table, and so running this file twice is harmless.

USE DATABASE SF360;
USE SCHEMA ORDERFORM;

MERGE INTO SF360.ORDERFORM.FIELD_SPEC t
USING (
  SELECT * FROM VALUES
    ('customer_name', 'Customer',
     'the customer legal entity name',
     'VARCHAR', TRUE, 10, NULL),
    ('order_form_number', 'Order Form #',
     'the order form number',
     'VARCHAR', TRUE, 20, 'Maps to CONTRACT_NUMBER.'),
    ('term_start_date', 'Term Start',
     'subscription term start date in YYYY-MM-DD format',
     'DATE', TRUE, 30,
     'Shifts to first of month if the Order Form Effective Date falls in a different month.'),
    ('term_length_months', 'Term Length (months)',
     'subscription term length in months as an integer',
     'NUMBER', TRUE, 40, NULL),
    ('capacity_amount', 'Capacity',
     'total capacity commitment amount as a plain number with no currency symbol or commas',
     'NUMBER', TRUE, 50, NULL),
    ('currency', 'Currency',
     'the three letter currency code',
     'VARCHAR', TRUE, 60, NULL),
    ('credit_discount_pct', 'Credit Discount %',
     'the credit discount percentage as a plain number',
     'NUMBER', FALSE, 70,
     'Cross-checked against capacity_credit_price vs RATE_SHEET_DAILY list price.'),
    ('capacity_credit_price', 'Capacity Credit Price',
     'the capacity credit price per credit as a plain number',
     'NUMBER', TRUE, 80, NULL),
    ('storage_price_per_tb', 'Storage $/TB',
     'the capacity storage price per TB per month as a plain number',
     'NUMBER', FALSE, 90, NULL),
    ('storage_tier', 'Storage Tier',
     'the capacity storage tier',
     'VARCHAR', FALSE, 100, NULL),
    -- Column-qualified and enum-constrained: an unqualified prompt returned the
    -- header "Cloud Provider" from the adjacent column.
    ('edition', 'Edition',
     'In the Snowflake Service table, the value in the "Snowflake Service Edition" column. Must be exactly one of: Standard, Enterprise, Business Critical, Virtual Private Snowflake. Do not return a column header.',
     'VARCHAR', TRUE, 110,
     'Column-qualified and enum-constrained. An unqualified prompt returned the header "Cloud Provider" from the adjacent column.'),
    ('cloud_provider', 'Cloud Provider',
     'In the Snowflake Service table, the value in the "Cloud Provider" column, such as AWS, Azure or GCP. Do not return a column header.',
     'VARCHAR', FALSE, 120, NULL),
    ('region', 'Region',
     'In the Snowflake Service table, the value in the "Region" column. Do not return a column header.',
     'VARCHAR', FALSE, 130, NULL),
    -- The field that failed extraction testing. Capacity and On Demand fees sit in
    -- adjacent columns of one row, so the column qualifier is what makes this right.
    ('capacity_billing_frequency', 'Billing Frequency (Capacity)',
     'In the Payment and Billing Terms table, the value in the Billing Frequency row under the Capacity Fees column. Return only that one value.',
     'VARCHAR', TRUE, 140,
     'Column-qualified. An unqualified prompt returns the On Demand value instead. This field drives the installment schedule.'),
    ('on_demand_billing_frequency', 'Billing Frequency (On Demand)',
     'In the Payment and Billing Terms table, the value in the Billing Frequency row under the On Demand Fees column. Return only that one value.',
     'VARCHAR', FALSE, 150,
     'Column-qualified. Governs invoicing after capacity is exhausted.'),
    ('payment_terms_days', 'Payment Terms (days)',
     'the net payment terms in days as an integer',
     'NUMBER', FALSE, 160, NULL),
    ('discount_applies_to_on_demand', 'Discount Applies to On Demand',
     'true or false, whether the credit discount applies to On Demand pricing',
     'BOOLEAN', FALSE, 170,
     'Normally false. Drives the price cliff calculation at capacity exhaustion.'),
    ('invoice_pull_forward', 'Invoice Pull-Forward Allowed',
     'true or false, whether Snowflake may pull forward subsequent invoices if consumption exceeds an installment period amount',
     'BOOLEAN', FALSE, 180,
     'Drives pull-forward warnings.')
  AS v (FIELD_NAME, FIELD_LABEL, PROMPT, DATA_TYPE, IS_REQUIRED, DISPLAY_ORDER, NOTES)
) s
  ON t.FIELD_NAME = s.FIELD_NAME
WHEN MATCHED THEN UPDATE SET
  t.FIELD_LABEL   = s.FIELD_LABEL,
  t.PROMPT        = s.PROMPT,
  t.DATA_TYPE     = s.DATA_TYPE,
  t.IS_REQUIRED   = s.IS_REQUIRED,
  t.DISPLAY_ORDER = s.DISPLAY_ORDER,
  t.NOTES         = s.NOTES
WHEN NOT MATCHED THEN INSERT
  (FIELD_NAME, FIELD_LABEL, PROMPT, DATA_TYPE, IS_REQUIRED, DISPLAY_ORDER, NOTES)
  VALUES
  (s.FIELD_NAME, s.FIELD_LABEL, s.PROMPT, s.DATA_TYPE, s.IS_REQUIRED, s.DISPLAY_ORDER, s.NOTES);

-- Expect 18 rows, and every required field non-empty.
SELECT COUNT(*) AS FIELD_COUNT,
       COUNT_IF(IS_REQUIRED) AS REQUIRED_COUNT,
       COUNT_IF(PROMPT IS NULL OR TRIM(PROMPT) = '') AS EMPTY_PROMPTS
FROM SF360.ORDERFORM.FIELD_SPEC;
