"""Read-only, evidence-backed bootstrap of unrepresented baseline SF versions.

Presence in a source baseline is neither proof of delivery nor permission to
re-create a missing activity. This contract reviews a finite candidate set
without consulting a balance or searching for a fitting subset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import os

from . import incremental_inputs as inputs
from . import incremental_projection as projection
from .domain import content_hash, require_hash
from importers.audit.forensic import _receipt_matches_plan
from importers.rebuild import bounded_promotion as promotion
from importers.rebuild import repair_lineage
from importers.rebuild.safety import plan_fingerprint
from importers.simplefin.application import ledger_fingerprint

CONTRACT_KIND = "incremental-reviewed-bootstrap"
INVENTORY_KIND = "incremental-bootstrap-inventory"
REPAIR_HISTORY_KIND = "incremental-bootstrap-repair-history"
CONTRACT_KEYS = frozenset({
    "schemaVersion", "kind", "scopeId", "instanceId", "baselineManifestHash", "policyHash",
    "applications", "repairHistory", "inventory", "sourceDateWindow", "maximumCandidates",
    "candidates", "candidateSetHash", "bootstrapHash",
})


def capture_inventory(scope, client, *, now=None) -> dict:
    """Return an authenticated inventory for separate operator review/storage."""
    state = projection.app_state(scope, client)
    return {
        "schemaVersion": 1, "kind": INVENTORY_KIND,
        "scopeId": scope.scope_id, "instanceId": scope.config["instanceId"],
        "capturedAt": inputs.timestamp(now or datetime.now(timezone.utc)).isoformat(),
        "state": state, "stateHash": content_hash(state),
        "accountLedgerFingerprint": ledger_fingerprint(state["rows"], {scope.account_id}),
    }


def export_repair_history(root, executions: list[dict], *, evidence_key=None, operator_key=None) -> dict:
    """Verify completed repair artifacts, then return a portable signed export.

    The existing verifier reads backup/receipt evidence, not the running app.
    The caller chooses where to store the returned document. Nothing here
    archives a slot, mutates runtime data, activates a marker, or writes a file.
    """
    root = inputs.private(root, ".")
    evidence_key, operator_key = repair_lineage.keys(evidence_key, operator_key)
    inputs.require(bool(executions), "bootstrap-completed-repair-history-required")
    previous = None
    account_id = None
    target = None
    steps, deleted = [], []
    applications = set()
    seen_ids, seen_aliases = set(), set()
    for specification in executions:
        path = inputs.bound_file(root, specification["execution"])
        context = inputs.private(root, specification.get("evidenceRoot", "."))
        document = inputs.document(path)
        checked = repair_lineage.completed_execution(
            document, root=context, evidence_key=evidence_key, operator_key=operator_key,
        )
        execution, plan = checked["execution"], checked["plan"]
        current_account = plan["scope"]["ledgerAccountId"]
        account_id = account_id or current_account
        target = target or execution["target"]
        inputs.require(current_account == account_id and execution["target"] == target
                       and execution.get("previousExecutionHash") == previous,
                       "bootstrap-repair-history-gap-or-target-drift")
        source_pair = (
            plan["evidence"]["sourceApplicationPlanSha256"],
            plan["evidence"]["sourceApplicationReceiptSha256"],
        )
        applications.add(source_pair)
        for operation in plan["operations"]["repairs"]:
            payload = operation["sourceRollbackPayload"]
            identity = payload["idempotencyKey"]
            activity_id = operation["sourceActivityId"]
            inputs.require(payload["accountId"] == account_id and activity_id not in seen_ids
                           and identity not in seen_aliases, "bootstrap-repair-history-repeats-deletion")
            seen_ids.add(activity_id)
            seen_aliases.add(identity)
            deleted.append({"activityId": activity_id, "sourceIdentity": identity})
        steps.append({
            "executionHash": document["documentHash"], "previousExecutionHash": previous,
            "repairPlanHash": plan["planHash"],
            "accountLedgerFingerprint": plan["expected"]["accountLedgerFingerprint"],
        })
        previous = document["documentHash"]
    body = {
        "schemaVersion": 1, "kind": REPAIR_HISTORY_KIND, "accountId": account_id,
        "originalTargetHash": plan_fingerprint(target),
        "sourceApplications": [{"planSha256": plan, "receiptSha256": receipt}
                               for plan, receipt in sorted(applications)],
        "steps": steps, "headExecutionHash": previous,
        "accountLedgerFingerprint": steps[-1]["accountLedgerFingerprint"],
        "deleted": sorted(deleted, key=lambda item: item["activityId"]),
    }
    return promotion.seal(body, evidence_key)


def _source_identity(scope, activity: dict) -> str | None:
    """Only exact scoped SF source identities, never descriptions or amounts."""
    if activity.get("accountId") != scope.account_id:
        return None
    key = activity.get("idempotencyKey")
    prefix = f"simplefin:{scope.account_id}:"
    if not isinstance(key, str) or not key.startswith(prefix) or not key[len(prefix):]:
        return None
    return f"simplefin:{scope.config['sourceAccountId']}:{key[len(prefix):]}"


def _history(scope, specifications: list[dict], repair_spec: dict):
    inputs.require(isinstance(specifications, list) and specifications, "bootstrap-source-application-history-required")
    projected, aliases, pairs = set(), set(), set()
    for specification in specifications:
        plan_path = inputs.bound_file(scope.root, specification["plan"])
        receipt_path = inputs.bound_file(scope.root, specification["receipt"])
        plan, receipt = inputs.document(plan_path), inputs.document(receipt_path)
        version = plan.get("schemaVersion")
        inputs.require(version in {1, 3}
                       and plan.get("mode") in {"staging-apply-plan", "production-promotion-plan"}
                       and plan.get("planFingerprint") == plan_fingerprint({
                           key: value for key, value in plan.items() if key != "planFingerprint"
                       }), "bootstrap-application-plan-seal-invalid")
        for key in ("planFingerprint", "intentFingerprint", "environmentFingerprint",
                    "ledgerFingerprint", "expectedPostLedgerFingerprint"):
            require_hash(plan[key])
        if version == 1:
            inputs.require(all(isinstance(plan.get(key), expected) for key, expected in {
                "portableIntent": dict, "ledgerAccountIds": list, "links": list,
                "manual": list, "monitors": list, "impact": dict, "spendingWindow": dict,
            }.items()), "bootstrap-historical-application-shape-invalid")
        inputs.require(receipt.get("schemaVersion") == {1: 3, 3: 5}[version]
                       and receipt.get("status") == "applied"
                       and _receipt_matches_plan(
                           {"value": plan, "sha256": inputs.digest(plan_path)},
                           {"value": receipt, "sha256": inputs.digest(receipt_path)},
                       ), "bootstrap-application-receipt-not-exact-applied")
        inputs.require(inputs.timestamp(receipt["generatedAt"]) >= inputs.timestamp(plan["generatedAt"]),
                       "bootstrap-application-chronology-invalid")
        assertions = [row for row in plan.get("assertions", []) if row.get("accountId") == scope.account_id]
        inputs.require(len(assertions) == 1 and assertions[0].get("canonicalAccountId") == scope.canonical_id,
                       "bootstrap-application-account-mismatch")
        operations = plan["operations"]
        inputs.require(not operations.get("deleteIds"), "bootstrap-application-unidentified-deletions")
        for name in ("creates", "updates", "metadataFinalizations"):
            if name == "metadataFinalizations" and version == 1 and name not in operations:
                continue
            inputs.require(isinstance(operations.get(name), list), "bootstrap-application-operations-invalid")
            for entry in operations[name]:
                row = entry.get("payload", {}) if name == "metadataFinalizations" else entry
                identity = _source_identity(scope, row)
                if identity is not None:
                    projected.add(identity)
                    aliases.add(row["idempotencyKey"])
        pairs.add((inputs.digest(plan_path), inputs.digest(receipt_path)))

    path = inputs.bound_file(scope.root, repair_spec["file"])
    key_name = repair_spec.get("verificationKeyEnv", promotion.EVIDENCE_KEY_ENV)
    inputs.require(isinstance(key_name, str) and key_name, "bootstrap-repair-verification-key-required")
    try:
        key = bytes.fromhex(os.environ.get(key_name, ""))
    except ValueError as error:
        raise inputs.IncrementalHold("bootstrap-repair-verification-key-invalid") from error
    inputs.require(len(key) >= 32, "bootstrap-repair-verification-key-required")
    signed = inputs.document(path)
    history = promotion.unseal(signed, key, REPAIR_HISTORY_KIND)
    inputs.require(history.get("schemaVersion") == 1 and history.get("accountId") == scope.account_id,
                   "bootstrap-repair-account-mismatch")
    require_hash(history["originalTargetHash"])
    head = require_hash(repair_spec["headExecutionHash"])
    inputs.require(history.get("headExecutionHash") == head, "bootstrap-repair-head-mismatch")
    required_pairs = {(item["planSha256"], item["receiptSha256"]) for item in history["sourceApplications"]}
    inputs.require(required_pairs and required_pairs <= pairs, "bootstrap-repair-source-history-mismatch")
    steps = history.get("steps")
    inputs.require(isinstance(steps, list) and steps, "bootstrap-completed-repair-history-required")
    previous, executions = None, set()
    for step in steps:
        execution = require_hash(step["executionHash"])
        require_hash(step["repairPlanHash"])
        require_hash(step["accountLedgerFingerprint"])
        inputs.require(step["previousExecutionHash"] == previous and execution not in executions,
                       "bootstrap-repair-history-gap")
        previous = execution
        executions.add(execution)
    inputs.require(previous == head and history["accountLedgerFingerprint"] == steps[-1]["accountLedgerFingerprint"],
                   "bootstrap-repair-history-head-invalid")
    removed, ids = set(), set()
    for item in history["deleted"]:
        inputs.require(item["sourceIdentity"] in aliases and item["activityId"] not in ids
                       and item["sourceIdentity"] not in removed, "bootstrap-repair-alias-not-source-proven")
        ids.add(item["activityId"])
        removed.add(item["sourceIdentity"])
    removed_sources = {_source_identity(scope, {"accountId": scope.account_id, "idempotencyKey": alias})
                       for alias in removed}
    return projected, removed_sources, ids, history, inputs.digest(path)


def _inventory(scope, specification: dict, expected_ledger: str):
    path = inputs.bound_file(scope.root, specification)
    inventory = inputs.document(path)
    inputs.require(inventory.get("schemaVersion") == 1 and inventory.get("kind") == INVENTORY_KIND
                   and inventory.get("scopeId") == scope.scope_id
                   and inventory.get("instanceId") == scope.config["instanceId"],
                   "bootstrap-inventory-scope-mismatch")
    inputs.timestamp(inventory["capturedAt"])
    state = inventory["state"]
    inputs.require(set(state) == {"account", "timezone", "rows"}
                   and inventory["stateHash"] == content_hash(state)
                   and state["account"]["id"] == scope.account_id
                   and state["account"].get("isArchived") is False
                   and state["account"].get("isActive") is True
                   and state["timezone"] == scope.config["timezone"], "bootstrap-inventory-invalid")
    rows = state["rows"]
    inputs.require(isinstance(rows, list) and len({row["id"] for row in rows}) == len(rows)
                   and rows == sorted(rows, key=lambda item: item["id"]), "bootstrap-inventory-rows-invalid")
    for row in rows:
        inputs.require(row["accountId"] == scope.account_id and row["currency"] == scope.baseline_currency,
                       "bootstrap-inventory-account-or-currency-mismatch")
        projection.posted(row)
        inputs.timestamp(row["date"])
        inputs.money(row["amount"])
    fingerprint = ledger_fingerprint(rows, {scope.account_id})
    inputs.require(fingerprint == inventory["accountLedgerFingerprint"] == expected_ledger,
                   "bootstrap-inventory-not-repaired-poststate")
    return inventory


def _candidate(scope, event, selected) -> dict:
    return {
        "sourceId": selected.provider_transaction_id,
        "observationId": selected.observation_id, "economicHash": inputs.economic_key(selected),
        "canonicalEventId": event.canonical_event_id,
        "sourceDay": event.source_day.isoformat(), "signedAmount": str(event.signed_amount),
        "currency": event.currency, "descriptionHash": content_hash(event.description),
        "currencyProof": inputs.baseline_currency_proof(scope, selected),
    }


def _candidates(scope, window, projected, inventory):
    start, end = date.fromisoformat(window["from"]), date.fromisoformat(window["through"])
    inputs.require(0 <= (end - start).days < 90, "bootstrap-window-must-be-bounded")
    observations = {item.observation_id: item for item in scope.baseline.observations}
    result = []
    for event in scope.baseline.canonical_events:
        if not start <= event.source_day <= end or event.status != "posted" or not event.trusted:
            continue
        members = [observations[key] for key in event.member_observation_ids]
        if not members or any(item.canonical_account_id != scope.canonical_id
                              or item.source_family != "simplefin"
                              or item.source_account_id != scope.config["sourceAccountId"] for item in members):
            continue
        ids = {item.provider_transaction_id for item in members}
        if len(ids) != 1 or ids & projected:
            continue
        selected = observations[event.selected_observation_id]
        keys = projection.lineage_keys(scope, members)
        if event.canonical_event_id in scope.published_canonical_ids:
            keys.add(f"canonical:{scope.canonical_id}:{event.canonical_event_id}")
        if any(row.get("idempotencyKey") in keys for row in inventory["state"]["rows"]):
            continue
        result.append(_candidate(scope, event, selected))
    return sorted(result, key=lambda row: row["sourceId"])


@dataclass(frozen=True)
class Bootstrap:
    document: dict
    projected: frozenset[str]
    removed: frozenset[str]
    removed_activity_ids: frozenset[str]
    inventory: dict

    def missing_reason(self, source_ids) -> str | None:
        if set(source_ids) & self.removed:
            return "bootstrap-known-removed-alias"
        if set(source_ids) & self.projected:
            return "bootstrap-previously-projected-source-missing"
        return None


def _verify(scope, body, *, review_draft=False):
    keys = CONTRACT_KEYS - {"candidates", "candidateSetHash", "bootstrapHash"} if review_draft else CONTRACT_KEYS
    inputs.require(set(body) == keys, "bootstrap-contract-fields-invalid")
    inputs.require(body.get("schemaVersion") == 1 and body.get("kind") == CONTRACT_KIND
                   and body.get("scopeId") == scope.scope_id
                   and body.get("instanceId") == scope.config["instanceId"]
                   and body.get("baselineManifestHash") == scope.config["baselineManifestSha256"]
                   and body.get("policyHash") == scope.baseline.policy.policy_hash,
                   "bootstrap-scope-or-baseline-mismatch")
    projected, removed, ids, history, history_hash = _history(scope, body["applications"], body["repairHistory"])
    inputs.require(body["repairHistory"]["file"]["sha256"] == history_hash, "bootstrap-history-drift")
    inventory = _inventory(scope, body["inventory"], history["accountLedgerFingerprint"])
    inputs.require(not any(str(row.get("idempotencyKey") or "").startswith(
        f"rebuild:assertion:{scope.canonical_id}:"
    ) for row in inventory["state"]["rows"]), "source-balance-anchor-contract-required")
    inputs.require(not ids & {row["id"] for row in inventory["state"]["rows"]}, "bootstrap-removed-activity-present")
    candidates = _candidates(scope, body["sourceDateWindow"], projected, inventory)
    cap = body["maximumCandidates"]
    inputs.require(type(cap) is int and 0 < cap <= 1000 and len(candidates) <= cap, "bootstrap-candidate-bound-exceeded")
    if review_draft:
        body = {**body, "candidates": candidates, "candidateSetHash": content_hash(candidates)}
        body["bootstrapHash"] = content_hash(body)
    else:
        inputs.require(body.get("bootstrapHash") == content_hash({key: value for key, value in body.items() if key != "bootstrapHash"})
                       and body.get("candidates") == candidates
                       and body.get("candidateSetHash") == content_hash(candidates),
                       "bootstrap-candidate-set-or-seal-mismatch")
    return Bootstrap(body, frozenset(projected), frozenset(removed), frozenset(ids), inventory)


def build_contract(scope, *, applications, repair_history, inventory, source_date_window,
                   maximum_candidates) -> dict:
    """Return all review candidates; never selects a subset using a cash balance."""
    body = {
        "schemaVersion": 1, "kind": CONTRACT_KIND, "scopeId": scope.scope_id,
        "instanceId": scope.config["instanceId"], "baselineManifestHash": scope.config["baselineManifestSha256"],
        "policyHash": scope.baseline.policy.policy_hash, "applications": applications,
        "repairHistory": repair_history, "inventory": inventory,
        "sourceDateWindow": source_date_window, "maximumCandidates": maximum_candidates,
    }
    return _verify(scope, body, review_draft=True).document


def load_contract(scope) -> Bootstrap | None:
    specification = scope.config.get("bootstrap")
    if specification is None:
        return None
    return _verify(scope, inputs.document(inputs.bound_file(scope.root, specification)))


def verify_live_inventory(contract: Bootstrap, actual: dict, applied: list[dict]) -> str:
    """Restate only journal-proven worker changes, never reinterpret user edits."""
    expected = {row["id"]: row for row in contract.inventory["state"]["rows"]}
    for entry in applied:
        operation, observed = entry["operation_document"], entry["observed_activity"]
        if operation["kind"] == "create":
            inputs.require(observed["id"] not in expected, "bootstrap-delivery-history-collision")
        else:
            inputs.require(content_hash(expected.get(operation["activityId"])) == content_hash(operation["before"]),
                           "bootstrap-delivery-history-prestate-drift")
        expected[observed["id"]] = observed
    restated = {**contract.inventory["state"], "rows": sorted(expected.values(), key=lambda row: row["id"])}
    inputs.require(content_hash(actual) == content_hash(restated), "bootstrap-live-inventory-drift")
    return content_hash(restated)


def permit(scope, contract: Bootstrap, event, members, seen, actual, applied) -> dict:
    ids = {item.provider_transaction_id for item in members}
    reason = contract.missing_reason(ids)
    inputs.require(reason is None, reason or "bootstrap-history-conflict")
    selected = next(item for item in members if item.observation_id == event.selected_observation_id)
    inputs.require(len(ids) == 1 and selected.source_family == "simplefin"
                   and selected.source_account_id == scope.config["sourceAccountId"]
                   and ids <= seen, "bootstrap-source-not-currently-observed")
    expected = next((row for row in contract.document["candidates"] if row["sourceId"] == selected.provider_transaction_id), None)
    inputs.require(expected is not None, "bootstrap-source-not-in-reviewed-window-set")
    inputs.require(selected.observation_id == expected["observationId"]
                   and inputs.economic_key(selected) == expected["economicHash"],
                   "bootstrap-source-version-changed")
    inputs.require(_candidate(scope, event, selected) == expected, "bootstrap-source-version-changed")
    inventory_hash = verify_live_inventory(contract, actual, applied)
    # This is a hold, not fuzzy adoption: an opaque existing identity with the
    # same economic tuple cannot be assumed unrelated just to create a new row.
    for row in contract.inventory["state"]["rows"]:
        day = inputs.timestamp(row["date"])
        if (row["currency"] == event.currency and projection._signed(row) == event.signed_amount
                and event.source_day in {day.date(), day.astimezone(scope.zone).date()}):
            raise inputs.IncrementalHold("bootstrap-existing-economic-collision")
    return {
        "bootstrapHash": contract.document["bootstrapHash"],
        "candidateSetHash": contract.document["candidateSetHash"], "candidate": expected,
        "inventoryStateHash": inventory_hash, "repairHeadExecutionHash": contract.document["repairHistory"]["headExecutionHash"],
    }
