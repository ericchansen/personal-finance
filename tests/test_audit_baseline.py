import hashlib
import json
import os
import shutil
import socket
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from importers.audit import baseline
from importers.audit.baseline import BaselineError, ReadOnlyClient
from importers.audit import cli
from importers.monarch.wealthfolio_client import WealthfolioError


FIXED_TIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_indexed_reads_are_bounded_and_preserve_key_order():
    keys = [f"SYN-{index}" for index in range(24)]
    barrier = threading.Barrier(baseline.READ_CONCURRENCY, timeout=10)
    lock = threading.Lock()
    active = peak = 0

    def read(key):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait()
        with lock:
            active -= 1
        return {"synthetic": key}

    result = baseline._read_indexed(keys, read)
    assert list(result) == keys
    assert result == {key: {"synthetic": key} for key in keys}
    assert peak == baseline.READ_CONCURRENCY


def test_indexed_read_failure_does_not_schedule_later_batches():
    calls = []

    def read(key):
        calls.append(key)
        if key == "SYN-0":
            raise RuntimeError("synthetic read failure")
        return key

    with pytest.raises(RuntimeError, match="synthetic read failure"):
        baseline._read_indexed([f"SYN-{index}" for index in range(24)], read)
    assert set(calls) <= {f"SYN-{index}" for index in range(baseline.READ_CONCURRENCY)}


class FakeClient:
    def __init__(
        self,
        *,
        activity_count=2,
        gaps=(),
        sensitive=False,
        secret_value="SYNTHETIC_PROVIDER_SECRET",
        db_path="synthetic-private.db",
    ):
        marker = "PRIVATE_PERSON" if sensitive else "Synthetic"
        self.calls = []
        self.activity_count = activity_count
        self.gaps = set(gaps)
        self.activities = [
            {
                "id": f"activity-{index}",
                "accountId": "account-1",
                "amount": str(index + 1),
                "description": f"{marker} Merchant",
                "source": "SYNTHETIC",
                "sourceId": f"source-{index}",
            }
            for index in range(activity_count)
        ]
        self.responses = {
            "/app/info": {
                "version": "3.7.0-synthetic",
                "dbPath": db_path,
                "logsDir": r"C:\Users\SYNTHETIC_USER\wealthfolio\logs",
            },
            "/accounts": [
                {
                    "id": "account-1",
                    "name": f"{marker} Account",
                    "trackingMode": "TRANSACTIONS",
                }
            ],
            "/taxonomies": [{"id": "spending_categories"}],
            "/taxonomies/spending_categories": {
                "taxonomy": {"id": "spending_categories"},
                "categories": [{"id": "category-1", "name": f"{marker} Category"}],
            },
            "/spending/event-types": [{"id": "event-type-1"}],
            "/spending/events": [{"id": "event-1"}],
            "/spending/cash-activities": [
                {"id": "activity-1", "eventId": "event-1"}
            ],
            "/spending/rules": [{"id": "rule-1"}],
            "/spending/budget": {"periodKey": "2026-01", "targets": []},
            "/spending/settings": {"enabled": True, "accountIds": ["account-1"]},
            "/settings": {"baseCurrency": "USD", "timezone": "UTC"},
            "/goals": [{"id": "goal-1", "name": f"{marker} Goal"}],
            "/goals/goal-1/funding": [{"id": "funding-1"}],
            "/goals/goal-1/plan": {"id": "plan-1", "planKind": "retirement"},
            "/assets": [{"id": "asset-1", "symbol": "SYN"}],
            "/assets/profile": {"assetId": "asset-1", "assetClass": "synthetic"},
            "/market-data/quotes/history": [
                {"id": "quote-1", "assetId": "asset-1", "close": "1"}
            ],
            "/exchange-rates/latest": [{"id": "fx-1", "rate": "1"}],
            "/providers/settings": [
                {
                    "id": "provider-1",
                    "enabled": True,
                    "requiresApiKey": True,
                    "hasApiKey": True,
                    "apiKey": secret_value,
                    "nested": {
                        "Refresh_Token": f"{secret_value}-refresh",
                        "headers": {"AUTHORIZATION": f"Bearer {secret_value}"},
                        "baseUrl": "https://provider.example.invalid/api",
                        "endpoint": (
                            f"https://synthetic-user:{secret_value}"
                            "@provider.example.invalid/api"
                        ),
                        "href": (
                            "https://provider.example.invalid/callback"
                            f"?access_token={secret_value}"
                        ),
                        "authHeader": f"Basic {secret_value}",
                        "accessUri": "https://provider.example.invalid/private",
                        "sessionId": f"{secret_value}-session",
                        "headerList": [
                            {
                                "name": "Authorization",
                                "value": f"Basic {secret_value}",
                            },
                            {
                                "key": "X-Api-Key",
                                "value": secret_value,
                            },
                        ],
                    },
                }
            ],
            "/snapshots": [
                {
                    "id": "snapshot-1",
                    "snapshotDate": "2026-01-01",
                    "positionCount": 1,
                }
            ],
            "/snapshots/holdings": [{"assetId": "asset-1", "quantity": "1"}],
            "/holdings": [{"assetId": "asset-1", "quantity": "1"}],
            "/alternative-holdings": [{"id": "alternative-1", "kind": "property"}],
            "/portfolios": [{"id": "portfolio-1"}],
            "/allocation-targets": [{"id": "allocation-1"}],
            "/allocation-targets/allocation-1/weights": [
                {"id": "weight-1", "weight": "1"}
            ],
            "/allocation-targets/allocation-1/constraints": [],
            "/healthz": "ok",
            "/health/status": {"status": "healthy", "issues": []},
            "/utilities/database/backups": [
                {"filename": "wealthfolio-synthetic-backup.zip", "size": 123}
            ],
            "/activities/import/mapping": {"accountId": "account-1", "fields": {}},
            "/activities/import/templates": [{"id": "template-1"}],
        }

    def login(self, password):
        self.calls.append(("LOGIN", "/auth/login"))

    def get(self, path):
        self.calls.append(("GET", path))
        route = urlsplit(path).path
        if route in self.gaps:
            raise WealthfolioError(404, route, "sensitive response body")
        if route.startswith("/spending/activities/"):
            if route.endswith("/assignments"):
                return [{"taxonomyId": "spending_categories", "categoryId": "category-1"}]
            if route.endswith("/splits"):
                return []
        if route.startswith("/activities/") and route.endswith("/transfer-pair"):
            return {"pair": None}
        if route.startswith("/taxonomies/assignments/asset/"):
            return [
                {
                    "id": "asset-assignment-1",
                    "assetId": "asset-1",
                    "taxonomyId": "spending_categories",
                    "categoryId": "category-1",
                    "weight": 10000,
                    "source": "manual",
                }
            ]
        if route not in self.responses:
            raise AssertionError(f"unexpected GET {path}")
        return self.responses[route]

    def post(self, path, payload):
        self.calls.append(("POST", path))
        if path != "/activities/search":
            raise AssertionError(f"mutation POST attempted: {path}")
        start = payload["page"] * payload["pageSize"]
        end = start + payload["pageSize"]
        return {
            "data": self.activities[start:end],
            "meta": {"totalRowCount": self.activity_count},
        }

    def put(self, path, payload):
        raise AssertionError(f"mutation PUT attempted: {path}")

    def delete(self, path, payload=None):
        raise AssertionError(f"mutation DELETE attempted: {path}")

    def backup_database(self):
        raise AssertionError("backup creation attempted")


def private_tree(tmp_path: Path) -> Path:
    root = tmp_path / "private"
    (root / "raw" / "example").mkdir(parents=True)
    (root / "facts").mkdir()
    (root / "decisions").mkdir()
    (root / "normalized" / "canonical").mkdir(parents=True)
    (root / "wealthfolio").mkdir()
    (root / "raw" / "example" / "snapshot.json").write_text(
        '{"synthetic":true}\n', encoding="utf-8"
    )
    (root / "facts" / "accounts.json").write_text(
        '{"schemaVersion":1}\n', encoding="utf-8"
    )
    (root / "decisions" / "review-plan.json").write_text(
        '{"approved":true}\n', encoding="utf-8"
    )
    (root / "normalized" / "canonical" / "manifest.json").write_text(
        '{"schemaVersion":4}\n', encoding="utf-8"
    )
    (root / "wealthfolio" / "ADMIN-PASSWORD.txt").write_text(
        "synthetic-secret\n", encoding="utf-8"
    )
    return root


def create(root: Path, client=None, **kwargs):
    return baseline.build(
        root,
        client or FakeClient(),
        base_url="http://127.0.0.1:8088",
        repo_root=kwargs.pop("repo_root", root.parent / "checkout"),
        now=kwargs.pop("now", FIXED_TIME),
        **kwargs,
    )


def current_manifest(root: Path) -> tuple[dict, Path]:
    output = root / "audit" / "baselines"
    pointer = json.loads((output / "current.json").read_text(encoding="utf-8"))
    publication = output / "publications" / pointer["publicationId"]
    return json.loads((publication / "manifest.json").read_text(encoding="utf-8")), publication


def publication_bytes(root: Path) -> bytes:
    _manifest, publication = current_manifest(root)
    paths = [root / "audit" / "baselines" / "current.json"]
    paths.extend(path for path in publication.rglob("*") if path.is_file())
    return b"".join(path.read_bytes() for path in sorted(paths))


def test_capture_is_read_only_and_paginates(monkeypatch, tmp_path):
    monkeypatch.setattr(baseline, "PAGE_SIZE", 2)
    root = private_tree(tmp_path)
    client = FakeClient(activity_count=5)

    result = create(root, client)

    search_calls = [call for call in client.calls if call == ("POST", "/activities/search")]
    assert len(search_calls) == 3
    assert all(method in {"GET", "POST"} for method, _path in client.calls)
    assert {path for method, path in client.calls if method == "POST"} == {
        "/activities/search"
    }
    assert ("GET", "/accounts?includeArchived=true") in client.calls
    assert (
        "GET",
        "/market-data/quotes/history?symbol=asset-1",
    ) in client.calls
    assert result["recordCounts"]["activities"] == 5
    manifest, publication = current_manifest(root)
    activities = json.loads(
        (publication / "domains" / "activities.json").read_text(encoding="utf-8")
    )
    asset_assignments = json.loads(
        (publication / "domains" / "asset-assignments.json").read_text(
            encoding="utf-8"
        )
    )
    import_metadata = json.loads(
        (publication / "domains" / "import-metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert activities["recordCount"] == 5
    assert asset_assignments["records"]["asset-1"][0]["weight"] == 10000
    assert set(import_metadata["records"]["accountMappings"]["account-1"]) == {
        "BROKER_ACTIVITY",
        "CSV_ACTIVITY",
        "CSV_HOLDINGS",
    }
    assert manifest["readOnly"] is True


def test_unavailable_domain_is_a_typed_capability_gap(tmp_path):
    root = private_tree(tmp_path)
    create(root, FakeClient(gaps={"/spending/events"}))

    manifest, publication = current_manifest(root)
    capability = manifest["capabilityResults"]["spending-events"]
    snapshot = json.loads(
        (publication / "domains" / "spending-events.json").read_text(encoding="utf-8")
    )
    assert capability == {
        "endpoint": "/spending/events",
        "httpStatus": 404,
        "method": "GET",
        "reasonType": "http-status",
        "status": "unavailable",
    }
    assert snapshot["status"] == "unavailable"
    assert snapshot["gap"] == capability


def test_child_resource_404_fails_closed_instead_of_becoming_a_gap(tmp_path):
    root = private_tree(tmp_path)
    client = FakeClient(gaps={"/goals/goal-1/funding"})

    with pytest.raises(BaselineError, match="goal funding changed"):
        create(root, client)

    assert not (root / "audit" / "baselines" / "current.json").exists()


def test_non_transfer_activity_400_is_recorded_as_no_pair():
    client = FakeClient(activity_count=1)

    def no_pair(path):
        raise WealthfolioError(
            400,
            path,
            json.dumps(
                {
                    "code": 400,
                    "message": baseline.NOT_TRANSFER_PAIR_MESSAGE,
                }
            ),
        )

    client.get = no_pair

    assert baseline._transfer_pairs(ReadOnlyClient(client), ["activity-0"]) == {
        "activity-0": None
    }


@pytest.mark.parametrize("suffix", ["assignments", "splits"])
def test_non_spending_activity_400_is_recorded_as_empty_children(suffix):
    client = FakeClient(activity_count=1)

    def not_opted_in(path):
        raise WealthfolioError(
            400,
            path,
            json.dumps(
                {
                    "code": 400,
                    "message": baseline.NOT_SPENDING_ACTIVITY_MESSAGE,
                }
            ),
        )

    client.get = not_opted_in

    assert baseline._activity_children(
        ReadOnlyClient(client), ["activity-0"], suffix
    ) == {"activity-0": []}


def test_unexpected_transfer_activity_400_fails_closed():
    client = FakeClient(activity_count=1)

    def unexpected(path):
        raise WealthfolioError(
            400,
            path,
            '{"code":400,"message":"synthetic unexpected failure"}',
        )

    client.get = unexpected

    with pytest.raises(WealthfolioError, match="synthetic unexpected failure"):
        baseline._transfer_pairs(ReadOnlyClient(client), ["activity-0"])


def test_pagination_requires_stable_declared_total(monkeypatch, tmp_path):
    root = private_tree(tmp_path)
    client = FakeClient(activity_count=3)
    original = client.post

    def truncated(path, payload):
        response = original(path, payload)
        response["meta"]["totalRowCount"] = 4
        return response

    client.post = truncated
    monkeypatch.setattr(baseline, "PAGE_SIZE", 2)

    with pytest.raises(BaselineError, match="ended before its declared total"):
        create(root, client)


def test_secret_material_is_classified_without_content_metadata(tmp_path):
    root = private_tree(tmp_path)
    create(root)
    manifest, _publication = current_manifest(root)

    password = next(
        entry
        for entry in manifest["sourceFiles"]
        if entry["path"] == "wealthfolio/ADMIN-PASSWORD.txt"
    )
    assert password == {
        "kind": "credential-material",
        "omitted": True,
        "path": "wealthfolio/ADMIN-PASSWORD.txt",
        "reason": "credential contents and metadata are excluded",
    }
    encoded = json.dumps(manifest)
    assert "synthetic-secret" not in encoded


def test_simplefin_access_url_is_never_read_or_hashed(tmp_path):
    root = private_tree(tmp_path)
    credential = root / "simplefin" / "access-url.txt"
    credential.parent.mkdir()
    credential.write_text("https://user:secret@example.invalid/data", encoding="utf-8")

    entries = baseline.inventory_evidence(root)

    entry = next(row for row in entries if row["path"] == "simplefin/access-url.txt")
    assert entry["kind"] == "credential-material"
    assert entry["omitted"] is True
    assert "size" not in entry
    assert "sha256" not in entry


@pytest.mark.parametrize(
    "name",
    [
        "live.db",
        "live.db-journal",
        "live.db-shm",
        "live.db-wal",
        "live.sqlite",
        "live.sqlite-journal",
        "live.sqlite-shm",
        "live.sqlite-wal",
        "live.sqlite3",
        "live.sqlite3-journal",
        "live.sqlite3-shm",
        "live.sqlite3-wal",
    ],
)
def test_live_database_state_is_omitted_without_content_metadata(tmp_path, name):
    root = tmp_path / "private"
    root.mkdir()
    (root / name).write_bytes(b"synthetic database state")

    assert baseline.inventory_evidence(root) == [
        {
            "kind": "live-database-state",
            "omitted": True,
            "path": name,
            "reason": "live database contents and metadata are excluded",
        }
    ]


def test_live_database_name_is_classified_before_stat(monkeypatch, tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    path = root / "transient.db-wal"
    path.write_bytes(b"synthetic transient state")
    original = Path.lstat

    def fail_if_inspected(self):
        if self == path:
            raise AssertionError("live database state was inspected")
        return original(self)

    monkeypatch.setattr(Path, "lstat", fail_if_inspected)

    assert baseline.inventory_evidence(root) == [
        {
            "kind": "live-database-state",
            "omitted": True,
            "path": "transient.db-wal",
            "reason": "live database contents and metadata are excluded",
        }
    ]


def test_disappearing_file_is_safely_omitted(monkeypatch, tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    path = root / "transient.json"
    path.write_text('{"synthetic":true}\n', encoding="utf-8")
    original = Path.lstat

    def disappear_during_stat(self):
        if self == path:
            raise FileNotFoundError(2, "synthetic disappearance", str(path))
        return original(self)

    monkeypatch.setattr(Path, "lstat", disappear_during_stat)

    assert baseline.inventory_evidence(root) == []


def test_live_database_sidecar_disappearance_does_not_invalidate_baseline(tmp_path):
    root = private_tree(tmp_path)
    sidecar = root / "runtime.db-shm"
    sidecar.write_bytes(b"synthetic transient state")
    create(root)

    sidecar.unlink()

    assert baseline.verify(
        root, repo_root=root.parent / "checkout"
    )["verified"] is True


def test_explicit_database_backup_is_hashed(tmp_path):
    root = tmp_path / "private"
    path = root / "backups" / "sealed.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic sealed database backup")

    entry = baseline.inventory_evidence(root)[0]

    assert entry["kind"] == "database-backup"
    assert entry["omitted"] is False
    assert entry["size"] == path.stat().st_size
    assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("raw/example/snapshot.json", "raw-snapshot"),
        ("extracts/example/export.json", "source-extract"),
        ("mappings/accounts.json", "mapping"),
        ("facts/accounts.json", "fact"),
        ("decisions/review.json", "decision"),
        ("plans/review.json", "plan"),
        ("receipts/run.json", "receipt"),
        ("normalized/simplefin/apply-report-2026-01-02.json", "receipt"),
        ("normalized/canonical/manifest.json", "canonical-publication"),
        ("normalized/analytics/manifest.json", "analytics-publication"),
        ("deployment/instance.json", "deployment-metadata"),
    ],
)
def test_evidence_artifact_classification(path, kind):
    assert baseline._artifact_kind(Path(path)) == kind


@pytest.mark.parametrize(
    "path",
    [
        "audit/duplicates/current.json",
        "audit/.lineage-state/review.lock",
        "audit/lineage-review/current.json",
        "normalized/analytics/current.json",
        "normalized/analytics-diagnostics/example/current.json",
        "normalized/.canonical-staging-synthetic/manifest.json",
        "normalized/.canonical-backup-synthetic/manifest.json",
        "normalized/canonical/manifest.json",
        "postgres-shadow/plans/synthetic.json",
        "wealthfolio-rebuild/bundles/current.json",
        "automation/authority-cycle/current.json",
        "incremental/plans/synthetic.json",
        "incremental/receipts/synthetic.json",
        "incremental/baselines/synthetic/raw/source.json",
    ],
)
def test_downstream_publications_are_not_baseline_evidence(tmp_path, path):
    root = tmp_path / "private"
    output = root / path
    output.parent.mkdir(parents=True)
    output.write_text('{"synthetic":true}\n', encoding="utf-8")

    assert baseline.inventory_evidence(root) == []
    assert (
        baseline._stable_inventory(
            [
                {
                    "path": path,
                    "kind": baseline._artifact_kind(Path(path)),
                    "omitted": False,
                }
            ]
        )
        == []
    )


def test_apply_report_is_inventoried_as_a_receipt(tmp_path):
    root = tmp_path / "private"
    report = (
        root
        / "normalized"
        / "simplefin"
        / "apply-report-2026-01-02-030405-000000.json"
    )
    report.parent.mkdir(parents=True)
    report.write_text('{"schemaVersion":5}\n', encoding="utf-8")

    entry = next(
        item
        for item in baseline.inventory_evidence(root)
        if item["path"].endswith(report.name)
    )

    assert entry["kind"] == "receipt"
    assert entry["sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()


def test_data_dir_inside_repository_is_rejected(tmp_path):
    checkout = tmp_path / "checkout"
    root = checkout / "private"
    root.mkdir(parents=True)
    with pytest.raises(Exception, match="repository"):
        create(root, repo_root=checkout)


def test_path_traversal_and_symlink_escape_are_rejected(tmp_path):
    root = private_tree(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    with pytest.raises(BaselineError, match="leaves --data-dir"):
        baseline._safe_relative(outside, root)

    link = root / "raw" / "escape.json"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(BaselineError, match="symlink escapes"):
        baseline.inventory_evidence(root)


def test_unix_socket_is_omitted_as_special_evidence(tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    path = root / "synthetic.sock"
    try:
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
    except (AttributeError, OSError):
        pytest.skip("AF_UNIX sockets are unavailable")
    try:
        assert baseline.inventory_evidence(root) == [
            {
                "kind": "special-file",
                "omitted": True,
                "path": "synthetic.sock",
                "reason": "non-regular files are not read",
            }
        ]
    finally:
        server.close()
        path.unlink(missing_ok=True)


def test_windows_af_unix_reparse_point_is_not_a_regular_file():
    metadata = type(
        "SyntheticMetadata",
        (),
        {
            "st_mode": stat.S_IFREG,
            "st_reparse_tag": baseline.AF_UNIX_REPARSE_TAG,
        },
    )()

    assert baseline._is_regular_file(metadata) is False


def test_publication_hashes_are_deterministic(tmp_path):
    left = private_tree(tmp_path / "left")
    right = private_tree(tmp_path / "right")

    left_result = create(left)
    right_result = create(right)

    assert left_result["publication"] == right_result["publication"]
    left_manifest, left_publication = current_manifest(left)
    right_manifest, right_publication = current_manifest(right)
    assert left_manifest == right_manifest
    assert (left_publication / "domains" / "activities.json").read_bytes() == (
        right_publication / "domains" / "activities.json"
    ).read_bytes()


def test_api_credentials_and_local_paths_are_sanitized_recursively(tmp_path):
    root = private_tree(tmp_path)
    sensitive_material = "opaque-provider-material"
    db_path = "\\".join(
        ("C:", "Users", "SYNTHETIC_PROFILE", "finance", "wealthfolio.db")
    )
    create(root, FakeClient(secret_value=sensitive_material, db_path=db_path))

    combined = publication_bytes(root)
    assert sensitive_material.encode() not in combined
    assert b"SYNTHETIC_PROFILE" not in combined
    assert db_path.encode() not in combined
    assert b"Bearer " not in combined

    manifest, publication = current_manifest(root)
    app_info = json.loads(
        (publication / "domains" / "app-info.json").read_text(encoding="utf-8")
    )["records"]
    providers = json.loads(
        (publication / "domains" / "market-data-provider-settings.json").read_text(
            encoding="utf-8"
        )
    )["records"]
    expected_path_hash = hashlib.sha256(db_path.encode("utf-8")).hexdigest()
    assert app_info["dbPath"] == f"sha256:{expected_path_hash}"
    assert app_info["logsDir"].startswith("sha256:")
    assert providers[0]["apiKey"] == baseline.REDACTED
    assert providers[0]["requiresApiKey"] is True
    assert providers[0]["hasApiKey"] is True
    assert providers[0]["nested"]["Refresh_Token"] == baseline.REDACTED
    assert providers[0]["nested"]["headers"]["AUTHORIZATION"] == baseline.REDACTED
    assert providers[0]["id"] == "provider-1"
    assert providers[0]["enabled"] is True
    assert providers[0]["nested"]["baseUrl"] == "https://provider.example.invalid/api"
    assert providers[0]["nested"]["endpoint"] == baseline.REDACTED
    assert providers[0]["nested"]["href"] == baseline.REDACTED
    assert providers[0]["nested"]["authHeader"] == baseline.REDACTED
    assert providers[0]["nested"]["accessUri"] == baseline.REDACTED
    assert providers[0]["nested"]["sessionId"] == baseline.REDACTED
    assert providers[0]["nested"]["headerList"][0]["name"] == "Authorization"
    assert providers[0]["nested"]["headerList"][0]["value"] == baseline.REDACTED
    assert providers[0]["nested"]["headerList"][1]["key"] == "X-Api-Key"
    assert providers[0]["nested"]["headerList"][1]["value"] == baseline.REDACTED
    assert manifest["environmentFingerprint"] == baseline.plan_fingerprint(
        {
            "origin": "http://127.0.0.1:8088",
            "version": "3.7.0-synthetic",
            "dbPath": db_path,
        }
    )


def test_secret_values_do_not_affect_deterministic_publication_hashes(tmp_path):
    left = private_tree(tmp_path / "left")
    right = private_tree(tmp_path / "right")

    left_result = create(left, FakeClient(secret_value="opaque-material-one"))
    right_result = create(right, FakeClient(secret_value="opaque-material-two"))

    assert left_result["publication"] == right_result["publication"]
    assert b"opaque-material-one" not in publication_bytes(left)
    assert b"opaque-material-two" not in publication_bytes(right)


def test_environment_fingerprint_binds_raw_db_path_without_publishing_it(tmp_path):
    left = private_tree(tmp_path / "left")
    right = private_tree(tmp_path / "right")
    left_path = "\\".join(("C:", "Users", "PROFILE_ONE", "wealthfolio.db"))
    right_path = "\\".join(("C:", "Users", "PROFILE_TWO", "wealthfolio.db"))

    create(left, FakeClient(db_path=left_path))
    create(right, FakeClient(db_path=right_path))

    left_manifest, _ = current_manifest(left)
    right_manifest, _ = current_manifest(right)
    assert left_manifest["environmentFingerprint"] != right_manifest[
        "environmentFingerprint"
    ]
    assert left_path.encode() not in publication_bytes(left)
    assert right_path.encode() not in publication_bytes(right)


def test_atomic_failure_preserves_prior_current_pointer(tmp_path):
    root = private_tree(tmp_path)
    create(root, now=FIXED_TIME)
    pointer = root / "audit" / "baselines" / "current.json"
    before = pointer.read_bytes()

    def fail_before_pointer(_publication):
        raise OSError("synthetic interruption")

    with pytest.raises(OSError, match="synthetic interruption"):
        create(
            root,
            now=datetime(2026, 1, 2, 4, 0, tzinfo=timezone.utc),
            before_pointer=fail_before_pointer,
        )

    assert pointer.read_bytes() == before
    assert baseline.verify(root, repo_root=root.parent / "checkout")["verified"] is True


def test_corrupt_existing_publication_is_not_reused(tmp_path):
    root = private_tree(tmp_path)
    create(root)
    pointer = root / "audit" / "baselines" / "current.json"
    before = pointer.read_bytes()
    _manifest, publication = current_manifest(root)
    (publication / "domains" / "activities.json").write_text(
        '{"corrupt":true}\n', encoding="utf-8"
    )

    with pytest.raises(BaselineError, match="existing baseline publication is corrupt"):
        create(root)

    assert pointer.read_bytes() == before


def test_verify_detects_changed_and_missing_evidence(tmp_path):
    root = private_tree(tmp_path)
    create(root)
    evidence = root / "facts" / "accounts.json"
    evidence.write_text('{"schemaVersion":2}\n', encoding="utf-8")
    with pytest.raises(BaselineError, match="evidence manifest changed"):
        baseline.verify(root, repo_root=root.parent / "checkout")

    evidence.unlink()
    with pytest.raises(BaselineError, match="evidence manifest changed"):
        baseline.verify(root, repo_root=root.parent / "checkout")


def test_verify_detects_output_hash_count_and_pointer_corruption(tmp_path):
    root = private_tree(tmp_path)
    create(root)
    _manifest, publication = current_manifest(root)
    activities = publication / "domains" / "activities.json"
    activities.write_text('{"schemaVersion":1}\n', encoding="utf-8")
    with pytest.raises(BaselineError, match="domain hash mismatch"):
        baseline.verify(root, repo_root=root.parent / "checkout")

    root = private_tree(tmp_path / "pointer")
    create(root)
    pointer = root / "audit" / "baselines" / "current.json"
    document = json.loads(pointer.read_text(encoding="utf-8"))
    document["manifestSha256"] = "0" * 64
    pointer.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(BaselineError, match="pointer schema"):
        baseline.verify(root, repo_root=root.parent / "checkout")


def test_verify_requires_the_authoritative_domain_inventory(tmp_path):
    root = private_tree(tmp_path)
    create(root)
    manifest, publication = current_manifest(root)
    omitted = "exchange-rate-history"
    manifest["capabilityResults"].pop(omitted)
    manifest["recordCounts"].pop(omitted)
    reference = manifest["domainFiles"].pop(f"{omitted}.json")
    content = baseline._json_bytes(manifest)
    publication_id = hashlib.sha256(content).hexdigest()
    replacement = publication.parent / publication_id
    shutil.copytree(publication, replacement)
    (replacement / "manifest.json").write_bytes(content)
    (replacement / reference["path"]).unlink()
    pointer = {
        "schemaVersion": 1,
        "publicationId": publication_id,
        "manifestSha256": publication_id,
    }
    (root / "audit" / "baselines" / "current.json").write_bytes(
        baseline._json_bytes(pointer)
    )

    with pytest.raises(BaselineError, match="required domain inventory"):
        baseline.verify(root, repo_root=root.parent / "checkout")


def test_cli_console_never_prints_private_records(monkeypatch, tmp_path, capsys):
    root = private_tree(tmp_path)
    client = FakeClient(sensitive=True)
    monkeypatch.setattr(cli, "WealthfolioClient", lambda _url: client)
    monkeypatch.setattr(cli, "REPO_ROOT", root.parent / "checkout")
    monkeypatch.setenv("WEALTHFOLIO_PASSWORD", "PRIVATE_PASSWORD")

    assert cli.main(["create", "--data-dir", str(root)]) == 0

    output = capsys.readouterr().out
    assert "PRIVATE_PERSON" not in output
    assert "PRIVATE_PASSWORD" not in output
    assert "Merchant" not in output
    assert "account-1" not in output
    assert "path=" in output
    assert "sources=" in output


def test_cli_stat_error_uses_relative_path_only(monkeypatch, tmp_path, capsys):
    root = private_tree(tmp_path)
    evidence = root / "facts" / "accounts.json"
    windows_private_path = "\\".join(
        (
            "C:",
            "Users",
            "PRIVATE_PROFILE",
            "documents",
            "finance-data",
            "facts",
            "accounts.json",
        )
    )
    original = Path.lstat

    def fail_with_private_path(self):
        if self == evidence:
            raise OSError(5, "synthetic stat failure", windows_private_path)
        return original(self)

    monkeypatch.setattr(Path, "lstat", fail_with_private_path)
    monkeypatch.setattr(cli, "WealthfolioClient", lambda _url: FakeClient())
    monkeypatch.setattr(cli, "REPO_ROOT", root.parent / "checkout")
    monkeypatch.setenv("WEALTHFOLIO_PASSWORD", "PRIVATE_PASSWORD")

    assert cli.main(["create", "--data-dir", str(root)]) == 1

    output = capsys.readouterr().out
    assert windows_private_path not in output
    assert str(root) not in output
    assert "cannot inspect evidence entry: facts/accounts.json" in output


def test_read_only_client_rejects_all_mutation_methods():
    guarded = ReadOnlyClient(FakeClient())
    with pytest.raises(BaselineError, match="refused POST"):
        guarded.post("/utilities/database/backup", {})
    with pytest.raises(BaselineError, match="refused PUT"):
        guarded.put("/settings", {})
    with pytest.raises(BaselineError, match="refused DELETE"):
        guarded.delete("/activities/example")
    with pytest.raises(BaselineError, match="backup creation"):
        guarded.backup_database()
