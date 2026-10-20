"""Append-only provenance for every label decision, kept privately in the data directory.

Each line is one decision. Later lines never edit earlier ones; an undo is a `revert`
entry naming the sequence number it cancels, so the full history stays auditable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# Stronger sources win; within a rank the newest entry wins.
RANK = {"user": 4, "wealthfolio-ui": 4, "merchant": 3, "cardmember": 2, "account": 1, "model": 0}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.entries: list[dict] = []
        if path.exists():
            self.entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self._index()

    def _index(self) -> None:
        reverted = {e["reverts"] for e in self.entries if e.get("reverts")}
        self.live = [e for e in self.entries if e["seq"] not in reverted and not e.get("reverts")]
        self.by_activity: dict[tuple[str, str], list[dict]] = {}
        self.by_merchant: dict[tuple[str, str], list[dict]] = {}
        for entry in self.live:
            if entry.get("activityId"):
                self.by_activity.setdefault((entry["field"], entry["activityId"]), []).append(entry)
            elif entry.get("merchant"):
                self.by_merchant.setdefault((entry["field"], entry["merchant"]), []).append(entry)

    def append(self, entries: list[dict]) -> list[dict]:
        if not entries:
            return []
        start = self.entries[-1]["seq"] + 1 if self.entries else 1
        stamped = [{"seq": start + i, "at": now(), **entry} for i, entry in enumerate(entries)]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.writelines(json.dumps(entry, sort_keys=True) + "\n" for entry in stamped)
        self.entries += stamped
        self._index()
        return stamped

    def candidates(self, field: str, activity_id: str, merchant: str) -> list[dict]:
        found = list(self.by_activity.get((field, activity_id), []))
        if merchant:
            found += self.by_merchant.get((field, merchant), [])
        return found

    def history(self, activity_id: str, merchant: str = "") -> list[dict]:
        cancelled = {e["reverts"] for e in self.entries if e.get("reverts")}
        mine = {e["seq"] for e in self.entries if e.get("activityId") == activity_id or (merchant and e.get("merchant") == merchant)}
        return [
            {**e, "reverted": e["seq"] in cancelled} for e in self.entries
            if e["seq"] in mine or e.get("reverts") in mine
        ]

    def last_action(self, by: str) -> list[dict]:
        """Live entries of the newest action made by `by` (one action may write several)."""
        mine = [e for e in self.live if e.get("by") == by and e.get("action")]
        if not mine:
            return []
        action = mine[-1]["action"]
        return [e for e in mine if e["action"] == action]


def strongest(entries: list[dict]) -> dict | None:
    return max(entries, key=lambda e: (RANK[e["source"]], e.get("seq", 0)), default=None)
