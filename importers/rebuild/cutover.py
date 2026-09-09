"""Verified backup, restore-copy, and runtime primitives for bounded promotion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from importers.analytics.publication import fsync_directory

from .decisions import DecisionError
from .projector import semantic_hash
from .safety import validate_private_output


SQLITE_HEADER = b"SQLite format 3\x00"
IDENTITY_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
REQUIRED_WEALTHFOLIO_TABLES = frozenset(
    {
        "__diesel_schema_migrations",
        "accounts",
        "activities",
        "assets",
        "app_settings",
    }
)


class CutoverError(RuntimeError):
    """Backup, restore, or runtime evidence is missing, changed, or unsafe."""


class DockerContainerRuntime:
    """Control exactly one allowlisted Docker Compose container by immutable ID."""

    def __init__(
        self,
        *,
        project: str,
        service: str,
        container: str,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        for label, value in {
            "project": project,
            "service": service,
            "container": container,
        }.items():
            if not isinstance(value, str) or not IDENTITY_COMPONENT.fullmatch(value):
                raise CutoverError(f"invalid Docker {label} identity")
        self.project = project
        self.service = service
        self.container = container
        self._runner = runner

    def _run(self, *arguments: str) -> str:
        try:
            result = self._runner(
                ["docker", *arguments],
                check=True,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CutoverError("Docker container operation failed") from exc
        return result.stdout.strip()

    def identity(self) -> dict[str, Any]:
        output = self._run("inspect", self.container)
        try:
            rows = json.loads(output)
        except json.JSONDecodeError as exc:
            raise CutoverError("Docker inspect returned invalid JSON") from exc
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise CutoverError("Docker inspect did not identify exactly one container")
        row = rows[0]
        labels = (row.get("Config") or {}).get("Labels") or {}
        name = str(row.get("Name") or "").removeprefix("/")
        if (
            name != self.container
            or labels.get("com.docker.compose.project") != self.project
            or labels.get("com.docker.compose.service") != self.service
        ):
            raise CutoverError("Docker container is outside the configured project allowlist")
        container_id = str(row.get("Id") or "")
        image = str(row.get("Image") or "")
        if len(container_id) < 12 or not image:
            raise CutoverError("Docker container identity is incomplete")
        return {
            "containerId": container_id,
            "containerName": name,
            "composeProject": self.project,
            "composeService": self.service,
            "imageId": image,
            "running": bool((row.get("State") or {}).get("Running")),
        }

    def _require_same(self, expected: Mapping[str, Any]) -> dict[str, Any]:
        actual = self.identity()
        for key in (
            "containerId",
            "containerName",
            "composeProject",
            "composeService",
            "imageId",
        ):
            if actual.get(key) != expected.get(key):
                raise CutoverError("Docker container identity changed during cutover")
        return actual

    def stop(self, expected: Mapping[str, Any]) -> None:
        actual = self._require_same(expected)
        if actual["running"]:
            self._run("stop", "--time", "60", str(actual["containerId"]))

    def start(self, expected: Mapping[str, Any]) -> None:
        actual = self._require_same(expected)
        if not actual["running"]:
            self._run("start", str(actual["containerId"]))

    def is_running(self, expected: Mapping[str, Any]) -> bool:
        return bool(self._require_same(expected)["running"])


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_file(path: Path, root: Path, repo_root: Path) -> tuple[Path, str]:
    try:
        resolved = validate_private_output(path, root, repo_root)
    except DecisionError as exc:
        raise CutoverError("backup must be inside the private data directory") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise CutoverError("backup must be a regular, non-symlink file")
    try:
        relative = resolved.relative_to(root.resolve())
    except ValueError:
        raise CutoverError("backup must remain inside the private data directory") from None
    return resolved, relative.as_posix()


def verify_backup_file(
    path: str | Path,
    *,
    data_dir: str | Path,
    repo_root: str | Path,
) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    resolved, relative = _relative_file(
        Path(path), root, Path(repo_root).resolve()
    )
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= len(SQLITE_HEADER):
        raise CutoverError("backup file is empty or not regular")
    with resolved.open("rb") as source:
        if source.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            raise CutoverError("backup is not a Wealthfolio SQLite database")
    connection = None
    try:
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro&immutable=1", uri=True
        )
        with connection:
            connection.execute("PRAGMA query_only = ON")
            integrity = [
                str(row[0])
                for row in connection.execute("PRAGMA integrity_check")
            ]
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            migration = connection.execute(
                """
                SELECT version
                FROM __diesel_schema_migrations
                ORDER BY run_on DESC
                LIMIT 1
                """
            ).fetchone()
    except sqlite3.Error as exc:
        raise CutoverError("backup SQLite integrity verification failed") from exc
    finally:
        if connection is not None:
            connection.close()
    if integrity != ["ok"] or not REQUIRED_WEALTHFOLIO_TABLES.issubset(tables):
        raise CutoverError("backup Wealthfolio schema or integrity is invalid")
    if migration is None or not str(migration[0]):
        raise CutoverError("backup has no Wealthfolio schema migration")
    return {
        "path": relative,
        "size": metadata.st_size,
        "sha256": _sha256(resolved),
        "schemaMigration": str(migration[0]),
    }


def download_backup(
    client: Any,
    filename: str,
    destination: str | Path,
    *,
    data_dir: str | Path,
    repo_root: str | Path,
) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    try:
        target = validate_private_output(
            Path(destination), root, Path(repo_root).resolve()
        )
    except DecisionError as exc:
        raise CutoverError(
            "backup download must be inside the private data directory"
        ) from exc
    if target.exists():
        raise CutoverError("backup download destination already exists")
    content = client.download_backup(filename)
    if (
        not isinstance(content, bytes)
        or len(content) <= len(SQLITE_HEADER)
        or not content.startswith(SQLITE_HEADER)
    ):
        raise CutoverError("downloaded backup is not a SQLite database")
    inventory = client.list_backups()
    matching = [
        row
        for row in inventory
        if isinstance(row, dict) and row.get("filename") == filename
    ] if isinstance(inventory, list) else []
    if len(matching) != 1:
        raise CutoverError("downloaded backup is absent from the backup inventory")
    expected_size = matching[0].get("sizeBytes")
    if expected_size is not None and expected_size != len(content):
        raise CutoverError("downloaded backup size differs from the inventory")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        fsync_directory(target.parent)
        target.chmod(stat.S_IREAD)
    finally:
        temporary.unlink(missing_ok=True)
    return verify_backup_file(target, data_dir=root, repo_root=repo_root)


def stage_restore_copy(
    backup: str | Path,
    destination: str | Path,
    *,
    data_dir: str | Path,
    repo_root: str | Path,
) -> dict[str, Any]:
    """Copy sealed evidence to a writable restore slot without changing the backup."""
    root = Path(data_dir).resolve()
    source = verify_backup_file(
        backup, data_dir=root, repo_root=repo_root
    )
    source_path = root / source["path"]
    try:
        target = validate_private_output(
            Path(destination), root, Path(repo_root).resolve()
        )
    except DecisionError as exc:
        raise CutoverError("restore slot must be inside the private data directory") from exc
    if target.exists():
        raise CutoverError("restore slot already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}-{uuid.uuid4().hex}.tmp"
    try:
        with source_path.open("rb") as input_stream, temporary.open("xb") as output:
            for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(stat.S_IREAD | stat.S_IWRITE)
        os.replace(temporary, target)
        fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)
    if _sha256(target) != source["sha256"]:
        raise CutoverError("staged restore copy does not match the sealed backup")
    body = {
        "schemaVersion": 1,
        "kind": "wealthfolio-stage-restore-copy",
        "sourcePath": source["path"],
        "stagedPath": target.relative_to(root).as_posix(),
        "sourceSha256": source["sha256"],
        "stagedSha256": _sha256(target),
        "size": target.stat().st_size,
        "schemaMigration": source["schemaMigration"],
    }
    return {**body, "stageHash": semantic_hash(body)}


def verify_stage_restore_report(
    report: object,
    *,
    backup: Mapping[str, Any],
    restored_database: str | Path,
    data_dir: str | Path,
    repo_root: str | Path,
) -> dict[str, Any]:
    """Verify the byte-exact copy receipt captured before the clone started."""

    if not isinstance(report, dict):
        raise CutoverError("stage restore report is missing")
    root = Path(data_dir).resolve()
    try:
        target = validate_private_output(
            Path(restored_database), root, Path(repo_root).resolve()
        )
    except DecisionError as exc:
        raise CutoverError(
            "restore slot must be inside the private data directory"
        ) from exc
    body = {key: value for key, value in report.items() if key != "stageHash"}
    if (
        set(report)
        != {
            "schemaVersion",
            "kind",
            "sourcePath",
            "stagedPath",
            "sourceSha256",
            "stagedSha256",
            "size",
            "schemaMigration",
            "stageHash",
        }
        or report.get("schemaVersion") != 1
        or report.get("kind") != "wealthfolio-stage-restore-copy"
        or report.get("stageHash") != semantic_hash(body)
        or report.get("sourcePath") != backup.get("path")
        or report.get("sourceSha256") != backup.get("sha256")
        or report.get("stagedSha256") != backup.get("sha256")
        or report.get("size") != backup.get("size")
        or report.get("schemaMigration") != backup.get("schemaMigration")
        or report.get("stagedPath") != target.relative_to(root).as_posix()
    ):
        raise CutoverError("stage restore report is changed or mismatched")
    return dict(report)


def _write_cutover_state(
    target: Path,
    document: Mapping[str, Any],
    *,
    create: bool,
) -> None:
    content = _json_bytes(document)
    target.parent.mkdir(parents=True, exist_ok=True)
    if create:
        try:
            with target.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise CutoverError("cutover receipt path already exists") from None
    else:
        temporary = target.parent / f".{target.name}-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    fsync_directory(target.parent)


def _copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    if destination.exists():
        raise CutoverError("cutover staging slot already exists")
    if source.is_symlink() or not source.is_file():
        raise CutoverError("cutover source is not a regular file")
    if source.stat().st_dev != destination.parent.stat().st_dev:
        raise CutoverError("candidate and live database are not on one filesystem")
    with source.open("rb") as input_stream, destination.open("xb") as output:
        for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if _sha256(destination) != expected_sha256:
        raise CutoverError("incoming candidate hash does not match preparation")
