import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from finance_store.sources import _extract_batches
from importers.extracts.correspondence import (
    LEGACY_RULE, RULE, parse_mapped_extract, parse_mapped_extracts,
)
from importers.normalized.builder import build, verify_publication
from tests.test_normalized import make_estate, write


DESCRIPTION = "Synthetic Merchant Reference A7B8C9D0 Full Detail"


def paired(root: Path, *, repeat: bool = False):
    records = [
        f"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20240110<TRNAMT>-12.34"
        f"<FITID>SYN-{index}<NAME>{DESCRIPTION[:27]}</STMTTRN>"
        for index in range(2 if repeat else 1)
    ]
    primary = write(
        root / "extracts" / "example" / "paired.ofx",
        "<OFX><CURDEF>USD<BANKACCTFROM><ACCTID>SYN-PRIVATE</BANKACCTFROM>"
        + "".join(records) + "</OFX>",
    )
    csv = write(
        primary.with_suffix(".csv"),
        "Status,Date,Description,Debit,Credit\n"
        + f"Posted,01/10/2024,{DESCRIPTION},12.34,\n" * len(records),
    )
    entry = {
        "file": str(primary.relative_to(root / "extracts")),
        "account": "Example Checking",
        "descriptionEvidence": {
            "rule": RULE,
            "path": str(csv.relative_to(root / "extracts")),
            "primarySha256": hashlib.sha256(primary.read_bytes()).hexdigest(),
            "supportingSha256": hashlib.sha256(csv.read_bytes()).hexdigest(),
        },
    }
    return primary, csv, entry


def test_explicit_bank_correspondence_preserves_primary_ids_and_bytes(tmp_path):
    primary, supporting, entry = paired(tmp_path)
    raw = primary.read_bytes()
    result = parse_mapped_extract(
        primary, entry, data_root=tmp_path, account_id="acct-main"
    )
    assert result.extract.transactions[0].description == DESCRIPTION
    assert result.extract.transactions[0].source_id == "SYN-0"
    assert not result.extract.transactions[0].id_is_synthetic
    assert primary.read_bytes() == raw
    assert result.supporting_files == (supporting,)
    assert result.description_evidence[0]["originalDescription"] == DESCRIPTION[:27]
    assert result.description_evidence[0]["description"] == DESCRIPTION


def test_no_implicit_neighbor_discovery(tmp_path):
    primary, _, entry = paired(tmp_path)
    del entry["descriptionEvidence"]
    result = parse_mapped_extract(
        primary, entry, data_root=tmp_path, account_id="acct-main"
    )
    assert result.extract.transactions[0].description == DESCRIPTION[:27]
    assert result.supporting_files == ()


def test_bank_cleared_status_is_supported(tmp_path):
    primary, supporting, entry = paired(tmp_path)
    supporting.write_text(supporting.read_text().replace("Posted", "Cleared"))
    entry["descriptionEvidence"]["supportingSha256"] = hashlib.sha256(
        supporting.read_bytes()
    ).hexdigest()
    result = parse_mapped_extract(
        primary, entry, data_root=tmp_path, account_id="acct-main"
    )
    assert result.extract.transactions[0].description == DESCRIPTION


def test_structured_bank_instrument_suffix_is_preserved_outside_description(tmp_path):
    primary, supporting, entry = paired(tmp_path)
    supporting.write_text(supporting.read_text().replace(
        DESCRIPTION, DESCRIPTION + " null XXXXXXXXXXXX1234"
    ))
    entry["descriptionEvidence"]["supportingSha256"] = hashlib.sha256(
        supporting.read_bytes()
    ).hexdigest()
    result = parse_mapped_extract(primary, entry, data_root=tmp_path, account_id="acct-main")
    assert result.extract.transactions[0].description == DESCRIPTION
    assert result.extract.transactions[0].source_id == "SYN-0"
    assert result.description_evidence[0]["paymentInstrumentMask"] == "XXXXXXXXXXXX1234"
    assert result.description_evidence[0]["bankDescription"] == (
        DESCRIPTION + " null XXXXXXXXXXXX1234"
    )
    entry["descriptionEvidence"]["rule"] = LEGACY_RULE
    legacy = parse_mapped_extract(primary, entry, data_root=tmp_path, account_id="acct-main")
    assert legacy.extract.transactions[0].description.endswith(" null XXXXXXXXXXXX1234")
    assert legacy.description_evidence[0]["paymentInstrumentMask"] is None


@pytest.mark.parametrize(
    "suffix", [" null", " XXXXXXXXXXXX1234", " null X1234", " null SYN-REF1234"]
)
def test_unrecognized_suffixes_are_not_removed(tmp_path, suffix):
    primary, supporting, entry = paired(tmp_path)
    supporting.write_text(supporting.read_text().replace(DESCRIPTION, DESCRIPTION + suffix))
    entry["descriptionEvidence"]["supportingSha256"] = hashlib.sha256(
        supporting.read_bytes()
    ).hexdigest()
    result = parse_mapped_extract(primary, entry, data_root=tmp_path, account_id="acct-main")
    assert result.extract.transactions[0].description == DESCRIPTION + suffix
    assert result.description_evidence[0]["paymentInstrumentMask"] is None


@pytest.mark.parametrize("source", ["primary", "supporting"])
def test_changed_pair_bytes_fail_closed(tmp_path, source):
    primary, supporting, entry = paired(tmp_path)
    (primary if source == "primary" else supporting).write_text("changed")
    with pytest.raises(ValueError, match="source hash changed"):
        parse_mapped_extract(primary, entry, data_root=tmp_path, account_id="acct-main")


def test_equal_count_but_different_economics_is_not_correspondence(tmp_path):
    primary, supporting, entry = paired(tmp_path)
    supporting.write_text(supporting.read_text().replace("12.34", "56.78"))
    entry["descriptionEvidence"]["supportingSha256"] = hashlib.sha256(
        supporting.read_bytes()
    ).hexdigest()
    with pytest.raises(ValueError, match="different economic occurrence"):
        parse_mapped_extract(primary, entry, data_root=tmp_path, account_id="acct-main")


def test_repeated_buckets_are_preserved_without_guessing_correspondence(tmp_path):
    primary, _, entry = paired(tmp_path, repeat=True)
    result = parse_mapped_extract(
        primary, entry, data_root=tmp_path, account_id="acct-main"
    )
    assert len(result.extract.transactions) == 2
    assert all(row.description == DESCRIPTION[:27] for row in result.extract.transactions)
    assert result.description_evidence == ()


def test_arbitrary_prefix_is_not_the_declared_fixed_width_transformation(tmp_path):
    primary, _, entry = paired(tmp_path)
    primary.write_text(primary.read_text().replace(DESCRIPTION[:27], "Synthetic"))
    entry["descriptionEvidence"]["primarySha256"] = hashlib.sha256(
        primary.read_bytes()
    ).hexdigest()
    result = parse_mapped_extract(
        primary, entry, data_root=tmp_path, account_id="acct-main"
    )
    assert result.extract.transactions[0].description == "Synthetic"
    assert result.description_evidence == ()


@pytest.mark.parametrize(
    "modification", ["bad-date", "both-amounts", "missing-id", "pending", "currency"]
)
def test_malformed_source_rows_cannot_be_silently_omitted(tmp_path, modification):
    primary, supporting, entry = paired(tmp_path)
    if modification in {"missing-id", "currency"}:
        text = primary.read_text()
        primary.write_text(
            text.replace("<FITID>SYN-0", "") if modification == "missing-id"
            else text.replace("<CURDEF>USD", "<CURDEF>EUR")
        )
        key, path = "primarySha256", primary
    else:
        text = supporting.read_text()
        if modification == "bad-date":
            text = text.replace("01/10/2024", "bad")
        elif modification == "pending":
            text = text.replace("Posted", "Pending")
        else:
            text = text.replace(",12.34,", ",12.34,12.34")
        supporting.write_text(text)
        key, path = "supportingSha256", supporting
    entry["descriptionEvidence"][key] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        parse_mapped_extract(primary, entry, data_root=tmp_path, account_id="acct-main")


def test_supporting_path_must_stay_in_private_extracts(tmp_path):
    primary, _, entry = paired(tmp_path)
    entry["descriptionEvidence"]["path"] = r"..\outside.csv"
    with pytest.raises(ValueError, match="paths are invalid"):
        parse_mapped_extract(primary, entry, data_root=tmp_path, account_id="acct-main")


def test_normalized_and_shadow_sources_use_the_same_evidence(tmp_path):
    root = make_estate(tmp_path)
    primary, supporting, entry = paired(root)
    write(root / "extracts" / "mapping.json", {"files": [entry]})
    manifest = build(root)
    verify_publication(root)
    assert manifest["sourceStats"]["bankCorroboratedDescriptions"] == 1
    assert any(
        row["sha256"] == entry["descriptionEvidence"]["supportingSha256"]
        for row in manifest["sourceFiles"]
    )
    observations = json.loads(
        (root / "normalized" / "canonical" / "transaction-observations.json").read_text()
    )["observations"]
    assert any(
        row["transaction"]["source_id"] == "extract:stable:SYN-0"
        and row["transaction"]["description"] == DESCRIPTION
        for row in observations
    )
    batches, files = _extract_batches(
        root, datetime(2026, 1, 1, tzinfo=timezone.utc),
        {"acct-main": SimpleNamespace(id="acct-main", display_name="Example Checking")},
    )
    encoded = json.dumps([asdict(batch) for batch in batches], default=str)
    assert DESCRIPTION in encoded
    assert "extract-description-evidence" in encoded
    assert entry["descriptionEvidence"]["supportingSha256"] in encoded
    assert len(files) == 3
    assert primary.is_file() and supporting.is_file()


def test_enriching_one_overlapping_export_cannot_double_the_same_fitid(tmp_path):
    root = make_estate(tmp_path)
    primary, _, entry = paired(root)
    earlier = write(primary.with_name("earlier.ofx"), primary.read_text())
    earlier_entry = {
        "file": str(earlier.relative_to(root / "extracts")),
        "account": entry["account"],
    }
    write(root / "extracts" / "mapping.json", {"files": [earlier_entry, entry]})
    build(root)
    verify_publication(root)
    observations = json.loads(
        (root / "normalized" / "canonical" / "transaction-observations.json").read_text()
    )["observations"]
    same_occurrence = [
        row for row in observations
        if row["transaction"]["source_id"] == "extract:stable:SYN-0"
    ]
    assert len(same_occurrence) == 2
    assert len({row["canonicalTransactionId"] for row in same_occurrence}) == 1
    assert all(row["transaction"]["description"] == DESCRIPTION for row in same_occurrence)
    import csv

    with (root / "normalized" / "canonical" / "transactions.csv").open(newline="") as f:
        published = [
            row for row in csv.DictReader(f)
            if row["source_id"].startswith("extract:stable:SYN-0")
        ]
    assert len(published) == 1
    assert published[0]["source_id"] == "extract:stable:SYN-0"
    assert published[0]["amount"] == "-12.34"


def test_source_correspondence_cannot_cross_account_or_changed_raw_content(tmp_path):
    primary, _, entry = paired(tmp_path)
    other = write(primary.with_name("other.ofx"), primary.read_text())
    changed = write(
        primary.with_name("changed.ofx"), primary.read_text().replace("-12.34", "-56.78")
    )
    other_source_account = write(
        primary.with_name("other-source.ofx"),
        primary.read_text().replace("SYN-PRIVATE", "SYN-OTHER-SOURCE"),
    )
    results = parse_mapped_extracts(
        [
            (primary, entry, "SYN-A"), (other, {}, "SYN-B"), (changed, {}, "SYN-A"),
            (other_source_account, {}, "SYN-A"),
        ],
        data_root=tmp_path,
    )
    assert results[0].extract.transactions[0].description == DESCRIPTION
    assert results[1].extract.transactions[0].description == DESCRIPTION[:27]
    assert results[2].extract.transactions[0].description == DESCRIPTION[:27]
    assert results[3].extract.transactions[0].description == DESCRIPTION[:27]


def test_supporting_csv_cannot_be_imported_a_second_time(tmp_path):
    primary, supporting, entry = paired(tmp_path)
    with pytest.raises(ValueError, match="must not also be imported"):
        parse_mapped_extracts(
            [(primary, entry, "SYN-A"), (supporting, {}, "SYN-A")],
            data_root=tmp_path,
        )


def test_contradictory_expansions_of_the_same_raw_occurrence_fail_closed(tmp_path):
    primary, supporting, entry = paired(tmp_path)
    other = write(primary.with_name("other.ofx"), primary.read_text())
    other_csv = write(
        other.with_suffix(".csv"),
        supporting.read_text().replace("Full Detail", "Different Detail"),
    )
    other_entry = {"descriptionEvidence": {
        **entry["descriptionEvidence"],
        "path": str(other_csv.relative_to(tmp_path / "extracts")),
        "supportingSha256": hashlib.sha256(other_csv.read_bytes()).hexdigest(),
    }}
    with pytest.raises(ValueError, match="contradict"):
        parse_mapped_extracts(
            [(primary, entry, "SYN-A"), (other, other_entry, "SYN-A")],
            data_root=tmp_path,
        )
