"""Conservative reconciliation: uncertainty becomes review, never deletion."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from .domain import (
    AuditEvent,
    CanonicalAccount,
    CanonicalMutation,
    CanonicalTransaction,
    DurableDecision,
    FinanceState,
    IngestionResult,
    ObservationBatch,
    ObservationLink,
    ProjectionRecord,
    ProjectionRun,
    QualityIssue,
    ReconciliationPlan,
    content_hash,
    normalized_description,
    stable_id,
)
from .replay import state_digest
from .repository import FinanceRepository


def projection_hash(canonical: CanonicalTransaction) -> str:
    return content_hash({
        "accountId": canonical.account_id,
        "effectiveAt": canonical.effective_at.isoformat(),
        "amount": format(canonical.amount.normalize(), "f"),
        "currency": canonical.currency,
        "description": canonical.description,
        "status": canonical.status,
    })


def _latest_by_identity(state: FinanceState) -> dict[tuple[str, str], object]:
    latest = {}
    for observation in sorted(
        state.transaction_observations,
        key=_sighting_key,
    ):
        latest[(observation.source_account_id, observation.source_transaction_id)] = observation
    return latest


def _sighting_key(observation) -> tuple:
    return (
        observation.last_seen_at,
        observation.last_seen_run_id,
        observation.last_seen_order,
        observation.observed_at,
        observation.processed_at,
        observation.id,
    )


def build_plan(
    state: FinanceState,
    batch: ObservationBatch,
    *,
    projection_target: str | None = "wealthfolio",
    projection_version: str = "candidate-v1",
) -> ReconciliationPlan:
    accounts_by_source = {account.id: account for account in batch.accounts}
    canonical_accounts_by_key = {account.key: account for account in state.canonical_accounts}
    links_by_observation = {
        link.observation_id: link for link in state.links if link.current
    }
    canonicals_by_id = {item.id: item for item in state.canonical_transactions}
    latest = _latest_by_identity(state)

    new_accounts: list[CanonicalAccount] = []
    new_canonicals: list[CanonicalTransaction] = []
    mutations: list[CanonicalMutation] = []
    links: list[ObservationLink] = []
    existing_decision_ids = {decision.id for decision in state.decisions}
    existing_issue_ids = {issue.id for issue in state.issues}
    decisions: list[DurableDecision] = [
        decision
        for decision in batch.source_decisions
        if decision.id not in existing_decision_ids
    ]
    issues: list[QualityIssue] = [
        issue for issue in batch.source_issues if issue.id not in existing_issue_ids
    ]
    audit: list[AuditEvent] = []
    replayed: list[str] = []
    projected: list[CanonicalTransaction] = []
    projection_withdrawals: set[str] = set()
    existing_source_accounts = {
        account.id: account for account in state.source_accounts
    }
    existing_issue_subjects = {
        (issue.issue_type, issue.subject_type, issue.subject_key)
        for issue in state.issues
    }

    if (
        batch.run.source_protocol == "simplefin"
        and batch.run.overlap_start is not None
        and batch.run.overlap_end is not None
    ):
        incoming_identities = {
            (item.source_account_id, item.source_transaction_id)
            for item in batch.transactions
        }
        batch_account_ids = {item.id for item in batch.accounts}
        for identity, previous in _latest_by_identity(state).items():
            if (
                identity[0] in batch_account_ids
                and previous.status == "posted"
                and previous.last_seen_at <= batch.run.observed_at
                and batch.run.overlap_start
                <= previous.effective_at
                <= batch.run.overlap_end
                and identity not in incoming_identities
            ):
                issues.append(
                    QualityIssue(
                        id=stable_id(
                            "quality_issue",
                            "vanished_posted",
                            previous.id,
                            batch.blob.id,
                        ),
                        issue_type="missing_history",
                        severity="warning",
                        subject_type="transaction_observation",
                        subject_key=previous.id,
                        details={
                            "policy": "retain_posted_no_silent_deletion",
                            "missingFromSourceBlobId": batch.blob.id,
                        },
                        effective_at=previous.effective_at,
                        observed_at=batch.run.observed_at,
                        processed_at=batch.run.processed_at,
                    )
                )

    for source_account in batch.accounts:
        mapping_effective_from = (
            source_account.mapping_effective_from or source_account.effective_from
        )
        if source_account.canonical_key not in canonical_accounts_by_key:
            canonical = CanonicalAccount(
                id=stable_id("canonical_account", source_account.canonical_key),
                key=source_account.canonical_key,
                display_name=source_account.name or "Unreviewed account",
                account_type=source_account.account_type,
                currency=source_account.currency,
                effective_from=source_account.effective_from,
                observed_at=source_account.observed_at,
                processed_at=source_account.processed_at,
            )
            canonical_accounts_by_key[canonical.key] = canonical
            new_accounts.append(canonical)
        canonical = canonical_accounts_by_key[source_account.canonical_key]
        previous_account = existing_source_accounts.get(source_account.id)
        if previous_account and not source_account.trust_cutoff_provided:
            source_account = replace(
                source_account,
                trust_cutoff_at=previous_account.trust_cutoff_at,
            )
            accounts_by_source[source_account.id] = source_account
        if previous_account and not source_account.status_provided:
            source_account = replace(source_account, status=previous_account.status)
            accounts_by_source[source_account.id] = source_account
        mapping_changed = (
            previous_account is None
            or previous_account.canonical_key != source_account.canonical_key
        )
        if (
            mapping_changed
            and previous_account is not None
            and mapping_effective_from
            < (
                previous_account.mapping_effective_from
                or previous_account.effective_from
            )
        ):
            raise ValueError(
                "source account remapping cannot precede the current mapping"
            )
        if mapping_changed:
            mapping_decision_id = stable_id(
                "decision", "account_mapping", source_account.id, canonical.id
            )
            decisions.append(
                DurableDecision(
                    id=mapping_decision_id,
                    decision_type="account_mapping",
                    subject_type="source_account",
                    subject_key=source_account.id,
                    action=f"map_to:{canonical.id}",
                    rationale="Synthetic/private mapping supplied by ingestion configuration",
                    decided_by="ingestion-service",
                    effective_at=mapping_effective_from,
                    observed_at=source_account.observed_at,
                    processed_at=source_account.processed_at,
                )
            )
            if previous_account is not None:
                audit.append(
                    AuditEvent(
                        id=stable_id(
                            "audit_event",
                            "source_account_remapped",
                            source_account.id,
                            previous_account.canonical_key,
                            source_account.canonical_key,
                            mapping_effective_from.isoformat(),
                        ),
                        event_type="source_account_remapped",
                        actor="ingestion-service",
                        subject_type="source_account",
                        subject_key=source_account.id,
                        data={
                            "previousCanonicalKey": previous_account.canonical_key,
                            "newCanonicalKey": source_account.canonical_key,
                            "mappingDecisionId": mapping_decision_id,
                        },
                        effective_at=mapping_effective_from,
                        observed_at=source_account.observed_at,
                        processed_at=source_account.processed_at,
                    )
                )
        cutoff_changed = (
            source_account.trust_cutoff_provided
            and previous_account is not None
            and previous_account.trust_cutoff_at != source_account.trust_cutoff_at
        )
        cutoff_initial = (
            source_account.trust_cutoff_provided
            and previous_account is None
            and source_account.trust_cutoff_at is not None
        )
        if cutoff_initial or cutoff_changed:
            cutoff_decision_id = stable_id(
                "decision",
                "trust_cutoff",
                source_account.id,
                source_account.trust_cutoff_at or "removed",
                batch.run.id,
            )
            decisions.append(
                DurableDecision(
                    id=cutoff_decision_id,
                    decision_type="trust_cutoff",
                    subject_type="source_account",
                    subject_key=source_account.id,
                    action=(
                        "set_cutoff"
                        if source_account.trust_cutoff_at is not None
                        else "clear_cutoff"
                    ),
                    rationale="Configured source trust boundary",
                    decided_by="ingestion-service",
                    effective_at=(
                        source_account.trust_cutoff_at or source_account.observed_at
                    ),
                    observed_at=source_account.observed_at,
                    processed_at=source_account.processed_at,
                )
            )
            if previous_account is not None:
                audit.append(
                    AuditEvent(
                        id=stable_id(
                            "audit_event",
                            "trust_cutoff_revised",
                            source_account.id,
                            previous_account.trust_cutoff_at,
                            source_account.trust_cutoff_at,
                            batch.run.id,
                        ),
                        event_type="trust_cutoff_revised",
                        actor="ingestion-service",
                        subject_type="source_account",
                        subject_key=source_account.id,
                        data={
                            "previousTrustCutoffAt": (
                                previous_account.trust_cutoff_at.isoformat()
                                if previous_account.trust_cutoff_at
                                else None
                            ),
                            "newTrustCutoffAt": (
                                source_account.trust_cutoff_at.isoformat()
                                if source_account.trust_cutoff_at
                                else None
                            ),
                            "decisionId": cutoff_decision_id,
                            "ingestionRunId": batch.run.id,
                        },
                        effective_at=(
                            source_account.trust_cutoff_at or source_account.observed_at
                        ),
                        observed_at=source_account.observed_at,
                        processed_at=source_account.processed_at,
                    )
                )
            prior_observations = [
                *(
                    (item, "transaction_observation")
                    for item in state.transaction_observations
                    if item.source_account_id == source_account.id
                ),
                *(
                    (item, "balance_observation")
                    for item in state.balance_observations
                    if item.source_account_id == source_account.id
                ),
            ]
            for observation, subject_type in prior_observations:
                issue_key = (
                    "stale_after_trust_cutoff",
                    subject_type,
                    observation.id,
                )
                if (
                    source_account.trust_cutoff_at is not None
                    and observation.effective_at > source_account.trust_cutoff_at
                    and issue_key not in existing_issue_subjects
                ):
                    issues.append(
                        QualityIssue(
                            id=stable_id(
                                "quality_issue",
                                "stale_after_trust_cutoff",
                                observation.id,
                            ),
                            issue_type="stale_after_trust_cutoff",
                            severity="warning",
                            subject_type=subject_type,
                            subject_key=observation.id,
                            details={
                                "trustCutoffAt": (
                                    source_account.trust_cutoff_at.isoformat()
                                ),
                                "policy": "not_projected",
                            },
                            effective_at=observation.effective_at,
                            observed_at=source_account.observed_at,
                            processed_at=source_account.processed_at,
                        )
                    )

            latest_for_account = [
                observation
                for identity, observation in _latest_by_identity(state).items()
                if identity[0] == source_account.id
            ]
            current_links = {
                link.observation_id: link for link in state.links if link.current
            }
            current_projections = {
                record.canonical_transaction_id: record
                for record in state.projection_records
                if (
                    projection_target is not None
                    and record.current
                    and record.target_system == projection_target
                )
            }
            for observation in latest_for_account:
                link = current_links.get(observation.id)
                if not link:
                    continue
                canonical = canonicals_by_id[link.canonical_transaction_id]
                eligible = (
                    observation.status == "posted"
                    and (
                        source_account.trust_cutoff_at is None
                        or observation.effective_at <= source_account.trust_cutoff_at
                    )
                )
                if eligible and canonical.id not in current_projections:
                    projected.append(canonical)
                elif not eligible and canonical.id in current_projections:
                    projection_withdrawals.add(canonical.id)

    all_observations = list(state.transaction_observations)
    all_canonicals = list(state.canonical_transactions)
    for observation in sorted(
        batch.transactions,
        key=lambda item: (
            item.source_account_id,
            item.last_seen_at,
            item.last_seen_run_id,
            item.last_seen_order,
            item.observed_at,
            item.processed_at,
            item.source_transaction_id,
            item.observation_hash,
        ),
    ):
        source_account = accounts_by_source[observation.source_account_id]
        canonical_account = canonical_accounts_by_key[source_account.canonical_key]
        previous = latest.get(
            (observation.source_account_id, observation.source_transaction_id)
        )
        if previous and previous.observation_hash == observation.observation_hash:
            replayed.append(observation.id)
            latest[
                (observation.source_account_id, observation.source_transaction_id)
            ] = replace(
                previous,
                last_seen_at=max(previous.last_seen_at, observation.last_seen_at),
                last_seen_run_id=observation.last_seen_run_id,
                last_seen_order=observation.last_seen_order,
            )
            continue

        previous_link = links_by_observation.get(previous.id) if previous else None
        if previous and previous_link:
            canonical = canonicals_by_id[previous_link.canonical_transaction_id]
            if _sighting_key(observation) <= _sighting_key(previous):
                issues.append(
                    QualityIssue(
                        id=stable_id(
                            "quality_issue",
                            "out_of_order_sighting",
                            observation.id,
                            previous.id,
                        ),
                        issue_type="identity_conflict",
                        severity="info",
                        subject_type="transaction_observation",
                        subject_key=observation.id,
                        details={
                            "policy": "retain_without_rewinding_canonical_state",
                            "newerObservationId": previous.id,
                        },
                        effective_at=observation.effective_at,
                        observed_at=observation.observed_at,
                        processed_at=observation.processed_at,
                    )
                )
                links.append(
                    ObservationLink(
                        id=stable_id("observation_link", observation.id, canonical.id),
                        observation_id=observation.id,
                        canonical_transaction_id=canonical.id,
                        method="source_id",
                        effective_at=observation.effective_at,
                        observed_at=observation.observed_at,
                        processed_at=observation.processed_at,
                    )
                )
                all_observations.append(observation)
                continue
            method = "source_id"
            decision_id = None
            trusted = (
                source_account.trust_cutoff_at is None
                or observation.effective_at <= source_account.trust_cutoff_at
            )
            changed = (
                previous.amount != observation.amount
                or previous.currency != observation.currency
                or previous.effective_at != observation.effective_at
                or normalized_description(previous.description)
                != normalized_description(observation.description)
            )
            if changed and previous.status == "posted":
                decision_id = stable_id(
                    "decision", "correction", observation.id, canonical.id
                )
                decisions.append(
                    DurableDecision(
                        id=decision_id,
                        decision_type="correction",
                        subject_type="canonical_transaction",
                        subject_key=canonical.id,
                        action="apply_latest_source_version",
                        rationale="Same account-scoped source ID supplied changed semantics",
                        decided_by="deterministic-reconciler",
                        effective_at=observation.effective_at,
                        observed_at=observation.observed_at,
                        processed_at=observation.processed_at,
                    )
                )
                method = "correction"
            if changed or (previous.status == "pending" and observation.status == "posted"):
                mutations.append(
                    CanonicalMutation(
                        transaction_id=canonical.id,
                        effective_at=observation.effective_at,
                        amount=observation.amount,
                        currency=observation.currency,
                        description=observation.description,
                        status=observation.status,
                    )
                )
                if trusted and observation.status == "posted":
                    projected.append(replace(
                        canonical,
                        effective_at=observation.effective_at,
                        amount=observation.amount,
                        currency=observation.currency,
                        description=observation.description,
                        status=observation.status,
                    ))
            if previous.status == "posted" and observation.status == "pending":
                issues.append(
                    QualityIssue(
                        id=stable_id(
                            "quality_issue", "status_regression", observation.id
                        ),
                        issue_type="identity_conflict",
                        severity="warning",
                        subject_type="transaction_observation",
                        subject_key=observation.id,
                        details={
                            "previousStatus": "posted",
                            "newStatus": "pending",
                            "policy": "review_before_canonical_regression",
                        },
                        effective_at=observation.effective_at,
                        observed_at=observation.observed_at,
                        processed_at=observation.processed_at,
                    )
                )
            link = ObservationLink(
                id=stable_id("observation_link", observation.id, canonical.id),
                observation_id=observation.id,
                canonical_transaction_id=canonical.id,
                method=method,
                decision_id=decision_id,
                effective_at=observation.effective_at,
                observed_at=observation.observed_at,
                processed_at=observation.processed_at,
            )
            links.append(link)
            links_by_observation[observation.id] = link
            latest[(observation.source_account_id, observation.source_transaction_id)] = observation
            all_observations.append(observation)
            continue

        candidate_ids: list[str] = []
        for old_observation in all_observations:
            old_link = links_by_observation.get(old_observation.id)
            if not old_link:
                continue
            same_semantics = (
                old_observation.amount == observation.amount
                and normalized_description(old_observation.description)
                == normalized_description(observation.description)
                and abs((old_observation.effective_at - observation.effective_at).days) <= 3
            )
            if same_semantics:
                candidate_ids.append(old_link.canonical_transaction_id)
        issue_id = None
        if candidate_ids:
            issue_id = stable_id(
                "quality_issue", "fuzzy_duplicate", observation.id, *sorted(set(candidate_ids))
            )
            issues.append(
                QualityIssue(
                    id=issue_id,
                    issue_type="fuzzy_duplicate",
                    severity="warning",
                    subject_type="transaction_observation",
                    subject_key=observation.id,
                    details={
                        "candidateCanonicalTransactionIds": sorted(set(candidate_ids)),
                        "policy": "review_only_no_automatic_suppression",
                    },
                    effective_at=observation.effective_at,
                    observed_at=observation.observed_at,
                    processed_at=observation.processed_at,
                )
            )

        mirror_ids = []
        for old_observation in all_observations:
            if (
                old_observation.source_account_id != observation.source_account_id
                and old_observation.amount == -observation.amount
                and abs((old_observation.effective_at - observation.effective_at).days) <= 3
            ):
                mirror_ids.append(old_observation.id)
        if mirror_ids:
            mirror_issue_id = stable_id(
                "quality_issue", "cross_account_mirror", observation.id, *sorted(mirror_ids)
            )
            issues.append(
                QualityIssue(
                    id=mirror_issue_id,
                    issue_type="unresolved_duplicate",
                    severity="info",
                    subject_type="transaction_observation",
                    subject_key=observation.id,
                    details={
                        "candidateObservationIds": sorted(mirror_ids),
                        "candidateKind": "cross_account_mirror",
                        "policy": "retain_both_until_reviewed",
                    },
                    effective_at=observation.effective_at,
                    observed_at=observation.observed_at,
                    processed_at=observation.processed_at,
                )
            )

        canonical_key = (
            f"{source_account.canonical_key}:"
            f"{observation.source_account_id}:{observation.source_transaction_id}"
        )
        canonical = CanonicalTransaction(
            id=stable_id("canonical_transaction", canonical_key),
            account_id=canonical_account.id,
            key=canonical_key,
            effective_at=observation.effective_at,
            observed_at=observation.observed_at,
            processed_at=observation.processed_at,
            amount=observation.amount,
            currency=observation.currency,
            description=observation.description,
            status=observation.status,
        )
        new_canonicals.append(canonical)
        all_canonicals.append(canonical)
        canonicals_by_id[canonical.id] = canonical
        link = ObservationLink(
            id=stable_id("observation_link", observation.id, canonical.id),
            observation_id=observation.id,
            canonical_transaction_id=canonical.id,
            method="exact",
            issue_id=issue_id,
            effective_at=observation.effective_at,
            observed_at=observation.observed_at,
            processed_at=observation.processed_at,
        )
        links.append(link)
        links_by_observation[observation.id] = link
        latest[(observation.source_account_id, observation.source_transaction_id)] = observation
        all_observations.append(observation)
        if observation.status == "posted" and (
            source_account.trust_cutoff_at is None
            or observation.effective_at <= source_account.trust_cutoff_at
        ):
            projected.append(canonical)

    for observation in (*batch.transactions, *batch.balances):
        source_account = accounts_by_source[observation.source_account_id]
        cutoff = source_account.trust_cutoff_at
        if cutoff is not None and observation.effective_at > cutoff:
            issues.append(
                QualityIssue(
                    id=stable_id("quality_issue", "stale_after_trust_cutoff", observation.id),
                    issue_type="stale_after_trust_cutoff",
                    severity="warning",
                    subject_type=(
                        "balance_observation"
                        if observation in batch.balances
                        else "transaction_observation"
                    ),
                    subject_key=observation.id,
                    details={
                        "trustCutoffAt": cutoff.isoformat(),
                        "policy": "not_projected",
                    },
                    effective_at=observation.effective_at,
                    observed_at=observation.observed_at,
                    processed_at=observation.processed_at,
                )
            )

    final_projected = {
        canonical.id: canonical
        for canonical in projected
        if canonical.status == "posted"
    }
    final_projected = {
        canonical_id: canonical
        for canonical_id, canonical in final_projected.items()
        if canonical_id not in projection_withdrawals
    }
    projection_run = None
    projection_records: list[ProjectionRecord] = []
    if final_projected and projection_target is not None:
        cutoff = max(item.effective_at for item in final_projected.values())
        projection_run_id = stable_id(
            "projection_run",
            batch.run.id,
            projection_target,
            projection_version,
        )
        projection_run = ProjectionRun(
            id=projection_run_id,
            target_system=projection_target,
            version=projection_version,
            status="succeeded",
            cutoff_effective_at=cutoff,
            observed_at=batch.run.observed_at,
            processed_at=batch.run.processed_at,
        )
        for canonical in sorted(final_projected.values(), key=lambda item: item.key):
            projection_records.append(
                ProjectionRecord(
                    id=stable_id(
                        "projection_record",
                        projection_run_id,
                        canonical.id,
                        projection_target,
                    ),
                    run_id=projection_run_id,
                    canonical_transaction_id=canonical.id,
                    target_system=projection_target,
                    target_record_key=canonical.key,
                    projected_hash=projection_hash(canonical),
                    status="planned",
                    effective_at=canonical.effective_at,
                    observed_at=batch.run.observed_at,
                    processed_at=batch.run.processed_at,
                )
            )

    audit.append(
        AuditEvent(
            id=stable_id("audit_event", "ingestion_reconciled", batch.run.id),
            event_type="ingestion_reconciled",
            actor="deterministic-reconciler",
            subject_type="ingestion_run",
            subject_key=batch.run.id,
            data={
                "newCanonicalTransactions": len(new_canonicals),
                "issues": len(issues),
                "replayedObservations": len(replayed),
            },
            effective_at=batch.run.effective_end or batch.run.observed_at,
            observed_at=batch.run.observed_at,
            processed_at=batch.run.processed_at,
        )
    )
    return ReconciliationPlan(
        canonical_accounts=tuple(new_accounts),
        canonical_transactions=tuple(new_canonicals),
        canonical_mutations=tuple(mutations),
        links=tuple(links),
        decisions=tuple(decisions),
        issues=tuple(issues),
        projection_run=projection_run,
        projection_records=tuple(projection_records),
        projection_withdrawals=tuple(sorted(projection_withdrawals)),
        audit_events=tuple(audit),
        replayed_observation_ids=tuple(replayed),
    )


class IngestionService:
    def __init__(
        self,
        repository: FinanceRepository,
        *,
        projection_target: str | None = "wealthfolio",
        projection_version: str = "candidate-v1",
    ) -> None:
        self.repository = repository
        self.projection_target = projection_target
        self.projection_version = projection_version

    def ingest(
        self,
        batch: ObservationBatch,
        *,
        interrupt_after_observations: int | None = None,
        after_apply: Callable[[object, FinanceState], None] | None = None,
        interruption_factory: Callable[[], Exception] | None = None,
    ) -> IngestionResult:
        """Ingest one sealed unit of work, optionally exercising rollback behavior.

        ``after_apply`` is intentionally invoked after repository writes and before
        the unit of work commits, so companion projections participate in the same
        transaction. Fault injection occurs only after that hook, proving rollback
        across both native evidence and companion projections.
        """
        lock_key = (
            f"{batch.connection.source_system}:{batch.connection.connection_key}"
        )
        with self.repository.unit_of_work(lock_key) as unit:
            before = unit.state()
            plan = build_plan(
                before,
                batch,
                projection_target=self.projection_target,
                projection_version=self.projection_version,
            )
            after = unit.apply(batch, plan)
            if after_apply is not None:
                after_apply(unit, after)
            if interrupt_after_observations is not None:
                exception = (
                    interruption_factory()
                    if interruption_factory is not None
                    else RuntimeError(
                        "ingestion interrupted after "
                        f"{interrupt_after_observations} observations"
                    )
                )
                raise exception
        accepted = len(batch.transactions) - len(plan.replayed_observation_ids)
        return IngestionResult(
            run_id=batch.run.id,
            accepted_observations=accepted,
            replayed_observations=len(plan.replayed_observation_ids),
            canonical_transactions_created=len(plan.canonical_transactions),
            issues_created=len(plan.issues),
            state_hash=state_digest(after),
        )
