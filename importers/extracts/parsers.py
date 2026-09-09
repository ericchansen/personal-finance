"""Parse financial extracts into a common transaction shape.

Three formats, because institutions agree on nothing:

- **OFX/QFX** — SGML-ish, and the only one of the three carrying a stable
  per-transaction id (``FITID``). Prefer it wherever offered, because that id
  is what makes a re-import safe.
- **Citi CSV** — ``Status,Date,Description,Debit,Credit``, amount split across
  two columns.
- **Ally CSV** — ``Date, Time, Amount, Type, Description``, one signed amount,
  and note the leading space on every header after the first.

Neither CSV carries a transaction id, so one is synthesized — see
:func:`synthesize_id` for why that is lossy and which way it errs.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from finance_store.identity import read_ofx_statement_window


@dataclass(frozen=True)
class ExtractTransaction:
    date: date
    amount: Decimal          # signed: negative is money out
    description: str
    source_id: str           # stable when the format supplied one, else synthesized
    id_is_synthetic: bool
    kind: str | None = None  # the institution's own type label, when present


@dataclass(frozen=True)
class Extract:
    """Transactions from one file, plus whatever account context it carried."""

    source: str
    format: str
    account_id: str | None
    account_type: str | None
    balance: Decimal | None
    balance_date: date | None
    transactions: list[ExtractTransaction] = field(default_factory=list)
    # The statement period the file declares for itself, when it declares one.
    # This is the institution's claim about coverage; it is deliberately not
    # derived from the transactions, which only show what arrived.
    statement_start: date | None = None
    statement_end: date | None = None


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _parse_amount(value: str | None) -> Decimal | None:
    text = _clean(value).replace("$", "").replace(",", "")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return -amount if negative else amount


def _parse_date(value: str | None) -> date | None:
    text = _clean(value)
    if not text:
        return None
    # OFX packs a timestamp, and sometimes a timezone, into one field.
    if re.fullmatch(r"\d{8}.*", text):
        try:
            return datetime.strptime(text[:8], "%Y%m%d").date()
        except ValueError:
            return None
    # Citi is not internally consistent: card exports use MM/DD/YYYY while
    # savings exports use MM-DD-YYYY. Accept both rather than silently dropping
    # every row of one file, which is how this was found.
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def synthesize_id(account: str, when: date, amount: Decimal, description: str) -> str:
    """Build the deterministic base of an id for formats that supply none.

    The parser appends an occurrence ordinal for identical rows. That preserves
    legitimate repeated transactions while keeping their order replay-stable
    within one immutable export. Prefer OFX wherever offered because FITID avoids
    this unavoidable CSV identity limitation.
    """
    payload = f"{account}|{when.isoformat()}|{amount}|{description.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _synthetic_occurrence_id(
    occurrences: Counter[str],
    account: str,
    when: date,
    amount: Decimal,
    description: str,
) -> str:
    base = synthesize_id(account, when, amount, description)
    occurrences[base] += 1
    return f"{base}:{occurrences[base]}"


# --------------------------------------------------------------------------
# OFX
# --------------------------------------------------------------------------

def _ofx_tag(block: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>([^<\r\n]*)", block, re.IGNORECASE)
    return _clean(match.group(1)) if match else None


def parse_ofx(text: str, source: str = "") -> Extract:
    """Parse OFX/QFX.

    OFX is SGML, not XML: closing tags are optional and usually absent, so
    values are read up to the next tag or newline rather than by parsing a tree.
    """
    account_id = _ofx_tag(text, "ACCTID")
    balance = _parse_amount(_ofx_tag(text, "BALAMT"))

    transactions: list[ExtractTransaction] = []
    synthetic_occurrences: Counter[str] = Counter()
    for block in re.findall(r"<STMTTRN>(.*?)</STMTTRN>", text, re.IGNORECASE | re.DOTALL):
        when = _parse_date(_ofx_tag(block, "DTPOSTED"))
        amount = _parse_amount(_ofx_tag(block, "TRNAMT"))
        if when is None or amount is None:
            continue
        description = _ofx_tag(block, "NAME") or _ofx_tag(block, "MEMO") or ""
        fitid = _ofx_tag(block, "FITID")
        transactions.append(
            ExtractTransaction(
                date=when,
                amount=amount,
                description=description,
                source_id=fitid
                or _synthetic_occurrence_id(
                    synthetic_occurrences,
                    account_id or "",
                    when,
                    amount,
                    description,
                ),
                id_is_synthetic=not fitid,
                kind=_ofx_tag(block, "TRNTYPE"),
            )
        )

    statement_start: date | None = None
    statement_end: date | None = None
    try:
        window = read_ofx_statement_window(text, account_id=account_id)
    except ValueError:
        # An absent, incoherent, or ambiguous window is simply not a declared
        # window.  Parsing stays permissive so a usable export is still read;
        # the authority builder is where an unusable window is refused.
        window = None
    if window is not None:
        statement_start = window.statement_start
        statement_end = window.statement_end

    return Extract(
        source=source,
        format="ofx",
        account_id=account_id,
        account_type=_ofx_tag(text, "ACCTTYPE"),
        balance=balance,
        balance_date=_parse_date(_ofx_tag(text, "DTASOF")),
        transactions=transactions,
        statement_start=statement_start,
        statement_end=statement_end,
    )


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

def _rows(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    # Institutions pad headers with spaces; normalize once, here.
    return [
        {_clean(k): _clean(v) for k, v in row.items() if k is not None}
        for row in reader
    ]


def parse_citi_csv(text: str, source: str = "", account_id: str | None = None) -> Extract:
    """Citi splits the amount across Debit and Credit columns.

    Two traps here, both found by diffing against the OFX for the same account:

    1. **Signs are inconsistent, even between Citi's own products.** On a card,
       a charge is a positive Debit and a payment a *negative* Credit. On a
       savings account, interest arrives as a *positive* Credit. Negating both
       columns reconciles the cards but inverts savings.

       What holds across all of them is the meaning of the columns: Debit is
       money out, Credit is money in. So Debit is negated — which also turns a
       negative Debit, a refund, back into an inflow — and Credit is taken as an
       inflow regardless of the sign the file happens to use.

       Synthetic debit, refund, payment, and interest fixtures verify these
       sign rules against equivalent OFX rows.

    2. **A zero is a real transaction.** A waived $0.00 membership fee has
       ``Debit=0.00`` and an empty Credit. Treating zero as "absent" and
       falling through to Credit silently dropped the row.

    So the column that is *present* decides, not the column that is non-zero.
    """
    transactions: list[ExtractTransaction] = []
    synthetic_occurrences: Counter[str] = Counter()
    for row in _rows(text):
        when = _parse_date(row.get("Date"))
        if when is None:
            continue

        debit = _parse_amount(row.get("Debit"))
        credit = _parse_amount(row.get("Credit"))
        if debit is not None:
            amount = -debit          # money out; a negative debit is a refund
        elif credit is not None:
            amount = abs(credit)     # money in, whatever sign the file used
        else:
            continue

        description = row.get("Description", "")
        transactions.append(
            ExtractTransaction(
                date=when,
                amount=amount,
                description=description,
                source_id=_synthetic_occurrence_id(
                    synthetic_occurrences,
                    account_id or source,
                    when,
                    amount,
                    description,
                ),
                id_is_synthetic=True,
                kind=row.get("Status"),
            )
        )
    return Extract(
        source=source, format="citi-csv", account_id=account_id, account_type=None,
        balance=None, balance_date=None, transactions=transactions,
    )


def parse_ally_csv(text: str, source: str = "", account_id: str | None = None) -> Extract:
    """Ally uses one signed Amount column.

    Nothing inside the file identifies the account — every download is named
    ``transactions.csv`` — so ``account_id`` must come from the caller. That is
    why the download order has to be recorded at the time it is taken.
    """
    transactions: list[ExtractTransaction] = []
    synthetic_occurrences: Counter[str] = Counter()
    for row in _rows(text):
        when = _parse_date(row.get("Date"))
        amount = _parse_amount(row.get("Amount"))
        if when is None or amount is None:
            continue
        description = row.get("Description", "")
        transactions.append(
            ExtractTransaction(
                date=when,
                amount=amount,
                description=description,
                source_id=_synthetic_occurrence_id(
                    synthetic_occurrences,
                    account_id or source,
                    when,
                    amount,
                    description,
                ),
                id_is_synthetic=True,
                kind=row.get("Type"),
            )
        )
    return Extract(
        source=source, format="ally-csv", account_id=account_id, account_type=None,
        balance=None, balance_date=None, transactions=transactions,
    )


def parse_fifth_third_csv(
    text: str, source: str = "", account_id: str | None = None
) -> Extract:
    """Fifth Third uses one signed Amount column plus a check number.

    The header (``Date,Description,"Check Number",Amount``) overlaps enough
    with Ally's to be caught by the same sniff rule, which would parse but
    would quietly drop the check number and mislabel the format. It is worth
    its own branch for that reason alone.

    Rows arrive grouped by statement period rather than in date order, so
    nothing downstream may assume the file is sorted.

    There is no transaction id, so one is synthesized with an occurrence ordinal
    to preserve identical same-day repeats inside the immutable export.
    """
    transactions: list[ExtractTransaction] = []
    synthetic_occurrences: Counter[str] = Counter()
    for row in _rows(text):
        when = _parse_date(row.get("Date"))
        amount = _parse_amount(row.get("Amount"))
        if when is None or amount is None:
            continue
        description = _clean(row.get("Description", ""))
        check = _clean(row.get("Check Number", ""))
        if check:
            description = f"{description} (check {check})"
        transactions.append(
            ExtractTransaction(
                date=when,
                amount=amount,
                description=description,
                source_id=_synthetic_occurrence_id(
                    synthetic_occurrences,
                    account_id or source,
                    when,
                    amount,
                    description,
                ),
                id_is_synthetic=True,
                kind=None,
            )
        )
    return Extract(
        source=source, format="fifth-third-csv", account_id=account_id,
        account_type=None, balance=None, balance_date=None,
        transactions=transactions,
    )


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

def sniff(text: str) -> str:
    """Identify a format from content, not from the file extension.

    Citi serves OFX from a file named .OFX but also serves CSV named .CSV, and
    downloads get renamed by hand, so the extension is not trustworthy.
    """
    head = text.lstrip()[:2000].upper()
    if "OFXHEADER" in head or "<OFX>" in head or "<STMTTRN>" in head:
        return "ofx"
    stripped = text.lstrip()
    if not stripped:
        return "unknown"
    first_line = stripped.splitlines()[0].upper().replace(" ", "").replace('"', "")
    if "DEBIT" in first_line and "CREDIT" in first_line:
        return "citi-csv"
    # Checked before the Ally rule, which this header would otherwise match.
    if "CHECKNUMBER" in first_line and "AMOUNT" in first_line:
        return "fifth-third-csv"
    if "AMOUNT" in first_line and "DESCRIPTION" in first_line:
        return "ally-csv"
    return "unknown"


def parse_text(text: str, source: str = "", account_id: str | None = None) -> Extract:
    kind = sniff(text)
    if kind == "ofx":
        return parse_ofx(text, source=source)
    if kind == "citi-csv":
        return parse_citi_csv(text, source=source, account_id=account_id)
    if kind == "fifth-third-csv":
        return parse_fifth_third_csv(text, source=source, account_id=account_id)
    if kind == "ally-csv":
        return parse_ally_csv(text, source=source, account_id=account_id)
    raise ValueError(f"unrecognised extract format: {source or '<text>'}")


def parse_file(path: Path, account_id: str | None = None) -> Extract:
    return parse_text(
        path.read_text(encoding="utf-8-sig", errors="replace"),
        source=path.name,
        account_id=account_id,
    )
