from datetime import date

from importers.assets.cli import loan_value_date, normalize_kind

ASSETS = [
    {"name": "A House", "kind": "PROPERTY", "valueDate": "2025-01-15"},
    {"name": "No Date", "kind": "PROPERTY"},
]
TODAY = date(2025, 6, 30)


def test_a_linked_loan_inherits_the_asset_valuation_date():
    # An alternative holding enters the net worth history on its valuation
    # date. A house dated at appraisal and its mortgage dated today leave a
    # window where the asset counts and the debt does not.
    spec = {"name": "Mortgage", "linkedTo": "A House"}
    assert loan_value_date(spec, ASSETS, TODAY) == "2025-01-15"


def test_an_explicit_value_date_wins():
    spec = {"name": "Mortgage", "linkedTo": "A House", "valueDate": "2025-01-01"}
    assert loan_value_date(spec, ASSETS, TODAY) == "2025-01-01"


def test_an_unlinked_loan_falls_back_to_the_run_date():
    assert loan_value_date({"name": "Car Loan"}, ASSETS, TODAY) == "2025-06-30"


def test_a_loan_linked_to_an_unknown_asset_falls_back():
    spec = {"name": "Mortgage", "linkedTo": "Not Here"}
    assert loan_value_date(spec, ASSETS, TODAY) == "2025-06-30"


def test_a_loan_linked_to_an_asset_without_a_date_falls_back():
    spec = {"name": "Mortgage", "linkedTo": "No Date"}
    assert loan_value_date(spec, ASSETS, TODAY) == "2025-06-30"


def test_kind_accepts_either_case():
    assert normalize_kind("PROPERTY") == "property"
    assert normalize_kind("property") == "property"
    assert normalize_kind("REAL_ESTATE") == "property"
