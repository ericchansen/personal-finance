"""Isolated archive and real PowerShell launcher checks; never installs a task."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest

from finance_store import incremental_launcher as launcher
from finance_store import incremental_release as release
from importers.monarch import mutation_guard as guard

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
NATIVE = pytest.mark.skipif(
    os.environ.get("FINANCE_INCREMENTAL_POSTGRES_TEST") != "1",
    reason="requires owned disposable PostgreSQL and authenticated synthetic REST",
)

# Reuse the real resolver/publication/REST/native PostgreSQL fixtures, not a
# replacement worker or a deployed DSN. Its files/containers have owned cleanup.
from tests.test_incremental import (  # noqa: E402, F401
    app as app, connection as connection, database as database, estate as estate, pg_server as pg_server,
)


@contextmanager
def owned_directory():
    path = ROOT / f".incremental-packaging-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        for child in path.rglob("*"):
            if child.is_file():
                child.chmod(0o666)
        shutil.rmtree(path)


@pytest.fixture
def owned():
    with owned_directory() as root:
        yield root


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(release.encoded(value) if isinstance(value, dict) else value.encode())
    return path


def minimal_archive(root):
    write(root / "finance_store" / "incremental.py", "# Synthetic runtime.\n")
    write(root / "importers" / "monarch" / "mutation_guard.py", "# Synthetic guard.\n")
    write(root / "deploy" / "postgres" / "migrations" / "0001_synthetic.sql", "-- Synthetic migration.\n")
    write(root / "README.md", "Synthetic archive metadata.\n")
    document = {
        "schemaVersion": 1, "kind": release.MANIFEST_KIND, "commit": "a" * 40, "tree": "b" * 40,
        "archiveSha256": "c" * 64, "files": release.archive_files(root),
        "codeHash": release.fingerprint(release.runtime_files(root)),
    }
    write(root / release.MANIFEST, document)
    return document


def test_current_guard_supports_manifest_bound_archive_without_git(owned, monkeypatch):
    root = owned / "archive"
    document = minimal_archive(root)
    monkeypatch.setattr(guard, "__file__", str(root / "importers" / "monarch" / "mutation_guard.py"))
    assert guard.current_incremental_release() == {
        "commit": document["commit"], "codeHash": document["codeHash"],
    }
    assert not (root / ".git").exists()


@pytest.mark.parametrize("change", ["source", "migration", "missing", "extra-source", "extra-migration",
                                  "extra-cache", "nonruntime-file", "codehash", "no-manifest", "path"])
def test_archive_rejects_every_file_drift_without_enclosing_git_fallback(owned, change, monkeypatch):
    root = owned / "archive"
    document = minimal_archive(root)
    if change == "source":
        write(root / "finance_store" / "incremental.py", "# Altered.\n")
    elif change == "migration":
        write(root / "deploy" / "postgres" / "migrations" / "0001_synthetic.sql", "-- Altered.\n")
    elif change == "missing":
        (root / "finance_store" / "incremental.py").unlink()
    elif change in {"extra-source", "extra-migration", "extra-cache", "nonruntime-file"}:
        name = {"extra-source": "finance_store/extra.py", "extra-migration": "deploy/postgres/migrations/0002_extra.sql",
                "extra-cache": "finance_store/__pycache__/incremental.pyc", "nonruntime-file": "unexpected.txt"}[change]
        write(root / name, "Synthetic unexpected file.")
    elif change == "codehash":
        document["codeHash"] = "d" * 64
        write(root / release.MANIFEST, document)
    elif change == "path":
        document["files"]["../outside.py"] = "e" * 64
        write(root / release.MANIFEST, document)
    else:
        (root / release.MANIFEST).unlink()
    monkeypatch.setattr(guard, "__file__", str(root / "importers" / "monarch" / "mutation_guard.py"))
    with pytest.raises(guard.MutationInterlockError, match="unavailable"):
        guard.current_incremental_release()


def test_reparse_release_and_external_secret_are_rejected(owned):
    actual = owned / "actual"
    minimal_archive(actual)
    link = owned / "linked"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation permission unavailable")
    with pytest.raises(release.ReleaseError, match="reparse"):
        release.verify_archive(link)
    root = owned / "data"
    root.mkdir()
    write(actual / "synthetic-secret.txt", "synthetic-secret")
    (root / "escape").symlink_to(actual, target_is_directory=True)
    with pytest.raises(release.ReleaseError, match="reparse"):
        launcher._secret(root, "escape/synthetic-secret.txt")


def test_launcher_environment_never_inherits_credentials_or_mutation_authority(monkeypatch):
    values = {"FINANCE_INCREMENTAL_DSN": "synthetic-inherited-dsn", "WEALTHFOLIO_PASSWORD": "synthetic-inherited",
              "WEALTHFOLIO_MUTATIONS_ENABLED": "true", "WEALTHFOLIO_WRITER_OWNERSHIP_MARKER": "synthetic-marker",
              "WEALTHFOLIO_PROMOTION_EVIDENCE_KEY": "a" * 64, "PGPASSWORD": "synthetic-inherited",
              "HTTP_PROXY": "http://127.0.0.1:1"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    with launcher.isolated_environment():
        assert not any(name in os.environ for name in values)
        os.environ["FINANCE_INCREMENTAL_DSN"] = "synthetic-file-dsn"
    assert all(os.environ[name] == value for name, value in values.items())


def test_worker_output_never_forwards_private_fields_or_raw_error_details():
    output = launcher._safe_worker_output(json.dumps({
        "state": "held", "reason": "synthetic private error password=synthetic-secret",
        "balance": "123", "accountId": "SYN-ACCOUNT", "operationCount": 0,
        "proposedOperationCount": 14, "journaledOperationCount": 0, "appliedOperationCount": True,
        "plannedBalanceMatchesSource": False, "newHistoricalReviewCount": "1", "frontierReviewCount": 1,
    }), str(uuid.UUID(int=1)))
    assert output == {"scopeId": str(uuid.UUID(int=1)), "state": "held", "reason": "detail-withheld",
                      "operationCount": 0, "proposedOperationCount": 14, "journaledOperationCount": 0,
                      "plannedBalanceMatchesSource": False, "frontierReviewCount": 1}


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout.decode().strip()


@pytest.fixture(scope="module")
def package_source():
    with owned_directory() as owned:
        root = owned / "synthetic source"
        root.mkdir()
        for prefix in ("finance_store", "importers",
                       "deploy/incremental", "deploy/postgres/migrations"):
            for source in (ROOT / prefix).rglob("*"):
                if source.is_file() and source.suffix in {".py", ".json", ".sql", ".ps1"}:
                    target = root / source.relative_to(ROOT)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
        shutil.copyfile(ROOT / "requirements.txt", root / "requirements.txt")
        git(root, "init", "--quiet")
        git(root, "add", ".")
        git(root, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Synthetic packaging fixture")
        yield root


def powershell_install(source, target, *, what_if=False):
    if not POWERSHELL:
        pytest.skip("PowerShell unavailable")
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    command = (
        f"& {quote(source / 'deploy' / 'incremental' / 'install-release.ps1')} "
        f"-RepositoryRoot {quote(source)} -ReleaseRoot {quote(target)} -Python {quote(sys.executable)} "
        + ("-WhatIf" if what_if else "-Confirm:$false")
    )
    return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, cwd=source, timeout=120)


@pytest.fixture(scope="module")
def installed_template(package_source):
    target = package_source.parent / "incremental runtime"
    result = powershell_install(package_source, target)
    assert result.returncode == 0, result.stdout + result.stderr
    yield target


@pytest.fixture
def packaged(installed_template, owned):
    target = owned / "incremental runtime"
    shutil.copytree(installed_template, target)
    pointer = release.read_document(target / release.POINTER)
    pointer["releasePath"] = str(target / release.RELEASES / pointer["commit"])
    write(target / release.POINTER, pointer)
    return target


def test_real_powershell_archive_install_and_idempotent_reinstall(package_source, installed_template):
    root, pointer = release.verify_pointer(installed_template)
    assert not (root / ".git").exists()
    assert release.current_release(root)["commit"] == git(package_source, "rev-parse", "HEAD")
    before = (installed_template / release.POINTER).read_bytes()
    result = powershell_install(package_source, installed_template)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (installed_template / release.POINTER).read_bytes() == before
    assert set(path.name for path in installed_template.iterdir()) == {
        release.RELEASES, release.POINTER, release.LAUNCHER,
    }
    assert pointer["pythonExecutable"] == str(Path(sys.executable).resolve())


def test_installer_does_not_touch_collector_root_or_create_anything_on_whatif(package_source, owned):
    collector = owned / "collector"
    old_pointer = write(collector / "current.json", {"synthetic": "collector pointer"})
    before = old_pointer.read_bytes()
    result = powershell_install(package_source, collector)
    assert result.returncode != 0 and old_pointer.read_bytes() == before
    assert list(collector.iterdir()) == [old_pointer]
    planned = owned / "not-installed"
    result = powershell_install(package_source, planned, what_if=True)
    assert result.returncode == 0 and not planned.exists()


def test_dirty_release_is_refused_without_affecting_pointer(package_source, installed_template):
    path = package_source / "dirty.txt"
    before = (installed_template / release.POINTER).read_bytes()
    try:
        path.write_text("Synthetic uncommitted file.")
        result = powershell_install(package_source, installed_template)
        assert result.returncode != 0
        assert (installed_template / release.POINTER).read_bytes() == before
    finally:
        path.unlink()


def launch_command(packaged, configuration, *, command="run", enabled=True, expected_hash=None, env=None):
    arguments = [
        POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(packaged / release.LAUNCHER),
        "-ReleaseRoot", str(packaged), "-Configuration", str(configuration),
        "-ConfigurationSha256", expected_hash or release.digest(configuration), "-Command", command,
    ]
    if enabled:
        arguments.append("-EnableMutations")
    return subprocess.run(arguments, capture_output=True, text=True, cwd=packaged, env=env, timeout=120)


@pytest.mark.parametrize("change", ["source", "migration", "missing", "extra", "pointer", "manifest", "launcher"])
def test_real_powershell_preflight_rejects_archive_drift_before_loading_worker(packaged, owned, change):
    root, pointer = release.verify_pointer(packaged)
    if change == "source":
        write(root / "finance_store" / "incremental.py", 'raise RuntimeError("must not execute")')
    elif change == "migration":
        migration = next((root / "deploy" / "postgres" / "migrations").glob("*.sql"))
        write(migration, "-- Altered.")
    elif change == "missing":
        (root / "finance_store" / "incremental.py").unlink()
    elif change == "extra":
        write(root / "finance_store" / "extra.py", "# Unexpected.")
    elif change == "pointer":
        pointer["releasePath"] = str(owned)
        write(packaged / release.POINTER, pointer)
    elif change == "manifest":
        document = release.read_document(root / release.MANIFEST)
        document["commit"] = "e" * 40
        write(root / release.MANIFEST, document)
    else:
        with (packaged / release.LAUNCHER).open("a") as output:
            output.write("\n# Synthetic drift.\n")
    config = write(owned / "data" / "launcher.json", {"synthetic": True})
    result = launch_command(packaged, config)
    assert result.returncode != 0
    assert json.loads(result.stdout)["reason"] == "incremental-launcher-preflight-failed"
    assert not (owned / "data" / "automation").exists()


def native_configuration(estate, scope, database, packaged, *, evidence_key="a" * 64):
    from psycopg.conninfo import make_conninfo
    root, _ = release.verify_pointer(packaged)
    marker_path = estate.root / guard.WRITER_MARKER_RELATIVE
    marker = json.loads(marker_path.read_text())
    marker["release"] = release.current_release(root)
    marker["markerHash"] = guard.writer_marker_hash(marker)
    write(marker_path, marker)  # Separate synthetic operator activation, never the installer.
    write(estate.root / "secrets" / "incremental-dsn.txt", make_conninfo(**database))
    write(estate.root / "secrets" / "app-password.txt", "synthetic-password")
    write(estate.root / "secrets" / "evidence-key.txt", evidence_key)
    return write(estate.root / "automation" / "launcher.json", {
        "schemaVersion": 1, "kind": "incremental-launch-configuration", "dataRoot": str(estate.root),
        "scopes": [{
            "path": "scope.json", "scopeId": scope.scope_id, "configurationHash": scope.config_hash,
            "dsnFile": "secrets/incremental-dsn.txt", "passwordFile": "secrets/app-password.txt",
            "evidenceKeyFile": "secrets/evidence-key.txt",
        }],
    })


@NATIVE
def test_archived_scheduler_entrypoint_real_resolver_postgres_rest_create_then_fresh_noop(packaged, estate, database):
    scope = estate.baseline([])
    now = datetime.now(timezone.utc) - timedelta(seconds=5)
    estate.snapshot([estate.transaction()], "-10", now=now)
    configuration = native_configuration(estate, scope, database, packaged)
    marker_before = (estate.root / guard.WRITER_MARKER_RELATIVE).read_bytes()
    env = {**os.environ, "FINANCE_INCREMENTAL_DSN": "synthetic invalid inherited DSN",
           "WEALTHFOLIO_PASSWORD": "synthetic wrong inherited password",
           "WEALTHFOLIO_PROMOTION_EVIDENCE_KEY": "not-hex", "WEALTHFOLIO_WRITER_MODE": "wrong",
           "WEALTHFOLIO_WRITER_OWNERSHIP_MARKER": "synthetic-wrong-root",
           "FINANCE_DATA": "synthetic-inherited-root", "PGPASSWORD": "synthetic-wrong",
           "HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}
    first = launch_command(packaged, configuration, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert json.loads(first.stdout)["state"] == "applied"
    assert len(estate.app.rows) == 1 and len(estate.app.mutations) == 1
    second = launch_command(packaged, configuration, env=env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(second.stdout)["state"] == "noop"
    assert len(estate.app.mutations) == 1
    assert (estate.root / guard.WRITER_MARKER_RELATIVE).read_bytes() == marker_before
    receipt = json.loads((estate.root / "automation" / "incremental-launcher" / "current.json").read_text())
    assert receipt["state"] == "completed" and receipt["scopes"][0]["state"] == "noop"
    assert len(list((estate.root / "automation" / "incremental-launcher" / "runs").glob("*.json"))) == 2


@NATIVE
@pytest.mark.parametrize("failure", ["opt-in", "marker", "scope", "scope-hash", "configuration-hash",
                                   "dsn", "password", "evidence-key", "busy", "marker-origin", "marker-instance",
                                   "marker-environment", "marker-scope", "marker-config", "marker-release",
                                   "migration-gate"])
def test_archived_launcher_holds_before_mutation_on_bindings_secrets_and_global_gate(
    packaged, estate, database, connection, failure,
):
    from finance_store.postgres import GLOBAL_WRITER_LOCK
    scope = estate.baseline([])
    estate.snapshot([estate.transaction()], "-10", now=datetime.now(timezone.utc) - timedelta(seconds=5))
    config = native_configuration(estate, scope, database, packaged)
    document = json.loads(config.read_text())
    if failure == "marker":
        (estate.root / guard.WRITER_MARKER_RELATIVE).unlink()
    if failure.startswith("marker-"):
        marker_path = estate.root / guard.WRITER_MARKER_RELATIVE
        marker = json.loads(marker_path.read_text())
        if failure == "marker-origin":
            marker["origin"] = "http://127.0.0.1:1"
        elif failure in {"marker-instance", "marker-environment"}:
            marker["instanceId" if failure == "marker-instance" else "environmentId"] = "d" * 64
        elif failure in {"marker-scope", "marker-config"}:
            marker["scopes"][0]["scopeId" if failure == "marker-scope" else "configurationHash"] = (
                str(uuid.uuid4()) if failure == "marker-scope" else "d" * 64
            )
        else:
            marker["release"]["codeHash"] = "d" * 64
        marker["markerHash"] = guard.writer_marker_hash(marker)
        write(marker_path, marker)
    if failure == "scope":
        document["scopes"][0]["scopeId"] = str(uuid.uuid4())
    if failure == "scope-hash":
        document["scopes"][0]["configurationHash"] = "e" * 64
    if failure in {"dsn", "password", "evidence-key"}:
        secret = {"dsn": "incremental-dsn.txt", "password": "app-password.txt", "evidence-key": "evidence-key.txt"}[failure]
        (estate.root / "secrets" / secret).unlink()
    write(config, document)
    if failure == "busy":
        connection.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (GLOBAL_WRITER_LOCK,))
    if failure == "migration-gate":
        import psycopg
        with psycopg.connect(**database, autocommit=True) as admin:
            admin.execute("UPDATE finance.writer_gate SET migrations_blocked=true,owner_token='synthetic-migration' WHERE singleton")
    try:
        result = launch_command(packaged, config, enabled=failure != "opt-in",
                                expected_hash="0" * 64 if failure == "configuration-hash" else None)
    finally:
        if failure == "busy":
            connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (GLOBAL_WRITER_LOCK,))
    assert result.returncode != 0 and json.loads(result.stdout)["state"] == "held"
    assert not estate.app.mutations and not estate.app.backups
    assert "synthetic-password" not in result.stdout + result.stderr


@NATIVE
def test_archived_launcher_local_scope_failure_does_not_stop_healthy_scope(packaged, estate, database):
    scope = estate.baseline([])
    estate.snapshot([estate.transaction()], "-10", now=datetime.now(timezone.utc) - timedelta(seconds=5))
    config = native_configuration(estate, scope, database, packaged)
    document = json.loads(config.read_text())
    document["scopes"].insert(0, {**document["scopes"][0], "scopeId": str(uuid.uuid4()), "path": "missing-scope.json"})
    write(config, document)
    result = launch_command(packaged, config)
    assert result.returncode == 1
    states = [json.loads(line)["state"] for line in result.stdout.splitlines()]
    assert states == ["held", "applied"] and len(estate.app.mutations) == 1


@NATIVE
def test_archive_uses_file_evidence_key_for_bound_source_anchor_not_inherited_key(packaged, estate, database):
    from tests.test_incremental import _source_anchor_case
    scope, current = _source_anchor_case(estate)
    estate.snapshot(current, "89", now=datetime.now(timezone.utc) - timedelta(seconds=5))
    config = native_configuration(
        estate, scope, database, packaged, evidence_key=b"synthetic-bootstrap-verification-key".hex(),
    )
    env = {**os.environ, "SYNTHETIC_INCREMENTAL_BOOTSTRAP_KEY": "bad-inherited-value"}
    result = launch_command(packaged, config, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["state"] == "applied"
    assert len(estate.app.mutations) == 3
    assert not any(row.get("idempotencyKey") == "simplefin:SYN-APP:REMOVED" for row in estate.app.rows)
    replay = launch_command(packaged, config, env=env)
    assert replay.returncode == 0 and json.loads(replay.stdout)["state"] == "noop"
    assert len(estate.app.mutations) == 3


@NATIVE
def test_archive_status_and_plan_do_not_activate_a_writer(packaged, estate, database):
    scope = estate.baseline([])
    estate.snapshot([estate.transaction()], "-10", now=datetime.now(timezone.utc) - timedelta(seconds=5))
    config = native_configuration(estate, scope, database, packaged)
    marker_path = estate.root / guard.WRITER_MARKER_RELATIVE
    marker_path.unlink()
    (estate.root / "secrets" / "app-password.txt").unlink()
    (estate.root / "secrets" / "evidence-key.txt").unlink()
    status = launch_command(packaged, config, command="status", enabled=False)
    assert status.returncode == 0 and json.loads(status.stdout)["state"] == "not-initialized"
    write(estate.root / "secrets" / "app-password.txt", "synthetic-password")
    write(estate.root / "secrets" / "evidence-key.txt", "a" * 64)
    plan = launch_command(packaged, config, command="plan", enabled=False)
    assert plan.returncode == 0 and json.loads(plan.stdout)["state"] == "pending"
    assert not marker_path.exists() and not estate.app.mutations and not estate.app.backups
