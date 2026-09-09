"""Build, rehearse, and explicitly promote private SimpleFIN category plans."""

from __future__ import annotations

import argparse
import json
import math
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from importers.monarch.wealthfolio_client import WealthfolioClient
from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import (
    instance_fingerprint,
    validate_apply_target,
    validate_private_output,
)
from importers.simplefin.categorization import (
    REPO_ROOT,
    build_blocked_category_plan,
    build_category_plan,
    evidence_binding,
    load_category_decisions,
    load_history,
    load_or_create_hash_key,
    promote_category_plan,
    rehearse_category_plan,
    sha256_file,
    validate_category_plan,
    validate_data_dir,
    write_blocked_category_plan,
    write_category_plan,
    write_category_review,
    write_rehearsal_receipt,
)
from importers.simplefin.budget_proposal import MIN_MONTHS
from importers.simplefin.category_rules import propose_v2_from_v1
from importers.simplefin.cli import read_wealthfolio_password
from importers.simplefin.live_budget import (
    promote_live_budget,
    propose_live_budget,
    read_live_evidence,
    summarize_live_proposal,
    trailing_complete_months,
    validate_live_proposal,
    write_live_proposal,
)
from importers.simplefin.spending_adapter import (
    DEFAULT_PERIOD_KEY,
    SPENDING_TAXONOMY,
    CapabilityStatus,
    SpendingAdapter,
    SpendingCapabilityBlocked,
)
from importers.simplefin.taxonomy_plan import (
    RECOMMENDED_TAXONOMIES,
    build_desired_categories,
    plan_taxonomy_difference,
    write_taxonomy_plan,
)
from importers.simplefin.taxonomy_plan import summarize_plan as summarize_taxonomy_plan


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


def _write_blocked_plan(
    data_dir: Path,
    client: WealthfolioClient,
    args: argparse.Namespace,
    blocked: list[CapabilityStatus],
) -> int:
    """Persist an actionable blocked-plan artifact instead of crashing.

    Never falls back to Wealthfolio's SQLite database and never mutates
    anything; it is a sealed, private record of exactly which Spending
    capability is unsupported, incompatible, or broken so an operator can
    act on it deliberately.
    """
    plan = build_blocked_category_plan(
        blocked,
        instance_fingerprint(client, args.base_url),
        report_start=args.start_date.isoformat(),
        report_end=args.end_date.isoformat(),
    )
    path = write_blocked_category_plan(data_dir, plan)
    print(f"blocked={path}")
    for status in blocked:
        print(
            f"blockedCapability={status.capability} status={status.status} "
            f"endpoint={status.endpoint}"
        )
    return 3


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
    adapter = SpendingAdapter(client)
    window = {
        "startDate": f"{args.start_date}T00:00:00Z",
        "endDate": f"{args.end_date}T23:59:59Z",
    }
    try:
        activities = list(client.iter_activities())
        spending_account_ids = adapter.spending_account_ids()
        simplefin = [
            row
            for row in activities
            if str(row.get("idempotencyKey") or "").startswith("simplefin:")
            and args.start_date.isoformat()
            <= str(row.get("date") or "")[:10]
            <= args.end_date.isoformat()
        ]
        assignments = {
            str(row["id"]): adapter.assignment_rows(row["id"])
            for row in simplefin
            if str(row.get("accountId") or "") in spending_account_ids
            and row.get("activityType") not in {"TRANSFER_IN", "TRANSFER_OUT"}
        }
        current_report = adapter.report(window)
        current_uncategorized_count = adapter.uncategorized_count(window)
        catalogs = adapter.catalogs()
    except SpendingCapabilityBlocked as exc:
        return _write_blocked_plan(data_dir, client, args, [exc.status])
    evidence_paths = [canonical, monarch, reviewed_path]
    manifest = canonical.parent / "manifest.json"
    canonical_review = {}
    if manifest.exists():
        evidence_paths.append(manifest)
        try:
            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DecisionError(f"cannot read canonical manifest: {exc}") from None
        if not isinstance(manifest_payload, dict):
            raise DecisionError("canonical manifest must be an object")
        canonical_review = {
            "transferReview": manifest_payload.get("transferReview", {}),
            "splitReview": manifest_payload.get("splitReview", {}),
        }
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
        catalogs,
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
        current_uncategorized_count=current_uncategorized_count,
        known_artifact_activity_ids=known_artifact_ids,
        canonical_review=canonical_review,
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
        f"externalReconciliation={metrics['externalReconciliationCount']} "
        f"transferCandidates={metrics['transferCandidateCount']} "
        f"splits={metrics['splitGroupCount']}"
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
    return 3 if receipt["status"] == "blocked" else 0


def _require_loopback_promotion_url(base_url: str, what: str) -> None:
    """Reject a non-loopback promotion target before any credential is sent.

    ``main`` calls this *before* constructing a client or logging in, so a
    mistyped or hostile ``--base-url`` never receives the Wealthfolio admin
    password. The promotion handlers call it again so the guard holds for any
    other caller.
    """
    target = urlparse(base_url)
    if (
        target.scheme not in {"http", "https"}
        or target.hostname not in {"127.0.0.1", "localhost", "::1"}
        or target.port != 8088
    ):
        raise DecisionError(
            f"{what} production promotion is restricted to loopback port 8088"
        )


def cmd_promote(args: argparse.Namespace, client: WealthfolioClient) -> int:
    data_dir = validate_data_dir(args.data_dir)
    _require_loopback_promotion_url(args.base_url, "category")
    plan_path = args.category_plan.resolve()
    receipt_path = args.rehearsal_receipt.resolve()
    validate_private_output(plan_path, data_dir, REPO_ROOT)
    validate_private_output(receipt_path, data_dir, REPO_ROOT)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    _hash_key_path, hash_key = load_or_create_hash_key(data_dir)
    environment = instance_fingerprint(client, args.base_url)
    receipt, output, already = promote_category_plan(
        client,
        plan,
        plan_path,
        receipt_path,
        data_dir,
        hash_key,
        environment_fingerprint=environment,
        supplied_plan_fingerprint=args.plan_fingerprint,
        supplied_environment_fingerprint=args.environment_fingerprint,
        allow_production=args.allow_production,
        wait_seconds=args.wait_seconds,
    )
    status = "already-applied" if already else receipt["status"]
    print(
        f"receipt={output} status={status} "
        f"targets={len(plan['autoCandidates'])}"
    )
    return 0


def cmd_migrate_decisions(args: argparse.Namespace) -> int:
    """Propose a v2 rule-engine document from v1 decisions without mutating it.

    This never requires a live Wealthfolio session: it is purely a local,
    offline text transform. Real category-ID existence is still fail-closed
    checked later, at actual plan-build time, against live catalogs.
    """
    data_dir = validate_data_dir(args.data_dir)
    decisions_path = (
        args.decisions.resolve()
        if args.decisions
        else data_dir / "simplefin" / "category-decisions.json"
    )
    if not decisions_path.exists():
        raise DecisionError(f"no private category decisions found at {decisions_path}")
    validate_private_output(decisions_path, data_dir, REPO_ROOT)

    output_path = (
        args.output.resolve()
        if args.output
        else decisions_path.with_name(f"{decisions_path.stem}.v2-proposal.json")
    )
    if output_path == decisions_path:
        raise DecisionError("migration output path must differ from the source decisions file")
    validate_private_output(output_path, data_dir, REPO_ROOT)
    if output_path.exists() and not args.force:
        raise DecisionError(
            f"{output_path} already exists; pass --force to overwrite the proposal"
        )

    decisions = load_category_decisions(decisions_path)
    if decisions["schemaVersion"] != 1:
        raise DecisionError("migration source must be a schemaVersion 1 document")
    proposal = propose_v2_from_v1(decisions)
    output_path.write_text(json.dumps(proposal, indent=2), encoding="utf-8")
    print(f"proposal={output_path}")
    print(f"rules={len(proposal['rules'])}")
    print("source file was not modified")
    return 0


def cmd_taxonomy_plan(args: argparse.Namespace, client: WealthfolioClient) -> int:
    """Diff the live spending taxonomy against the recommended compact shape.

    Read-only: it calls ``GET /taxonomies/{id}`` and writes a private plan.
    Nothing is created, renamed, moved, or deleted.
    """
    data_dir = validate_data_dir(args.data_dir)
    adapter = SpendingAdapter(client)
    taxonomy_id = args.taxonomy
    live = adapter.taxonomy(taxonomy_id)
    spec = RECOMMENDED_TAXONOMIES.get(taxonomy_id)
    if spec is None:
        raise DecisionError(
            f"no recommended hierarchy is defined for taxonomy {taxonomy_id!r}"
        )
    plan = plan_taxonomy_difference(
        live, build_desired_categories(spec), taxonomy_id=taxonomy_id
    )
    output = (
        args.output.resolve()
        if args.output
        else data_dir / "normalized" / "simplefin" / f"taxonomy-plan-{taxonomy_id}.json"
    )
    path = write_taxonomy_plan(plan, output, data_dir, REPO_ROOT)
    print(f"taxonomyPlan={path}")
    print(summarize_taxonomy_plan(plan))
    print(f"planFingerprint={plan['fingerprint']}")
    print("no taxonomy changes were applied")
    return 0


def cmd_budget_propose(args: argparse.Namespace, client: WealthfolioClient) -> int:
    """Propose monthly budget targets from live Wealthfolio spending history.

    Read-only: one ``POST /spending/report`` per complete month in the
    caller-specified trailing window, plus ``GET /taxonomies/{id}`` and
    ``GET /spending/budget``. Nothing is created and no budget is written.
    Only categories a human already assigned to a native budget group are
    considered; categories are never created to make a proposal fit.
    """
    data_dir = validate_data_dir(args.data_dir)
    as_of = args.as_of or date.today()
    months = trailing_complete_months(as_of, args.months)
    adapter = SpendingAdapter(client)
    evidence = read_live_evidence(
        adapter,
        months,
        taxonomy_id=args.taxonomy,
        period_key=args.period,
    )
    proposal = propose_live_budget(
        evidence,
        min_months=args.min_months,
        as_of=as_of,
        environment_fingerprint=instance_fingerprint(client, args.base_url),
    )
    output = (
        args.output.resolve()
        if args.output
        else data_dir / "normalized" / "simplefin" / "budget-proposal.json"
    )
    if output.exists() and not args.force:
        raise DecisionError(f"{output} already exists; pass --force to overwrite it")
    path = write_live_proposal(proposal, output, data_dir, REPO_ROOT)
    print(f"budgetProposal={path}")
    print(summarize_live_proposal(proposal))
    print(f"proposalFingerprint={proposal['proposalFingerprint']}")
    print(f"evidenceFingerprint={proposal['evidence']['evidenceSha256']}")
    print(f"environmentFingerprint={proposal['environmentFingerprint']}")
    print("no budget targets were written")
    return 0


def cmd_budget_promote(args: argparse.Namespace, client: WealthfolioClient) -> int:
    """Write an exactly fingerprinted budget proposal to production, or prove it applied."""
    data_dir = validate_data_dir(args.data_dir)
    _require_loopback_promotion_url(args.base_url, "budget")
    proposal_path = args.budget_proposal.resolve()
    validate_private_output(proposal_path, data_dir, REPO_ROOT)
    proposal = validate_live_proposal(
        json.loads(proposal_path.read_text(encoding="utf-8"))
    )
    receipt, output, already = promote_live_budget(
        client,
        proposal,
        proposal_path,
        data_dir,
        environment_fingerprint=instance_fingerprint(client, args.base_url),
        supplied_proposal_fingerprint=args.proposal_fingerprint,
        supplied_environment_fingerprint=args.environment_fingerprint,
        allow_production=args.allow_production,
    )
    status = "already-applied" if already else receipt["status"]
    print(f"receipt={output} status={status} targets={len(proposal['targets'])}")
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

    promote = subparsers.add_parser(
        "promote", help="promote an exactly rehearsed category plan to production"
    )
    promote.add_argument("--data-dir", type=Path, required=True)
    promote.add_argument("--base-url", required=True)
    promote.add_argument("--category-plan", type=Path, required=True)
    promote.add_argument("--rehearsal-receipt", type=Path, required=True)
    promote.add_argument("--plan-fingerprint", required=True)
    promote.add_argument("--environment-fingerprint", required=True)
    promote.add_argument("--allow-production", action="store_true")
    promote.add_argument("--wait-seconds", type=float, default=10)

    migrate = subparsers.add_parser(
        "migrate-decisions",
        help="propose a schema v2 rule document from v1 decisions (does not modify the source)",
    )
    migrate.add_argument("--data-dir", type=Path, required=True)
    migrate.add_argument("--decisions", type=Path)
    migrate.add_argument("--output", type=Path)
    migrate.add_argument("--force", action="store_true")

    taxonomy = subparsers.add_parser(
        "taxonomy-plan",
        help="diff the live taxonomy against the recommended compact hierarchy (read-only)",
    )
    taxonomy.add_argument("--data-dir", type=Path, required=True)
    taxonomy.add_argument("--base-url", default="http://127.0.0.1:8088")
    taxonomy.add_argument("--taxonomy", default=SPENDING_TAXONOMY)
    taxonomy.add_argument("--output", type=Path)

    budget = subparsers.add_parser(
        "budget-propose",
        help=(
            "propose monthly budget targets from live Wealthfolio spending "
            "history (read-only; never writes a budget)"
        ),
    )
    budget.add_argument("--data-dir", type=Path, required=True)
    budget.add_argument("--base-url", default="http://127.0.0.1:8088")
    budget.add_argument(
        "--months",
        type=int,
        required=True,
        help="number of trailing complete months to analyze",
    )
    budget.add_argument("--min-months", type=int, default=MIN_MONTHS)
    budget.add_argument(
        "--as-of",
        type=date.fromisoformat,
        help="anchor date; the month containing it is incomplete and excluded",
    )
    budget.add_argument("--taxonomy", default=SPENDING_TAXONOMY)
    budget.add_argument("--period", default=DEFAULT_PERIOD_KEY)
    budget.add_argument("--output", type=Path)
    budget.add_argument("--force", action="store_true")

    budget_promote = subparsers.add_parser(
        "budget-promote",
        help="promote an exactly fingerprinted live budget proposal to production",
    )
    budget_promote.add_argument("--data-dir", type=Path, required=True)
    budget_promote.add_argument("--base-url", required=True)
    budget_promote.add_argument("--budget-proposal", type=Path, required=True)
    budget_promote.add_argument("--proposal-fingerprint", required=True)
    budget_promote.add_argument("--environment-fingerprint", required=True)
    budget_promote.add_argument("--allow-production", action="store_true")

    args = parser.parse_args(argv)
    if getattr(args, "end_date", None) and args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date")
    if hasattr(args, "wait_seconds") and (
        not math.isfinite(args.wait_seconds) or args.wait_seconds < 0
    ):
        parser.error("--wait-seconds must be a finite non-negative number")
    if args.command == "budget-propose":
        if args.months < 1:
            parser.error("--months must be at least 1")
        if args.min_months < 1:
            parser.error("--min-months must be at least 1")
        if args.min_months > args.months:
            parser.error("--min-months cannot exceed --months")

    try:
        validate_data_dir(args.data_dir)
        if args.command == "migrate-decisions":
            return cmd_migrate_decisions(args)
        # Validate a promotion target before a client exists, so the admin
        # password is never sent to a non-loopback URL that would be rejected
        # only once the handler runs.
        if args.command == "promote":
            _require_loopback_promotion_url(args.base_url, "category")
        if args.command == "budget-promote":
            _require_loopback_promotion_url(args.base_url, "budget")
        writer_data_dir = (
            args.staging_data_dir
            if args.command == "rehearse"
            else args.data_dir
        )
        client = WealthfolioClient(
            args.base_url, writer_data_dir=writer_data_dir
        )
        if args.command == "rehearse":
            validate_data_dir(args.staging_data_dir)
        client.login(read_wealthfolio_password(args.data_dir))
        if args.command == "plan":
            return cmd_plan(args, client)
        if args.command == "taxonomy-plan":
            return cmd_taxonomy_plan(args, client)
        if args.command == "budget-propose":
            return cmd_budget_propose(args, client)
        if args.command == "budget-promote":
            return cmd_budget_promote(args, client)
        if args.command == "rehearse":
            return cmd_rehearse(args, client)
        return cmd_promote(args, client)
    except DecisionError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
