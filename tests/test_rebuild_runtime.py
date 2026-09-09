"""Synthetic regressions for the shared bounded-repair and backup safeguards."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
from types import SimpleNamespace

import pytest

from importers.rebuild import cutover, projector, receipt_repair_cli
from importers.rebuild.cutover import (
    CutoverError,
    DockerContainerRuntime,
    _copy_verified,
    _write_cutover_state,
    download_backup,
    stage_restore_copy,
    verify_backup_file,
    verify_stage_restore_report,
)
from importers.rebuild.projector import (
    REBUILD_DB_PATH,
    REBUILD_FINGERPRINT_ENV,
    REBUILD_MARKER,
    WEALTHFOLIO_VERSION,
    ProjectionError,
    _require_backup,
    inspect_rebuild_target,
    require_rebuild_target,
    semantic_hash,
    validate_rebuild_boundary,
    wait_for_recalculation,
)


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:18091"
BACKUP_NAME = "wealthfolio_backup_20260102_030405.db"


class Clock:
    def __init__(self):
        self.elapsed = 0.0

    def __call__(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


class TargetClient:
    def __init__(self, **info):
        self.info = {
            "version": WEALTHFOLIO_VERSION,
            "dbPath": REBUILD_DB_PATH,
            **info,
        }

    def get(self, path):
        assert path == "/app/info"
        return dict(self.info)


@pytest.mark.parametrize(
    ("url", "marker", "message"),
    [
        ("http://127.0.0.1:8088", REBUILD_MARKER, "production"),
        ("http://192.0.2.1:18091", REBUILD_MARKER, "loopback"),
        ("http://127.0.0.1", REBUILD_MARKER, "explicit"),
        ("ftp://127.0.0.1:18091", REBUILD_MARKER, "loopback"),
        (ORIGIN, None, "marker"),
        (ORIGIN, "incorrect-marker", "marker"),
    ],
)
def test_rebuild_boundary_fails_before_credentials_or_transport(
    tmp_path, monkeypatch, url, marker, message
):
    def forbidden(*_args, **_kwargs):
        pytest.fail("boundary rejection must precede credentials and transport")

    monkeypatch.setattr(receipt_repair_cli, "_password", forbidden)
    monkeypatch.setattr(receipt_repair_cli, "WealthfolioClient", forbidden)
    with pytest.raises(ProjectionError, match=message):
        receipt_repair_cli._client(
            url, None, tmp_path, marker=marker, expected_instance_id="synthetic"
        )


@pytest.mark.parametrize("url", [ORIGIN, "https://localhost:18091", "http://[::1]:18091"])
def test_rebuild_boundary_accepts_only_marked_explicit_loopback(url):
    validate_rebuild_boundary(url, REBUILD_MARKER)


@pytest.mark.parametrize(
    ("info", "message"),
    [
        ({"version": "3.6.0"}, "version"),
        ({"version": ""}, "version"),
        ({"dbPath": "/data/wealthfolio.db"}, "database path"),
        ({"dbPath": ""}, "database path"),
    ],
)
def test_rebuild_target_requires_exact_version_and_database(info, message):
    with pytest.raises(ProjectionError, match=message):
        inspect_rebuild_target(TargetClient(**info), ORIGIN, REBUILD_MARKER)


def test_rebuild_target_requires_matching_authenticated_origin_fingerprint():
    client = TargetClient()
    identity = inspect_rebuild_target(client, ORIGIN, REBUILD_MARKER)
    assert identity.version == WEALTHFOLIO_VERSION
    assert identity.db_path == REBUILD_DB_PATH
    assert require_rebuild_target(
        client, ORIGIN + "/", REBUILD_MARKER, identity.fingerprint
    ) == identity
    with pytest.raises(ProjectionError, match=REBUILD_FINGERPRINT_ENV):
        require_rebuild_target(client, ORIGIN, REBUILD_MARKER, None)
    with pytest.raises(ProjectionError, match="fingerprint"):
        require_rebuild_target(
            client, "http://127.0.0.1:18092", REBUILD_MARKER, identity.fingerprint
        )


def test_repair_client_authenticates_before_identity_reads(tmp_path, monkeypatch):
    events = []

    class Client(TargetClient):
        def __init__(self, origin, *, writer_data_dir):
            super().__init__()
            assert origin == ORIGIN and writer_data_dir == tmp_path

        def health(self):
            events.append("health")
            return True

        def login(self, password):
            assert password == "synthetic-test-only"
            events.append("login")

        def get(self, path):
            assert "login" in events
            events.append("identity")
            return super().get(path)

    expected = inspect_rebuild_target(TargetClient(), ORIGIN, REBUILD_MARKER)
    monkeypatch.setattr(receipt_repair_cli, "WealthfolioClient", Client)
    monkeypatch.setattr(
        receipt_repair_cli, "_password", lambda _path: "synthetic-test-only"
    )
    receipt_repair_cli._client(
        ORIGIN, None, tmp_path, marker=REBUILD_MARKER,
        expected_instance_id=expected.fingerprint,
    )
    assert events[:2] == ["health", "login"]
    assert events[2:] == ["identity", "identity"]


def test_backup_creation_waits_out_same_second_and_verifies_unique_inventory():
    class BackupClient:
        def __init__(self):
            self.backups = [{"filename": BACKUP_NAME}]
            self.pending = None
            self.reads_after_create = 0

        def list_backups(self):
            if self.pending is not None:
                self.reads_after_create += 1
                if self.reads_after_create >= 2:
                    self.backups.append(self.pending)
                    self.pending = None
            return deepcopy(self.backups)

        def backup_database(self):
            self.pending = {"filename": "wealthfolio_backup_20260102_030406.db"}
            return deepcopy(self.pending)

    client = BackupClient()
    clock = Clock()
    result = _require_backup(
        client, timeout_seconds=1, interval_seconds=0.1,
        clock=clock, sleeper=clock.sleep,
        utcnow=lambda: datetime(
            2026, 1, 2, 3, 4, 6 if clock.elapsed else 5, tzinfo=timezone.utc
        ),
    )
    assert result["filename"] == "wealthfolio_backup_20260102_030406.db"
    assert client.reads_after_create == 2


@pytest.mark.parametrize(
    "inventory",
    [None, {}, [None], [{}], [{"filename": ""}], [{"filename": 1}]],
)
def test_backup_creation_rejects_invalid_inventory_before_mutation(inventory):
    client = SimpleNamespace(list_backups=lambda: inventory)
    with pytest.raises(ProjectionError, match="inventory is invalid"):
        _require_backup(client)


def test_backup_creation_rejects_duplicate_inventory_before_mutation():
    client = SimpleNamespace(
        list_backups=lambda: [{"filename": BACKUP_NAME}] * 2
    )
    with pytest.raises(ProjectionError, match="duplicate"):
        _require_backup(client)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ([], [BACKUP_NAME, "wealthfolio_backup_20260102_030406.db"]),
        (["older.db"], [BACKUP_NAME]),
        ([], ["wealthfolio_backup_20260102_030406.db"]),
    ],
)
def test_backup_creation_rejects_concurrent_inventory_changes(before, after):
    inventories = iter([before, after])
    client = SimpleNamespace(
        list_backups=lambda: [{"filename": name} for name in next(inventories)],
        backup_database=lambda: {"filename": BACKUP_NAME},
    )
    with pytest.raises(ProjectionError, match="ambiguously"):
        _require_backup(client)


@pytest.mark.parametrize("visible_collision", [False, True])
def test_backup_creation_times_out_without_a_unique_backup(visible_collision):
    clock = Clock()
    client = SimpleNamespace(
        list_backups=lambda: [{"filename": BACKUP_NAME}] if visible_collision else [],
        backup_database=lambda: {"filename": BACKUP_NAME},
    )
    with pytest.raises(ProjectionError, match="timestamp|did not appear uniquely"):
        _require_backup(
            client, timeout_seconds=0.2, interval_seconds=0.1,
            clock=clock, sleeper=clock.sleep,
            utcnow=lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize("reply", [None, {}, {"filename": "unsafe.db"}])
def test_backup_creation_requires_confirmed_filename(reply):
    client = SimpleNamespace(
        list_backups=lambda: [], backup_database=lambda: reply
    )
    with pytest.raises(ProjectionError, match="confirmed backup"):
        _require_backup(client)


def test_backup_creation_rejects_server_reusing_an_existing_filename():
    client = SimpleNamespace(
        list_backups=lambda: [{"filename": BACKUP_NAME}],
        backup_database=lambda: {"filename": BACKUP_NAME},
    )
    with pytest.raises(ProjectionError, match="overwrite"):
        _require_backup(
            client,
            utcnow=lambda: datetime(2026, 1, 2, 3, 4, 6, tzinfo=timezone.utc),
        )


def test_unique_backup_holds_process_lock_across_inventory_and_creation(monkeypatch):
    events = []
    held = False

    class Lock:
        def __enter__(self):
            nonlocal held
            assert not held
            held = True
            events.append("lock")

        def __exit__(self, *_args):
            nonlocal held
            held = False
            events.append("unlock")

    class Client:
        created = False

        def list_backups(self):
            assert held
            events.append("inventory")
            return [{"filename": BACKUP_NAME}] if self.created else []

        def backup_database(self):
            assert held
            events.append("create")
            self.created = True
            return {"filename": BACKUP_NAME}

    monkeypatch.setattr(projector, "_BACKUP_LOCK", Lock())
    assert projector.require_unique_backup(Client()) == {"filename": BACKUP_NAME}
    assert events == ["lock", "inventory", "create", "inventory", "unlock"]


def test_wait_for_recalculation_requires_stable_scoped_reads_and_minimum_wait():
    clock = Clock()
    responses = iter([
        [{"accountId": "SYN-B", "totalValue": "1"}, {"accountId": "SYN-A"}],
        [{"accountId": "SYN-A"}, {"accountId": "SYN-B", "totalValue": "2"}],
    ])
    expected = [{"accountId": "SYN-A"}, {"accountId": "SYN-B", "totalValue": "2"}]
    calls = []

    def post(path, payload):
        calls.append((path, payload))
        return next(responses, list(reversed(expected)))

    result = wait_for_recalculation(
        SimpleNamespace(post=post), ["SYN-B", "SYN-A", "SYN-B"],
        timeout_seconds=5, interval_seconds=0.5,
        clock=clock, sleeper=clock.sleep,
    )
    assert result == expected
    assert len(calls) == 5 and clock.elapsed == 2
    assert all(
        call == ("/performance/accounts/simple", {"accountIds": ["SYN-A", "SYN-B"]})
        for call in calls
    )


def test_wait_for_recalculation_rejects_invalid_response_and_unsettled_values():
    clock = Clock()
    with pytest.raises(ProjectionError, match="invalid response"):
        wait_for_recalculation(SimpleNamespace(post=lambda *_args: {}), [])
    with pytest.raises(ProjectionError, match="timeout"):
        wait_for_recalculation(
            SimpleNamespace(post=lambda *_args: [
                {"accountId": "SYN-A", "totalValue": clock.elapsed}
            ]),
            ["SYN-A"], timeout_seconds=1, interval_seconds=0.5,
            clock=clock, sleeper=clock.sleep,
        )


def sqlite_backup(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.executescript(
                """
                CREATE TABLE __diesel_schema_migrations (
                    version TEXT NOT NULL, run_on TEXT NOT NULL
                );
                INSERT INTO __diesel_schema_migrations
                VALUES ('synthetic-v1', '2026-01-02T00:00:00Z');
                CREATE TABLE accounts (id TEXT PRIMARY KEY);
                CREATE TABLE activities (id TEXT PRIMARY KEY);
                CREATE TABLE assets (id TEXT PRIMARY KEY);
                CREATE TABLE app_settings (id TEXT PRIMARY KEY);
                """
            )
    finally:
        connection.close()
    return path


@pytest.fixture
def backup_paths(tmp_path):
    private = tmp_path / "private"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    backup = sqlite_backup(private / "backups" / "synthetic.db")
    return private, checkout, backup


def test_backup_verification_is_read_only_and_binds_bytes_schema_and_path(backup_paths):
    private, checkout, backup = backup_paths
    content = backup.read_bytes()
    evidence = verify_backup_file(backup, data_dir=private, repo_root=checkout)
    assert evidence == {
        "path": "backups/synthetic.db",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "schemaMigration": "synthetic-v1",
    }
    assert backup.read_bytes() == content
    assert sorted(path.name for path in backup.parent.iterdir()) == [backup.name]


@pytest.mark.parametrize("kind", ["empty", "text", "corrupt", "missing-table", "no-migration"])
def test_backup_verification_rejects_invalid_sqlite_evidence(backup_paths, kind):
    private, checkout, backup = backup_paths
    if kind in {"empty", "text", "corrupt"}:
        backup.write_bytes({
            "empty": b"", "text": b"not a SQLite database",
            "corrupt": cutover.SQLITE_HEADER + b"synthetic corruption" * 20,
        }[kind])
    else:
        connection = sqlite3.connect(backup)
        try:
            with connection:
                connection.execute(
                    "DROP TABLE activities" if kind == "missing-table"
                    else "DELETE FROM __diesel_schema_migrations"
                )
        finally:
            connection.close()
    with pytest.raises(CutoverError):
        verify_backup_file(backup, data_dir=private, repo_root=checkout)


@pytest.mark.parametrize("destination", ["checkout", "outside"])
def test_backup_and_restore_reject_non_private_paths_before_transport(
    backup_paths, destination
):
    private, checkout, backup = backup_paths
    target = (checkout if destination == "checkout" else private.parent) / "rejected.db"
    with pytest.raises(CutoverError, match="private data directory"):
        download_backup(
            object(), BACKUP_NAME, target, data_dir=private, repo_root=checkout
        )
    with pytest.raises(CutoverError, match="private data directory"):
        stage_restore_copy(backup, target, data_dir=private, repo_root=checkout)
    with pytest.raises(CutoverError, match="private data directory"):
        verify_backup_file(backup, data_dir=checkout, repo_root=checkout)
    assert not target.exists()


def test_backup_download_is_private_verified_and_refuses_overwrite(backup_paths):
    private, checkout, backup = backup_paths
    content = backup.read_bytes()
    client = SimpleNamespace(
        download_backup=lambda _filename: content,
        list_backups=lambda: [{"filename": BACKUP_NAME, "sizeBytes": len(content)}],
    )
    destination = private / "downloads" / "before.db"
    evidence = download_backup(
        client, BACKUP_NAME, destination, data_dir=private, repo_root=checkout
    )
    assert evidence["path"] == "downloads/before.db"
    assert evidence["sha256"] == hashlib.sha256(content).hexdigest()
    assert destination.read_bytes() == content
    assert not destination.stat().st_mode & stat.S_IWRITE
    with pytest.raises(CutoverError, match="already exists"):
        download_backup(
            object(), BACKUP_NAME, destination, data_dir=private, repo_root=checkout
        )


@pytest.mark.parametrize("problem", ["non-sqlite", "absent", "duplicate", "size"])
def test_backup_download_rejects_content_or_inventory_mismatch(backup_paths, problem):
    private, checkout, backup = backup_paths
    content = backup.read_bytes()
    inventory = [{"filename": BACKUP_NAME, "sizeBytes": len(content)}]
    if problem == "non-sqlite":
        content = b"synthetic non-SQLite response"
    elif problem == "absent":
        inventory = []
    elif problem == "duplicate":
        inventory *= 2
    else:
        inventory[0]["sizeBytes"] += 1
    client = SimpleNamespace(
        download_backup=lambda _filename: content, list_backups=lambda: inventory
    )
    target = private / "downloads" / "before.db"
    with pytest.raises(CutoverError):
        download_backup(client, BACKUP_NAME, target, data_dir=private, repo_root=checkout)
    assert not target.exists()


def test_restore_copy_is_writable_hash_identical_and_preserves_sealed_backup(backup_paths):
    private, checkout, backup = backup_paths
    backup.chmod(stat.S_IREAD)
    original = backup.read_bytes()
    destination = private / "restore" / "wealthfolio-rebuild.db"
    report = stage_restore_copy(backup, destination, data_dir=private, repo_root=checkout)
    evidence = verify_backup_file(backup, data_dir=private, repo_root=checkout)
    body = {key: value for key, value in report.items() if key != "stageHash"}
    assert report["sourceSha256"] == report["stagedSha256"] == evidence["sha256"]
    assert report["stageHash"] == hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    assert verify_stage_restore_report(
        report, backup=evidence, restored_database=destination,
        data_dir=private, repo_root=checkout,
    ) == report
    with pytest.raises(CutoverError, match="already exists"):
        stage_restore_copy(backup, destination, data_dir=private, repo_root=checkout)
    with destination.open("ab") as target:
        target.write(b"synthetic startup changes")
    assert backup.read_bytes() == original
    # The stage receipt describes pre-startup bytes, not the post-startup database.
    assert verify_stage_restore_report(
        report, backup=evidence, restored_database=destination,
        data_dir=private, repo_root=checkout,
    ) == report


@pytest.mark.parametrize(
    "field",
    ["schemaVersion", "kind", "sourcePath", "stagedPath", "sourceSha256",
     "stagedSha256", "size", "schemaMigration", "unexpected"],
)
def test_restore_receipt_rejects_mismatched_bindings_even_if_rehashed(backup_paths, field):
    private, checkout, backup = backup_paths
    destination = private / "restore.db"
    report = stage_restore_copy(backup, destination, data_dir=private, repo_root=checkout)
    report[field] = "synthetic mismatch"
    report["stageHash"] = semantic_hash({
        key: value for key, value in report.items() if key != "stageHash"
    })
    with pytest.raises(CutoverError, match="changed or mismatched"):
        verify_stage_restore_report(
            report,
            backup=verify_backup_file(backup, data_dir=private, repo_root=checkout),
            restored_database=destination, data_dir=private, repo_root=checkout,
        )


def test_verified_copy_preserves_source_and_rejects_wrong_hash_or_existing_slot(backup_paths):
    private, _checkout, source = backup_paths
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    destination = private / "incoming.db"
    _copy_verified(source, destination, digest)
    assert destination.read_bytes() == content
    assert source.read_bytes() == content
    with pytest.raises(CutoverError, match="already exists"):
        _copy_verified(source, destination, digest)
    with pytest.raises(CutoverError, match="hash"):
        _copy_verified(source, private / "wrong-hash.db", "0" * 64)


def test_verified_copy_refuses_cross_filesystem_before_creating_slot(backup_paths, monkeypatch):
    private, _checkout, source = backup_paths
    destination = private / "incoming.db"
    original_stat = Path.stat

    def different_device(path, *args, **kwargs):
        metadata = original_stat(path, *args, **kwargs)
        if path == destination.parent:
            return SimpleNamespace(st_dev=metadata.st_dev + 1)
        return metadata

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", different_device)
        with pytest.raises(CutoverError, match="one filesystem"):
            _copy_verified(source, destination, hashlib.sha256(source.read_bytes()).hexdigest())
    assert not destination.exists()


def test_recovery_journal_is_fsynced_before_replace_and_directory_after(tmp_path, monkeypatch):
    journal = tmp_path / "recovery.json"
    events = []
    replace = cutover.os.replace

    def recording_replace(source, target):
        assert json.loads(Path(source).read_text()) == {"phase": "staged"}
        events.append("replace")
        replace(source, target)

    monkeypatch.setattr(cutover.os, "fsync", lambda _fd: events.append("file-fsync"))
    monkeypatch.setattr(cutover, "fsync_directory", lambda _path: events.append("dir-fsync"))
    _write_cutover_state(journal, {"phase": "prepared"}, create=True)
    assert events == ["file-fsync", "dir-fsync"]
    with pytest.raises(CutoverError, match="already exists"):
        _write_cutover_state(journal, {"phase": "replaced"}, create=True)
    events.clear()
    monkeypatch.setattr(cutover.os, "replace", recording_replace)
    _write_cutover_state(journal, {"phase": "staged"}, create=False)
    assert events == ["file-fsync", "replace", "dir-fsync"]
    assert json.loads(journal.read_text()) == {"phase": "staged"}
    assert list(tmp_path.iterdir()) == [journal]


def test_failed_journal_replace_preserves_previous_recovery_state(tmp_path, monkeypatch):
    journal = tmp_path / "recovery.json"
    _write_cutover_state(journal, {"phase": "prepared"}, create=True)
    original = journal.read_bytes()

    def fail(*_args):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(cutover.os, "replace", fail)
    with pytest.raises(OSError, match="synthetic publication failure"):
        _write_cutover_state(journal, {"phase": "staged"}, create=False)
    assert journal.read_bytes() == original
    assert list(tmp_path.iterdir()) == [journal]


@pytest.fixture
def docker_runtime():
    row = {
        "Id": "c" * 64, "Name": "/synthetic-wealthfolio", "Image": "i" * 64,
        "Config": {"Labels": {
            "com.docker.compose.project": "synthetic-finance",
            "com.docker.compose.service": "wealthfolio",
        }},
        "State": {"Running": True},
    }
    commands = []

    def runner(command, **kwargs):
        assert kwargs == {
            "check": True, "capture_output": True, "text": True, "timeout": 90
        }
        commands.append(command)
        if command[1] == "inspect":
            return SimpleNamespace(stdout=json.dumps([row]))
        assert command[1] in {"start", "stop"}
        assert command[-1] == row["Id"]
        row["State"]["Running"] = command[1] == "start"
        return SimpleNamespace(stdout="")

    runtime = DockerContainerRuntime(
        project="synthetic-finance", service="wealthfolio",
        container="synthetic-wealthfolio", runner=runner,
    )
    return runtime, row, commands


def test_docker_runtime_targets_only_inspected_container_id(docker_runtime):
    runtime, _row, commands = docker_runtime
    identity = runtime.identity()
    runtime.stop(identity)
    runtime.stop(identity)
    assert runtime.is_running(identity) is False
    runtime.start(identity)
    runtime.start(identity)
    assert runtime.is_running(identity) is True
    assert [command for command in commands if command[1] != "inspect"] == [
        ["docker", "stop", "--time", "60", "c" * 64],
        ["docker", "start", "c" * 64],
    ]


@pytest.mark.parametrize("field", ["project", "service", "container", "id", "image"])
def test_docker_runtime_rejects_identity_drift_before_any_control(docker_runtime, field):
    runtime, row, commands = docker_runtime
    identity = runtime.identity()
    if field in {"project", "service"}:
        row["Config"]["Labels"][f"com.docker.compose.{field}"] = "other"
    else:
        row[{"container": "Name", "id": "Id", "image": "Image"}[field]] = "d" * 64
    for operation in (runtime.start, runtime.stop, runtime.is_running):
        with pytest.raises(CutoverError, match="allowlist|identity changed"):
            operation(identity)
    assert all(command[1] == "inspect" for command in commands)


@pytest.mark.parametrize("field", ["project", "service", "container"])
def test_docker_runtime_rejects_unsafe_selectors_without_invoking_docker(field):
    selectors = {"project": "synthetic", "service": "wealthfolio", "container": "synthetic"}
    selectors[field] = "--all"
    with pytest.raises(CutoverError, match="invalid Docker"):
        DockerContainerRuntime(**selectors, runner=lambda *_args, **_kwargs: pytest.fail())


@pytest.mark.parametrize("output", ["not json", "[]", "[{}, {}]", "{}", "[null]"])
def test_docker_runtime_requires_exactly_one_inspection_record(output):
    runtime = DockerContainerRuntime(
        project="synthetic", service="wealthfolio", container="synthetic",
        runner=lambda *_args, **_kwargs: SimpleNamespace(stdout=output),
    )
    with pytest.raises(CutoverError, match="JSON|exactly one"):
        runtime.identity()


def test_rebuild_compose_is_pinned_isolated_and_authenticated():
    compose = (ROOT / "deploy" / "wealthfolio-rebuild" / "compose.yml").read_text()
    assert ":latest" not in compose
    assert "3.7.0@sha256:" in compose
    assert "18091" in compose
    assert "8088}:8088" not in compose
    assert "/data/wealthfolio-rebuild.db" in compose
    assert 'WF_AUTH_REQUIRED: "true"' in compose
    assert "/api/v1/healthz" in compose
    assert REBUILD_MARKER in compose


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell unavailable")
def test_rebuild_init_env_rejects_paths_inside_checkout_before_generating_secrets(tmp_path):
    script = ROOT / "deploy" / "wealthfolio-rebuild" / "init-env.ps1"
    forbidden_data = script.parent / f".pytest-{tmp_path.name}-data"
    forbidden_env = script.parent / f".pytest-{tmp_path.name}.env"
    assert not forbidden_data.exists() and not forbidden_env.exists()
    for arguments in (
        ["-DataDir", str(forbidden_data)],
        ["-DataDir", str(ROOT.parent / "synthetic-unused"), "-EnvFile", str(forbidden_env)],
    ):
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(script), *arguments],
            cwd=ROOT, check=False, capture_output=True, text=True,
        )
        assert result.returncode != 0
        assert "outside the public checkout" in result.stderr
    assert not forbidden_data.exists() and not forbidden_env.exists()


def test_rebuild_init_env_source_has_no_checkout_secret_destination():
    source = (ROOT / "deploy" / "wealthfolio-rebuild" / "init-env.ps1").read_text()
    assert "Join-Path $PSScriptRoot '.env'" not in source
    assert "wealthfolio-rebuild.env" in source
    assert "--env-file" in source
    assert "FileMode]::CreateNew" in source
