from scripts import run_sales_journal_user_e2e_guarded as driver


def test_schema_column_choice_supports_stable_suffix_patterns(monkeypatch):
    monkeypatch.setattr(
        driver,
        "SCHEMA_COLUMN_CHOICES",
        {
            "*posting_date": "DocDate",
            "*doc_status": "DocStatus",
            "*include_cancelled": "CANCELED",
        },
    )

    assert driver._desired_schema_column(
        "input_ar_invoice_header_oinv_posting_date"
    ) == "DocDate"
    assert driver._desired_schema_column(
        "input_f_invoice_header_doc_status"
    ) == "DocStatus"
    assert driver._desired_schema_column(
        "input_policy_fact_filter_f_invoice_header_include_cancelled"
    ) == "CANCELED"


def test_conflicting_schema_patterns_do_not_guess(monkeypatch):
    monkeypatch.setattr(
        driver,
        "SCHEMA_COLUMN_CHOICES",
        {
            "input_*": "DocDate",
            "*posting_date": "TaxDate",
        },
    )

    assert driver._desired_schema_column(
        "input_invoice_posting_date"
    ) is None
