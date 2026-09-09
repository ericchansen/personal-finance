"""Recover bank-truncated descriptions from explicitly bound paired exports."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from . import parsers


LEGACY_RULE = "citi-ofx-name-27-v1"
RULE = "citi-ofx-name-27-v2"
INSTRUMENT_SUFFIX = re.compile(
    r"^(?P<description>.+?)\s+null\s+(?P<mask>X{12}\d{4})\s*$", re.IGNORECASE
)


@dataclass(frozen=True)
class CorroboratedExtract:
    extract: parsers.Extract
    supporting_files: tuple[Path, ...] = ()
    description_evidence: tuple[dict[str, Any], ...] = ()
    original_extract: parsers.Extract | None = None


def _read_bound(path: Path, expected: Any) -> bytes:
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ValueError("paired export requires an exact source hash")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("paired export source hash changed")
    return raw


def parse_mapped_extract(
    path: Path,
    entry: Mapping[str, Any],
    *,
    data_root: Path,
    account_id: str,
) -> CorroboratedExtract:
    declaration = entry.get("descriptionEvidence")
    if declaration is None:
        return CorroboratedExtract(parsers.parse_file(path, account_id=account_id))
    if (
        not isinstance(declaration, dict)
        or set(declaration) != {"rule", "path", "primarySha256", "supportingSha256"}
        or declaration.get("rule") not in {LEGACY_RULE, RULE}
    ):
        raise ValueError("paired export description declaration is invalid")
    extracts_root = (data_root / "extracts").resolve()
    primary = path.resolve()
    supporting = (extracts_root / str(declaration["path"])).resolve()
    if (
        not primary.is_relative_to(extracts_root)
        or not supporting.is_relative_to(extracts_root)
        or primary == supporting
        or primary.suffix.casefold() not in {".ofx", ".qfx"}
        or supporting.suffix.casefold() != ".csv"
    ):
        raise ValueError("paired export paths are invalid")
    raw_primary = _read_bound(primary, declaration["primarySha256"])
    raw_supporting = _read_bound(supporting, declaration["supportingSha256"])
    primary_text = raw_primary.decode("utf-8-sig")
    supporting_text = raw_supporting.decode("utf-8-sig")
    supporting_rows = parsers._rows(supporting_text)
    header = set(supporting_rows[0]) if supporting_rows else set()
    if header != {"Status", "Date", "Description", "Debit", "Credit"}:
        raise ValueError("paired export is not the declared bank CSV format")
    source_accounts = {
        value.strip()
        for value in re.findall(r"<ACCTID>([^<\r\n]+)", primary_text, re.IGNORECASE)
    }
    if len(source_accounts) != 1 or "" in source_accounts:
        raise ValueError("paired primary export is not uniquely account scoped")
    currencies = {
        value.strip().upper()
        for value in re.findall(r"<CURDEF>([^<\r\n]+)", primary_text, re.IGNORECASE)
    }
    if currencies != {"USD"}:
        raise ValueError("paired export rule requires an explicit USD primary currency")
    if any(
        bool(row.get("Debit")) == bool(row.get("Credit"))
        for row in supporting_rows
    ):
        raise ValueError("paired bank CSV has ambiguous debit/credit columns")
    if any(
        row.get("Status", "").casefold() not in {"posted", "cleared"}
        for row in supporting_rows
    ):
        raise ValueError("paired bank CSV contains unposted rows")
    original = parsers.parse_ofx(primary_text, source=str(primary))
    supplement = parsers.parse_citi_csv(
        supporting_text, source=str(supporting), account_id=account_id
    )
    primary_count = len(re.findall(r"<STMTTRN>", primary_text, re.IGNORECASE))
    if (
        not original.transactions
        or len(original.transactions) != primary_count
        or len(supplement.transactions) != len(supporting_rows)
        or any(
            not row.amount.is_finite()
            for row in (*original.transactions, *supplement.transactions)
        )
        or any(row.id_is_synthetic for row in original.transactions)
        or len({row.source_id for row in original.transactions}) != primary_count
    ):
        raise ValueError("paired exports lack a complete stable primary occurrence inventory")

    def key(row: parsers.ExtractTransaction):
        return row.date, row.amount

    primary_counts = Counter(key(row) for row in original.transactions)
    supporting_counts = Counter(key(row) for row in supplement.transactions)
    if primary_counts != supporting_counts:
        raise ValueError("paired exports have different economic occurrence inventories")
    by_economics = defaultdict(list)
    for row in supplement.transactions:
        by_economics[key(row)].append(row)
    transactions = []
    evidence = []
    for row in original.transactions:
        candidates = by_economics[key(row)]
        if primary_counts[key(row)] != 1 or len(candidates) != 1:
            transactions.append(row)
            continue
        bank_description = candidates[0].description
        if row.description != bank_description[:27].rstrip():
            transactions.append(row)
            continue
        expanded = bank_description
        instrument = (
            INSTRUMENT_SUFFIX.fullmatch(bank_description)
            if declaration["rule"] == RULE else None
        )
        if instrument is not None:
            expanded = instrument["description"]
        if row.description == expanded and instrument is None:
            transactions.append(row)
            continue
        transactions.append(replace(row, description=expanded))
        evidence.append({
            "rule": declaration["rule"],
            "sourceId": row.source_id,
            "date": row.date.isoformat(),
            "amount": format(row.amount, "f"),
            "currency": "USD",
            "originalDescription": row.description,
            "bankDescription": bank_description,
            "description": expanded,
            "paymentInstrumentMask": instrument["mask"] if instrument is not None else None,
            "primarySha256": declaration["primarySha256"],
            "primaryPath": str(primary.relative_to(data_root.resolve())),
            "supportingSha256": declaration["supportingSha256"],
            "supportingPath": str(supporting.relative_to(data_root.resolve())),
        })
    return CorroboratedExtract(
        replace(original, transactions=transactions),
        (supporting,),
        tuple(evidence),
        original,
    )


def parse_mapped_extracts(
    specifications: list[tuple[Path, Mapping[str, Any], str]],
    *,
    data_root: Path,
) -> tuple[CorroboratedExtract, ...]:
    parsed = [
        parse_mapped_extract(path, entry, data_root=data_root, account_id=account_id)
        for path, entry, account_id in specifications
    ]
    primary_paths = {path.resolve() for path, _, _ in specifications}
    if any(
        path in primary_paths
        for result in parsed for path in result.supporting_files
    ):
        raise ValueError("supporting export must not also be imported as primary transactions")
    expansions: dict[
        tuple[str, str | None, str, parsers.ExtractTransaction], dict[str, Any]
    ] = {}
    for (_, _, account_id), result in sorted(
        zip(specifications, parsed, strict=True), key=lambda item: str(item[0][0])
    ):
        original = result.original_extract or result.extract
        by_id = {row.source_id: row for row in original.transactions}
        for proof in result.description_evidence:
            key = (
                account_id, original.account_id, original.format, by_id[proof["sourceId"]]
            )
            prior = expansions.get(key)
            if prior is not None and prior["bankDescription"] != proof["bankDescription"]:
                raise ValueError("paired exports contradict a stable source description")
            if prior is None or prior["rule"] == LEGACY_RULE and proof["rule"] == RULE:
                expansions[key] = proof
    resolved = []
    for (_, _, account_id), result in zip(specifications, parsed, strict=True):
        original = result.original_extract or result.extract
        transactions = []
        proofs = []
        supporting = set(result.supporting_files)
        for row in original.transactions:
            proof = expansions.get((account_id, original.account_id, original.format, row))
            if proof is None or row.id_is_synthetic:
                transactions.append(row)
                continue
            # The entire raw source occurrence must match, not merely its FITID.
            # All identical replays receive the same verified enrichment before
            # canonical deduplication, so it cannot manufacture a collision ID.
            transactions.append(replace(row, description=proof["description"]))
            proofs.append(proof)
            supporting.add((data_root / proof["supportingPath"]).resolve())
        resolved.append(CorroboratedExtract(
            replace(original, transactions=transactions),
            tuple(sorted(supporting)),
            tuple(proofs),
            original,
        ))
    return tuple(resolved)
