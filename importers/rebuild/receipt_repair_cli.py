"""Plan, apply, and verify a receipt-bound Wealthfolio duplicate repair."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
from pathlib import Path
from typing import Any

from importers.analytics.publication import fsync_directory
from importers.monarch.wealthfolio_client import WealthfolioClient, WealthfolioError
from importers.monarch.mutation_guard import MutationInterlockError
from importers.simplefin.spending_adapter import SpendingCapabilityBlocked

from .cutover import CutoverError
from .bounded_promotion import PromotionError
from .decisions import DecisionError
from .receipt_repair import (
    ReceiptRepairError,
    apply_plan,
    build_plan,
    load_plan,
    verify_target,
    write_plan,
    write_receipt,
)
from .projector import (
    REBUILD_MARKER_ENV,
    ProjectionError,
    require_rebuild_target,
    validate_rebuild_boundary,
)
from .safety import validate_private_output
from .immutable_metadata import publish_json


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_ERRORS = (
    OSError,
    ValueError,
    CutoverError,
    DecisionError,
    MutationInterlockError,
    ProjectionError,
    PromotionError,
    ReceiptRepairError,
    SpendingCapabilityBlocked,
    WealthfolioError,
)


def _password(path: Path | None) -> str:
    value = os.environ.get("WEALTHFOLIO_PASSWORD")
    if value:
        return value
    if path is not None:
        return path.read_text(encoding="utf-8").strip()
    return getpass.getpass("Wealthfolio password: ")


def _client(
    base_url: str,
    password_file: Path | None,
    data_dir: Path,
    *,
    marker: str | None,
    expected_instance_id: str,
) -> WealthfolioClient:
    validate_rebuild_boundary(base_url, marker)
    client = WealthfolioClient(base_url, writer_data_dir=data_dir)
    if not client.health():
        raise ReceiptRepairError("repair target is unhealthy")
    client.login(_password(password_file))
    require_rebuild_target(
        client,
        base_url,
        marker,
        expected_instance_id,
    )
    return client


def _summary(document: dict[str, Any]) -> dict[str, Any]:
    counts = document.get("counts") or document.get("operationCounts") or {}
    return {
        "kind": document.get("kind"),
        "status": document.get("status"),
        "planHash": document.get("planHash"),
        "receiptHash": document.get("receiptHash"),
        "counts": counts,
        "verified": document.get("selectedDuplicatePairsRemaining") == 0,
        **({"historicalMatchingReplayAvailable": False,
            "historicalReplayQualificationHash": document["productionLineage"]["historicalReplayQualificationHash"]}
           if (document.get("productionLineage") or {}).get("historicalMatchingReplayAvailable") is False else {}),
    }


def _reservation(path: Path) -> Path:
    return path.with_name(f".{path.name}.reservation")


def _reserve_output(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ReceiptRepairError("repair output path already exists")
    try:
        _reservation(path).open("xb").close()
        fsync_directory(path.parent)
    except FileExistsError:
        raise ReceiptRepairError("repair output path already exists") from None


def _write_reserved(path: Path, document: dict[str, Any]) -> None:
    marker = _reservation(path)
    if not marker.is_file() or marker.stat().st_size != 0:
        raise ReceiptRepairError("repair output reservation changed")
    publish_json(path, document)
    path.chmod(stat.S_IREAD)
    marker.unlink()
    fsync_directory(path.parent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--data-dir", type=Path, required=True)
    plan_parser.add_argument("--selection", type=Path, required=True)
    plan_parser.add_argument("--output", type=Path, required=True)
    plan_parser.add_argument("--production-lineage", type=Path)
    for name in ("apply", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--data-dir", type=Path, required=True)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--base-url", required=True)
        command.add_argument("--password-file", type=Path)
        command.add_argument("--instance-id", required=True)
        command.add_argument("--plan-hash", required=True)
        command.add_argument("--receipt", type=Path, required=True)
        if name == "apply":
            command.add_argument("--backup-download", type=Path, required=True)
            command.add_argument("--recovery", type=Path, required=True)
        else:
            command.add_argument("--report", type=Path, required=True)
            command.add_argument("--recovery", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            selection_path = validate_private_output(
                args.selection, args.data_dir, REPO_ROOT
            )
            output = validate_private_output(
                args.output, args.data_dir, REPO_ROOT
            )
            selection = _load_selection(selection_path)
            document = build_plan(args.data_dir, selection, production_lineage=args.production_lineage)
            write_plan(output, document)
        else:
            plan = load_plan(
                validate_private_output(args.plan, args.data_dir, REPO_ROOT)
            )
            marker = os.environ.get(REBUILD_MARKER_ENV)
            if args.command == "apply":
                receipt_path = validate_private_output(
                    args.receipt, args.data_dir, REPO_ROOT
                )
                recovery_path = validate_private_output(
                    args.recovery, args.data_dir, REPO_ROOT
                )
                backup_path = validate_private_output(
                    args.backup_download, args.data_dir, REPO_ROOT
                )
                if backup_path.exists():
                    raise ReceiptRepairError(
                        "repair backup download path already exists"
                    )
                reserved = []
                try:
                    _reserve_output(receipt_path)
                    reserved.append(receipt_path)
                    _reserve_output(recovery_path)
                    reserved.append(recovery_path)
                    client = _client(
                        args.base_url,
                        args.password_file,
                        args.data_dir,
                        marker=marker,
                        expected_instance_id=args.instance_id,
                    )
                    document = apply_plan(
                        client,
                        plan,
                        base_url=args.base_url,
                        expected_instance_id=args.instance_id,
                        supplied_plan_hash=args.plan_hash,
                        marker=marker,
                        backup_download=backup_path,
                        data_dir=args.data_dir,
                        record_recovery=lambda value: _write_reserved(
                            recovery_path, value
                        ),
                    )
                    _write_reserved(receipt_path, document)
                except EXPECTED_ERRORS:
                    for path in reserved:
                        marker = _reservation(path)
                        if marker.is_file() and marker.stat().st_size == 0:
                            marker.unlink()
                            fsync_directory(marker.parent)
                    raise
            else:
                report_path = validate_private_output(
                    args.report, args.data_dir, REPO_ROOT
                )
                if report_path.exists():
                    raise ReceiptRepairError(
                        "repair verification report already exists"
                    )
                validate_rebuild_boundary(args.base_url, marker)
                client = _client(
                    args.base_url,
                    args.password_file,
                    args.data_dir,
                    marker=marker,
                    expected_instance_id=args.instance_id,
                )
                receipt = load_receipt(
                    validate_private_output(
                        args.receipt, args.data_dir, REPO_ROOT
                    )
                )
                recovery = load_receipt(
                    validate_private_output(
                        args.recovery, args.data_dir, REPO_ROOT
                    )
                )
                document = verify_target(
                    client,
                    plan,
                    receipt,
                    base_url=args.base_url,
                    expected_instance_id=args.instance_id,
                    supplied_plan_hash=args.plan_hash,
                    data_dir=args.data_dir,
                    recovery=recovery,
                    marker=marker,
                )
                write_receipt(
                    report_path,
                    document,
                )
    except EXPECTED_ERRORS as exc:
        print(f"ERROR: {exc}")
        return 1
    print(json.dumps(_summary(document), sort_keys=True))
    return 0


def _load_selection(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptRepairError("repair selection is unavailable") from exc
    if not isinstance(value, dict):
        raise ReceiptRepairError("repair selection is not an object")
    return value


def load_receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptRepairError("repair receipt is unavailable") from exc
    if not isinstance(value, dict):
        raise ReceiptRepairError("repair receipt is not an object")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
