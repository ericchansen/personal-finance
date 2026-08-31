"""Summarize or export Ledger Live's local app.json cache.

Examples:
    python -m importers.crypto.cli summary
    python -m importers.crypto.cli assert-current
    python -m importers.crypto.cli export
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from .ledger_live import (
    DEFAULT_APP_JSON,
    LedgerLiveError,
    assert_current_balances,
    export_payload,
    load_ledger_live,
    satoshis_to_btc,
)

DEFAULT_DATA_DIR = Path("D:/documents/finance-data")
DEFAULT_OUTPUT = DEFAULT_DATA_DIR / "raw" / "ledger-live-normalized.json"


def ensure_private_output(path: Path, private_root: Path = DEFAULT_DATA_DIR) -> Path:
    resolved = path.expanduser().resolve()
    root = private_root.expanduser().resolve()
    allowed = (root / "facts", root / "raw")
    if not any(resolved == base or base in resolved.parents for base in allowed):
        raise LedgerLiveError(
            "output must be under the private finance-data facts or raw directory"
        )
    return resolved


def _atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as destination:
            json.dump(payload, destination, indent=2)
            destination.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-json", type=Path, default=DEFAULT_APP_JSON)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("summary", help="show a safe local-cache summary")
    commands.add_parser(
        "assert-current",
        help="offline assertion that cached operations reconcile to current balances",
    )
    export = commands.add_parser("export", help="write portable normalized JSON")
    export.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    try:
        accounts = load_ledger_live(args.app_json)
        if args.command == "assert-current":
            assert_current_balances(accounts)
            print(f"ok: {len(accounts)} account balance(s) reconcile offline")
            return 0
        if args.command == "summary":
            print(f"accounts: {len(accounts)}")
            print(f"cached operations: {sum(len(a.operations) for a in accounts)}")
            print(f"balance: {sum(a.current_balance_sat for a in accounts)} sat")
            print(
                "balance BTC: "
                f"{satoshis_to_btc(sum(a.current_balance_sat for a in accounts))}"
            )
            print("source: local Ledger Live cache (no network requests)")
            return 0

        assert_current_balances(accounts)
        output = ensure_private_output(args.output)
        _atomic_json_write(output, export_payload(accounts))
        print(f"exported {len(accounts)} account(s) to {output}")
        return 0
    except LedgerLiveError as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
