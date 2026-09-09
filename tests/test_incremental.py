"""Synthetic producer -> resolver -> PostgreSQL -> authenticated REST proof.

Opt in with FINANCE_INCREMENTAL_POSTGRES_TEST=1. No external DSN is accepted.
The disposable files stay in this checkout's owned test directory and are
removed; the modeled public checkout is a separate path in that fixture.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
from threading import Thread
import time
import uuid

import pytest

from finance_store import incremental
from finance_store import incremental_bootstrap as bootstrap
from finance_store import incremental_anchor as source_anchor
from finance_store import incremental_inputs as inputs
from finance_store.domain import content_hash
from finance_store.source_admission import organization_scope_id, organization_scope
from importers.lineage_review import canonical
from importers.monarch.wealthfolio_client import WealthfolioClient
from importers.monarch.mutation_guard import (
    INCREMENTAL_WRITER_MODE_VALUE, MutationInterlockError, WRITER_MARKER_RELATIVE,
    current_incremental_release, incremental_writer_context, writer_marker_hash,
)
from importers.normalized import builder
from importers.rebuild.safety import instance_fingerprint
from importers.rebuild import bounded_promotion as promotion
from importers.simplefin.application import ledger_fingerprint
from importers.simplefin.collection_status import build_receipt, write_receipt
from tests.test_normalized import account as account_fact

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 2, 11, 12, tzinfo=timezone.utc)
pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_INCREMENTAL_POSTGRES_TEST") != "1",
    reason="requires owned disposable PostgreSQL and authenticated synthetic REST",
)


def docker(*args, **kwargs):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


@pytest.fixture(scope="module")
def pg_server():
    name = f"finance-incremental-test-{uuid.uuid4().hex[:12]}"
    env = dict(os.environ, POSTGRES_PASSWORD=secrets.token_hex(24))
    started = False
    try:
        docker("run", "--detach", "--rm", "--name", name, "--label", "finance.test=incremental",
               "--publish", "127.0.0.1::5432", "--env", "POSTGRES_PASSWORD",
               "--env", "POSTGRES_DB=incremental_template",
               "--mount", "type=tmpfs,destination=/var/lib/postgresql/data",
               "postgres:17.11-bookworm", env=env)
        started = True
        port = int(docker("port", name, "5432/tcp").rsplit(":", 1)[1])
        for _ in range(60):
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-q", "-h", "127.0.0.1", "-U", "postgres"],
                                   capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("owned PostgreSQL did not start")
        provision = (ROOT / "deploy" / "postgres" / "scripts" / "provision-logins.sh").read_text()
        sql = provision.split("<<'SQL'\n", 1)[1].split("\nSQL\n", 1)[0]
        role_env = dict(os.environ, **{f"FINANCE_SHADOW_{role}_PASSWORD": env["POSTGRES_PASSWORD"]
                                      for role in ("LOADER", "AGENT", "BACKUP")})
        role_args = ["--env", "FINANCE_SHADOW_LOADER_PASSWORD", "--env", "FINANCE_SHADOW_AGENT_PASSWORD",
                     "--env", "FINANCE_SHADOW_BACKUP_PASSWORD"]
        docker("exec", "-i", *role_args, name, "psql", "-X", "-q", "-U", "postgres", "-d", "incremental_template",
               "-v", "ON_ERROR_STOP=1", "-v", "roles_only=true", input=sql, env=role_env)
        for path in sorted((ROOT / "deploy" / "postgres" / "migrations").glob("*.sql")):
            text = path.read_text()
            docker("exec", "-i", name, "psql", "-X", "-q", "-U", "postgres", "-d", "incremental_template",
                   "-v", "ON_ERROR_STOP=1", "-v", f"migration_version={path.name.split('_', 1)[0]}",
                   "-v", f"migration_name={path.stem.split('_', 1)[1]}",
                   "-v", f"migration_checksum={hashlib.sha256(text.encode()).hexdigest()}",
                   "-v", "shadow_environment=synthetic-shadow-integration", input=text)
        docker("exec", "-i", *role_args, name, "psql", "-X", "-q", "-U", "postgres", "-d", "incremental_template",
               "-v", "ON_ERROR_STOP=1", "-v", "roles_only=false", input=sql, env=role_env)
        docker("exec", name, "psql", "-X", "-q", "-U", "postgres", "-d", "postgres",
               "-v", "ON_ERROR_STOP=1", "-c", "CREATE DATABASE incremental_schema_check TEMPLATE incremental_template")
        docker("exec", "-i", name, "psql", "-X", "-q", "-U", "postgres", "-d", "incremental_schema_check",
               "-v", "ON_ERROR_STOP=1",
               input=(ROOT / "deploy" / "postgres" / "integration" / "schema_test.sql").read_text())
        docker("exec", name, "psql", "-X", "-q", "-U", "postgres", "-d", "postgres",
               "-v", "ON_ERROR_STOP=1", "-c", "DROP DATABASE incremental_schema_check")
        yield {"host": "127.0.0.1", "port": port, "user": "postgres", "password": env["POSTGRES_PASSWORD"]}
    finally:
        if started:
            docker("rm", "--force", name)


@pytest.fixture
def database(pg_server):
    import psycopg
    from psycopg import sql
    name = f"incremental_case_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(**pg_server, dbname="postgres", autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE incremental_template").format(sql.Identifier(name)))
        try:
            yield dict(pg_server, dbname=name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def connection(database):
    import psycopg
    with psycopg.connect(**database, autocommit=True) as connection:
        connection.execute("SET ROLE finance_shadow_ingest")
        yield connection


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True)
    path.write_text(value, encoding="utf-8")
    return path


class App:
    def __init__(self):
        self.account = {"id": "SYN-APP", "name": "Synthetic Checking", "accountType": "CASH",
                        "isActive": True, "isArchived": False, "currency": "USD", "trackingMode": "TRANSACTIONS",
                        "unknownAccountField": {"retain": "synthetic"}}
        self.rows, self.assignments, self.backups, self.mutations = [], {}, {}, []
        self.activity_write_requests = []
        self.zone = "America/Chicago"
        self.authorized_reads = 0

    def activity(self, key, amount, *, identifier=None, **extra):
        row = {"id": identifier or str(uuid.uuid4()), "accountId": "SYN-APP",
               "activityType": "DEPOSIT" if amount >= 0 else "WITHDRAWAL",
               "date": "2026-01-15T12:00:00Z", "amount": abs(amount), "currency": "USD",
               "status": "POSTED", "isUserModified": False, "comment": "Synthetic note",
               "idempotencyKey": key, "assetId": None, "metadata": {"opaque": "SYN-KEEP"},
               "unknownField": {"keep": True}, "createdAt": "2026-01-16T00:00:00Z", **extra}
        self.rows.append(row)
        self.assignments[row["id"]] = [{"id": "SYN-ASSIGN", "taxonomyId": "SYN-TAX",
                                       "categoryId": "SYN-CAT", "source": "manual",
                                       "updatedAt": "2026-01-16T00:00:00"}]
        return row

    def backup(self):
        with sqlite3.connect(":memory:") as db:
            for table in ("accounts", "activities", "assets", "app_settings"):
                db.execute(f"CREATE TABLE {table}(id text)")
            db.execute("CREATE TABLE __diesel_schema_migrations(version text,run_on text)")
            db.execute("INSERT INTO __diesel_schema_migrations VALUES ('synthetic-schema','2026-01-01')")
            db.commit()
            raw = db.serialize()
        filename = f"wealthfolio_backup_20260211_1200{len(self.backups):02d}.db"
        self.backups[filename] = raw
        return {"filename": filename}


@pytest.fixture
def app():
    state = App()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            route = self.path.removeprefix("/api/v1")
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            if route == "/auth/login" and self.command == "POST":
                assert body["password"] == "synthetic-password"
                self.send_response(200)
                self.send_header("Set-Cookie", "wf_session=synthetic-session; HttpOnly; Path=/")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            if "wf_session=synthetic-session" not in self.headers.get("Cookie", ""):
                self.send_error(401)
                return
            state.authorized_reads += 1
            if route.startswith("/activities") and route != "/activities/search" and self.command != "GET":
                state.activity_write_requests.append((self.command, route))
            raw = None
            if route == "/app/info":
                answer = {"version": "3.7.0", "dbPath": "/synthetic/wealthfolio.db"}
            elif route.startswith("/accounts"):
                answer = [state.account]
            elif route == "/settings":
                answer = {"timezone": state.zone}
            elif route == "/activities/search":
                start = body["page"] * body["pageSize"]
                answer = {"data": state.rows[start:start + body["pageSize"]]}
            elif route.startswith("/spending/activities/"):
                answer = state.assignments.get(route.split("/")[3], [])
            elif route == "/utilities/database/backup":
                answer = state.backup()
            elif route == "/utilities/database/backups":
                answer = [{"filename": name, "sizeBytes": len(value)} for name, value in state.backups.items()]
            elif route.endswith("/download"):
                raw = state.backups[route.split("/")[-2]]
                answer = None
            elif route == "/activities/bulk":
                assert body["deleteIds"] == []
                for value in body["creates"]:
                    if not any(row["idempotencyKey"] == value["idempotencyKey"] for row in state.rows):
                        row = deepcopy(value)
                        row["date"] = row.pop("activityDate")
                        row.update(id=str(uuid.uuid4()), isUserModified=False)
                        state.rows.append(row)
                for value in body["updates"]:
                    matching = [row for row in state.rows if row["id"] == value["id"]]
                    assert len(matching) == 1
                    row = deepcopy(value)
                    row["date"] = row.pop("activityDate")
                    row.pop("isUserModified", None)
                    matching[0].update(row)
                    # Parent's independent empty-instance 3.7 probe established
                    # these lossy semantics. Do not model this as safe PATCH.
                    matching[0]["comment"] = value.get("comment")
                    matching[0]["isUserModified"] = True
                    matching[0]["updatedAt"] = "2026-02-11T12:00:01Z"
                state.mutations.append(deepcopy(body))
                answer = {}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream" if raw else "application/json")
            self.end_headers()
            self.wfile.write(raw if raw is not None else json.dumps(answer).encode())

        do_GET = do_POST = do_PUT = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.origin = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def estate(monkeypatch, app, connection):
    root = ROOT / f".incremental-fixture-{uuid.uuid4().hex[:12]}"
    root.mkdir()
    fake_checkout = root / "modeled-public-checkout"
    monkeypatch.setattr(inputs, "REPO_ROOT", fake_checkout)
    monkeypatch.setattr(builder, "REPO_ROOT", fake_checkout)
    for key in ("FINANCE_DATA", "WEALTHFOLIO_DATA", "WF_DATA_DIR", "WEALTHFOLIO_WRITER_OWNERSHIP_MARKER",
                "WEALTHFOLIO_WRITER_ENVIRONMENT_ID", "WEALTHFOLIO_WRITER_MODE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WEALTHFOLIO_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("WEALTHFOLIO_WRITER_MODE", INCREMENTAL_WRITER_MODE_VALUE)
    monkeypatch.setenv("WEALTHFOLIO_WRITER_ENVIRONMENT_ID", content_hash("synthetic-writer"))
    fixture = Fixture(root, app, fake_checkout)
    fixture.monkeypatch = monkeypatch
    db, identifier, environment = connection.execute(
        "SELECT current_database(),instance_id,environment_marker FROM finance.shadow_authority_metadata"
    ).fetchone()
    fixture.postgres = {"database": db, "instanceId": str(identifier), "environment": environment}
    try:
        yield fixture
    finally:
        for path in root.rglob("*"):
            if path.is_file():
                path.chmod(0o666)
        shutil.rmtree(root)


class Fixture:
    def __init__(self, root, app, repo):
        self.root, self.app, self.repo = root, app, repo
        self.raw_account = {"id": "SYN-SF", "name": "Synthetic Checking",
                            "org": {"name": "Synthetic Bank", "domain": "synthetic.example"},
                            "currency": "USD", "balance": "0", "balance-date": int(NOW.timestamp()),
                            "transactions": []}
        self.connection_id = organization_scope_id(self.raw_account, "1")
        self.fact_path = write(root / "facts" / "account.json", account_fact("SYN-CANONICAL", "Synthetic Checking"))
        self.map_path = write(root / "simplefin" / "account-map.json", {
            "version": 1, "accounts": {"SYN-SF": {"action": "import",
                "wealthfolioAccountId": "SYN-APP", "assertionAccountId": "SYN-CANONICAL"}},
        })
        self.client = WealthfolioClient(app.origin, writer_data_dir=root)
        self.client.login("synthetic-password")
        self.counter = 0

    def transaction(self, name="A", amount="-10.00", pending=False):
        return {"id": name, "amount": amount, "posted": int(datetime(2026, 1, 15, 12, tzinfo=timezone.utc).timestamp()),
                "pending": pending, "description": f"Synthetic merchant {name}"}

    def baseline(self, transactions, *, extra_rows=(), authority=None):
        root = self.root / "baseline"
        raw = deepcopy(self.raw_account)
        raw["transactions"] = transactions
        raw["balance-date"] = int((NOW - timedelta(days=1)).timestamp())
        original = write(root / "raw" / "simplefin" / "2026-02-10" / "simplefin-120000.json", {"accounts": [raw], "errors": []})
        rows = [builder.TransactionRow(
            datetime.fromtimestamp(item["posted"], timezone.utc).date().isoformat(),
            "SYN-CANONICAL", item["amount"], item["description"],
            f"simplefin:SYN-SF:{item['id']}", original.relative_to(root).as_posix(),
            excluded=item["pending"], exclusion_reason="pending transaction" if item["pending"] else "",
            transaction_kind="expense",
        ) for item in transactions]
        rows.extend(extra_rows)
        sources = {original}
        copied_fact = write(root / "facts" / "account.json", self.fact_path.read_text())
        sources.add(copied_fact)
        if authority:
            path = write(root / "identity" / "source-authority.json", {"coverageIntervals": authority})
            sources.add(path)
        for row in extra_rows:
            sources.add(root / row.source_file)
        projection = canonical.project(root, rows, rows, (), repo_root=self.repo,
                                       source_artifact_hashes={path.relative_to(root).as_posix(): inputs.digest(path)
                                                               for path in sources})
        estate = builder.Estate(
            [builder.AccountRow("SYN-CANONICAL", "Synthetic Bank", "Synthetic Checking",
                                self.app.account["accountType"], self.app.account["currency"])],
            [builder.TransactionRow(**row) for row in projection["rows"]], [], [],
            sources, [], {}, transaction_observations=projection["observations"],
            transaction_lineage=projection["lineage"], lineage_review=projection["summary"],
            split_review={"groups": []},
        )
        files = builder._data_documents(estate)
        directory = root / "normalized" / "canonical"
        directory.mkdir(parents=True)
        for name, raw in files.items():
            (directory / name).write_bytes(raw)
        manifest = builder._summary(estate, root)
        manifest["dataFiles"] = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
        write(directory / "manifest.json", manifest)
        config = {
            "schemaVersion": 1, "kind": "incremental-cash-scope", "dataRoot": str(self.root),
            "canonicalAccountId": "SYN-CANONICAL", "sourceAccountId": "SYN-SF",
            "sourceConnectionId": self.connection_id, "wealthfolioAccountId": "SYN-APP",
            "rawConnectionId": organization_scope(self.raw_account, "1"),
            "origin": self.app.origin, "instanceId": instance_fingerprint(self.client, self.app.origin),
            "timezone": "America/Chicago", "writerEnvironmentId": content_hash("synthetic-writer"),
            "maxSnapshotAgeSeconds": 7200, "maxBalanceAgeSeconds": 7200,
            "postgres": self.postgres,
            "accountMap": {"path": str(self.map_path.relative_to(self.root)), "sha256": inputs.digest(self.map_path)},
            "accountFact": {"path": str(self.fact_path.relative_to(self.root)), "sha256": inputs.digest(self.fact_path)},
            "baselineRoot": "baseline",
            "baselineManifestSha256": inputs.digest(directory / "manifest.json"),
        }
        write(self.root / "scope.json", config)
        self.scope = inputs.load_scope(self.root, "scope.json")
        self.install_marker(self.scope)
        return self.scope

    def install_marker(self, scope):
        """Model the operator, not the worker, installing reviewed scope ownership."""
        marker = {
            "schemaVersion": 2, "mode": "incremental-projector-only",
            "writerModeToken": INCREMENTAL_WRITER_MODE_VALUE,
            "origin": self.app.origin, "instanceId": instance_fingerprint(self.client, self.app.origin),
            "environmentId": scope.config["writerEnvironmentId"], "release": current_incremental_release(),
            "scopes": [{"scopeId": scope.scope_id, "configurationHash": scope.config_hash}],
            "activatedAt": NOW.isoformat(),
        }
        marker["markerHash"] = writer_marker_hash(marker)
        write(self.root / WRITER_MARKER_RELATIVE, marker)
        return marker

    def snapshot(self, transactions, balance, *, now=NOW, errors=None, account_changes=None, other_accounts=(),
                 requested_start=None):
        self.counter += 1
        raw = deepcopy(self.raw_account)
        raw.update(transactions=transactions, balance=str(balance), **{"balance-date": int(now.timestamp())})
        raw.update(account_changes or {})
        payload = {"accounts": [raw, *other_accounts], "errors": errors or []}
        path = write(self.root / "raw" / "simplefin" / now.date().isoformat()
                     / f"simplefin-{now:%H%M%S}-{self.counter:06d}.json", payload)
        request = path.with_name("request-" + path.stem.removeprefix("simplefin-") + ".json")
        start = requested_start or max(datetime(2026, 1, 1, tzinfo=timezone.utc).date(), now.date() - timedelta(days=89))
        write(request, {"schemaVersion": 1, "protocolVersion": 1, "requestedStart": start.isoformat(),
                        "requestedEnd": now.date().isoformat()})
        receipt = build_receipt(self.root, path, payload, requested_days=(now.date() - start).days + 1,
                                observed_at=now, release_commit="synthetic")
        write_receipt(self.root, receipt)
        return receipt

    def duet(self):
        from tests.test_identity_source_authority import evidence
        txn = self.transaction()
        path = write(self.root / "baseline" / "extracts" / "synthetic.ofx",
                     "<OFX><CURDEF>USD<STMTTRN><DTPOSTED>20260115<TRNAMT>-10.00"
                     "<FITID>SYN-FITID<NAME>Synthetic merchant A</STMTTRN></OFX>")
        ofx = builder.TransactionRow(
            "2026-01-15", "SYN-CANONICAL", "-10.00", txn["description"],
            "extract:stable:SYN-FITID", "extracts/synthetic.ofx", transaction_kind="expense",
        )
        raw = deepcopy(self.raw_account)
        raw.update(transactions=[txn], **{"balance-date": int((NOW - timedelta(days=1)).timestamp())})
        raw_hash = hashlib.sha256(json.dumps({"accounts": [raw], "errors": []}, sort_keys=True).encode()).hexdigest()
        authority = [
            evidence(family="ofx", strength="stable-provider-id", count=1,
                     account="SYN-CANONICAL", connection="ofx-account-scoped",
                     extracted_at="2026-02-10T12:00:00+00:00", freshness_as_of="2026-02-10T12:00:00+00:00",
                     source_hashes=[inputs.digest(path)]),
            evidence(family="simplefin", strength="posted-observation", count=1,
                     account="SYN-SF", canonical_account="SYN-CANONICAL", connection="simplefin-account-scoped",
                     extracted_at="2026-02-10T12:00:00+00:00", freshness_as_of="2026-02-10T12:00:00+00:00",
                     source_hashes=[raw_hash]),
        ]
        scope = self.baseline([txn], extra_rows=(ofx,), authority=authority)
        assert len(scope.baseline.canonical_events) == 1
        return scope

    def bootstrap(self, scope, *, start="2026-01-15", through="2026-01-16", maximum=20,
                  source_anchor_spec=None, assertion_id=None, legacy=False):
        """Bind synthetic exact prior receipts and a signed portable repair export.

        Export signatures are verified by the real consumer; no history-verifier
        or bootstrap permission function is patched. Production exporters use
        export_repair_history's existing completed-execution verifier.
        """
        now = "2026-01-15T10:00:00+00:00"
        removed_alias = "simplefin:SYN-APP:REMOVED"
        removed = {
            "accountId": "SYN-APP", "activityType": "WITHDRAWAL",
            "activityDate": "2026-01-14T12:00:00Z", "amount": 7, "currency": "USD",
            "comment": "Synthetic old source", "idempotencyKey": removed_alias,
        }
        compensation = self.app.activity("gap:synthetic-existing-bootstrap-compensation", 0,
                                         identifier="SYN-BOOTSTRAP-COMPENSATION")
        before = deepcopy(self.app.rows)
        post = before + [{"id": "SYN-REMOVED-ACTIVITY", "date": removed["activityDate"], **removed}]
        post = deepcopy(post)
        next(row for row in post if row["id"] == compensation["id"])["amount"] = 7
        gap_before = {**compensation, "activityDate": compensation["date"]}
        gap_after = {**gap_before, "amount": 7}
        reconciliation = {"accountId": "SYN-APP", "activityId": compensation["id"], "action": "update",
                          "before": gap_before, "after": gap_after}
        source = {
            "schemaVersion": 3, "generatedAt": now, "mode": "production-promotion-plan",
            # A historical absolute locator stays opaque; the consumer must
            # validate bound copies rather than following old-root paths.
            "evidence": {"snapshot": {"path": "Z:\\synthetic-history\\snapshot.json", "sha256": "a" * 64}},
            "environmentFingerprint": "b" * 64, "intentFingerprint": "c" * 64,
            "ledgerFingerprint": ledger_fingerprint(before, {"SYN-APP"}),
            "expectedPostLedgerFingerprint": ledger_fingerprint(post, {"SYN-APP"}),
            "decisionEvidence": {},
            "assertions": [{"accountId": "SYN-APP", "canonicalAccountId": "SYN-CANONICAL",
                            "sourceBalance": str(bootstrap.projection.cash(before)),
                            "beforeBalance": str(bootstrap.projection.cash(before))}],
            "operations": {"creates": [removed], "updates": [gap_after], "deleteIds": [], "metadataFinalizations": []},
            "reconciliations": [reconciliation],
        }
        if legacy:
            source.update(schemaVersion=1, portableIntent={}, ledgerAccountIds=["SYN-APP"],
                          links=[], manual=[], monitors=[], impact={}, spendingWindow={})
            source["operations"].pop("metadataFinalizations")
        source["planFingerprint"] = promotion.plan_fingerprint(source)
        source_path = write(self.root / "bootstrap" / "source-plan.json", source)
        receipt = {
            "schemaVersion": 3 if legacy else 5, "generatedAt": "2026-01-15T11:00:00+00:00", "status": "applied",
            "applicationPlanSha256": inputs.digest(source_path),
            "planFingerprint": source["planFingerprint"], "intentFingerprint": source["intentFingerprint"],
            "environmentFingerprint": source["environmentFingerprint"],
            "preLedgerFingerprint": source["ledgerFingerprint"], "postLedgerFingerprint": source["expectedPostLedgerFingerprint"],
            "decisionEvidence": {}, "operations": {key: len(value) for key, value in source["operations"].items()},
            "reconciliations": [reconciliation],
        }
        receipt_path = write(self.root / "bootstrap" / "source-receipt.json", receipt)
        head = content_hash("synthetic-completed-repair")
        history = {
            "schemaVersion": 1, "kind": bootstrap.REPAIR_HISTORY_KIND, "accountId": "SYN-APP",
            "originalTargetHash": content_hash("synthetic-original-target"),
            "sourceApplications": [{"planSha256": inputs.digest(source_path), "receiptSha256": inputs.digest(receipt_path)}],
            "steps": [{"executionHash": head, "previousExecutionHash": None,
                       "repairPlanHash": content_hash("synthetic-repair-plan"),
                       "accountLedgerFingerprint": ledger_fingerprint(before, {"SYN-APP"})}],
            "headExecutionHash": head, "accountLedgerFingerprint": ledger_fingerprint(before, {"SYN-APP"}),
            "deleted": [{"activityId": "SYN-REMOVED-ACTIVITY", "sourceIdentity": removed_alias}],
        }
        verification_key = b"synthetic-bootstrap-verification-key"
        key_env = "SYNTHETIC_INCREMENTAL_BOOTSTRAP_KEY"
        self.monkeypatch.setenv(key_env, verification_key.hex())
        history_path = write(self.root / "bootstrap" / "repairs.json", promotion.seal(history, verification_key))
        inventory_path = write(self.root / "bootstrap" / "inventory.json",
                               bootstrap.capture_inventory(scope, self.client, now=NOW))

        def file(path):
            return {"path": path.relative_to(self.root).as_posix(), "sha256": inputs.digest(path)}

        arguments = {
            "applications": [{"plan": file(source_path), "receipt": file(receipt_path)}],
            "repair_history": {"file": file(history_path), "headExecutionHash": head, "verificationKeyEnv": key_env},
            "inventory": file(inventory_path),
        }
        if source_anchor_spec:
            contract = source_anchor.build_contract(
                scope, **arguments, source_anchor_spec=source_anchor_spec,
                assertion_activity_id=assertion_id, maximum_transitions=maximum,
            )
        else:
            contract = bootstrap.build_contract(
                scope, **arguments, source_date_window={"from": start, "through": through},
                maximum_candidates=maximum,
            )
        contract_path = write(self.root / "bootstrap" / "contract.json", contract)
        config = json.loads((self.root / "scope.json").read_text())
        config["bootstrapSourceAnchor" if source_anchor_spec else "bootstrap"] = file(contract_path)
        write(self.root / "scope.json", config)
        self.scope = inputs.load_scope(self.root, "scope.json")
        self.install_marker(self.scope)
        self.bootstrap_contract = contract
        return self.scope

    def source_anchor(self, scope, transactions, *, balance="100", balance_at=None, assertion_amount=100,
                      errors=None, legacy=False):
        observed = datetime(2026, 2, 2, 11, tzinfo=timezone.utc)
        raw = {**deepcopy(self.raw_account), "transactions": transactions, "balance": balance,
               "balance-date": int((balance_at or observed.replace(hour=4)).timestamp())}
        path = write(self.root / "raw" / "simplefin" / "2026-02-02" / "simplefin-110000.json",
                     {"accounts": [raw], "errors": errors or []})
        request = write(path.with_name("request-110000.json"), {
            "schemaVersion": 1, "protocolVersion": 1,
            "requestedStart": "2026-01-10", "requestedEnd": "2026-01-15",
        })
        assertion = self.app.activity("rebuild:assertion:SYN-CANONICAL:2026-02-02",
                                       assertion_amount, identifier="SYN-CASH-ASSERTION",
                                       comment="Canonical current-balance assertion",
                                       date="2026-02-02T12:00:00Z")
        spec = {"snapshot": {"path": path.relative_to(self.root).as_posix(), "sha256": inputs.digest(path)},
                "request": {"path": request.relative_to(self.root).as_posix(), "sha256": inputs.digest(request)}}
        return self.bootstrap(scope, source_anchor_spec=spec, assertion_id=assertion["id"], legacy=legacy)


def test_historical_schema1_application_and_schema3_receipt_bind_real_source_anchor(estate):
    scope = estate.baseline([])
    scope = estate.source_anchor(scope, [estate.transaction()], legacy=True)
    contract = source_anchor.load_contract(scope)
    assert contract.removed == frozenset({"simplefin:SYN-SF:REMOVED"})
    assert contract.document["source"]["requestEvidence"] is not None


@pytest.mark.parametrize("problem", ["receipt-version", "missing-portable-intent", "wrong-plan-mode"])
def test_historical_bootstrap_keeps_version_and_shape_guards(estate, problem):
    scope = estate.source_anchor(estate.baseline([]), [estate.transaction()], legacy=True)
    contract = deepcopy(source_anchor.load_contract(scope).document)
    source_path = estate.root / contract["applications"][0]["plan"]["path"]
    receipt_path = estate.root / contract["applications"][0]["receipt"]["path"]
    plan, receipt = inputs.document(source_path), inputs.document(receipt_path)
    if problem == "receipt-version":
        receipt["schemaVersion"] = 5
    elif problem == "missing-portable-intent":
        plan.pop("portableIntent")
    else:
        plan["mode"] = "unrecognized"
    plan["planFingerprint"] = promotion.plan_fingerprint({k: v for k, v in plan.items() if k != "planFingerprint"})
    write(source_path, plan)
    receipt["planFingerprint"] = plan["planFingerprint"]
    receipt["applicationPlanSha256"] = inputs.digest(source_path)
    write(receipt_path, receipt)
    contract["applications"][0]["plan"]["sha256"] = inputs.digest(source_path)
    contract["applications"][0]["receipt"]["sha256"] = inputs.digest(receipt_path)
    with pytest.raises(inputs.IncrementalHold, match="bootstrap-(application|historical-application)"):
        bootstrap._history(scope, contract["applications"], contract["repairHistory"])


@pytest.mark.parametrize("anchor", [False, True])
@pytest.mark.parametrize("actionable", [False, True])
def test_unscoped_advisory_is_preserved_but_unknown_failure_still_holds(estate, anchor, actionable):
    scope = estate.baseline([])
    advisory = "Requested date range exceeds recommended range"
    errors = [advisory]
    if actionable:
        errors.append("Unrecognized provider failure")
    if anchor:
        def load():
            updated = estate.source_anchor(scope, [estate.transaction()], errors=errors)
            return source_anchor.load_contract(updated).document["source"]
        reason = "anchor-connection-not-clean"
    else:
        estate.snapshot([estate.transaction()], "-10", errors=errors)
        def load():
            return inputs.load_source(scope, NOW + timedelta(seconds=1)).admission
        reason = "unscopable-source-errors"
    if actionable:
        with pytest.raises(inputs.IncrementalHold, match=reason):
            load()
    else:
        assert load()["unscopedAdvisories"] == [advisory]


@pytest.mark.parametrize("has_effective_date", [True, False])
def test_legacy_epoch_pending_is_retained_unqualified_without_blocking_settlement(connection, estate, has_effective_date):
    pending = estate.transaction(pending=True)
    pending["posted"] = 0
    if has_effective_date:
        pending["transacted_at"] = int(NOW.timestamp())
    scope = estate.baseline([pending])
    estate.snapshot([estate.transaction()], "-10")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["state"] == "pending" and len(planned["operations"]) == 1
    retained = connection.execute("""
        SELECT admitted,reason FROM finance.incremental_source_versions
        WHERE scope_id=%s AND version_number=1
    """, (scope.scope_id,)).fetchone()
    assert retained == (False, "unverified-normalized-pending-date" if has_effective_date else "unavailable-pending")
    assert incremental.apply(connection, scope, estate.client, planned["runHash"],
                             now=NOW + timedelta(seconds=2))["state"] == "applied"


def test_readonly_projection_history_retains_actual_effect_after_noop(connection, estate):
    import psycopg

    scope = estate.baseline([])
    estate.snapshot([estate.transaction()], "-10")
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    query = """SELECT operation_state,run_state,planned_cash_effect,observed_activity_id
               FROM finance_read.incremental_projection_history WHERE scope_id=%s AND run_hash=%s"""
    before = connection.execute(query, (scope.scope_id, first["runHash"])).fetchone()
    assert before == ("pending", "pending", inputs.money("-10"), None)
    assert incremental.apply(connection, scope, estate.client, first["runHash"],
                             now=NOW + timedelta(seconds=2))["state"] == "applied"
    assert incremental.run(connection, scope, estate.client, now=NOW + timedelta(seconds=3))["state"] == "noop"
    connection.execute("SET ROLE finance_shadow_agent_readonly")
    try:
        after = connection.execute(query, (scope.scope_id, first["runHash"])).fetchone()
        assert after == ("applied", "applied", inputs.money("-10"), estate.app.rows[0]["id"])
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT * FROM finance.incremental_outbox")
    finally:
        connection.execute("SET ROLE finance_shadow_ingest")


def test_new_posted_exactly_once_with_real_client_and_native_journal(connection, estate):
    scope = estate.baseline([])
    estate.snapshot([estate.transaction()], "-10.00")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["state"] == "pending"
    assert len(planned["operations"]) == 1
    result = incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))
    assert result["state"] == "applied"
    assert len(estate.app.mutations) == 1
    assert estate.app.rows[0]["idempotencyKey"].startswith("finance:accepted:")
    assert estate.app.rows[0]["date"] == "2026-01-15T18:00:00+00:00"
    assert incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW)["replay"]
    replay = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert replay["state"] == "noop"
    assert len(estate.app.mutations) == 1
    assert connection.execute("SELECT count(*) FROM finance.incremental_attempts WHERE state='applied'").fetchone()[0] == 1


def test_identical_snapshot_is_sighting_not_financial_version(connection, estate):
    txn = estate.transaction()
    scope = estate.baseline([txn])
    original = deepcopy(scope.baseline.observations[0])
    row = estate.app.activity("simplefin:SYN-APP:A", -10)
    estate.snapshot([txn], "-10.00")
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert first["state"] == "noop"
    later = NOW + timedelta(minutes=10)
    estate.snapshot([txn], "-10.00", now=later)
    second = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert second["state"] == "noop"
    assert first["generationHash"] == second["generationHash"]
    stored = connection.execute("SELECT observation_document FROM finance.incremental_source_versions").fetchone()[0]
    assert inputs.observation_from_document(stored) == original
    assert connection.execute("SELECT count(*) FROM finance.incremental_source_sightings").fetchone()[0] == 2
    assert not estate.app.mutations and estate.app.rows == [row]


def test_same_provider_posted_correction_holds_before_lossy_api_and_preserves_all_fields(connection, estate):
    old = estate.transaction()
    scope = estate.baseline([old])
    row = estate.app.activity("simplefin:SYN-APP:A", -10, date="2026-01-15T00:00:00Z",
                              comment="Synthetic user note that is not the bank description")
    assignments = deepcopy(estate.app.assignments)
    before = deepcopy(row)
    estate.snapshot([old], "-10.00")
    initial = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    accepted_id = initial["qualifications"][0]["acceptedEventId"]
    changed = {**old, "amount": "-12.00"}
    later = NOW + timedelta(minutes=10)
    estate.snapshot([changed], "-12.00", now=later)
    planned = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert planned["state"] == "held" and not planned["operations"]
    qualification = planned["qualifications"][0]
    assert qualification["acceptedEventId"] == accepted_id
    assert qualification["reason"] == "upstream-update-contract-unsupported"
    assert qualification["proof"]["activityWriteContract"] == "create-only-v1"
    assert inputs.money(qualification["proof"]["deliveredSourceBasis"]["signed_amount"]) == inputs.money("-10")
    assert incremental.apply(connection, scope, estate.client, planned["runHash"], now=later + timedelta(seconds=1))["state"] == "held"
    assert row == before
    assert estate.app.assignments == assignments
    assert not estate.app.mutations and not estate.app.backups
    assert not estate.app.activity_write_requests
    assert connection.execute("SELECT count(*) FROM finance.incremental_outbox").fetchone()[0] == 0
    assert connection.execute(
        "SELECT reason,projection_status FROM finance_read.incremental_cash_events"
    ).fetchone() == ("upstream-update-contract-unsupported", "held")
    assert connection.execute("SELECT count(*) FROM finance.incremental_source_versions").fetchone()[0] == 2
    later += timedelta(minutes=5)
    estate.snapshot([changed], "-12.00", now=later)
    replay = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert replay["state"] == "held" and replay["heldCount"] == 1
    assert row == before and not estate.app.mutations


def test_pending_to_posted_creates_only_posted_cash_and_keeps_stable_identity(connection, estate):
    scope = estate.baseline([])
    pending = estate.transaction(pending=True)
    estate.snapshot([pending], "0")
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert first["state"] == "noop"
    assert first["qualifications"][0]["status"] == "pending"
    stable = first["qualifications"][0]["acceptedEventId"]
    assert not estate.app.mutations
    later = NOW + timedelta(minutes=10)
    estate.snapshot([{**pending, "pending": False}], "-10", now=later)
    second = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert second["operations"][0]["acceptedEventId"] == stable
    assert incremental.apply(connection, scope, estate.client, second["runHash"], now=later + timedelta(seconds=1))["state"] == "applied"
    assert len(estate.app.rows) == 1 and estate.app.rows[0]["status"] == "POSTED"


def test_missing_posted_source_is_retained_without_fresh_version_or_delete(connection, estate):
    old = estate.transaction()
    scope = estate.baseline([old])
    row = estate.app.activity("simplefin:SYN-APP:A", -10)
    estate.snapshot([old], "-10")
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    later = NOW + timedelta(minutes=10)
    estate.snapshot([], "-10", now=later)
    missing = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert missing["state"] == "noop"
    assert first["generationHash"] == missing["generationHash"]
    assert not missing["qualifications"][0]["proof"]["observedInSnapshot"]
    assert estate.app.rows == [row] and not estate.app.mutations
    observed, financial, sighted = connection.execute(
        "SELECT collection_observed_at,financial_observed_at,last_sighted_at FROM finance_read.incremental_cash_events"
    ).fetchone()
    assert observed > sighted > financial


def test_batch_balance_mismatch_does_not_choose_a_matching_subset_or_invent_gap(connection, estate):
    scope = estate.baseline([estate.transaction()])
    estate.app.activity("simplefin:SYN-APP:A", -10)
    estate.app.activity("gap:existing-reviewed-compensation", 100)
    before = deepcopy(estate.app.rows)
    estate.snapshot([estate.transaction(), estate.transaction("NEW", "-5")], "90")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["state"] == "held"
    assert planned["reason"] == "whole-eligible-batch-balance-mismatch"
    assert planned["startingCash"] == "90" and planned["predictedCash"] == "85"
    assert len(planned["operations"]) == 1
    assert connection.execute("SELECT count(*) FROM finance.incremental_outbox").fetchone()[0] == 0
    assert estate.app.rows == before and not estate.app.mutations


@pytest.mark.parametrize("failure", ["stale", "connection", "currency", "transaction-currency", "pending-state", "balance-stale", "account-missing", "investment"])
def test_ineligible_source_holds_without_projection(connection, estate, failure):
    scope = estate.baseline([])
    changes = {}
    txn = estate.transaction()
    errors = []
    now = NOW + timedelta(seconds=1)
    if failure == "stale":
        now = NOW + timedelta(days=4)
    if failure == "connection":
        errors = ["Synthetic Bank: authentication required"]
    if failure == "currency":
        changes["currency"] = None
    if failure == "transaction-currency":
        txn["currency"] = "EUR"
    if failure == "pending-state":
        txn["pending"] = "unknown"
    if failure == "balance-stale":
        changes["balance-date"] = int((NOW - timedelta(days=4)).timestamp())
    if failure == "account-missing":
        changes["id"] = "SYN-WRONG-ACCOUNT"
    if failure == "investment":
        changes["account_type"] = "investment"
    estate.snapshot([txn], "-10", errors=errors, account_changes=changes)
    result = incremental.plan(connection, scope, estate.client, now=now)
    assert result["state"] == "held"
    assert connection.execute("SELECT count(*) FROM finance.incremental_outbox").fetchone()[0] == 0
    assert not estate.app.rows and not estate.app.mutations


@pytest.mark.parametrize("change", ["user-modified", "split", "linked-transfer"])
def test_user_owned_and_linked_updates_are_held_locally(connection, estate, change):
    scope = estate.baseline([estate.transaction()])
    extra = {"isUserModified": True} if change == "user-modified" else (
        {"splitGroupId": "SYN-SPLIT"} if change == "split" else {"sourceGroupId": "SYN-TRANSFER"}
    )
    row = estate.app.activity("simplefin:SYN-APP:A", -10, **extra)
    estate.snapshot([estate.transaction(amount="-12")], "-12")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["state"] == "held"
    assert planned["qualifications"][0]["reason"] == (
        "upstream-update-contract-unsupported" if change == "user-modified" else "linked-activity-unsupported"
    )
    assert row["amount"] == 10 and not estate.app.mutations


def test_crash_after_http_commit_before_ack_is_observed_not_retried(connection, estate):
    scope = estate.baseline([])
    estate.snapshot([estate.transaction()], "-10")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))

    def crash():
        raise SystemExit("synthetic post-commit interruption")

    with pytest.raises(SystemExit):
        incremental.apply(connection, scope, estate.client, planned["runHash"],
                          now=NOW + timedelta(seconds=1), after_api=crash)
    assert len(estate.app.mutations) == 1
    assert connection.execute("SELECT state FROM finance.incremental_attempts ORDER BY event_number DESC LIMIT 1").fetchone()[0] == "prepared"
    recovered = incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))
    assert recovered["state"] == "applied"
    assert len(estate.app.mutations) == 1
    assert connection.execute(
        "SELECT evidence->>'recovered' FROM finance.incremental_attempts WHERE state='applied'"
    ).fetchone()[0] == "true"


def test_uncertain_remote_attempt_is_never_resent_when_poststate_is_missing(connection, estate, monkeypatch):
    scope = estate.baseline([])
    estate.snapshot([estate.transaction()], "-10")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    original = estate.client.save_activities
    monkeypatch.setattr(estate.client, "save_activities", lambda **kwargs: (_ for _ in ()).throw(TimeoutError()))
    result = incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))
    assert result["state"] == "uncertain"
    monkeypatch.setattr(estate.client, "save_activities", original)
    retry = incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))
    assert retry["state"] == "uncertain"
    assert not estate.app.mutations


def test_intervening_user_work_is_not_rolled_back_or_overwritten(connection, estate):
    scope = estate.baseline([estate.transaction()])
    old = estate.app.activity("simplefin:SYN-APP:A", -10)
    estate.snapshot([estate.transaction(amount="-12")], "-12")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    old["comment"] = "Synthetic legitimate edit after planning"
    result = incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))
    assert result["state"] == "held"
    assert old["amount"] == 10 and old["comment"].endswith("after planning")
    assert not estate.app.mutations


def test_global_worker_ownership_and_migration_gate_block_before_effects(connection, database, estate):
    import psycopg
    scope = estate.baseline([])
    estate.snapshot([estate.transaction()], "-10")
    with psycopg.connect(**database, autocommit=True) as other:
        with incremental.worker(connection):
            with pytest.raises(inputs.IncrementalHold, match="writer-busy"):
                incremental.plan(other, scope, estate.client, now=NOW + timedelta(seconds=1))
        other.execute("UPDATE finance.writer_gate SET migrations_blocked=true,owner_token='synthetic-migration' WHERE singleton")
        with pytest.raises(ValueError, match="blocked for migration"):
            incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert not estate.app.mutations


@pytest.mark.parametrize("change", ["archived", "excluded-fact", "mapping"])
def test_account_admission_is_not_an_approved_boolean(connection, estate, change):
    scope = estate.baseline([])
    estate.snapshot([estate.transaction()], "-10")
    if change == "archived":
        estate.app.account["isActive"] = False
        assert incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))["state"] == "held"
    else:
        if change == "excluded-fact":
            fact = json.loads(estate.fact_path.read_text())
            fact.update(excluded=True, reason="Synthetic exclusion")
            write(estate.fact_path, fact)
        else:
            mapping = json.loads(estate.map_path.read_text())
            mapping["accounts"]["SYN-SF"]["wealthfolioAccountId"] = "SYN-WRONG-APP"
            write(estate.map_path, mapping)
        with pytest.raises(inputs.IncrementalHold, match="evidence-hash-drift"):
            incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert not estate.app.mutations


def test_timezone_extreme_offset_uses_business_day_and_replay_keeps_instant(connection, estate):
    from zoneinfo import ZoneInfo
    estate.app.zone = "Pacific/Kiritimati"
    estate.baseline([])
    config = json.loads((estate.root / "scope.json").read_text())
    config["timezone"] = estate.app.zone
    write(estate.root / "scope.json", config)
    scope = inputs.load_scope(estate.root, "scope.json")
    estate.install_marker(scope)
    estate.snapshot([estate.transaction()], "-10")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
    instant = inputs.timestamp(estate.app.rows[0]["date"])
    assert instant.date().isoformat() == "2026-01-14"
    assert instant.astimezone(ZoneInfo(estate.app.zone)).date().isoformat() == "2026-01-15"
    assert incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))["state"] == "noop"


def test_closed_interval_and_deleted_alias_are_preserved_across_fresh_snapshots(connection, estate):
    scope = estate.duet()
    authority_bytes = (estate.root / "baseline" / "identity" / "source-authority.json").read_bytes()
    survivor = estate.app.activity("extract:SYN-APP:SYN-FITID", -10)
    estate.snapshot([estate.transaction()], "-10")
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert first["state"] == "noop"
    assert len(first["qualifications"]) == 1
    later = NOW + timedelta(minutes=20)
    estate.snapshot([estate.transaction()], "-10", now=later)
    replay = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert replay["state"] == "noop" and replay["generationHash"] == first["generationHash"]
    assert estate.app.rows == [survivor] and not estate.app.mutations
    assert not any(row["idempotencyKey"].startswith("simplefin:") for row in estate.app.rows)
    assert (estate.root / "baseline" / "identity" / "source-authority.json").read_bytes() == authority_bytes
    assert connection.execute("SELECT count(*) FROM finance.incremental_source_versions").fetchone()[0] == 2
    assert connection.execute("SELECT count(*) FROM finance.incremental_outbox").fetchone()[0] == 0


def test_multiple_legacy_rows_for_one_event_remain_local_projection_conflicts(connection, estate):
    scope = estate.duet()
    estate.app.activity("extract:SYN-APP:SYN-FITID", -10)
    estate.app.activity("simplefin:SYN-APP:A", -10)
    estate.app.activity("gap:synthetic-existing-compensation", 10)
    before = deepcopy(estate.app.rows)
    estate.snapshot([estate.transaction()], "-10")
    result = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert result["state"] == "held"
    assert result["qualifications"][0]["reason"] == "multiple-legacy-activities"
    assert not estate.app.mutations and estate.app.rows == before


def test_cross_source_correction_does_not_split_and_recreate_deleted_alias(connection, estate):
    scope = estate.duet()
    estate.app.activity("extract:SYN-APP:SYN-FITID", -10)
    estate.snapshot([estate.transaction()], "-10")
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert first["state"] == "noop"
    later = NOW + timedelta(minutes=10)
    estate.snapshot([estate.transaction(amount="-12")], "-12", now=later)
    result = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert result["state"] == "held"
    assert all(row["status"] == "held" for row in result["qualifications"])
    assert not estate.app.mutations and len(estate.app.rows) == 1


def test_failed_other_connection_does_not_block_this_healthy_account(connection, estate):
    scope = estate.baseline([])
    other = {"id": "SYN-FAILED", "org": {"name": "Failed Institution", "domain": "failed.example"},
             "currency": None, "transactions": [], "balance": None}
    estate.snapshot([estate.transaction()], "-10", errors=["Failed Institution: authentication required"],
                    other_accounts=(other,))
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["state"] == "pending"
    assert incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"


def test_recovery_can_acknowledge_old_commit_after_collector_advances(connection, estate):
    scope = estate.baseline([])
    estate.snapshot([estate.transaction()], "-10")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    with pytest.raises(SystemExit):
        incremental.apply(connection, scope, estate.client, planned["runHash"],
                          now=NOW + timedelta(seconds=1),
                          after_api=lambda: (_ for _ in ()).throw(SystemExit()))
    later = NOW + timedelta(minutes=10)
    estate.snapshot([estate.transaction(), estate.transaction("B", "-3")], "-13", now=later)
    recovered = incremental.apply(connection, scope, estate.client, planned["runHash"], now=later + timedelta(seconds=1))
    assert recovered["state"] == "applied" and len(estate.app.mutations) == 1
    next_plan = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert len(next_plan["operations"]) == 1
    assert incremental.apply(connection, scope, estate.client, next_plan["runHash"], now=later + timedelta(seconds=1))["state"] == "applied"
    assert len(estate.app.rows) == 2


def test_unattempted_old_payload_is_not_sent_after_collector_advances(connection, estate):
    scope = estate.baseline([])
    estate.snapshot([estate.transaction()], "-10")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    later = NOW + timedelta(minutes=10)
    estate.snapshot([estate.transaction(amount="-12")], "-12", now=later)
    result = incremental.apply(connection, scope, estate.client, planned["runHash"], now=later + timedelta(seconds=1))
    assert result["state"] == "held" and not estate.app.mutations


def test_cli_run_and_status_use_real_authenticated_client(database, estate, monkeypatch, capsys):
    from psycopg.conninfo import make_conninfo
    from finance_store.incremental_cli import main
    estate.baseline([])
    estate.snapshot([estate.transaction()], "-10")
    # The CLI clock is real; a synthetic current receipt retains the old source
    # business date but uses the actual test clock for collection/balance freshness.
    current = datetime.now(timezone.utc).replace(microsecond=0)
    txn = {**estate.transaction(), "posted": int((current - timedelta(days=1)).timestamp())}
    estate.snapshot([txn], "-10", now=current)
    monkeypatch.setenv("FINANCE_INCREMENTAL_DSN", make_conninfo(**database))
    monkeypatch.setenv("WEALTHFOLIO_PASSWORD", "synthetic-password")
    assert main(["run", "--data-dir", str(estate.root), "--scope", "scope.json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "applied" and result["operationCount"] == 1
    assert main(["status", "--data-dir", str(estate.root), "--scope", "scope.json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "applied"


def test_new_fact_file_cannot_be_ignored_by_an_old_scope(connection, estate):
    scope = estate.baseline([])
    write(estate.root / "facts" / "new-decision.json", {"type": "decision", "id": "SYN-NEW"})
    with pytest.raises(inputs.IncrementalHold, match="facts-catalog-drift"):
        incremental.plan(connection, scope, estate.client, now=NOW)


def test_native_constraints_reject_unbound_cash_qualification(connection, estate):
    import psycopg
    scope = estate.baseline([])
    estate.snapshot([estate.transaction()], "-10")
    incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    with pytest.raises(psycopg.errors.CheckViolation):
        connection.execute("""
            INSERT INTO finance.incremental_qualifications
            SELECT gen_random_uuid(),scope_id,run_hash,generation_event_id,accepted_event_id,revision_number,
                   source_day,signed_amount+1,currency_code,description,event_status,qualification_status,reason,proof
            FROM finance.incremental_qualifications LIMIT 1
        """)
    assert not estate.app.mutations


def test_credit_card_uses_signed_cash_ledger_without_performance_or_gap(connection, estate):
    estate.app.account["accountType"] = "CREDIT_CARD"
    fact = json.loads(estate.fact_path.read_text())
    fact["kind"] = "CREDIT_CARD"
    write(estate.fact_path, fact)
    scope = estate.baseline([])
    estate.snapshot([estate.transaction(amount="8")], "8")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["operations"][0]["payload"]["activityType"] == "CREDIT"
    assert planned["startingCash"] == "0" and planned["predictedCash"] == "8"
    assert incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
    assert len(estate.app.rows) == 1


def test_synthetic_37_contract_probe_reproduces_comment_loss_and_automation_flag(estate):
    scope = estate.baseline([estate.transaction()])
    row = estate.app.activity("simplefin:SYN-APP:A", -10)
    before = deepcopy(row)
    assignments = deepcopy(estate.app.assignments)
    # A direct operator probe of the synthetic endpoint, not a worker operation.
    with incremental_writer_context(**incremental.require_writer(scope, estate.client)):
        estate.client.save_activities(updates=[{
            "id": row["id"], "accountId": "SYN-APP", "activityType": "WITHDRAWAL",
            "activityDate": row["date"], "amount": 12, "currency": "USD",
        }])
    assert {key for key in before.keys() | row.keys() if before.get(key) != row.get(key)} == {
        "amount", "comment", "isUserModified", "updatedAt",
    }
    assert row["comment"] is None and row["isUserModified"] is True
    assert row["metadata"] == before["metadata"] and estate.app.assignments == assignments
    assert len(estate.app.mutations) == 1


def test_held_correction_never_resends_old_comment_over_concurrent_note(connection, estate):
    scope = estate.baseline([estate.transaction()])
    row = estate.app.activity("simplefin:SYN-APP:A", -10)
    estate.snapshot([estate.transaction(amount="-12")], "-12")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))

    row["comment"] = "Synthetic concurrent note"
    row["isUserModified"] = True
    before = deepcopy(row)
    result = incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))
    assert result["state"] == "held" and row == before
    assert not estate.app.mutations and not estate.app.backups
    assert not estate.app.activity_write_requests


@pytest.mark.parametrize("prepared", [False, True])
def test_queued_legacy_update_is_blocked_before_backup_or_http(connection, estate, prepared):
    scope = estate.baseline([estate.transaction()])
    row = estate.app.activity("simplefin:SYN-APP:A", -10)
    before = deepcopy(row)
    estate.snapshot([estate.transaction(amount="-12")], "-12")
    held = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert held["state"] == "held" and not held["operations"]
    historical = deepcopy(held)
    q = historical["qualifications"][0]
    q["status"], q["reason"] = "eligible", "synthetic-historical-update-intent"
    payload = {
        "id": row["id"], "accountId": "SYN-APP", "activityType": "WITHDRAWAL",
        "activityDate": row["date"], "amount": 12, "currency": "USD",
    }
    operation = {
        "kind": "update", "canonicalEventId": q["canonicalEventId"],
        "acceptedEventId": q["acceptedEventId"], "revisionNumber": q["revisionNumber"],
        "activityId": row["id"], "key": row["idempotencyKey"],
        "payload": payload, "payloadHash": content_hash(payload), "before": before,
        "delta": "-2", "assignments": estate.app.assignments[row["id"]],
        "deliveryBasis": q["proof"]["deliveredSourceBasis"],
    }
    historical.update(
        operations=[operation], state="pending", reason="synthetic-historical-update-intent",
        activityWriteContract="synthetic-previous-update-contract", predictedCash="-12", heldCount=0,
    )
    historical.pop("runHash")
    run_hash = historical["runHash"] = content_hash(historical)
    qid = incremental.stable_id("incremental_qualification", run_hash, q["canonicalEventId"])
    oid = incremental.stable_id("incremental_operation", run_hash, q["acceptedEventId"], operation["payloadHash"])
    # Model append-only historical intent with genuine source/resolver/FK evidence.
    # Do not alter production eligibility or bypass PostgreSQL validators.
    with connection.transaction():
        connection.execute("""
            INSERT INTO finance.incremental_runs(
                run_hash,scope_id,receipt_hash,snapshot_hash,manifest_hash,policy_hash,generation_id,
                source_observed_at,balance_effective_at,source_balance,currency_code,plan_document)
            SELECT %s,scope_id,receipt_hash,snapshot_hash,manifest_hash,policy_hash,generation_id,
                   source_observed_at,balance_effective_at,source_balance,currency_code,%s::jsonb
            FROM finance.incremental_runs WHERE run_hash=%s
        """, (run_hash, json.dumps(historical), held["runHash"]))
        connection.execute("""
            INSERT INTO finance.incremental_qualifications
            SELECT %s,scope_id,%s,generation_event_id,accepted_event_id,revision_number,
                   source_day,signed_amount,currency_code,description,event_status,'eligible',%s,proof
            FROM finance.incremental_qualifications WHERE run_hash=%s
        """, (qid, run_hash, q["reason"], held["runHash"]))
        connection.execute("""
            INSERT INTO finance.incremental_outbox(
                operation_id,run_hash,qualification_id,accepted_event_id,revision_number,
                operation_kind,payload_hash,operation_document)
            VALUES (%s,%s,%s,%s,%s,'update',%s,%s::jsonb)
        """, (oid, run_hash, qid, q["acceptedEventId"], q["revisionNumber"],
              operation["payloadHash"], json.dumps(operation)))
        incremental._event(connection, run_hash, "pending", {"reason": "synthetic-historical-update-intent"})
        if prepared:
            incremental._attempt(connection, oid, "prepared", {"payloadHash": operation["payloadHash"]})
    write(estate.root / "incremental" / "plans" / f"{run_hash}.json", historical)
    result = incremental.apply(connection, scope, estate.client, run_hash, now=NOW + timedelta(seconds=1))
    assert result["state"] == ("uncertain" if prepared else "held")
    assert result["evidence"]["reason"] == "upstream-update-contract-unsupported"
    assert result["evidence"]["activityWriteContract"] == "create-only-v1"
    assert row == before and not estate.app.mutations and not estate.app.backups
    assert not estate.app.activity_write_requests
    assert connection.execute("SELECT count(*) FROM finance.incremental_attempts").fetchone()[0] == int(prepared)


def test_held_date_correction_does_not_stop_independent_balanced_create(connection, estate):
    scope = estate.baseline([estate.transaction()])
    row = estate.app.activity("simplefin:SYN-APP:A", -10)
    before = deepcopy(row)
    changed = {**estate.transaction(), "posted": int(datetime(2026, 1, 16, 12, tzinfo=timezone.utc).timestamp())}
    estate.snapshot([changed, estate.transaction("SAFE", "-5")], "-15")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["state"] == "pending" and planned["heldCount"] == 1
    assert len(planned["operations"]) == 1 and planned["operations"][0]["kind"] == "create"
    held = next(q for q in planned["qualifications"] if q["status"] == "held")
    assert held["reason"] == "upstream-update-contract-unsupported"
    assert held["proof"]["deliveredSourceBasis"]["source_day"] == "2026-01-15"
    result = incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))
    assert result["state"] == "applied" and result["evidence"]["heldCount"] == 1
    assert row == before and len(estate.app.rows) == 2
    assert len(estate.app.mutations) == 1 and not estate.app.mutations[0]["updates"]


def test_failed_source_status_retains_last_known_materialization_and_balance(connection, estate):
    scope = estate.baseline([estate.transaction()])
    estate.app.activity("simplefin:SYN-APP:A", -10)
    estate.snapshot([estate.transaction()], "-10")
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    later = NOW + timedelta(minutes=10)
    estate.snapshot([], "-10", errors=["Synthetic Bank: authentication required"], now=later)
    assert incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))["state"] == "held"
    value, source_run, state = connection.execute(
        "SELECT source_balance,last_source_run_hash,state FROM finance_read.incremental_scope_status"
    ).fetchone()
    assert value == inputs.money("-10") and source_run == first["runHash"] and state == "held"
    assert connection.execute("SELECT signed_amount FROM finance_read.incremental_cash_events").fetchone()[0] == inputs.money("-10")


def test_closed_interval_remains_usable_when_a_new_posted_day_arrives(connection, estate):
    scope = estate.duet()
    survivor = estate.app.activity("extract:SYN-APP:SYN-FITID", -10)
    fresh = {**estate.transaction("NEW", "-5"), "posted": int(NOW.timestamp())}
    estate.snapshot([estate.transaction(), fresh], "-15")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["state"] == "pending" and len(planned["operations"]) == 1
    assert planned["heldCount"] == 0
    assert incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
    assert survivor in estate.app.rows and len(estate.app.rows) == 2
    assert not any(row["idempotencyKey"].startswith("simplefin:") for row in estate.app.rows)


def test_unresolved_baseline_is_not_certified_and_does_not_hide_safe_new_event(connection, estate):
    txn = estate.transaction()
    write(estate.root / "baseline" / "extracts" / "unresolved.ofx",
          "<OFX><CURDEF>USD<STMTTRN><FITID>SYN-UNRESOLVED</STMTTRN></OFX>")
    extra = builder.TransactionRow(
        "2026-01-15", "SYN-CANONICAL", "-10.00", txn["description"],
        "extract:stable:SYN-UNRESOLVED", "extracts/unresolved.ofx", transaction_kind="expense",
    )
    scope = estate.baseline([txn], extra_rows=(extra,))
    assert scope.baseline.report_document()["counts"]["unresolvedDuplicateGroups"]
    estate.app.activity("simplefin:SYN-APP:A", -10)
    estate.app.activity("extract:SYN-APP:SYN-UNRESOLVED", -10)
    estate.snapshot([txn, estate.transaction("SAFE", "-5")], "-25")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["state"] == "pending" and planned["heldCount"] == 2
    assert len(planned["operations"]) == 1
    assert incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
    assert len(estate.app.rows) == 3


def test_posted_regression_return_does_not_replace_original_financial_version(connection, estate):
    txn = estate.transaction()
    scope = estate.baseline([txn])
    estate.app.activity("simplefin:SYN-APP:A", -10)
    estate.snapshot([txn], "-10")
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    later = NOW + timedelta(minutes=10)
    estate.snapshot([{**txn, "pending": True}], "-10", now=later)
    regressed = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert regressed["state"] == "held" and regressed["heldSourceChanges"]
    later += timedelta(minutes=10)
    estate.snapshot([txn], "-10", now=later)
    restored = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert restored["state"] == "noop" and restored["generationHash"] == first["generationHash"]
    assert connection.execute("SELECT count(*) FROM finance.incremental_source_versions WHERE admitted").fetchone()[0] == 1
    assert not estate.app.mutations


def test_postgres_authority_mismatch_is_not_a_new_empty_registry(connection, estate):
    estate.baseline([])
    config = json.loads((estate.root / "scope.json").read_text())
    config["postgres"]["database"] = "synthetic-unrelated-database"
    write(estate.root / "scope.json", config)
    scope = inputs.load_scope(estate.root, "scope.json")
    estate.snapshot([estate.transaction()], "-10")
    with pytest.raises(inputs.IncrementalHold, match="postgres-authority-binding-mismatch"):
        incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert connection.execute("SELECT count(*) FROM finance.incremental_scopes").fetchone()[0] == 0


def test_plan_does_not_need_financial_interlock_but_apply_requires_real_writer_marker(connection, estate, monkeypatch):
    scope = estate.baseline([])
    estate.snapshot([estate.transaction()], "-10")
    monkeypatch.delenv("WEALTHFOLIO_MUTATIONS_ENABLED")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["state"] == "pending" and not estate.app.mutations
    (estate.root / WRITER_MARKER_RELATIVE).unlink()
    with pytest.raises(MutationInterlockError, match="schema-2 scoped ownership"):
        incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))
    assert not estate.app.mutations and not estate.app.backups


@pytest.mark.parametrize("change", ["amount", "date", "amount-and-date", "later-amount"])
def test_unsent_accepted_revision_never_replaces_delivered_predecessor(connection, estate, change):
    original = estate.transaction()
    scope = estate.baseline([original])
    activity = estate.app.activity("simplefin:SYN-APP:A", -10, date="2026-01-15T00:00:00Z")
    before = deepcopy(activity)
    changed = dict(original)
    if change in {"amount", "amount-and-date", "later-amount"}:
        changed["amount"] = "-12"
    if change in {"date", "amount-and-date"}:
        changed["posted"] = int(datetime(2026, 1, 16, 12, tzinfo=timezone.utc).timestamp())
    estate.snapshot([changed], "-99")
    held = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert held["state"] == "held" and not estate.app.mutations
    assert connection.execute(
        "SELECT source_revision_number FROM finance.incremental_projection_observations "
        "ORDER BY observation_number DESC LIMIT 1"
    ).fetchone()[0] == 1
    assert connection.execute("SELECT max(revision_number) FROM finance.accepted_identity_revisions").fetchone()[0] == 2
    later = NOW + timedelta(minutes=10)
    if change == "later-amount":
        changed["amount"] = "-13"
    estate.snapshot([changed], changed["amount"], now=later)
    ready = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert ready["state"] == "held" and not ready["operations"]
    qualification = ready["qualifications"][0]
    assert qualification["reason"] == "upstream-update-contract-unsupported"
    basis = qualification["proof"]["deliveredSourceBasis"]
    assert basis["source_revision_number"] == 1
    assert inputs.money(basis["signed_amount"]) == inputs.money("-10")
    assert basis["source_day"] == "2026-01-15"
    assert inputs.money(qualification["signedAmount"]) == inputs.money(changed["amount"])
    if change in {"date", "amount-and-date"}:
        assert qualification["sourceDay"] == "2026-01-16"
    assert incremental.apply(connection, scope, estate.client, ready["runHash"], now=later + timedelta(seconds=1))["state"] == "held"
    assert activity == before
    assert connection.execute(
        "SELECT source_revision_number FROM finance.incremental_projection_observations "
        "ORDER BY observation_number DESC LIMIT 1"
    ).fetchone()[0] == 1
    later += timedelta(minutes=5)
    estate.snapshot([changed], changed["amount"], now=later)
    assert incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))["state"] == "held"
    assert not estate.app.mutations and not estate.app.backups
    assert connection.execute("SELECT count(*) FROM finance.incremental_outbox").fetchone()[0] == 0


@pytest.mark.parametrize(("account_type", "old", "new", "original_type"), [
    ("CASH", "-10", "-12", "FEE"),
    ("CASH", "10", "12", "INTEREST"),
    ("CASH", "-10", "4", "WITHDRAWAL"),
    ("CASH", "10", "-4", "DEPOSIT"),
    ("CREDIT_CARD", "-10", "4", "WITHDRAWAL"),
    ("CREDIT_CARD", "10", "-4", "CREDIT"),
])
def test_ordinary_corrections_hold_without_altering_existing_type(connection, estate, account_type, old, new, original_type):
    estate.app.account["accountType"] = account_type
    fact = json.loads(estate.fact_path.read_text())
    fact["kind"] = account_type
    write(estate.fact_path, fact)
    scope = estate.baseline([estate.transaction(amount=old)])
    row = estate.app.activity("simplefin:SYN-APP:A", float(old), activityType=original_type)
    before = deepcopy(row)
    estate.snapshot([estate.transaction(amount=new)], new)
    ready = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert ready["state"] == "held" and not ready["operations"]
    assert ready["qualifications"][0]["reason"] == "upstream-update-contract-unsupported"
    assert incremental.apply(connection, scope, estate.client, ready["runHash"], now=NOW + timedelta(seconds=1))["state"] == "held"
    assert row == before and not estate.app.mutations


def test_baseline_currency_is_bound_to_exact_raw_transaction(connection, estate):
    baseline = estate.transaction()
    baseline["currency"] = "EUR"
    scope = estate.baseline([baseline])
    estate.app.activity("simplefin:SYN-APP:A", -10)
    estate.snapshot([estate.transaction()], "-10")
    with pytest.raises(inputs.IncrementalHold, match="transaction-currency-mismatch"):
        incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert connection.execute("SELECT count(*) FROM finance.incremental_source_versions").fetchone()[0] == 0
    assert not estate.app.mutations


def test_artifact_bound_verifier_objects_and_raw_transaction_proof_are_reused(connection, estate):
    from finance_store.canonical_identity import verified_resolution
    transaction = estate.transaction()
    scope = estate.baseline([transaction])
    verified = verified_resolution(scope.baseline_root, require_resolved=False)
    assert scope.baseline == verified.resolution
    original = scope.baseline.observations[0]
    assert original.attribute("sourceArtifactSha256")
    proof = inputs.baseline_currency_proof(scope, original)
    assert proof["artifactSha256"] == original.attribute("sourceArtifactSha256")
    assert proof["rawAccountId"] == "SYN-SF" and proof["rawTransactionId"] == "A"
    assert proof["transactionSha256"] == content_hash(transaction)
    estate.app.activity("simplefin:SYN-APP:A", -10)
    estate.snapshot([transaction], "-10")
    assert incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))["state"] == "noop"
    assert inputs.observation_from_document(connection.execute(
        "SELECT observation_document FROM finance.incremental_source_versions"
    ).fetchone()[0]) == original


def test_verified_canonical_key_is_adopted_and_kept_on_correction(connection, estate):
    scope = estate.baseline([estate.transaction()])
    canonical_key = f"canonical:{scope.canonical_id}:{scope.baseline.canonical_events[0].canonical_event_id}"
    row = estate.app.activity(canonical_key, -10)
    estate.snapshot([estate.transaction()], "-10")
    assert incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))["state"] == "noop"
    later = NOW + timedelta(minutes=10)
    estate.snapshot([estate.transaction(amount="-12")], "-12", now=later)
    ready = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert ready["state"] == "held" and not ready["operations"]
    assert ready["qualifications"][0]["reason"] == "upstream-update-contract-unsupported"
    assert incremental.apply(connection, scope, estate.client, ready["runHash"], now=later + timedelta(seconds=1))["state"] == "held"
    assert len(estate.app.rows) == 1 and row["idempotencyKey"] == canonical_key
    assert row["amount"] == 10 and not estate.app.mutations


def test_verified_application_binding_beats_missing_legacy_key_without_fuzzy_matching(connection, estate):
    from finance_store.identity_postgres import persist_identity_resolution, persist_application_projection_bindings
    scope = estate.baseline([estate.transaction()])
    row = estate.app.activity("canonical:historical-explicit-binding", -10)
    with connection.transaction():
        persist_identity_resolution(connection, scope.baseline)
        persist_application_projection_bindings(connection, scope.baseline, {
            scope.baseline.canonical_events[0].canonical_event_id: content_hash(row["id"]),
        })
    estate.snapshot([estate.transaction()], "-10")
    result = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert result["state"] == "noop" and not estate.app.mutations
    assert connection.execute("SELECT activity_id FROM finance.incremental_activity_bindings").fetchone()[0] == row["id"]


def test_unverified_canonical_prefix_is_not_an_adoption_proof(connection, estate):
    scope = estate.baseline([estate.transaction()])
    estate.app.activity("canonical:SYN-CANONICAL:unproved-event", -10)
    estate.snapshot([estate.transaction()], "-10")
    result = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert result["state"] == "held"
    assert result["qualifications"][0]["reason"] == "baseline-posted-activity-missing"
    assert not estate.app.mutations


def test_optional_simplefin_pending_uses_existing_posted_parser_semantics(connection, estate):
    transaction = estate.transaction()
    transaction.pop("pending")
    scope = estate.baseline([])
    estate.snapshot([transaction], "-10")
    ready = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert ready["state"] == "pending" and ready["qualifications"][0]["eventStatus"] == "posted"
    assert incremental.apply(connection, scope, estate.client, ready["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"


@pytest.mark.parametrize("timestamp_field", ["posted", "transacted_at"])
def test_explicit_pending_status_needs_no_pending_boolean(connection, estate, timestamp_field):
    transaction = estate.transaction()
    transaction.pop("pending")
    transaction["status"] = "pending"
    if timestamp_field == "transacted_at":
        transaction["transacted_at"] = transaction.pop("posted")
    scope = estate.baseline([])
    estate.snapshot([transaction], "0")
    result = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert result["state"] == "noop" and result["qualifications"][0]["status"] == "pending"
    assert not estate.app.mutations


@pytest.mark.parametrize("status", ["PENDING", "DRAFT", "VOID"])
def test_real_dto_only_posted_status_affects_cash(connection, estate, status):
    scope = estate.baseline([])
    untouched = estate.app.activity("manual:non-posted", 125, status=status)
    assert "isDraft" not in untouched
    estate.snapshot([estate.transaction()], "-10")
    ready = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert ready["startingCash"] == "0" and ready["state"] == "pending"
    assert ready["appBefore"]["account"]["unknownAccountField"] == {"retain": "synthetic"}
    assert incremental.apply(connection, scope, estate.client, ready["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
    assert untouched["status"] == status


@pytest.mark.parametrize("problem", ["missing-status", "unknown-status", "archived", "missing-archive"])
def test_missing_or_unsupported_real_dto_state_is_not_defaulted(connection, estate, problem):
    scope = estate.baseline([estate.transaction()])
    row = estate.app.activity("simplefin:SYN-APP:A", -10)
    if problem == "missing-status":
        row.pop("status")
    elif problem == "unknown-status":
        row["status"] = "unknown"
    elif problem == "archived":
        estate.app.account["isArchived"] = True
    else:
        estate.app.account.pop("isArchived")
    estate.snapshot([estate.transaction()], "-10")
    assert incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))["state"] == "held"
    assert not estate.app.mutations


@pytest.mark.parametrize("failure", ["scope", "configuration", "origin", "instance", "environment", "mode", "release", "marker-path"])
def test_real_schema2_guard_rejects_wrong_staging_bindings(connection, estate, monkeypatch, failure):
    scope = estate.baseline([])
    estate.snapshot([estate.transaction()], "-10")
    ready = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    path = estate.root / WRITER_MARKER_RELATIVE
    marker = json.loads(path.read_text())
    if failure == "scope":
        marker["scopes"][0]["scopeId"] = str(uuid.uuid4())
    elif failure == "configuration":
        marker["scopes"][0]["configurationHash"] = content_hash("wrong config")
    elif failure == "origin":
        marker["origin"] = "http://127.0.0.1:1"
    elif failure == "instance":
        marker["instanceId"] = content_hash("wrong instance")
    elif failure == "release":
        marker["release"]["codeHash"] = content_hash("wrong code")
    elif failure == "environment":
        monkeypatch.setenv("WEALTHFOLIO_WRITER_ENVIRONMENT_ID", content_hash("wrong environment"))
    elif failure == "mode":
        monkeypatch.setenv("WEALTHFOLIO_WRITER_MODE", "clean-canonical-projector-v1")
    else:
        monkeypatch.setenv("WEALTHFOLIO_WRITER_OWNERSHIP_MARKER", str(estate.root / "elsewhere.json"))
    marker["markerHash"] = writer_marker_hash(marker)
    write(path, marker)
    with pytest.raises(MutationInterlockError):
        incremental.apply(connection, scope, estate.client, ready["runHash"], now=NOW + timedelta(seconds=1))
    assert not estate.app.mutations and not estate.app.backups


def test_schema2_marker_does_not_authorize_an_unscoped_legacy_client(connection, estate):
    estate.baseline([])
    with pytest.raises(MutationInterlockError, match="scoped request context"):
        estate.client.save_activities(creates=[])
    assert not estate.app.mutations


def test_legacy_binding_supplies_delivered_basis_without_inventing_an_accepted_revision(connection, estate):
    from finance_store.identity import observations_from_transaction_rows, resolve_identity
    from finance_store.identity_postgres import persist_identity_resolution, persist_application_projection_bindings
    scope = estate.baseline([estate.transaction(amount="-12")])
    original = estate.transaction(amount="-10")
    path = write(estate.root / "historical" / "source.json",
                 {"accounts": [{**estate.raw_account, "transactions": [original]}]})
    old_observations = observations_from_transaction_rows([{
        "account_id": scope.canonical_id, "source_id": "simplefin:SYN-SF:A",
        "date": "2026-01-15", "amount": "-10", "currency": "USD",
        "description": original["description"], "source_file": "historical/source.json",
        "observed_at": "2026-02-09T12:00:00+00:00", "status": "posted",
    }], source_artifact_hashes={"historical/source.json": inputs.digest(path)})
    old = resolve_identity(old_observations, policy=scope.baseline.policy)
    row = estate.app.activity("canonical:verified-older-source", -10)
    with connection.transaction():
        persist_identity_resolution(connection, old)
        persist_application_projection_bindings(connection, old, {
            old.canonical_events[0].canonical_event_id: content_hash(row["id"]),
        })
    estate.snapshot([estate.transaction(amount="-12")], "-12")
    ready = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert ready["state"] == "held" and not ready["operations"]
    assert ready["qualifications"][0]["reason"] == "upstream-update-contract-unsupported"
    basis = ready["qualifications"][0]["proof"]["deliveredSourceBasis"]
    assert basis["source_revision_number"] is None
    assert basis["original_projection_binding_id"]
    assert inputs.money(basis["signed_amount"]) == inputs.money("-10")
    assert incremental.apply(connection, scope, estate.client, ready["runHash"], now=NOW + timedelta(seconds=1))["state"] == "held"
    assert len(estate.app.rows) == 1 and row["amount"] == 10 and row["activityType"] == "WITHDRAWAL"
    assert not estate.app.mutations


def _bootstrap_ahead(estate, *, two=False):
    first = estate.transaction("NEW", "-10")
    transactions = [first]
    if two:
        second = estate.transaction("SECOND", "-5")
        second["posted"] = int(datetime(2026, 1, 16, 12, tzinfo=timezone.utc).timestamp())
        transactions.append(second)
    scope = estate.baseline(transactions)
    estate.app.activity("extract:SYN-APP:OLD-SURVIVOR", -7, date="2026-01-14T12:00:00Z")
    return estate.bootstrap(scope), transactions


def test_reviewed_bootstrap_catches_up_source_baseline_ahead_of_app_and_replays(connection, estate):
    scope, transactions = _bootstrap_ahead(estate, two=True)
    assert len(estate.bootstrap_contract["candidates"]) == 2
    estate.snapshot(transactions, "-22")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["state"] == "pending" and len(planned["operations"]) == 2
    assert all(op["bootstrapProof"]["bootstrapHash"] == estate.bootstrap_contract["bootstrapHash"]
               for op in planned["operations"])
    assert incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
    assert len(estate.app.mutations) == 2
    assert len([row for row in estate.app.rows if row["idempotencyKey"].startswith("finance:accepted:")]) == 2
    later = NOW + timedelta(minutes=10)
    estate.snapshot(transactions, "-22", now=later)
    replay = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert replay["state"] == "noop" and not replay["operations"]
    assert len(estate.app.mutations) == 2
    assert next(row for row in estate.app.rows if row["id"] == "SYN-BOOTSTRAP-COMPENSATION")["amount"] == 0


def test_bootstrap_never_recreates_completed_repair_alias_even_when_not_in_baseline(connection, estate):
    scope, transactions = _bootstrap_ahead(estate)
    removed = estate.transaction("REMOVED", "-7")
    removed["posted"] = int(datetime(2026, 1, 14, 12, tzinfo=timezone.utc).timestamp())
    estate.snapshot([*transactions, removed], "-17")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["state"] == "pending" and len(planned["operations"]) == 1
    held = [row for row in planned["qualifications"] if row["status"] == "held"]
    assert any(row["reason"] == "bootstrap-known-removed-alias" for row in held)
    assert incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
    assert not any(row["idempotencyKey"] == "simplefin:SYN-APP:REMOVED" for row in estate.app.rows)
    assert len(estate.app.mutations) == 1


def test_bootstrap_does_not_recreate_its_own_later_deleted_bound_activity(connection, estate):
    scope, transactions = _bootstrap_ahead(estate)
    estate.snapshot(transactions, "-17")
    initial = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert incremental.apply(connection, scope, estate.client, initial["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
    created = next(row for row in estate.app.rows if row["idempotencyKey"].startswith("finance:accepted:"))
    estate.app.rows.remove(created)
    later = NOW + timedelta(minutes=10)
    estate.snapshot(transactions, "-17", now=later)
    missing = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert missing["state"] == "held"
    assert missing["qualifications"][0]["reason"] == "bound-activity-missing"
    assert len(estate.app.mutations) == 1


def test_bootstrap_still_requires_entire_eligible_batch_to_match_fresh_balance(connection, estate):
    scope, transactions = _bootstrap_ahead(estate, two=True)
    estate.snapshot(transactions, "-17")  # Only a subset would happen to fit.
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert len(planned["operations"]) == 2 and planned["predictedCash"] == "-22"
    assert planned["state"] == "held" and planned["reason"] == "whole-eligible-batch-balance-mismatch"
    assert connection.execute("SELECT count(*) FROM finance.incremental_outbox").fetchone()[0] == 0
    assert not estate.app.mutations
    assert next(row for row in estate.app.rows if row["id"] == "SYN-BOOTSTRAP-COMPENSATION")["amount"] == 0


def test_bootstrap_inventory_drift_is_not_an_allow_missing_switch(connection, estate):
    scope, transactions = _bootstrap_ahead(estate)
    estate.app.rows[0]["comment"] = "Synthetic edit after inventory review"
    estate.snapshot(transactions, "-17")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["state"] == "held"
    assert planned["qualifications"][0]["reason"] == "bootstrap-live-inventory-drift"
    assert not estate.app.mutations


def test_bootstrap_reviewed_source_version_is_not_a_permanent_source_id_allowlist(connection, estate):
    scope, transactions = _bootstrap_ahead(estate)
    changed = {**transactions[0], "amount": "-11"}
    estate.snapshot([changed], "-18")
    result = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert result["state"] == "held"
    assert result["qualifications"][0]["reason"] == "bootstrap-source-version-changed"
    assert not estate.app.mutations


def test_bootstrap_candidate_set_cannot_be_edited_to_a_balance_fitting_subset(connection, estate):
    scope, transactions = _bootstrap_ahead(estate, two=True)
    contract = deepcopy(estate.bootstrap_contract)
    contract["candidates"].pop()
    contract["candidateSetHash"] = content_hash(contract["candidates"])
    contract["bootstrapHash"] = content_hash({key: value for key, value in contract.items() if key != "bootstrapHash"})
    path = write(estate.root / "bootstrap" / "tampered-contract.json", contract)
    config = json.loads((estate.root / "scope.json").read_text())
    config["bootstrap"] = {"path": path.relative_to(estate.root).as_posix(), "sha256": inputs.digest(path)}
    write(estate.root / "scope.json", config)
    scope = inputs.load_scope(estate.root, "scope.json")
    estate.install_marker(scope)
    estate.snapshot(transactions, "-17")
    with pytest.raises(inputs.IncrementalHold, match="candidate-set-or-seal-mismatch"):
        incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert not estate.app.mutations


def test_bootstrap_multiple_existing_aliases_remain_a_hold(connection, estate):
    transaction = estate.transaction("NEW", "-10")
    scope = estate.baseline([transaction])
    event = scope.baseline.canonical_events[0]
    estate.app.activity("simplefin:SYN-APP:NEW", -10)
    estate.app.activity(f"canonical:{scope.canonical_id}:{event.canonical_event_id}", -10)
    scope = estate.bootstrap(scope)
    assert estate.bootstrap_contract["candidates"] == []
    estate.snapshot([transaction], "-20")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["state"] == "held" and planned["qualifications"][0]["reason"] == "multiple-legacy-activities"
    assert not estate.app.mutations


def test_bootstrap_export_will_not_sign_a_claimed_completed_boolean(estate):
    key = b"synthetic-bootstrap-verification-key"
    alleged = promotion.seal({
        "kind": promotion.KIND + "-execution", "schemaVersion": 1,
        "phase": "completed", "history": [],
    }, key)
    path = write(estate.root / "bootstrap" / "unverified-completed.json", alleged)
    with pytest.raises(promotion.PromotionError, match="completed"):
        bootstrap.export_repair_history(
            estate.root, [{"execution": {"path": path.relative_to(estate.root).as_posix(), "sha256": inputs.digest(path)}}],
            evidence_key=key, operator_key=key,
        )


def test_bootstrap_equal_value_distinct_source_occurrences_are_not_a_subset_search(connection, estate):
    transactions = [estate.transaction("ONE", "-10"), estate.transaction("TWO", "-10")]
    scope = estate.baseline(transactions)
    estate.app.activity("extract:SYN-APP:OLD-SURVIVOR", -7, date="2026-01-14T12:00:00Z")
    scope = estate.bootstrap(scope)
    estate.snapshot(transactions, "-27")
    ready = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert ready["state"] == "pending" and len(ready["operations"]) == 2
    assert incremental.apply(connection, scope, estate.client, ready["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
    assert len(estate.app.mutations) == 2


def test_completed_bootstrap_does_not_freeze_later_legitimate_notes(connection, estate):
    scope, transactions = _bootstrap_ahead(estate)
    estate.snapshot(transactions, "-17")
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert incremental.apply(connection, scope, estate.client, first["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
    created = next(row for row in estate.app.rows if row["idempotencyKey"].startswith("finance:accepted:"))
    created["comment"] = "Synthetic legitimate note after bootstrap"
    created["isUserModified"] = True
    later = NOW + timedelta(minutes=10)
    estate.snapshot(transactions, "-17", now=later)
    assert incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))["state"] == "noop"
    assert created["comment"].endswith("after bootstrap") and len(estate.app.mutations) == 1


@pytest.mark.parametrize("mismatch", ["receipt", "repair-signature", "repair-head", "window", "maximum"])
def test_bootstrap_evidence_and_bounds_cannot_be_replaced_by_approval_flags(estate, mismatch):
    scope, _ = _bootstrap_ahead(estate, two=True)
    contract = deepcopy(estate.bootstrap_contract)
    if mismatch == "receipt":
        path = estate.root / contract["applications"][0]["receipt"]["path"]
        receipt = json.loads(path.read_text())
        receipt["postLedgerFingerprint"] = "f" * 64
        write(path, receipt)
        contract["applications"][0]["receipt"]["sha256"] = inputs.digest(path)
    elif mismatch == "repair-signature":
        path = estate.root / contract["repairHistory"]["file"]["path"]
        history = json.loads(path.read_text())
        history["deleted"] = []
        write(path, history)
        contract["repairHistory"]["file"]["sha256"] = inputs.digest(path)
    elif mismatch == "repair-head":
        contract["repairHistory"]["headExecutionHash"] = "f" * 64
    elif mismatch == "window":
        contract["sourceDateWindow"] = {"from": "2025-01-01", "through": "2026-12-31"}
    else:
        contract["maximumCandidates"] = 1
    contract["bootstrapHash"] = content_hash({key: value for key, value in contract.items() if key != "bootstrapHash"})
    path = write(estate.root / "bootstrap" / "changed-contract.json", contract)
    config = json.loads((estate.root / "scope.json").read_text())
    config["bootstrap"] = {"path": path.relative_to(estate.root).as_posix(), "sha256": inputs.digest(path)}
    write(estate.root / "scope.json", config)
    changed_scope = inputs.load_scope(estate.root, "scope.json")
    with pytest.raises((inputs.IncrementalHold, promotion.PromotionError)):
        bootstrap.load_contract(changed_scope)


def _source_anchor_case(estate, *, detailed=False, dateless_current=False):
    known = estate.transaction("KNOWN", "-10")
    pending = {**estate.transaction("PENDING", "-3", pending=True), "posted": 0}
    unchanged = {**estate.transaction("UNCHANGED", "20"),
                 "posted": int(datetime(2026, 1, 12, 12, tzinfo=timezone.utc).timestamp())}
    anchor_transactions = [known, pending, unchanged]
    changed = {**known, "amount": "-12"}
    settled = {**pending, "pending": False, "amount": "-4",
               "posted": int(datetime(2026, 1, 10, 12, tzinfo=timezone.utc).timestamp())}
    tail = {**estate.transaction("TAIL", "-5"),
            "posted": int(datetime(2026, 1, 16, 12, tzinfo=timezone.utc).timestamp())}
    older = {**estate.transaction("OLD-EXPOSED", "-30"),
             "posted": int(datetime(2026, 1, 5, 12, tzinfo=timezone.utc).timestamp())}
    current = [changed, settled, unchanged, tail, older]
    if dateless_current:
        current.append({**estate.transaction("STILL-PENDING", "-50", pending=True), "posted": 0})
    scope = estate.baseline(current)
    if detailed:
        estate.app.activity("simplefin:SYN-APP:KNOWN", -10)
    scope = estate.source_anchor(scope, anchor_transactions, assertion_amount=110 if detailed else 100)
    return scope, current


def test_source_anchor_all_tail_pending_and_amount_deltas_without_rewriting_assertion(connection, estate):
    scope, current = _source_anchor_case(estate, dateless_current=True)
    contract = source_anchor.load_contract(scope)
    assert contract.document["source"]["observedPostedWatermark"] == "2026-01-15"
    assert contract.states["simplefin:SYN-SF:PENDING"]["sourceDay"] is None
    assertion = deepcopy(next(row for row in estate.app.rows if row["id"] == "SYN-CASH-ASSERTION"))
    estate.snapshot(current, "89")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["state"] == "pending"
    assert len(planned["operations"]) == 3
    transitions = {op["anchorTransition"]["sourceId"]: op for op in planned["operations"]}
    assert set(transitions) == {"simplefin:SYN-SF:KNOWN", "simplefin:SYN-SF:PENDING", "simplefin:SYN-SF:TAIL"}
    assert transitions["simplefin:SYN-SF:KNOWN"]["delta"] == "-2"
    assert transitions["simplefin:SYN-SF:KNOWN"]["representation"]["representation_kind"] == "source-delta"
    assert transitions["simplefin:SYN-SF:PENDING"]["delta"] == "-4"
    assert transitions["simplefin:SYN-SF:PENDING"]["anchorTransition"]["kind"] == "pending-settled"
    assert all(op["activityId"] != "SYN-CASH-ASSERTION" for op in planned["operations"])
    assert planned["historicalBackfillCount"] >= 2
    assert planned["pendingSourceRecords"][0]["sourceDay"] is None
    applied = incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))
    assert applied["state"] == "applied"
    assert next(row for row in estate.app.rows if row["id"] == "SYN-CASH-ASSERTION") == assertion
    assert not any("OLD-EXPOSED" in row.get("comment", "") for row in estate.app.rows)
    source_amount, cash_amount, base = connection.execute(
        "SELECT source_amount,represented_cash_amount,base_source_amount "
        "FROM finance_read.incremental_cash_representations WHERE representation_kind='source-delta'"
    ).fetchone()
    assert (source_amount, cash_amount, base) == (inputs.money("-12"), inputs.money("-2"), inputs.money("-10"))
    watermark, balance = connection.execute(
        "SELECT observed_posted_watermark,source_balance FROM finance_read.incremental_source_anchor_status"
    ).fetchone()
    assert watermark.isoformat() == "2026-01-16" and balance == inputs.money("89")


def test_source_anchor_existing_detailed_correction_holds_entire_transition_set(connection, estate):
    scope, current = _source_anchor_case(estate, detailed=True)
    estate.snapshot(current, "89")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    before = deepcopy(estate.app.rows)
    assert planned["state"] == "held" and planned["reason"] == "source-anchor-transition-set-held"
    held = next(q for q in planned["qualifications"] if q["proof"]["sourceIds"] == ["simplefin:SYN-SF:KNOWN"])
    assert held["reason"] == "upstream-update-contract-unsupported"
    assert len(planned["operations"]) == 2 and all(op["kind"] == "create" for op in planned["operations"])
    assert incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))["state"] == "held"
    assert estate.app.rows == before and not estate.app.mutations and not estate.app.backups
    assert connection.execute("SELECT count(*) FROM finance.incremental_outbox").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM finance.incremental_anchor_checkpoints").fetchone()[0] == 1
    assert not any(row["idempotencyKey"].startswith("finance:source-delta:") for row in estate.app.rows)


def test_source_anchor_progress_is_observed_not_balance_date_or_request_coverage(connection, estate):
    scope, current = _source_anchor_case(estate)
    contract = source_anchor.load_contract(scope)
    assert contract.document["source"]["balanceObservedAt"].startswith("2026-02-02")
    assert contract.document["source"]["requestEvidence"]["requestedStart"] == "2026-01-10"
    estate.snapshot(current, "89")  # Wider current window exposes January 5 history.
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert first["state"] == "pending"
    assert first["sourceAnchor"]["changes"]["simplefin:SYN-SF:OLD-EXPOSED"]["kind"] == "historical"
    assert first["sourceAnchor"]["changes"]["simplefin:SYN-SF:TAIL"]["kind"] == "new-tail"
    assert incremental.apply(connection, scope, estate.client, first["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
    later = NOW + timedelta(minutes=10)
    equal_watermark = {**estate.transaction("LATE-EXPOSED", "-8"),
                       "posted": int(datetime(2026, 1, 16, 12, tzinfo=timezone.utc).timestamp())}
    new_tail = {**estate.transaction("NEXT-TAIL", "-6"),
                "posted": int(datetime(2026, 1, 17, 12, tzinfo=timezone.utc).timestamp())}
    estate.snapshot([*current, equal_watermark, new_tail], "83", now=later)
    second = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert second["state"] == "pending" and len(second["operations"]) == 1
    assert second["sourceAnchor"]["changes"]["simplefin:SYN-SF:LATE-EXPOSED"]["kind"] == "historical"
    assert second["operations"][0]["anchorTransition"]["sourceId"] == "simplefin:SYN-SF:NEXT-TAIL"
    assert incremental.apply(connection, scope, estate.client, second["runHash"], now=later + timedelta(seconds=1))["state"] == "applied"


def test_source_anchor_replay_and_opposite_same_id_deltas_do_not_optimize_subset(connection, estate):
    scope, current = _source_anchor_case(estate)
    estate.snapshot(current, "89")
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert incremental.apply(connection, scope, estate.client, first["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
    later = NOW + timedelta(minutes=10)
    estate.snapshot(current, "89", now=later)
    replay = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert replay["state"] == "noop" and not replay["operations"]
    assert len(estate.app.mutations) == 3
    changed = [dict(row) for row in current]
    next(row for row in changed if row["id"] == "KNOWN")["amount"] = "-11"  # +1 from delivered -12
    next(row for row in changed if row["id"] == "OLD-EXPOSED")["amount"] = "-31"  # -1, now a known posted ID
    later += timedelta(minutes=10)
    estate.snapshot(changed, "89", now=later)
    before = deepcopy(estate.app.rows)
    checkpoints = connection.execute("SELECT count(*) FROM finance.incremental_anchor_checkpoints").fetchone()[0]
    corrections = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert corrections["state"] == "held" and corrections["reason"] == "source-anchor-transition-set-held"
    assert corrections["sourceAnchor"]["expectedSourceBalance"] == "89"
    assert len(corrections["operations"]) == 1 and corrections["operations"][0]["kind"] == "create"
    held = next(q for q in corrections["qualifications"] if q["proof"]["sourceIds"] == ["simplefin:SYN-SF:KNOWN"])
    assert held["reason"] == "upstream-update-contract-unsupported"
    assert incremental.apply(connection, scope, estate.client, corrections["runHash"], now=later + timedelta(seconds=1))["state"] == "held"
    representations = connection.execute(
        "SELECT source_amount,represented_cash_amount,base_source_amount "
        "FROM finance_read.incremental_cash_representations WHERE representation_kind='source-delta'"
    ).fetchall()
    assert representations == [(inputs.money("-12"), inputs.money("-2"), inputs.money("-10"))]
    assert connection.execute("SELECT count(*) FROM finance.incremental_anchor_checkpoints").fetchone()[0] == checkpoints
    assert connection.execute("SELECT count(*) FROM finance.incremental_outbox WHERE run_hash=%s",
                              (corrections["runHash"],)).fetchone()[0] == 0
    assert estate.app.rows == before and len(estate.app.mutations) == 3
    assert next(row for row in estate.app.rows if row["id"] == "SYN-CASH-ASSERTION")["amount"] == 100


def _post_origin_late_case(connection, estate, *, collision=False, previous_start=None, late_day=4, late_id="LATE"):
    known = estate.transaction("KNOWN", "-10")
    scope = estate.baseline([known])
    if collision:
        estate.app.activity("manual:SYN-EXISTING", -8, comment=f"Synthetic merchant {late_id}",
                            date="2026-02-04T12:00:00Z")
    scope = estate.source_anchor(scope, [known], assertion_amount=108 if collision else 100)
    first_tail = {**estate.transaction("FIRST-TAIL", "-5"),
                  "posted": int(datetime(2026, 2, 4, 12, tzinfo=timezone.utc).timestamp())}
    estate.snapshot([known, first_tail], "95", requested_start=previous_start)
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert incremental.apply(connection, scope, estate.client, first["runHash"],
                             now=NOW + timedelta(seconds=1))["state"] == "applied"
    late_time = int(datetime(2026, 2, late_day, 12, tzinfo=timezone.utc).timestamp())
    late = {**estate.transaction(late_id, "-8"), "posted": late_time, "transacted_at": late_time}
    tail = {**estate.transaction("NEXT-TAIL", "-6"),
            "posted": int(datetime(2026, 2, 5, 12, tzinfo=timezone.utc).timestamp())}
    return scope, [known, first_tail, late, tail], late


@pytest.mark.parametrize("late_day", [3, 4])
def test_explicit_post_origin_late_arrival_projects_full_batch_and_replays(connection, estate, late_day):
    scope, current, late = _post_origin_late_case(connection, estate, late_day=late_day)
    before = deepcopy(estate.app.rows)
    now = NOW + timedelta(minutes=10)
    estate.snapshot(current, "81", now=now)
    plan = incremental.plan(connection, scope, estate.client, now=now + timedelta(seconds=1))
    assert plan["state"] == "pending" and len(plan["operations"]) == 2
    assert plan["sourceAnchor"]["transitionRule"] == source_anchor.TRANSITION_RULE
    transition = plan["sourceAnchor"]["changes"][f"simplefin:SYN-SF:{late['id']}"]
    assert transition["kind"] == "late-posted"
    proof = transition["lateArrivalEvidence"]
    assert proof["eligible"] is True and proof["requestCompletenessClaimed"] is False
    assert proof["previousReceiptHash"] and proof["currentReceiptHash"] == plan["receiptHash"]
    assert incremental.apply(connection, scope, estate.client, plan["runHash"],
                             now=now + timedelta(seconds=1))["state"] == "applied"
    assert all(row in estate.app.rows for row in before)
    assert bootstrap.projection.cash(estate.app.rows) == inputs.money("81")
    assert incremental.run(connection, scope, estate.client, now=now + timedelta(seconds=2))["state"] == "noop"


@pytest.mark.parametrize("failure", ["missing-transacted", "pre-origin-transacted", "future-transacted",
                                    "widened-request", "existing-economic-match", "known-removed",
                                    "balance-mismatch", "prior-receipt-drift"])
def test_late_arrival_proof_cannot_bypass_history_identity_or_balance_guards(connection, estate, failure):
    scope, current, late = _post_origin_late_case(
        connection, estate, collision=failure == "existing-economic-match",
        previous_start=datetime(2026, 1, 10).date() if failure == "widened-request" else None,
        late_id="REMOVED" if failure == "known-removed" else "LATE",
    )
    before = deepcopy(estate.app.rows)
    mutations = len(estate.app.mutations)
    checkpoints = connection.execute("SELECT count(*) FROM finance.incremental_anchor_checkpoints").fetchone()[0]
    if failure == "missing-transacted":
        late.pop("transacted_at")
    elif failure == "pre-origin-transacted":
        late["transacted_at"] = int(datetime(2026, 1, 30, 12, tzinfo=timezone.utc).timestamp())
    elif failure == "future-transacted":
        late["transacted_at"] = int((NOW + timedelta(days=1)).timestamp())
    elif failure == "prior-receipt-drift":
        receipt_hash = connection.execute(
            "SELECT receipt_hash FROM finance_read.incremental_source_anchor_status"
        ).fetchone()[0]
        path = estate.root / "automation" / "source-collection" / "runs" / f"{receipt_hash}.json"
        body = inputs.document(path)
        body["inputManifest"]["requestedStart"] = "2025-12-01"
        path.chmod(0o600)
        write(path, body)
    now = NOW + timedelta(minutes=10)
    estate.snapshot(current, "80" if failure == "balance-mismatch" else "81", now=now)
    if failure == "prior-receipt-drift":
        with pytest.raises(inputs.IncrementalHold, match="late-arrival-prior-receipt-invalid"):
            incremental.plan(connection, scope, estate.client, now=now + timedelta(seconds=1))
    else:
        held = incremental.plan(connection, scope, estate.client, now=now + timedelta(seconds=1))
        assert held["state"] == "held"
        assert connection.execute("SELECT count(*) FROM finance.incremental_outbox WHERE run_hash=%s",
                                  (held["runHash"],)).fetchone()[0] == 0
        if failure == "existing-economic-match":
            assert any(q["reason"] == "late-arrival-existing-economic-match" for q in held["qualifications"])
        if failure == "known-removed":
            assert any(q["reason"] == "source-anchor-prior-projection-blocks-create" for q in held["qualifications"])
    assert estate.app.rows == before and len(estate.app.mutations) == mutations
    assert connection.execute("SELECT count(*) FROM finance.incremental_anchor_checkpoints").fetchone()[0] == checkpoints


@pytest.mark.parametrize("source_balance,expected", [("89", "pending"), ("81", "held")])
def test_all_post_origin_arrivals_are_selected_without_amount_subset_fitting(connection, estate, source_balance, expected):
    scope, current, late = _post_origin_late_case(connection, estate)
    other = {**late, "id": "LATE-CREDIT", "amount": "8", "description": "Synthetic late credit"}
    now = NOW + timedelta(minutes=10)
    estate.snapshot([*current, other], source_balance, now=now)
    plan = incremental.plan(connection, scope, estate.client, now=now + timedelta(seconds=1))
    assert plan["state"] == expected and len(plan["operations"]) == 3
    assert sum(op["anchorTransition"]["kind"] == "late-posted" for op in plan["operations"]) == 2
    assert plan["journaledOperationCount"] == (3 if expected == "pending" else 0)


@pytest.mark.parametrize("field,value", [
    ("correction_of", "SYN-OTHER"), ("reversal_of", "SYN-OTHER"),
    ("pending_of", "SYN-OTHER"), ("counterpart_id", "SYN-OTHER"),
    ("assetId", "SYN-ASSET"), ("asset_id", "SYN-ASSET"),
    ("security_id", "SYN-ASSET"), ("symbol", "SYN"), ("shares", "1"),
])
def test_unchanged_held_late_version_cannot_acquire_link_or_security_permission(connection, estate, field, value):
    scope, current, late = _post_origin_late_case(connection, estate)
    explicit_time = late.pop("transacted_at")
    now = NOW + timedelta(minutes=10)
    estate.snapshot(current, "81", now=now)
    first = incremental.plan(connection, scope, estate.client, now=now + timedelta(seconds=1))
    assert first["state"] == "held"
    source_id = f"simplefin:SYN-SF:{late['id']}"
    version_count = connection.execute(
        "SELECT count(*) FROM finance.incremental_source_versions WHERE scope_id=%s AND source_id=%s",
        (scope.scope_id, source_id),
    ).fetchone()[0]
    before = deepcopy(estate.app.rows)
    writes = len(estate.app.activity_write_requests)
    late.update(transacted_at=explicit_time, **{field: value})
    later = now + timedelta(minutes=10)
    estate.snapshot(current, "81", now=later)
    second = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert second["state"] == "held"
    reason = "linked-source-transition-unsupported" if field in {
        "correction_of", "reversal_of", "pending_of", "counterpart_id",
    } else "security-source-activity-unsupported"
    assert {"sourceId": source_id, "reason": reason} in second["heldSourceChanges"]
    assert second["sourceAnchor"]["changes"][source_id]["lateArrivalEvidence"]["eligible"] is False
    assert connection.execute(
        "SELECT count(*) FROM finance.incremental_source_versions WHERE scope_id=%s AND source_id=%s",
        (scope.scope_id, source_id),
    ).fetchone()[0] == version_count + 1
    assert incremental.apply(connection, scope, estate.client, second["runHash"],
                             now=later + timedelta(seconds=1))["state"] == "held"
    assert estate.app.rows == before and len(estate.app.activity_write_requests) == writes
    assert connection.execute("SELECT count(*) FROM finance.incremental_outbox WHERE run_hash=%s",
                              (second["runHash"],)).fetchone()[0] == 0
    late.pop(field)
    later += timedelta(minutes=10)
    estate.snapshot(current, "81", now=later)
    third = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert third["state"] == "held"
    assert {"sourceId": source_id, "reason": reason} in third["heldSourceChanges"]
    assert connection.execute(
        "SELECT count(*) FROM finance.incremental_source_versions WHERE scope_id=%s AND source_id=%s",
        (scope.scope_id, source_id),
    ).fetchone()[0] == version_count + 1
    assert estate.app.rows == before and len(estate.app.activity_write_requests) == writes


@pytest.mark.parametrize("anchored", [False, True])
@pytest.mark.parametrize("field", ["correction_of", "reversal_of", "assetId"])
def test_dateless_pending_restrictions_survive_settlement_and_field_omission(connection, estate, anchored, field):
    known = estate.transaction("KNOWN", "-20")
    scope = estate.baseline([known] if anchored else [])
    if anchored:
        scope = estate.source_anchor(scope, [known])
    pending = {**estate.transaction("PENDING-RESTRICTED", "-8", pending=True),
               "posted": 0, field: "SYN-RESTRICTED"}
    prior = [known] if anchored else []
    estate.snapshot([*prior, pending], "100" if anchored else "0")
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    source_id = "simplefin:SYN-SF:PENDING-RESTRICTED"
    reason = "security-source-activity-unsupported" if field == "assetId" else "linked-source-transition-unsupported"
    assert first["state"] == "held"
    assert {"sourceId": source_id, "reason": reason} in first["heldSourceChanges"]
    assert connection.execute(
        "SELECT count(*) FROM finance.incremental_source_versions WHERE scope_id=%s AND source_id=%s",
        (scope.scope_id, source_id),
    ).fetchone()[0] == 0
    assert first["pendingSourceRecords"][0]["sourceDay"] is None
    before = deepcopy(estate.app.rows)
    settled = {**estate.transaction("PENDING-RESTRICTED", "-8"),
               "posted": int(datetime(2026, 1, 16, 12, tzinfo=timezone.utc).timestamp())}
    later = NOW + timedelta(minutes=10)
    estate.snapshot([*prior, settled], "92" if anchored else "-8", now=later)
    second = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert second["state"] == "held"
    assert {"sourceId": source_id, "reason": reason} in second["heldSourceChanges"]
    assert second["journaledOperationCount"] == second["appliedOperationCount"] == 0
    assert incremental.apply(connection, scope, estate.client, second["runHash"],
                             now=later + timedelta(seconds=1))["state"] == "held"
    assert estate.app.rows == before and not estate.app.activity_write_requests


@pytest.mark.parametrize("field", ["correction_of", "assetId"])
def test_snapshot_omission_does_not_release_an_undelivered_source_restriction(connection, estate, field):
    scope = estate.baseline([])
    clean = estate.transaction("UNDISPATCHED", "-10")
    estate.snapshot([clean], "0")  # The clean version exists, but was never delivered.
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert first["state"] == "held"
    restricted = {**clean, field: "SYN-OTHER"}
    later = NOW + timedelta(minutes=10)
    estate.snapshot([restricted], "0", now=later)
    second = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert second["state"] == "held"
    later += timedelta(minutes=10)
    estate.snapshot([], "-10", now=later)  # Old clean economics would fit if the hold were lost.
    third = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    reason = "security-source-activity-unsupported" if field == "assetId" else "linked-source-transition-unsupported"
    assert third["state"] == "held"
    assert {"sourceId": "simplefin:SYN-SF:UNDISPATCHED", "reason": reason} in third["heldSourceChanges"]
    assert third["journaledOperationCount"] == third["appliedOperationCount"] == 0
    assert incremental.apply(connection, scope, estate.client, third["runHash"],
                             now=later + timedelta(seconds=1))["state"] == "held"
    assert not estate.app.rows and not estate.app.activity_write_requests


def test_frontier_arrival_balance_match_is_review_evidence_not_permission(connection, estate):
    scope, current = _source_anchor_case(estate)
    estate.snapshot(current, "89")
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert incremental.apply(connection, scope, estate.client, first["runHash"],
                             now=NOW + timedelta(seconds=1))["state"] == "applied"
    before = deepcopy(estate.app.rows)
    mutations = len(estate.app.mutations)
    checkpoints = connection.execute("SELECT count(*) FROM finance.incremental_anchor_checkpoints").fetchone()[0]
    later = NOW + timedelta(minutes=10)
    frontier = {**estate.transaction("LATE-EXPOSED", "-8"),
                "posted": int(datetime(2026, 1, 16, 12, tzinfo=timezone.utc).timestamp())}
    tail = {**estate.transaction("NEXT-TAIL", "-6"),
            "posted": int(datetime(2026, 1, 17, 12, tzinfo=timezone.utc).timestamp())}
    estate.snapshot([*current, frontier, tail], "75", now=later)
    held = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert held["state"] == "held"
    assert inputs.money(held["sourceBalance"]) - inputs.money(held["predictedCash"]) == inputs.money(frontier["amount"])
    assert held["proposedOperationCount"] == 1 and held["journaledOperationCount"] == 0
    assert held["appliedOperationCount"] == 0 and held["plannedBalanceMatchesSource"] is False
    assert held["newHistoricalReviewCount"] == held["frontierReviewCount"] == 1
    connection.execute("SET ROLE finance_shadow_agent_readonly")
    try:
        status = incremental.status(connection, scope.scope_id, root=scope.root)
        assert status["state"] == "held"
        assert status["evidence"]["proposedOperationCount"] == 1
        assert status["evidence"]["journaledOperationCount"] == status["evidence"]["appliedOperationCount"] == 0
        assert status["evidence"]["plannedBalanceMatchesSource"] is False
        assert status["evidence"]["frontierReviewCount"] == 1
    finally:
        connection.execute("SET ROLE finance_shadow_ingest")
    assert incremental.apply(connection, scope, estate.client, held["runHash"],
                             now=later + timedelta(seconds=1))["state"] == "held"
    assert estate.app.rows == before and len(estate.app.mutations) == mutations
    assert connection.execute("SELECT count(*) FROM finance.incremental_anchor_checkpoints").fetchone()[0] == checkpoints
    assert connection.execute("SELECT count(*) FROM finance.incremental_outbox WHERE run_hash=%s",
                              (held["runHash"],)).fetchone()[0] == 0


def test_status_derives_legacy_plan_diagnostics_but_rejects_artifact_drift(connection, estate):
    scope = estate.baseline([])
    estate.snapshot([estate.transaction()], "-10")
    plan = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert incremental.status(connection, scope.scope_id, root=scope.root)["evidence"]["journaledOperationCount"] == 1
    legacy = {key: value for key, value in plan.items() if key not in {
        "proposedOperationCount", "journaledOperationCount", "appliedOperationCount",
        "plannedBalanceMatchesSource", "newHistoricalReviewCount", "frontierReviewCount",
    }}
    assert incremental.reconciliation_diagnostics(legacy) == {
        "proposedOperationCount": 1, "plannedBalanceMatchesSource": True,
    }
    path = estate.root / "incremental" / "plans" / f"{plan['runHash']}.json"
    path.chmod(0o600)
    write(path, {**plan, "sourceBalance": "999"})
    with pytest.raises(inputs.IncrementalHold, match="status-plan-binding-drift"):
        incremental.status(connection, scope.scope_id, root=scope.root)


def test_source_anchor_balance_mismatch_holds_all_changes_and_does_not_advance_progress(connection, estate):
    scope, current = _source_anchor_case(estate)
    estate.snapshot(current, "90")  # Fixed set predicts 89, not a requested residual.
    held = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert held["state"] == "held" and len(held["operations"]) == 3
    assert held["sourceAnchor"]["expectedSourceBalance"] == "89"
    assert connection.execute("SELECT count(*) FROM finance.incremental_outbox").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM finance.incremental_anchor_checkpoints").fetchone()[0] == 1
    later = NOW + timedelta(minutes=10)
    estate.snapshot(current, "89", now=later)
    ready = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert ready["state"] == "pending" and len(ready["operations"]) == 3
    assert incremental.apply(connection, scope, estate.client, ready["runHash"], now=later + timedelta(seconds=1))["state"] == "applied"


def test_source_anchor_known_removed_alias_cannot_be_reintroduced_as_delta(connection, estate):
    removed = estate.transaction("REMOVED", "-7")
    changed = {**removed, "amount": "-8"}
    scope = estate.baseline([changed])
    scope = estate.source_anchor(scope, [removed])
    estate.snapshot([changed], "99")
    result = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert result["state"] == "held"
    assert result["qualifications"][0]["reason"] == "source-anchor-prior-projection-blocks-create"
    assert connection.execute("SELECT count(*) FROM finance.incremental_outbox").fetchone()[0] == 0
    assert not estate.app.mutations


def test_source_anchor_cash_anchor_and_assertion_are_exact_not_sum_fitting(connection, estate):
    scope = estate.baseline([estate.transaction()])
    with pytest.raises(inputs.IncrementalHold, match="cash-prestate-mismatch"):
        estate.source_anchor(scope, [estate.transaction()], balance="101", assertion_amount=100)
    assert not estate.app.mutations


def test_existing_balance_assertion_requires_source_anchor_not_date_window_bootstrap(connection, estate):
    scope = estate.baseline([estate.transaction()])
    estate.app.activity("rebuild:assertion:SYN-CANONICAL:2026-02-02", 100, identifier="SYN-ASSERTION")
    estate.snapshot([estate.transaction()], "90")
    result = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert result["state"] == "held" and result["qualifications"][0]["reason"] == "source-balance-anchor-contract-required"
    assert not estate.app.mutations


def test_source_anchor_lost_ack_recovery_records_delta_representation_once(connection, estate):
    known = estate.transaction("KNOWN", "-10")
    changed = {**known, "amount": "-12"}
    scope = estate.baseline([changed])
    scope = estate.source_anchor(scope, [known])
    estate.snapshot([changed], "98")
    ready = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    with pytest.raises(SystemExit):
        incremental.apply(connection, scope, estate.client, ready["runHash"], now=NOW + timedelta(seconds=1),
                          after_api=lambda: (_ for _ in ()).throw(SystemExit()))
    assert len(estate.app.mutations) == 1
    assert incremental.apply(connection, scope, estate.client, ready["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
    assert len(estate.app.mutations) == 1
    assert connection.execute(
        "SELECT represented_cash_amount FROM finance_read.incremental_cash_representations"
    ).fetchone()[0] == inputs.money("-2")


def test_source_anchor_missing_posted_records_and_nonfinancial_notes_are_retained(connection, estate):
    scope, current = _source_anchor_case(estate)
    estate.snapshot(current, "89")
    ready = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert incremental.apply(connection, scope, estate.client, ready["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
    delta = next(row for row in estate.app.rows if row["idempotencyKey"].startswith("finance:source-delta:"))
    delta["comment"] = "Synthetic user note after cash projection"
    delta["isUserModified"] = True
    before = deepcopy(estate.app.rows)
    later = NOW + timedelta(minutes=10)
    estate.snapshot([], "89", now=later)
    replay = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert replay["state"] == "noop" and not replay["operations"]
    assert estate.app.rows == before and len(estate.app.mutations) == 3


def test_source_anchor_user_deleted_delta_or_changed_assertion_is_not_repaired(connection, estate):
    known = estate.transaction("KNOWN", "-10")
    changed = {**known, "amount": "-12"}
    scope = estate.baseline([changed])
    scope = estate.source_anchor(scope, [known])
    estate.snapshot([changed], "98")
    first = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert incremental.apply(connection, scope, estate.client, first["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
    delta = next(row for row in estate.app.rows if row["idempotencyKey"].startswith("finance:source-delta:"))
    estate.app.rows.remove(delta)
    later = NOW + timedelta(minutes=10)
    estate.snapshot([changed], "98", now=later)
    with pytest.raises(inputs.IncrementalHold, match="financial-state-drift"):
        incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert len(estate.app.mutations) == 1


def test_partial_anchor_delivery_does_not_advance_watermark_past_unsent_tail(connection, estate):
    known = estate.transaction("KNOWN", "-10")
    first_tail = {**estate.transaction("EARLY", "-5"),
                  "posted": int(datetime(2026, 1, 16, 12, tzinfo=timezone.utc).timestamp())}
    second_tail = {**estate.transaction("LATER", "-6"),
                   "posted": int(datetime(2026, 1, 17, 12, tzinfo=timezone.utc).timestamp())}
    current = [known, first_tail, second_tail]
    scope = estate.baseline(current)
    scope = estate.source_anchor(scope, [known])
    estate.snapshot(current, "89")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    ordered = connection.execute(
        "SELECT operation_document FROM finance.incremental_outbox ORDER BY operation_id"
    ).fetchall()
    first_op, remaining_op = ordered[0][0], ordered[1][0]
    fresh = [dict(row) for row in current]
    target = next(row for row in fresh if row["id"] == remaining_op["anchorTransition"]["targetState"]["rawTransactionId"])
    target["posted"] = int(datetime.fromisoformat(
        first_op["anchorTransition"]["targetState"]["sourceDay"] + "T12:00:00+00:00"
    ).timestamp())
    later = NOW + timedelta(minutes=10)

    def advance_source():
        estate.snapshot(fresh, "89", now=later)

    interrupted = incremental.apply(connection, scope, estate.client, planned["runHash"],
                                    now=NOW + timedelta(seconds=1), after_api=advance_source)
    assert interrupted["state"] == "held" and len(estate.app.mutations) == 1
    watermark = connection.execute(
        "SELECT observed_posted_watermark FROM finance_read.incremental_source_anchor_status"
    ).fetchone()[0]
    assert watermark.isoformat() == "2026-01-15"
    resumed = incremental.plan(connection, scope, estate.client, now=later + timedelta(seconds=1))
    assert resumed["state"] == "pending" and len(resumed["operations"]) == 1
    assert resumed["operations"][0]["anchorTransition"]["sourceId"] == remaining_op["anchorTransition"]["sourceId"]
    assert incremental.apply(connection, scope, estate.client, resumed["runHash"], now=later + timedelta(seconds=1))["state"] == "applied"
    assert len(estate.app.mutations) == 2


def test_source_anchor_all_pending_has_no_invented_posted_watermark(connection, estate):
    pending = {**estate.transaction("PENDING", "-5", pending=True), "posted": 0}
    settled = {**pending, "pending": False, "posted": int(datetime(2026, 1, 10, 12, tzinfo=timezone.utc).timestamp())}
    unknown = estate.transaction("UNCLASSIFIED", "-20")
    scope = estate.baseline([settled, unknown])
    scope = estate.source_anchor(scope, [pending])
    assert source_anchor.load_contract(scope).document["source"]["observedPostedWatermark"] is None
    estate.snapshot([settled, unknown], "95")
    planned = incremental.plan(connection, scope, estate.client, now=NOW + timedelta(seconds=1))
    assert planned["state"] == "pending" and len(planned["operations"]) == 1
    assert planned["operations"][0]["anchorTransition"]["kind"] == "pending-settled"
    assert planned["sourceAnchor"]["changes"]["simplefin:SYN-SF:UNCLASSIFIED"]["kind"] == "historical"
    assert incremental.apply(connection, scope, estate.client, planned["runHash"], now=NOW + timedelta(seconds=1))["state"] == "applied"
