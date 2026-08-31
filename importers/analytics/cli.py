"""Build, verify, or diagnose private canonical analytics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .diagnostics import diagnose
from .generator import AnalyticsError, build, plan, verify


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "build", "verify", "diagnose-wealthfolio"))
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(r"D:\documents\finance-data"),
        help="private finance data root",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--upstream-version", default="")
    parser.add_argument("--upstream-revision", default="")
    parser.add_argument("--image-digest", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "diagnose-wealthfolio":
            result = diagnose(
                args.data_dir,
                base_url=args.base_url,
                upstream_version=args.upstream_version,
                upstream_revision=args.upstream_revision,
                image_digest=args.image_digest,
            )
        else:
            result = {"plan": plan, "build": build, "verify": verify}[args.command](args.data_dir)
    except (AnalyticsError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
