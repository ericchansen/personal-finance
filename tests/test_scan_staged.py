from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def scanner():
    path = Path(__file__).parents[1] / ".githooks" / "scan_staged.py"
    spec = importlib.util.spec_from_file_location("scan_staged", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("filename", ["migration.sql", ".env.example"])
@pytest.mark.parametrize(
    ("label", "content"),
    [
        ("Account number label", "account number: " + ("9" * 9)),
        ("AWS access key", "AWS_ACCESS_KEY_ID=" + "AKIA" + ("A1" * 8)),
        ("Private key block", "-----BEGIN " + "PRIVATE KEY-----"),
    ],
)
def test_sql_and_example_configs_reject_sensitive_content(
    scanner,
    tmp_path,
    filename,
    label,
    content,
):
    candidate = tmp_path / filename
    candidate.write_text(content + "\n", encoding="utf-8")

    assert scanner.scan([candidate]) == [f"{candidate}:1: possible {label}"]


def test_safe_sql_and_example_placeholders_pass(scanner, tmp_path):
    sql = tmp_path / "migration.sql"
    sql.write_text(
        "-- synthetic account number: 00000000\n"
        "INSERT INTO settings VALUES ('api_key', 'your_example_api_key');\n",
        encoding="utf-8",
    )
    example = tmp_path / ".env.example"
    example.write_text(
        "ACCOUNT_NUMBER=00000000\n"
        "API_KEY=your_example_api_key\n"
        "PRIVATE_KEY=replace-with-generated-value\n",
        encoding="utf-8",
    )

    assert scanner.scan([sql, example]) == []


def test_existing_source_content_scan_remains_active(scanner, tmp_path):
    source = tmp_path / "module.py"
    source.write_text(
        'ACCESS_KEY = "' + "AKIA" + ("B2" * 8) + '"\n',
        encoding="utf-8",
    )

    assert scanner.scan([source]) == [f"{source}:1: possible AWS access key"]


@pytest.mark.parametrize("suffix", [".csv", ".key", ".pem"])
def test_denied_suffixes_remain_blocked(scanner, tmp_path, suffix):
    candidate = tmp_path / f"synthetic-placeholder{suffix}"
    candidate.write_text("safe synthetic placeholder\n", encoding="utf-8")

    problems = scanner.scan([candidate])

    assert len(problems) == 1
    assert "data/secret file type" in problems[0]
