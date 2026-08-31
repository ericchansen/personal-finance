"""Read-only diagnosis of Wealthfolio's aggregate performance API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from importers.monarch.wealthfolio_client import WealthfolioClient

from .publication import ensure_durable_directory, fsync_directory

UNKNOWN_FLOW_DATE = re.compile(r"TWR unavailable for (\d{4}-\d{2}-\d{2}) because an external flow")


def _publish(root: Path, result: dict[str, Any]) -> dict[str, str]:
    content = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    output = root / "normalized" / "analytics-diagnostics" / "wealthfolio-performance"
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
