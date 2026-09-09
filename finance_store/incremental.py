"""Scoped, journaled cash projection. PostgreSQL and HTTP are never one commit."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date, datetime, timezone

from . import incremental_inputs as inputs
from . import incremental_projection as projection
from . import incremental_bootstrap as bootstrap
from . import incremental_anchor as source_anchor
from .domain import content_hash, stable_id
from .identity import observations_from_transaction_rows, resolve_identity, source_account_scope
from .identity_postgres import (
    _lock_identity_writer, _uuid, accepted_current_generation_hash,
    persist_accepted_identity_resolution, persist_application_projection_bindings,
)
from .postgres import GLOBAL_WRITER_LOCK
from importers.monarch.mutation_guard import (
    current_incremental_release, incremental_writer_context, require_wealthfolio_mutations,
)
from importers.rebuild.cutover import download_backup, verify_backup_file
from importers.rebuild.projector import require_unique_backup

PROCESS_RELEASE = current_incremental_release()


def _json(value):
    return json.dumps(value, sort_keys=True)


def _rows(connection, sql, params=()):
    from psycopg.rows import dict_row
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


@contextmanager
def worker(connection):
    """No expiring lease: a live backend owns the gate across journal commits.

    A lost backend cannot hand its uncertain HTTP attempt to a retrying writer:
    durable prepared/uncertain attempts may only be observed, never resent.
    """
    inputs.require(connection.autocommit, "incremental-session-requires-autocommit-connection")
    row = connection.execute(
        "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (GLOBAL_WRITER_LOCK,)
    ).fetchone()
    inputs.require(bool(row[0]), "incremental-writer-busy")
    try:
        with connection.transaction():
            _lock_identity_writer(connection)
        yield
    finally:
        if not connection.closed:
            connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (GLOBAL_WRITER_LOCK,))


def require_writer(scope, client):
    inputs.require(PROCESS_RELEASE == current_incremental_release(), "running-incremental-release-changed")
    actual_instance = projection.instance_fingerprint(client, client.base)
    inputs.require(actual_instance == scope.config["instanceId"], "app-instance-mismatch")
    arguments = {
        "base_url": client.base, "data_dir": scope.root, "scope_id": scope.scope_id,
        "configuration_hash": scope.config_hash, "instance_id": actual_instance,
        "environment_id": scope.config["writerEnvironmentId"],
    }
    with incremental_writer_context(**arguments):
        require_wealthfolio_mutations("POST", "/activities/bulk", base_url=client.base, data_dir=scope.root)
    return arguments


def _event(connection, run_hash, state, evidence):
    event_id = stable_id("incremental_run_event", run_hash, state, content_hash(evidence))
    connection.execute(
        "INSERT INTO finance.incremental_run_events(event_id, run_hash, state, evidence) "
        "VALUES (%s,%s,%s,%s::jsonb) ON CONFLICT (event_id) DO NOTHING",
        (event_id, run_hash, state, _json(evidence)),
    )


def _attempt(connection, operation_id, state, evidence):
    event_id = stable_id("incremental_attempt", operation_id, state, content_hash(evidence))
    connection.execute(
        "INSERT INTO finance.incremental_attempts(event_id, operation_id, state, evidence) "
        "VALUES (%s,%s,%s,%s::jsonb) ON CONFLICT (event_id) DO NOTHING",
        (event_id, operation_id, state, _json(evidence)),
    )


def _register_scope(connection, scope):
    _database_binding(connection, scope)
    existing = _rows(connection, "SELECT configuration_hash FROM finance.incremental_scopes WHERE scope_id=%s",
                     (scope.scope_id,))
    if existing:
        inputs.require(existing[0]["configuration_hash"] == scope.config_hash, "registered-scope-configuration-drift")
        return False
    connection.execute(
        """INSERT INTO finance.incremental_scopes(
            scope_id, configuration_hash, configuration, canonical_account_hash,
            target_origin, activity_account_id, source_connection_id, source_account_id
        ) VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s,%s)""",
        (scope.scope_id, scope.config_hash, _json(scope.config), content_hash(scope.canonical_id),
         scope.config["origin"], scope.account_id, scope.config["rawConnectionId"],
         scope.config["sourceAccountId"]),
    )
    return True


def _database_binding(connection, scope):
    rows = _rows(connection, """SELECT current_database() AS database,instance_id,environment_marker
        FROM finance.shadow_authority_metadata WHERE singleton""")
    expected = scope.config["postgres"]
    inputs.require(len(rows) == 1 and rows[0]["database"] == expected["database"]
                   and str(rows[0]["instance_id"]) == expected["instanceId"]
                   and rows[0]["environment_marker"] == expected["environment"],
                   "postgres-authority-binding-mismatch")


def _versions(connection, scope):
    return _rows(connection,
                 "SELECT * FROM finance.incremental_source_versions WHERE scope_id=%s ORDER BY source_id,version_number",
                 (scope.scope_id,))


def _add_version(connection, scope, observation, number, predecessor, artifact, proof, admitted=True, reason=""):
    source_id = observation.provider_transaction_id or f"observation:{observation.observation_id}"
    version_id = stable_id("incremental_version", scope.scope_id, source_id, number, observation.observation_id)
    body = inputs.observation_document(observation)
    connection.execute(
        """INSERT INTO finance.incremental_source_versions(
            version_id, scope_id, source_id, version_number, economic_hash,
            predecessor_version_id, observation_document, currency_proof, first_snapshot_hash,
            admitted, reason
        ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)""",
        (version_id, scope.scope_id, source_id, number, inputs.economic_key(observation),
         predecessor, _json(body), _json(proof or {}), artifact, admitted, reason),
    )
    return {"version_id": version_id, "source_id": source_id, "version_number": number,
            "economic_hash": inputs.economic_key(observation), "observation_document": body,
            "currency_proof": proof or {}, "admitted": admitted, "reason": reason}


def _accept(connection, resolution):
    return persist_accepted_identity_resolution(
        connection, resolution,
        expected_previous_generation_hash=accepted_current_generation_hash(connection),
    )


def _seed(connection, scope):
    observations = tuple(item for item in scope.baseline.observations if item.canonical_account_id == scope.canonical_id)
    numbers = {}
    for observation in sorted(observations, key=lambda item: (item.observed_at, item.observation_id)):
        source_id = observation.provider_transaction_id or f"observation:{observation.observation_id}"
        number, predecessor = numbers.get(source_id, (0, None))
        proof = inputs.baseline_currency_proof(scope, observation)
        admission_hold = (proof or {}).get("rawProjectionBlocker") or (proof or {}).get("dateStatus", "")
        version = _add_version(
            connection, scope, observation, number + 1, predecessor,
            (proof or {}).get("artifactSha256", scope.config["baselineManifestSha256"]), proof,
            admitted=not admission_hold, reason=admission_hold,
        )
        numbers[source_id] = (number + 1, version["version_id"])
    baseline = resolve_identity(observations, policy=scope.baseline.policy, token_scopes=scope.baseline.token_scopes)
    _accept(connection, baseline)


def _baseline_resolution(scope):
    return resolve_identity(
        tuple(item for item in scope.baseline.observations if item.canonical_account_id == scope.canonical_id),
        policy=scope.baseline.policy, token_scopes=scope.baseline.token_scopes,
    )


def _ingest(connection, scope, source):
    versions = _versions(connection, scope)
    latest = {row["source_id"]: row for row in versions}
    latest_admitted = {row["source_id"]: row for row in versions if row["admitted"]}
    persistent_blocks = {
        row["source_id"]: row["reason"] for row in versions
        if row["reason"] in inputs.PERSISTENT_SOURCE_BLOCKERS
    }
    # Dateless observations cannot become financial versions. Their safety
    # restrictions persist in the sealed run's existing source-change journal.
    for restriction in _rows(connection, """
        SELECT DISTINCT restriction->>'sourceId' AS source_id,restriction->>'reason' AS reason
        FROM finance.incremental_runs run
        CROSS JOIN LATERAL jsonb_array_elements(
            COALESCE(run.plan_document->'heldSourceChanges','[]'::jsonb)
        ) restriction
        WHERE run.scope_id=%s AND restriction->>'reason'=ANY(%s)
        ORDER BY source_id,reason
    """, (scope.scope_id, sorted(inputs.PERSISTENT_SOURCE_BLOCKERS))):
        persistent_blocks.setdefault(restriction["source_id"], restriction["reason"])
    identity_connections = {
        item.source_connection_id for item in scope.baseline.observations
        if item.source_family == "simplefin" and item.source_account_id == scope.config["sourceAccountId"]
        and item.canonical_account_id == scope.canonical_id
    }
    inputs.require(len(identity_connections) <= 1, "baseline-source-namespace-ambiguous")
    blocked, seen = dict(persistent_blocks), set()
    for transaction in source.transactions:
        source_id = f"simplefin:{scope.config['sourceAccountId']}:{transaction.source_transaction_id}"
        seen.add(source_id)
        connection_id = next(iter(identity_connections), source_account_scope(
            "simplefin", source_id, canonical_account_id=scope.canonical_id
        )[1])
        row = {
            "account_id": scope.canonical_id, "source_id": source_id,
            "source_file": str(source.snapshot_path.relative_to(scope.root)),
            "source_connection_id": connection_id,
            "date": transaction.effective_at.date().isoformat(), "amount": str(transaction.amount),
            "currency": source.currency, "description": transaction.description,
            "status": transaction.status, "observed_at": source.observed_at.isoformat(),
        }
        candidate, = observations_from_transaction_rows(
            (row,), duplicate_summaries=scope.declarations.duplicate_summaries,
            token_scopes=scope.declarations.token_scopes,
            source_artifact_hashes={row["source_file"]: source.snapshot_hash},
        )
        previous = latest.get(source_id)
        financial_previous = latest_admitted.get(source_id)
        raw_reason = inputs.raw_projection_blocker(
            source.raw_transactions[transaction.source_transaction_id], candidate.status,
        ) or persistent_blocks.get(source_id, "")
        if raw_reason:
            # Safety evidence is material to admission even with unchanged cash
            # fields; omitting it from a later sighting must not grant permission.
            unchanged = (previous if previous and not previous["admitted"]
                         and previous["reason"] == raw_reason
                         and previous["economic_hash"] == inputs.economic_key(candidate) else None)
        else:
            unchanged = next((item for item in (financial_previous, previous)
                              if item and item["economic_hash"] == inputs.economic_key(candidate)), None)
        if unchanged:
            # Sightings never replace the financial version's original row,
            # source artifact, observation id, or authority-interval timestamp.
            version = unchanged
        else:
            reason = ""
            if financial_previous:
                old = inputs.observation_from_document(financial_previous["observation_document"])
                if old.status == "posted" and candidate.status == "pending":
                    reason = "posted-to-pending-regression"
                if source.observed_at <= old.observed_at:
                    reason = "out-of-order-financial-version"
            reason = raw_reason or reason
            version = _add_version(
                connection, scope, candidate,
                1 if previous is None else previous["version_number"] + 1,
                previous["version_id"] if previous else None, source.snapshot_hash,
                {"currency": source.currency, "artifactSha256": source.snapshot_hash,
                 "sourceFamily": "simplefin", "rawSourceAccountId": scope.config["sourceAccountId"]},
                not reason, reason,
            )
            versions.append(version)
            latest[source_id] = version
            if reason in inputs.PERSISTENT_SOURCE_BLOCKERS:
                persistent_blocks[source_id] = reason
            if version["admitted"]:
                latest_admitted[source_id] = version
        if raw_reason:
            blocked[source_id] = raw_reason
        elif not version["admitted"]:
            blocked[source_id] = version["reason"]
        connection.execute(
            """INSERT INTO finance.incremental_source_sightings(
                scope_id, receipt_hash, source_id, version_id, observed_at, snapshot_hash
            ) VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (scope_id,receipt_hash,source_id) DO NOTHING""",
            (scope.scope_id, source.receipt_hash, source_id, version["version_id"],
             source.observed_at, source.snapshot_hash),
        )
    admitted = [item for item in versions if item["admitted"]]
    for state in source.pending_without_date:
        source_id = state["sourceId"]
        seen.add(source_id)
        previous = latest_admitted.get(source_id)
        raw_reason = inputs.raw_projection_blocker(
            source.raw_transactions[state["rawTransactionId"]], state["status"],
        ) or persistent_blocks.get(source_id, "")
        if raw_reason:
            blocked[source_id] = raw_reason
        elif previous and previous["observation_document"]["status"] == "posted":
            blocked[source_id] = "posted-to-dateless-pending-regression"
    observations = tuple(inputs.observation_from_document(row["observation_document"]) for row in admitted)
    resolution = resolve_identity(observations, policy=scope.baseline.policy, token_scopes=scope.baseline.token_scopes)
    proofs = {row["observation_document"]["observation_id"]: row["currency_proof"] for row in admitted}
    return resolution, proofs, blocked, seen


def _projection_bases(connection, scope):
    rows = _rows(connection, """
        SELECT DISTINCT ON (observation.accepted_event_id) observation.*,
            event.source_day,event.signed_amount,event.currency_code
        FROM finance.incremental_projection_observations observation
        JOIN finance.canonical_identity_events event
          ON event.canonical_identity_event_id=observation.source_generation_event_id
        WHERE observation.scope_id=%s
        ORDER BY observation.accepted_event_id,observation.observation_number DESC
    """, (scope.scope_id,))
    result = {str(row["accepted_event_id"]): {
        "source_day": row["source_day"].isoformat(), "signed_amount": str(row["signed_amount"]),
        "currency": row["currency_code"], "source_revision_number": row["source_revision_number"],
        "source_generation_event_id": str(row["source_generation_event_id"]),
        "observed_activity": row["observed_activity"], "activity_id": row["activity_id"],
        "original_projection_binding_id": (
            str(row["original_projection_binding_id"]) if row["original_projection_binding_id"] else None
        ),
        "representation_kind": row["representation_kind"], "base_source_amount": str(row["base_source_amount"]),
        "anchor_hash": row["anchor_hash"], "anchor_checkpoint_hash": row["anchor_checkpoint_hash"],
        "anchor_source_id": row["anchor_source_id"],
    } for row in rows}
    for row in _rows(connection, """
        SELECT adoption.*,checkpoint.source_state->adoption.source_id AS source_state
        FROM finance.incremental_anchor_adoptions adoption
        JOIN finance.incremental_anchor_checkpoints checkpoint ON checkpoint.checkpoint_hash=adoption.checkpoint_hash
        WHERE adoption.scope_id=%s
    """, (scope.scope_id,)):
        state = row["source_state"]
        result.setdefault(str(row["accepted_event_id"]), {
            "source_day": state["sourceDay"], "signed_amount": state["amount"], "currency": state["currency"],
            "source_revision_number": None, "source_generation_event_id": None,
            "observed_activity": row["observed_activity"], "activity_id": row["activity_id"],
            "original_projection_binding_id": None, "representation_kind": "full", "base_source_amount": "0",
            "anchorBasis": {"anchorHash": row["anchor_hash"], "checkpointHash": row["checkpoint_hash"], "sourceId": row["source_id"]},
        })
    return result


def _bootstrap_applied(connection, scope):
    return _rows(connection, """
        SELECT outbox.operation_document,observation.observed_activity
        FROM finance.incremental_projection_observations observation
        JOIN finance.incremental_outbox outbox ON outbox.operation_id=observation.operation_id
        WHERE observation.scope_id=%s AND observation.observation_kind='applied'
        ORDER BY observation.observation_number
    """, (scope.scope_id,))


def _known_bindings(connection, scope):
    bindings = {str(row["accepted_event_id"]): row for row in _rows(
        connection, "SELECT * FROM finance.incremental_activity_bindings WHERE scope_id=%s", (scope.scope_id,)
    )}
    historical = _rows(connection, """
        SELECT link.accepted_event_id,binding.application_projection_binding_id,
               binding.target_activity_hash,binding.canonical_id,binding.is_active,
               event.canonical_identity_event_id,event.source_day,event.signed_amount,
               event.currency_code,event.status,event.trusted,mapping.revision_number
        FROM finance.accepted_identity_projection_links link
        JOIN finance.application_projection_bindings binding
          ON binding.application_projection_binding_id=link.application_projection_binding_id
        JOIN finance.canonical_identity_events event
          ON event.canonical_identity_event_id=binding.canonical_identity_event_id
        LEFT JOIN finance.accepted_identity_event_mappings mapping
          ON mapping.generation_event_id=event.canonical_identity_event_id
         AND mapping.accepted_event_id=link.accepted_event_id
        WHERE event.canonical_account_hash=%s AND binding.target_application='wealthfolio'
    """, (content_hash(scope.canonical_id),))
    for row in historical:
        key = str(row["accepted_event_id"])
        binding = bindings.setdefault(key, {})
        binding.setdefault("target_hashes", []).append(row["target_activity_hash"])
        binding.setdefault("verified_keys", []).append(f"canonical:{scope.canonical_id}:{row['canonical_id']}")
        if not row["is_active"]:
            binding["inactive"] = True
    return bindings, historical


def _canonical_keys(scope, event):
    claims = set(event.member_claim_ids)
    return {
        f"canonical:{scope.canonical_id}:{original.canonical_event_id}"
        for original in scope.baseline.canonical_events
        if original.canonical_event_id in scope.published_canonical_ids
        and original.canonical_account_hash == event.canonical_account_hash
        and set(original.member_claim_ids).issubset(claims)
    }


def _source_basis(event, revision, event_id, row, original_binding_id=None, representation=None):
    return {
        "source_day": event.source_day.isoformat(), "signed_amount": str(event.signed_amount),
        "currency": event.currency, "source_revision_number": revision,
        "source_generation_event_id": str(event_id), "observed_activity": row,
        "activity_id": row["id"],
        "original_projection_binding_id": str(original_binding_id) if original_binding_id else None,
        **(representation or {"representation_kind": "full", "base_source_amount": "0"}),
    }


def _baseline_adoptions(connection, scope, app, bases, bindings):
    baseline = _baseline_resolution(scope)
    mappings = {row["canonical_id"]: row for row in _rows(connection, """
        SELECT event.canonical_id,mapping.accepted_event_id,mapping.revision_number
        FROM finance.accepted_identity_event_mappings mapping
        JOIN finance.canonical_identity_events event ON event.canonical_identity_event_id=mapping.generation_event_id
        WHERE mapping.generation_id=%s
    """, (_uuid("generation", baseline.generation_hash),))}
    observations = {item.observation_id: item for item in baseline.observations}
    result = []
    for event in baseline.canonical_events:
        mapped = mappings.get(event.canonical_event_id)
        if not mapped or mapped["accepted_event_id"] is None:
            continue
        accepted_id = str(mapped["accepted_event_id"])
        if accepted_id in bases:
            continue
        binding = bindings.get(accepted_id)
        try:
            found = projection.operation(
                scope, event, accepted_id, mapped["revision_number"],
                [observations[key] for key in event.member_observation_ids],
                app, binding, None, True,
                verified_keys=_canonical_keys(scope, event) | set((binding or {}).get("verified_keys", ())),
            )
        except inputs.IncrementalHold:
            continue
        if found["kind"] == "adopt":
            basis = _source_basis(event, mapped["revision_number"],
                                  _uuid("event", baseline.generation_hash, event.canonical_event_id), found["observed"])
            bases[accepted_id] = basis
            bindings.setdefault(accepted_id, {}).update(activity_id=basis["activity_id"])
            result.append({"acceptedEventId": accepted_id, "basis": basis})
    return result


def _historical_adoptions(scope, app, bases, bindings, historical):
    from types import SimpleNamespace
    result = []
    for record in historical:
        accepted_id = str(record["accepted_event_id"])
        if accepted_id in bases or not record["is_active"] or not record["trusted"] or record["status"] != "posted":
            continue
        matches = [row for row in app["rows"] if content_hash(row["id"]) == record["target_activity_hash"]]
        event = SimpleNamespace(source_day=record["source_day"], signed_amount=record["signed_amount"],
                                currency=record["currency_code"])
        if len(matches) != 1 or projection.unsupported_link(matches[0]) or not projection.economics_match(matches[0], event):
            continue
        basis = _source_basis(event, record["revision_number"], record["canonical_identity_event_id"],
                              matches[0], record["application_projection_binding_id"])
        bases[accepted_id] = basis
        bindings.setdefault(accepted_id, {}).update(activity_id=basis["activity_id"])
        result.append({"acceptedEventId": accepted_id, "basis": basis})
    return result


def _anchor_adoptions(scope, contract, context_value, app, bases, bindings, resolution, mappings):
    from dataclasses import replace
    observations = {item.observation_id: item for item in resolution.observations}
    result = []
    for event in resolution.canonical_events:
        mapping = mappings[event.canonical_event_id]
        if mapping.accepted_event_id is None or mapping.accepted_event_id in bases:
            continue
        members = [observations[key] for key in event.member_observation_ids]
        ids = {item.provider_transaction_id for item in members}
        if len(ids) != 1 or any(item.source_family != "simplefin" for item in members):
            continue
        source_id = next(iter(ids))
        state = context_value["checkpointStates"].get(source_id)
        if state is None or state["status"] != "posted":
            continue
        historical = replace(event, signed_amount=inputs.money(state["amount"]),
                             source_day=date.fromisoformat(state["sourceDay"]), currency=state["currency"])
        binding = bindings.get(mapping.accepted_event_id)
        found = projection.operation(
            scope, historical, mapping.accepted_event_id, mapping.revision_number, members, app,
            binding, None, True, verified_keys=_canonical_keys(scope, event) | set((binding or {}).get("verified_keys", ())),
        )
        if found["kind"] != "adopt" or found["activityId"] == contract.document["assertionActivityId"]:
            continue
        basis = {
            "source_day": state["sourceDay"], "signed_amount": state["amount"], "currency": state["currency"],
            "source_generation_event_id": None, "source_revision_number": None,
            "original_projection_binding_id": None, "observed_activity": found["observed"],
            "activity_id": found["activityId"], "representation_kind": "full", "base_source_amount": "0",
            "anchorBasis": {"anchorHash": contract.anchor_hash, "checkpointHash": context_value["checkpointHash"], "sourceId": source_id},
        }
        bases[mapping.accepted_event_id] = basis
        bindings.setdefault(mapping.accepted_event_id, {}).update(activity_id=found["activityId"])
        result.append({"acceptedEventId": mapping.accepted_event_id, "basis": basis})
    return result


def _store_run(connection, scope, plan, source=None, resolution=None):
    connection.execute(
        """INSERT INTO finance.incremental_runs(
            run_hash,scope_id,receipt_hash,snapshot_hash,manifest_hash,policy_hash,generation_id,
            source_observed_at,balance_effective_at,source_balance,currency_code,plan_document
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (run_hash) DO NOTHING""",
        (plan["runHash"], scope.scope_id, source.receipt_hash if source else None,
         source.snapshot_hash if source else None, scope.config["baselineManifestSha256"],
         scope.baseline.policy.policy_hash,
         _uuid("generation", resolution.generation_hash) if resolution else None,
         source.observed_at if source else None, source.balance_at if source else None,
         source.balance if source else None, source.currency if source else None, _json(plan)),
    )


def reconciliation_diagnostics(plan):
    """Explain a sealed plan without changing which source records may project."""
    result = {}
    if "operations" in plan:
        result["proposedOperationCount"] = len(plan["operations"])
    if "predictedCash" in plan or "sourceBalance" in plan:
        inputs.require("predictedCash" in plan and "sourceBalance" in plan,
                      "status-reconciliation-fields-incomplete")
        result["plannedBalanceMatchesSource"] = (
            inputs.money(plan["predictedCash"]) == inputs.money(plan["sourceBalance"])
        )
    anchor = plan.get("sourceAnchor")
    if anchor is not None:
        review_ids = {
            source_id for item in plan["qualifications"]
            if item["reason"] == "anchor-new-historical-id-held"
            for source_id in item["proof"]["sourceIds"]
        }
        changes = anchor["changes"]
        inputs.require(review_ids <= changes.keys(), "status-source-review-binding-drift")
        result["newHistoricalReviewCount"] = len(review_ids)
        result["frontierReviewCount"] = sum(
            changes[key]["targetState"]["sourceDay"] == changes[key]["observedPostedWatermark"]
            for key in review_ids
        )
    return result


def status(connection, scope_id, *, root=None):
    rows = _rows(connection, "SELECT run_hash,state,evidence FROM finance_read.incremental_scope_status WHERE scope_id=%s",
                 (scope_id,))
    result = {"scopeId": scope_id, **(rows[0] if rows else {"state": "not-initialized"})}
    if not rows or not result["run_hash"]:
        return result
    evidence = dict(result.get("evidence") or {})
    if root is not None:
        run_hash = inputs.require_hash(result["run_hash"])
        plan = inputs.document(inputs.private(root, f"incremental/plans/{run_hash}.json"))
        inputs.require(plan.get("scopeId") == str(scope_id) and plan.get("runHash") == run_hash
                      and content_hash({k: v for k, v in plan.items() if k != "runHash"}) == run_hash,
                      "status-plan-binding-drift")
        evidence.update(reconciliation_diagnostics(plan))
    states = _rows(connection, """SELECT operation_state,count(*) AS count
        FROM finance_read.incremental_projection_history WHERE scope_id=%s AND run_hash=%s
        GROUP BY operation_state""", (scope_id, result["run_hash"]))
    evidence["journaledOperationCount"] = sum(row["count"] for row in states)
    evidence["appliedOperationCount"] = sum(row["count"] for row in states if row["operation_state"] == "applied")
    return {**result, "evidence": evidence}


def _unfinished(connection, scope_id):
    return _rows(connection, """
        SELECT r.run_hash FROM finance.incremental_runs r
        JOIN LATERAL (
            SELECT state FROM finance.incremental_run_events e WHERE e.run_hash=r.run_hash
            ORDER BY event_number DESC LIMIT 1
        ) e ON true WHERE r.scope_id=%s AND e.state IN ('pending','uncertain','backup')
    """, (scope_id,))


def _bind(connection, scope, run_hash, accepted_id, row, resolution=None, canonical_id=None,
          *, basis, operation_id=None):
    existing = _rows(connection, """SELECT activity_id FROM finance.incremental_activity_bindings
        WHERE scope_id=%s AND accepted_event_id=%s""", (scope.scope_id, accepted_id))
    if existing:
        inputs.require(existing[0]["activity_id"] == row["id"], "activity-binding-conflict")
    else:
        connection.execute(
            """INSERT INTO finance.incremental_activity_bindings(
                scope_id,accepted_event_id,activity_id,idempotency_key,origin_run_hash,observed_activity
            ) VALUES (%s,%s,%s,%s,%s,%s::jsonb)""",
            (scope.scope_id, accepted_id, row["id"], row["idempotencyKey"], run_hash, _json(row)),
        )
    if basis.get("anchorBasis") and basis["source_generation_event_id"] is None:
        anchor_basis = basis["anchorBasis"]
        connection.execute("""
            INSERT INTO finance.incremental_anchor_adoptions(
                adoption_id,scope_id,accepted_event_id,activity_id,run_hash,anchor_hash,checkpoint_hash,source_id,observed_activity
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT (scope_id,accepted_event_id) DO NOTHING
        """, (stable_id("anchor_adoption", scope.scope_id, accepted_id, anchor_basis["checkpointHash"]),
              scope.scope_id, accepted_id, row["id"], run_hash, anchor_basis["anchorHash"],
              anchor_basis["checkpointHash"], anchor_basis["sourceId"], _json(row)))
        return
    if resolution is not None and basis.get("representation_kind", "full") == "full":
        persist_application_projection_bindings(
            connection, resolution, {canonical_id: content_hash(row["id"])},
        )
    evidence = {
        "scopeId": scope.scope_id, "acceptedEventId": accepted_id, "runHash": run_hash,
        "basis": basis, "operationId": str(operation_id) if operation_id else None,
    }
    connection.execute(
        """INSERT INTO finance.incremental_projection_observations(
            observation_id,scope_id,accepted_event_id,activity_id,run_hash,
            source_generation_event_id,source_revision_number,original_projection_binding_id,
            operation_id,observation_kind,observed_activity,representation_kind,base_source_amount,
            anchor_hash,anchor_checkpoint_hash,anchor_source_id
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
        ON CONFLICT (observation_id) DO NOTHING""",
        (stable_id("incremental_projection_observation", content_hash(evidence)), scope.scope_id,
         accepted_id, row["id"], run_hash, basis["source_generation_event_id"],
         basis["source_revision_number"], basis["original_projection_binding_id"],
         operation_id, "applied" if operation_id else "adopted", _json(row),
         basis.get("representation_kind", "full"), basis.get("base_source_amount", "0"),
         basis.get("anchor_hash"), basis.get("anchor_checkpoint_hash"), basis.get("anchor_source_id")),
    )


def plan(connection, scope, client, *, now=None):
    """Accept eligible identity revisions and enqueue a fully reconciled batch."""
    now = inputs.timestamp(now or datetime.now(timezone.utc))
    with worker(connection):
        inputs.require(PROCESS_RELEASE == current_incremental_release(), "running-incremental-release-changed")
        checked = inputs.load_scope(scope.root, scope.config_path)
        inputs.require(checked.config_hash == scope.config_hash, "scope-configuration-changed")
        with connection.transaction():
            _lock_identity_writer(connection)
            created = _register_scope(connection, scope)
            inputs.require(not _unfinished(connection, scope.scope_id), "scope-has-unfinished-run")
            try:
                source = inputs.load_source(scope, now)
                app = projection.app_state(scope, client)
                inputs.require(app["account"]["currency"] == source.currency, "app-source-currency-mismatch")
            except (ValueError, KeyError) as error:
                reason = str(error) if isinstance(error, inputs.IncrementalHold) else "input-contract-invalid"
                body = {"scopeId": scope.scope_id, "configurationHash": scope.config_hash,
                        "state": "held", "reason": reason, "checkedAt": now.isoformat(),
                        "release": PROCESS_RELEASE}
                body["runHash"] = content_hash(body)
                _store_run(connection, scope, body)
                _event(connection, body["runHash"], "held", {"reason": reason})
                inputs.write_receipt(scope.root, f"incremental/plans/{body['runHash']}.json", body)
                return body
            bootstrap_contract = bootstrap.load_contract(scope)
            bootstrap_applied = _bootstrap_applied(connection, scope) if bootstrap_contract else []
            anchor_contract = source_anchor.load_contract(scope)
            anchor_context = source_anchor.context(connection, scope, anchor_contract, source, app) if anchor_contract else None
            assertion_requires_anchor = bool(source_anchor.assertion_rows(scope, app)) and anchor_contract is None
            if assertion_requires_anchor and bootstrap_contract is not None:
                raise inputs.IncrementalHold("source-balance-anchor-contract-required")
            if created or not _versions(connection, scope):
                _seed(connection, scope)
            previous = _projection_bases(connection, scope)
            bindings, _ = _known_bindings(connection, scope)
            bootstrap_adoptions = _baseline_adoptions(connection, scope, app, previous, bindings)
            resolution, proofs, blocked, seen = _ingest(connection, scope, source)
            accepted = _accept(connection, resolution)
            mappings = {item.canonical_event_id: item for item in accepted.mappings}
            current_bindings, historical_bindings = _known_bindings(connection, scope)
            for key, value in current_bindings.items():
                bindings.setdefault(key, {}).update(value)
            bootstrap_adoptions.extend(_historical_adoptions(
                scope, app, previous, bindings, historical_bindings
            ))
            if anchor_contract:
                bootstrap_adoptions.extend(_anchor_adoptions(
                    scope, anchor_contract, anchor_context, app, previous, bindings, resolution, mappings
                ))
            observations = {item.observation_id: item for item in resolution.observations}
            baseline_posted_claims = {
                observation.provider_transaction_id for observation in scope.baseline.observations
                if observation.canonical_account_id == scope.canonical_id and observation.status == "posted"
            }
            related_events = {
                value for link in scope.baseline.relationships
                for value in (link.left_canonical_event_id, link.right_canonical_event_id)
            }
            baseline_observations = {item.observation_id: item for item in scope.baseline.observations}
            related_claims = set()
            for historical in scope.baseline.canonical_events:
                if historical.canonical_event_id in related_events or any(
                    baseline_observations[key].canonical_account_id != scope.canonical_id
                    for key in historical.member_observation_ids
                ):
                    related_claims.update(
                        baseline_observations[key].provider_transaction_id
                        for key in historical.member_observation_ids
                        if baseline_observations[key].canonical_account_id == scope.canonical_id
                    )
            qualifications, operations, adoptions = [], [], []
            for event in resolution.canonical_events:
                mapping = mappings[event.canonical_event_id]
                members = [observations[key] for key in event.member_observation_ids]
                source_ids = sorted({item.provider_transaction_id for item in members if item.provider_transaction_id})
                supplied_proofs = [proofs[item.observation_id] for item in members if proofs.get(item.observation_id)]
                reason = ""
                if mapping.outcome == "conflict":
                    reason = ";".join(mapping.conflict_reasons)
                elif set(source_ids) & related_claims:
                    reason = "baseline-related-event-unsupported"
                elif not supplied_proofs or any(proof["currency"] != event.currency for proof in supplied_proofs):
                    reason = "event-currency-not-source-proven"
                elif any(source_id in blocked for source_id in source_ids):
                    reason = "held-source-financial-version"
                elif event.currency != source.currency:
                    reason = "event-account-currency-conflict"
                if reason:
                    op = {"kind": "hold", "reason": reason}
                else:
                    try:
                        op = projection.operation(
                            scope, event, mapping.accepted_event_id, mapping.revision_number, members,
                            app, bindings.get(mapping.accepted_event_id),
                            previous.get(mapping.accepted_event_id),
                            bool(set(source_ids) & baseline_posted_claims),
                            verified_keys=(_canonical_keys(scope, event)
                                           | set(bindings.get(mapping.accepted_event_id, {}).get("verified_keys", ()))),
                        )
                    except inputs.IncrementalHold as error:
                        op = {"kind": "hold", "reason": str(error)}
                    if anchor_contract:
                        op = source_anchor.project(
                            scope, anchor_contract, anchor_context, event, members,
                            mapping.accepted_event_id, mapping.revision_number, app,
                            bindings.get(mapping.accepted_event_id), previous.get(mapping.accepted_event_id),
                            op, _canonical_keys(scope, event) | set(
                                bindings.get(mapping.accepted_event_id, {}).get("verified_keys", ())
                            ),
                        )
                    elif assertion_requires_anchor and (
                        op["kind"] == "create" or op.get("reason") == "baseline-posted-activity-missing"
                    ):
                        op = {"kind": "hold", "reason": "source-balance-anchor-contract-required"}
                    if bootstrap_contract and (
                        op["kind"] == "create" or op.get("reason") == "baseline-posted-activity-missing"
                    ):
                        prior_reason = bootstrap_contract.missing_reason(source_ids)
                        if prior_reason:
                            op = {"kind": "hold", "reason": prior_reason}
                        elif op.get("reason") == "baseline-posted-activity-missing":
                            try:
                                permit = bootstrap.permit(
                                    scope, bootstrap_contract, event, members, seen, app, bootstrap_applied,
                                )
                                op = projection.operation(
                                    scope, event, mapping.accepted_event_id, mapping.revision_number, members,
                                    app, bindings.get(mapping.accepted_event_id), previous.get(mapping.accepted_event_id),
                                    True, verified_keys=(_canonical_keys(scope, event)
                                        | set(bindings.get(mapping.accepted_event_id, {}).get("verified_keys", ()))),
                                    creation_evidence=permit,
                                )
                            except inputs.IncrementalHold as error:
                                op = {"kind": "hold", "reason": str(error)}
                    if op["kind"] == "update":
                        op = {"kind": "hold", "reason": projection.UPDATE_CONTRACT_HOLD}
                qualification = {
                    "canonicalEventId": event.canonical_event_id, "acceptedEventId": mapping.accepted_event_id,
                    "revisionNumber": mapping.revision_number, "sourceDay": event.source_day.isoformat(),
                    "signedAmount": str(event.signed_amount), "currency": event.currency,
                    "description": event.description, "eventStatus": event.status,
                    "status": "pending" if event.status == "pending" else "held" if op["kind"] == "hold" else "eligible",
                    "reason": op.get("reason", "exact-source-and-application-contract"),
                    "proof": {"sourceIds": source_ids, "currencyEvidence": supplied_proofs,
                              "observedInSnapshot": bool(set(source_ids) & seen),
                              "sourceAdmission": source.admission,
                              "bootstrap": op.get("bootstrapProof"),
                              "sourceAnchorTransition": anchor_context["changes"].get(source_ids[0])
                                  if anchor_context and len(source_ids) == 1 else None,
                              "identityReviewDecisionHashes": list(mapping.review_decision_hashes)},
                }
                if op.get("reason") == projection.UPDATE_CONTRACT_HOLD:
                    qualification["proof"]["deliveredSourceBasis"] = previous.get(mapping.accepted_event_id)
                    qualification["proof"]["activityWriteContract"] = projection.ACTIVITY_WRITE_CONTRACT
                qualifications.append(qualification)
                if op["kind"] == "create":
                    op["canonicalEventId"] = event.canonical_event_id
                    op["deliveryBasis"] = previous.get(mapping.accepted_event_id)
                    operations.append(op)
                elif op["kind"] == "adopt":
                    adoptions.append({
                        "canonicalEventId": event.canonical_event_id, "acceptedEventId": mapping.accepted_event_id,
                        "basis": _source_basis(
                            event, mapping.revision_number,
                            _uuid("event", resolution.generation_hash, event.canonical_event_id), op["observed"],
                            representation=op.get("representation"),
                        ),
                    })
            predicted = projection.cash(app["rows"]) + sum((inputs.money(op["delta"]) for op in operations), inputs.money(0))
            balanced = predicted == source.balance
            financial_sources = set(anchor_context["financialSources"]) if anchor_context else set()
            covered_sources = {op["anchorTransition"]["sourceId"] for op in operations if op.get("anchorTransition")}
            anchor_set_valid = (not anchor_context or (
                covered_sources == financial_sources and not anchor_context["invalidLifecycleSources"]
                and not blocked
                and inputs.money(anchor_context["expectedSourceBalance"]) == source.balance
            ))
            balanced = balanced and anchor_set_valid
            held_count = sum(item["status"] == "held" for item in qualifications)
            represented = {source_id for item in qualifications if item["status"] == "held"
                           for source_id in item["proof"]["sourceIds"]}
            held_count += len(set(blocked) - represented)
            historical_count = sum(
                item["status"] == "held" and not set(item["proof"]["sourceIds"]) & financial_sources
                for item in qualifications
            ) if anchor_context else 0
            cash_holds = held_count - historical_count if anchor_context else held_count
            body = {
                "schemaVersion": 1, "scopeId": scope.scope_id, "configurationHash": scope.config_hash,
                "release": PROCESS_RELEASE,
                "activityWriteContract": projection.ACTIVITY_WRITE_CONTRACT,
                "bootstrapHash": bootstrap_contract.document["bootstrapHash"] if bootstrap_contract else None,
                "sourceAnchor": anchor_context,
                "sourceAnchorHash": anchor_contract.anchor_hash if anchor_contract else None,
                "receiptHash": source.receipt_hash, "snapshotHash": source.snapshot_hash,
                "baselineManifestHash": scope.config["baselineManifestSha256"],
                "generationHash": resolution.generation_hash, "policyHash": resolution.policy.policy_hash,
                "sourceObservedAt": source.observed_at.isoformat(), "balanceEffectiveAt": source.balance_at.isoformat(),
                "sourceBalance": str(source.balance), "startingCash": str(projection.cash(app["rows"])),
                "pendingSourceRecords": list(source.pending_without_date),
                "predictedCash": str(predicted), "currency": source.currency,
                "appBefore": app, "qualifications": qualifications, "operations": operations,
                "projectionAdoptions": bootstrap_adoptions + adoptions,
                "heldCount": held_count,
                "historicalBackfillCount": historical_count,
                "cashTransitionCount": len(financial_sources) if anchor_context else len(operations),
                "heldSourceChanges": [{"sourceId": key, "reason": value} for key, value in sorted(blocked.items())],
                "state": "held" if not balanced else "pending" if operations else "held" if cash_holds else "noop",
                "reason": ("source-anchor-transition-set-held" if not anchor_set_valid
                           else "whole-eligible-batch-balance-mismatch" if not balanced
                           else "local-projection-holds" if cash_holds and not operations
                           else "whole-eligible-batch-reconciled"),
            }
            body.update(reconciliation_diagnostics(body))
            body["journaledOperationCount"] = len(operations) if balanced else 0
            body["appliedOperationCount"] = 0
            body["runHash"] = content_hash(body)
            _store_run(connection, scope, body, source, resolution)
            for qualification in qualifications:
                qid = stable_id("incremental_qualification", body["runHash"], qualification["canonicalEventId"])
                qualification_state = qualification["status"] if balanced else "held"
                connection.execute(
                    """INSERT INTO finance.incremental_qualifications(
                        qualification_id,scope_id,run_hash,generation_event_id,accepted_event_id,revision_number,
                        source_day,signed_amount,currency_code,description,event_status,qualification_status,reason,proof
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (qualification_id) DO NOTHING""",
                    (qid, scope.scope_id, body["runHash"],
                     _uuid("event", resolution.generation_hash, qualification["canonicalEventId"]),
                     qualification["acceptedEventId"], qualification["revisionNumber"],
                     qualification["sourceDay"], qualification["signedAmount"], qualification["currency"],
                     qualification["description"], qualification["eventStatus"], qualification_state,
                     qualification["reason"] if balanced or qualification["status"] == "held"
                     else body["reason"], _json(qualification["proof"])),
                )
            for adopted in bootstrap_adoptions + adoptions:
                basis = adopted["basis"]
                _bind(
                    connection, scope, body["runHash"], adopted["acceptedEventId"], basis["observed_activity"],
                    resolution if "canonicalEventId" in adopted else None,
                    adopted.get("canonicalEventId"), basis=basis,
                )
            if balanced:
                for op in operations:
                    oid = stable_id("incremental_operation", body["runHash"], op["acceptedEventId"], op["payloadHash"])
                    connection.execute(
                        """INSERT INTO finance.incremental_outbox(
                            operation_id,run_hash,qualification_id,accepted_event_id,revision_number,
                            operation_kind,payload_hash,operation_document
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (operation_id) DO NOTHING""",
                        (oid, body["runHash"],
                         stable_id("incremental_qualification", body["runHash"], op["canonicalEventId"]),
                         op["acceptedEventId"], op["revisionNumber"], op["kind"], op["payloadHash"], _json(op)),
                    )
            _event(connection, body["runHash"], body["state"], {
                "reason": body["reason"], "checkedAt": now.isoformat(),
                "heldCount": held_count, "eligibleOperationCount": len(operations),
                **reconciliation_diagnostics(body),
                "journaledOperationCount": body["journaledOperationCount"], "appliedOperationCount": 0,
            })
            if anchor_contract and body["state"] == "noop":
                source_anchor.finish(
                    connection, scope, anchor_contract, body, source.snapshot_hash,
                    source.receipt_hash, source.balance, app,
                )
            inputs.write_receipt(scope.root, f"incremental/plans/{body['runHash']}.json", body)
            return body


def _latest_attempt(connection, operation_id):
    rows = _rows(connection, """SELECT state,evidence FROM finance.incremental_attempts
        WHERE operation_id=%s ORDER BY event_number DESC LIMIT 1""", (operation_id,))
    return rows[0] if rows else None


def _receipt(connection, scope, run_hash, state, evidence):
    with connection.transaction():
        _lock_identity_writer(connection)
        _event(connection, run_hash, state, evidence)
    body = {"runHash": run_hash, "state": state, "evidence": evidence}
    inputs.write_receipt(scope.root, f"incremental/receipts/{run_hash}/{content_hash(body)}.json", body)
    return body


def apply(connection, scope, client, run_hash, *, now=None, after_api=None):
    """Deliver one stored plan, observing uncertain attempts instead of resending."""
    supplied_now = now
    now = inputs.timestamp(now or datetime.now(timezone.utc))
    with worker(connection):
        checked = inputs.load_scope(scope.root, scope.config_path)
        inputs.require(checked.config_hash == scope.config_hash, "scope-configuration-changed")
        _database_binding(connection, scope)
        writer_bindings = require_writer(scope, client)
        records = _rows(connection, "SELECT plan_document FROM finance.incremental_runs WHERE run_hash=%s AND scope_id=%s",
                        (run_hash, scope.scope_id))
        inputs.require(len(records) == 1, "incremental-plan-not-found")
        body = records[0]["plan_document"]
        inputs.require(content_hash({k: v for k, v in body.items() if k != "runHash"}) == run_hash
                       and body["configurationHash"] == scope.config_hash, "incremental-plan-binding-drift")
        inputs.require(inputs.document(inputs.private(scope.root, f"incremental/plans/{run_hash}.json")) == body,
                       "external-plan-receipt-drift")
        states = _rows(connection, """SELECT state FROM finance.incremental_run_events
            WHERE run_hash=%s ORDER BY event_number DESC LIMIT 1""", (run_hash,))
        if states[0]["state"] in {"applied", "noop", "held"}:
            return {"runHash": run_hash, "state": states[0]["state"], "replay": True}
        inputs.require(body.get("release") == PROCESS_RELEASE, "plan-release-changed-review-required")
        bootstrap_contract = bootstrap.load_contract(scope)
        inputs.require(body.get("bootstrapHash") == (
            bootstrap_contract.document["bootstrapHash"] if bootstrap_contract else None
        ), "plan-bootstrap-evidence-drift")
        anchor_contract = source_anchor.load_contract(scope)
        inputs.require(body.get("sourceAnchorHash") == (anchor_contract.anchor_hash if anchor_contract else None),
                       "plan-source-anchor-drift")
        try:
            source = inputs.load_source(scope, now)
        except (ValueError, KeyError):
            source = None
        operations = _rows(connection, "SELECT operation_id,operation_document FROM finance.incremental_outbox WHERE run_hash=%s ORDER BY operation_id",
                           (run_hash,))
        expected_operations = {
            stable_id("incremental_operation", run_hash, op["acceptedEventId"], op["payloadHash"]): op
            for op in body["operations"]
        }
        inputs.require({str(record["operation_id"]): record["operation_document"] for record in operations}
                       == expected_operations, "outbox-does-not-match-sealed-plan")
        inputs.require(body["policyHash"] == scope.baseline.policy.policy_hash
                       and body["baselineManifestHash"] == scope.config["baselineManifestSha256"],
                       "plan-policy-or-baseline-drift")
        inputs.require(all(content_hash(op["payload"]) == op["payloadHash"] for op in expected_operations.values()),
                       "operation-payload-fingerprint-drift")
        unsupported = [
            record for record in operations
            if record["operation_document"]["kind"] != "create"
            and (_latest_attempt(connection, record["operation_id"]) or {}).get("state") != "applied"
        ]
        if unsupported:
            uncertain = any((_latest_attempt(connection, record["operation_id"]) or {}).get("state")
                            in {"prepared", "uncertain"} for record in unsupported)
            return _receipt(connection, scope, run_hash, "uncertain" if uncertain else "held", {
                "reason": projection.UPDATE_CONTRACT_HOLD,
                "activityWriteContract": projection.ACTIVITY_WRITE_CONTRACT,
                "blockedOperationCount": len(unsupported),
            })
        inputs.require(all(
            op["kind"] == "create" and op.get("before") is None and op.get("activityId") is None
            and "id" not in op["payload"]
            for op in expected_operations.values()
            if op["kind"] == "create"
        ), "create-envelope-must-not-target-existing-activity")
        expected = {row["id"]: row for row in body["appBefore"]["rows"]}
        for record in operations:
            previous = _latest_attempt(connection, record["operation_id"])
            if previous and previous["state"] == "applied":
                row = previous["evidence"]["observed"]
                expected[row["id"]] = row
        backups = _rows(connection, """SELECT evidence FROM finance.incremental_run_events
            WHERE run_hash=%s AND state='backup' ORDER BY event_number LIMIT 1""", (run_hash,))
        if backups:
            verified_backup = verify_backup_file(inputs.private(scope.root, backups[0]["evidence"]["path"]),
                                                data_dir=scope.root, repo_root=inputs.REPO_ROOT)
            inputs.require(verified_backup == backups[0]["evidence"], "backup-evidence-drift")
        else:
            if any(_latest_attempt(connection, item["operation_id"]) for item in operations):
                return _receipt(connection, scope, run_hash, "uncertain", {"reason": "pre-mutation-backup-missing"})
            current = projection.app_state(scope, client)
            if current != body["appBefore"]:
                return _receipt(connection, scope, run_hash, "held", {"reason": "app-prestate-changed-before-backup"})
            inventory = require_unique_backup(client)
            backup = download_backup(
                client, inventory["filename"], inputs.private(scope.root, f"incremental/backups/{run_hash}.db"),
                data_dir=scope.root, repo_root=inputs.REPO_ROOT,
            )
            _receipt(connection, scope, run_hash, "backup", backup)
        for record in operations:
            oid, op = record["operation_id"], record["operation_document"]
            previous = _latest_attempt(connection, oid)
            if previous and previous["state"] == "applied":
                continue
            if previous is None:
                check_time = inputs.timestamp(supplied_now or datetime.now(timezone.utc))
                latest_pointer = inputs.document(inputs.private(scope.root, "automation/source-collection/latest-success.json"))
                latest_status = inputs.document(inputs.private(scope.root, "automation/source-collection/current.json"))
                if (
                    source is None
                    or source.receipt_hash != body["receiptHash"]
                    or source.balance != inputs.money(body["sourceBalance"])
                    or source.currency != body["currency"]
                    or source.observed_at != inputs.timestamp(body["sourceObservedAt"])
                    or source.balance_at != inputs.timestamp(body["balanceEffectiveAt"])
                    or latest_pointer.get("receiptHash") != body["receiptHash"]
                    or latest_status.get("receiptHash") != body["receiptHash"]
                    or latest_status.get("status") != "collected"
                    or not 0 <= (check_time - source.observed_at).total_seconds() <= scope.config["maxSnapshotAgeSeconds"]
                    or not 0 <= (check_time - source.balance_at).total_seconds() <= scope.config["maxBalanceAgeSeconds"]
                    or inputs.digest(source.snapshot_path) != source.snapshot_hash
                ):
                    return _receipt(connection, scope, run_hash, "held",
                                    {"reason": "source-evidence-changed-or-expired"})
            eligible = _rows(connection, """SELECT revision_number,identity_status FROM
                finance_read.accepted_identity_current WHERE accepted_event_id=%s""", (op["acceptedEventId"],))
            if not eligible or eligible[0]["revision_number"] != op["revisionNumber"] or eligible[0]["identity_status"] != "no-open-duplicate-decision":
                return _receipt(connection, scope, run_hash, "uncertain" if previous else "held",
                                {"reason": "accepted-revision-no-longer-current"})
            current = projection.app_state(scope, client)
            if current["account"] != body["appBefore"]["account"] or current["timezone"] != body["appBefore"]["timezone"]:
                return _receipt(connection, scope, run_hash, "uncertain" if previous else "held",
                                {"reason": "app-account-metadata-changed"})
            matches = [row for row in current["rows"] if row.get("idempotencyKey") == op["key"]
                       or op["activityId"] and row["id"] == op["activityId"]]
            recovered = previous and previous["state"] in {"prepared", "uncertain"}
            if recovered:
                if len(matches) != 1 or not projection.matches_post(matches[0], op):
                    return _receipt(connection, scope, run_hash, "uncertain", {"reason": "uncertain-remote-attempt-no-resend"})
                observed = matches[0]
                without = {row["id"]: row for row in current["rows"] if row["id"] != observed["id"]}
                prior_without = {key: value for key, value in expected.items() if key != op["activityId"]}
                if without != prior_without:
                    return _receipt(connection, scope, run_hash, "uncertain", {"reason": "intervening-app-work"})
            else:
                if {row["id"]: row for row in current["rows"]} != expected:
                    return _receipt(connection, scope, run_hash, "held", {"reason": "intervening-app-work"})
                if op["kind"] != "create":
                    return _receipt(connection, scope, run_hash, "held", {"reason": projection.UPDATE_CONTRACT_HOLD})
                if matches:
                    return _receipt(connection, scope, run_hash, "held", {"reason": "create-key-already-present"})
                if op.get("bootstrapProof"):
                    inputs.require(bootstrap_contract is not None
                                   and op["bootstrapProof"]["bootstrapHash"] == bootstrap_contract.document["bootstrapHash"]
                                   and op["bootstrapProof"]["candidate"] in bootstrap_contract.document["candidates"],
                                   "bootstrap-operation-not-reviewed")
                    try:
                        bootstrap.verify_live_inventory(
                            bootstrap_contract, current, _bootstrap_applied(connection, scope),
                        )
                    except inputs.IncrementalHold as error:
                        return _receipt(connection, scope, run_hash, "held", {"reason": str(error)})
                if anchor_contract:
                    try:
                        source_anchor.verify_continuity(anchor_contract, current, source_anchor._applied(connection, scope))
                        inputs.require(op.get("anchorTransition") is not None
                                       and op["anchorTransition"]["anchorHash"] == anchor_contract.anchor_hash
                                       and op.get("activityId") != anchor_contract.document["assertionActivityId"],
                                       "source-anchor-operation-not-proved")
                    except inputs.IncrementalHold as error:
                        return _receipt(connection, scope, run_hash, "held", {"reason": str(error)})
                with connection.transaction():
                    _lock_identity_writer(connection)
                    _attempt(connection, oid, "prepared", {"prestateHash": content_hash(current), "payloadHash": op["payloadHash"]})
                try:
                    require_writer(scope, client)
                    with connection.transaction():
                        _lock_identity_writer(connection)
                    with incremental_writer_context(**writer_bindings):
                        client.save_activities(creates=[op["payload"]])
                    if after_api:
                        after_api()
                except Exception:
                    with connection.transaction():
                        _lock_identity_writer(connection)
                        _attempt(connection, oid, "uncertain", {"reason": "remote-response-or-ack-unavailable"})
                    return _receipt(connection, scope, run_hash, "uncertain", {"reason": "remote-response-or-ack-unavailable"})
                latest = projection.app_state(scope, client)
                matches = [row for row in latest["rows"] if row.get("idempotencyKey") == op["key"]]
                if len(matches) != 1 or not projection.matches_post(matches[0], op):
                    return _receipt(connection, scope, run_hash, "uncertain", {"reason": "app-poststate-not-proved"})
                observed = matches[0]
                rest = {row["id"]: row for row in latest["rows"] if row["id"] != observed["id"]}
                if rest != {key: value for key, value in expected.items() if key != op["activityId"]}:
                    return _receipt(connection, scope, run_hash, "uncertain", {"reason": "intervening-app-work"})
            with connection.transaction():
                _lock_identity_writer(connection)
                qualification = next(item for item in body["qualifications"]
                                     if item["canonicalEventId"] == op["canonicalEventId"])
                basis = {
                    "source_day": qualification["sourceDay"], "signed_amount": qualification["signedAmount"],
                    "currency": qualification["currency"], "source_revision_number": op["revisionNumber"],
                    "source_generation_event_id": _uuid("event", body["generationHash"], op["canonicalEventId"]),
                    "observed_activity": observed, "activity_id": observed["id"], "original_projection_binding_id": None,
                    **op.get("representation", {"representation_kind": "full", "base_source_amount": "0"}),
                }
                _bind(connection, scope, run_hash, op["acceptedEventId"], observed, basis=basis, operation_id=oid)
                _attempt(connection, oid, "applied", {"observed": observed, "recovered": bool(recovered)})
            expected[observed["id"]] = observed
            inputs.write_receipt(scope.root, f"incremental/attempts/{oid}/applied.json",
                                 {"operationId": str(oid), "observed": observed})
        final = projection.app_state(scope, client)
        if {row["id"]: row for row in final["rows"]} != expected or projection.cash(final["rows"]) != inputs.money(body["sourceBalance"]):
            return _receipt(connection, scope, run_hash, "uncertain", {"reason": "final-account-reconciliation-failed"})
        checkpoint = None
        if anchor_contract:
            with connection.transaction():
                _lock_identity_writer(connection)
                checkpoint = source_anchor.finish(
                    connection, scope, anchor_contract, body, body["snapshotHash"],
                    body["receiptHash"], body["sourceBalance"], final,
                )
        return _receipt(connection, scope, run_hash, "applied", {
            "accountStateHash": content_hash(final), "cash": str(projection.cash(final["rows"])),
            "sourceBalance": body["sourceBalance"], "operationCount": len(operations),
            "heldCount": body.get("heldCount", 0), "eligibleBatchOnly": True,
            "historicalBackfillCount": body.get("historicalBackfillCount", 0),
            "cashTransitionCount": body.get("cashTransitionCount", len(operations)),
            "sourceCheckpointHash": checkpoint,
            **reconciliation_diagnostics(body),
            "journaledOperationCount": len(operations), "appliedOperationCount": len(operations),
        })


def run(connection, scope, client, *, now=None):
    pending = _unfinished(connection, scope.scope_id)
    if pending:
        inputs.require(len(pending) == 1, "multiple-unfinished-scope-runs")
        return apply(connection, scope, client, pending[0]["run_hash"], now=now)
    prepared = plan(connection, scope, client, now=now)
    if prepared["state"] == "pending":
        return apply(connection, scope, client, prepared["runHash"], now=now)
    return prepared
