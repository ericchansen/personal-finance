"""End-to-end coverage for the source-agnostic categorization CLI.

Every fixture is synthetic: invented accounts, invented merchants, invented
amounts. Nothing here touches a network service.
"""

import csv
import json
from datetime import date
from types import SimpleNamespace

import pytest

from importers.categorize.cli import cmd_merchant_rules, cmd_plan, main
from importers.categorize.live_history import (
    DEFAULT_LOOKBACK_MONTHS,
    MIN_LIVE_EVIDENCE,
)
from importers.monarch.wealthfolio_client import WealthfolioError
from importers.simplefin.categorization import INCOME_TAXONOMY, SPENDING_TAXONOMY
from importers.simplefin.spending_adapter import CAP_SETTINGS_READ

from tests.test_source_categorization import (
    ACCOUNT_MAP,
    LIVE_CHECKING,
    canonical_row,
    live_activity,
)

CANONICAL_COLUMNS = list(canonical_row("monarch:seed"))


class FakeSpendingClient:
    """A minimal, read-only stand-in for the Wealthfolio Spending API."""

    def __init__(self, activities, *, accounts=None, settings=None, assignments=None):
        self.activities = activities
        self.accounts = accounts or [
            {"id": LIVE_CHECKING, "accountType": "CASH", "name": "Synthetic Checking"}
        ]
        self.settings = settings or {"accountIds": [LIVE_CHECKING]}
        self.assignments = {row["id"]: [] for row in activities}
        self.assignments.update(assignments or {})
        self.assignment_reads = []

    def iter_activities(self):
        return iter(self.activities)

    def list_accounts(self):
        return self.accounts

    def get(self, path):
        if path == "/app/info":
            return {"version": "3.7.0-synthetic", "dbPath": "synthetic.db"}
        if path == "/spending/settings":
            return self.settings
        if path == f"/taxonomies/{SPENDING_TAXONOMY}":
            return {
                "taxonomy": {"id": SPENDING_TAXONOMY},
                "categories": [
                    {"id": "groceries", "name": "Groceries"},
                    {"id": "housing", "name": "Rent/Mortgage"},
                ],
            }
        if path == f"/taxonomies/{INCOME_TAXONOMY}":
            return {
                "taxonomy": {"id": INCOME_TAXONOMY},
                "categories": [{"id": "salary", "name": "Salary"}],
            }
        if path.startswith("/spending/activities/"):
            activity_id = path.split("/")[3]
            self.assignment_reads.append(activity_id)
            return self.assignments[activity_id]
        raise AssertionError(f"unexpected GET {path}")

    def _in_window(self, payload):
        start = str(payload.get("startDate") or "")[:10]
        end = str(payload.get("endDate") or "")[:10]
        return [
            row
            for row in self.activities
            if row.get("activityType") not in {"TRANSFER_IN", "TRANSFER_OUT"}
            and not str(row.get("idempotencyKey") or "").startswith("gap:")
            and start <= str(row.get("date") or "")[:10] <= end
        ]

    def post(self, path, payload):
        eligible = self._in_window(payload)
        if path == "/spending/cash-activities/search":
            return {
                "totalCount": sum(
                    1 for row in eligible if not self.assignments.get(row["id"])
                )
            }
        if path == "/spending/report":
            outflow = sum(float(row.get("amount") or 0) for row in eligible)
            return {
                "current": {
                    "income": "0",
                    "outflow": str(outflow),
                    "net": str(-outflow),
                    "count": len(eligible),
                },
                "spendingBreakdown": [],
                "incomeBreakdown": [],
            }
        raise AssertionError(f"unexpected POST {path}")


def private_data_dir(tmp_path, rows, *, account_map=None):
    data_dir = tmp_path / "private-data"
    canonical_dir = data_dir / "normalized" / "canonical"
    canonical_dir.mkdir(parents=True)
    with (canonical_dir / "transactions.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=CANONICAL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    (data_dir / "canonical-account-map.json").write_text(
        json.dumps(ACCOUNT_MAP if account_map is None else account_map),
        encoding="utf-8",
    )
    return data_dir


def plan_args(data_dir, **overrides):
    args = SimpleNamespace(
        data_dir=data_dir,
        base_url="http://127.0.0.1:8088",
        source=None,
        reviewed_plan=None,
        account_map=None,
        monarch_history=None,
        decisions=None,
        no_fallback=False,
        no_live_history=False,
        live_history_lookback_months=DEFAULT_LOOKBACK_MONTHS,
        live_history_min_evidence=MIN_LIVE_EVIDENCE,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_cmd_plan_categorizes_every_source_without_a_reviewed_plan(tmp_path, capsys):
    rows = [
        canonical_row("monarch:row-1", description="Alpha Market"),
        canonical_row(
            "extract:stable:FIT-1",
            description="Beta Servicer",
            category="Mortgage",
            category_id="mortgage",
            amount="-1200.00",
        ),
        canonical_row("simplefin:sf-account:txn-1", description="Gamma Store"),
    ]
    data_dir = private_data_dir(tmp_path, rows)
    client = FakeSpendingClient([
        live_activity("activity-1", "monarch:row-1", description="Alpha Market"),
        live_activity(
            "activity-2",
            f"extract:{LIVE_CHECKING}:FIT-1",
            description="Beta Servicer",
            amount="1200.00",
        ),
        live_activity(
            "activity-3",
            f"simplefin:{LIVE_CHECKING}:txn-1",
            description="Gamma Store",
        ),
    ])

    assert cmd_plan(plan_args(data_dir), client) == 0

    output = capsys.readouterr().out
    assert "sources=*" in output
    plan_paths = list(
        (data_dir / "normalized" / "simplefin").glob("category-plan-*.json")
    )
    assert len(plan_paths) == 1
    plan = json.loads(plan_paths[0].read_text(encoding="utf-8"))
    assert plan["metrics"]["autoCount"] == 3
    assert plan["metrics"]["sourceSystemCounts"] == {
        "extract": 1,
        "monarch": 1,
        "simplefin": 1,
    }
    assert {row["categoryId"] for row in plan["autoCandidates"]} == {
        "groceries",
        "housing",
    }
    assert "Alpha Market" not in json.dumps(plan)
    assert "Beta Servicer" not in json.dumps(plan)


def test_cmd_plan_honours_an_explicit_source_filter(tmp_path):
    rows = [
        canonical_row("monarch:row-1", description="Alpha Market"),
        canonical_row("simplefin:sf-account:txn-1", description="Gamma Store"),
    ]
    data_dir = private_data_dir(tmp_path, rows)
    client = FakeSpendingClient([
        live_activity("activity-1", "monarch:row-1", description="Alpha Market"),
        live_activity(
            "activity-3",
            f"simplefin:{LIVE_CHECKING}:txn-1",
            description="Gamma Store",
        ),
    ])

    assert cmd_plan(plan_args(data_dir, source=["monarch"]), client) == 0

    plan = json.loads(
        next(
            (data_dir / "normalized" / "simplefin").glob("category-plan-*.json")
        ).read_text(encoding="utf-8")
    )
    assert plan["sourceSystems"] == ["monarch"]
    assert plan["metrics"]["scopedActivities"] == 1


def test_cmd_plan_binds_the_account_map_into_sealed_evidence(tmp_path):
    rows = [canonical_row("monarch:row-1", description="Alpha Market")]
    data_dir = private_data_dir(tmp_path, rows)
    client = FakeSpendingClient([
        live_activity("activity-1", "monarch:row-1", description="Alpha Market")
    ])

    cmd_plan(plan_args(data_dir), client)

    plan = json.loads(
        next(
            (data_dir / "normalized" / "simplefin").glob("category-plan-*.json")
        ).read_text(encoding="utf-8")
    )
    bound = {row["path"] for row in plan["evidence"]}
    assert str(data_dir / "canonical-account-map.json") in bound
    assert str(data_dir / "normalized" / "canonical" / "transactions.csv") in bound


def test_cmd_plan_writes_a_blocked_plan_when_spending_is_unsupported(tmp_path):
    rows = [canonical_row("monarch:row-1")]
    data_dir = private_data_dir(tmp_path, rows)

    class BlockedClient(FakeSpendingClient):
        def get(self, path):
            if path == "/spending/settings":
                raise WealthfolioError(404, path, "spending module disabled")
            return super().get(path)

    client = BlockedClient([live_activity("activity-1", "monarch:row-1")])

    assert cmd_plan(plan_args(data_dir), client) == 3

    blocked_paths = list(
        (data_dir / "normalized" / "simplefin").glob("category-plan-blocked-*.json")
    )
    assert len(blocked_paths) == 1
    blocked = json.loads(blocked_paths[0].read_text(encoding="utf-8"))
    assert blocked["blockedCapabilities"][0]["capability"] == CAP_SETTINGS_READ
    assert blocked["policy"]["databaseFallbackPermitted"] is False


def test_cmd_plan_requires_canonical_transactions(tmp_path):
    from importers.rebuild.decisions import DecisionError

    data_dir = tmp_path / "private-data"
    data_dir.mkdir()

    with pytest.raises(DecisionError, match="canonical transactions.csv is required"):
        cmd_plan(plan_args(data_dir), FakeSpendingClient([]))


def test_merchant_rules_command_is_offline_and_writes_private_artifacts(
    tmp_path, capsys
):
    rows = [
        canonical_row("monarch:row-1", date="2026-05-02", description="Alpha Market"),
        canonical_row(
            "extract:stable:FIT-1", date="2026-06-02", description="Alpha Market"
        ),
    ]
    data_dir = private_data_dir(tmp_path, rows)
    args = SimpleNamespace(data_dir=data_dir, monarch_history=None, min_evidence=2)

    assert cmd_merchant_rules(args) == 0

    output = capsys.readouterr().out
    assert "no categories were assigned" in output
    written = sorted((data_dir / "normalized" / "categorize").glob("merchant-rules-*"))
    assert len(written) == 2
    document = json.loads(
        next(path for path in written if path.suffix == ".json").read_text(
            encoding="utf-8"
        )
    )
    assert document["metrics"]["ruleCount"] == 2
    assert all("Alpha Market" not in path.read_text(encoding="utf-8") for path in written)


def filler_canonical_row():
    """A canonical row that cannot match anything in the synthetic window."""
    return canonical_row(
        "monarch:unrelated",
        description="Unrelated Filler",
        amount="-1.23",
        date="2024-01-05",
    )


def latest_plan(data_dir):
    return json.loads(
        max(
            (data_dir / "normalized" / "simplefin").glob("category-plan-*.json"),
            key=lambda path: path.stat().st_mtime,
        ).read_text(encoding="utf-8")
    )


def live_history_client(*, history_date="2025-11-04"):
    """One uncategorized activity plus two already-categorized ones."""
    history = [
        live_activity("history-1", "monarch:old-1", date=history_date),
        live_activity("history-2", "monarch:old-2", date="2026-06-14"),
    ]
    return FakeSpendingClient(
        history + [live_activity("activity-1", "monarch:row-9")],
        assignments={
            row["id"]: [{"taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"}]
            for row in history
        },
    )


def test_cmd_plan_learns_from_wealthfolios_own_category_history(tmp_path, capsys):
    data_dir = private_data_dir(tmp_path, [filler_canonical_row()])

    assert cmd_plan(plan_args(data_dir), live_history_client()) == 0

    plan = latest_plan(data_dir)
    assert plan["metrics"]["autoCount"] == 1
    assert plan["metrics"]["liveHistoryCount"] == 1
    assert plan["metrics"]["merchantIdentityCount"] == 1
    assert plan["liveHistory"]["scope"]["lookbackMonths"] == DEFAULT_LOOKBACK_MONTHS
    output = capsys.readouterr().out
    assert "coverageByEvidence=live-account-history=1" in output
    assert "liveHistoryApplied=1 (account=1 global=0)" in output


def test_cmd_plan_lookback_bounds_the_history_it_learns_from(tmp_path):
    data_dir = private_data_dir(tmp_path, [filler_canonical_row()])

    assert (
        cmd_plan(
            plan_args(data_dir, live_history_lookback_months=3),
            live_history_client(),
        )
        == 0
    )

    plan = latest_plan(data_dir)
    assert plan["liveHistory"]["scope"]["startDate"] == "2026-06-01"
    assert plan["liveHistory"]["metrics"]["observedActivityCount"] == 1
    assert plan["metrics"]["autoCount"] == 0
    assert plan["metrics"]["liveHistoryAbstentionCounts"] == {
        "insufficient-live-history": 1
    }


def test_cmd_plan_can_ignore_wealthfolios_own_category_history(tmp_path):
    data_dir = private_data_dir(tmp_path, [filler_canonical_row()])
    client = live_history_client()

    assert cmd_plan(plan_args(data_dir, no_live_history=True), client) == 0

    plan = latest_plan(data_dir)
    assert "liveHistory" not in plan
    assert plan["metrics"]["autoCount"] == 0
    assert client.assignment_reads == ["activity-1"]


def test_cmd_plan_requires_the_configured_evidence_threshold(tmp_path):
    data_dir = private_data_dir(tmp_path, [filler_canonical_row()])

    assert (
        cmd_plan(
            plan_args(data_dir, live_history_min_evidence=3), live_history_client()
        )
        == 0
    )

    plan = latest_plan(data_dir)
    assert plan["metrics"]["autoCount"] == 0
    assert plan["liveHistory"]["scope"]["minEvidenceCount"] == 3


def test_cmd_plan_never_trains_on_a_transfer_or_a_balance_gap(tmp_path):
    data_dir = private_data_dir(tmp_path, [filler_canonical_row()])
    history = [
        live_activity(
            "history-1", "monarch:old-1", date="2025-11-04", kind="TRANSFER_OUT"
        ),
        live_activity(
            "history-2", f"gap:{LIVE_CHECKING}:2026-06-14", date="2026-06-14"
        ),
    ]
    client = FakeSpendingClient(
        history + [live_activity("activity-1", "monarch:row-9")],
        assignments={
            row["id"]: [{"taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"}]
            for row in history
        },
    )

    assert cmd_plan(plan_args(data_dir), client) == 0

    plan = latest_plan(data_dir)
    assert plan["metrics"]["autoCount"] == 0
    assert plan["liveHistory"]["metrics"]["observedActivityCount"] == 0
    assert plan["liveHistory"]["metrics"]["excludedCounts"] == {
        "reconciliation-source": 1,
        "transfer": 1,
        "uncategorized": 1,
    }


def test_cmd_plan_writes_no_merchant_text_when_learning_from_live_history(tmp_path):
    data_dir = private_data_dir(tmp_path, [filler_canonical_row()])
    history = [
        live_activity(
            "history-1", "monarch:old-1", date="2025-11-04", description="Delta Payee"
        ),
        live_activity(
            "history-2", "monarch:old-2", date="2026-06-14", description="Delta Payee"
        ),
    ]
    client = FakeSpendingClient(
        history
        + [live_activity("activity-1", "monarch:row-9", description="Delta Payee")],
        assignments={
            row["id"]: [{"taxonomyId": SPENDING_TAXONOMY, "categoryId": "groceries"}]
            for row in history
        },
    )

    assert cmd_plan(plan_args(data_dir), client) == 0

    written = list((data_dir / "normalized" / "simplefin").glob("category-*"))
    assert written
    assert all("Delta Payee" not in path.read_text(encoding="utf-8") for path in written)


@pytest.mark.parametrize(
    "flag", ["--live-history-lookback-months", "--live-history-min-evidence"]
)
def test_main_rejects_a_non_positive_live_history_setting(tmp_path, flag, capsys):
    data_dir = tmp_path / "private-data"
    data_dir.mkdir()

    with pytest.raises(SystemExit):
        main([
            "plan",
            "--data-dir",
            str(data_dir),
            "--start-date",
            "2026-08-01",
            "--end-date",
            "2026-08-31",
            flag,
            "0",
        ])

    assert "must be at least 1" in capsys.readouterr().err


def test_main_rejects_a_data_dir_inside_this_repository(tmp_path, capsys):
    from importers.simplefin.categorization import REPO_ROOT

    with pytest.raises(SystemExit):
        main([
            "merchant-rules",
            "--data-dir",
            str(REPO_ROOT / "importers"),
        ])

    assert "cannot be written inside the repository" in capsys.readouterr().err


def test_main_rejects_a_non_loopback_promotion_before_any_login(monkeypatch, tmp_path, capsys):
    data_dir = tmp_path / "private-data"
    data_dir.mkdir()

    def explode(*_args, **_kwargs):
        raise AssertionError("a client must not be constructed for a rejected target")

    monkeypatch.setattr("importers.categorize.cli.WealthfolioClient", explode)

    with pytest.raises(SystemExit):
        main([
            "promote",
            "--data-dir",
            str(data_dir),
            "--base-url",
            "http://evil.example.com:8088",
            "--category-plan",
            str(tmp_path / "plan.json"),
            "--rehearsal-receipt",
            str(tmp_path / "receipt.json"),
            "--plan-fingerprint",
            "x",
            "--environment-fingerprint",
            "y",
        ])

    assert "restricted to loopback port 8088" in capsys.readouterr().err


def test_main_rejects_a_negative_minimum_evidence(tmp_path):
    data_dir = tmp_path / "private-data"
    data_dir.mkdir()

    with pytest.raises(SystemExit):
        main([
            "merchant-rules",
            "--data-dir",
            str(data_dir),
            "--min-evidence",
            "0",
        ])
