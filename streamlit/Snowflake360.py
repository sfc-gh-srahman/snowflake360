"""Snowflake360 - Setup & Settings, and the app's landing page.

This is deliberately the landing page rather than a dashboard. Snowflake360 is
deployed by the customer into their own account, so the first thing a new
installation needs is configuration, not a chart. Nothing downstream can be
trusted until a contract exists, and the fastest way to get one is to upload the
order form, which is why that is the first tab.

Once configured, Active Contract is the day-to-day page.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.append(str(Path(__file__).parent))
from lib import sf  # noqa: E402
from lib.sf import (  # noqa: E402
    d,
    DB,
    freshness,
    get_conn,
    init_page,
    mi,
    money_column_config,
    q,
    scope_accounts,
    section,
    sql_str,
    table_with_download,
    usd,
)

st.set_page_config(
    page_title="Setup & Settings | Snowflake360",
    page_icon=mi("analytics"),
    layout="wide",
    initial_sidebar_state="expanded",
)
PAGE = "Settings"
init_page("Setup & Settings")
st.caption(
    "Everything Snowflake360 needs from you lives here. Start by uploading your order "
    "form; after that, in steady state the only change is the contract capacity and "
    "end date at renewal."
)


def exec_write(sql: str) -> None:
    """Run a write statement outside the cached read path."""
    kind, handle = get_conn()
    if kind == "snowpark":
        handle.sql(sql).collect()
    else:
        cur = handle.cursor()
        try:
            cur.execute(sql)
        finally:
            cur.close()
    st.cache_data.clear()


tabs = st.tabs(
    ["Order form", "Contract", "Billing & thresholds", "Rates", "Account scope",
     "Refresh & alerts"]
)


def coalesce(*vals) -> str:
    """First genuinely present value, as a string.

    A plain `a or b` cannot be used here. Pandas represents a NULL VARCHAR column
    as float NaN, and NaN is truthy in Python, so `nan or "x"` returns NaN and
    str() turns it into the literal "nan". That would pre-fill every review field
    with "nan" and write it straight back on save.
    """
    for v in vals:
        if v is None:
            continue
        try:
            if pd.isna(v):
                continue
        except (TypeError, ValueError):
            pass
        s = str(v).strip()
        if s and s.lower() != "nan":
            return s
    return ""


# ---------------------------------------------------------------------------
# Order form: upload, extract, review, activate
#
# First tab because it is the fastest path from a blank installation to a
# working one. Extraction is good but not perfect -- on a real order form 17 of
# 18 fields were right first pass, and the one failure (reading the On Demand
# column of the billing frequency row instead of the Capacity column) looked
# entirely plausible in isolation. Hence the review gate: nothing reaches CONFIG
# until a human confirms it.
# ---------------------------------------------------------------------------
with tabs[0]:
    st.caption(
        "Upload your Snowflake capacity order form. The app extracts contract terms, "
        "cross-checks them against your rate sheet, and asks you to confirm before "
        "using them. Prefer to type the terms in yourself? Use the Contract tab."
    )

    section("Upload")

    uploaded = st.file_uploader(
        "Capacity order form (PDF)",
        type=["pdf"],
        help="Stored in an encrypted internal stage inside the SF360 database. "
             "The file never leaves your account.",
    )

    if uploaded is not None and st.button("Upload and extract", type="primary"):
        with st.status("Processing order form", expanded=True) as status:
            try:
                st.write("Uploading to stage...")
                safe_name = sf.upload_to_stage(uploaded.name, uploaded.getvalue())

                # The directory table is what the registry reads from, and it is
                # not automatically current after a PUT.
                sf.exec_sql(
                    f"ALTER STAGE {DB}.ORDERFORM.ORDER_FORMS REFRESH",
                    page=PAGE, query_name="refresh_stage",
                )

                st.write("Registering upload...")
                sf.exec_sql(
                    f"""
                    INSERT INTO {DB}.ORDERFORM.RAW_UPLOAD
                      (FILE_NAME, STAGE_PATH, FILE_SIZE_BYTES, FILE_MD5)
                    SELECT RELATIVE_PATH,
                           '{sf.ORDER_FORM_STAGE}/' || RELATIVE_PATH, SIZE, MD5
                    FROM DIRECTORY({sf.ORDER_FORM_STAGE})
                    WHERE RELATIVE_PATH = {sf.sql_str(safe_name)}
                      AND RELATIVE_PATH NOT IN (
                        SELECT FILE_NAME FROM {DB}.ORDERFORM.RAW_UPLOAD)
                    """,
                    page=PAGE, query_name="register_upload",
                )

                st.write("Parsing and extracting with Cortex AI...")
                rows = sf.exec_sql(
                    f"""
                    CALL {DB}.ORDERFORM.SP_EXTRACT_ORDER_FORM(
                      (SELECT UPLOAD_ID FROM {DB}.ORDERFORM.RAW_UPLOAD
                        WHERE FILE_NAME = {sf.sql_str(safe_name)}
                        ORDER BY UPLOADED_AT DESC LIMIT 1))
                    """,
                    page=PAGE, query_name="extract_order_form",
                )
                result = rows[0][0] if rows else "no result"
                if str(result).startswith("ERROR"):
                    status.update(label="Extraction failed", state="error")
                    st.error(result)
                else:
                    status.update(label=f"Done - {result}", state="complete")
                    st.cache_data.clear()
            except Exception as exc:  # surfaced rather than swallowed
                status.update(label="Upload failed", state="error")
                st.error(str(exc))

    uploads = sf.q(
        f"""
        SELECT UPLOAD_ID, FILE_NAME, UPLOADED_AT, UPLOADED_BY,
               PARSE_STATUS, PARSE_ERROR, IS_ACCEPTED, ACCEPTED_AT,
               ROUND(FILE_SIZE_BYTES/1024.0, 1) AS SIZE_KB
        FROM {DB}.ORDERFORM.RAW_UPLOAD
        ORDER BY UPLOADED_AT DESC
        """,
        page=PAGE, query_name="upload_history",
    )

    if uploads.empty:
        st.info(
            "No order forms uploaded yet. Upload one above, or enter your contract "
            "terms by hand on the Contract tab."
        )
    else:
        section("Review and confirm")

        labels = {
            r.UPLOAD_ID: (
                f"{r.FILE_NAME}  -  {sf.display_ts(r.UPLOADED_AT)}"
                f"{'  (active contract)' if r.IS_ACCEPTED else ''}"
            )
            for r in uploads.itertuples()
        }
        choice = st.selectbox(
            "Uploaded order form",
            options=list(labels.keys()),
            format_func=lambda k: labels[k],
        )
        row = uploads[uploads.UPLOAD_ID == choice].iloc[0]

        if row.PARSE_STATUS == "FAILED":
            st.error(f"Parsing failed: {row.PARSE_ERROR}")
            fields = pd.DataFrame()
        else:
            fields = sf.q(
                f"""
                SELECT s.FIELD_NAME, s.FIELD_LABEL, s.DATA_TYPE, s.IS_REQUIRED, s.NOTES,
                       e.RAW_VALUE, e.NORMALIZED_VALUE, e.REVIEWED_VALUE,
                       e.CHECK_STATUS, e.CHECK_DETAIL
                FROM {DB}.ORDERFORM.EXTRACTED e
                JOIN {DB}.ORDERFORM.FIELD_SPEC s ON s.FIELD_NAME = e.FIELD_NAME
                WHERE e.UPLOAD_ID = {sf.sql_str(choice)}
                ORDER BY s.DISPLAY_ORDER
                """,
                page=PAGE, query_name="extracted_fields",
            )

        if fields.empty:
            st.warning("Nothing extracted for this upload yet.")
        else:
            n_fail = int((fields.CHECK_STATUS == "FAIL").sum())
            n_warn = int((fields.CHECK_STATUS == "WARN").sum())
            n_pass = int((fields.CHECK_STATUS == "PASS").sum())

            k1, k2, k3 = st.columns(3)
            k1.metric("Confirmed", n_pass)
            k2.metric("Needs a look", n_warn, delta_color="off")
            k3.metric("Must fix", n_fail, delta_color="off")

            if n_fail:
                st.error(
                    f"{n_fail} required field could not be read. Fill it in below "
                    "before accepting."
                )
            elif n_warn:
                st.warning(
                    f"{n_warn} field needs confirmation. These are usually correct, but "
                    "the billing frequency in particular is worth checking against the "
                    "document: capacity and On Demand fees sit in adjacent columns of "
                    "the same row, and reading the wrong one changes the whole schedule."
                )

            with st.form("review_order_form"):
                st.caption(
                    "Values below are pre-filled from the document. Edit anything that "
                    "is wrong. What you confirm here is what the app uses - the "
                    "extracted value is never written straight through."
                )
                edits: dict[str, str] = {}
                for f in fields.itertuples():
                    current = coalesce(f.REVIEWED_VALUE, f.NORMALIZED_VALUE)
                    left, right = st.columns([2, 3])
                    with left:
                        label = f.FIELD_LABEL + (" *" if f.IS_REQUIRED else "")
                        edits[f.FIELD_NAME] = st.text_input(
                            label, value=current, key=f"fld_{f.FIELD_NAME}"
                        )
                    with right:
                        tone = {"FAIL": st.error, "WARN": st.warning}.get(
                            f.CHECK_STATUS, st.caption
                        )
                        detail = coalesce(f.CHECK_DETAIL)
                        raw = coalesce(f.RAW_VALUE)
                        if raw and raw != current:
                            detail += f'  Document read: "{raw}".'
                        if tone is st.caption:
                            st.caption(detail)
                        else:
                            tone(detail)

                saved = st.form_submit_button("Save corrections", type="secondary")

            if saved:
                try:
                    for name, val in edits.items():
                        orig = fields[fields.FIELD_NAME == name].iloc[0]
                        baseline = coalesce(orig.REVIEWED_VALUE, orig.NORMALIZED_VALUE)
                        if str(val).strip() != baseline:
                            sf.exec_sql(
                                f"""
                                UPDATE {DB}.ORDERFORM.EXTRACTED
                                   SET REVIEWED_VALUE = {sf.sql_str(val)}, WAS_EDITED = TRUE
                                 WHERE UPLOAD_ID = {sf.sql_str(choice)}
                                   AND FIELD_NAME = {sf.sql_str(name)}
                                """,
                                page=PAGE, query_name="save_review",
                            )
                    st.cache_data.clear()
                    st.success("Corrections saved.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

            section("Activate this contract")

            vals = {
                f.FIELD_NAME: coalesce(f.REVIEWED_VALUE, f.NORMALIZED_VALUE)
                for f in fields.itertuples()
            }
            cadence = vals.get("capacity_billing_frequency") or "?"
            months = vals.get("term_length_months") or "?"
            cap = vals.get("capacity_amount")
            cur_code = vals.get("currency") or ""

            try:
                per_count = int(float(months) / {"Monthly": 1, "Quarterly": 3,
                                                 "Semi-Annually": 6, "Annually": 12}[cadence])
                per_amt = float(cap) / per_count
                st.info(
                    f"This will create **{per_count} {cadence.lower()} installments** of "
                    f"**{cur_code} {per_amt:,.2f}** across a **{int(float(months))}-month** "
                    f"term, totalling {cur_code} {float(cap):,.2f}."
                )
            except (ValueError, KeyError, TypeError, ZeroDivisionError):
                st.caption(
                    "Confirm term length, capacity and billing frequency to preview "
                    "the schedule."
                )

            if row.IS_ACCEPTED:
                st.success(f"Active since {sf.display_ts(row.ACCEPTED_AT)}.")

            st.caption(
                "Activating supersedes the current contract. The previous one is retained "
                "so historical positions stay reproducible."
            )
            if st.button("Accept and activate", type="primary", disabled=bool(n_fail)):
                try:
                    rows = sf.exec_sql(
                        f"CALL {DB}.ORDERFORM.SP_ACCEPT_ORDER_FORM({sf.sql_str(choice)})",
                        page=PAGE, query_name="accept_order_form",
                    )
                    msg = rows[0][0] if rows else ""
                    if str(msg).startswith("BLOCKED"):
                        st.error(msg)
                    else:
                        st.cache_data.clear()
                        st.success(msg)
                        st.rerun()
                except Exception as exc:
                    st.error(str(exc))

    with st.expander("What the app does with these values"):
        st.markdown(
            """
- **Term start, term length, billing frequency** generate the installment
  schedule, anniversary-aligned to the term start rather than calendar quarters.
- **Capacity** divided by the installment count gives each period's allocation.
- **Credit discount and capacity credit price** are cross-checked against your
  rate sheet, and the undiscounted On Demand price is derived by backing the
  discount out. The gap between the two is the price increase that applies once
  capacity is exhausted.
- **Storage price** and **edition** are used to reconcile metered usage.
- Where the order form disagrees with your rate sheet, the order form wins.
  Negotiated pricing routinely differs from list.
"""
        )

# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------
with tabs[1]:
    section("Contract terms")
    contracts = q(
        f"""SELECT CONTRACT_SK, CONTRACT_NUMBER, AGREEMENT_TYPE, CONTRACT_START_DATE,
                   CONTRACT_END_DATE, CAPACITY_PURCHASED, ADDITIONAL_CAPACITY,
                   TOTAL_FREE_USAGE, ROLLOVER, ADJUSTMENT, BALANCE_TRANSFER,
                   CURRENCY_CONVERSION_ADJUSTMENT, DATA_SHARING_REBATE, BALANCE_MIGRATION,
                   COMPUTE_SUPPORT_ADJUSTMENT, STORAGE_SUPPORT_ADJUSTMENT,
                   METERED_CURRENCY, CONTRACT_SOURCE, IS_ACTIVE
            FROM {DB}.CONFIG.CONTRACT ORDER BY CONTRACT_START_DATE DESC""",
        PAGE,
        "contracts",
    )

    prefilled = contracts[contracts["CONTRACT_SOURCE"] == "PREFILLED_FROM_CONTRACT_ITEMS"]
    if not prefilled.empty:
        st.success(
            f"{len(prefilled)} contract(s) prefilled from "
            "`ORGANIZATION_USAGE.CONTRACT_ITEMS`.",
            icon=mi("cloud_download"),
        )
        if not prefilled["IS_ACTIVE"].any():
            st.warning(
                "None of the prefilled contracts is currently active — the most recent ended "
                f"{d(prefilled['CONTRACT_END_DATE'].max())}. Snowflake has no current contract "
                "record for this organization, so the active contract below must be "
                "customer-entered.",
                icon=mi("warning"),
            )

    table_with_download(
        contracts.drop(columns=["CONTRACT_SK"]),
        "snowflake360_settings_1", "settings_1",
    )

    st.markdown("**Edit the active contract**")
    active = contracts[contracts["IS_ACTIVE"] & contracts["CONTRACT_SOURCE"].notna()]
    if active.empty:
        st.info(
            "No contract configured yet. Fill in the form below and save to create one, "
            "or upload an order form on the **Order form** tab to have the terms extracted "
            "for you. Nothing else in Snowflake360 is meaningful until a contract exists.",
            icon=mi("info"),
        )
        a = None
    else:
        a = active.iloc[0]

    with st.form("contract_form"):
        c1, c2, c3 = st.columns(3)
        num_in = c1.text_input("Contract number", value=str(a["CONTRACT_NUMBER"]) if a is not None else "")
        start_in = c2.date_input(
            "Term start", value=pd.Timestamp(a["CONTRACT_START_DATE"]).date() if a is not None else None
        )
        end_in = c3.date_input(
            "Term end", value=pd.Timestamp(a["CONTRACT_END_DATE"]).date() if a is not None else None
        )

        c4, c5, c6 = st.columns(3)
        cap_in = c4.number_input(
            "Capacity purchased", min_value=0.0, step=1000.0,
            value=float(a["CAPACITY_PURCHASED"]) if a is not None else 0.0,
        )
        free_in = c5.number_input(
            "Total free usage", min_value=0.0, step=100.0,
            value=float(a["TOTAL_FREE_USAGE"]) if a is not None else 0.0,
        )
        roll_in = c6.number_input(
            "Rollover", min_value=0.0, step=100.0,
            value=float(a["ROLLOVER"]) if a is not None else 0.0,
        )

        c7, c8, c9 = st.columns(3)
        adj_in = c7.number_input("Adjustment", step=100.0,
                                 value=float(a["ADJUSTMENT"]) if a is not None else 0.0)
        bt_in = c8.number_input("Balance transfer", step=100.0,
                                value=float(a["BALANCE_TRANSFER"]) if a is not None else 0.0)
        dsr_in = c9.number_input("Data sharing rebate", step=100.0,
                                 value=float(a["DATA_SHARING_REBATE"]) if a is not None else 0.0)

        c10, c11, c12 = st.columns(3)
        cca_in = c10.number_input(
            "Currency conversion adjustment", step=100.0,
            value=float(a["CURRENCY_CONVERSION_ADJUSTMENT"]) if a is not None else 0.0,
        )
        bm_in = c11.number_input("Balance migration", step=100.0,
                                 value=float(a["BALANCE_MIGRATION"]) if a is not None else 0.0)
        cur_in = c12.text_input("Currency",
                                value=str(a["METERED_CURRENCY"]) if a is not None else "USD")

        submitted = st.form_submit_button(
            "Save contract" if a is not None else "Create contract", type="primary"
        )

    if submitted:
        errors = []
        if not num_in.strip():
            errors.append("Contract number is required.")
        if not start_in or not end_in:
            errors.append("Term start and term end are both required.")
        if start_in and end_in and start_in >= end_in:
            errors.append("Term start must be before term end.")
        if cap_in <= 0:
            errors.append("Capacity purchased must be greater than zero.")
        if errors:
            for e in errors:
                st.error(e)
        elif a is None:
            # INSERT, not UPDATE. This branch did not exist: the form could only
            # ever edit a contract that already came from an order form, so on a
            # fresh install the only documented way to configure the app by hand
            # failed with a TypeError on a None row. Manual entry is the primary
            # path for anyone whose region has no Cortex, so it has to work.
            #
            # VALID_FROM/VALID_TO and IS_ACTIVE are the versioning columns: a new
            # contract is valid from today with no end, and superseding one later
            # closes it rather than deleting it, so historical positions stay
            # reproducible.
            exec_write(
                f"""INSERT INTO {DB}.CONFIG.CONTRACT (
                      CONTRACT_NUMBER, CONTRACT_START_DATE, CONTRACT_END_DATE,
                      CAPACITY_PURCHASED, TOTAL_FREE_USAGE, ROLLOVER, ADJUSTMENT,
                      BALANCE_TRANSFER, DATA_SHARING_REBATE,
                      CURRENCY_CONVERSION_ADJUSTMENT, BALANCE_MIGRATION,
                      METERED_CURRENCY, CONTRACT_SOURCE, IS_ACTIVE, VALID_FROM
                    )
                    SELECT {sql_str(num_in.strip())}, '{start_in}', '{end_in}',
                           {cap_in}, {free_in}, {roll_in}, {adj_in},
                           {bt_in}, {dsr_in}, {cca_in}, {bm_in},
                           {sql_str(cur_in.strip().upper() or 'USD')}, 'CUSTOMER_ENTERED',
                           TRUE, CURRENT_DATE()"""
            )
            st.success(
                "Contract created. Enter your negotiated rates on the **Rates** tab, then "
                "run the refresh from **Refresh & alerts** so projections pick it up."
            )
            st.rerun()
        else:
            exec_write(
                f"""UPDATE {DB}.CONFIG.CONTRACT SET
                      CONTRACT_NUMBER = {sql_str(num_in.strip())},
                      CONTRACT_START_DATE = '{start_in}',
                      CONTRACT_END_DATE = '{end_in}',
                      CAPACITY_PURCHASED = {cap_in},
                      TOTAL_FREE_USAGE = {free_in},
                      ROLLOVER = {roll_in},
                      ADJUSTMENT = {adj_in},
                      BALANCE_TRANSFER = {bt_in},
                      DATA_SHARING_REBATE = {dsr_in},
                      CURRENCY_CONVERSION_ADJUSTMENT = {cca_in},
                      BALANCE_MIGRATION = {bm_in},
                      METERED_CURRENCY = {sql_str(cur_in.strip().upper() or 'USD')},
                      CONTRACT_SOURCE = 'CUSTOMER_ENTERED',
                      UPDATED_BY = CURRENT_USER(),
                      UPDATED_AT = CURRENT_TIMESTAMP()
                    WHERE CONTRACT_SK = {int(a['CONTRACT_SK'])}"""
            )
            st.success(
                "Contract saved. Run the refresh task or wait for the 11:00 UTC run for "
                "projections to pick it up."
            )
            st.rerun()

    # -----------------------------------------------------------------------
    # Commercial terms
    #
    # These are the fields the order form extraction identifies, exposed for
    # manual entry so the app is fully usable without a PDF. They are separated
    # from the balance fields above because they drive different things: these
    # generate the installment schedule and the price cliff, the ones above
    # compose total capacity.
    # -----------------------------------------------------------------------
    section("Commercial terms")

    if a is None:
        st.info("Save an active contract above before entering commercial terms.")
    else:
        terms = q(
            f"""SELECT CUSTOMER_NAME, BILLING_FREQUENCY, ON_DEMAND_BILLING_FREQUENCY,
                       TERM_LENGTH_MONTHS, CAPACITY_CREDIT_PRICE, ON_DEMAND_CREDIT_PRICE,
                       CAPACITY_DISCOUNT_PCT, DISCOUNT_APPLIES_ON_DEMAND,
                       INVOICE_PULL_FORWARD, EDITION, CLOUD_PROVIDER, REGION_NAME,
                       STORAGE_PRICE_PER_TB, STORAGE_TIER, PAYMENT_TERMS_DAYS
                FROM {DB}.CONFIG.CONTRACT WHERE CONTRACT_SK = {int(a['CONTRACT_SK'])}""",
            PAGE, "commercial_terms",
        )
        t = terms.iloc[0] if not terms.empty else None

        CADENCES = ["Monthly", "Quarterly", "Semi-Annually", "Annually", "Upfront"]
        EDITIONS = ["Standard", "Enterprise", "Business Critical", "Virtual Private Snowflake"]

        def _idx(options, value, default=0):
            """Index of value in options, or a default. Tolerates NULL and casing."""
            v = coalesce(value)
            for i, o in enumerate(options):
                if o.lower() == v.lower():
                    return i
            return default

        def _f(value, default=0.0):
            v = coalesce(value)
            try:
                return float(v)
            except ValueError:
                return default

        with st.form("terms_form"):
            t1, t2, t3 = st.columns(3)
            cust_in = t1.text_input(
                "Customer name", value=coalesce(t["CUSTOMER_NAME"]) if t is not None else "",
                help="Shown in the header of every page.",
            )
            cad_in = t2.selectbox(
                "Capacity billing frequency", CADENCES,
                index=_idx(CADENCES, t["BILLING_FREQUENCY"] if t is not None else None, 1),
                help="Drives the installment schedule. Anniversary-aligned to the term "
                     "start, not to calendar quarters.",
            )
            od_cad_in = t3.selectbox(
                "On Demand billing frequency", CADENCES + ["Monthly in Arrears"],
                index=_idx(CADENCES + ["Monthly in Arrears"],
                           t["ON_DEMAND_BILLING_FREQUENCY"] if t is not None else None, 5),
            )

            t4, t5, t6 = st.columns(3)
            months_in = t4.number_input(
                "Term length (months)", min_value=1, max_value=120, step=1,
                value=int(_f(t["TERM_LENGTH_MONTHS"] if t is not None else None, 12)) or 12,
            )
            price_in = t5.number_input(
                "Capacity credit price", min_value=0.0, step=0.01, format="%.2f",
                value=_f(t["CAPACITY_CREDIT_PRICE"] if t is not None else None),
                help="Your discounted per-credit price, as stated on the order form.",
            )
            disc_in = t6.number_input(
                "Credit discount %", min_value=0.0, max_value=100.0, step=0.5,
                value=_f(t["CAPACITY_DISCOUNT_PCT"] if t is not None else None),
            )

            t7, t8, t9 = st.columns(3)
            od_price_in = t7.number_input(
                "On Demand credit price", min_value=0.0, step=0.01, format="%.2f",
                value=_f(t["ON_DEMAND_CREDIT_PRICE"] if t is not None else None),
                help="The undiscounted price that applies once capacity is exhausted. "
                     "Leave at zero to derive it from the discount.",
            )
            ed_in = t8.selectbox(
                "Edition", EDITIONS,
                index=_idx(EDITIONS, t["EDITION"] if t is not None else None, 1),
            )
            pay_in = t9.number_input(
                "Payment terms (days)", min_value=0, max_value=180, step=5,
                value=int(_f(t["PAYMENT_TERMS_DAYS"] if t is not None else None, 30)),
            )

            t10, t11, t12 = st.columns(3)
            stor_in = t10.number_input(
                "Storage $/TB", min_value=0.0, step=1.0, format="%.2f",
                value=_f(t["STORAGE_PRICE_PER_TB"] if t is not None else None),
            )
            cloud_in = t11.text_input(
                "Cloud provider",
                value=coalesce(t["CLOUD_PROVIDER"]) if t is not None else "",
            )
            region_in = t12.text_input(
                "Region", value=coalesce(t["REGION_NAME"]) if t is not None else "",
            )

            pf_in = st.checkbox(
                "Contract allows invoice pull-forward",
                value=bool(t["INVOICE_PULL_FORWARD"]) if t is not None
                and pd.notna(t["INVOICE_PULL_FORWARD"]) else True,
                help="Most capacity order forms let Snowflake invoice the next "
                     "installment early once consumption exceeds the current one. This "
                     "is what usually gets mistaken for double billing.",
            )
            od_disc_in = st.checkbox(
                "Credit discount also applies to On Demand",
                value=bool(t["DISCOUNT_APPLIES_ON_DEMAND"]) if t is not None
                and pd.notna(t["DISCOUNT_APPLIES_ON_DEMAND"]) else False,
                help="Rare. When false, exhausting capacity raises the per-credit price.",
            )

            terms_saved = st.form_submit_button("Save commercial terms", type="primary")

        if terms_saved:
            errs = []
            if price_in <= 0:
                errs.append("Capacity credit price must be greater than zero.")
            if cad_in != "Upfront" and months_in % {
                "Monthly": 1, "Quarterly": 3, "Semi-Annually": 6, "Annually": 12
            }[cad_in]:
                errs.append(
                    f"A {months_in}-month term does not divide evenly into "
                    f"{cad_in.lower()} installments. The final period will be short."
                )
            if errs:
                for e in errs:
                    st.error(e)
            else:
                # Derive the On Demand price when it was left blank: order forms
                # state the discounted price and the discount, not list, and
                # backing list out of those two is exact.
                od_price = od_price_in
                if od_price <= 0:
                    od_price = (
                        price_in if od_disc_in or disc_in <= 0
                        else round(price_in / (1 - disc_in / 100), 6)
                    )
                exec_write(
                    f"""UPDATE {DB}.CONFIG.CONTRACT SET
                          CUSTOMER_NAME = {sf.sql_str(cust_in.strip())},
                          BILLING_FREQUENCY = {sf.sql_str(cad_in)},
                          ON_DEMAND_BILLING_FREQUENCY = {sf.sql_str(od_cad_in)},
                          TERM_LENGTH_MONTHS = {int(months_in)},
                          CAPACITY_CREDIT_PRICE = {price_in},
                          ON_DEMAND_CREDIT_PRICE = {od_price},
                          CAPACITY_DISCOUNT_PCT = {disc_in},
                          DISCOUNT_APPLIES_ON_DEMAND = {str(bool(od_disc_in)).upper()},
                          INVOICE_PULL_FORWARD = {str(bool(pf_in)).upper()},
                          EDITION = {sf.sql_str(ed_in)},
                          CLOUD_PROVIDER = {sf.sql_str(cloud_in.strip())},
                          REGION_NAME = {sf.sql_str(region_in.strip())},
                          STORAGE_PRICE_PER_TB = {stor_in},
                          PAYMENT_TERMS_DAYS = {int(pay_in)},
                          UPDATED_BY = CURRENT_USER(),
                          UPDATED_AT = CURRENT_TIMESTAMP()
                        WHERE CONTRACT_SK = {int(a['CONTRACT_SK'])}"""
                )
                if od_price != od_price_in:
                    st.info(
                        f"On Demand price derived as {usd(od_price)} per credit from "
                        f"{usd(price_in)} at a {disc_in:g}% discount. That gap is the "
                        "price increase that applies once capacity is exhausted."
                    )
                st.success("Commercial terms saved. The installment schedule now reflects them.")
                st.rerun()

# ---------------------------------------------------------------------------
# Billing cadence, opening balance, and thresholds
# ---------------------------------------------------------------------------
with tabs[2]:
    section("Billing cadence")

    bc = q(
        f"""SELECT CONTRACT_NUMBER, BILLING_FREQUENCY, ON_DEMAND_BILLING_FREQUENCY,
                   TERM_LENGTH_MONTHS, CARRYOVER_MODE, CONTRACT_START_DATE,
                   CONTRACT_END_DATE, CAPACITY_PURCHASED, METERED_CURRENCY,
                   CAPACITY_CREDIT_PRICE, ON_DEMAND_CREDIT_PRICE,
                   INVOICE_PULL_FORWARD, CONTRACT_SOURCE,
                   PRIOR_CONSUMPTION_AMOUNT, PRIOR_CONSUMPTION_AS_OF
            FROM {DB}.CONFIG.CONTRACT WHERE IS_ACTIVE""",
        page=PAGE, query_name="billing_cadence",
    )

    if bc.empty:
        st.warning("No active contract. Upload an order form or add one on the Contract tab.")
    else:
        b = bc.iloc[0]
        if b.CONTRACT_SOURCE == "ORDER_FORM_EXTRACTED":
            st.success(
                f"Terms for **{b.CONTRACT_NUMBER}** came from an uploaded order form. "
                "Change them on the Order form tab so the document and the "
                "configuration stay in agreement."
            )

        sched = q(
            f"""SELECT PERIOD_COUNT, ALLOCATION, PERIOD_LABEL, PERIOD_START, PERIOD_END
                FROM {DB}.CONFIG.BILLING_SCHEDULE
                WHERE IS_ACTIVE AND IS_CURRENT_PERIOD""",
            page=PAGE, query_name="current_period",
        )

        c1, c2, c3 = st.columns(3)
        c1.metric("Capacity billing", str(b.BILLING_FREQUENCY or "not set"))
        c1.caption(f"On Demand: {b.ON_DEMAND_BILLING_FREQUENCY or 'not set'}")
        c2.metric(
            "Term",
            f"{int(b.TERM_LENGTH_MONTHS)} months" if pd.notna(b.TERM_LENGTH_MONTHS)
            else "not set",
        )
        c2.caption(f"{d(b.CONTRACT_START_DATE)} to {d(b.CONTRACT_END_DATE)}")
        if not sched.empty:
            s0 = sched.iloc[0]
            c3.metric("Current period", str(s0.PERIOD_LABEL))
            c3.caption(
                f"{d(s0.PERIOD_START)} to {d(s0.PERIOD_END)} - "
                f"{b.METERED_CURRENCY} {s0.ALLOCATION:,.2f} installment"
            )
        else:
            st.info(
                "No billing schedule yet. It is derived from the contract term and billing frequency, so set those first.",
                icon=mi("info"),
            )

        if pd.notna(b.CAPACITY_CREDIT_PRICE) and pd.notna(b.ON_DEMAND_CREDIT_PRICE):
            gap = float(b.ON_DEMAND_CREDIT_PRICE) - float(b.CAPACITY_CREDIT_PRICE)
            if gap > 0:
                st.info(
                    f"Credits cost **{b.METERED_CURRENCY} "
                    f"{b.CAPACITY_CREDIT_PRICE:,.2f}** under the contract and "
                    f"**{b.METERED_CURRENCY} {b.ON_DEMAND_CREDIT_PRICE:,.2f}** once "
                    f"capacity is exhausted, a "
                    f"{gap / float(b.CAPACITY_CREDIT_PRICE) * 100:,.1f}% increase. "
                    "Order forms normally state the credit discount does not apply "
                    "to On Demand pricing."
                )

        st.divider()
        section("Consumption already drawn")
        st.caption(
            "Snowflake retains about 365 days of usage history, so a multi-year term "
            "cannot be measured from its start. Enter the amount already consumed "
            "against this contract, taken from your most recent invoice or "
            "remaining-balance statement, and the app counts measured usage only "
            "after that date. Without it, remaining capacity is overstated."
        )

        with st.form("opening_balance"):
            o1, o2 = st.columns(2)
            amt = o1.number_input(
                f"Consumed to date ({b.METERED_CURRENCY})",
                min_value=0.0,
                value=float(b.PRIOR_CONSUMPTION_AMOUNT or 0.0),
                step=1000.0, format="%.2f",
            )
            as_of = o2.date_input(
                "As of date",
                value=(pd.Timestamp(b.PRIOR_CONSUMPTION_AS_OF).date()
                       if pd.notna(b.PRIOR_CONSUMPTION_AS_OF)
                       else pd.Timestamp(b.CONTRACT_START_DATE).date()),
                min_value=pd.Timestamp(b.CONTRACT_START_DATE).date(),
                max_value=pd.Timestamp(b.CONTRACT_END_DATE).date(),
            )
            if st.form_submit_button("Save opening balance", type="primary"):
                exec_write(
                    f"""UPDATE {DB}.CONFIG.CONTRACT
                           SET PRIOR_CONSUMPTION_AMOUNT = {amt},
                               PRIOR_CONSUMPTION_AS_OF  = '{as_of}',
                               UPDATED_AT = CURRENT_TIMESTAMP(),
                               UPDATED_BY = CURRENT_USER()
                         WHERE IS_ACTIVE"""
                )
                st.success("Saved. Billing positions recalculated.")
                st.rerun()

    st.divider()
    section("Alert thresholds")
    st.caption(
        "Your own trigger points. PERIOD thresholds measure against the current "
        "installment allocation; TERM thresholds measure against total capacity. "
        "Each is checked against both actual and forecast position, so a threshold "
        "can warn before it is crossed."
    )

    th = q(
        f"""SELECT THRESHOLD_ID, SCOPE, THRESHOLD_PCT, LABEL, SEVERITY,
                   NOTIFY_EMAIL, IS_ENABLED
            FROM {DB}.CONFIG.ALERT_THRESHOLDS
            ORDER BY SCOPE DESC, THRESHOLD_PCT""",
        page=PAGE, query_name="thresholds",
    )

    # Quartile presets. Offered rather than seeded: an installed default would
    # show a fresh customer eight "Breached" rows describing limits nobody chose,
    # which reads as a finding instead of an opinion. Quarters of the way through
    # a period or a term is the interval people actually reason in.
    if th.empty:
        st.info(
            "No thresholds set yet, so nothing is being watched. Add rows below, "
            "or start from quartiles and adjust.",
            icon=mi("notifications_off"),
        )

    QUARTILES = [
        (25, "INFO", False),
        (50, "INFO", False),
        (75, "WARNING", False),
        (100, "CRITICAL", True),
    ]

    def add_quartiles(scope: str) -> None:
        """Insert 25/50/75/100 for one scope, skipping percentages already set."""
        existing = set()
        if not th.empty:
            existing = {
                (r.SCOPE, float(r.THRESHOLD_PCT)) for r in th.itertuples()
                if pd.notna(r.THRESHOLD_PCT)
            }
        base = int(th["THRESHOLD_ID"].max()) if not th.empty else 0
        noun = "Period" if scope == "PERIOD" else "Contract"
        added = 0
        for pctv, sev, email in QUARTILES:
            if (scope, float(pctv)) in existing:
                continue
            added += 1
            label = (
                f"{noun} allocation exceeded" if pctv == 100 and scope == "PERIOD"
                else "Capacity exhausted, On Demand pricing"
                if pctv == 100 else f"{noun} {pctv}% consumed"
            )
            exec_write(
                f"""INSERT INTO {DB}.CONFIG.ALERT_THRESHOLDS
                      (THRESHOLD_ID, SCOPE, THRESHOLD_PCT, LABEL, SEVERITY,
                       NOTIFY_EMAIL, IS_ENABLED)
                    VALUES ({base + added}, '{scope}', {pctv},
                            {sql_str(label)}, '{sev}', {email}, TRUE)"""
            )
        if added:
            st.success(f"Added {added} {scope.lower()} threshold(s) at 25/50/75/100%.")
            st.rerun()
        else:
            st.info("Those quartiles are already set for this scope.")

    p1, p2, _ = st.columns([1, 1, 2])
    if p1.button("Add period quartiles", help="25 / 50 / 75 / 100% of the current "
                                              "installment allocation"):
        add_quartiles("PERIOD")
    if p2.button("Add term quartiles", help="25 / 50 / 75 / 100% of total capacity"):
        add_quartiles("TERM")

    th_edit = st.data_editor(
        th, hide_index=True, use_container_width=True, num_rows="dynamic",
        key="th_editor",
        column_config={
            "THRESHOLD_ID": st.column_config.NumberColumn("ID", disabled=True),
            "SCOPE": st.column_config.SelectboxColumn(
                "Scope", options=["PERIOD", "TERM"], required=True),
            "THRESHOLD_PCT": st.column_config.NumberColumn(
                "Trigger at %", min_value=1, max_value=500, step=5, required=True),
            "LABEL": st.column_config.TextColumn("Label"),
            "SEVERITY": st.column_config.SelectboxColumn(
                "Severity", options=["INFO", "WARNING", "CRITICAL"]),
            "NOTIFY_EMAIL": st.column_config.CheckboxColumn("Email"),
            "IS_ENABLED": st.column_config.CheckboxColumn("Enabled"),
        },
    )

    if st.button("Save thresholds", type="primary"):
        rows = th_edit.dropna(subset=["SCOPE", "THRESHOLD_PCT"])
        # Rewrite wholesale so deletions in the editor are honoured. The table is
        # tiny and customer-owned, so a full replace is simpler and safer than
        # diffing rows.
        exec_write(f"DELETE FROM {DB}.CONFIG.ALERT_THRESHOLDS")
        for i, r in enumerate(rows.itertuples(), start=1):
            label = str(r.LABEL) if pd.notna(r.LABEL) else ""
            exec_write(
                f"""INSERT INTO {DB}.CONFIG.ALERT_THRESHOLDS
                      (THRESHOLD_ID, SCOPE, THRESHOLD_PCT, LABEL, SEVERITY,
                       NOTIFY_EMAIL, IS_ENABLED)
                    VALUES ({i}, '{r.SCOPE}', {float(r.THRESHOLD_PCT)},
                            '{label.replace("'", "''")}',
                            '{r.SEVERITY if pd.notna(r.SEVERITY) else 'WARNING'}',
                            {bool(r.NOTIFY_EMAIL)}, {bool(r.IS_ENABLED)})"""
            )
        st.success(f"Saved {len(rows)} threshold(s).")
        st.rerun()

    status = q(
        f"""SELECT SCOPE, LABEL, THRESHOLD_AMOUNT, CURRENT_PCT, PROJECTED_PCT,
                   STATUS, DAYS_UNTIL_BREACH, CURRENCY
            FROM {DB}.CURATED.FCT_THRESHOLD_STATUS
            ORDER BY SCOPE DESC, THRESHOLD_PCT""",
        page=PAGE, query_name="threshold_status_settings",
    )
    if not status.empty:
        st.caption("Current evaluation")
        table_with_download(
            pd.DataFrame({
                "Scope": status.SCOPE.str.title(),
                "Threshold": status.LABEL,
                "Trigger at": status.apply(
                    lambda r: f"{r.CURRENCY} {r.THRESHOLD_AMOUNT:,.2f}", axis=1),
                "Now": status.CURRENT_PCT.map(
                    lambda v: f"{v:,.1f}%" if pd.notna(v) else "n/a"),
                "Projected": status.PROJECTED_PCT.map(
                    lambda v: f"{v:,.1f}%" if pd.notna(v) else "n/a"),
                "Status": status.STATUS.map({
                    "BREACHED": "Breached",
                    "FORECAST_BREACH": "Forecast to breach",
                    "OK": "OK"}),
                "Lead time": status.DAYS_UNTIL_BREACH.map(
                    lambda v: "now" if pd.notna(v) and v <= 0
                    else (f"{int(v)} days" if pd.notna(v) else "not projected")),
            }),
            "snowflake360_settings_2", "settings_2",
        )
    else:
        st.info(
            "No threshold status to show. Thresholds are evaluated once you add them and the refresh has run.",
            icon=mi("info"),
        )

    st.caption(
        "Email delivery requires a notification integration, configured on the "
        "Refresh & alerts tab. Native Snowflake Budgets can be attached as a "
        "companion transport, but cannot replace these thresholds: a budget's "
        "limit is in credits rather than currency, its interval is a fixed "
        "calendar month, and it does not pool unspent capacity across periods."
    )

# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------
with tabs[3]:
    section("Credit and storage rates")
    st.caption(
        "Rates are prefilled from `ORGANIZATION_USAGE.RATE_SHEET_DAILY`. Entered values always "
        "override the prefill. AI credit price is flat regardless of edition and depends on "
        "routing: $2.00 global, $2.20 regional. Capacity discounts do not apply to AI credits."
    )

    subs = q(
        f"""SELECT SUBSCRIPTION_ID, ACCOUNT_NAME, SERVICE_LEVEL, CONTRACT_CURRENCY,
                   PRICE_PER_PLATFORM_CREDIT, OVERAGE_PRICE_PER_CREDIT, PRICE_PER_AI_CREDIT,
                   PRICE_PER_AI_CREDIT_GLOBAL, PRICE_PER_AI_CREDIT_REGIONAL,
                   STORAGE_PRICE_PER_TB_MONTH, AI_ROUTING_MODE, RATE_SOURCE, VALID_FROM
            FROM {DB}.CONFIG.SUBSCRIPTION ORDER BY ACCOUNT_NAME""",
        PAGE,
        "subscription_rates",
    )

    # Variance against the live rate sheet
    variance = q(
        f"""WITH latest AS (SELECT MAX(RATE_DATE) AS D FROM {DB}.CURATED.FCT_RATE_EFFECTIVE),
            sheet AS (
              SELECT ACCOUNT_NAME,
                     MAX(CASE WHEN RATING_TYPE='COMPUTE'    THEN EFFECTIVE_RATE END) AS SHEET_PLATFORM,
                     MAX(CASE WHEN RATING_TYPE='AI_COMPUTE' THEN EFFECTIVE_RATE END) AS SHEET_AI,
                     MAX(CASE WHEN RATING_TYPE='STORAGE'    THEN EFFECTIVE_RATE END) AS SHEET_STORAGE
              FROM {DB}.CURATED.FCT_RATE_EFFECTIVE
              WHERE RATE_DATE = (SELECT D FROM latest) GROUP BY 1
            )
            SELECT s.ACCOUNT_NAME,
                   s.PRICE_PER_PLATFORM_CREDIT AS ENTERED_PLATFORM, h.SHEET_PLATFORM,
                   s.PRICE_PER_AI_CREDIT       AS ENTERED_AI,       h.SHEET_AI,
                   s.STORAGE_PRICE_PER_TB_MONTH AS ENTERED_STORAGE, h.SHEET_STORAGE
            FROM {DB}.CONFIG.SUBSCRIPTION s
            LEFT JOIN sheet h ON h.ACCOUNT_NAME = s.ACCOUNT_NAME
            WHERE s.VALID_TO IS NULL""",
        PAGE,
        "rate_variance",
    )

    if not variance.empty:
        variance["PLATFORM_DIFF"] = (
            variance["ENTERED_PLATFORM"] - variance["SHEET_PLATFORM"]
        )
        drift = variance[variance["PLATFORM_DIFF"].abs() > 0.001]
        if drift.empty:
            st.success(
                "All entered rates match Snowflake's current rate sheet.",
                icon=mi("check_circle"),
            )
        else:
            st.warning(
                f"{len(drift)} account(s) have an entered platform rate that differs from "
                "Snowflake's rate sheet. Entered values win, which is intended for what-if "
                "modelling, but verify it is deliberate.",
                icon=mi("warning"),
            )
            table_with_download(
                drift,
                "snowflake360_settings_3", "settings_3",
            )
    else:
        st.info(
            "No rate sheet rows to compare against. RATE_SHEET_DAILY comes from ORGANIZATION_USAGE and is unavailable in ACCOUNT mode.",
            icon=mi("info"),
        )

    edited = st.data_editor(
        subs,
        hide_index=True,
        use_container_width=True,
        disabled=["SUBSCRIPTION_ID", "ACCOUNT_NAME", "SERVICE_LEVEL", "RATE_SOURCE", "VALID_FROM"],
        column_config=money_column_config(
            subs,
            [
                "PRICE_PER_PLATFORM_CREDIT", "OVERAGE_PRICE_PER_CREDIT",
                "PRICE_PER_AI_CREDIT", "PRICE_PER_AI_CREDIT_GLOBAL",
                "PRICE_PER_AI_CREDIT_REGIONAL", "STORAGE_PRICE_PER_TB_MONTH",
            ],
        ),
        key="rate_editor",
    )

    if st.button("Save rates", type="primary"):
        bad = edited[
            (edited["PRICE_PER_PLATFORM_CREDIT"] <= 0)
            | (edited["PRICE_PER_AI_CREDIT"] <= 0)
            | (edited["STORAGE_PRICE_PER_TB_MONTH"] <= 0)
        ]
        if not bad.empty:
            st.error("All prices must be greater than zero.")
        else:
            for _, r in edited.iterrows():
                exec_write(
                    f"""UPDATE {DB}.CONFIG.SUBSCRIPTION SET
                          PRICE_PER_PLATFORM_CREDIT = {float(r['PRICE_PER_PLATFORM_CREDIT'])},
                          OVERAGE_PRICE_PER_CREDIT  = {float(r['OVERAGE_PRICE_PER_CREDIT'])},
                          PRICE_PER_AI_CREDIT       = {float(r['PRICE_PER_AI_CREDIT'])},
                          STORAGE_PRICE_PER_TB_MONTH = {float(r['STORAGE_PRICE_PER_TB_MONTH'])},
                          RATE_SOURCE = 'CUSTOMER_ENTERED',
                          UPDATED_BY = CURRENT_USER(), UPDATED_AT = CURRENT_TIMESTAMP()
                        WHERE SUBSCRIPTION_ID = {int(r['SUBSCRIPTION_ID'])}"""
                )
            st.success("Rates saved and marked CUSTOMER_ENTERED.")
            st.rerun()

# ---------------------------------------------------------------------------
# Account scope
# ---------------------------------------------------------------------------
with tabs[4]:
    section("Account scope")
    st.caption(
        "Controls which accounts org queries cover. An **empty table means all accounts**. "
        "Narrowing the scope is worthwhile in an organization with many accounts, "
        "because an unbounded org scan reads every account's usage on every refresh."
    )

    scope = q(
        f"""SELECT ACCOUNT_NAME, ACCOUNT_LOCATOR, IS_INCLUDED, NOTE
            FROM {DB}.CONFIG.ACCOUNT_SCOPE ORDER BY ACCOUNT_NAME""",
        PAGE,
        "account_scope",
    )
    table_with_download(
        scope,
        "snowflake360_settings_4", "settings_4",
    )

    org_total = q(
        f"""SELECT COUNT(*) AS TOTAL,
                   COUNT_IF(NOT IS_DELETED AND NOT IS_MOVED) AS LIVE,
                   COUNT_IF(IS_IN_SCOPE) AS IN_SCOPE,
                   COUNT_IF(IS_GOV_REGION) AS GOV
            FROM {DB}.CURATED.DIM_ACCOUNT""",
        PAGE,
        "org_account_counts",
    )
    o = org_total.iloc[0]
    m = st.columns(4)
    m[0].metric("Accounts in org", int(o["TOTAL"]))
    m[1].metric("Live accounts", int(o["LIVE"]))
    m[2].metric("In scope", int(o["IN_SCOPE"]))
    m[3].metric("GOV region accounts", int(o["GOV"]),
                "org usage unavailable", delta_color="off")
    st.caption(
        "ORGANIZATION_USAGE views are not available in US SnowGov regions, so accounts there "
        "under-report. Widening scope to the full organization needs a scale test first."
    )

    # Carried over from the former Overview page's "Scope and coverage" expander.
    # Account detail and source freshness are configuration facts, so they belong
    # beside the scope they describe rather than on a dashboard.
    with st.expander("Accounts in scope and source freshness", expanded=False):
        accts = scope_accounts()
        st.write(f"**{len(accts)} accounts in scope**")
        table_with_download(
            accts[[
                "ACCOUNT_NAME", "ACCOUNT_LOCATOR", "REGION", "SERVICE_LEVEL",
                "IS_INSTALLED_ACCOUNT", "IS_DELETED", "IS_MANAGED", "IS_GOV_REGION",
            ]],
            "snowflake360_accounts_in_scope", "settings_scope_detail",
        )
        st.caption(
            "Accounts are keyed on ACCOUNT_LOCATOR + REGION. Locator alone is not "
            "unique - Snowflake reuses it across regions, and joining on it inflates "
            "totals."
        )
        st.write("**Source freshness**")
        table_with_download(
            freshness(), "snowflake360_freshness", "settings_freshness",
        )

# ---------------------------------------------------------------------------
# Refresh and alerts
#
# The Benchmarks tab that used to sit here was removed as redundant. CONFIG.BENCHMARKS
# is still read by the Account health signals on the Active Contract page, but it
# held published Snowflake medians the customer neither sets nor changes, so a tab
# whose only content was a read-only five-row table earned no space.
#
# The Verification tab was also removed. Its 23 checks guard against silently wrong
# rollups, which makes them a build-time concern for us rather than something a
# customer should be asked to interpret: a red FAIL in a shipped app reads as
# "this product is broken" even when the cause is an unset config value. The
# checks live on as SF360.CURATED.VW_VERIFICATION and are run locally by
# tests/verification.py.
# ---------------------------------------------------------------------------
with tabs[5]:
    section("Refresh schedule")
    stg = q(
        f"SELECT SETTING_KEY, SETTING_VALUE, DESCRIPTION FROM {DB}.CONFIG.SETTINGS ORDER BY 1",
        PAGE,
        "settings_all",
    )
    table_with_download(
        stg,
        "snowflake360_settings_6", "settings_6",
    )

    st.markdown(
        "The refresh runs as a three-task DAG at **11:00 UTC** daily: rebuild org landing, "
        "rebuild account landing, then refresh curated dynamic tables in dependency order. "
        "11:00 UTC sits after the 8-hour attribution latency and before 8am Central "
        "year-round, so no daylight-saving logic is needed."
    )
    st.code(
        "EXECUTE TASK SF360.LANDING.TSK_SF360_ROOT;   -- run now\n"
        "ALTER TASK SF360.LANDING.TSK_SF360_ROOT SUSPEND;  -- pause the schedule",
        language="sql",
    )

    fresh = q(
        f"""SELECT SOURCE_NAME, SCOPE, MEASURE, AS_OF_DATE, LAG_DAYS,
                   DOCUMENTED_LATENCY_HOURS, SOURCE_VIEW
            FROM {DB}.CURATED.FCT_FRESHNESS ORDER BY LAG_DAYS DESC""",
        PAGE,
        "freshness_detail",
    )
    st.markdown("**Measured freshness per source**")
    table_with_download(
        fresh,
        "snowflake360_settings_7", "settings_7",
    )
    st.caption(
        "Measured lag is often better than the documented latency, which is a worst-case "
        "bound. The app reports what it measures rather than what the docs promise."
    )

    st.divider()
    section("Native anomaly email alerts")
    st.caption(
        "Snowflake's own cost anomaly detection sends these emails. Nothing to build or "
        "maintain here: the app only records who should receive them and registers that "
        "list with Snowflake. Recipients must be verified Snowflake users in this account."
    )

    current_emails = sf.settings().get("ANOMALY_ALERT_EMAILS", "") or ""
    emails_in = st.text_input(
        "Alert recipients",
        value=current_emails,
        placeholder="finops@example.com, platform-owner@example.com",
        help="Comma-separated. Leave empty and save to stop the emails.",
        key="alert_emails",
    )

    # Validated before it reaches SQL: this string is interpolated into a CALL, and
    # a stray quote would either break the statement or inject into it.
    raw = [e.strip() for e in emails_in.replace(";", ",").split(",") if e.strip()]
    invalid = [e for e in raw if not re.fullmatch(r"[^@\s,';]+@[^@\s,';]+\.[A-Za-z]{2,}", e)]

    if invalid:
        st.error("Not valid email addresses: " + ", ".join(invalid))

    if st.button("Save alert recipients", type="primary", disabled=bool(invalid)):
        exec_write(
            f"""MERGE INTO {DB}.CONFIG.SETTINGS t
                USING (SELECT 'ANOMALY_ALERT_EMAILS' AS K, {sql_str(', '.join(raw))} AS V) s
                  ON t.SETTING_KEY = s.K
                WHEN MATCHED THEN UPDATE SET
                  SETTING_VALUE = s.V, UPDATED_BY = CURRENT_USER(), UPDATED_AT = CURRENT_TIMESTAMP()
                WHEN NOT MATCHED THEN INSERT
                  (SETTING_KEY, SETTING_VALUE, DESCRIPTION, UPDATED_BY, UPDATED_AT)
                  VALUES (s.K, s.V, 'Recipients for Snowflake native cost anomaly emails',
                          CURRENT_USER(), CURRENT_TIMESTAMP())"""
        )
        # Registering the list with Snowflake is a separate, privileged call. It is
        # attempted rather than assumed: the recipient list is still worth saving
        # even when the caller cannot register it, and the SQL is then shown so an
        # admin can run it.
        array = "[" + ", ".join(sql_str(e) for e in raw) + "]"
        call = (
            "CALL SNOWFLAKE.LOCAL.ANOMALY_INSIGHTS!SET_ACCOUNT_NOTIFICATION_EMAILS(\n"
            f"  {array}\n);"
        )
        try:
            exec_write(call.rstrip(";"))
            st.success(
                f"Saved and registered with Snowflake: {len(raw)} recipient(s)."
                if raw else "Saved. Anomaly emails are now off.",
                icon=mi("check_circle"),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            st.warning(
                "Recipients were saved, but registering them with Snowflake failed. "
                "That call needs privileges on SNOWFLAKE.LOCAL.ANOMALY_INSIGHTS. "
                "Ask an ACCOUNTADMIN to run the statement below.",
                icon=mi("warning"),
            )
            st.code(call, language="sql")
            st.caption(f"Snowflake returned: {exc}")
        st.rerun()

    if current_emails:
        st.caption(f"Currently registered: {current_emails}")
    else:
        st.caption("No recipients configured, so Snowflake is not sending anomaly emails.")
