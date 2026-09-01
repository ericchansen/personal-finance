"""Resolve a live Wealthfolio activity to canonical transaction evidence.

Every importer in this repository writes a stable ``idempotencyKey`` onto the
Wealthfolio activities it creates, and the canonical estate writes a matching
``source_id`` onto the transaction rows it derives from the same extract. The
two strings are *related* but not identical, because each side scopes the key
by whatever account identifier it owns:

===========  ======================================  ==================================
source       live ``idempotencyKey``                 canonical ``source_id``
===========  ======================================  ==================================
simplefin    ``simplefin:<wealthfolioAccount>:<id>``  ``simplefin:<simplefinAccount>:<id>``
extract      ``extract:<wealthfolioAccount>:<id>``    ``extract:<stable|synthetic>:<id>``
monarch      ``monarch:<rowId>``                      ``monarch:<rowId>``
gap          ``gap:<wealthfolioAccount>:<day>``       ``gap:...`` (reconciliation)
===========  ======================================  ==================================

The invariant that makes a source-agnostic join possible is that the *trailing*
token -- the per-transaction identifier the institution or export supplied -- is
byte-identical on both sides. This module parses both shapes into the same
``(sourceSystem, sourceKey)`` pair and joins on it.

Where a source cannot supply a stable per-transaction id at all (a CSV extract
with no FITID synthesizes one from the account identifier, which differs between
the canonical build and the Wealthfolio import), the join falls back to an
exact, *conservative* normalized account/date/amount/description match, and only
when that match is unique on both sides. Anything ambiguous is preserved as an
ambiguity and left for a human; it is never guessed.

This module is pure: no network, no writes, no global state.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence

from importers.rebuild.decisions import DecisionError
from importers.simplefin.pipeline import normalize_description

#: Sources whose key carries an account segment between the system name and the
#: per-transaction id, i.e. ``<system>:<account>:<key>``. Every other source
#: uses ``<system>:<key>`` and the whole remainder is the per-transaction id.
#: Splitting is bounded to two segments so a per-transaction id that itself
#: contains a colon survives the round trip intact.
ACCOUNT_SCOPED_SOURCES = frozenset({"simplefin", "extract", "gap"})

#: Sources that exist to reconcile a balance or restate history rather than to
#: record a purchase. They are reported, never categorized.
STRUCTURAL_SOURCES = frozenset({"gap", "rebuild"})

#: Canonical ``transaction_kind`` values whose reviewed category may be carried
#: onto a live activity. The canonical builder only writes a ``category_id`` for
#: exactly these kinds, so the two gates agree by construction.
CATEGORIZABLE_KINDS = frozenset({"expense", "income", "refund", "reimbursement"})

_CENTS = Decimal("0.01")


@dataclass(frozen=True)
class ParsedIdentity:
    """The stable source identity carried by one idempotency key or source id."""

    source_system: str
    account_token: str
    source_key: str

    @property
    def account_scoped(self) -> bool:
        return self.source_system in ACCOUNT_SCOPED_SOURCES

    @property
    def structural(self) -> bool:
        return self.source_system in STRUCTURAL_SOURCES


def parse_source_identity(key: str | None) -> ParsedIdentity | None:
    """Split a stable key into ``(system, accountToken, perTransactionKey)``.

    Returns ``None`` for an empty or system-less key, which is the fail-closed
    signal that this activity has no stable identity to join on.
    """
    text = str(key or "").strip()
    if ":" not in text:
        return None
    system, remainder = text.split(":", 1)
    system = system.strip().casefold()
    if not system or not remainder:
        return None
    if system in ACCOUNT_SCOPED_SOURCES:
        account_token, _, source_key = remainder.partition(":")
        if not source_key:
            # ``<system>:<key>`` with no account segment: treat the remainder as
            # the per-transaction key rather than silently losing it.
            return ParsedIdentity(system, "", account_token)
        return ParsedIdentity(system, account_token, source_key)
    return ParsedIdentity(system, "", remainder)


def activity_identity(activity: dict[str, Any]) -> ParsedIdentity | None:
    return parse_source_identity(activity.get("idempotencyKey"))


def activity_description(activity: dict[str, Any]) -> str:
    return str(
        activity.get("comment")
        or activity.get("notes")
        or activity.get("description")
        or ""
    )


def amount_key(value: Any) -> str:
    """A sign-free, cent-quantized amount string usable as a dictionary key."""
    try:
        return format(abs(Decimal(str(value or 0))).quantize(_CENTS), "f")
    except (InvalidOperation, ValueError, ArithmeticError):
        raise DecisionError("transaction amount is not a decimal value") from None


@dataclass(frozen=True)
class CanonicalTransaction:
    """One canonical transaction row, reduced to what categorization needs."""

    source_id: str
    account_id: str
    date: str
    amount: str
    description: str
    category: str
    category_id: str
    transaction_kind: str
    transfer_group: str
    split_group: str
    excluded: bool
    assignment_source: str
    source_system: str
    source_account_token: str
    source_key: str

    @property
    def categorizable(self) -> bool:
        """Is this row a reviewed, carryable spending/income decision?

        The canonical builder writes ``category_id`` only for expense, income,
        refund and reimbursement rows, so a transfer, card payment or
        reconciliation can never present itself as a purchase category here.
        """
        return bool(
            self.category
            and self.category_id
            and not self.excluded
            and not self.transfer_group
            and self.transaction_kind in CATEGORIZABLE_KINDS
        )


def _fallback_key(
    account_id: str, when: str, amount: Any, description: str
) -> tuple[str, str, str, str]:
    return (
        str(account_id or ""),
        str(when or "")[:10],
        amount_key(amount),
        normalize_description(str(description or "")),
    )


@dataclass
class CanonicalIndex:
    """Canonical transactions indexed for exact and conservative joins."""

    by_identity: dict[tuple[str, str], list[CanonicalTransaction]]
    by_fallback: dict[tuple[str, str, str, str], list[CanonicalTransaction]]
    rows: list[CanonicalTransaction]

    @property
    def source_systems(self) -> set[str]:
        return {row.source_system for row in self.rows if row.source_system}


def build_canonical_index(
    rows: Iterable[dict[str, Any]]
) -> CanonicalIndex:
    by_identity: dict[tuple[str, str], list[CanonicalTransaction]] = defaultdict(list)
    by_fallback: dict[
        tuple[str, str, str, str], list[CanonicalTransaction]
    ] = defaultdict(list)
    parsed_rows: list[CanonicalTransaction] = []
    for row in rows:
        source_id = str(row.get("source_id") or "")
        identity = parse_source_identity(source_id)
        record = CanonicalTransaction(
            source_id=source_id,
            account_id=str(row.get("account_id") or ""),
            date=str(row.get("date") or "")[:10],
            amount=str(row.get("amount") or "0"),
            description=str(row.get("description") or ""),
            category=str(row.get("category") or "").strip(),
            category_id=str(row.get("category_id") or "").strip(),
            transaction_kind=str(row.get("transaction_kind") or "").strip(),
            transfer_group=str(row.get("transfer_group") or "").strip(),
            split_group=str(row.get("split_group") or "").strip(),
            excluded=str(row.get("excluded") or "").casefold() == "true",
            assignment_source=str(row.get("assignment_source") or "").strip(),
            source_system=identity.source_system if identity else "",
            source_account_token=identity.account_token if identity else "",
            source_key=identity.source_key if identity else "",
        )
        parsed_rows.append(record)
        if identity:
            by_identity[(record.source_system, record.source_key)].append(record)
        by_fallback[
            _fallback_key(
                record.account_id, record.date, record.amount, record.description
            )
        ].append(record)
    return CanonicalIndex(dict(by_identity), dict(by_fallback), parsed_rows)


def load_canonical_index(path: Path) -> CanonicalIndex:
    with path.open(encoding="utf-8-sig", newline="") as source:
        return build_canonical_index(list(csv.DictReader(source)))


@dataclass(frozen=True)
class AccountBridge:
    """Two-way map between canonical account ids and live Wealthfolio ids."""

    canonical_to_live: dict[str, str]
    live_to_canonical: dict[str, str]

    def canonical_for_live(self, live_account_id: str) -> str:
        return self.live_to_canonical.get(str(live_account_id or ""), "")

    def live_for_canonical(self, canonical_account_id: str) -> str:
        return self.canonical_to_live.get(str(canonical_account_id or ""), "")


def build_account_bridge(
    *,
    canonical_account_map: dict[str, Any] | None = None,
    reviewed_plan: dict[str, Any] | None = None,
) -> AccountBridge:
    """Merge every known canonical-to-live account mapping into one bridge.

    ``canonical_account_map`` is the private ``canonical-account-map.json``
    (canonical id -> Wealthfolio id) already used by the rebuild commands.
    ``reviewed_plan`` is a reviewed SimpleFIN source plan, which carries the
    same relation for SimpleFIN accounts. A conflicting mapping is a
    fail-closed error rather than a silent last-writer-wins.
    """
    canonical_to_live: dict[str, str] = {}
    live_to_canonical: dict[str, str] = {}

    def link(canonical: str, live: str) -> None:
        canonical = str(canonical or "").strip()
        live = str(live or "").strip()
        if not canonical or not live:
            return
        if canonical_to_live.setdefault(canonical, live) != live:
            raise DecisionError(
                "canonical account maps to more than one Wealthfolio account"
            )
        if live_to_canonical.setdefault(live, canonical) != canonical:
            raise DecisionError(
                "Wealthfolio account maps to more than one canonical account"
            )

    for canonical, live in (canonical_account_map or {}).items():
        if not isinstance(live, str):
            raise DecisionError("canonical account map values must be strings")
        link(canonical, live)
    for account in (reviewed_plan or {}).get("accounts", []) or []:
        if not isinstance(account, dict):
            raise DecisionError("reviewed source plan accounts must be objects")
        link(
            str(account.get("assertionAccountId") or ""),
            str(account.get("wealthfolioAccountId") or ""),
        )
    return AccountBridge(canonical_to_live, live_to_canonical)


@dataclass(frozen=True)
class SourceResolution:
    """How one live activity was joined to canonical evidence, or why not.

    ``identity`` is the exact three-field identity the sealed plan, the staging
    rehearsal and the production promotion all key on. It is ``None`` whenever
    the join abstained, and ``reason`` then names the abstention so the review
    report can show it.

    ``merchant_identity`` is the weaker identity that merchant-history matching
    can use when no canonical row was found. It names *where a decision would be
    written and verified*, not *which canonical transaction this is*, so it is
    deliberately never used for canonical carryover.
    """

    source_system: str
    source_key: str
    identity: dict[str, str] | None = None
    match_kind: str = ""
    reason: str = ""
    canonical_source_id: str = ""
    canonical_category: str = ""
    canonical_category_id: str = ""
    transaction_kind: str = ""
    structural: bool = False
    merchant_identity: dict[str, str] | None = None


def _resolution_identity(
    canonical: CanonicalTransaction, parsed: ParsedIdentity
) -> dict[str, str]:
    """Build the portable identity, preferring the *live* per-transaction key.

    ``sourceId`` has to be the live key because the staging rehearsal rebuilds
    a staging idempotency key from it. ``sourceAccountId`` is taken from the
    canonical side, which is the stable, institution-scoped account token a
    private activity override names.
    """
    return {
        "canonicalAccountId": canonical.account_id,
        "sourceAccountId": canonical.source_account_token or canonical.account_id,
        "sourceId": parsed.source_key,
    }


class SourceResolver:
    """Join live activities to canonical evidence, preserving ambiguity.

    Resolution runs in two passes over the *whole* candidate window, so the
    outcome never depends on the order activities happen to arrive in:

    1. Exact source identity, narrowed by account when a key is reused.
    2. A conservative account/date/amount/description match, but only for
       activities pass 1 left unresolved, only against canonical rows pass 1
       did not already claim, and only when the match is unique on both sides.

    Anything still unresolved -- or resolvable two ways -- is an abstention
    naming why, never a guess.
    """

    def __init__(
        self,
        index: CanonicalIndex,
        bridge: AccountBridge,
        activities: Sequence[dict[str, Any]] = (),
        *,
        allow_fallback: bool = True,
    ) -> None:
        self._index = index
        self._bridge = bridge
        self._allow_fallback = allow_fallback
        self._resolutions: dict[str, SourceResolution] = {}
        self._resolve_all(list(activities))

    # -- pass helpers ------------------------------------------------------

    def _fallback_key_for(
        self, activity: dict[str, Any]
    ) -> tuple[str, str, str, str] | None:
        canonical_account = self._bridge.canonical_for_live(
            str(activity.get("accountId") or "")
        )
        if not canonical_account:
            return None
        return _fallback_key(
            canonical_account,
            str(activity.get("date") or ""),
            activity.get("amount"),
            activity_description(activity),
        )

    @staticmethod
    def _matched(
        parsed: ParsedIdentity, canonical: CanonicalTransaction, match_kind: str
    ) -> SourceResolution:
        return SourceResolution(
            source_system=parsed.source_system,
            source_key=parsed.source_key,
            identity=_resolution_identity(canonical, parsed),
            match_kind=match_kind,
            canonical_source_id=canonical.source_id,
            canonical_category=canonical.category if canonical.categorizable else "",
            canonical_category_id=(
                canonical.category_id if canonical.categorizable else ""
            ),
            transaction_kind=canonical.transaction_kind,
        )

    def _merchant_identity(
        self, activity: dict[str, Any], parsed: ParsedIdentity | None
    ) -> dict[str, str] | None:
        """A portable identity usable for *merchant* history matching only.

        Categorizing from merchant consensus does not need to know which exact
        canonical transaction this is. It needs somewhere stable to write the
        decision back to, and something to verify byte for byte at promotion
        time. Both come from the live activity's own per-transaction key plus
        the account bridge, so an activity whose canonical row is missing,
        ambiguous or simply never built is still reachable by merchant history.

        ``sourceAccountId`` is the canonical account rather than the live key's
        account segment: the live segment is a Wealthfolio account id, which is
        a different namespace from the institution-scoped token a canonical
        match would have supplied.
        """
        if parsed is None or parsed.structural or not parsed.source_key:
            return None
        canonical_account = self._bridge.canonical_for_live(
            str(activity.get("accountId") or "")
        )
        if not canonical_account:
            return None
        return {
            "canonicalAccountId": canonical_account,
            "sourceAccountId": canonical_account,
            "sourceId": parsed.source_key,
        }

    def _abstain(
        self,
        activity: dict[str, Any] | None,
        parsed: ParsedIdentity | None,
        reason: str,
    ) -> SourceResolution:
        """Refuse to join, naming why. ``match_kind`` stays empty: nothing matched."""
        return SourceResolution(
            source_system=parsed.source_system if parsed else "",
            source_key=parsed.source_key if parsed else "",
            reason=reason,
            merchant_identity=(
                self._merchant_identity(activity, parsed)
                if activity is not None
                else None
            ),
        )

    def _identity_match(
        self, activity: dict[str, Any], parsed: ParsedIdentity
    ) -> tuple[CanonicalTransaction | None, str]:
        rows = self._index.by_identity.get(
            (parsed.source_system, parsed.source_key), []
        )
        if len(rows) == 1:
            return rows[0], "identity-exact"
        if len(rows) > 1:
            canonical_account = self._bridge.canonical_for_live(
                str(activity.get("accountId") or "")
            )
            narrowed = [row for row in rows if row.account_id == canonical_account]
            if len(narrowed) == 1:
                return narrowed[0], "identity-account"
            return None, "ambiguous-source-identity"
        return None, ""

    # -- the two passes ----------------------------------------------------

    def _resolve_all(self, activities: list[dict[str, Any]]) -> None:
        parsed_by_activity: dict[str, ParsedIdentity] = {}
        by_activity_id: dict[str, dict[str, Any]] = {}
        pending: list[dict[str, Any]] = []
        identity_claims: dict[str, list[str]] = defaultdict(list)
        identity_matches: dict[str, tuple[ParsedIdentity, CanonicalTransaction, str]] = {}

        for activity in activities:
            activity_id = str(activity.get("id") or "")
            by_activity_id[activity_id] = activity
            parsed = activity_identity(activity)
            if parsed is None:
                self._resolutions[activity_id] = self._abstain(
                    activity, None, "unknown-source-identity"
                )
                continue
            parsed_by_activity[activity_id] = parsed
            if parsed.structural:
                self._resolutions[activity_id] = SourceResolution(
                    source_system=parsed.source_system,
                    source_key=parsed.source_key,
                    match_kind="structural-source",
                    reason="structural-source",
                    structural=True,
                )
                continue
            canonical, match_kind = self._identity_match(activity, parsed)
            if canonical is not None:
                identity_matches[activity_id] = (parsed, canonical, match_kind)
                identity_claims[canonical.source_id].append(activity_id)
                continue
            if match_kind:
                self._resolutions[activity_id] = self._abstain(
                    activity, parsed, match_kind
                )
                continue
            pending.append(activity)

        # Two live activities cannot both be the same canonical transaction.
        # Neither wins: both abstain, so the outcome does not depend on which
        # one the API happened to list first.
        claimed: set[str] = set()
        for activity_id, (parsed, canonical, match_kind) in identity_matches.items():
            if len(identity_claims[canonical.source_id]) > 1:
                self._resolutions[activity_id] = self._abstain(
                    by_activity_id.get(activity_id), parsed, "ambiguous-canonical-claim"
                )
                continue
            claimed.add(canonical.source_id)
            self._resolutions[activity_id] = self._matched(
                parsed, canonical, match_kind
            )

        if not self._allow_fallback:
            for activity in pending:
                activity_id = str(activity.get("id") or "")
                self._resolutions[activity_id] = self._abstain(
                    activity,
                    parsed_by_activity[activity_id],
                    "unresolved-source-identity",
                )
            return

        live_key_counts: Counter[tuple[str, str, str, str]] = Counter()
        for activity in pending:
            key = self._fallback_key_for(activity)
            if key is not None:
                live_key_counts[key] += 1

        fallback_claims: dict[str, list[str]] = defaultdict(list)
        fallback_matches: dict[str, tuple[ParsedIdentity, CanonicalTransaction]] = {}
        for activity in pending:
            activity_id = str(activity.get("id") or "")
            parsed = parsed_by_activity[activity_id]
            key = self._fallback_key_for(activity)
            if key is None:
                self._resolutions[activity_id] = self._abstain(
                    activity, parsed, "unmapped-account"
                )
                continue
            if live_key_counts[key] > 1:
                self._resolutions[activity_id] = self._abstain(
                    activity, parsed, "ambiguous-fallback-match"
                )
                continue
            candidates = [
                row
                for row in self._index.by_fallback.get(key, [])
                if row.source_id not in claimed
            ]
            if len(candidates) == 1:
                fallback_matches[activity_id] = (parsed, candidates[0])
                fallback_claims[candidates[0].source_id].append(activity_id)
                continue
            self._resolutions[activity_id] = self._abstain(
                activity,
                parsed,
                "ambiguous-fallback-match"
                if candidates
                else "unresolved-source-identity",
            )

        for activity_id, (parsed, canonical) in fallback_matches.items():
            if len(fallback_claims[canonical.source_id]) > 1:
                self._resolutions[activity_id] = self._abstain(
                    by_activity_id.get(activity_id), parsed, "ambiguous-canonical-claim"
                )
                continue
            self._resolutions[activity_id] = self._matched(
                parsed, canonical, "fallback-unique"
            )

    def __call__(self, activity: dict[str, Any]) -> SourceResolution:
        activity_id = str(activity.get("id") or "")
        resolved = self._resolutions.get(activity_id)
        if resolved is not None:
            return resolved
        # An activity the resolver was not constructed with cannot be checked
        # for uniqueness against its window, so it never gets a fallback match.
        parsed = activity_identity(activity)
        if parsed is None:
            return self._abstain(activity, None, "unknown-source-identity")
        canonical, match_kind = self._identity_match(activity, parsed)
        if canonical is not None:
            return self._matched(parsed, canonical, match_kind)
        return self._abstain(
            activity, parsed, match_kind or "unresolved-source-identity"
        )
