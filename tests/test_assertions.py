import json
from decimal import Decimal
from pathlib import Path

from importers.facts.assertions import load_balance_snapshot, verify_assertions
from importers.facts.cli import main
from importers.facts.loader import load_facts


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def account(account_id: str = "acct-one") -> dict:
    return {
        "type": "account",
        "id": account_id,
        "institution": "Example Bank",
        "displayName": "Example Checking",
        "maskedNumber": "0001",
        "kind": "depository",
        "opened": "2024-01-01",
        "closed": None,
        "excluded": False,
        "reason": None,
        "source": "synthetic fixture",
        "sourcePath": None,
        "notes": "synthetic",
    }


def assertion(on: str, balance: str = "1234.56") -> dict:
    return {
        "type": "assertion",
        "accountId": "acct-one",
        "date": on,
        "balance": balance,
        "source": "synthetic statement",
        "sourcePath": "extracts/example/synthetic.ofx",
        "notes": "synthetic",
    }


def test_assertions_are_append_and_history_safe(tmp_path):
    write_json(tmp_path / "accounts.json", [account(), assertion("2024-01-31"), assertion("2024-02-29", "1250.00")])
    result = load_facts(tmp_path)
    assert result.ok
    assert len([fact for fact in result.facts if fact.fact_type == "assertion"]) == 2


def test_conflicting_duplicate_assertions_are_errors(tmp_path):
    write_json(tmp_path / "facts.json", [account(), assertion("2024-01-31"), assertion("2024-01-31", "999.00")])
    result = load_facts(tmp_path)
    assert any("conflicting assertion" in issue.message for issue in result.errors)


def test_assertion_requires_nonempty_provenance(tmp_path):
    item = assertion("2024-01-31")
    item["source"] = ""
    write_json(tmp_path / "facts.json", [account(), item])
    result = load_facts(tmp_path)
    assert any(error.field == "source" and "provenance" in error.message for error in result.errors)


def test_identical_duplicate_assertions_are_only_warned(tmp_path):
    write_json(tmp_path / "facts.json", [account(), assertion("2024-01-31"), assertion("2024-01-31")])
    result = load_facts(tmp_path)
    assert result.ok
    assert any("repeated assertion" in issue.message for issue in result.warnings)


def test_snapshot_uses_decimal_and_inherits_top_level_date(tmp_path):
    path = tmp_path / "snapshot.json"
    write_json(
        path,
        {
            "date": "2024-01-31",
            "balances": [
                {"accountId": "acct-one", "balance": "1234.56", "source": "synthetic export"}
            ],
        },
    )
    balances, errors = load_balance_snapshot(path)
    assert not errors
    assert balances[0].balance == Decimal("1234.56")


def test_snapshot_rejects_conflicting_duplicates(tmp_path):
    path = tmp_path / "snapshot.json"
    write_json(
        path,
        {
            "date": "2024-01-31",
            "balances": [
                {"accountId": "acct-one", "balance": "1.00", "source": "first"},
                {"accountId": "acct-one", "balance": "2.00", "source": "second"},
            ],
        },
    )
    _, errors = load_balance_snapshot(path)
    assert any("conflicting balances" in error.message for error in errors)


def test_verify_checks_only_snapshot_dates_and_reports_mismatch(tmp_path):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    write_json(
        facts_dir / "facts.json",
        [account(), assertion("2024-01-31"), assertion("2024-02-29", "50.00")],
    )
    snapshot_path = tmp_path / "snapshot.json"
    write_json(
        snapshot_path,
        {
            "date": "2024-02-29",
            "balances": [
                {"accountId": "acct-one", "balance": "49.99", "source": "synthetic export"}
            ],
        },
    )
    snapshot, errors = load_balance_snapshot(snapshot_path)
    result = verify_assertions(load_facts(facts_dir), snapshot, errors)
    assert result.checked == 1
    assert result.mismatches[0].expected == Decimal("50.00")
    assert not result.ok


def test_verify_rejects_snapshot_date_without_assertions(tmp_path):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    write_json(facts_dir / "facts.json", [account(), assertion("2024-01-31")])
    snapshot_path = tmp_path / "snapshot.json"
    write_json(
        snapshot_path,
        {
            "date": "2025-01-31",
            "balances": [
                {"accountId": "acct-one", "balance": "1234.56", "source": "synthetic export"}
            ],
        },
    )
    snapshot, errors = load_balance_snapshot(snapshot_path)
    result = verify_assertions(load_facts(facts_dir), snapshot, errors)
    assert not result.ok
    assert "no assertions exist" in result.errors[0].message


def test_source_scoped_snapshot_does_not_require_unrelated_assertions(tmp_path):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    write_json(
        facts_dir / "facts.json",
        [
            account("simplefin-account"),
            account("coinbase-account"),
            {**assertion("2024-01-31"), "accountId": "simplefin-account"},
            {
                **assertion("2024-01-31", "2.77"),
                "accountId": "coinbase-account",
                "source": "manual crypto reconciliation",
            },
        ],
    )
    snapshot_path = tmp_path / "snapshot.json"
    write_json(
        snapshot_path,
        {
            "date": "2024-01-31",
            "balances": [
                {
                    "accountId": "simplefin-account",
                    "balance": "1234.56",
                    "source": "simplefin",
                }
            ],
        },
    )
    snapshot, errors = load_balance_snapshot(snapshot_path)
    result = verify_assertions(load_facts(facts_dir), snapshot, errors)
    assert result.ok
    assert result.checked == 1


def test_snapshot_account_without_an_assertion_is_reported_missing(tmp_path):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    write_json(
        facts_dir / "facts.json",
        [account("known"), account("new-source-account"), {
            **assertion("2024-01-31"),
            "accountId": "known",
        }],
    )
    snapshot_path = tmp_path / "snapshot.json"
    write_json(
        snapshot_path,
        {
            "date": "2024-01-31",
            "balances": [
                {
                    "accountId": "new-source-account",
                    "balance": "10.00",
                    "source": "simplefin",
                }
            ],
        },
    )
    snapshot, errors = load_balance_snapshot(snapshot_path)
    result = verify_assertions(load_facts(facts_dir), snapshot, errors)
    assert not result.ok
    assert result.missing[0].account_id == "new-source-account"


def test_assertion_can_target_a_loan_entity_without_a_fake_account(tmp_path):
    facts = [
        {
            "type": "property",
            "name": "Example home",
            "address": "1 Example Ave",
            "purchaseDate": "2024-01-01",
            "purchasePrice": "200000",
            "saleDate": None,
            "salePrice": None,
            "netProceeds": None,
            "appraisals": [],
            "source": "synthetic",
            "sourcePath": None,
            "notes": "synthetic",
        },
        {
            "type": "loan",
            "name": "Example mortgage",
            "principal": "150000",
            "annualRate": "0.05",
            "termMonths": 360,
            "originationDate": "2024-01-01",
            "firstPayment": "2024-02-01",
            "lender": "Example Lender",
            "linkedTo": "Example home",
            "payoffAmount": None,
            "payoffDate": None,
            "pmi": False,
            "source": "synthetic",
            "sourcePath": None,
            "notes": "synthetic",
        },
        {
            "type": "assertion",
            "accountId": "loan:Example mortgage",
            "date": "2024-01-31",
            "balance": "-149900",
            "source": "synthetic statement",
            "sourcePath": None,
            "notes": "synthetic",
        },
    ]
    write_json(tmp_path / "facts.json", facts)
    assert load_facts(tmp_path).ok


def test_verify_cli_is_read_only_and_succeeds(tmp_path, capsys):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    write_json(facts_dir / "facts.json", [account(), assertion("2024-01-31")])
    snapshot = tmp_path / "snapshot.json"
    write_json(
        snapshot,
        {
            "date": "2024-01-31",
            "balances": [
                {"accountId": "acct-one", "balance": "1234.56", "source": "synthetic export"}
            ],
        },
    )
    before = (facts_dir / "facts.json").read_bytes()
    assert main(["verify-assertions", "--facts-dir", str(facts_dir), "--snapshot", str(snapshot)]) == 0
    assert "verified: 1 assertions" in capsys.readouterr().out
    assert (facts_dir / "facts.json").read_bytes() == before
