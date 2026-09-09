"""Manage private lineage review queues and verified decisions."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Sequence

from importers.rebuild.decisions import DecisionError

from .model import ReviewError
from .workflow import build, import_decisions, status, verify

REPO_ROOT = Path(__file__).resolve().parents[2]


def _summary(result: dict[str, Any]) -> str:
    counts = result["counts"]
    readiness = result["readinessCounts"]
    priorities = ",".join(
        f"{key}:{value}" for key, value in sorted(result["priorityCounts"].items())
    )
    cardinalities = ",".join(
        f"{key}:{value}"
        for key, value in sorted(result["cardinalityCounts"].items())
    )
    return (
        f"status={result['status']} "
        f"queue={result['queuePublicationId']} "
        f"forensic={result['forensicPublicationId']} "
        f"graph={result['candidateGraphHash']} "
        f"groups={counts['queue-groups']} "
        f"resolved={counts['resolved-groups']} "
        f"unresolved={counts['unresolved-groups']} "
        f"ready={readiness['ready-groups']} "
        f"restore={readiness['restore-eligible-groups']} "
        f"surgical={readiness['surgical-eligible-groups']} "
        f"rebuild={readiness['rebuild-eligible-groups']} "
        f"priorities={priorities} "
        f"cardinalities={cardinalities}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "import", "verify", "status"))
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build(
                args.data_dir,
                repo_root=REPO_ROOT,
                batch_size=args.batch_size,
            )
        elif args.command == "import":
            if args.input is None:
                raise ReviewError("decision-input-required")
            result = import_decisions(
                args.data_dir,
                args.input,
                repo_root=REPO_ROOT,
            )
        elif args.command == "verify":
            result = verify(args.data_dir, repo_root=REPO_ROOT)
        else:
            result = status(args.data_dir, repo_root=REPO_ROOT)
    except OSError:
        print("ERROR: private-io-failure")
        return 1
    except (ReviewError, DecisionError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
