"""Schema for hand-established financial facts.

The bank downloads are replayable. These records are for the judgments and
documented truths that are not: purchase dates, duplicate rulings, exclusions
and other facts that would otherwise live only in one mutable application DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


FACT_TYPES = {
    "property",
    "loan",
    "vehicle",
    "crypto",
    "account",
    "decision",
    "assertion",
    "quote",
}


@dataclass(frozen=True)
class FactSource:
    source: str
    source_path: str | None = None
    notes: str = ""


@dataclass(frozen=True)
class Appraisal:
    on: date
    value: Decimal
    kind: str


@dataclass(frozen=True)
class PropertyFact(FactSource):
    name: str = ""
    address: str = ""
    purchase_date: date | None = None
    purchase_price: Decimal | None = None
    sale_date: date | None = None
    sale_price: Decimal | None = None
    net_proceeds: Decimal | None = None
    appraisals: tuple[Appraisal, ...] = ()


@dataclass(frozen=True)
class LoanFact(FactSource):
    name: str = ""
    principal: Decimal | None = None
    annual_rate: Decimal | None = None
    term_months: int | None = None
    origination_date: date | None = None
    first_payment: date | None = None
    lender: str = ""
    linked_to: str = ""
    payoff_amount: Decimal | None = None
    payoff_date: date | None = None
    pmi: bool | Decimal | str | None = None


@dataclass(frozen=True)
class VehicleFact(FactSource):
    name: str = ""
    purchase_date: date | None = None
    purchase_price: Decimal | None = None
    current_value: Decimal | None = None
    current_value_date: date | None = None


@dataclass(frozen=True)
class CryptoFact(FactSource):
    label: str = ""
    chain: str = ""
    unit: str = ""
    quantity: Decimal | None = None
    account_id: str | None = None
    as_of: date | None = None
    public_address: str | None = None
    xpub: str | None = None
    derivation_path: str | None = None


@dataclass(frozen=True)
class AccountFact(FactSource):
    id: str = ""
    institution: str = ""
    display_name: str = ""
    masked_number: str | None = None
    kind: str = ""
    opened: date | None = None
    closed: date | None = None
    excluded: bool = False
    reason: str | None = None
    tracking_mode: str = "TRANSACTIONS"


@dataclass(frozen=True)
class DecisionFact(FactSource):
    id: str = ""
    kind: str = ""
    resolution: str = ""
    evidence: str = ""
    decided_on: date | None = None
    affects: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssertionFact(FactSource):
    account_id: str = ""
    on: date | None = None
    balance: Decimal | None = None


@dataclass(frozen=True)
class QuoteFact(FactSource):
    symbol: str = ""
    instrument_type: str = ""
    on: date | None = None
    close: Decimal | None = None
    currency: str = ""


Fact = (
    PropertyFact
    | LoanFact
    | VehicleFact
    | CryptoFact
    | AccountFact
    | DecisionFact
    | AssertionFact
    | QuoteFact
)


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    message: str
    fact_type: str | None = None
    field: str | None = None

    def __str__(self) -> str:
        bits = [self.path]
        if self.fact_type:
            bits.append(self.fact_type)
        if self.field:
            bits.append(self.field)
        return ": ".join(bits + [self.message])


@dataclass(frozen=True)
class ParsedFact:
    fact_type: str
    fact_id: str
    data: dict[str, Any]
    path: Path
    fact: Fact


@dataclass(frozen=True)
class LoadResult:
    facts: tuple[ParsedFact, ...] = ()
    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


class FactValidation:
    def __init__(self, path: Path, fact_type: str | None):
        self.path = path
        self.fact_type = fact_type
        self.errors: list[ValidationIssue] = []

    def issue(self, message: str, field_name: str | None = None) -> None:
        self.errors.append(
            ValidationIssue(str(self.path), message, self.fact_type, field_name)
        )

    def required(self, data: dict[str, Any], field_name: str) -> Any:
        if field_name not in data:
            self.issue("missing required field", field_name)
            return None
        return data[field_name]

    def date(self, data: dict[str, Any], field_name: str) -> date | None:
        value = self.required(data, field_name)
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            self.issue("date must be an ISO YYYY-MM-DD string", field_name)
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            self.issue("date must be an ISO YYYY-MM-DD string", field_name)
            return None

    def optional_date(self, data: dict[str, Any], field_name: str) -> date | None:
        value = data.get(field_name)
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            self.issue("date must be an ISO YYYY-MM-DD string", field_name)
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            self.issue("date must be an ISO YYYY-MM-DD string", field_name)
            return None

    def decimal(self, data: dict[str, Any], field_name: str) -> Decimal | None:
        value = self.required(data, field_name)
        return self._decimal_value(value, field_name)

    def optional_decimal(self, data: dict[str, Any], field_name: str) -> Decimal | None:
        return self._decimal_value(data.get(field_name), field_name)

    def _decimal_value(self, value: Any, field_name: str) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            self.issue("amount must be decimal-compatible", field_name)
            return None
        if not amount.is_finite():
            self.issue("amount must be a finite decimal", field_name)
            return None
        return amount


def extract_fact_objects(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("facts"), list):
        return [p for p in payload["facts"] if isinstance(p, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def parse_fact(data: dict[str, Any], path: Path) -> tuple[ParsedFact | None, list[ValidationIssue]]:
    fact_type = data.get("type")
    check = FactValidation(path, str(fact_type) if fact_type else None)
    if fact_type not in FACT_TYPES:
        check.issue("unknown fact type" if fact_type else "missing fact type", "type")
        return None, check.errors

    check.required(data, "source")
    check.required(data, "notes")

    common = {
        "source": str(data.get("source") or ""),
        "source_path": data.get("sourcePath"),
        "notes": str(data.get("notes") or ""),
    }
    fact: Fact
    fact_id: str

    if fact_type == "property":
        appraisals = []
        for index, raw in enumerate(data.get("appraisals") or []):
            if not isinstance(raw, dict):
                check.issue("appraisal must be an object", f"appraisals[{index}]")
                continue
            sub = FactValidation(path, fact_type)
            on = sub.date(raw, "date")
            value = sub.decimal(raw, "value")
            kind = sub.required(raw, "type")
            check.errors.extend(sub.errors)
            if on and value is not None and kind:
                appraisals.append(Appraisal(on, value, str(kind)))
        fact = PropertyFact(
            **common,
            name=str(check.required(data, "name") or ""),
            address=str(check.required(data, "address") or ""),
            purchase_date=check.date(data, "purchaseDate"),
            purchase_price=check.decimal(data, "purchasePrice"),
            sale_date=check.optional_date(data, "saleDate"),
            sale_price=check.optional_decimal(data, "salePrice"),
            net_proceeds=check.optional_decimal(data, "netProceeds"),
            appraisals=tuple(appraisals),
        )
        fact_id = f"property:{fact.name}"
    elif fact_type == "loan":
        fact = LoanFact(
            **common,
            name=str(check.required(data, "name") or ""),
            principal=check.decimal(data, "principal"),
            annual_rate=check.decimal(data, "annualRate"),
            term_months=_int_or_none(check, data, "termMonths"),
            origination_date=check.date(data, "originationDate"),
            first_payment=check.date(data, "firstPayment"),
            lender=str(check.required(data, "lender") or ""),
            linked_to=str(check.required(data, "linkedTo") or ""),
            payoff_amount=check.optional_decimal(data, "payoffAmount"),
            payoff_date=check.optional_date(data, "payoffDate"),
            pmi=_pmi(check, data),
        )
        fact_id = f"loan:{fact.name}"
    elif fact_type == "vehicle":
        fact = VehicleFact(
            **common,
            name=str(check.required(data, "name") or ""),
            purchase_date=check.date(data, "purchaseDate"),
            purchase_price=check.decimal(data, "purchasePrice"),
            current_value=check.decimal(data, "currentValue"),
            current_value_date=check.date(data, "currentValueDate"),
        )
        fact_id = f"vehicle:{fact.name}"
    elif fact_type == "crypto":
        public_address = data.get("publicAddress")
        xpub = data.get("xpub")
        account_id_value = data.get("accountId")
        as_of_value = data.get("asOf")
        has_account_id = account_id_value not in (None, "")
        has_as_of = as_of_value not in (None, "")
        if has_account_id != has_as_of:
            missing = "asOf" if has_account_id else "accountId"
            check.issue("accountId and asOf must be provided together", missing)
        if not public_address and not xpub and not (has_account_id and has_as_of):
            check.issue(
                "publicAddress or xpub, or complete accountId/asOf snapshot, is required",
                "publicAddress",
            )
        quantity = check.decimal(data, "quantity")
        as_of = check.optional_date(data, "asOf")
        if has_account_id and has_as_of and quantity is None and "quantity" in data:
            check.issue("quantity is required for an account snapshot", "quantity")
        fact = CryptoFact(
            **common,
            label=str(check.required(data, "label") or ""),
            chain=str(check.required(data, "chain") or ""),
            unit=str(check.required(data, "unit") or ""),
            quantity=quantity,
            account_id=str(account_id_value) if has_account_id else None,
            as_of=as_of,
            public_address=public_address,
            xpub=xpub,
            derivation_path=data.get("derivationPath"),
        )
        fact_id = (
            f"crypto:{fact.account_id}:{fact.as_of}:{fact.unit}"
            if fact.account_id and fact.as_of
            else f"crypto:{fact.label}"
        )
    elif fact_type == "account":
        tracking_mode = str(data.get("trackingMode") or "TRANSACTIONS").upper()
        if tracking_mode not in {"TRANSACTIONS", "HOLDINGS"}:
            check.issue(
                "tracking mode must be TRANSACTIONS or HOLDINGS", "trackingMode"
            )
        fact = AccountFact(
            **common,
            id=str(check.required(data, "id") or ""),
            institution=str(check.required(data, "institution") or ""),
            display_name=str(check.required(data, "displayName") or ""),
            masked_number=data.get("maskedNumber"),
            kind=str(check.required(data, "kind") or ""),
            opened=check.optional_date(data, "opened"),
            closed=check.optional_date(data, "closed"),
            excluded=bool(data.get("excluded", False)),
            reason=data.get("reason"),
            tracking_mode=tracking_mode,
        )
        fact_id = f"account:{fact.id}"
    elif fact_type == "decision":
        affects = data.get("affects") or []
        if not isinstance(affects, list):
            check.issue("affects must be a list", "affects")
            affects = []
        fact = DecisionFact(
            **common,
            id=str(check.required(data, "id") or ""),
            kind=str(check.required(data, "decisionType") or ""),
            resolution=str(check.required(data, "resolution") or ""),
            evidence=str(check.required(data, "evidence") or ""),
            decided_on=check.date(data, "decidedOn"),
            affects=tuple(str(a) for a in affects),
        )
        fact_id = f"decision:{fact.id}"
    elif fact_type == "assertion":
        if not isinstance(data.get("source"), str) or not data.get("source", "").strip():
            check.issue("source must describe assertion provenance", "source")
        fact = AssertionFact(
            **common,
            account_id=str(check.required(data, "accountId") or ""),
            on=check.date(data, "date"),
            balance=check.decimal(data, "balance"),
        )
        fact_id = f"assertion:{fact.account_id}:{fact.on}"
    else:
        fact = QuoteFact(
            **common,
            symbol=str(check.required(data, "symbol") or ""),
            instrument_type=str(check.required(data, "instrumentType") or ""),
            on=check.date(data, "date"),
            close=check.decimal(data, "close"),
            currency=str(check.required(data, "currency") or ""),
        )
        fact_id = f"quote:{fact.instrument_type}:{fact.symbol}:{fact.on}"

    if check.errors:
        return None, check.errors
    return ParsedFact(str(fact_type), fact_id, data, path, fact), []


def _int_or_none(check: FactValidation, data: dict[str, Any], field_name: str) -> int | None:
    value = check.required(data, field_name)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        check.issue("value must be an integer", field_name)
        return None


def _pmi(check: FactValidation, data: dict[str, Any]) -> bool | Decimal | str | None:
    if "pmi" not in data:
        check.issue("missing required field", "pmi")
        return None
    value = data.get("pmi")
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return Decimal(stripped)
        except InvalidOperation:
            return stripped
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        check.issue("pmi must be a boolean, decimal amount, string, or null", "pmi")
        return None
