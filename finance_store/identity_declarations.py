"""Read the durable private identity declarations, once, for every producer.

Two producers resolve canonical identity from the same household evidence: the
canonical projection reads canonical transaction rows, and the private
PostgreSQL shadow reads sealed forensic activities.  They must see the *same*
durable declarations -- coverage-authority intervals, duplicate-summary account
decisions, scoped provider-token lineage -- or the shadow reports unresolved
duplicate groups that canonical has already resolved, and the shadow evidence
stops matching canonical.

This module is the single reader.  It never infers a declaration: a missing
file yields nothing at all, and every existing conservative default is
unchanged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .identity import (
    DEFAULT_POLICY,
    DuplicateSummaryMapping,
    IdentityPolicy,
    ProviderTokenScope,
    build_source_authority,
    duplicate_summary_mappings,
    provider_token_scopes,
    read_ofx_statement_window,
)


DUPLICATE_SUMMARY_RELATIVE = Path("simplefin") / "account-map.json"
TOKEN_SCOPE_RELATIVE = Path("identity") / "provider-token-scopes.json"
SOURCE_AUTHORITY_RELATIVE = Path("identity") / "source-authority.json"


class DeclarationError(RuntimeError):
    """A private declaration exists but cannot be read or trusted.

    The ``code`` is the stable blocker string each caller re-raises in its own
    error type, so the codes published today do not change.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _document(path: Path, unreadable_code: str) -> tuple[dict[str, Any], str] | None:
    """The parsed declaration and the SHA-256 of the exact bytes that made it."""

    if not path.is_file():
        return None
    raw = path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeclarationError(unreadable_code) from exc
    if not isinstance(document, dict):
        raise DeclarationError(unreadable_code)
    return document, hashlib.sha256(raw).hexdigest()


def duplicate_summaries(root: Path) -> tuple[DuplicateSummaryMapping, ...]:
    """Durable duplicate-summary account decisions, read from the private map.

    Only an explicit operator decision naming both source accounts admits a
    cross-account suppression downstream.  A missing or unreadable map yields no
    mappings at all, so the conservative behaviour is unchanged.
    """

    loaded = _document(
        root / DUPLICATE_SUMMARY_RELATIVE, "simplefin-account-map-unreadable"
    )
    if loaded is None:
        return ()
    document, map_hash = loaded
    try:
        return duplicate_summary_mappings(
            document,
            map_hash=map_hash,
            source_family="simplefin",
        )
    except ValueError as exc:
        raise DeclarationError("simplefin-duplicate-summary-map-invalid") from exc


def token_scopes(root: Path) -> tuple[ProviderTokenScope, ...]:
    """Durable scoped provider-token decisions, read from the private map.

    A shared provider token is only lineage when an operator has proven the two
    namespaces are the same scoped account and import provenance.  Without the
    file, or without a fully specified entry inside it, no token is ever equated
    across providers.
    """

    loaded = _document(
        root / TOKEN_SCOPE_RELATIVE, "provider-token-scope-map-unreadable"
    )
    if loaded is None:
        return ()
    document, map_hash = loaded
    try:
        return provider_token_scopes(document, map_hash=map_hash)
    except ValueError as exc:
        raise DeclarationError("provider-token-scope-map-invalid") from exc


def _bind_ofx_statement_window(root: Path, record: Any) -> Any:
    """Replace a declared OFX/QFX statement file with the window it states.

    The record names a file under the private root and the SHA-256 it expects.
    This reader verifies the bytes, then reads ``DTSTART`` / ``DTEND`` from the
    export's own transaction list.  The dates are therefore the institution's
    claim about what the statement covers, bound to the exact bytes that made
    the claim -- not the minimum and maximum dates of the rows inside it.
    """

    if not isinstance(record, Mapping):
        raise DeclarationError("source-authority-map-invalid")
    declared = record.get("ofxStatementFile")
    if declared is None:
        return record
    if not isinstance(declared, Mapping):
        raise DeclarationError("source-authority-statement-invalid")
    relative = str(declared.get("path") or "")
    expected = str(declared.get("sourceSha256") or "")
    if not relative or not expected:
        raise DeclarationError("source-authority-statement-incomplete")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        raise DeclarationError("source-authority-statement-outside-root") from None
    if not path.is_file():
        raise DeclarationError("source-authority-statement-missing")
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise DeclarationError("source-authority-statement-hash-mismatch")
    try:
        window = read_ofx_statement_window(
            raw.decode("utf-8", errors="replace"),
            account_id=(
                str(declared["accountId"]) if declared.get("accountId") else None
            ),
        )
    except ValueError as exc:
        raise DeclarationError("source-authority-statement-window-invalid") from exc
    bound = dict(record)
    bound.pop("ofxStatementFile", None)
    bound["ofx_statement_window"] = {
        "source_sha256": actual,
        "statement_start": window.statement_start,
        "statement_end": window.statement_end,
        "account_id": window.account_id,
    }
    hashes = list(bound.get("source_hashes") or [])
    if actual not in {str(item) for item in hashes}:
        hashes.append(actual)
    bound["source_hashes"] = hashes
    return bound


def source_authority(root: Path) -> IdentityPolicy:
    """The identity policy, carrying any durable coverage-authority intervals.

    Coverage authority is evidence, never an inference: this reader takes the
    explicit interval records an operator recorded and hands them to
    ``build_source_authority`` unchanged.  It never derives a window from the
    minimum and maximum dates it happens to see in the rows.  An OFX/QFX record
    may name its statement file instead of literal boundaries, in which case the
    file's own ``DTSTART`` / ``DTEND`` are read and bound to its verified hash.
    Without the file the returned policy is ``DEFAULT_POLICY`` itself, so the
    conservative behaviour and every existing policy hash are unchanged.
    """

    loaded = _document(
        root / SOURCE_AUTHORITY_RELATIVE, "source-authority-map-unreadable"
    )
    if loaded is None:
        return DEFAULT_POLICY
    document, _map_hash = loaded
    records = document.get("coverageIntervals")
    if records is None:
        return DEFAULT_POLICY
    if not isinstance(records, list):
        raise DeclarationError("source-authority-map-invalid")
    if not records:
        return DEFAULT_POLICY
    bound = [_bind_ofx_statement_window(root, record) for record in records]
    try:
        authority = build_source_authority(bound)
    except (ValueError, TypeError) as exc:
        raise DeclarationError("source-authority-map-invalid") from exc
    return replace(DEFAULT_POLICY, source_authority=authority)


@dataclass(frozen=True, slots=True)
class DeclaredIdentityInputs:
    """Everything a producer needs to resolve identity the same way twice."""

    policy: IdentityPolicy
    duplicate_summaries: tuple[DuplicateSummaryMapping, ...]
    token_scopes: tuple[ProviderTokenScope, ...]

    def evidence(self) -> dict[str, Any]:
        """Safe aggregates: hashes and counts, never a declaration's content."""

        return {
            "policyVersion": self.policy.version,
            "policyHash": self.policy.policy_hash,
            "authorityHash": self.policy.source_authority.authority_hash,
            "authorityIntervalCount": len(self.policy.source_authority.intervals),
            "duplicateSummaryCount": len(self.duplicate_summaries),
            "duplicateSummaryMapHashes": sorted(
                {item.map_hash for item in self.duplicate_summaries}
            ),
            "providerTokenScopeCount": len(self.token_scopes),
            "providerTokenScopeMapHashes": sorted(
                {item.map_hash for item in self.token_scopes}
            ),
        }


def load_declarations(root: str | Path) -> DeclaredIdentityInputs:
    """Read every durable identity declaration under one private root."""

    resolved = Path(root)
    return DeclaredIdentityInputs(
        policy=source_authority(resolved),
        duplicate_summaries=duplicate_summaries(resolved),
        token_scopes=token_scopes(resolved),
    )
