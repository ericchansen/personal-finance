from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from importers.simplefin import cli
from importers.simplefin.pipeline import PipelineError


ROOT = Path(__file__).resolve().parents[1]


def test_pull_snapshot_collects_without_constructing_wealthfolio_client(
    tmp_path, monkeypatch, capsys
):
    payload = {
        "accounts": [
            {
                "org": {"name": "Synthetic Bank", "domain": "example.test"},
                "id": "synthetic-account",
                "name": "Synthetic Checking",
                "currency": "USD",
                "balance": "0",
                "balance-date": 1_767_225_600,
                "transactions": [],
            }
        ],
        "errors": ["synthetic institution unavailable"],
    }
    content = json.dumps(payload, sort_keys=True).encode()
    snapshot = (
        tmp_path
        / "raw"
        / "simplefin"
        / "2026-01-01"
        / "simplefin-010203-000001.json"
    )
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(content)
    snapshot.with_name("request-010203-000001.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "protocolVersion": 1,
                "snapshotSha256": hashlib.sha256(content).hexdigest(),
                "requestedStart": "2025-10-04",
                "requestedEnd": "2026-01-01",
                "pendingIncluded": True,
            }
        ),
        encoding="utf-8",
    )
    mapping = tmp_path / "simplefin" / "account-map.json"
    mapping.parent.mkdir()
    mapping.write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": {
                    "synthetic-account": {
                        "action": "observe",
                        "assertionAccountId": "synthetic-account",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli, "read_access_url", lambda _data_dir: "synthetic-access")
    monkeypatch.setattr(
        cli,
        "fetch_snapshot",
        lambda *_args, **_kwargs: (snapshot, payload),
    )
    monkeypatch.setattr(
        cli,
        "WealthfolioClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("collector must not construct a Wealthfolio client")
        ),
    )

    result = cli.cmd_pull_snapshot(
        SimpleNamespace(data_dir=tmp_path, days=90, protocol_version="1")
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert {
        key: output[key]
        for key in (
            "accountCount",
            "institutionErrorCount",
            "missingExpectedAccountCount",
            "requestWindowDays",
            "snapshotSha256",
        )
    } == {
        "accountCount": 1,
        "institutionErrorCount": 1,
        "missingExpectedAccountCount": 0,
        "requestWindowDays": 90,
        "snapshotSha256": hashlib.sha256(content).hexdigest(),
    }
    assert len(output["inputSetHash"]) == 64
    assert len(output["receiptHash"]) == 64
    assert Path(output["receiptPath"]).is_file()
    assert (
        tmp_path
        / "automation"
        / "source-collection"
        / "latest-success.json"
    ).is_file()


def test_scheduled_collector_is_source_only():
    installer = (
        ROOT / "importers" / "simplefin" / "install-task.ps1"
    ).read_text(encoding="utf-8")

    assert "pull-plan" not in installer
    assert "run-source-collector.ps1" in installer
    assert "current.json" in installer


def test_release_launcher_removes_all_wealthfolio_writer_opt_ins():
    launcher = (
        ROOT / "deploy" / "release" / "run-source-collector.ps1"
    ).read_text(encoding="utf-8")

    assert "WEALTHFOLIO_MUTATIONS_ENABLED" in launcher
    assert "WEALTHFOLIO_WRITER_MODE" in launcher
    assert "WEALTHFOLIO_WRITER_ENVIRONMENT_ID" in launcher
    assert "pull-snapshot" in launcher
    assert "pull-plan" not in launcher


def test_failed_collection_writes_a_safe_failure_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "read_access_url", lambda _data_dir: "synthetic-access")
    monkeypatch.setattr(
        cli,
        "fetch_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PipelineError("synthetic transport failure")
        ),
    )

    with pytest.raises(PipelineError, match="receipt retained"):
        cli.cmd_pull_snapshot(
            SimpleNamespace(data_dir=tmp_path, days=90, protocol_version="1")
        )

    pointer = json.loads(
        (
            tmp_path / "automation" / "source-collection" / "current.json"
        ).read_text(encoding="utf-8")
    )
    receipt = json.loads(
        (
            tmp_path
            / "automation"
            / "source-collection"
            / "runs"
            / f"{pointer['receiptHash']}.json"
        ).read_text(encoding="utf-8")
    )
    assert receipt["status"] == "failed"
    assert receipt["errorCode"] == "PipelineError"
    assert receipt["inputManifest"]["snapshotSha256"] is None


def test_release_installer_requires_clean_commit_and_external_target():
    installer = (
        ROOT / "deploy" / "release" / "install-release.ps1"
    ).read_text(encoding="utf-8")

    assert "git -C $repository status --porcelain" in installer
    assert "git -C $repository archive" in installer
    assert "The release root must be outside the repository" in installer
    assert "release-manifest.json" in installer
