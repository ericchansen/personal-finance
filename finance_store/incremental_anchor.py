"""Account-cash anchors and observed source-state transitions, not coverage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json

from . import incremental_bootstrap as bootstrap
from . import incremental_inputs as inputs
from . import incremental_projection as projection
from .domain import content_hash, normalized_description
from .simplefin import detect_protocol_version
from .source_admission import (
    organization_scope, partition_connection_errors, partition_snapshot, snapshot_observed_at,
)
from importers.simplefin.application import _activity_fingerprint, _signed

KIND = "incremental-source-anchor-contract"
RULE = "observed-posted-progress-v1"
TRANSITION_RULE = "observed-posted-progress-v2"
LATE_ARRIVAL_RULE = "explicit-post-anchor-arrival-v1"
HISTORICAL_REASONS = {
    "anchor-source-already-in-cash", "anchor-new-historical-id-held",
    "anchor-date-only-change-not-projected", "anchor-source-not-observed",
    "anchor-history-detail-not-projected",
}


def _rows(connection, sql, params=()):
    from psycopg.rows import dict_row
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def financial_state(state):
    """Cash identity/economics, not user notes or unrelated account presentation."""
    return {
        "account": {key: state["account"].get(key) for key in
                    ("id", "accountType", "currency", "isActive", "isArchived", "trackingMode")},
        "rows": sorted(
            [{**_activity_fingerprint(row), "status": row["status"]} for row in state["rows"]],
            key=lambda row: row["id"],
        ),
    }


def assertion_rows(scope, app):
    prefix = f"rebuild:assertion:{scope.canonical_id}:"
    return [row for row in app["rows"] if str(row.get("idempotencyKey") or "").startswith(prefix)]


def source_anchor(scope, specification):
    path = inputs.bound_file(scope.root, specification["snapshot"])
    raw = inputs.document(path)
    version = detect_protocol_version(raw)
    body = raw.get("data", raw) if version == "2" else raw
    accounts = body.get("accounts")
    inputs.require(isinstance(accounts, list), "anchor-account-inventory-invalid")
    selected = [item for item in accounts
                if str(item.get("id") or item.get("account_id")) == scope.config["sourceAccountId"]
                and organization_scope(item, version) == scope.config["rawConnectionId"]]
    inputs.require(len(selected) == 1, "anchor-account-or-connection-mismatch")
    account = selected[0]
    inputs.require(str(account.get("status") or account.get("account_status") or "active").lower() == "active"
                   and not account.get("holdings") and not account.get("positions"), "anchor-account-not-cash-active")
    unit = inputs.currency(account.get("currency"))
    inputs.require(unit == scope.baseline_currency, "anchor-currency-mismatch")
    observed = snapshot_observed_at(path, datetime.min.replace(tzinfo=timezone.utc))
    inputs.require(observed != datetime.min.replace(tzinfo=timezone.utc), "anchor-source-observation-time-unavailable")
    request = None
    if specification.get("request") is not None:
        request_path = inputs.bound_file(scope.root, specification["request"])
        metadata = inputs.document(request_path)
        inputs.require(str(metadata["protocolVersion"]) == version, "anchor-request-protocol-mismatch")
        start, end = date.fromisoformat(metadata["requestedStart"]), date.fromisoformat(metadata["requestedEnd"])
        inputs.require(0 <= (end - start).days < 90, "anchor-request-window-invalid")
        request = {"sha256": inputs.digest(request_path), "requestedStart": start.isoformat(), "requestedEnd": end.isoformat()}
    partition = partition_snapshot(
        snapshot_sha256=inputs.digest(path), observed_at=observed, version=version,
        accounts=accounts, errors=body.get("errlist" if version == "2" else "errors", []),
        requested_start=date.fromisoformat(request["requestedStart"]) if request else None,
        requested_end=date.fromisoformat(request["requestedEnd"]) if request else None,
    )
    relevant = [item for item in partition.evidence if item.connection_id == scope.config["sourceConnectionId"]]
    advisories, actionable = partition_connection_errors(partition.unscopable_errors)
    inputs.require(not actionable and len(relevant) == 1 and relevant[0].clean,
                   "anchor-connection-not-clean")
    transactions = account.get("transactions")
    inputs.require(isinstance(transactions, list), "anchor-transactions-required")
    states = {}
    for transaction in transactions:
        state = inputs.raw_transaction_state(transaction, unit, scope.config["sourceAccountId"])
        inputs.require(state["status"] in {"posted", "pending"} and state["sourceId"] not in states,
                       "anchor-transaction-state-invalid")
        states[state["sourceId"]] = state
    posted_days = [state["sourceDay"] for state in states.values() if state["status"] == "posted"]
    balance_at = inputs.timestamp(account.get("balance-date") or account.get("balance_date") or account.get("balance_at"))
    inputs.require(balance_at <= observed, "anchor-balance-after-snapshot")
    return {
        "snapshotSha256": inputs.digest(path), "accountStateHash": content_hash(account),
        "transactionStateHash": content_hash(states), "states": states,
        "currency": unit, "balance": str(inputs.money(account.get("balance", account.get("current_balance")))),
        "balanceObservedAt": balance_at.isoformat(), "sourceObservedAt": observed.isoformat(),
        "observedPostedWatermark": max(posted_days) if posted_days else None,
        "requestEvidence": request,
        "unscopedAdvisories": list(advisories),
    }


@dataclass(frozen=True)
class Anchor:
    document: dict
    projected: frozenset
    removed: frozenset
    inventory: dict

    @property
    def anchor_hash(self):
        return self.document["anchorHash"]

    @property
    def states(self):
        return self.document["source"]["states"]


def _verify(scope, body, *, build=False):
    inputs.require(body.get("schemaVersion") == 1 and body.get("kind") == KIND
                   and body.get("rule") == RULE and body.get("scopeId") == scope.scope_id
                   and body.get("instanceId") == scope.config["instanceId"]
                   and body.get("baselineManifestHash") == scope.config["baselineManifestSha256"]
                   and body.get("policyHash") == scope.baseline.policy.policy_hash, "source-anchor-contract-mismatch")
    projected, removed, deleted_ids, history, _ = bootstrap._history(scope, body["applications"], body["repairHistory"])
    inventory = bootstrap._inventory(scope, body["inventory"], history["accountLedgerFingerprint"])
    source = source_anchor(scope, body["bootstrapSourceAnchor"])
    matches = [row for row in assertion_rows(scope, inventory["state"]) if row["id"] == body["assertionActivityId"]]
    inputs.require(len(matches) == 1 and projection.posted(matches[0])
                   and not matches[0].get("assetId"), "source-anchor-assertion-not-exact")
    suffix = matches[0]["idempotencyKey"].removeprefix(f"rebuild:assertion:{scope.canonical_id}:")
    date.fromisoformat(suffix)  # Identity only: never used as a posting cutoff.
    inputs.require(not deleted_ids & {row["id"] for row in inventory["state"]["rows"]},
                   "source-anchor-deleted-activity-present")
    inputs.require(projection.cash(inventory["state"]["rows"]) == inputs.money(source["balance"]),
                   "source-anchor-cash-prestate-mismatch")
    inputs.require(type(body.get("maximumTransitions")) is int and 0 < body["maximumTransitions"] <= 1000,
                   "source-anchor-transition-bound-invalid")
    derived = {
        "source": source, "assertionFingerprint": content_hash(matches[0]),
        "inventoryFinancialHash": content_hash(financial_state(inventory["state"])),
    }
    if build:
        body = {**body, **derived}
        body["anchorHash"] = content_hash(body)
    else:
        inputs.require(all(body.get(key) == value for key, value in derived.items())
                       and body.get("anchorHash") == content_hash({
                           key: value for key, value in body.items() if key != "anchorHash"
                       }), "source-anchor-state-or-seal-drift")
    return Anchor(body, frozenset(projected), frozenset(removed), inventory)


def build_contract(scope, *, source_anchor_spec, inventory, applications, repair_history,
                   assertion_activity_id, maximum_transitions):
    """Return an exact source/app cash-anchor binding, never choose a date cutoff."""
    return _verify(scope, {
        "schemaVersion": 1, "kind": KIND, "rule": RULE, "scopeId": scope.scope_id,
        "instanceId": scope.config["instanceId"], "baselineManifestHash": scope.config["baselineManifestSha256"],
        "policyHash": scope.baseline.policy.policy_hash, "bootstrapSourceAnchor": source_anchor_spec,
        "inventory": inventory, "applications": applications, "repairHistory": repair_history,
        "assertionActivityId": assertion_activity_id, "maximumTransitions": maximum_transitions,
    }, build=True).document


def load_contract(scope):
    spec = scope.config.get("bootstrapSourceAnchor")
    if spec is None:
        return None
    inputs.require(scope.config.get("bootstrap") is None, "source-anchor-cannot-use-date-window-bootstrap")
    return _verify(scope, inputs.document(inputs.bound_file(scope.root, spec)))


def _applied(connection, scope):
    return _rows(connection, """
        SELECT observation.observation_number,outbox.operation_document,observation.observed_activity,
               run.plan_document
        FROM finance.incremental_projection_observations observation
        JOIN finance.incremental_outbox outbox ON outbox.operation_id=observation.operation_id
        JOIN finance.incremental_runs run ON run.run_hash=outbox.run_hash
        WHERE observation.scope_id=%s AND observation.observation_kind='applied'
        ORDER BY observation.observation_number
    """, (scope.scope_id,))


def _restated_inventory(contract, applied):
    expected = {row["id"]: row for row in contract.inventory["state"]["rows"]}
    account = contract.inventory["state"]["account"]
    for record in applied:
        op, observed = record["operation_document"], record["observed_activity"]
        if op["kind"] == "create":
            inputs.require(observed["id"] not in expected, "anchor-journal-create-collision")
        else:
            old = expected.get(op["activityId"])
            inputs.require(old is not None and financial_state({
                "account": account, "rows": [old],
            }) == financial_state({"account": account, "rows": [op["before"]]}),
                "anchor-journal-prestate-drift")
        expected[observed["id"]] = observed
    return {**contract.inventory["state"], "rows": sorted(expected.values(), key=lambda row: row["id"])}


def verify_continuity(contract, app, applied):
    restated = _restated_inventory(contract, applied)
    inputs.require(content_hash(financial_state(app)) == content_hash(financial_state(restated)),
                   "source-anchor-app-financial-state-drift")
    assertion = next(row for row in app["rows"] if row["id"] == contract.document["assertionActivityId"])
    inputs.require(content_hash(assertion) == contract.document["assertionFingerprint"], "source-anchor-assertion-changed")
    expected_cash = inputs.money(contract.document["source"]["balance"]) + sum(
        (inputs.money(record["operation_document"]["delta"]) for record in applied), inputs.money(0)
    )
    inputs.require(projection.cash(app["rows"]) == expected_cash, "source-anchor-journal-cash-mismatch")
    return expected_cash


def _verify_checkpoint(connection, scope, contract, checkpoint, applied):
    inputs.require(checkpoint["source_state_hash"] == content_hash(checkpoint["source_state"]),
                   "source-anchor-checkpoint-state-hash-drift")
    prefix = [row for row in applied if row["observation_number"] <= checkpoint["projection_observation_cursor"]]
    expected_cursor = max((row["observation_number"] for row in prefix), default=0)
    financial_hash = content_hash(financial_state(_restated_inventory(contract, prefix)))
    inputs.require(expected_cursor == checkpoint["projection_observation_cursor"]
                   and financial_hash == checkpoint["app_financial_state_hash"],
                   "source-anchor-checkpoint-journal-drift")
    watermark = checkpoint["observed_posted_watermark"]
    watermark = watermark.isoformat() if watermark else None
    if checkpoint["run_hash"] is None:
        inputs.require(checkpoint["source_state"] == contract.states
                       and checkpoint["source_snapshot_hash"] == contract.document["source"]["snapshotSha256"]
                       and checkpoint["source_balance"] == inputs.money(contract.document["source"]["balance"])
                       and watermark == contract.document["source"]["observedPostedWatermark"]
                       and not prefix, "source-anchor-origin-checkpoint-drift")
        return
    rows = _rows(connection, "SELECT plan_document FROM finance.incremental_runs WHERE run_hash=%s AND scope_id=%s",
                 (checkpoint["run_hash"], scope.scope_id))
    inputs.require(len(rows) == 1, "source-anchor-checkpoint-plan-missing")
    plan = rows[0]["plan_document"]
    inputs.require(inputs.document(inputs.private(scope.root, f"incremental/plans/{checkpoint['run_hash']}.json")) == plan
                   and content_hash({key: value for key, value in plan.items() if key != "runHash"}) == checkpoint["run_hash"]
                   and plan["state"] in {"pending", "noop"}
                   and plan["sourceAnchorHash"] == contract.anchor_hash
                   and checkpoint["previous_checkpoint_hash"] == plan["sourceAnchor"]["checkpointHash"]
                   and checkpoint["source_state"] == plan["sourceAnchor"]["nextStates"]
                   and checkpoint["source_snapshot_hash"] == plan["snapshotHash"]
                   and checkpoint["receipt_hash"] == plan["receiptHash"]
                   and checkpoint["source_balance"] == inputs.money(plan["sourceBalance"])
                   and watermark == plan["sourceAnchor"]["nextWatermark"],
                   "source-anchor-checkpoint-plan-drift")
    delivered = {row["operation_document"]["acceptedEventId"] for row in prefix
                 if row["plan_document"]["runHash"] == checkpoint["run_hash"]}
    inputs.require(delivered == {op["acceptedEventId"] for op in plan["operations"]},
                   "source-anchor-checkpoint-has-undelivered-operations")


def _insert_checkpoint(connection, scope, contract, *, previous, run_hash, receipt_hash,
                       snapshot_hash, states, balance, watermark, cursor, financial_hash):
    material = {
        "anchorHash": contract.anchor_hash, "previous": previous, "runHash": run_hash,
        "receiptHash": receipt_hash, "snapshotHash": snapshot_hash, "states": states,
        "balance": str(balance), "watermark": watermark, "cursor": cursor, "financialHash": financial_hash,
    }
    checkpoint_hash = content_hash(material)
    connection.execute("""
        INSERT INTO finance.incremental_anchor_checkpoints(
            checkpoint_hash,anchor_hash,scope_id,previous_checkpoint_hash,run_hash,receipt_hash,
            source_snapshot_hash,source_state,source_state_hash,source_balance,observed_posted_watermark,
            projection_observation_cursor,app_financial_state_hash
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
        ON CONFLICT (checkpoint_hash) DO NOTHING
    """, (checkpoint_hash, contract.anchor_hash, scope.scope_id, previous, run_hash, receipt_hash,
          snapshot_hash, json.dumps(states), content_hash(states), balance, watermark, cursor, financial_hash))
    return checkpoint_hash


def _late_arrival_evidence(scope, contract, source, checkpoint, state):
    """Positive chronology/request proof, independent of amounts or residuals."""
    origin = inputs.timestamp(contract.document["source"]["sourceObservedAt"])
    if date.fromisoformat(state["sourceDay"]) <= origin.date():
        return {"rule": LATE_ARRIVAL_RULE, "eligible": False, "reason": "not-proved-after-origin-observation"}
    raw = source.raw_transactions[state["rawTransactionId"]]
    blocker = inputs.raw_projection_blocker(raw, state["status"])
    if blocker:
        return {"rule": LATE_ARRIVAL_RULE, "eligible": False, "reason": blocker}
    for observation in scope.baseline.observations:
        if (observation.source_family == "simplefin" and observation.canonical_account_id == scope.canonical_id
                and observation.provider_transaction_id == state["sourceId"]):
            proof = inputs.baseline_currency_proof(scope, observation)
            if proof and proof.get("rawProjectionBlocker"):
                return {"rule": LATE_ARRIVAL_RULE, "eligible": False, "reason": proof["rawProjectionBlocker"]}
    posted = raw.get("posted") if "posted" in raw else raw.get("posted_at")
    transacted = raw.get("transacted_at")
    if posted in (None, "", 0, "0") or transacted in (None, "", 0, "0"):
        return {"rule": LATE_ARRIVAL_RULE, "eligible": False, "reason": "explicit-source-times-required"}
    posted_at, transacted_at = inputs.timestamp(posted), inputs.timestamp(transacted)
    if not (origin.date() < posted_at.date() and origin.date() < transacted_at.date()
            and transacted_at <= posted_at <= source.observed_at):
        return {"rule": LATE_ARRIVAL_RULE, "eligible": False, "reason": "not-proved-after-origin-observation"}
    previous_hash = checkpoint["receipt_hash"]
    if previous_hash is None:
        return {"rule": LATE_ARRIVAL_RULE, "eligible": False, "reason": "prior-reconciled-request-required"}
    inputs.require_hash(previous_hash)
    previous = inputs.document(inputs.private(scope.root, f"automation/source-collection/runs/{previous_hash}.json"))
    inputs.require(previous.get("schemaVersion") == 1
                   and previous.get("kind") == "simplefin-source-collection-run"
                   and previous.get("status") == "collected"
                   and previous.get("receiptHash") == previous_hash
                   and content_hash({k: v for k, v in previous.items() if k != "receiptHash"}) == previous_hash,
                   "late-arrival-prior-receipt-invalid")
    manifest = previous["inputManifest"]
    inputs.require(manifest["snapshotSha256"] == checkpoint["source_snapshot_hash"]
                   and str(previous["protocolVersion"]) == str(source.receipt["protocolVersion"])
                   and inputs.timestamp(previous["observedAt"]) <= source.observed_at
                   and previous["inputSetHash"] == content_hash({
                       "inputManifest": manifest,
                       "expectedInventoryHash": previous["inventory"]["expectedInventoryHash"],
                   }),
                   "late-arrival-prior-source-binding-invalid")
    previous_start, previous_end = date.fromisoformat(manifest["requestedStart"]), date.fromisoformat(manifest["requestedEnd"])
    current_manifest = source.receipt["inputManifest"]
    current_start, current_end = date.fromisoformat(current_manifest["requestedStart"]), date.fromisoformat(current_manifest["requestedEnd"])
    day = date.fromisoformat(state["sourceDay"])
    if not (0 <= (previous_end - previous_start).days < 90
            and previous_start <= current_start and previous_end <= current_end
            and max(previous_start, current_start) <= day <= min(previous_end, current_end)):
        return {"rule": LATE_ARRIVAL_RULE, "eligible": False, "reason": "common-nonwidened-request-required"}
    inputs.require(inputs.raw_transaction_state(raw, source.currency, scope.config["sourceAccountId"]) == state,
                   "late-arrival-source-row-drift")
    return {
        "rule": LATE_ARRIVAL_RULE, "eligible": True,
        "originSnapshotSha256": contract.document["source"]["snapshotSha256"],
        "originObservedAt": origin.isoformat(), "postedAt": posted_at.isoformat(),
        "transactedAt": transacted_at.isoformat(), "rawTransactionHash": state["rawTransactionHash"],
        "previousReceiptHash": previous_hash, "previousSnapshotSha256": checkpoint["source_snapshot_hash"],
        "currentReceiptHash": source.receipt_hash, "currentSnapshotSha256": source.snapshot_hash,
        "previousRequest": {"from": previous_start.isoformat(), "through": previous_end.isoformat()},
        "currentRequest": {"from": current_start.isoformat(), "through": current_end.isoformat()},
        "requestCompletenessClaimed": False,
    }


def context(connection, scope, contract, source, app):
    connection.execute("""
        INSERT INTO finance.incremental_source_anchors(
            anchor_hash,scope_id,source_snapshot_hash,source_account_state_hash,source_transaction_state_hash,
            source_balance,currency_code,assertion_activity_id,anchor_document
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (anchor_hash) DO NOTHING
    """, (contract.anchor_hash, scope.scope_id, contract.document["source"]["snapshotSha256"],
          contract.document["source"]["accountStateHash"], contract.document["source"]["transactionStateHash"],
          contract.document["source"]["balance"], contract.document["source"]["currency"],
          contract.document["assertionActivityId"], json.dumps(contract.document)))
    stored = _rows(connection, "SELECT scope_id,anchor_document FROM finance.incremental_source_anchors WHERE anchor_hash=%s",
                   (contract.anchor_hash,))
    inputs.require(len(stored) == 1 and str(stored[0]["scope_id"]) == scope.scope_id
                   and stored[0]["anchor_document"] == contract.document, "source-anchor-database-binding-drift")
    applied = _applied(connection, scope)
    starting = verify_continuity(contract, app, applied)
    checkpoints = _rows(connection, """SELECT * FROM finance.incremental_anchor_checkpoints
        WHERE anchor_hash=%s ORDER BY checkpoint_number DESC LIMIT 1""", (contract.anchor_hash,))
    if not checkpoints:
        inputs.require(not applied, "source-anchor-origin-has-unexplained-journal")
        _insert_checkpoint(
            connection, scope, contract, previous=None, run_hash=None, receipt_hash=None,
            snapshot_hash=contract.document["source"]["snapshotSha256"], states=contract.states,
            balance=inputs.money(contract.document["source"]["balance"]),
            watermark=contract.document["source"]["observedPostedWatermark"], cursor=0,
            financial_hash=contract.document["inventoryFinancialHash"],
        )
        checkpoints = _rows(connection, """SELECT * FROM finance.incremental_anchor_checkpoints
            WHERE anchor_hash=%s ORDER BY checkpoint_number DESC LIMIT 1""", (contract.anchor_hash,))
    checkpoint = checkpoints[0]
    _verify_checkpoint(connection, scope, contract, checkpoint, applied)
    effective = dict(checkpoint["source_state"])
    for record in applied:
        if record["observation_number"] > checkpoint["projection_observation_cursor"]:
            proof = record["operation_document"].get("anchorTransition")
            inputs.require(proof is not None, "source-anchor-unclassified-applied-operation")
            effective[proof["sourceId"]] = proof["targetState"]
    watermark = checkpoint["observed_posted_watermark"]
    watermark = watermark.isoformat() if watermark else None
    changes = {}
    for source_id, current in source.states.items():
        prior = effective.get(source_id)
        delta = inputs.money(0)
        arrival = None
        if current["status"] == "pending":
            kind = "regression" if prior and prior["status"] == "posted" else "pending"
        elif current["status"] != "posted":
            kind = "unsupported"
        elif prior and prior["status"] == "posted":
            delta = inputs.money(current["amount"]) - inputs.money(prior["amount"])
            kind = "posted-change" if delta else "known-posted"
        elif prior and prior["status"] == "pending":
            kind, delta = "pending-settled", inputs.money(current["amount"])
        elif watermark is not None and current["sourceDay"] > watermark:
            kind, delta = "new-tail", inputs.money(current["amount"])
        else:
            kind = "historical"
            arrival = _late_arrival_evidence(scope, contract, source, checkpoint, current)
            if arrival["eligible"]:
                kind, delta = "late-posted", inputs.money(current["amount"])
        changes[source_id] = {
            "sourceId": source_id, "kind": kind, "priorState": prior, "targetState": current,
            "cashDelta": str(delta), "financialTransition": kind in {"posted-change", "pending-settled", "new-tail", "late-posted"},
            "anchorHash": contract.anchor_hash, "checkpointHash": checkpoint["checkpoint_hash"],
            "observedPostedWatermark": watermark, "rule": TRANSITION_RULE,
            "lateArrivalEvidence": arrival,
        }
    financial = [item for item in changes.values() if item["financialTransition"]]
    inputs.require(len(financial) <= contract.document["maximumTransitions"], "source-anchor-transition-count-exceeded")
    following = {**effective, **source.states}
    posted_days = [row["sourceDay"] for row in source.states.values() if row["status"] == "posted"]
    next_watermark = max(([watermark] if watermark else []) + posted_days, default=None)
    expected_balance = starting + sum((inputs.money(item["cashDelta"]) for item in financial), inputs.money(0))
    return {
        "transitionRule": TRANSITION_RULE,
        "anchorHash": contract.anchor_hash, "checkpointHash": checkpoint["checkpoint_hash"],
        "effectiveStates": effective, "changes": changes, "startingCash": str(starting),
        "checkpointStates": checkpoint["source_state"],
        "requiredSourceDelta": str(expected_balance - starting), "expectedSourceBalance": str(expected_balance),
        "nextStates": following, "nextWatermark": next_watermark,
        "financialSources": sorted(item["sourceId"] for item in financial),
        "invalidLifecycleSources": sorted(key for key, item in changes.items() if item["kind"] in {"regression", "unsupported"}),
    }


def project(scope, contract, context_value, event, members, accepted_id, revision, app,
            binding, basis, ordinary, verified_keys):
    ids = {item.provider_transaction_id for item in members}
    scoped = [item for item in members if item.source_family == "simplefin"
              and item.source_account_id == scope.config["sourceAccountId"]]
    single = len(ids) == 1 and len(scoped) == len(members)
    source_id = next(iter(ids)) if single else None
    change = context_value["changes"].get(source_id)
    if ordinary.get("activityId") == contract.document["assertionActivityId"]:
        return {"kind": "hold", "reason": "source-anchor-assertion-immutable"}
    if not single:
        return ordinary if ordinary["kind"] == "adopt" else {"kind": "hold", "reason": "anchor-history-detail-not-projected"}
    if change is None:
        return ordinary if ordinary["kind"] == "adopt" else {"kind": "hold", "reason": "anchor-source-not-observed"}
    if not change["financialTransition"]:
        if change["kind"] in {"regression", "unsupported"}:
            return {"kind": "hold", "reason": "source-anchor-lifecycle-requires-review"}
        if change["kind"] == "pending":
            return {"kind": "hold", "reason": "pending-or-ineligible-event"}
        if change["kind"] == "historical":
            return ordinary if ordinary["kind"] == "adopt" else {"kind": "hold", "reason": "anchor-new-historical-id-held"}
        if ordinary["kind"] == "adopt":
            return ordinary
        if ordinary.get("reason") == projection.UPDATE_CONTRACT_HOLD:
            return ordinary
        return {"kind": "hold", "reason": "anchor-date-only-change-not-projected"
                if ordinary["kind"] == "update" else "anchor-source-already-in-cash"}
    state = change["targetState"]
    if (event.status != "posted" or event.source_day.isoformat() != state["sourceDay"]
            or event.signed_amount != inputs.money(state["amount"]) or event.currency != state["currency"]):
        return {"kind": "hold", "reason": "source-anchor-resolved-state-mismatch"}
    if change["kind"] == "late-posted" and any(
        projection.posted(row) and _signed(row) == event.signed_amount and row["currency"] == event.currency
        and (str(row["date"])[:10] == state["sourceDay"]
             or inputs.timestamp(row["date"]).astimezone(scope.zone).date() == event.source_day)
        and normalized_description(str(row.get("comment") or "")) == normalized_description(state["description"])
        for row in app["rows"]
    ):
        return {"kind": "hold", "reason": "late-arrival-existing-economic-match"}
    if ordinary["kind"] == "hold" and ordinary.get("reason") != "baseline-posted-activity-missing":
        return ordinary
    if ordinary["kind"] == "adopt":
        return {"kind": "hold", "reason": "source-anchor-transition-already-represented"}
    if ordinary["kind"] == "create" or ordinary.get("reason") == "baseline-posted-activity-missing":
        if source_id in contract.removed or source_id in contract.projected:
            return {"kind": "hold", "reason": "source-anchor-prior-projection-blocks-create"}
    result = projection.operation(
        scope, event, accepted_id, revision, members, app, binding, basis, True,
        verified_keys=verified_keys, anchor_evidence=change,
    )
    if result["kind"] not in {"create", "update"} or inputs.money(result["delta"]) != inputs.money(change["cashDelta"]):
        return {"kind": "hold", "reason": "source-anchor-operation-delta-not-proved"}
    return result


def finish(connection, scope, contract, plan, source_snapshot_hash, receipt_hash, balance, app):
    context_value = plan["sourceAnchor"]
    applied = _applied(connection, scope)
    verify_continuity(contract, app, applied)
    current = _rows(connection, """SELECT * FROM finance.incremental_anchor_checkpoints
        WHERE anchor_hash=%s ORDER BY checkpoint_number DESC LIMIT 1""", (contract.anchor_hash,))[0]
    _verify_checkpoint(connection, scope, contract, current, applied)
    if current["run_hash"] == plan["runHash"]:
        return current["checkpoint_hash"]
    cursor = max((row["observation_number"] for row in applied), default=0)
    if (current["receipt_hash"] == receipt_hash and current["source_state_hash"] == content_hash(context_value["nextStates"])
            and current["source_balance"] == inputs.money(balance)
            and current["projection_observation_cursor"] == cursor):
        return current["checkpoint_hash"]
    inputs.require(current["checkpoint_hash"] == context_value["checkpointHash"], "source-anchor-checkpoint-advanced")
    inputs.require(projection.cash(app["rows"]) == inputs.money(balance), "source-anchor-final-cash-mismatch")
    return _insert_checkpoint(
        connection, scope, contract, previous=current["checkpoint_hash"], run_hash=plan["runHash"],
        receipt_hash=receipt_hash, snapshot_hash=source_snapshot_hash, states=context_value["nextStates"],
        balance=inputs.money(balance), watermark=context_value["nextWatermark"],
        cursor=cursor,
        financial_hash=content_hash(financial_state(app)),
    )
