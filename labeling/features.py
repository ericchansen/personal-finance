"""Turn a Wealthfolio spending row into discrete classifier features."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path

TOKEN = re.compile(r"[A-Z][A-Z0-9'&]*")
# Processor and bank wording that precedes the real merchant name.
NOISE = {
    "SQ", "TST", "SP", "PAYPAL", "PP", "POS", "DEBIT", "PURCHASE", "CHECKCARD", "CARD",
    "WEB", "INITIATED", "PAYMENT", "AT", "ACH", "RECURRING", "WITHDRAWAL", "DEPOSIT", "ELECTRONIC", "PREAUTHORIZED",
}
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
KINDS = {"m": "merchant", "w": "word", "mcc": "MCC", "type": "type"}


def tokens(text: str | None) -> list[str]:
    return TOKEN.findall((text or "").upper())


def merchant_key(text: str | None) -> str:
    """The first two words after processor prefixes; empty when there is no description."""
    words = tokens(text)
    start = 0
    while start < len(words) and words[start] in NOISE:
        start += 1
    return " ".join((words[start:] or words)[:2])


def merchant_pattern(key: str) -> str:
    """A regex for a native categorization rule matching descriptions with this key.

    Uses only syntax shared by Python `re` and Rust `regex` (no lookaround). A one-word or
    all-prefix key is the whole merchant name, so only prefix words may follow it.
    """
    noise = "|".join(sorted(NOISE))
    words = key.split()
    body = "[^A-Z]+".join(re.escape(word) for word in words)
    if len(words) > 1 and not set(words) <= NOISE:
        tail = "(?:[^A-Z0-9'&]|$)"
    else:
        tail = f"(?:[^A-Z]+(?:{noise}))*[^A-Z]*$"
    return rf"(?i)^[^A-Z]*(?:(?:{noise})[^A-Z]+)*{body}{tail}"


def raw_index(data_dir: Path) -> dict[str, dict]:
    """SimpleFIN extras (payee, MCC, time of day) keyed like activity idempotency keys."""
    index = {}
    for path in sorted((data_dir / "raw" / "simplefin").glob("*/*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for account in payload.get("accounts") or []:
            for txn in account.get("transactions") or []:
                index[f"simplefin:{account.get('id')}:{txn.get('id')}"] = {
                    key: txn.get(key) for key in ("payee", "mcc", "transacted_at")
                }
    return index


def features(row: dict, raw: dict | None = None, zone=timezone.utc) -> list[str]:
    raw = raw or {}
    notes = row.get("notes")
    found = [f"type:{row['activityType'].lower()}"]
    key = merchant_key(notes)
    if key:
        found.append(f"m:{key}")
        found += [f"w:{word}" for word in dict.fromkeys(tokens(notes)) if word not in NOISE]
    amount = abs(float(row.get("amount") or 0))
    found.append(f"amt:{int(math.log2(amount + 1))}")
    day = date.fromisoformat(row["activityDate"][:10])
    found.append(f"dow:{DAYS[day.weekday()]}")
    found += [f"cat:{category}" for category in row.get("categoryIds") or []]
    if raw.get("mcc"):
        found.append(f"mcc:{raw['mcc']}")
    if raw.get("transacted_at"):
        hour = datetime.fromtimestamp(int(raw["transacted_at"]), zone).hour
        found.append(f"hr:{hour // 3 * 3:02d}")
    return found


def describe(feature: str, category_names: dict[str, str]) -> str:
    kind, _, value = feature.partition(":")
    if kind == "amt":
        return f"amount ${2 ** int(value) - 1:,}-{2 ** (int(value) + 1) - 1:,}"
    if kind == "cat":
        return f"category {category_names.get(value, value)}"
    if kind == "hr":
        return f"{value}:00-{int(value) + 3:02d}:00"
    if kind == "dow":
        return value
    return f"{KINDS[kind]} {value}"
