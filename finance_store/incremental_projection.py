"""Exact cash projection guards; no matching heuristics or gap transactions."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time, timezone

from .domain import content_hash
from .incremental_inputs import Scope, currency, money, require, timestamp
from importers.rebuild.safety import instance_fingerprint
from importers.simplefin.application import _signed
from importers.simplefin.pipeline import INFLOW, OUTFLOW

UPDATE_CONTRACT_HOLD = "upstream-update-contract-unsupported"
ACTIVITY_WRITE_CONTRACT = "create-only-v1"


def posted(row: dict) -> bool:
    require(row.get("status") in {"POSTED", "PENDING", "DRAFT", "VOID"}, "app-status-not-explicit-or-supported")
    return row["status"] == "POSTED"


def ordinary_type(effect, account_type: str) -> str:
    return ("CREDIT" if account_type == "CREDIT_CARD" else "DEPOSIT") if effect >= 0 else "WITHDRAWAL"


def app_state(scope: Scope, client) -> dict:
    require(client.base == scope.config["origin"], "app-origin-mismatch")
    require(any(cookie.name == "wf_session" for cookie in client._jar), "authenticated-session-required")
    require(instance_fingerprint(client, client.base) == scope.config["instanceId"], "app-instance-mismatch")
    accounts = client.get("/accounts?includeArchived=true")
    require(isinstance(accounts, list), "app-account-inventory-invalid")
    selected = [row for row in accounts if row.get("id") == scope.account_id]
    require(len(selected) == 1, "app-account-not-unique")
    account = selected[0]
    require(account.get("isActive") is True and account.get("isArchived") is False
            and account.get("accountType") == scope.fact.kind
            and account.get("trackingMode") == "TRANSACTIONS", "app-account-not-active-cash-card")
    require(account.get("name") == scope.fact.display_name, "canonical-display-name-drift")
    require(currency(account.get("currency")) == scope.baseline_currency, "app-baseline-currency-mismatch")
    settings = client.get("/settings")
    require(isinstance(settings, dict) and settings.get("timezone") == scope.config["timezone"],
            "app-timezone-mismatch")
    rows = [deepcopy(row) for row in client.iter_activities()
            if row.get("accountId") == scope.account_id]
    require(len({row["id"] for row in rows}) == len(rows), "app-activity-id-duplicated")
    for row in rows:
        require(not row.get("assetId") and row.get("activityType") in INFLOW | OUTFLOW,
                "app-cash-ledger-unsupported")
        require(currency(row.get("currency")) == account["currency"], "app-ledger-currency-mismatch")
        money(row.get("amount"))
        posted(row)
        timestamp(row["date"])
    return {"account": deepcopy(account), "timezone": settings["timezone"],
            "rows": sorted(rows, key=lambda item: item["id"])}


def cash(rows: list[dict]):
    return sum((_signed(row) for row in rows if posted(row)), money(0))


def lineage_keys(scope: Scope, observations: list) -> set[str]:
    keys = set()
    for observation in observations:
        source_id = observation.provider_transaction_id or ""
        if not source_id:
            continue
        keys.add(source_id)
        parts = source_id.split(":", 2)
        if observation.source_family == "simplefin":
            require(observation.source_account_id == scope.config["sourceAccountId"],
                    "event-has-other-source-account")
            if len(parts) == 3 and parts[0] == "simplefin":
                keys.add(f"simplefin:{scope.account_id}:{parts[2]}")
        elif observation.source_family in {"ofx", "qfx", "extract"}:
            if len(parts) == 3 and parts[0] == "extract":
                keys.add(f"extract:{scope.account_id}:{parts[2]}")
        elif observation.source_family == "monarch":
            keys.add(source_id if source_id.startswith("monarch:") else f"monarch:{source_id}")
    return keys


def economics_match(row: dict, event) -> bool:
    return (
        posted(row) and _signed(row) == event.signed_amount
        and row["currency"] == event.currency
        and str(row["date"])[:10] == event.source_day.isoformat()
    )


def unsupported_link(row: dict) -> bool:
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        import json
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return True
    return (
        any(row.get(key) for key in ("sourceGroupId", "splitGroupId", "splitParentId", "parentActivityId"))
        or row.get("activityType") in {"TRANSFER_IN", "TRANSFER_OUT"}
        or row.get("subtype") not in {None, "", "deposit", "withdrawal"}
        or isinstance(metadata, dict) and any(metadata.get(key) for key in ("split", "transfer", "correction", "flow"))
    )


def operation(scope: Scope, event, accepted_id: str, revision: int, observations: list,
              state: dict, binding: dict | None, prior: dict | None,
              baseline_posted: bool, *, verified_keys=(), creation_evidence=None, anchor_evidence=None) -> dict:
    key = f"finance:accepted:{accepted_id}"
    representation = {name: (prior or {}).get(name) for name in
                      ("representation_kind", "base_source_amount", "anchor_hash", "anchor_checkpoint_hash", "anchor_source_id")}
    representation["representation_kind"] = representation["representation_kind"] or "full"
    representation["base_source_amount"] = representation["base_source_amount"] or "0"
    base_amount = money(representation["base_source_amount"])
    if representation["representation_kind"] == "source-delta":
        key = f"finance:source-delta:{accepted_id}:{representation['anchor_hash']}"
    projected_amount = event.signed_amount - base_amount
    if binding and binding.get("inactive"):
        return {"kind": "hold", "reason": "inactive-projection-binding"}
    aliases = lineage_keys(scope, observations) | {key} | set(verified_keys)
    target_hashes = set((binding or {}).get("target_hashes", ()))
    matched = [row for row in state["rows"]
               if row.get("idempotencyKey") in aliases
               or binding and row["id"] == binding.get("activity_id")
               or content_hash(row["id"]) in target_hashes]
    if binding and (
        binding.get("activity_id") and not any(row["id"] == binding["activity_id"] for row in matched)
        or any(not any(content_hash(row["id"]) == target for row in matched) for target in target_hashes)
    ):
        return {"kind": "hold", "reason": "bound-activity-missing"}
    if len(matched) > 1:
        return {"kind": "hold", "reason": "multiple-legacy-activities"}
    if event.status != "posted" or not event.trusted:
        return {"kind": "hold", "reason": "pending-or-ineligible-event"}
    if any(observation.source_group_id or any(
        name in dict(observation.attributes) for name in
        ("correction_of", "counterpart_id", "pending_of", "provider_error_of", "reversal_of")
    ) for observation in observations):
        return {"kind": "hold", "reason": "linked-event-unsupported"}
    before = matched[0] if matched else None
    if before:
        if unsupported_link(before):
            return {"kind": "hold", "reason": "linked-activity-unsupported"}
        # A delivered/adopted source revision, never the latest accepted intent,
        # defines the meaning of an existing timestamp (including legacy UTC dates).
        correct = (posted(before) and _signed(before) == projected_amount
                   and before["currency"] == event.currency
                   and str(before["date"])[:10] == event.source_day.isoformat())
        if prior:
            correct = (posted(before) and _signed(before) == projected_amount
                       and before["currency"] == event.currency
                       and prior["source_day"] == event.source_day.isoformat()
                       and timestamp(before["date"]) == timestamp(prior["observed_activity"]["date"]))
        if correct:
            return {"kind": "adopt", "activityId": before["id"], "observed": before, "representation": representation}
        # Both pinned 3.7 update routes clear an omitted comment and mark a
        # record user-modified. Neither switching routes nor resending an old
        # comment makes this lossless. No automatic update capability is enabled.
        return {"kind": "hold", "reason": UPDATE_CONTRACT_HOLD, "activityId": before["id"]}
    else:
        if baseline_posted and creation_evidence is None and anchor_evidence is None:
            return {"kind": "hold", "reason": "baseline-posted-activity-missing"}
        if anchor_evidence is not None:
            require(anchor_evidence["financialTransition"]
                    and anchor_evidence["kind"] in {"new-tail", "pending-settled", "posted-change", "late-posted"}
                    and {item.provider_transaction_id for item in observations} == {anchor_evidence["sourceId"]}
                    and money(anchor_evidence["targetState"]["amount"]) == event.signed_amount
                    and anchor_evidence["targetState"]["currency"] == event.currency
                    and anchor_evidence["targetState"]["sourceDay"] == event.source_day.isoformat(),
                    "source-anchor-creation-evidence-mismatch")
            if anchor_evidence["kind"] == "late-posted":
                require(anchor_evidence.get("lateArrivalEvidence", {}).get("eligible") is True
                        and anchor_evidence["lateArrivalEvidence"].get("rule") == "explicit-post-anchor-arrival-v1",
                        "late-arrival-creation-evidence-required")
            if anchor_evidence["kind"] == "posted-change":
                base_amount = money(anchor_evidence["priorState"]["amount"])
                representation = {
                    "representation_kind": "source-delta", "base_source_amount": str(base_amount),
                    "anchor_hash": anchor_evidence["anchorHash"],
                    "anchor_checkpoint_hash": anchor_evidence["checkpointHash"],
                    "anchor_source_id": anchor_evidence["sourceId"],
                }
                projected_amount = event.signed_amount - base_amount
                key = f"finance:source-delta:{accepted_id}:{anchor_evidence['anchorHash']}"
        if creation_evidence is not None:
            from .incremental_inputs import economic_key
            selected = next(item for item in observations if item.observation_id == event.selected_observation_id)
            reviewed = creation_evidence["candidate"]
            require(reviewed["sourceId"] == selected.provider_transaction_id
                    and reviewed["observationId"] == selected.observation_id
                    and reviewed["economicHash"] == economic_key(selected)
                    and reviewed["sourceDay"] == event.source_day.isoformat()
                    and money(reviewed["signedAmount"]) == event.signed_amount
                    and reviewed["currency"] == event.currency, "bootstrap-creation-evidence-mismatch")
        payload = {
            "accountId": scope.account_id,
            "activityType": ordinary_type(projected_amount, scope.fact.kind),
            "activityDate": datetime.combine(event.source_day, time(12), scope.zone).astimezone(timezone.utc).isoformat(),
            "amount": float(abs(projected_amount)), "currency": event.currency,
            "status": "POSTED",
            "comment": ("Source amount delta: " if representation["representation_kind"] == "source-delta" else "") + event.description,
            "idempotencyKey": key,
        }
        kind, delta = "create", projected_amount
    require(money(payload["amount"]) == abs(projected_amount), "amount-not-exact-in-activity-payload")
    return {
        "kind": kind, "acceptedEventId": accepted_id, "revisionNumber": revision,
        "activityId": before["id"] if before else None,
        "key": before["idempotencyKey"] if before else payload["idempotencyKey"],
        "payload": payload, "payloadHash": content_hash(payload), "before": before,
        "delta": str(delta), "assignments": [], "bootstrapProof": creation_evidence,
        "representation": representation, "anchorTransition": anchor_evidence,
    }


def matches_post(row: dict, op: dict) -> bool:
    payload = op["payload"]
    for key, expected in payload.items():
        if key in {"createdAt", "updatedAt"}:
            continue
        actual = row.get("date" if key == "activityDate" else key)
        if key == "amount":
            if money(actual) != money(expected):
                return False
        elif key == "activityDate":
            if timestamp(actual) != timestamp(expected):
                return False
        elif actual != expected:
            return False
    if op["before"]:
        for key, value in op["before"].items():
            if key not in {"date", "amount", "activityType", "updatedAt"} and row.get(key) != value:
                return False
    return True
