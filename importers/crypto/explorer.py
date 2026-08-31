"""Interface for an optional block-explorer refresh.

No implementation is supplied or called here. An adapter necessarily receives
sensitive public wallet material, so callers must opt in and must not log,
serialize, or send it anywhere except to their explicitly selected provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .ledger_live import Account


@dataclass(frozen=True)
class ExplorerAccountSecret:
    """Sensitive public derivation material passed only to an opted-in adapter."""

    xpub: str = field(repr=False)
    derivation_path: str

    def __repr__(self) -> str:
        return (
            "ExplorerAccountSecret(xpub=<redacted>, "
            f"derivation_path={self.derivation_path!r})"
        )


class BlockExplorerAdapter(Protocol):
    """An external adapter capable of returning a normalized account snapshot."""

    def fetch_account(self, secret: ExplorerAccountSecret) -> Account:
        """Fetch one account without logging or persisting ``secret``."""
