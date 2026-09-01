"""Synthetic tests for live-sourced budget proposals and guarded promotion.

Every identifier, amount, and category name here is invented. Nothing in this
file is derived from a real financial institution, a real export, or a real
Wealthfolio instance, and no test contacts a network.
"""

from __future__ import annotations

import json
import stat
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import plan_fingerprint
from importers.simplefin import categorize_cli as cli_module
from importers.simplefin.live_budget import (
    BudgetTargetOnlyClient,
    LiveBudgetEvidence,
    month_window,
    previous_month,
    promote_live_budget,
    propose_live_budget,
    read_live_evidence,
    summarize_live_proposal,
    trailing_complete_months,
    validate_live_proposal,
    write_live_proposal,
)
from importers.simplefin.spending_adapter import SPENDING_TAXONOMY, SpendingAdapter

TAXONOMY = SPENDING_TAXONOMY
PERIOD = "default"
ENVIRONMENT = "synthetic-environment-fingerprint"
AS_OF = date(2024, 7, 15)

CATEGORIES = [
    {"id": "cat-groceries", "parentId": None, "name": "Groceries"},
    {"id": "cat-transit", "parentId": None, "name": "Transit"},
    {"id": "cat-gifts", "parentId": None, "name": "Gifts"},
    {"id": "cat-unbudgeted", "parentId": None, "name": "Unbudgeted"},
    {"id": "cat-quiet", "parentId": None, "name": "Quiet"},
]

GROUPS = [
    {"id": "grp-essentials", "name": "Essentials"},
    {"id": "grp-discretionary", "name": "Discretionary"},
]

ASSIGNMENTS = [
    {"taxonomyId": TAXONOMY, "categoryId": "cat-groceries", "groupId": "grp-essentials"},
    {"taxonomyId": TAXONOMY, "categoryId": "cat-transit", "groupId": "grp-essentials"},
    {"taxonomyId": TAXONOMY, "categoryId": "cat-gifts", "groupId": "grp-discretionary"},
    {"taxonomyId": TAXONOMY, "categoryId": "cat-quiet", "groupId": "grp-discretionary"},
]

#: Groceries spends every month, transit spends in only one, gifts spends in
#: two, quiet never spends, and unbudgeted spends every month but was never
#: curated into a budget group.
SPEND = {
    "2024-04": {
        "cat-groceries": ("412.00", 21),
        "cat-unbudgeted": ("80.00", 3),
        "cat-gifts": ("60.00", 2),
    },
    "2024-05": {
        "cat-groceries": ("398.50", 19),
        "cat-unbudgeted": ("95.00", 4),
        "cat-transit": ("44.00", 6),
    },
    "2024-06": {
        "cat-groceries": ("431.25", 23),
        "cat-unbudgeted": ("70.00", 2),
        "cat-gifts": ("140.00", 3),
    },
}


class SyntheticWealthfolio:
    """An in-memory stand-in for a live Wealthfolio REST server."""

    def __init__(self, *, spend=None, targets=None):
        self.spend = dict(spend if spend is not None else SPEND)
        self.categories = [dict(row) for row in CATEGORIES]
        self.groups = [dict(row) for row in GROUPS]
        self.assignments = [dict(row) for row in ASSIGNMENTS]
        self.targets = [dict(row) for row in (targets or [])]
        self.backups = [
            {
                "filename": "synthetic-000.db",
                "sizeBytes": 1024,
                "modifiedAt": "2024-07-01T00:00:00Z",
            }
        ]
        self.calls: list[tuple[str, str]] = []
        self.next_target = 1
        self.write_failure: Exception | None = None
        self.corrupt_written_amount = False
        self.rollback_failure = False
        self.on_report = None

    # -- helpers ------------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "state": {
                "groups": [dict(row) for row in self.groups],
                "groupAssignments": [dict(row) for row in self.assignments],
                "targets": [dict(row) for row in self.targets],
                "rolloverSettings": [],
            },
            "computed": {
                "groupRows": [],
                "ungroupedRows": [],
                "incomeRows": [],
                "totals": {},
                "periodKey": PERIOD,
            },
        }

    def report_for(self, window: dict) -> dict:
        month = window["startDate"][:7]
        # Wealthfolio reports spending as a positive outflow and a refund as a
        # negative one, matching the payloads in tests/test_simplefin_
        # categorization.py. A net-refunded month is therefore negative.
        rows = [
            {
                "taxonomyId": TAXONOMY,
                "categoryId": category_id,
                "amount": amount,
                "count": count,
            }
            for category_id, (amount, count) in sorted(self.spend.get(month, {}).items())
        ]
        rows.append(
            {"taxonomyId": "income", "categoryId": "inc-salary", "amount": "5000.00", "count": 2}
        )
        return {"spendingBreakdown": rows}

    # -- transport ----------------------------------------------------------

    def get(self, path):
        self.calls.append(("GET", path))
        route = urlsplit(path).path
        if route == f"/taxonomies/{TAXONOMY}":
            return {
                "taxonomy": {"id": TAXONOMY},
                "categories": [dict(row) for row in self.categories],
            }
        if route.startswith("/taxonomies/"):
            return {"taxonomy": {"id": route.rsplit("/", 1)[-1]}, "categories": []}
        if route == "/taxonomies":
            return [{"id": TAXONOMY}]
        if route == "/spending/settings":
            return {"accountIds": ["acct-synthetic"]}
        if route == "/spending/rules":
            return []
        if route == "/spending/budget":
            return self.snapshot()
        if route == "/utilities/database/backups":
            return [dict(row) for row in self.backups]
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, payload):
        self.calls.append(("POST", path))
        route = urlsplit(path).path
        if route == "/spending/report":
            if self.on_report is not None:
                self.on_report(self)
            return self.report_for(payload)
        if route == "/spending/cash-activities/search":
            return {"totalCount": 0}
        if route == "/spending/budget/targets":
            if self.write_failure is not None:
                failure, self.write_failure = self.write_failure, None
                raise failure
            period = parse_qs(urlsplit(path).query).get("periodKey", [PERIOD])[0]
            amount = payload["amount"]
            if self.corrupt_written_amount:
                amount = str(Decimal(amount) + Decimal("1.00"))
            self.targets = [
                row
                for row in self.targets
                if not (
                    row.get("targetType") == "category"
                    and row.get("categoryId") == payload.get("categoryId")
                    and row.get("periodKey") == period
                )
            ]
            self.targets.append(
                {
                    "id": f"tgt-{self.next_target:03d}",
                    "periodKey": period,
                    "targetType": "category",
                    "taxonomyId": payload.get("taxonomyId"),
                    "categoryId": payload.get("categoryId"),
                    "groupId": None,
                    "amount": amount,
                }
            )
            self.next_target += 1
            return self.snapshot()
        raise AssertionError(f"unexpected POST {path}")

    def put(self, path, payload):
        self.calls.append(("PUT", path))
        raise AssertionError(f"unexpected PUT {path}")

    def delete(self, path, payload=None):
        self.calls.append(("DELETE", path))
        route = urlsplit(path).path
        if route.startswith("/spending/budget/targets/"):
            if self.rollback_failure:
                return self.snapshot()
            target_id = route.rsplit("/", 1)[-1]
            self.targets = [row for row in self.targets if row["id"] != target_id]
            return self.snapshot()
        raise AssertionError(f"unexpected DELETE {path}")

    def backup_database(self):
        self.calls.append(("BACKUP", ""))
        index = len(self.backups)
        record = {
            "filename": f"synthetic-{index:03d}.db",
            "sizeBytes": 2048 + index,
            "modifiedAt": f"2024-07-0{index + 1}T00:00:00Z",
        }
        self.backups.append(record)
        return {"filename": record["filename"]}


def build_proposal(server: SyntheticWealthfolio, *, months=3, min_months=3):
    adapter = SpendingAdapter(server)
    evidence = read_live_evidence(
        adapter,
        trailing_complete_months(AS_OF, months),
        taxonomy_id=TAXONOMY,
        period_key=PERIOD,
    )
    return propose_live_budget(
        evidence,
        min_months=min_months,
        as_of=AS_OF,
        environment_fingerprint=ENVIRONMENT,
        generated_at=datetime(2024, 7, 15, tzinfo=timezone.utc),
    )


def written_proposal(tmp_path: Path, proposal: dict) -> tuple[Path, Path]:
    data_dir = tmp_path / "finance-data"
    (data_dir / "normalized" / "simplefin").mkdir(parents=True)
    path = write_live_proposal(
        proposal,
        data_dir / "normalized" / "simplefin" / "budget-proposal.json",
        data_dir,
        tmp_path / "repo",
    )
    return data_dir, path


def promote(server, proposal, path, data_dir, **overrides):
    kwargs = {
        "environment_fingerprint": ENVIRONMENT,
        "supplied_proposal_fingerprint": proposal["proposalFingerprint"],
        "supplied_environment_fingerprint": ENVIRONMENT,
        "allow_production": True,
        "generated_at": datetime(2024, 7, 16, tzinfo=timezone.utc),
    }
    kwargs.update(overrides)
    return promote_live_budget(server, proposal, path, data_dir, **kwargs)


# -- window ------------------------------------------------------------------


def test_previous_month_crosses_the_year_boundary():
    assert previous_month("2024-01") == "2023-12"
    assert previous_month("2024-07") == "2024-06"


def test_trailing_window_excludes_the_month_in_progress():
    assert trailing_complete_months(date(2024, 7, 1), 3) == ["2024-04", "2024-05", "2024-06"]
    # The last day of a month is still inside that month, so it stays excluded.
    assert trailing_complete_months(date(2024, 7, 31), 1) == ["2024-06"]


def test_trailing_window_requires_at_least_one_month():
    with pytest.raises(DecisionError):
        trailing_complete_months(AS_OF, 0)


def test_month_window_covers_the_whole_month():
    assert month_window("2024-02") == {
        "startDate": "2024-02-01T00:00:00Z",
        "endDate": "2024-02-29T23:59:59Z",
    }


def test_month_window_rejects_the_default_period():
    with pytest.raises(DecisionError):
        month_window("default")


# -- proposal ----------------------------------------------------------------


def test_proposal_only_targets_categories_in_a_budget_group():
    proposal = build_proposal(SyntheticWealthfolio(), min_months=2)
    assert [target["categoryId"] for target in proposal["targets"]] == [
        "cat-gifts",
        "cat-groceries",
    ]
    excluded = {row["categoryId"]: row["reason"] for row in proposal["excluded"]}
    assert excluded == {"cat-unbudgeted": "not-assigned-to-a-budget-group"}


def test_proposal_reports_insufficient_history_instead_of_guessing():
    proposal = build_proposal(SyntheticWealthfolio(), min_months=2)
    insufficient = {
        row["categoryId"]: row["monthsObserved"] for row in proposal["insufficientHistory"]
    }
    # Transit spends in one month and gifts in two, so with a two-month
    # minimum only transit (and the never-spending category) is held back.
    assert insufficient["cat-transit"] == 1
    assert insufficient["cat-quiet"] == 0
    assert "cat-gifts" not in insufficient


def test_a_higher_minimum_holds_back_more_categories():
    proposal = build_proposal(SyntheticWealthfolio(), min_months=3)
    insufficient = {row["categoryId"] for row in proposal["insufficientHistory"]}
    assert insufficient == {"cat-transit", "cat-gifts", "cat-quiet"}


def test_proposal_uses_the_median_rounded_up():
    proposal = build_proposal(SyntheticWealthfolio(), min_months=3)
    assert len(proposal["targets"]) == 1
    target = proposal["targets"][0]
    assert target["categoryId"] == "cat-groceries"
    assert target["medianMonthly"] == "412.00"
    assert target["proposedMonthly"] == "415.00"
    assert target["minimumMonthly"] == "398.50"
    assert target["maximumMonthly"] == "431.25"
    assert target["monthsObserved"] == 3
    assert target["transactions"] == 63


def test_proposal_is_advisory_and_writes_nothing():
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    assert proposal["status"] == "proposal-only"
    assert not [call for call in server.calls if call[0] in {"PUT", "DELETE", "BACKUP"}]
    assert not [call for call in server.calls if "budget/targets" in call[1]]


def test_proposal_rejects_min_months_larger_than_the_window():
    server = SyntheticWealthfolio()
    with pytest.raises(DecisionError):
        build_proposal(server, months=2, min_months=3)


def test_proposal_requires_a_window():
    adapter = SpendingAdapter(SyntheticWealthfolio())
    with pytest.raises(DecisionError):
        read_live_evidence(adapter, [], taxonomy_id=TAXONOMY, period_key=PERIOD)


def test_summary_never_names_a_category_or_an_amount():
    proposal = build_proposal(SyntheticWealthfolio())
    summary = summarize_live_proposal(proposal)
    assert "Groceries" not in summary and "cat-groceries" not in summary
    assert "412" not in summary and "415" not in summary
    assert "3 complete months" in summary


def test_evidence_is_sealed_and_tamper_evident():
    proposal = build_proposal(SyntheticWealthfolio())
    validate_live_proposal(proposal)
    tampered = json.loads(json.dumps(proposal))
    tampered["evidence"]["categorySha256"] = "0" * 64
    with pytest.raises(DecisionError):
        validate_live_proposal(tampered)


def test_proposal_fingerprint_covers_the_amounts():
    proposal = build_proposal(SyntheticWealthfolio())
    tampered = json.loads(json.dumps(proposal))
    tampered["targets"][0]["proposedMonthly"] = "999.00"
    with pytest.raises(DecisionError):
        validate_live_proposal(tampered)


def test_proposal_rejects_a_non_category_target():
    proposal = build_proposal(SyntheticWealthfolio())
    tampered = json.loads(json.dumps(proposal))
    tampered["targets"][0]["targetType"] = "group_buffer"
    tampered["proposalFingerprint"] = plan_fingerprint(
        {key: value for key, value in tampered.items() if key != "proposalFingerprint"}
    )
    with pytest.raises(DecisionError):
        validate_live_proposal(tampered)


def test_amounts_are_written_only_under_the_private_data_dir(tmp_path):
    proposal = build_proposal(SyntheticWealthfolio())
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    with pytest.raises(DecisionError):
        write_live_proposal(proposal, repo_root / "budget.json", tmp_path / "data", repo_root)


def test_written_proposal_round_trips(tmp_path):
    proposal = build_proposal(SyntheticWealthfolio())
    _data_dir, path = written_proposal(tmp_path, proposal)
    assert validate_live_proposal(json.loads(path.read_text(encoding="utf-8")))


def test_evidence_block_is_stable_across_reads():
    server = SyntheticWealthfolio()
    months = trailing_complete_months(AS_OF, 3)
    first = read_live_evidence(SpendingAdapter(server), months, taxonomy_id=TAXONOMY, period_key=PERIOD)
    second = read_live_evidence(SpendingAdapter(server), months, taxonomy_id=TAXONOMY, period_key=PERIOD)
    assert first.evidence_block() == second.evidence_block()


def test_evidence_block_changes_when_live_categorization_changes():
    server = SyntheticWealthfolio()
    months = trailing_complete_months(AS_OF, 3)
    before = read_live_evidence(SpendingAdapter(server), months, taxonomy_id=TAXONOMY, period_key=PERIOD)
    server.assignments.append(
        {"taxonomyId": TAXONOMY, "categoryId": "cat-unbudgeted", "groupId": "grp-essentials"}
    )
    after = read_live_evidence(SpendingAdapter(server), months, taxonomy_id=TAXONOMY, period_key=PERIOD)
    assert before.evidence_block() != after.evidence_block()


def test_evidence_block_hash_is_self_consistent():
    evidence = LiveBudgetEvidence(taxonomy_id=TAXONOMY, period_key=PERIOD, months=["2024-06"])
    block = evidence.evidence_block()
    sealed = {key: value for key, value in block.items() if key != "evidenceSha256"}
    assert block["evidenceSha256"] == plan_fingerprint(sealed)


# -- write narrowing ---------------------------------------------------------


def test_proxy_refuses_to_create_a_category_or_a_group():
    proxy = BudgetTargetOnlyClient(SyntheticWealthfolio())
    with pytest.raises(DecisionError):
        proxy.post("/taxonomies/spending_categories/categories", {})
    with pytest.raises(DecisionError):
        proxy.post("/spending/budget/groups", {})
    with pytest.raises(DecisionError):
        proxy.post("/spending/rules", {})


def test_proxy_refuses_every_put():
    proxy = BudgetTargetOnlyClient(SyntheticWealthfolio())
    with pytest.raises(DecisionError):
        proxy.put("/spending/budget/targets", {})


def test_proxy_refuses_a_non_target_delete():
    proxy = BudgetTargetOnlyClient(SyntheticWealthfolio())
    with pytest.raises(DecisionError):
        proxy.delete("/spending/budget/groups/grp-essentials")


def test_proxy_allows_reads_and_target_writes():
    server = SyntheticWealthfolio()
    proxy = BudgetTargetOnlyClient(server)
    assert proxy.get("/spending/budget")["state"]["groups"]
    assert proxy.post("/spending/report", month_window("2024-06"))["spendingBreakdown"]
    assert proxy.post(
        "/spending/budget/targets?periodKey=default",
        {"taxonomyId": TAXONOMY, "categoryId": "cat-groceries", "amount": "415.00"},
    )


# -- promotion ---------------------------------------------------------------


def test_promotion_writes_targets_and_an_immutable_receipt(tmp_path):
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    data_dir, path = written_proposal(tmp_path, proposal)
    receipt, receipt_path, already = promote(server, proposal, path, data_dir)
    assert already is False
    assert receipt["status"] == "applied"
    assert receipt["mode"] == "live-budget-promotion"
    assert [row["categoryId"] for row in receipt["writtenTargets"]] == ["cat-groceries"]
    assert receipt["backup"]["filename"] != "synthetic-000.db"
    assert not receipt_path.stat().st_mode & stat.S_IWRITE
    assert [row["amount"] for row in server.targets] == ["415.00"]


def test_promotion_is_idempotent(tmp_path):
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    data_dir, path = written_proposal(tmp_path, proposal)
    first, first_path, _ = promote(server, proposal, path, data_dir)
    backups_after_first = len(server.backups)
    second, second_path, already = promote(server, proposal, path, data_dir)
    assert already is True
    assert second == first and second_path == first_path
    assert len(server.backups) == backups_after_first
    assert len(server.targets) == 1


def test_promotion_requires_the_allow_flag(tmp_path):
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    data_dir, path = written_proposal(tmp_path, proposal)
    with pytest.raises(DecisionError, match="allow-production"):
        promote(server, proposal, path, data_dir, allow_production=False)
    assert server.targets == []


def test_promotion_requires_the_exact_proposal_fingerprint(tmp_path):
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    data_dir, path = written_proposal(tmp_path, proposal)
    with pytest.raises(DecisionError, match="proposal fingerprint"):
        promote(server, proposal, path, data_dir, supplied_proposal_fingerprint="0" * 64)
    assert server.targets == []


def test_promotion_requires_the_exact_environment_fingerprint(tmp_path):
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    data_dir, path = written_proposal(tmp_path, proposal)
    with pytest.raises(DecisionError, match="environment fingerprint"):
        promote(server, proposal, path, data_dir, supplied_environment_fingerprint="other")
    with pytest.raises(DecisionError, match="environment fingerprint"):
        promote(server, proposal, path, data_dir, environment_fingerprint="other")
    assert server.targets == []


def test_promotion_refuses_when_live_evidence_changed(tmp_path):
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    data_dir, path = written_proposal(tmp_path, proposal)
    server.assignments.append(
        {"taxonomyId": TAXONOMY, "categoryId": "cat-unbudgeted", "groupId": "grp-essentials"}
    )
    with pytest.raises(DecisionError, match="evidence changed"):
        promote(server, proposal, path, data_dir)
    assert server.targets == []


def test_promotion_refuses_a_conflicting_target(tmp_path):
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    data_dir, path = written_proposal(tmp_path, proposal)
    # A human set a different amount for the same category after the proposal.
    server.targets.append(
        {
            "id": "tgt-human",
            "periodKey": PERIOD,
            "targetType": "category",
            "taxonomyId": TAXONOMY,
            "categoryId": "cat-groceries",
            "amount": "300.00",
        }
    )
    with pytest.raises(DecisionError):
        promote(server, proposal, path, data_dir)
    assert [row["id"] for row in server.targets] == ["tgt-human"]


def test_promotion_refuses_a_partially_applied_proposal(tmp_path):
    server = SyntheticWealthfolio(
        spend={
            month: {**rows, "cat-gifts": rows.get("cat-gifts", ("60.00", 2))}
            for month, rows in SPEND.items()
        }
    )
    proposal = build_proposal(server, min_months=2)
    assert len(proposal["targets"]) == 2
    data_dir, path = written_proposal(tmp_path, proposal)
    applied = proposal["targets"][0]
    server.targets.append(
        {
            "id": "tgt-partial",
            "periodKey": PERIOD,
            "targetType": "category",
            "taxonomyId": TAXONOMY,
            "categoryId": applied["categoryId"],
            "amount": applied["proposedMonthly"],
        }
    )
    with pytest.raises(DecisionError, match="partially applied"):
        promote(server, proposal, path, data_dir)


def test_promotion_refuses_an_applied_state_without_a_receipt(tmp_path):
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    data_dir, path = written_proposal(tmp_path, proposal)
    target = proposal["targets"][0]
    server.targets.append(
        {
            "id": "tgt-orphan",
            "periodKey": PERIOD,
            "targetType": "category",
            "taxonomyId": TAXONOMY,
            "categoryId": target["categoryId"],
            "amount": target["proposedMonthly"],
        }
    )
    with pytest.raises(DecisionError, match="without an immutable receipt"):
        promote(server, proposal, path, data_dir)


def test_promotion_refuses_a_tampered_receipt(tmp_path):
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    data_dir, path = written_proposal(tmp_path, proposal)
    _receipt, receipt_path, _ = promote(server, proposal, path, data_dir)
    receipt_path.chmod(0o600)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["environmentFingerprint"] = "other"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    receipt_path.chmod(0o444)
    with pytest.raises(DecisionError, match="receipt is invalid"):
        promote(server, proposal, path, data_dir)


def test_promotion_refuses_a_mutable_receipt(tmp_path):
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    data_dir, path = written_proposal(tmp_path, proposal)
    _receipt, receipt_path, _ = promote(server, proposal, path, data_dir)
    receipt_path.chmod(0o600)
    with pytest.raises(DecisionError, match="not immutable"):
        promote(server, proposal, path, data_dir)


def test_promotion_rolls_back_when_verification_fails(tmp_path):
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    data_dir, path = written_proposal(tmp_path, proposal)
    server.corrupt_written_amount = True
    with pytest.raises(DecisionError, match="verification failed"):
        promote(server, proposal, path, data_dir)
    assert server.targets == []
    assert not list((data_dir / "normalized" / "simplefin").glob("budget-promotion-*.json"))


def test_promotion_reports_a_failed_rollback(tmp_path):
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    data_dir, path = written_proposal(tmp_path, proposal)
    server.corrupt_written_amount = True
    server.rollback_failure = True
    with pytest.raises(DecisionError, match="rollback failed"):
        promote(server, proposal, path, data_dir)


def test_promotion_refuses_a_proposal_with_no_targets(tmp_path):
    server = SyntheticWealthfolio(spend={month: {} for month in SPEND})
    proposal = build_proposal(server)
    assert proposal["targets"] == []
    data_dir, path = written_proposal(tmp_path, proposal)
    with pytest.raises(DecisionError, match="no targets"):
        promote(server, proposal, path, data_dir)


def test_promotion_refuses_when_the_proposal_file_changed(tmp_path):
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    data_dir, path = written_proposal(tmp_path, proposal)

    def rewrite(instance):
        instance.on_report = None
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")

    server.on_report = rewrite
    with pytest.raises(DecisionError, match="proposal file changed"):
        promote(server, proposal, path, data_dir)
    assert server.targets == []


def test_promotion_never_writes_a_category_or_a_group(tmp_path):
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    data_dir, path = written_proposal(tmp_path, proposal)
    promote(server, proposal, path, data_dir)
    mutations = [
        call
        for call in server.calls
        if call[0] in {"PUT", "DELETE"}
        or (
            call[0] == "POST"
            and "budget/targets" not in call[1]
            and "report" not in call[1]
            and "search" not in call[1]
        )
    ]
    assert mutations == []


# -- command line ------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch):
    """Wire the CLI to a synthetic server instead of a real Wealthfolio."""
    server = SyntheticWealthfolio()

    monkeypatch.setattr(cli_module, "WealthfolioClient", lambda base_url: server)
    monkeypatch.setattr(cli_module, "read_wealthfolio_password", lambda data_dir: "synthetic")
    monkeypatch.setattr(server, "login", lambda password: None, raising=False)
    monkeypatch.setattr(cli_module, "instance_fingerprint", lambda client, base_url: ENVIRONMENT)
    return server


def cli_data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "finance-data"
    (data_dir / "normalized" / "simplefin").mkdir(parents=True)
    return data_dir


def test_cli_budget_propose_writes_only_under_the_data_dir(tmp_path, capsys, cli):
    data_dir = cli_data_dir(tmp_path)
    rc = cli_module.main(
        [
            "budget-propose",
            "--data-dir",
            str(data_dir),
            "--months",
            "3",
            "--min-months",
            "3",
            "--as-of",
            AS_OF.isoformat(),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    proposal = json.loads(
        (data_dir / "normalized" / "simplefin" / "budget-proposal.json").read_text(
            encoding="utf-8"
        )
    )
    assert validate_live_proposal(proposal)
    assert proposal["targets"][0]["categoryId"] == "cat-groceries"
    assert "no budget targets were written" in out
    assert "Groceries" not in out and "415.00" not in out
    assert not [call for call in cli.calls if "budget/targets" in call[1]]


def test_cli_budget_propose_refuses_to_overwrite_without_force(tmp_path, cli):
    data_dir = cli_data_dir(tmp_path)
    argv = ["budget-propose", "--data-dir", str(data_dir), "--months", "3"]
    assert cli_module.main(argv) == 0
    with pytest.raises(SystemExit):
        cli_module.main(argv)
    assert cli_module.main(argv + ["--force"]) == 0


def test_cli_budget_propose_rejects_a_minimum_larger_than_the_window(tmp_path, cli):
    data_dir = cli_data_dir(tmp_path)
    with pytest.raises(SystemExit):
        cli_module.main(
            [
                "budget-propose",
                "--data-dir",
                str(data_dir),
                "--months",
                "2",
                "--min-months",
                "3",
            ]
        )


def test_cli_budget_promote_requires_loopback_production(tmp_path, capsys, cli):
    data_dir = cli_data_dir(tmp_path)
    assert (
        cli_module.main(
            [
                "budget-propose",
                "--data-dir",
                str(data_dir),
                "--months",
                "3",
                "--min-months",
                "3",
                "--as-of",
                AS_OF.isoformat(),
            ]
        )
        == 0
    )
    proposal_path = data_dir / "normalized" / "simplefin" / "budget-proposal.json"
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    capsys.readouterr()

    argv = [
        "budget-promote",
        "--data-dir",
        str(data_dir),
        "--budget-proposal",
        str(proposal_path),
        "--proposal-fingerprint",
        proposal["proposalFingerprint"],
        "--environment-fingerprint",
        ENVIRONMENT,
        "--allow-production",
    ]
    with pytest.raises(SystemExit):
        cli_module.main(argv + ["--base-url", "http://192.168.1.10:8088"])
    assert cli.targets == []

    assert cli_module.main(argv + ["--base-url", "http://127.0.0.1:8088"]) == 0
    first = capsys.readouterr().out
    assert "status=applied" in first and "targets=1" in first
    assert [row["amount"] for row in cli.targets] == ["415.00"]

    assert cli_module.main(argv + ["--base-url", "http://127.0.0.1:8088"]) == 0
    assert "status=already-applied" in capsys.readouterr().out
    assert len(cli.targets) == 1


@pytest.mark.parametrize(
    "command",
    ["promote", "budget-promote"],
)
def test_production_promotion_never_builds_a_client_for_a_remote_url(
    tmp_path, monkeypatch, command
):
    """The admin password must not reach a URL the handler would reject."""
    data_dir = cli_data_dir(tmp_path)
    proposal_path = data_dir / "normalized" / "simplefin" / "proposal.json"
    proposal_path.write_text(json.dumps({"kind": "synthetic"}), encoding="utf-8")
    events: list[tuple[str, str]] = []

    class Recorder:
        def __init__(self, base_url):
            events.append(("client", base_url))

        def login(self, password):
            events.append(("login", password))

        def get(self, path):  # pragma: no cover - must never run
            raise AssertionError("a rejected promotion must not reach the network")

        def post(self, path, payload):  # pragma: no cover - must never run
            raise AssertionError("a rejected promotion must not reach the network")

    def leaking_password(_data_dir):  # pragma: no cover - must never run
        raise AssertionError("a rejected promotion must not read the admin password")

    monkeypatch.setattr(cli_module, "WealthfolioClient", Recorder)
    monkeypatch.setattr(cli_module, "read_wealthfolio_password", leaking_password)

    common = [
        command,
        "--data-dir",
        str(data_dir),
        "--allow-production",
        "--base-url",
        "http://wealthfolio.internal.example:8088",
    ]
    if command == "budget-promote":
        common += [
            "--budget-proposal",
            str(proposal_path),
            "--proposal-fingerprint",
            "synthetic-fingerprint",
            "--environment-fingerprint",
            ENVIRONMENT,
        ]
    else:
        common += ["--plan", str(proposal_path)]

    with pytest.raises(SystemExit):
        cli_module.main(common)

    assert events == []


@pytest.mark.parametrize(
    "base_url",
    [
        "http://192.168.1.10:8088",
        "https://wealthfolio.example.com:8088",
        "http://127.0.0.1:9999",
        "http://127.0.0.1",
        "ftp://127.0.0.1:8088",
        "http://127.0.0.1.evil.example:8088",
    ],
)
def test_only_loopback_port_8088_is_a_production_promotion_target(
    tmp_path, monkeypatch, base_url
):
    data_dir = cli_data_dir(tmp_path)
    proposal_path = data_dir / "normalized" / "simplefin" / "proposal.json"
    proposal_path.write_text(json.dumps({"kind": "synthetic"}), encoding="utf-8")

    def unreachable(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("a rejected promotion must not construct a client")

    monkeypatch.setattr(cli_module, "WealthfolioClient", unreachable)
    monkeypatch.setattr(cli_module, "read_wealthfolio_password", unreachable)

    with pytest.raises(SystemExit):
        cli_module.main(
            [
                "budget-promote",
                "--data-dir",
                str(data_dir),
                "--allow-production",
                "--base-url",
                base_url,
                "--budget-proposal",
                str(proposal_path),
                "--proposal-fingerprint",
                "synthetic-fingerprint",
                "--environment-fingerprint",
                ENVIRONMENT,
            ]
        )


def test_a_net_refunded_month_is_excluded_instead_of_counted_as_spending():
    """abs() turned a refunded month into spending and inflated the median."""
    refunded = {
        "2024-04": {"cat-groceries": ("400.00", 10)},
        "2024-05": {"cat-groceries": ("-50.00", 2)},
        "2024-06": {"cat-groceries": ("400.00", 10)},
    }
    server = SyntheticWealthfolio(spend=refunded)
    evidence = read_live_evidence(
        SpendingAdapter(server),
        trailing_complete_months(AS_OF, 3),
        taxonomy_id=TAXONOMY,
        period_key=PERIOD,
    )
    may_month = next(row for row in evidence.monthly if row["month"] == "2024-05")
    observed = {row["categoryId"]: row["amount"] for row in may_month["rows"]}
    assert observed["cat-groceries"] == "-50.00"

    proposal = build_proposal(server, min_months=3)
    assert [row["categoryId"] for row in proposal["targets"]] == []
    holdback = {row["categoryId"]: row for row in proposal["insufficientHistory"]}
    assert holdback["cat-groceries"]["monthsObserved"] == 2

    lenient = build_proposal(server, min_months=2)
    groceries = next(
        row for row in lenient["targets"] if row["categoryId"] == "cat-groceries"
    )
    assert groceries["medianMonthly"] == "400.00"


def test_rollback_deletes_a_target_the_server_committed_without_recording_it(tmp_path):
    """An ambiguous write can commit server-side and still raise."""
    server = SyntheticWealthfolio()
    proposal = build_proposal(server)
    data_dir, path = written_proposal(tmp_path, proposal)

    committed = {"done": False}
    original_post = server.post

    def ambiguous_post(route_path, payload):
        route = urlsplit(route_path).path
        if route == "/spending/budget/targets" and not committed["done"]:
            committed["done"] = True
            original_post(route_path, payload)
            raise DecisionError("connection reset before the response arrived")
        return original_post(route_path, payload)

    server.post = ambiguous_post

    with pytest.raises(DecisionError):
        promote(server, proposal, path, data_dir)

    assert committed["done"] is True
    assert server.targets == []
    assert not list((data_dir / "normalized" / "simplefin").glob("budget-promotion-*.json"))


def test_cli_budget_promote_requires_the_allow_flag(tmp_path, cli):
    data_dir = cli_data_dir(tmp_path)
    cli_module.main(
        ["budget-propose", "--data-dir", str(data_dir), "--months", "3", "--min-months", "3"]
    )
    proposal_path = data_dir / "normalized" / "simplefin" / "budget-proposal.json"
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    with pytest.raises(SystemExit):
        cli_module.main(
            [
                "budget-promote",
                "--data-dir",
                str(data_dir),
                "--base-url",
                "http://127.0.0.1:8088",
                "--budget-proposal",
                str(proposal_path),
                "--proposal-fingerprint",
                proposal["proposalFingerprint"],
                "--environment-fingerprint",
                ENVIRONMENT,
            ]
        )
    assert cli.targets == []
