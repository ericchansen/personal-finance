"""Scoped routine entrypoint; never collects, installs a task, or activates ownership."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import sys
import uuid

from . import incremental_release as release

EVIDENCE_ENV = "WEALTHFOLIO_PROMOTION_EVIDENCE_KEY"
SCOPE_KEYS = {"path", "scopeId", "configurationHash", "dsnFile", "passwordFile", "evidenceKeyFile"}
PRIVATE_ENV_PREFIXES = ("FINANCE_", "WEALTHFOLIO_", "WF_", "SIMPLEFIN_", "PG", "PYTHON")
PROXY_ENV_NAMES = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}


def _controlled_environment(name):
    return name.upper().startswith(PRIVATE_ENV_PREFIXES) or name.upper() in PROXY_ENV_NAMES


@contextmanager
def isolated_environment():
    saved = {name: value for name, value in os.environ.items() if _controlled_environment(name)}
    try:
        for name in tuple(os.environ):
            if _controlled_environment(name):
                del os.environ[name]
        os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
        yield
    finally:
        for name in tuple(os.environ):
            if _controlled_environment(name):
                del os.environ[name]
        os.environ.update(saved)


def _inside(root, name):
    release.require(isinstance(name, str) and name, "incremental-launch-file-required")
    path = Path(name)
    release.require(not path.is_absolute() and not path.drive and ".." not in path.parts,
                    "incremental-launch-file-must-be-relative")
    path = release.regular_path(root / path).resolve()
    release.require(path.is_relative_to(root) and path.is_file(), "incremental-launch-file-unavailable")
    return path


def load_configuration(path, expected_hash, release_root):
    path = release.regular_path(path).resolve()
    release.require(release.is_hash(expected_hash) and release.digest(path) == expected_hash,
                    "incremental-launch-configuration-drift")
    document = release.read_document(path)
    release.require(set(document) == {"schemaVersion", "kind", "dataRoot", "scopes"}
                    and type(document["schemaVersion"]) is int and document["schemaVersion"] == 1
                    and document["kind"] == "incremental-launch-configuration",
                    "incremental-launch-configuration-invalid")
    root = Path(document["dataRoot"])
    release.require(root.is_absolute(), "incremental-launch-data-root-required")
    root = release.regular_path(root).resolve()
    release.require(path.is_relative_to(root) and not root.is_relative_to(release_root)
                    and not release_root.is_relative_to(root), "incremental-launch-data-root-not-separate")
    scopes = document["scopes"]
    release.require(isinstance(scopes, list) and 0 < len(scopes) <= 100,
                    "incremental-launch-scopes-required")
    seen = set()
    for scope in scopes:
        release.require(isinstance(scope, dict) and set(scope) == SCOPE_KEYS,
                        "incremental-launch-scope-invalid")
        identifier = scope["scopeId"]
        release.require(str(uuid.UUID(identifier)) == identifier and identifier not in seen
                        and release.is_hash(scope["configurationHash"]),
                        "incremental-launch-scope-binding-invalid")
        seen.add(identifier)
    return root, scopes


def _secret(root, relative, *, hex_key=False):
    value = _inside(root, relative).read_text(encoding="utf-8-sig").strip()
    release.require(value and "\x00" not in value and "\r" not in value and "\n" not in value,
                    "incremental-launch-secret-invalid")
    if hex_key:
        release.require(len(value) >= 64 and len(value) % 2 == 0
                        and all(c in "0123456789abcdefABCDEF" for c in value),
                        "incremental-launch-evidence-key-invalid")
    return value


def _safe_worker_output(raw, scope_id):
    lines = raw.strip().splitlines()
    release.require(len(lines) == 1, "incremental-launch-worker-output-invalid")
    value = json.loads(lines[0])
    release.require(isinstance(value, dict), "incremental-launch-worker-output-invalid")
    result = {"scopeId": scope_id}
    for key in ("state", "reason"):
        text = value.get(key)
        if text is not None:
            result[key] = text if isinstance(text, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,159}", text) else "detail-withheld"
    release.require("state" in result, "incremental-launch-worker-state-missing")
    for key in ("runHash", "run_hash", "sourceCheckpointHash"):
        if release.is_hash(value.get(key)):
            result[key] = value[key]
    for key in ("operationCount", "heldCount", "historicalBackfillCount", "cashTransitionCount",
                "proposedOperationCount", "journaledOperationCount", "appliedOperationCount",
                "newHistoricalReviewCount", "frontierReviewCount"):
        if type(value.get(key)) is int and value[key] >= 0:
            result[key] = value[key]
    for key in ("replay", "eligibleBatchOnly", "plannedBalanceMatchesSource"):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    return result


@contextmanager
def evidence_key_environment(root, scope, filename):
    from .incremental_inputs import bound_file, document
    key_name = EVIDENCE_ENV
    contract = scope.config.get("bootstrapSourceAnchor") or scope.config.get("bootstrap")
    if contract:
        key_name = document(bound_file(root, contract))["repairHistory"].get("verificationKeyEnv", EVIDENCE_ENV)
    release.require(isinstance(key_name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*_KEY", key_name)
                    and not key_name.startswith(("GIT_", "PG", "PYTHON"))
                    and key_name != "WEALTHFOLIO_PROMOTION_OPERATOR_KEY",
                    "incremental-launch-evidence-key-environment-invalid")
    old = os.environ.pop(key_name, None)
    try:
        os.environ[key_name] = _secret(root, filename, hex_key=True)
        yield
    finally:
        os.environ.pop(key_name, None)
        if old is not None:
            os.environ[key_name] = old


def _scope_run(root, specification, command, enable_mutations):
    from importers.monarch import mutation_guard as guard
    from .incremental_inputs import load_scope
    with isolated_environment():
        scope_path = _inside(root, specification["path"])
        scope = load_scope(root, scope_path)
        release.require(scope.scope_id == specification["scopeId"]
                        and scope.config_hash == specification["configurationHash"],
                        "incremental-launch-scope-configuration-drift")
        if command == "run":
            release.require(enable_mutations, "incremental-launch-explicit-mutation-opt-in-required")
            os.environ[guard.MUTATION_INTERLOCK_ENV] = guard.MUTATION_INTERLOCK_VALUE
            os.environ[guard.WRITER_MODE_ENV] = guard.INCREMENTAL_WRITER_MODE_VALUE
            os.environ[guard.WRITER_ENVIRONMENT_ENV] = scope.config["writerEnvironmentId"]
            os.environ[guard.WRITER_OWNERSHIP_MARKER_ENV] = str(root / guard.WRITER_MARKER_RELATIVE)
            # Check only this configured scope. The worker independently checks
            # the authenticated instance, current release and gate before writes.
            guard.require_incremental_writer(
                base_url=scope.config["origin"], data_dir=root, scope_id=scope.scope_id,
                configuration_hash=scope.config_hash, instance_id=scope.config["instanceId"],
                environment_id=scope.config["writerEnvironmentId"],
            )
        os.environ["FINANCE_INCREMENTAL_DSN"] = _secret(root, specification["dsnFile"])
        from psycopg.conninfo import conninfo_to_dict
        dsn = conninfo_to_dict(os.environ["FINANCE_INCREMENTAL_DSN"])
        release.require(all(dsn.get(key) for key in ("host", "dbname", "user", "password"))
                        and not {"service", "passfile"} & dsn.keys(),
                        "incremental-launch-explicit-dsn-credentials-required")
        if command != "status":
            os.environ["WEALTHFOLIO_PASSWORD"] = _secret(root, specification["passwordFile"])
        from .incremental_cli import main as worker_main
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            arguments = [command, "--data-dir", str(root), "--scope", str(scope_path)]
            if command == "status":
                exit_code = worker_main(arguments)
            else:
                with evidence_key_environment(root, scope, specification["evidenceKeyFile"]):
                    exit_code = worker_main(arguments)
        result = _safe_worker_output(stdout.getvalue(), scope.scope_id)
        if exit_code and result["state"] not in {"held", "uncertain"}:
            result = {"scopeId": scope.scope_id, "state": "held", "reason": "incremental-launch-worker-failed"}
        return result


def launch(release_root, configuration, configuration_sha256, *, command="status", enable_mutations=False):
    release_root = release.regular_path(release_root).resolve()
    archive, pointer = release.verify_pointer(release_root)
    release.require(archive == Path(__file__).resolve().parents[1],
                    "incremental-launch-executing-other-release")
    release.require(command in {"run", "plan", "status"}
                    and (command == "run" or not enable_mutations), "incremental-launch-mode-invalid")
    root, scopes = load_configuration(configuration, configuration_sha256, release_root)
    started = datetime.now(timezone.utc).isoformat()
    results = []
    for scope in scopes:
        try:
            current_archive, current_pointer = release.verify_pointer(release_root)
            release.require(current_archive == archive and current_pointer == pointer,
                            "incremental-launch-release-changed")
            release.require(release.digest(configuration) == configuration_sha256,
                            "incremental-launch-configuration-drift")
            result = _scope_run(root, scope, command, enable_mutations)
        except Exception as error:
            result = {"scopeId": scope["scopeId"], "state": "held",
                      "reason": str(error) if isinstance(error, release.ReleaseError) else type(error).__name__}
        results.append(result)
        print(json.dumps(result, sort_keys=True))
    body = {
        "schemaVersion": 1, "kind": "incremental-launch-result", "runId": str(uuid.uuid4()),
        "command": command, "release": {"commit": pointer["commit"], "manifestSha256": pointer["manifestSha256"]},
        "configurationSha256": configuration_sha256, "startedAt": started,
        "completedAt": datetime.now(timezone.utc).isoformat(), "scopes": results,
        "state": "held" if any(item["state"] in {"held", "uncertain"} for item in results) else "completed",
    }
    status_root = release.regular_path(root / "automation" / "incremental-launcher")
    status_root.mkdir(parents=True, exist_ok=True)
    runs = status_root / "runs"
    release.regular_path(runs).mkdir(exist_ok=True)
    with (runs / f"{body['runId']}.json").open("xb") as output:
        output.write(release.encoded(body))
    release._write_atomic(status_root / "current.json", release.encoded(body))
    return int(body["state"] == "held")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--configuration-sha256", required=True)
    parser.add_argument("--command", choices=("run", "plan", "status"), default="status")
    parser.add_argument("--enable-mutations", action="store_true")
    args = parser.parse_args(argv)
    try:
        return launch(args.release_root, args.configuration, args.configuration_sha256,
                      command=args.command, enable_mutations=args.enable_mutations)
    except Exception as error:
        print(json.dumps({"state": "held", "reason":
                         str(error) if isinstance(error, release.ReleaseError)
                         else "incremental-launch-failed"}, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
