"""Safe operations for the isolated PostgreSQL finance shadow authority."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from .postgres import postgres_error_type
from .shadow import (
    DEFAULT_ENVIRONMENT,
    ShadowSafetyError,
    apply_plan,
    create_drift_report,
    create_plan,
    deterministic_export,
    prometheus_metrics,
    read_dsn_file,
    scheduled_run_lock,
    schema_document,
    status_document,
)
from .sources import SourceLoadError

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def private_data_dir(value: Path) -> Path:
    resolved = value.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("data directory must exist")
    try:
        resolved.relative_to(REPOSITORY_ROOT)
    except ValueError:
        return resolved
    raise ValueError("data directory must be outside the public repository")


def private_path(data_dir: Path, value: Path, *, must_exist: bool) -> Path:
    if value.is_absolute():
        raise ValueError("path must be relative to --data-dir")
    resolved = (data_dir / value).resolve(strict=must_exist)
    try:
        resolved.relative_to(data_dir)
    except ValueError as exc:
        raise ValueError("path escapes --data-dir") from exc
    return resolved


def parse_trust_cutoffs(settings: list[str]) -> dict[str, datetime | None]:
    trust_cutoffs = {}
    for setting in settings:
        account_id, separator, raw_cutoff = setting.partition("=")
        if not separator or not account_id or not raw_cutoff:
            raise ValueError(
                "trust cutoff must be SOURCE_ACCOUNT_ID=ISO_TIMESTAMP|none"
            )
        trust_cutoffs[account_id] = (
            None
            if raw_cutoff.casefold() == "none"
            else datetime.fromisoformat(raw_cutoff.replace("Z", "+00:00"))
        )
    return trust_cutoffs


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument(
        "--data-dir",
        required=True,
        type=Path,
        help="Existing private data directory outside this repository",
    )
    root.add_argument(
        "--dsn-file",
        type=Path,
        default=Path("postgres-shadow/secrets/ingest-dsn.txt"),
        help="External secret file relative to --data-dir",
    )
    root.add_argument(
        "--environment",
        default=DEFAULT_ENVIRONMENT,
        help="Expected immutable shadow environment marker",
    )
    commands = root.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    plan.add_argument("--generated-at")

    apply = commands.add_parser("apply")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--plan-hash", required=True)
    apply.add_argument("--backup-basename")

    commands.add_parser("drift")
    commands.add_parser("status")
    commands.add_parser("schema")
    health = commands.add_parser("health")
    health.add_argument("--format", choices=("json", "prometheus"), default="json")

    export = commands.add_parser("export")
    export.add_argument("output", type=Path)

    scheduled = commands.add_parser("scheduled-run")
    scheduled.add_argument("--apply", action="store_true")
    scheduled.add_argument("--backup-basename")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        data_dir = private_data_dir(args.data_dir)
    except ValueError as exc:
        raise SystemExit(f"--data-dir {exc}") from exc
    try:
        dsn = read_dsn_file(data_dir, args.dsn_file)
        if args.command == "plan":
            generated_at = (
                datetime.fromisoformat(args.generated_at.replace("Z", "+00:00"))
                if args.generated_at
                else None
            )
            plan, _path = create_plan(
                data_dir,
                dsn,
                environment=args.environment,
                generated_at=generated_at,
            )
            print(
                json.dumps(
                    {
                        "ready": plan["ready"],
                        "planHash": plan["planHash"],
                        "inputSetHash": plan["inputSetHash"],
                        "expectedStateHash": plan["expectedStateHash"],
                        "counts": plan["counts"],
                        "blockers": plan["blockers"],
                        "gaps": plan["gaps"],
                    },
                    sort_keys=True,
                )
            )
            return 0 if plan["ready"] else 2
        if args.command == "apply":
            result = apply_plan(
                data_dir,
                dsn,
                plan_path=args.plan,
                supplied_hash=args.plan_hash,
                backup_basename=args.backup_basename,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "drift":
            report, _path = create_drift_report(
                data_dir, dsn, environment=args.environment
            )
            print(
                json.dumps(
                    {
                        key: report[key]
                        for key in (
                            "driftHash",
                            "inputSetHash",
                            "stateHash",
                            "missingInputBlobs",
                            "extraDatabaseBlobs",
                            "sourceBlockers",
                            "openQualityIssues",
                            "unresolvedLineageGroups",
                            "sealedPlanFound",
                            "sealedPlanInputMatches",
                            "canonicalStateMatchesSealedPlan",
                        )
                    },
                    sort_keys=True,
                )
            )
            return 0 if report["sourceBlockers"] == 0 else 2
        if args.command == "schema":
            print(
                json.dumps(
                    schema_document(dsn, args.environment), sort_keys=True
                )
            )
            return 0
        if args.command in {"status", "health"}:
            document = status_document(dsn, args.environment)
            if args.command == "health" and args.format == "prometheus":
                print(prometheus_metrics(document), end="")
            else:
                print(json.dumps(document, sort_keys=True))
            return 0
        if args.command == "export":
            result = deterministic_export(data_dir, dsn, args.output)
            print(json.dumps(result, sort_keys=True))
            return 0
        with scheduled_run_lock(data_dir):
            plan, path = create_plan(
                data_dir, dsn, environment=args.environment
            )
            result: dict[str, object] = {
                "ready": plan["ready"],
                "planHash": plan["planHash"],
                "inputSetHash": plan["inputSetHash"],
                "counts": plan["counts"],
                "blockers": plan["blockers"],
                "gaps": plan["gaps"],
            }
            if args.apply and plan["ready"]:
                applied = apply_plan(
                    data_dir,
                    dsn,
                    plan_path=path.relative_to(data_dir),
                    supplied_hash=plan["planHash"],
                    backup_basename=args.backup_basename,
                )
                result["apply"] = applied
            print(json.dumps(result, sort_keys=True))
            return 0 if plan["ready"] else 2
    except (ShadowSafetyError, SourceLoadError, ValueError) as exc:
        raise SystemExit(f"shadow operation refused: {exc}") from exc
    except postgres_error_type() as exc:
        raise SystemExit(
            "shadow operation refused: PostgreSQL rejected the request"
        ) from exc


if __name__ == "__main__":
    raise SystemExit(main())
