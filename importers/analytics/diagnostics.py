"""Read-only diagnostics for Wealthfolio performance and supported capabilities."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable

from importers.monarch.wealthfolio_client import WealthfolioClient, WealthfolioError
from importers.simplefin.spending_adapter import (
    budget_snapshot_problem,
    report_problem,
    rule_list_problem,
    settings_problem,
    taxonomy_categories_problem,
)

from .publication import ensure_durable_directory, fsync_directory

UNKNOWN_FLOW_DATE = re.compile(
    r"TWR unavailable for (\d{4}-\d{2}-\d{2}) because an external flow"
)
IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
CAPABILITY_PUBLICATION = "wealthfolio-capabilities"
PERFORMANCE_PUBLICATION = "wealthfolio-performance"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _publish(
    root: Path,
    result: dict[str, Any],
    publication_name: str = PERFORMANCE_PUBLICATION,
) -> dict[str, str]:
    content = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    output = root / "normalized" / "analytics-diagnostics" / publication_name
    publications = output / "publications"
    ensure_durable_directory(publications, fsync_directory)
    publication = publications / f"{digest}.json"
    if publication.exists():
        if publication.read_bytes() != content:
            raise OSError("diagnostic publication identifier collision")
    else:
        temporary = publications / f".staging-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, publication)
            fsync_directory(publications)
        finally:
            temporary.unlink(missing_ok=True)
    pointer = {
        "schemaVersion": 1,
        "publication": f"publications/{digest}.json",
        "sha256": digest,
    }
    pointer_content = (json.dumps(pointer, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = output / f".current-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as target:
            target.write(pointer_content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, output / "current.json")
        fsync_directory(output)
    finally:
        temporary.unlink(missing_ok=True)
    return pointer


def _probe(
    method: str,
    path: str,
    request: Any,
    *,
    expected_type: type,
    payload: Any = None,
    validate: Callable[[Any], str | None] | None = None,
) -> tuple[dict[str, Any], Any]:
    try:
        response = (
            request.get(path)
            if method == "GET"
            else request.post(path, payload)
        )
    except WealthfolioError as exc:
        status = "unsupported" if exc.status in {404, 405} else "error"
        return (
            {
                "method": method,
                "path": f"/api/v1{path}",
                "status": status,
                "httpStatus": exc.status,
            },
            None,
        )
    if not isinstance(response, expected_type):
        return (
            {
                "method": method,
                "path": f"/api/v1{path}",
                "status": "incompatible",
                "reason": f"response is not {expected_type.__name__}",
            },
            response,
        )
    # A route that answers 200 with a body this integration cannot consume is
    # incompatible, not available. The reason comes from the same structural
    # validators SpendingAdapter enforces, so the two agree by construction and
    # never echo a value.
    problem = validate(response) if validate is not None else None
    if problem:
        return (
            {
                "method": method,
                "path": f"/api/v1{path}",
                "status": "incompatible",
                "reason": problem,
            },
            response,
        )
    return (
        {
            "method": method,
            "path": f"/api/v1{path}",
            "status": "available",
        },
        response,
    )


def _private_data_root(data_dir: str | Path) -> Path:
    root = Path(data_dir).resolve()
    if root == REPO_ROOT or REPO_ROOT in root.parents:
        raise OSError("capability diagnostics cannot be published inside the repository")
    return root


def _taxonomy_summary(payload: Any) -> dict[str, Any]:
    if taxonomy_categories_problem(payload) is not None:
        return {"status": "incompatible"}
    categories = payload["categories"]

    parent_by_id = {
        str(category.get("id")): category.get("parentId")
        for category in categories
        if category.get("id")
    }
    maximum_depth = 0
    for category_id in parent_by_id:
        depth = 1
        seen = {category_id}
        parent = parent_by_id[category_id]
        while parent:
            parent_id = str(parent)
            if parent_id in seen:
                return {"status": "incompatible", "reason": "category-hierarchy-cycle"}
            seen.add(parent_id)
            depth += 1
            parent = parent_by_id.get(parent_id)
        maximum_depth = max(maximum_depth, depth)
    return {
        "status": "available",
        "categoryCount": len(categories),
        "maximumDepth": maximum_depth,
        "atMostTwoLevels": maximum_depth <= 2,
    }


def _deployment_metadata(
    app_info: Any,
    *,
    image_reference: str,
    image_digest: str,
) -> dict[str, str]:
    app_version = str(app_info.get("version") or "") if isinstance(app_info, dict) else ""
    if not app_version:
        raise OSError("Wealthfolio /app/info did not identify the deployed version")
    reference_digest = image_reference.rpartition("@")[2] if "@" in image_reference else ""
    digest = image_digest or reference_digest
    if not image_reference:
        raise OSError("--image-reference is required for a capability diagnostic")
    if not IMAGE_DIGEST.fullmatch(digest):
        raise OSError("a sha256 image digest is required for a capability diagnostic")
    if reference_digest and image_digest and reference_digest != image_digest:
        raise OSError("--image-digest does not match the digest in --image-reference")
    return {
        "appVersion": app_version,
        "imageReference": image_reference,
        "imageDigest": digest,
    }


def diagnose_capabilities(
    data_dir: str | Path,
    *,
    base_url: str = "http://127.0.0.1:8088",
    image_reference: str,
    image_digest: str = "",
) -> dict[str, Any]:
    """Record only read-observable API capabilities; never test a mutation."""
    root = _private_data_root(data_dir)
    password_path = root / "wealthfolio" / "ADMIN-PASSWORD.txt"
    client = WealthfolioClient(base_url)
    client.login(password_path.read_text(encoding="utf-8").strip())

    app_info_probe, app_info = _probe("GET", "/app/info", client, expected_type=dict)
    settings_probe, _ = _probe(
        "GET", "/spending/settings", client, expected_type=dict, validate=settings_problem
    )
    report_probe, _ = _probe(
        "POST",
        "/spending/report",
        client,
        expected_type=dict,
        payload={
            "startDate": "1970-01-01T00:00:00Z",
            "endDate": "1970-01-31T23:59:59Z",
        },
        validate=report_problem,
    )
    rules_probe, _ = _probe(
        "GET", "/spending/rules", client, expected_type=list, validate=rule_list_problem
    )
    budget_probe, _ = _probe(
        "GET", "/spending/budget", client, expected_type=dict, validate=budget_snapshot_problem
    )

    taxonomies: dict[str, dict[str, Any]] = {}
    for taxonomy_id in (
        "spending_categories",
        "income_sources",
        "savings_categories",
    ):
        probe, payload = _probe(
            "GET",
            f"/taxonomies/{taxonomy_id}",
            client,
            expected_type=dict,
            validate=taxonomy_categories_problem,
        )
        summary = (
            _taxonomy_summary(payload)
            if probe["status"] == "available"
            else {"status": probe["status"]}
        )
        taxonomies[taxonomy_id] = {**probe, **summary}

    result = {
        "schemaVersion": 1,
        "private": True,
        "readOnly": True,
        "productionMutation": False,
        "deployment": {
            "baseUrl": base_url,
            **_deployment_metadata(
                app_info,
                image_reference=image_reference,
                image_digest=image_digest,
            ),
        },
        "endpoints": {
            "appInfo": app_info_probe,
            "spendingSettings": settings_probe,
            "spendingReport": report_probe,
            "categorizationRules": rules_probe,
            "budget": budget_probe,
        },
        "taxonomies": taxonomies,
        "operations": {
            "categoryCatalogRead": taxonomies["spending_categories"]["status"],
            "spendingReportRead": report_probe["status"],
            "categorizationRuleRead": rules_probe["status"],
            "budgetRead": budget_probe["status"],
            "categoryAssignmentWrite": "not-probed-read-only",
            "categoryManagementWrite": "not-probed-read-only",
            "categorizationRuleWrite": "not-probed-read-only",
            "budgetWrite": "not-probed-read-only",
        },
        "policy": {
            "unsupportedInterfaceAction": "block-supported-api-integration",
            "databaseFallbackPermitted": False,
        },
    }
    _publish(root, result, CAPABILITY_PUBLICATION)
    return result


def diagnose(
    data_dir: str | Path,
    *,
    base_url: str = "http://127.0.0.1:8088",
    upstream_version: str = "",
    upstream_revision: str = "",
    image_digest: str = "",
) -> dict[str, Any]:
    root = Path(data_dir)
    password_path = root / "wealthfolio" / "ADMIN-PASSWORD.txt"
    client = WealthfolioClient(base_url)
    client.login(password_path.read_text(encoding="utf-8").strip())
    payload = {"itemType": "account", "itemId": "portfolio:all", "filter": {"type": "all"}}
    summary = client.post("/performance/summary", payload)
    history = client.post("/performance/history", payload)
    reasons = summary.get("dataQuality", {}).get("notApplicableReasons", [])
    unknown_dates = sorted(
        {match.group(1) for reason in reasons if (match := UNKNOWN_FLOW_DATE.search(reason))}
    )
    result = {
        "schemaVersion": 1,
        "private": True,
        "readOnly": True,
        "productionMutation": False,
        "deployment": {
            "baseUrl": base_url,
            "upstreamVersion": upstream_version,
            "upstreamRevision": upstream_revision,
            "imageDigest": image_digest,
        },
        "requests": {
            "summary": "/api/v1/performance/summary",
            "history": "/api/v1/performance/history",
            "payload": payload,
        },
        "diagnosis": {
            "mode": summary.get("mode"),
            "twr": summary.get("returns", {}).get("twr"),
            "mwr": summary.get("returns", {}).get("irr"),
            "historySeriesCount": len(history.get("series") or []),
            "unknownExternalFlowDates": unknown_dates,
            "notApplicableReasons": reasons,
            "warnings": summary.get("dataQuality", {}).get("warnings", []),
            "chartSuppressedByFrontendContract": (
                summary.get("returns", {}).get("twr") is None
                and bool(history.get("series"))
            ),
        },
        "fixability": {
            "data": [
                "Resolve every UNKNOWN and UNKNOWN_BOUNDARY_TRANSFER valuation day from source evidence.",
                "Link true internal transfer pairs or explicitly classify genuine external transfers, then recalculate valuations.",
            ],
            "upstream": [
                "Wealthfolio nulls the entire headline TWR and XIRR when any day has unavailable external-flow provenance.",
                "The performance chart rejects a time-weighted series when headline TWR is null, even when the API returned points.",
                "Alternative-asset zero values cannot represent a disposal reliably in Wealthfolio history; canonical ownership windows remain external.",
            ],
        },
    }
    _publish(root, result)
    return result
