from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from importers.monarch.mutation_guard import (
    MUTATION_INTERLOCK_ENV,
    WRITER_ENVIRONMENT_ENV,
    WRITER_MODE_ENV,
    WRITER_MODE_VALUE,
    WRITER_OWNERSHIP_MARKER_ENV,
    MutationInterlockError,
    require_wealthfolio_mutations,
    writer_marker_hash,
)
from importers.monarch.wealthfolio_client import WealthfolioClient


REPO_ROOT = Path(__file__).resolve().parents[1]


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps({"ok": True}).encode()


class _RecordingOpener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return _Response()


def _activate_writer_marker(path):
    body = {
        "schemaVersion": 1,
        "mode": "canonical-projector-only",
        "writerModeToken": WRITER_MODE_VALUE,
        "environmentId": "e" * 64,
        "origin": "http://127.0.0.1:8088",
        "preparationId": "a" * 64,
        "executionId": "b" * 64,
        "bundleId": "c" * 64,
        "planHash": "d" * 64,
        "activatedAt": "2026-09-04T00:00:00+00:00",
        "target": {
            "composeProject": "synthetic",
            "composeService": "wealthfolio",
            "containerName": "wealthfolio",
            "liveDatabase": "wealthfolio/wealthfolio.db",
        },
    }
    marker = {**body, "markerHash": writer_marker_hash(body)}
    path.write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return marker


def test_interlock_denies_by_default_and_names_operation(monkeypatch):
    monkeypatch.delenv(MUTATION_INTERLOCK_ENV, raising=False)

    with pytest.raises(MutationInterlockError) as denied:
        require_wealthfolio_mutations("POST", "/activities/bulk")

    assert "POST /activities/bulk" in str(denied.value)
    assert MUTATION_INTERLOCK_ENV in str(denied.value)


def test_interlock_accepts_only_exact_opt_in(monkeypatch):
    monkeypatch.setenv(MUTATION_INTERLOCK_ENV, "true")
    require_wealthfolio_mutations("DELETE", "/activities/example")

    for invalid in ("1", "yes", "TRUE", " true", "true "):
        monkeypatch.setenv(MUTATION_INTERLOCK_ENV, invalid)
        with pytest.raises(MutationInterlockError):
            require_wealthfolio_mutations("DELETE", "/activities/example")


def test_writer_ownership_is_backward_compatible_before_marker(monkeypatch, tmp_path):
    monkeypatch.setenv(MUTATION_INTERLOCK_ENV, "true")
    monkeypatch.delenv(WRITER_OWNERSHIP_MARKER_ENV, raising=False)
    monkeypatch.setenv("FINANCE_DATA", str(tmp_path))

    require_wealthfolio_mutations(
        "DELETE",
        "/activities/example",
        base_url="http://127.0.0.1:8088",
    )


def test_retirement_marker_denies_legacy_writer_despite_global_opt_in(
    monkeypatch, tmp_path
):
    marker_path = tmp_path / "writer-ownership.json"
    _activate_writer_marker(marker_path)
    monkeypatch.setenv(MUTATION_INTERLOCK_ENV, "true")
    monkeypatch.setenv(WRITER_OWNERSHIP_MARKER_ENV, str(marker_path))
    monkeypatch.delenv(WRITER_MODE_ENV, raising=False)
    monkeypatch.delenv(WRITER_ENVIRONMENT_ENV, raising=False)

    with pytest.raises(MutationInterlockError, match=WRITER_MODE_ENV):
        require_wealthfolio_mutations(
            "POST",
            "/activities/bulk",
            base_url="http://127.0.0.1:8088",
        )
    client = WealthfolioClient("http://127.0.0.1:8088")
    opener = _RecordingOpener()
    client._opener = opener
    with pytest.raises(MutationInterlockError, match=WRITER_MODE_ENV):
        client.post("/activities/bulk", [])
    assert opener.requests == []


def test_client_discovers_retirement_marker_from_data_dir_without_env(
    monkeypatch, tmp_path
):
    marker_path = (
        tmp_path / "wealthfolio-rebuild" / "writer-ownership.json"
    )
    marker_path.parent.mkdir()
    _activate_writer_marker(marker_path)
    monkeypatch.setenv(MUTATION_INTERLOCK_ENV, "true")
    for variable in (
        WRITER_OWNERSHIP_MARKER_ENV,
        "FINANCE_DATA",
        "WEALTHFOLIO_DATA",
        "WF_DATA_DIR",
        WRITER_MODE_ENV,
        WRITER_ENVIRONMENT_ENV,
    ):
        monkeypatch.delenv(variable, raising=False)
    client = WealthfolioClient(
        "http://127.0.0.1:8088", writer_data_dir=tmp_path
    )
    opener = _RecordingOpener()
    client._opener = opener

    with pytest.raises(MutationInterlockError, match=WRITER_MODE_ENV):
        client.post("/activities/bulk", [])

    assert opener.requests == []


def test_data_dir_rejects_conflicting_explicit_marker_path(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(MUTATION_INTERLOCK_ENV, "true")
    monkeypatch.setenv(
        WRITER_OWNERSHIP_MARKER_ENV, str(tmp_path / "other.json")
    )
    with pytest.raises(MutationInterlockError, match="conflicts"):
        require_wealthfolio_mutations(
            "POST",
            "/activities/bulk",
            base_url="http://127.0.0.1:8088",
            data_dir=tmp_path,
        )


def test_data_dir_fails_closed_for_configured_missing_marker(
    monkeypatch, tmp_path
):
    marker_path = (
        tmp_path / "wealthfolio-rebuild" / "writer-ownership.json"
    )
    monkeypatch.setenv(MUTATION_INTERLOCK_ENV, "true")
    monkeypatch.setenv(WRITER_OWNERSHIP_MARKER_ENV, str(marker_path))

    with pytest.raises(MutationInterlockError, match="unavailable"):
        require_wealthfolio_mutations(
            "POST",
            "/activities/bulk",
            base_url="http://127.0.0.1:8088",
            data_dir=tmp_path,
        )


@pytest.mark.parametrize(
    "relative_path",
    (
        "importers/maintenance/gap_cli.py",
        "importers/maintenance/cli.py",
        "importers/maintenance/basis_repair_cli.py",
        "importers/assets/cli.py",
        "importers/categorize/cli.py",
    ),
)
def test_every_remaining_production_mutator_binds_writer_data_root(
    relative_path,
):
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    constructors = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "WealthfolioClient"
    ]

    assert constructors
    assert all(
        any(
            keyword.arg == "writer_data_dir"
            and not (
                isinstance(keyword.value, ast.Constant)
                and keyword.value.value is None
            )
            for keyword in constructor.keywords
        )
        for constructor in constructors
    )


def test_reviewed_read_only_clients_remain_without_writer_data_root():
    for relative_path in (
        "importers/maintenance/status_cli.py",
        "importers/valuations/cli.py",
    ):
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "writer_data_dir" not in source

    valuation_pipeline = (
        REPO_ROOT / "importers/valuations/pipeline.py"
    ).read_text(encoding="utf-8")
    assert ".save_activities(" not in valuation_pipeline
    assert ".backup_database(" not in valuation_pipeline
    assert ".post(" not in valuation_pipeline
    assert ".put(" not in valuation_pipeline
    assert ".delete(" not in valuation_pipeline
    status_source = (
        REPO_ROOT / "importers/maintenance/status_cli.py"
    ).read_text(encoding="utf-8")
    assert '"/performance/accounts/simple"' in status_source
    assert ".save_activities(" not in status_source
    assert ".backup_database(" not in status_source
    assert ".put(" not in status_source
    assert ".delete(" not in status_source


def test_retirement_marker_accepts_only_exact_mode_environment_and_origin(
    monkeypatch, tmp_path
):
    marker_path = tmp_path / "writer-ownership.json"
    _activate_writer_marker(marker_path)
    monkeypatch.setenv(MUTATION_INTERLOCK_ENV, "true")
    monkeypatch.setenv(WRITER_OWNERSHIP_MARKER_ENV, str(marker_path))
    monkeypatch.setenv(WRITER_MODE_ENV, WRITER_MODE_VALUE)
    monkeypatch.setenv(WRITER_ENVIRONMENT_ENV, "e" * 64)

    require_wealthfolio_mutations(
        "POST",
        "/activities/bulk",
        base_url="http://127.0.0.1:8088",
    )

    monkeypatch.setenv(WRITER_ENVIRONMENT_ENV, "f" * 64)
    with pytest.raises(MutationInterlockError, match="binding"):
        require_wealthfolio_mutations(
            "POST",
            "/activities/bulk",
            base_url="http://127.0.0.1:8088",
        )
    monkeypatch.setenv(WRITER_ENVIRONMENT_ENV, "e" * 64)
    with pytest.raises(MutationInterlockError, match="target origin"):
        require_wealthfolio_mutations(
            "POST",
            "/activities/bulk",
            base_url="http://127.0.0.1:18091",
        )


def test_configured_missing_or_tampered_writer_marker_fails_closed(
    monkeypatch, tmp_path
):
    marker_path = tmp_path / "writer-ownership.json"
    monkeypatch.setenv(MUTATION_INTERLOCK_ENV, "true")
    monkeypatch.setenv(WRITER_OWNERSHIP_MARKER_ENV, str(marker_path))
    with pytest.raises(MutationInterlockError, match="unavailable"):
        require_wealthfolio_mutations("DELETE", "/activities/example")

    marker = _activate_writer_marker(marker_path)
    marker["environmentId"] = "f" * 64
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(MutationInterlockError, match="invalid"):
        require_wealthfolio_mutations("DELETE", "/activities/example")


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("POST", "/activities/bulk"),
        ("PUT", "/activities"),
        ("DELETE", "/activities/example"),
        ("POST", "/activities/link"),
        ("POST", "/activities/unlink"),
        ("PUT", "/spending/activities/example/assignments"),
        ("DELETE", "/spending/activities/example/assignments/example"),
        ("POST", "/taxonomies/categories"),
        ("PUT", "/taxonomies/categories"),
        ("POST", "/taxonomies/categories/move"),
        ("DELETE", "/taxonomies/example/categories/example"),
        ("POST", "/spending/rules"),
        ("PUT", "/spending/rules/example"),
        ("DELETE", "/spending/rules/example"),
        ("POST", "/spending/rules/rerun"),
        ("POST", "/spending/budget/targets"),
        ("DELETE", "/spending/budget/targets/example"),
        ("POST", "/spending/budget/rollovers"),
        ("DELETE", "/spending/budget/rollovers/example"),
        ("POST", "/spending/budget/groups"),
        ("PUT", "/spending/budget/groups/example"),
        ("DELETE", "/spending/budget/groups/example"),
        ("POST", "/spending/budget/group-assignments"),
        ("POST", "/spending/budget/copy"),
        ("POST", "/accounts"),
        ("PUT", "/accounts/example"),
        ("POST", "/alternative-assets"),
        ("POST", "/market-data/quotes/import"),
        ("POST", "/portfolio/recalculate"),
        ("POST", "/health/dismiss"),
    ),
)
def test_client_denies_mutation_families_before_transport(
    monkeypatch, method, path
):
    monkeypatch.delenv(MUTATION_INTERLOCK_ENV, raising=False)
    client = WealthfolioClient()
    opener = _RecordingOpener()
    client._opener = opener

    with pytest.raises(MutationInterlockError, match=f"{method} {path}"):
        client._request(method, path, {})

    assert opener.requests == []


@pytest.mark.parametrize(
    "base_url", ("http://127.0.0.1:18088", "http://127.0.0.1:8088")
)
def test_interlock_covers_staging_and_production(monkeypatch, base_url):
    monkeypatch.delenv(MUTATION_INTERLOCK_ENV, raising=False)
    client = WealthfolioClient(base_url)
    opener = _RecordingOpener()
    client._opener = opener

    with pytest.raises(MutationInterlockError, match="POST /portfolio/recalculate"):
        client.post("/portfolio/recalculate", {})

    assert opener.requests == []


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/accounts"),
        ("POST", "/auth/login"),
        ("POST", "/activities/search"),
        ("POST", "/activities/transfer-match-candidates"),
        ("POST", "/income/summary/query"),
        ("POST", "/performance/accounts/simple"),
        ("POST", "/performance/summary"),
        ("POST", "/performance/history"),
        ("POST", "/performance/summaries"),
        ("POST", "/health/check"),
        ("POST", "/spending/report"),
        ("POST", "/spending/cash-activities/search"),
        ("POST", "/spending/insight"),
        ("POST", "/spending/event-spending-summaries"),
        ("POST", "/utilities/database/backup"),
    ),
)
def test_read_only_and_backup_requests_remain_available(
    monkeypatch, method, path
):
    monkeypatch.delenv(MUTATION_INTERLOCK_ENV, raising=False)
    client = WealthfolioClient()
    opener = _RecordingOpener()
    client._opener = opener

    assert client._request(method, path, {}) == {"ok": True}
    assert len(opener.requests) == 1


def test_backup_download_uses_the_authenticated_read_route():
    client = WealthfolioClient("http://127.0.0.1:18091")
    opener = _RecordingOpener()
    client._opener = opener

    assert (
        client.download_backup("wealthfolio_backup_20260102_030405.db")
        == b'{"ok": true}'
    )
    assert opener.requests[0][0].full_url.endswith(
        "/utilities/database/backups/"
        "wealthfolio_backup_20260102_030405.db/download"
    )

    with pytest.raises(ValueError, match="invalid"):
        client.download_backup("../private.db")
    assert len(opener.requests) == 1
