"""Plan, rehearse, and promote source-agnostic Wealthfolio category decisions.

``importers.simplefin.categorize_cli`` remains the SimpleFIN-scoped entry point
and is unchanged. This command enumerates *every* spending-enabled cash activity
in a window that carries a stable source identity -- Monarch, mapped CSV/OFX/QFX
extracts, SimpleFIN -- joins each one to canonical transaction evidence, and
seals the result into exactly the same reviewable plan the SimpleFIN flow
already rehearses against staging and promotes to production.

Every artifact is written under the private data directory. Nothing here prints
or stores a merchant description, an amount tied to a name, or an account
number.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from importers.categorize.agent import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MIN_CONFIDENCE,
    AgentCache,
    agent_cache_dir,
    run_agent,
    summarize_agent,
    write_agent_review,
    write_agent_suggestions,
)
from importers.categorize.agent_tools import (
    LocalFileMerchantResearch,
    MerchantResearch,
)
from importers.categorize.identity import (
    SourceResolver,
    build_account_bridge,
    load_canonical_index,
)
from importers.categorize.live_history import (
    DEFAULT_LOOKBACK_MONTHS,
    MIN_LIVE_EVIDENCE,
    build_live_history_index,
    build_live_history_scope,
    summarize_live_history,
)
from importers.categorize.merchant_rules import (
    build_merchant_rules,
    summarize_merchant_rules,
    write_merchant_rule_review,
    write_merchant_rules,
)
from importers.categorize.ollama import (
    DEFAULT_MODEL,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_URL,
    DEFAULT_TIMEOUT_SECONDS,
    OllamaClient,
)
from importers.monarch.wealthfolio_client import WealthfolioClient
from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import instance_fingerprint, validate_private_output
from importers.simplefin.categorization import (
    ALL_SOURCE_SYSTEMS,
    MIN_HISTORY_COUNT,
    REPO_ROOT,
    STRUCTURAL_CANONICAL_KINDS,
    build_category_plan,
    evidence_binding,
    in_source_scope,
    load_category_decisions,
    load_history,
    load_or_create_hash_key,
    normalize_source_systems,
    validate_data_dir,
    write_category_plan,
    write_category_review,
)
from importers.simplefin.categorize_cli import (
    _require_loopback_promotion_url,
    _write_blocked_plan,
    cmd_promote,
    cmd_rehearse,
)
from importers.simplefin.cli import read_wealthfolio_password
from importers.simplefin.spending_adapter import (
    SpendingAdapter,
    SpendingCapabilityBlocked,
)

TRANSFER_ACTIVITY_TYPES = frozenset({"TRANSFER_IN", "TRANSFER_OUT"})


def _optional_latest(folder: Path, pattern: str) -> Path | None:
    paths = sorted(folder.glob(pattern), key=lambda path: path.stat().st_mtime)
    return paths[-1].resolve() if paths else None


def _optional_reviewed_plan(data_dir: Path, supplied: Path | None) -> Path | None:
    """Find a reviewed SimpleFIN source plan, if one exists.

    A reviewed plan is no longer required: it only contributes SimpleFIN account
    mappings, which ``canonical-account-map.json`` can also supply. Requiring it
    would make a Monarch-only or extract-only library unplannable.
    """
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
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime).resolve()


def _load_account_map(data_dir: Path, supplied: Path | None) -> tuple[Path | None, dict]:
    path = supplied.resolve() if supplied else (data_dir / "canonical-account-map.json")
    if not path.is_file():
        return None, {}
    validate_private_output(path, data_dir, REPO_ROOT)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionError(f"cannot read canonical account map: {exc}") from None
    if not isinstance(payload, dict):
        raise DecisionError("canonical-account-map.json must be an object")
    return path, payload


def _canonical_inputs(args: argparse.Namespace, data_dir: Path) -> dict:
    """Resolve, validate and bind every private evidence file a plan reads."""
    canonical = data_dir / "normalized" / "canonical" / "transactions.csv"
    if not canonical.is_file():
        raise DecisionError("canonical transactions.csv is required")
    monarch = (
        args.monarch_history.resolve()
        if args.monarch_history
        else _optional_latest(data_dir / "legacy" / "monarch", "Transactions*.csv")
    )
    reviewed_path = _optional_reviewed_plan(data_dir, args.reviewed_plan)
    account_map_path, account_map = _load_account_map(data_dir, args.account_map)
    decisions_path = (
        args.decisions.resolve()
        if args.decisions
        else data_dir / "simplefin" / "category-decisions.json"
    )
    evidence_paths = [canonical]
    for path in (monarch, reviewed_path, account_map_path):
        if path is not None:
            validate_private_output(path, data_dir, REPO_ROOT)
            evidence_paths.append(path)
    if decisions_path.exists():
        validate_private_output(decisions_path, data_dir, REPO_ROOT)
    reviewed = (
        json.loads(reviewed_path.read_text(encoding="utf-8"))
        if reviewed_path is not None
        else {"accounts": []}
    )
    return {
        "canonical": canonical,
        "monarch": monarch,
        "reviewedPath": reviewed_path,
        "reviewed": reviewed,
        "accountMap": account_map,
        "accountMapPath": account_map_path,
        "decisionsPath": decisions_path,
        "evidencePaths": evidence_paths,
    }


def _live_history_training_set(
    activities: list[dict],
    *,
    scope,
    spending_account_ids: set[str],
) -> list[dict]:
    """Every activity the live-history index is allowed to consider.

    Deliberately unfiltered beyond window and account: the structural,
    direction and uncategorized filters run inside the index, so the plan can
    report exactly how much candidate evidence each one removed instead of
    silently narrowing the denominator here.
    """
    return [
        row
        for row in activities
        if scope.contains(str(row.get("date") or ""))
        and str(row.get("accountId") or "") in spending_account_ids
    ]


def _live_history_assignment_targets(training: list[dict]) -> list[dict]:
    """The subset worth spending an assignment read on.

    The index removes transfers and reconciliation rows *before* it looks at an
    assignment, so asking Wealthfolio about them would be pure round trips.
    """
    return [
        row
        for row in training
        if row.get("activityType") not in TRANSFER_ACTIVITY_TYPES
        and in_source_scope(row, ("*",))
    ]


def _structural_activity_ids(
    activities: list[dict], index, bridge
) -> set[str]:
    """Live activities whose canonical row is a money movement, not a purchase.

    Exact source identity only: this is a *training* filter, and a conservative
    account/date/amount fallback over two years of history would trade a large
    increase in ambiguity for a small increase in exclusions.
    """
    resolver = SourceResolver(index, bridge, activities, allow_fallback=False)
    return {
        str(row.get("id") or "")
        for row in activities
        if resolver(row).transaction_kind in STRUCTURAL_CANONICAL_KINDS
    }


def cmd_plan(args: argparse.Namespace, client: WealthfolioClient) -> int:
    """Write a production read-only category plan across every source."""
    data_dir = validate_data_dir(args.data_dir)
    systems = normalize_source_systems(args.source or [ALL_SOURCE_SYSTEMS])
    inputs = _canonical_inputs(args, data_dir)
    decisions_path = inputs["decisionsPath"]
    decisions = load_category_decisions(
        decisions_path if decisions_path.exists() else None
    )
    hash_key_path, hash_key = load_or_create_hash_key(data_dir)
    adapter = SpendingAdapter(client)
    window = {
        "startDate": f"{args.start_date}T00:00:00Z",
        "endDate": f"{args.end_date}T23:59:59Z",
    }
    index = load_canonical_index(inputs["canonical"])
    bridge = build_account_bridge(
        canonical_account_map=inputs["accountMap"],
        reviewed_plan=inputs["reviewed"],
    )
    try:
        activities = list(client.iter_activities())
        accounts = client.list_accounts()
        spending_account_ids = adapter.spending_account_ids()
        scoped = [
            row
            for row in activities
            if in_source_scope(row, systems)
            and args.start_date.isoformat()
            <= str(row.get("date") or "")[:10]
            <= args.end_date.isoformat()
        ]
        assignments = {
            str(row["id"]): adapter.assignment_rows(row["id"])
            for row in scoped
            if str(row.get("accountId") or "") in spending_account_ids
            and row.get("activityType") not in TRANSFER_ACTIVITY_TYPES
        }
        live_history = None
        if not args.no_live_history:
            scope = build_live_history_scope(
                end_date=args.end_date.isoformat(),
                lookback_months=args.live_history_lookback_months,
                account_ids=spending_account_ids,
                min_evidence=args.live_history_min_evidence,
            )
            training = _live_history_training_set(
                activities,
                scope=scope,
                spending_account_ids=spending_account_ids,
            )
            targets = _live_history_assignment_targets(training)
            history_assignments = {
                **{
                    str(row["id"]): adapter.assignment_rows(row["id"])
                    for row in targets
                    if str(row.get("id") or "") not in assignments
                },
                **assignments,
            }
            live_history = build_live_history_index(
                training,
                history_assignments,
                accounts,
                hash_key,
                scope=scope,
                spending_account_ids=spending_account_ids,
                structural_activity_ids=_structural_activity_ids(
                    targets, index, bridge
                ),
            )
        current_report = adapter.report(window)
        current_uncategorized_count = adapter.uncategorized_count(window)
        catalogs = adapter.catalogs()
    except SpendingCapabilityBlocked as exc:
        return _write_blocked_plan(data_dir, client, args, [exc.status])

    resolver = SourceResolver(
        index, bridge, scoped, allow_fallback=not args.no_fallback
    )

    evidence_paths = list(inputs["evidencePaths"])
    manifest = inputs["canonical"].parent / "manifest.json"
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

    canonical_history = load_history(inputs["canonical"], inputs["monarch"])
    environment_fingerprint = instance_fingerprint(client, args.base_url)

    def build(agent: object | None = None) -> dict:
        return build_category_plan(
            activities,
            accounts,
            assignments,
            catalogs,
            inputs["reviewed"],
            canonical_history,
            decisions,
            evidence_binding(evidence_paths),
            environment_fingerprint,
            hash_key,
            report_start=args.start_date.isoformat(),
            report_end=args.end_date.isoformat(),
            spending_account_ids=spending_account_ids,
            current_report=current_report,
            current_uncategorized_count=current_uncategorized_count,
            known_artifact_activity_ids=known_artifact_ids,
            canonical_review=canonical_review,
            resolver=resolver,
            source_systems=systems,
            live_history=live_history,
            agent_suggestions=agent,
        )

    plan = build()
    agent_summary = ""
    if getattr(args, "ollama", False):
        # The agent is handed the *deterministic* plan, so it can only ever see
        # what every private, canonical and live-history source already failed
        # to resolve. When its suggestions are applied the plan is rebuilt from
        # scratch rather than patched, which keeps precedence identical.
        suggestions = _run_agent_pass(
            args,
            data_dir,
            plan=plan,
            activities=scoped,
            accounts=accounts,
            catalogs=catalogs,
            hash_key=hash_key,
            live_history=live_history,
            canonical_history=canonical_history,
        )
        document = suggestions.as_document()
        suggestion_path = write_agent_suggestions(data_dir, document)
        agent_review_path = write_agent_review(data_dir, document)
        agent_summary = summarize_agent(document)
        print(f"agentSuggestions={suggestion_path}")
        print(f"agentReview={agent_review_path}")
        if args.apply_agent_suggestions:
            plan = build(suggestions)
        else:
            print("agentApplied=no (suggestions are plan-only)")
    path = write_category_plan(data_dir, plan)
    report_path = write_category_review(data_dir, plan)
    metrics = plan["metrics"]
    print(f"plan={path}")
    print(f"review={report_path}")
    print(f"fingerprint={plan['planFingerprint']}")
    print(f"sources={','.join(plan['sourceSystems'])}")
    print(
        "scoped={scoped} bySource={by_source}".format(
            scoped=metrics["scopedActivities"],
            by_source=",".join(
                f"{system or 'unknown'}={count}"
                for system, count in metrics["sourceSystemCounts"].items()
            ),
        )
    )
    print(
        f"auto={metrics['autoCount']} autoAmount={metrics['autoAmount']} "
        f"canonicalCarryover={metrics['canonicalCarryoverCount']} "
        f"manual={metrics['manualCount']} manualAmount={metrics['manualAmount']} "
        f"structural={metrics['structuralKindCount']} "
        f"unresolvedIdentity={metrics['unresolvedIdentityCount']} "
        f"transfers={metrics['transferCount']} "
        f"externalReconciliation={metrics['externalReconciliationCount']} "
        f"transferCandidates={metrics['transferCandidateCount']} "
        f"splits={metrics['splitGroupCount']}"
    )
    print(
        "coverageByEvidence="
        + ",".join(
            f"{kind or 'none'}={count}"
            for kind, count in metrics["evidenceKindCounts"].items()
        )
    )
    print(
        "abstentionsByReason="
        + ",".join(
            f"{reason or 'none'}={count}"
            for reason, count in metrics["manualReasonCounts"].items()
        )
    )
    if plan.get("liveHistory"):
        print(summarize_live_history(plan["liveHistory"]))
        print(
            f"liveHistoryApplied={metrics['liveHistoryCount']} "
            f"(account={metrics['liveHistoryAccountCount']} "
            f"global={metrics['liveHistoryGlobalCount']}) "
            f"merchantIdentity={metrics['merchantIdentityCount']}"
        )
    if agent_summary:
        print(agent_summary)
        print(
            f"agentApplied={metrics.get('agentCount', 0)} "
            f"clusters={metrics.get('agentClusterCount', 0)}"
        )
    return 0


def _agent_min_confidence(value: str) -> Decimal:
    try:
        threshold = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise argparse.ArgumentTypeError(
            "--agent-min-confidence must be a decimal number"
        ) from None
    if not threshold.is_finite() or not (Decimal(0) < threshold <= Decimal(1)):
        raise argparse.ArgumentTypeError(
            "--agent-min-confidence must be greater than 0 and at most 1"
        )
    return threshold


def _merchant_research(args: argparse.Namespace, data_dir: Path) -> MerchantResearch:
    """Build the merchant research backend, which is local-only by design.

    ``none`` (the default) means the model is told nothing about the merchant
    beyond what the private evidence already proves. ``local-file`` reads an
    operator-curated dictionary from the private data directory. There is no web
    backend: looking a payee up on the internet would publish the merchant name,
    and the timing of the query, to a third party.
    """
    if getattr(args, "merchant_research", "none") != "local-file":
        return MerchantResearch()
    path = (
        args.merchant_research_path.resolve()
        if args.merchant_research_path
        else data_dir / "ollama-agent" / "merchant-research.json"
    )
    validate_private_output(path, data_dir, REPO_ROOT)
    if not path.is_file():
        raise DecisionError("local merchant research file does not exist")
    return LocalFileMerchantResearch(path)


def _ollama_client(args: argparse.Namespace) -> OllamaClient:
    return OllamaClient(
        args.ollama_url,
        model=args.ollama_model,
        timeout=args.ollama_timeout,
        num_ctx=args.ollama_num_ctx,
    )


def _run_agent_pass(
    args: argparse.Namespace,
    data_dir: Path,
    *,
    plan: dict,
    activities: list[dict],
    accounts: list[dict],
    catalogs: dict,
    hash_key: bytes,
    live_history,
    canonical_history,
):
    """Ask a local model about the merchants nothing deterministic resolved."""
    client = _ollama_client(args)
    health = client.health()
    if not health.ready:
        raise DecisionError(
            f"local model is not ready: {health.detail or 'model unavailable'}"
        )
    cache = (
        None
        if args.no_agent_cache
        else AgentCache(agent_cache_dir(data_dir), data_dir)
    )
    return run_agent(
        plan=plan,
        activities=activities,
        accounts=accounts,
        catalogs=catalogs,
        client=client,
        merchant_hash_key=hash_key,
        live_history=live_history,
        canonical_history=canonical_history,
        research=_merchant_research(args, data_dir),
        cache=cache,
        min_confidence=args.agent_min_confidence,
        max_attempts=args.agent_max_attempts,
        max_clusters=args.agent_max_clusters,
    )


def cmd_agent_health(args: argparse.Namespace) -> int:
    """Report whether a local model endpoint can answer, and with which model."""
    client = _ollama_client(args)
    health = client.health()
    print(health.summary())
    if health.reachable and not health.model_present:
        print(f"installedModels={','.join(health.installed_models) or 'none'}")
        print(f"hint=pull the model first: ollama pull {args.ollama_model}")
    if health.detail:
        print(f"detail={health.detail}")
    print(f"ready={'yes' if health.ready else 'no'}")
    return 0 if health.ready else 1


def cmd_merchant_rules(args: argparse.Namespace) -> int:
    """Learn exact merchant rules from unanimous reviewed canonical history.

    Purely offline: no Wealthfolio session, no network, no model. A rule exists
    only because ``--min-evidence`` already-reviewed transactions agree.
    """
    data_dir = validate_data_dir(args.data_dir)
    canonical = data_dir / "normalized" / "canonical" / "transactions.csv"
    if not canonical.is_file():
        raise DecisionError("canonical transactions.csv is required")
    validate_private_output(canonical, data_dir, REPO_ROOT)
    monarch = (
        args.monarch_history.resolve()
        if args.monarch_history
        else _optional_latest(data_dir / "legacy" / "monarch", "Transactions*.csv")
    )
    if monarch is not None:
        validate_private_output(monarch, data_dir, REPO_ROOT)
    _hash_key_path, hash_key = load_or_create_hash_key(data_dir)
    history = load_history(canonical, monarch)
    document = build_merchant_rules(
        history, hash_key, min_evidence=args.min_evidence
    )
    path = write_merchant_rules(data_dir, document)
    review = write_merchant_rule_review(data_dir, document)
    print(f"merchantRules={path}")
    print(f"review={review}")
    print(summarize_merchant_rules(document))
    print(f"ruleSetFingerprint={document['ruleSetFingerprint']}")
    print("no categories were assigned")
    return 0


def _add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument(
        "--source",
        action="append",
        help=(
            "source system to enumerate (repeatable); defaults to every "
            "activity carrying a stable source identity"
        ),
    )
    parser.add_argument("--reviewed-plan", type=Path)
    parser.add_argument("--account-map", type=Path)
    parser.add_argument("--monarch-history", type=Path)
    parser.add_argument("--decisions", type=Path)
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help=(
            "require exact source identity; never fall back to a unique "
            "account/date/amount/description match"
        ),
    )
    parser.add_argument(
        "--live-history-lookback-months",
        type=int,
        default=DEFAULT_LOOKBACK_MONTHS,
        help=(
            "how many calendar months of Wealthfolio's own categorized history "
            "to learn exact-merchant consensus from, ending on --end-date "
            f"(default {DEFAULT_LOOKBACK_MONTHS}, long enough to have seen an "
            "annually recurring merchant twice)"
        ),
    )
    parser.add_argument(
        "--live-history-min-evidence",
        type=int,
        default=MIN_LIVE_EVIDENCE,
        help=(
            "distinct already-categorized activities required before a live "
            f"merchant consensus may be applied (default {MIN_LIVE_EVIDENCE})"
        ),
    )
    parser.add_argument(
        "--no-live-history",
        action="store_true",
        help=(
            "ignore Wealthfolio's own existing category assignments and plan "
            "from private canonical evidence alone"
        ),
    )
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)


def _add_ollama_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ollama-url",
        default=DEFAULT_OLLAMA_URL,
        help=(
            "loopback base URL of the local Ollama server "
            f"(default {DEFAULT_OLLAMA_URL}); a non-loopback URL is refused"
        ),
    )
    parser.add_argument(
        "--ollama-model",
        default=DEFAULT_MODEL,
        help=f"local model tag to ask (default {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--ollama-timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"seconds to wait per model call (default {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument("--ollama-num-ctx", type=int, default=DEFAULT_NUM_CTX)


def _add_agent_arguments(parser: argparse.ArgumentParser) -> None:
    _add_ollama_arguments(parser)
    parser.add_argument(
        "--agent-min-confidence",
        type=_agent_min_confidence,
        default=DEFAULT_MIN_CONFIDENCE,
        help=(
            "minimum model confidence before a suggestion may enter a sealed "
            f"plan (default {DEFAULT_MIN_CONFIDENCE}); anything lower stays "
            "manual review"
        ),
    )
    parser.add_argument(
        "--agent-max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=(
            "bounded self-correction attempts per merchant when the model "
            f"returns invalid output (default {DEFAULT_MAX_ATTEMPTS})"
        ),
    )
    parser.add_argument(
        "--agent-max-clusters",
        type=int,
        help=(
            "stop after this many merchant clusters; the largest clusters are "
            "asked first so a bounded run still covers the most activities"
        ),
    )
    parser.add_argument(
        "--no-agent-cache",
        action="store_true",
        help="do not read or write the private decision cache",
    )
    parser.add_argument(
        "--merchant-research",
        choices=("none", "local-file"),
        default="none",
        help=(
            "optional local merchant lookup. 'local-file' reads a private "
            "operator-curated JSON dictionary. There is no web backend: a web "
            "lookup would send merchant names off this machine"
        ),
    )
    parser.add_argument("--merchant-research-path", type=Path)
    parser.add_argument(
        "--apply-agent-suggestions",
        action="store_true",
        help=(
            "seal suggestions at or above --agent-min-confidence into the "
            "category plan; without this the pass is suggestion-only and "
            "assigns nothing"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser(
        "plan",
        help="write a production read-only category plan across every source",
    )
    _add_plan_arguments(plan)
    plan.set_defaults(ollama=False, apply_agent_suggestions=False)

    agent_plan = subparsers.add_parser(
        "agent-plan",
        help=(
            "plan, then ask a local Ollama model about the merchants no "
            "deterministic source could resolve"
        ),
    )
    _add_plan_arguments(agent_plan)
    _add_agent_arguments(agent_plan)
    agent_plan.set_defaults(ollama=True)

    health = subparsers.add_parser(
        "agent-health",
        help="check that the local model endpoint is reachable and the model is installed",
    )
    _add_ollama_arguments(health)

    rules = subparsers.add_parser(
        "merchant-rules",
        help="learn exact merchant rules from reviewed canonical history (offline)",
    )
    rules.add_argument("--data-dir", type=Path, required=True)
    rules.add_argument("--monarch-history", type=Path)
    rules.add_argument("--min-evidence", type=int, default=MIN_HISTORY_COUNT)

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

    args = parser.parse_args(argv)
    if getattr(args, "end_date", None) and args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date")
    if hasattr(args, "wait_seconds") and (
        not math.isfinite(args.wait_seconds) or args.wait_seconds < 0
    ):
        parser.error("--wait-seconds must be a finite non-negative number")
    if args.command == "merchant-rules" and args.min_evidence < 1:
        parser.error("--min-evidence must be at least 1")
    if args.command in {"plan", "agent-plan"}:
        if args.live_history_lookback_months < 1:
            parser.error("--live-history-lookback-months must be at least 1")
        if args.live_history_min_evidence < 1:
            parser.error("--live-history-min-evidence must be at least 1")
    if args.command == "agent-plan":
        if args.agent_max_attempts < 1:
            parser.error("--agent-max-attempts must be at least 1")
        if args.agent_max_clusters is not None and args.agent_max_clusters < 1:
            parser.error("--agent-max-clusters must be at least 1")
        if args.merchant_research_path and args.merchant_research != "local-file":
            parser.error(
                "--merchant-research-path requires --merchant-research local-file"
            )

    try:
        if args.command == "agent-health":
            # Purely a reachability probe: no private data, no Wealthfolio
            # session, and nothing written anywhere.
            return cmd_agent_health(args)
        validate_data_dir(args.data_dir)
        if args.command == "merchant-rules":
            return cmd_merchant_rules(args)
        # Validate a promotion target before a client exists, so the admin
        # password is never sent to a non-loopback URL.
        if args.command == "promote":
            _require_loopback_promotion_url(args.base_url, "category")
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
        if args.command in {"plan", "agent-plan"}:
            return cmd_plan(args, client)
        if args.command == "rehearse":
            return cmd_rehearse(args, client)
        return cmd_promote(args, client)
    except DecisionError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
