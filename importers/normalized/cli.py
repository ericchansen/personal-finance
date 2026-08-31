"""Plan, build, or verify the private canonical financial system of record."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .builder import BuildError, build, plan, verify


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "build", "verify"))
    parser.add_argument(
        "--data-dir", type=Path, default=Path(r"D:\documents\finance-data"),
        help="private finance data root",
    )
    args = parser.parse_args(argv)
    try:
        result = {
            "plan": plan,
            "build": build,
            "verify": verify,
        }[args.command](args.data_dir)
    except BuildError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
