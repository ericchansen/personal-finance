"""Build or verify a private, read-only duplicate and lineage audit."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from importers.rebuild.decisions import DecisionError

from .forensic import ForensicAuditError, build, verify

REPO_ROOT = Path(__file__).resolve().parents[2]


def _summary(result: dict) -> str:
    counts = result["counts"]
    return (
        f"publication={result['publication']['publicationId']} "
        f"activities={counts['accounted-activities']} "
        f"candidate-groups={counts['candidate-groups']} "
        f"review-required={counts['review-required-groups']}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify"))
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = (
            build(args.data_dir, repo_root=REPO_ROOT)
            if args.command == "build"
            else verify(args.data_dir, repo_root=REPO_ROOT)
        )
    except (ForensicAuditError, DecisionError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
