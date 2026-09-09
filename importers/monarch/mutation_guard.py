"""Global and canonical-writer fail-closed interlocks for Wealthfolio."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import stat
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


MUTATION_INTERLOCK_ENV = "WEALTHFOLIO_MUTATIONS_ENABLED"
MUTATION_INTERLOCK_VALUE = "true"
WRITER_OWNERSHIP_MARKER_ENV = "WEALTHFOLIO_WRITER_OWNERSHIP_MARKER"
WRITER_MODE_ENV = "WEALTHFOLIO_WRITER_MODE"
WRITER_MODE_VALUE = "clean-canonical-projector-v1"
INCREMENTAL_WRITER_MODE_VALUE = "incremental-cash-projector-v1"
WRITER_ENVIRONMENT_ENV = "WEALTHFOLIO_WRITER_ENVIRONMENT_ID"
WRITER_MARKER_RELATIVE = Path("wealthfolio-rebuild") / "writer-ownership.json"
WRITER_MARKER_KEYS = frozenset(
    {
        "schemaVersion",
        "mode",
        "writerModeToken",
        "environmentId",
        "origin",
        "preparationId",
        "executionId",
        "bundleId",
        "planHash",
        "activatedAt",
        "target",
        "markerHash",
    }
)
INCREMENTAL_MARKER_KEYS = frozenset({
    "schemaVersion", "mode", "writerModeToken", "environmentId", "origin",
    "instanceId", "release", "scopes", "activatedAt", "markerHash",
})
_INCREMENTAL_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "wealthfolio_incremental_writer", default=None
)

_READ_ONLY_POST_PATHS = frozenset(
    {
        "/activities/search",
        "/activities/transfer-match-candidates",
        "/auth/login",
        "/health/check",
        "/income/summary/query",
        "/performance/accounts/simple",
        "/performance/history",
        "/performance/summary",
        "/performance/summaries",
        "/spending/cash-activities/search",
        "/spending/event-spending-summaries",
        "/spending/insight",
        "/spending/report",
    }
)
_BACKUP_POST_PATH = "/utilities/database/backup"


class MutationInterlockError(RuntimeError):
    """Raised before a Wealthfolio mutation when the global interlock is closed."""


def writer_marker_hash(document: Mapping[str, Any]) -> str:
    body = {key: value for key, value in document.items() if key != "markerHash"}
    encoded = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest(value: object, lengths=(64,)) -> bool:
    return isinstance(value, str) and len(value) in lengths and all(c in "0123456789abcdef" for c in value)


def _loopback_origin(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
        return (loopback and parsed.scheme in {"http", "https"} and parsed.port is not None
                and not parsed.username and not parsed.password and not parsed.path
                and not parsed.query and not parsed.fragment)
    except (ValueError, TypeError):
        return False


def current_incremental_release() -> dict[str, str]:
    """Bind live Git sources or every file of a strict installed archive."""
    from finance_store.incremental_release import current_release
    try:
        return current_release(Path(__file__).resolve().parents[2])
    except Exception as exc:
        raise MutationInterlockError("incremental current release is unavailable") from exc


def _validate_incremental_marker(document: dict) -> dict[str, Any]:
    if (
        set(document) != INCREMENTAL_MARKER_KEYS
        or type(document.get("schemaVersion")) is not int
        or document.get("mode") != "incremental-projector-only"
        or document.get("writerModeToken") != INCREMENTAL_WRITER_MODE_VALUE
        or not _digest(document.get("environmentId"))
        or not _digest(document.get("instanceId"))
        or not _loopback_origin(document.get("origin"))
        or document.get("markerHash") != writer_marker_hash(document)
    ):
        raise MutationInterlockError("incremental writer ownership marker is invalid")
    release = document.get("release")
    if (not isinstance(release, dict) or set(release) != {"commit", "codeHash"}
            or not _digest(release.get("commit"), (40, 64)) or not _digest(release.get("codeHash"))):
        raise MutationInterlockError("incremental writer release binding is invalid")
    try:
        activated = datetime.fromisoformat(document["activatedAt"].replace("Z", "+00:00"))
        if activated.tzinfo is None or activated.utcoffset() is None:
            raise ValueError("timestamp has no zone")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise MutationInterlockError("incremental writer activation timestamp is invalid") from exc
    scopes = document.get("scopes")
    if not isinstance(scopes, list) or not scopes:
        raise MutationInterlockError("incremental writer scopes are required")
    seen = set()
    for scope in scopes:
        if (not isinstance(scope, dict) or set(scope) != {"scopeId", "configurationHash"}
                or not _digest(scope.get("configurationHash"))):
            raise MutationInterlockError("incremental writer scope binding is invalid")
        try:
            scope_id = str(uuid.UUID(scope["scopeId"]))
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise MutationInterlockError("incremental writer scope id is invalid") from exc
        if scope_id != scope["scopeId"] or scope_id in seen:
            raise MutationInterlockError("incremental writer scope ids must be unique and canonical")
        seen.add(scope_id)
    return document


def validate_writer_ownership_marker(
    document: object,
) -> dict[str, Any]:
    if isinstance(document, dict) and document.get("schemaVersion") == 2:
        return _validate_incremental_marker(document)
    if (
        not isinstance(document, dict)
        or set(document) != WRITER_MARKER_KEYS
        or document.get("schemaVersion") != 1
        or document.get("mode") != "canonical-projector-only"
        or document.get("writerModeToken") != WRITER_MODE_VALUE
        or not isinstance(document.get("environmentId"), str)
        or len(document["environmentId"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in document["environmentId"]
        )
        or document.get("origin") != "http://127.0.0.1:8088"
        or not isinstance(document.get("target"), dict)
        or set(document["target"])
        != {
            "composeProject",
            "composeService",
            "containerName",
            "liveDatabase",
        }
        or any(
            not isinstance(value, str) or not value
            for value in document["target"].values()
        )
        or not isinstance(document.get("activatedAt"), str)
        or not document["activatedAt"]
        or document.get("markerHash") != writer_marker_hash(document)
    ):
        raise MutationInterlockError(
            "canonical writer ownership marker is invalid"
        )
    for key in ("preparationId", "executionId", "bundleId", "planHash"):
        value = document.get(key)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise MutationInterlockError(
                "canonical writer ownership marker binding is invalid"
            )
    return document


def _marker_candidates(
    environment: Mapping[str, str],
    *,
    data_dir: str | Path | None = None,
) -> tuple[list[Path], bool]:
    explicit = environment.get(WRITER_OWNERSHIP_MARKER_ENV)
    if data_dir is not None:
        root = Path(data_dir).expanduser().absolute()
        expected = root / WRITER_MARKER_RELATIVE
        if not expected.resolve().is_relative_to(root.resolve()):
            raise MutationInterlockError("canonical writer marker path escapes writer data root")
        if explicit:
            configured = Path(explicit).expanduser()
            if (
                not configured.is_absolute()
                or configured.resolve() != expected.resolve()
            ):
                raise MutationInterlockError(
                    "canonical writer marker path conflicts with writer data root"
                )
        return [expected], bool(explicit)
    if explicit:
        marker = Path(explicit).expanduser()
        if not marker.is_absolute():
            raise MutationInterlockError(
                "canonical writer ownership marker path must be absolute"
            )
        return [marker], True
    candidates: list[Path] = []
    for variable in ("FINANCE_DATA", "WEALTHFOLIO_DATA"):
        value = environment.get(variable)
        if value:
            candidates.append(Path(value).expanduser() / WRITER_MARKER_RELATIVE)
    wealthfolio = environment.get("WF_DATA_DIR")
    if wealthfolio:
        root = Path(wealthfolio).expanduser()
        candidates.extend(
            (
                root / WRITER_MARKER_RELATIVE,
                root.parent / WRITER_MARKER_RELATIVE,
            )
        )
    unique: list[Path] = []
    resolved_seen: set[Path] = set()
    for candidate in candidates:
        absolute = candidate.absolute()
        resolved = absolute.resolve()
        if resolved not in resolved_seen:
            unique.append(absolute)
            resolved_seen.add(resolved)
    return unique, False


def active_writer_ownership_marker(
    environment: Mapping[str, str] | None = None,
    *,
    data_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    values = os.environ if environment is None else environment
    candidates, explicit = _marker_candidates(values, data_dir=data_dir)
    existing = [path for path in candidates if path.exists()]
    if explicit and not existing:
        raise MutationInterlockError(
            "configured canonical writer ownership marker is unavailable"
        )
    if len(existing) > 1:
        raise MutationInterlockError(
            "multiple canonical writer ownership markers are visible"
        )
    if not existing:
        return None
    marker = existing[0]
    try:
        metadata = marker.lstat()
        if (
            marker.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
        ):
            raise MutationInterlockError(
                "canonical writer ownership marker is not a regular file"
            )
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MutationInterlockError(
            "canonical writer ownership marker cannot be read"
        ) from exc
    validated = validate_writer_ownership_marker(document)
    return {**validated, "markerPath": str(marker.resolve())}


def require_canonical_writer(
    *,
    base_url: str | None = None,
    environment: Mapping[str, str] | None = None,
    data_dir: str | Path | None = None,
) -> None:
    """Require canonical writer ownership only after its marker is activated."""
    values = os.environ if environment is None else environment
    marker = active_writer_ownership_marker(values, data_dir=data_dir)
    if marker is None:
        if _INCREMENTAL_CONTEXT.get() is not None:
            raise MutationInterlockError("incremental writer ownership marker is unavailable")
        return
    if marker["schemaVersion"] == 2:
        context = _INCREMENTAL_CONTEXT.get()
        if context is None:
            raise MutationInterlockError("incremental writer requires an explicit scoped request context")
        _require_incremental_binding(marker, values, context, base_url)
        return
    if _INCREMENTAL_CONTEXT.get() is not None:
        raise MutationInterlockError("incremental writer marker contract changed")
    if values.get(WRITER_MODE_ENV) != WRITER_MODE_VALUE:
        raise MutationInterlockError(
            f"canonical writer ownership requires {WRITER_MODE_ENV} exactly "
            f"{WRITER_MODE_VALUE!r}"
        )
    if values.get(WRITER_ENVIRONMENT_ENV) != marker["environmentId"]:
        raise MutationInterlockError(
            f"canonical writer ownership requires exact "
            f"{WRITER_ENVIRONMENT_ENV} binding"
        )
    if base_url is not None:
        parsed = urlsplit(base_url)
        origin = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        if origin != marker["origin"]:
            raise MutationInterlockError(
                "canonical writer ownership denied a different target origin"
            )


def _require_incremental_binding(marker, values, context, base_url):
    if values.get(WRITER_MODE_ENV) != INCREMENTAL_WRITER_MODE_VALUE:
        raise MutationInterlockError(f"incremental writer requires {WRITER_MODE_ENV} exactly {INCREMENTAL_WRITER_MODE_VALUE!r}")
    if (values.get(WRITER_ENVIRONMENT_ENV) != marker["environmentId"]
            or context["environmentId"] != marker["environmentId"]):
        raise MutationInterlockError("incremental writer environment binding differs")
    if base_url != marker["origin"] or context["origin"] != marker["origin"]:
        raise MutationInterlockError("incremental writer target origin differs")
    if context["instanceId"] != marker["instanceId"]:
        raise MutationInterlockError("incremental writer actual instance binding differs")
    if context["markerPath"] != marker["markerPath"]:
        raise MutationInterlockError("incremental writer data-root marker differs")
    if {"scopeId": context["scopeId"], "configurationHash": context["configurationHash"]} not in marker["scopes"]:
        raise MutationInterlockError("incremental writer scope/config binding differs")
    if marker["release"] != current_incremental_release():
        raise MutationInterlockError("incremental writer current release binding differs")


def require_incremental_writer(
    *, base_url: str, data_dir: str | Path, scope_id: str, configuration_hash: str,
    instance_id: str, environment_id: str, environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate an operator-installed marker; never creates or changes one."""
    values = os.environ if environment is None else environment
    marker = active_writer_ownership_marker(values, data_dir=data_dir)
    if marker is None or marker["schemaVersion"] != 2:
        raise MutationInterlockError("incremental writer requires schema-2 scoped ownership")
    context = {
        "scopeId": scope_id, "configurationHash": configuration_hash,
        "instanceId": instance_id, "environmentId": environment_id,
        "origin": base_url, "markerPath": marker["markerPath"],
    }
    _require_incremental_binding(marker, values, context, base_url)
    return context


@contextmanager
def incremental_writer_context(**bindings):
    """Scope only the actual HTTP side effect, not unrelated caller requests."""
    context = require_incremental_writer(**bindings)
    token = _INCREMENTAL_CONTEXT.set(context)
    try:
        yield
    finally:
        _INCREMENTAL_CONTEXT.reset(token)


def is_wealthfolio_mutation(method: str, path: str) -> bool:
    """Classify requests conservatively; unknown non-GET requests are mutations."""
    verb = method.upper()
    route = urlsplit(path).path
    if verb == "GET":
        return False
    if verb == "POST" and (
        route in _READ_ONLY_POST_PATHS or route == _BACKUP_POST_PATH
    ):
        return False
    return True


def require_wealthfolio_mutations(
    method: str,
    path: str,
    *,
    base_url: str | None = None,
    data_dir: str | Path | None = None,
) -> None:
    """Require the one exact opt-in value before a mutating request is sent."""
    if not is_wealthfolio_mutation(method, path):
        return
    operation = f"{method.upper()} {urlsplit(path).path}"
    if os.environ.get(MUTATION_INTERLOCK_ENV) != MUTATION_INTERLOCK_VALUE:
        raise MutationInterlockError(
            f"Wealthfolio mutation interlock denied {operation}; set "
            f"{MUTATION_INTERLOCK_ENV} exactly to {MUTATION_INTERLOCK_VALUE!r} "
            "only for a reviewed mutation run"
        )
    require_canonical_writer(base_url=base_url, data_dir=data_dir)
