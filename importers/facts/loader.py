"""Load and cross-check hand-maintained fact files."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

try:
    from .schema import (
        AccountFact,
        AssertionFact,
        CryptoFact,
        LoadResult,
        LoanFact,
        ParsedFact,
        PropertyFact,
        ValidationIssue,
        VehicleFact,
        extract_fact_objects,
        parse_fact,
    )
except ImportError:  # pragma: no cover - lets ``python cli.py`` work.
    from schema import (  # type: ignore
        AccountFact,
        AssertionFact,
        CryptoFact,
        LoadResult,
        LoanFact,
        ParsedFact,
        PropertyFact,
        ValidationIssue,
        VehicleFact,
        extract_fact_objects,
        parse_fact,
    )


@dataclass(frozen=True)
class FactsSummary:
    counts: Counter[str]
    earliest: date | None
    latest: date | None
    warnings: tuple[ValidationIssue, ...]


def load_facts(directory: str | Path) -> LoadResult:
    root = Path(directory)
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    facts: list[ParsedFact] = []

    if not root.exists():
        return LoadResult(
            errors=(ValidationIssue(str(root), "facts directory does not exist"),)
        )

    for path in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(ValidationIssue(str(path), f"invalid JSON: {exc.msg}"))
            continue

        objects = extract_fact_objects(payload)
        if not objects:
            errors.append(ValidationIssue(str(path), "file contains no fact objects"))
            continue
        for raw in objects:
            parsed, parse_errors = parse_fact(raw, path)
            errors.extend(parse_errors)
            if parsed:
                facts.append(parsed)

    seen: dict[str, ParsedFact] = {}
    for fact in facts:
        if fact.fact_id in seen:
            previous = seen[fact.fact_id]
            if isinstance(fact.fact, AssertionFact) and isinstance(previous.fact, AssertionFact):
                if fact.fact.balance != previous.fact.balance:
                    errors.append(
                        ValidationIssue(
                            str(fact.path),
                            f"conflicting assertion also seen in {previous.path}",
                            fact.fact_type,
                            "balance",
                        )
                    )
                else:
                    warnings.append(
                        ValidationIssue(
                            str(fact.path),
                            f"repeated assertion also seen in {previous.path}",
                            fact.fact_type,
                            "balance",
                        )
                    )
                continue
            errors.append(
                ValidationIssue(
                    str(fact.path),
                    f"duplicate id also seen in {previous.path}",
                    fact.fact_type,
                    fact.fact_id,
                )
            )
        else:
            seen[fact.fact_id] = fact

    assets = {
        f.fact.name
        for f in facts
        if isinstance(f.fact, (PropertyFact, VehicleFact)) and f.fact.name
    }
    loans = [f for f in facts if isinstance(f.fact, LoanFact)]
    for loan in loans:
        if loan.fact.linked_to and loan.fact.linked_to not in assets:
            errors.append(
                ValidationIssue(
                    str(loan.path),
                    "loan linkedTo names no known asset",
                    "loan",
                    "linkedTo",
                )
            )
        if loan.fact.annual_rate is None:
            warnings.append(
                ValidationIssue(str(loan.path), "annual rate is unknown", "loan", "annualRate")
            )
        if loan.fact.term_months is None:
            warnings.append(
                ValidationIssue(str(loan.path), "term is unknown", "loan", "termMonths")
            )

    assertion_targets = {
        f.fact.id for f in facts if isinstance(f.fact, AccountFact) and f.fact.id
    }
    assertion_targets.update(
        fact.fact_id
        for fact in facts
        if isinstance(fact.fact, (LoanFact, PropertyFact, VehicleFact))
    )
    for fact in facts:
        if (
            isinstance(fact.fact, AssertionFact)
            and fact.fact.account_id not in assertion_targets
        ):
            errors.append(
                ValidationIssue(
                    str(fact.path),
                    "assertion names no known account or valued entity",
                    "assertion",
                    "accountId",
                )
            )

    accounts = {
        fact.fact.id: fact.fact
        for fact in facts
        if isinstance(fact.fact, AccountFact) and fact.fact.id
    }
    for parsed in facts:
        fact = parsed.fact
        if not isinstance(fact, CryptoFact) or not fact.account_id:
            continue
        account = accounts.get(fact.account_id)
        if account is None:
            errors.append(
                ValidationIssue(
                    str(parsed.path),
                    "crypto snapshot names no known account",
                    "crypto",
                    "accountId",
                )
            )
        elif account.kind.strip().casefold() not in {"crypto", "cryptocurrency"}:
            errors.append(
                ValidationIssue(
                    str(parsed.path),
                    "crypto snapshot account must have crypto or cryptocurrency kind",
                    "crypto",
                    "accountId",
                )
            )

    return LoadResult(tuple(facts), tuple(errors), tuple(warnings))


def summarize(result: LoadResult) -> FactsSummary:
    dates: list[date] = []
    counts = Counter(f.fact_type for f in result.facts)
    for parsed in result.facts:
        fact = parsed.fact
        for value in vars(fact).values():
            if isinstance(value, date):
                dates.append(value)
            elif isinstance(value, tuple):
                for item in value:
                    for nested in vars(item).values() if hasattr(item, "__dataclass_fields__") else []:
                        if isinstance(nested, date):
                            dates.append(nested)
    return FactsSummary(
        counts,
        min(dates) if dates else None,
        max(dates) if dates else None,
        result.warnings,
    )


def facts_to_jsonable(facts: list[dict]) -> str:
    return json.dumps(facts, indent=2, sort_keys=True)


def money(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")
