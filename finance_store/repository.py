"""Repository boundary used by reconciliation services."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from .domain import (
    AppStateSnapshot,
    DurableDecision,
    FinanceState,
    ObservationBatch,
    ReconciliationPlan,
)


class FinanceUnitOfWork(Protocol):
    def state(self) -> FinanceState: ...

    def apply(self, batch: ObservationBatch, plan: ReconciliationPlan) -> FinanceState: ...

    def append_decision(self, decision: DurableDecision) -> None: ...

    def record_app_state(self, snapshot: AppStateSnapshot) -> None: ...


class FinanceRepository(Protocol):
    def unit_of_work(self, lock_key: str) -> AbstractContextManager[FinanceUnitOfWork]: ...

    def state(self) -> FinanceState: ...

    def append_decision(self, decision: DurableDecision) -> None: ...

    def record_app_state(self, snapshot: AppStateSnapshot) -> None: ...
