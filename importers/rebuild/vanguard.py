"""Run the committed Vanguard history migration against canonical identities."""

from __future__ import annotations

import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from importers.extracts import vanguard_activity, vanguard_history

from .accounts import build_aliases, load_canonical_accounts
from .decisions import DecisionError


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_vanguard_plan(
    client,
    source_data_dir: Path,
    app_account_map: dict[str, str],
    resolutions_path: Path,
) -> dict:
    canonical_dir = source_data_dir / "normalized" / "canonical"
    canonical = load_canonical_accounts(canonical_dir / "accounts.csv")
    aliases = build_aliases(source_data_dir, canonical)
    config = json.loads(
        (source_data_dir / "extracts" / "vanguard" / "mapping.json").read_text(
            encoding="utf-8"
        )
    )
    with (canonical_dir / "positions.csv").open(
        encoding="utf-8-sig", newline=""
    ) as source:
        positions = list(csv.DictReader(source))

    specs = {}
    for number, item in config["accounts"].items():
        canonical_id = aliases.get(str(item["name"]).casefold())
        if not canonical_id or canonical_id not in app_account_map:
            raise DecisionError(f"Vanguard account {number} has no staging identity")
        shares = {
            row["symbol"]: Decimal(row["quantity"])
            for row in positions
            if row["account_id"] == canonical_id
        }
        if not shares:
            raise DecisionError(f"Vanguard account {number} has no canonical positions")
        specs[number] = vanguard_history.AccountSpec(
            number,
            app_account_map[canonical_id],
            canonical[canonical_id].name,
            shares,
            shares.get(vanguard_history.CASH_SYMBOL, Decimal("0")),
        )

    workbook_dir = source_data_dir / "extracts" / "vanguard"
    paths = [
        path
        for path in sorted(workbook_dir.glob("*.xlsx"))
        if vanguard_activity.sniff_workbook(path)
    ]
    if len(paths) != 4:
        raise DecisionError(f"expected four Vanguard history workbooks, found {len(paths)}")
    reports = vanguard_activity.parse_files(paths)
    resolutions = json.loads(resolutions_path.read_text(encoding="utf-8"))["events"]
    live = list(client.iter_activities())
    return vanguard_history.build_plan(
        reports,
        specs,
        live,
        {path.name: _hash(path) for path in paths},
        resolutions,
    )
