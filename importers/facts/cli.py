"""Validate and summarize a directory of durable financial facts."""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from .assertions import load_balance_snapshot, verify_assertions
    from .loader import load_facts, summarize
except ImportError:  # pragma: no cover - lets ``python cli.py`` work.
    from assertions import load_balance_snapshot, verify_assertions  # type: ignore
    from loader import load_facts, summarize  # type: ignore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "summary", "verify-assertions"))
    parser.add_argument("--facts-dir", default="D:/documents/finance-data/facts")
    parser.add_argument(
        "--snapshot",
        help="JSON balance snapshot (required by verify-assertions)",
    )
    args = parser.parse_args(argv)

    result = load_facts(Path(args.facts_dir))
    if args.command == "verify-assertions":
        if not args.snapshot:
            parser.error("--snapshot is required by verify-assertions")
        snapshot, snapshot_errors = load_balance_snapshot(args.snapshot)
        verification = verify_assertions(result, snapshot, snapshot_errors)
        for error in verification.errors:
            print(f"error: {error}")
        for mismatch in verification.mismatches:
            print(
                f"mismatch: {mismatch.account_id} on {mismatch.on}: "
                f"expected {mismatch.expected}, got {mismatch.actual}"
            )
        for assertion in verification.missing:
            print(f"missing: {assertion.account_id} on {assertion.on}")
        if verification.ok:
            print(f"verified: {verification.checked} assertions")
        return 0 if verification.ok else 1

    if args.command == "validate":
        for warning in result.warnings:
            print(f"warning: {warning}")
        for error in result.errors:
            print(f"error: {error}")
        if result.ok:
            print(f"valid: {len(result.facts)} facts")
        return 0 if result.ok else 1

    summary = summarize(result)
    print("counts")
    for fact_type, count in sorted(summary.counts.items()):
        print(f"  {fact_type}: {count}")
    if summary.earliest and summary.latest:
        print(f"coverage: {summary.earliest} to {summary.latest}")
    else:
        print("coverage: none")
    for warning in result.warnings:
        print(f"warning: {warning}")
    for error in result.errors:
        print(f"error: {error}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
