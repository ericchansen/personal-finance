"""Build private SimpleFIN category plans or rehearse them on staging."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from importers.monarch.wealthfolio_client import WealthfolioClient
from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import (
    instance_fingerprint,
    validate_apply_target,
    validate_private_output,
)
from importers.simplefin.categorization import (
    INCOME_TAXONOMY,
    REPO_ROOT,
    SPENDING_TAXONOMY,
    build_category_plan,
    evidence_binding,
    load_category_decisions,
    load_history,
    load_or_create_hash_key,
    rehearse_category_plan,
    sha256_file,
    validate_category_plan,
    validate_data_dir,
    write_category_plan,
    write_category_review,
    write_rehearsal_receipt,
)
from importers.simplefin.cli import read_wealthfolio_password


def _latest(folder: Path, pattern: str, description: str) -> Path:
    paths = sorted(folder.glob(pattern), key=lambda path: path.stat().st_mtime)
    if not paths:
        raise DecisionError(f"no {description} found under {folder}")
    return paths[-1]


def _reviewed_plan(data_dir: Path, supplied: Path | None) -> Path:
    if supplied:
        return supplied.resolve()
    candidates = []
    for path in (data_dir / "normalized" / "simplefin").glob("plan-*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload.get("accounts"), list) and payload.get("snapshot"):
            candidates.append(path)
    if not candidates:
        raise DecisionError("no reviewed SimpleFIN source plan was found")
    return max(candidates, key=lambda path: path.stat().st_mtime).resolve()


def _catalogs(client: WealthfolioClient) -> dict[str, list[dict]]:
    result = {}
    for taxonomy_id in (SPENDING_TAXONOMY, INCOME_TAXONOMY):
        payload = client.get(f"/taxonomies/{taxonomy_id}") or {}
        categories = payload.get("categories")
        if not isinstance(categories, list):
            raise DecisionError(f"Wealthfolio taxonomy is unavailable: {taxonomy_id}")
        result[taxonomy_id] = categories
    return result


def cmd_plan(args: argparse.Namespace, client: WealthfolioClient) -> int:
    data_dir = validate_data_dir(args.data_dir)
    canonical = data_dir / "normalized" / "canonical" / "transactions.csv"
    if not canonical.is_file():
        raise DecisionError("canonical transactions.csv is required")
    monarch = (
        args.monarch_history.resolve()
        if args.monarch_history
        else _latest(data_dir / "legacy" / "monarch", "Transactions*.csv", "Monarch history")
    )
    reviewed_path = _reviewed_plan(data_dir, args.reviewed_plan)
    decisions_path = (
        args.decisions.resolve()
        if args.decisions
        else data_dir / "simplefin" / "category-decisions.json"
    )
    for path in (canonical, monarch, reviewed_path):
        validate_private_output(path, data_dir, REPO_ROOT)
    if decisions_path.exists():
        validate_private_output(decisions_path, data_dir, REPO_ROOT)

    reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
    decisions = load_category_decisions(
        decisions_path if decisions_path.exists() else None
    )
    hash_key_path, hash_key = load_or_create_hash_key(data_dir)
    activities = list(client.iter_activities())
    settings = client.get("/spending/settings") or {}
    spending_account_ids = {
        str(account_id) for account_id in settings.get("accountIds") or []
    }
    simplefin = [
        row
        for row in activities
        if str(row.get("idempotencyKey") or "").startswith("simplefin:")
        and args.start_date.isoformat()
        <= str(row.get("date") or "")[:10]
        <= args.end_date.isoformat()
    ]
    assignments = {
        str(row["id"]): client.get(
            f"/spending/activities/{row['id']}/assignments"
        )
        for row in simplefin
        if str(row.get("accountId") or "") in spending_account_ids
        and row.get("activityType") not in {"TRANSFER_IN", "TRANSFER_OUT"}
    }
    window = {
        "startDate": f"{args.start_date}T00:00:00Z",
        "endDate": f"{args.end_date}T23:59:59Z",
    }
    current_report = client.post("/spending/report", window)
    uncategorized = client.post(
        "/spending/cash-activities/search",
        {
            "status": "uncategorized",
            **window,
            "limit": 1,
            "offset": 0,
        },
    )
    evidence_paths = [canonical, monarch, reviewed_path]
    manifest = canonical.parent / "manifest.json"
    if manifest.exists():
        evidence_paths.append(manifest)
    evidence_paths.extend(sorted((data_dir / "facts").glob("*.json")))
    if decisions_path.exists():
        evidence_paths.append(decisions_path)
    evidence_paths.append(hash_key_path)
    artifact_paths = sorted(
        (data_dir / "normalized" / "simplefin").glob("spending-artifact-*.json"),
        key=lambda path: path.stat().st_mtime,
    )
    known_artifact_ids: set[str] = set()
    if artifact_paths:
        artifact_path = artifact_paths[-1]
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact_id = str(artifact.get("activity", {}).get("id") or "")
        if artifact_id:
            known_artifact_ids.add(artifact_id)
            evidence_paths.append(artifact_path)
    plan = build_category_plan(
        activities,
        client.list_accounts(),
        assignments,
        _catalogs(client),
        reviewed,
        load_history(canonical, monarch),
        decisions,
        evidence_binding(evidence_paths),
        instance_fingerprint(client, args.base_url),
        hash_key,
        report_start=args.start_date.isoformat(),
        report_end=args.end_date.isoformat(),
        spending_account_ids=spending_account_ids,
        current_report=current_report,
        current_uncategorized_count=int(uncategorized.get("totalCount") or 0),
        known_artifact_activity_ids=known_artifact_ids,
    )
    path = write_category_plan(data_dir, plan)
    report_path = write_category_review(data_dir, plan)
    metrics = plan["metrics"]
    print(f"plan={path}")
    print(f"review={report_path}")
    print(f"fingerprint={plan['planFingerprint']}")
    print(
        f"auto={metrics['autoCount']} autoAmount={metrics['autoAmount']} "
        f"manual={metrics['manualCount']} manualAmount={metrics['manualAmount']} "
        f"transfers={metrics['transferCount']} "
        f"externalReconciliation={metrics['externalReconciliationCount']}"
    )
    return 0


def cmd_rehearse(args: argparse.Namespace, client: WealthfolioClient) -> int:
    data_dir = validate_data_dir(args.data_dir)
    staging_data_dir = validate_data_dir(args.staging_data_dir)
    plan_path = args.category_plan.resolve()
    map_path = args.rebuild_map.resolve()
    validate_private_output(plan_path, data_dir, REPO_ROOT)
    validate_private_output(map_path, staging_data_dir, REPO_ROOT)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    validate_category_plan(plan)
    validate_apply_target(
        client,
        args.base_url,
        plan["planFingerprint"],
        args.plan_fingerprint,
    )
    stage_map = json.loads(map_path.read_text(encoding="utf-8"))
    _hash_key_path, hash_key = load_or_create_hash_key(data_dir)
    receipt = rehearse_category_plan(client, plan, stage_map, hash_key)
    receipt["categoryPlanSha256"] = sha256_file(plan_path)
    path = write_rehearsal_receipt(data_dir, receipt, plan_path)
    print(
        f"receipt={path} status={receipt['status']} "
        f"applied={receipt['appliedCount']} targets={receipt['targetCount']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="write a production read-only category plan")
    plan.add_argument("--data-dir", type=Path, required=True)
    plan.add_argument("--base-url", default="http://127.0.0.1:8088")
    plan.add_argument("--reviewed-plan", type=Path)
    plan.add_argument("--monarch-history", type=Path)
    plan.add_argument("--decisions", type=Path)
    plan.add_argument("--start-date", type=date.fromisoformat, required=True)
    plan.add_argument("--end-date", type=date.fromisoformat, required=True)

    rehearse = subparsers.add_parser(
        "rehearse", help="apply a sealed category plan to isolated staging"
    )
    rehearse.add_argument("--data-dir", type=Path, required=True)
    rehearse.add_argument("--base-url", required=True)
    rehearse.add_argument("--category-plan", type=Path, required=True)
    rehearse.add_argument("--rebuild-map", type=Path, required=True)
    rehearse.add_argument("--staging-data-dir", type=Path, required=True)
    rehearse.add_argument("--plan-fingerprint", required=True)

    args = parser.parse_args(argv)
    if getattr(args, "end_date", None) and args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date")
    client = WealthfolioClient(args.base_url)
    try:
        validate_data_dir(args.data_dir)
        if args.command == "rehearse":
            validate_data_dir(args.staging_data_dir)
        client.login(read_wealthfolio_password(args.data_dir))
        return cmd_plan(args, client) if args.command == "plan" else cmd_rehearse(args, client)
    except DecisionError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
