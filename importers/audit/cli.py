"""Create or verify a private, read-only evidence baseline."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
from typing import Sequence

from importers.monarch.wealthfolio_client import WealthfolioClient, WealthfolioError
from importers.rebuild.decisions import DecisionError

from .baseline import BaselineError, build, verify

REPO_ROOT = Path(__file__).resolve().parents[2]


def _password(data_dir: Path, password_file: Path | None) -> str:
    password = os.environ.get("WEALTHFOLIO_PASSWORD")
    if password:
        return password
    path = (password_file or data_dir / "wealthfolio" / "ADMIN-PASSWORD.txt").resolve()
    try:
        path.relative_to(data_dir.resolve())
    except ValueError:
        raise BaselineError("--password-file must be under --data-dir") from None
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return getpass.getpass("Wealthfolio password: ")


def _summary(result: dict) -> str:
    return (
        f"path={result['outputPath']} "
        f"sources={result['sourceFileCount']} "
        f"domains={result['domainCount']} "
        f"records={sum(result['recordCounts'].values())}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "verify"))
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--password-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = verify(args.data_dir, repo_root=REPO_ROOT)
        else:
            client = WealthfolioClient(args.base_url)
            client.login(_password(args.data_dir, args.password_file))
            result = build(
                args.data_dir,
                client,
                base_url=args.base_url,
                repo_root=REPO_ROOT,
            )
    except WealthfolioError:
        print("ERROR: Wealthfolio authentication or read request failed")
        return 1
    except (BaselineError, DecisionError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
