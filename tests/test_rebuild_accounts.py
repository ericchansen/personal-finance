from importers.rebuild.accounts import (
    CanonicalAccount,
    build_account_plan,
)


def test_account_plan_renames_and_deactivates_from_canonical_identity():
    canonical = {
        "one": CanonicalAccount("one", "Canonical Card", "CREDIT_CARD", True, False),
        "ledger": CanonicalAccount(
            "ledger", "Hardware Wallet", "CRYPTOCURRENCY", False, False
        ),
    }
    existing = [{
        "id": "app-one", "name": "Old Card", "accountType": "CREDIT_CARD",
        "currency": "USD", "isActive": True, "isDefault": False,
        "group": "Credit Cards", "trackingMode": "TRANSACTIONS",
    }]
    plan = build_account_plan(existing, canonical, {"old card": "one"})
    assert plan.updates[0][1]["name"] == "Canonical Card"
    assert plan.updates[0][1]["isActive"] is False
    assert plan.creates[0]["_canonicalId"] == "ledger"


def test_account_plan_preserves_canonical_holdings_tracking_mode():
    canonical = {
        "retirement": CanonicalAccount(
            "retirement",
            "Balance-only Retirement",
            "SECURITIES",
            False,
            False,
            "HOLDINGS",
        )
    }
    plan = build_account_plan([], canonical, {})
    assert plan.creates[0]["tracking_mode"] == "HOLDINGS"
