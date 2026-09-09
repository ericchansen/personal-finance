"""Transactional in-memory repository for conformance and interruption tests."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import replace

from .domain import (
    AppStateSnapshot,
    DurableDecision,
    FinanceState,
    ObservationBatch,
    ReconciliationPlan,
)


def _unique(items, key):
    found = {}
    for item in items:
        found.setdefault(key(item), item)
    return tuple(found[value] for value in sorted(found))


def _merge_source_accounts(existing, incoming):
    found = {item.id: item for item in existing}
    for item in incoming:
        previous = found.get(item.id)
        if previous:
            mapping_changed = previous.canonical_key != item.canonical_key
            refresh = item.observed_at >= previous.observed_at
            item = replace(
                item,
                effective_from=previous.effective_from,
                mapping_effective_from=(
                    (item.mapping_effective_from or item.effective_from)
                    if mapping_changed
                    else (previous.mapping_effective_from or previous.effective_from)
                ),
                name=item.name if refresh else previous.name,
                account_type=item.account_type if refresh else previous.account_type,
                currency=item.currency if refresh else previous.currency,
                observed_at=item.observed_at if refresh else previous.observed_at,
                processed_at=item.processed_at if refresh else previous.processed_at,
                trust_cutoff_at=(
                    item.trust_cutoff_at
                    if item.trust_cutoff_provided
                    else previous.trust_cutoff_at
                ),
                status=(
                    item.status
                    if item.status_provided and refresh
                    else previous.status
                ),
                status_provided=False,
                trust_cutoff_provided=False,
            )
        else:
            item = replace(
                item,
                trust_cutoff_provided=False,
                status_provided=False,
            )
        found[item.id] = item
    return tuple(found[value] for value in sorted(found))


class _MemoryUnit:
    def __init__(self, repository: "MemoryRepository") -> None:
        self.repository = repository
        self.working = repository._state

    def state(self) -> FinanceState:
        return self.working

    def append_decision(self, decision: DurableDecision) -> None:
        self.working = replace(
            self.working,
            decisions=_unique((*self.working.decisions, decision), lambda item: item.id),
        )

    def record_app_state(self, snapshot: AppStateSnapshot) -> None:
        self.working = replace(
            self.working,
            app_state_snapshots=_unique(
                (*self.working.app_state_snapshots, snapshot), lambda item: item.id
            ),
        )

    def apply(self, batch: ObservationBatch, plan: ReconciliationPlan) -> FinanceState:
        sightings = {item.id: item for item in batch.transactions}
        existing_transactions = [
            replace(
                item,
                last_seen_at=sightings[item.id].last_seen_at,
                last_seen_run_id=sightings[item.id].last_seen_run_id,
                last_seen_order=sightings[item.id].last_seen_order,
            )
            if item.id in sightings
            and (
                sightings[item.id].last_seen_at,
                sightings[item.id].last_seen_run_id,
                sightings[item.id].last_seen_order,
            )
            > (item.last_seen_at, item.last_seen_run_id, item.last_seen_order)
            else item
            for item in self.working.transaction_observations
        ]
        transactions = _unique(
            (*existing_transactions, *batch.transactions),
            lambda item: (item.source_account_id, item.source_transaction_id, item.observation_hash),
        )
        canonical_transactions = {
            item.id: item for item in (*self.working.canonical_transactions, *plan.canonical_transactions)
        }
        for mutation in plan.canonical_mutations:
            current = canonical_transactions[mutation.transaction_id]
            canonical_transactions[mutation.transaction_id] = replace(
                current,
                effective_at=mutation.effective_at,
                amount=mutation.amount,
                currency=mutation.currency,
                description=mutation.description,
                status=mutation.status,
            )
        projection_records = list(self.working.projection_records)
        new_projection_keys = {
            (item.target_system, item.canonical_transaction_id)
            for item in plan.projection_records
        }
        projection_records = [
            replace(item, current=False, status="superseded")
            if item.current
            and (item.target_system, item.canonical_transaction_id)
            in new_projection_keys
            or (
                item.current
                and item.canonical_transaction_id in plan.projection_withdrawals
            )
            else item
            for item in projection_records
        ]
        self.working = FinanceState(
            blobs=_unique((*self.working.blobs, batch.blob), lambda item: item.id),
            runs=_unique((*self.working.runs, batch.run), lambda item: item.id),
            connections=_unique(
                (*self.working.connections, batch.connection), lambda item: item.id
            ),
            source_accounts=_merge_source_accounts(
                self.working.source_accounts, batch.accounts
            ),
            transaction_observations=transactions,
            balance_observations=_unique(
                (*self.working.balance_observations, *batch.balances), lambda item: item.id
            ),
            position_observations=_unique(
                (*self.working.position_observations, *batch.positions), lambda item: item.id
            ),
            valuation_observations=_unique(
                (*self.working.valuation_observations, *batch.valuations), lambda item: item.id
            ),
            artifact_observations=_unique(
                (*self.working.artifact_observations, *batch.artifacts),
                lambda item: item.id,
            ),
            canonical_accounts=_unique(
                (*self.working.canonical_accounts, *plan.canonical_accounts),
                lambda item: item.id,
            ),
            canonical_transactions=tuple(
                canonical_transactions[key] for key in sorted(canonical_transactions)
            ),
            links=_unique((*self.working.links, *plan.links), lambda item: item.id),
            decisions=_unique(
                (*self.working.decisions, *plan.decisions), lambda item: item.id
            ),
            issues=_unique((*self.working.issues, *plan.issues), lambda item: item.id),
            app_state_snapshots=self.working.app_state_snapshots,
            projection_runs=_unique(
                (
                    *self.working.projection_runs,
                    *((plan.projection_run,) if plan.projection_run else ()),
                ),
                lambda item: item.id,
            ),
            projection_records=_unique(
                (*projection_records, *plan.projection_records),
                lambda item: item.id,
            ),
            audit_events=_unique(
                (*self.working.audit_events, *plan.audit_events), lambda item: item.id
            ),
        )
        return self.working


class MemoryRepository:
    def __init__(self, state: FinanceState | None = None) -> None:
        self._state = state or FinanceState()
        self._lock = threading.RLock()

    @contextmanager
    def unit_of_work(self, lock_key: str):
        del lock_key
        with self._lock:
            unit = _MemoryUnit(self)
            yield unit
            self._state = unit.working

    def state(self) -> FinanceState:
        with self._lock:
            return self._state

    def append_decision(self, decision: DurableDecision) -> None:
        with self.unit_of_work(f"decision:{decision.subject_type}:{decision.subject_key}") as unit:
            unit.append_decision(decision)

    def record_app_state(self, snapshot: AppStateSnapshot) -> None:
        with self.unit_of_work(f"app-state:{snapshot.app_name}:{snapshot.state_kind}") as unit:
            unit.record_app_state(snapshot)
