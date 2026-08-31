"""Fail-closed safety checks shared by isolated rebuild mutation tools."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from urllib.parse import urlparse

from .decisions import DecisionError


STAGING_INSTANCE_ENV = "WEALTHFOLIO_REBUILD_STAGING_INSTANCE_ID"


def plan_fingerprint(value: object) -> str:
    if is_dataclass(value):
        value = asdict(value)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def instance_fingerprint(client, base_url: str) -> str:
    """Fingerprint the authenticated app endpoint, including its bound origin."""
    target = urlparse(base_url)
    origin = f"{target.scheme}://{target.hostname}:{target.port}"
    info = client.get("/app/info") or {}
    material = {
        "origin": origin,
        "version": info.get("version"),
        "dbPath": info.get("dbPath"),
    }
    if not material["version"] or not material["dbPath"]:
        raise DecisionError("staging instance did not provide a complete app identity")
    return plan_fingerprint(material)


def validate_apply_target(
    client,
    base_url: str,
    actual_plan_fingerprint: str,
    supplied_plan_fingerprint: str | None,
    expected_instance_id: str | None = None,
) -> None:
    """Require a loopback staging instance and two independent fingerprints."""
    target = urlparse(base_url)
    if target.scheme not in {"http", "https"} or not _is_loopback(target.hostname):
        raise DecisionError("rebuild mutations are restricted to a loopback staging instance")
    if target.port in {None, 8088}:
        raise DecisionError("rebuild mutations are prohibited on production port 8088")
    expected = expected_instance_id or os.environ.get(STAGING_INSTANCE_ENV)
    if not expected:
        raise DecisionError(f"{STAGING_INSTANCE_ENV} is required for rebuild mutations")
    if not supplied_plan_fingerprint:
        raise DecisionError("--plan-fingerprint is required for rebuild mutations")
    if supplied_plan_fingerprint != actual_plan_fingerprint:
        raise DecisionError("operator-supplied plan fingerprint does not match the current plan")
    if instance_fingerprint(client, base_url) != expected:
        raise DecisionError("staging instance identity does not match the expected fingerprint")


def validate_private_output(output: Path, data_dir: Path, repo_root: Path) -> Path:
    resolved = output.resolve()
    private_root = data_dir.resolve()
    checkout = repo_root.resolve()
    if resolved == checkout or checkout in resolved.parents:
        raise DecisionError("private migration output cannot be written inside the repository")
    if resolved != private_root and private_root not in resolved.parents:
        raise DecisionError("private migration output must be under --data-dir")
    return resolved
