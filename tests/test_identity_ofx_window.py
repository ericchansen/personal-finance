"""OFX/QFX statement windows are read from the export, never from its rows.

A coverage interval is a claim that a source *would have* reported anything that
happened in a period.  The transactions a file contains cannot support that
claim: a stable export covering a quiet month legitimately contains nothing, and
inferring the window from row extrema would silently shrink coverage to the
first and last thing that happened to arrive.  These tests pin the only
admissible source of the window -- the statement header the institution wrote --
and pin that an incoherent header is refused rather than repaired.

The two real residuals this was built for are covered end to end: both are
``extract:stable`` rows from immutable ``.qfx`` files whose statement period runs
2024-08-27 through 2026-08-27, disputed against legacy Monarch rows two days
earlier.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from finance_store.identity import (
    DEFAULT_POLICY,
    OfxStatementWindow,
    build_source_authority,
    read_ofx_statement_window,
    read_ofx_statement_windows,
)
from importers.extracts.parsers import parse_ofx
from importers.lineage_review.canonical import declared_source_authority
from importers.lineage_review.model import ReviewError

from tests.test_identity_posting_window import (
    monarch_interval,
    monarch_row,
    qfx_interval,
    qfx_row,
    resolve,
)
from tests.test_identity_source_authority import suppressions, unresolved_codes

# The two real files, by the hashes the operator supplied.
DECEMBER_SHA = "fa913c47cade3cf99d45874b50e374df7d0badea79b8a7c8e4af0d5c8bbbf171"
JULY_SHA = "8a11c935f18d3e149418cf103ef511feb874c730472d79278704b4d07b9261a4"
STATEMENT_START = date(2024, 8, 27)
STATEMENT_END = date(2026, 8, 27)


def ofx_document(
    *,
    start: str = "20240827",
    end: str = "20260827",
    account_id: str = "111122223333",
    transactions: tuple[tuple[str, str, str], ...] = (
        ("20251214", "-42.15", "SOME MERCHANT"),
    ),
    include_start: bool = True,
    include_end: bool = True,
) -> str:
    """A minimally realistic single-account OFX/QFX body."""

    rows = "\n".join(
        f"""<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>{when}120000[0:GMT]
<TRNAMT>{amount}
<FITID>FIT-{when}-{index}
<NAME>{name}
</STMTTRN>"""
        for index, (when, amount, name) in enumerate(transactions)
    )
    header = ""
    if include_start:
        header += f"<DTSTART>{start}\n"
    if include_end:
        header += f"<DTEND>{end}\n"
    return f"""OFXHEADER:100
DATA:OFXSGML

<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<CURDEF>USD
<BANKACCTFROM>
<BANKID>123456789
<ACCTID>{account_id}
<ACCTTYPE>CHECKING
</BANKACCTFROM>
<BANKTRANLIST>
{header}{rows}
</BANKTRANLIST>
<LEDGERBAL>
<BALAMT>1234.56
<DTASOF>20260827120000
</LEDGERBAL>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""


def statement_window(sha: str = DECEMBER_SHA) -> dict[str, object]:
    return {
        "source_sha256": sha,
        "statement_start": STATEMENT_START,
        "statement_end": STATEMENT_END,
    }


def bound_qfx_interval(
    *, sha: str = DECEMBER_SHA, window: object | None = None, **kwargs: object
) -> dict[str, object]:
    """A QFX interval whose boundaries come from a bound statement window."""

    kwargs.setdefault("requested_from", STATEMENT_START.isoformat())
    kwargs.setdefault("requested_through", STATEMENT_END.isoformat())
    kwargs.setdefault("extracted_at", "2026-09-03T00:00:00+00:00")
    kwargs.setdefault("freshness_as_of", "2026-09-03T00:00:00+00:00")
    record = qfx_interval(**kwargs)
    record["source_hashes"] = [sha]
    record.pop("effective_from", None)
    record.pop("effective_through", None)
    if window is not False:
        record["ofx_statement_window"] = (
            statement_window(sha) if window is None else window
        )
    return record


def monarch_over_statement(**kwargs: object) -> dict[str, object]:
    return monarch_interval(
        effective_from=STATEMENT_START.isoformat(),
        effective_through=STATEMENT_END.isoformat(),
        requested_from=STATEMENT_START.isoformat(),
        requested_through=STATEMENT_END.isoformat(),
        extracted_at="2026-09-03T00:00:00+00:00",
        freshness_as_of="2026-09-03T00:00:00+00:00",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Reading the header
# ---------------------------------------------------------------------------


def test_statement_window_comes_from_the_header():
    window = read_ofx_statement_window(ofx_document())
    assert window.statement_start == STATEMENT_START
    assert window.statement_end == STATEMENT_END


def test_window_is_not_the_range_of_the_transactions():
    text = ofx_document(
        transactions=(
            ("20251214", "-42.15", "SOME MERCHANT"),
            ("20251220", "-10.00", "ANOTHER MERCHANT"),
        )
    )
    window = read_ofx_statement_window(text)
    assert (window.statement_start, window.statement_end) == (
        STATEMENT_START,
        STATEMENT_END,
    )
    assert window.statement_start < date(2025, 12, 14)
    assert window.statement_end > date(2025, 12, 20)


def test_window_survives_a_statement_with_no_transactions_at_all():
    window = read_ofx_statement_window(ofx_document(transactions=()))
    assert (window.statement_start, window.statement_end) == (
        STATEMENT_START,
        STATEMENT_END,
    )


def test_header_carries_the_account_it_belongs_to():
    window = read_ofx_statement_window(ofx_document(account_id="999988887777"))
    assert window.account_id == "999988887777"


def test_ofx_timestamps_with_zone_suffixes_are_read():
    text = ofx_document().replace(
        "<DTSTART>20240827", "<DTSTART>20240827000000[0:GMT]"
    )
    assert read_ofx_statement_window(text).statement_start == STATEMENT_START


def test_missing_dtstart_is_refused():
    with pytest.raises(ValueError, match="DTSTART"):
        read_ofx_statement_window(ofx_document(include_start=False))


def test_missing_dtend_is_refused():
    with pytest.raises(ValueError, match="DTEND"):
        read_ofx_statement_window(ofx_document(include_end=False))


def test_unreadable_date_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="unreadable"):
        read_ofx_statement_window(ofx_document(start="2024-08"))


def test_impossible_calendar_date_is_refused():
    with pytest.raises(ValueError, match="invalid"):
        read_ofx_statement_window(ofx_document(start="20240230"))


def test_end_before_start_is_refused_not_repaired():
    """Real exports do emit this.  Repairing it would be a guess."""

    with pytest.raises(ValueError, match="ends before it starts"):
        read_ofx_statement_window(ofx_document(start="20260827", end="20240827"))


def test_a_document_with_no_transaction_list_declares_no_window():
    assert read_ofx_statement_windows("<OFX>\n<SIGNONMSGSRSV1>\n</OFX>") == ()
    with pytest.raises(ValueError, match="declares no statement window"):
        read_ofx_statement_window("<OFX>\n</OFX>")


def test_dtstart_outside_the_transaction_list_is_not_a_statement_window():
    stray = "<OFX>\n<DTSTART>19990101\n<DTEND>19991231\n</OFX>"
    assert read_ofx_statement_windows(stray) == ()


def test_multiple_statements_are_all_read_in_file_order():
    text = ofx_document(account_id="1111") + ofx_document(
        account_id="2222", start="20250101", end="20251231"
    )
    windows = read_ofx_statement_windows(text)
    assert [window.account_id for window in windows] == ["1111", "2222"]
    assert windows[1].statement_start == date(2025, 1, 1)


def test_multiple_statements_without_an_account_are_ambiguous():
    text = ofx_document(account_id="1111") + ofx_document(account_id="2222")
    with pytest.raises(ValueError, match="more than one statement window"):
        read_ofx_statement_window(text)


def test_account_selection_picks_exactly_one_statement():
    text = ofx_document(account_id="1111") + ofx_document(
        account_id="2222", start="20250101", end="20251231"
    )
    window = read_ofx_statement_window(text, account_id="2222")
    assert window.statement_start == date(2025, 1, 1)


def test_account_selection_matches_a_shared_last_four():
    text = ofx_document(account_id="XXXXXXXX4321")
    window = read_ofx_statement_window(text, account_id="111122224321")
    assert window.account_id == "XXXXXXXX4321"


def test_account_selection_that_matches_nothing_is_refused():
    with pytest.raises(ValueError, match="no window for that account"):
        read_ofx_statement_window(ofx_document(account_id="1111"), account_id="9999")


def test_ambiguous_account_selection_is_refused():
    text = ofx_document(account_id="XXXX4321") + ofx_document(account_id="YYYY4321")
    with pytest.raises(ValueError, match="ambiguous account window"):
        read_ofx_statement_window(text, account_id="000000004321")


def test_window_document_is_stable():
    window = OfxStatementWindow(STATEMENT_START, STATEMENT_END, "1111")
    assert window.document() == {
        "statementStart": "2024-08-27",
        "statementEnd": "2026-08-27",
        "accountId": "1111",
    }


def test_window_covers_its_own_boundaries():
    window = OfxStatementWindow(STATEMENT_START, STATEMENT_END)
    assert window.covers(STATEMENT_START)
    assert window.covers(STATEMENT_END)
    assert not window.covers(date(2024, 8, 26))
    assert not window.covers(date(2026, 8, 28))


def test_a_single_day_statement_is_coherent():
    window = OfxStatementWindow(STATEMENT_END, STATEMENT_END)
    assert window.covers(STATEMENT_END)


# ---------------------------------------------------------------------------
# The extract parser surfaces the window
# ---------------------------------------------------------------------------


def test_parser_surfaces_the_declared_window():
    extract = parse_ofx(ofx_document(), source="statement.qfx")
    assert extract.statement_start == STATEMENT_START
    assert extract.statement_end == STATEMENT_END


def test_parser_leaves_the_window_unset_when_the_header_is_incoherent():
    extract = parse_ofx(ofx_document(start="20260827", end="20240827"))
    assert extract.statement_start is None
    assert extract.statement_end is None
    assert extract.transactions, "an unusable window must not lose the rows"


def test_parser_leaves_the_window_unset_when_absent():
    extract = parse_ofx(ofx_document(include_start=False, include_end=False))
    assert (extract.statement_start, extract.statement_end) == (None, None)


# ---------------------------------------------------------------------------
# Binding the window into coverage authority
# ---------------------------------------------------------------------------


def test_bound_window_supplies_the_interval_boundaries():
    interval = build_source_authority([bound_qfx_interval()]).intervals[0]
    assert interval.effective_from == STATEMENT_START
    assert interval.effective_through == STATEMENT_END


def test_bound_window_accepts_a_parsed_window_object():
    record = bound_qfx_interval(
        window=OfxStatementWindow(STATEMENT_START, STATEMENT_END)
    )
    record["ofx_statement_sha256"] = DECEMBER_SHA
    interval = build_source_authority([record]).intervals[0]
    assert interval.effective_through == STATEMENT_END


def test_window_must_bind_a_declared_source_hash():
    record = bound_qfx_interval()
    record["source_hashes"] = ["d" * 64]
    with pytest.raises(ValueError, match="must bind a declared source hash"):
        build_source_authority([record])


def test_window_may_only_bind_an_ofx_family_interval():
    record = bound_qfx_interval()
    record["source_family"] = "monarch"
    with pytest.raises(ValueError, match="only bind an ofx or qfx interval"):
        build_source_authority([record])


def test_ofx_family_is_accepted_too():
    record = bound_qfx_interval()
    record["source_family"] = "ofx"
    interval = build_source_authority([record]).intervals[0]
    assert interval.effective_from == STATEMENT_START


def test_window_conflicting_with_literal_dates_is_refused():
    record = bound_qfx_interval()
    record["effective_through"] = "2025-01-01"
    with pytest.raises(ValueError, match="conflicts with the bound ofx statement"):
        build_source_authority([record])


def test_window_agreeing_with_literal_dates_is_accepted():
    record = bound_qfx_interval()
    record["effective_from"] = STATEMENT_START
    record["effective_through"] = STATEMENT_END
    interval = build_source_authority([record]).intervals[0]
    assert interval.effective_through == STATEMENT_END


def test_window_without_a_hash_is_refused():
    record = bound_qfx_interval(
        window={
            "statement_start": STATEMENT_START,
            "statement_end": STATEMENT_END,
        }
    )
    with pytest.raises(ValueError, match="source_sha256"):
        build_source_authority([record])


def test_window_without_boundaries_is_refused():
    record = bound_qfx_interval(window={"source_sha256": DECEMBER_SHA})
    with pytest.raises(ValueError, match="statement_start"):
        build_source_authority([record])


def test_incoherent_bound_window_is_refused():
    record = bound_qfx_interval(
        window={
            "source_sha256": DECEMBER_SHA,
            "statement_start": STATEMENT_END,
            "statement_end": STATEMENT_START,
        }
    )
    with pytest.raises(ValueError, match="ends before it starts"):
        build_source_authority([record])


def test_a_window_of_the_wrong_shape_is_refused():
    record = bound_qfx_interval(window="2024-08-27/2026-08-27")
    with pytest.raises(ValueError, match="must be a mapping or a parsed window"):
        build_source_authority([record])


def test_a_record_without_a_window_still_requires_explicit_dates():
    record = bound_qfx_interval(window=False)
    with pytest.raises(ValueError, match="must state effective_from explicitly"):
        build_source_authority([record])


def test_binding_a_window_yields_the_same_interval_as_stating_the_dates():
    """The window is a way of *reading* the dates, not a different claim."""

    literal = bound_qfx_interval(window=False)
    literal["effective_from"] = STATEMENT_START.isoformat()
    literal["effective_through"] = STATEMENT_END.isoformat()
    assert (
        build_source_authority([bound_qfx_interval()]).intervals[0].interval_id
        == build_source_authority([literal]).intervals[0].interval_id
    )


# ---------------------------------------------------------------------------
# The two real residuals
# ---------------------------------------------------------------------------

REAL_RESIDUALS = [
    pytest.param(DECEMBER_SHA, "2025-12-14", "2025-12-12", "-42.15", id="december"),
    pytest.param(JULY_SHA, "2025-07-24", "2025-07-22", "-118.40", id="july"),
]


@pytest.mark.parametrize(("sha", "qfx_day", "monarch_day", "amount"), REAL_RESIDUALS)
def test_statement_window_proves_the_real_qfx_over_monarch_residual(
    sha, qfx_day, monarch_day, amount
):
    resolution = resolve(
        [
            qfx_row(day=qfx_day, amount=amount),
            monarch_row(day=monarch_day, amount=amount),
        ],
        [bound_qfx_interval(sha=sha), monarch_over_statement()],
    )
    assert unresolved_codes(resolution) == []
    suppressed = suppressions(resolution)
    assert len(suppressed) == 1
    features = dict(suppressed[0].feature_vector)
    interval = next(
        item.interval
        for item in resolution.interval_authorities
        if item.interval.interval_id == features["authoritativeIntervalId"]
    )
    assert sha in interval.evidence.source_hashes
    assert interval.effective_from == STATEMENT_START
    assert interval.effective_through == STATEMENT_END
    assert len(resolution.canonical_events) == 1


@pytest.mark.parametrize(("sha", "qfx_day", "monarch_day", "amount"), REAL_RESIDUALS)
def test_the_decision_records_the_two_day_source_date_difference(
    sha, qfx_day, monarch_day, amount
):
    resolution = resolve(
        [
            qfx_row(day=qfx_day, amount=amount),
            monarch_row(day=monarch_day, amount=amount),
        ],
        [bound_qfx_interval(sha=sha), monarch_over_statement()],
    )
    decision = suppressions(resolution)[0]
    proof = dict(decision.competing_candidate_proof)
    features = dict(decision.feature_vector)
    assert proof["sourceDayDistanceDays"] == 2
    assert proof["maxPostingDateToleranceDays"] == 2
    assert features["sameSourceDay"] == "false"
    assert features["authoritativeSourceDay"] == qfx_day
    assert features["suppressedSourceDay"] == monarch_day


@pytest.mark.parametrize(("sha", "qfx_day", "monarch_day", "amount"), REAL_RESIDUALS)
def test_a_day_outside_the_statement_window_is_never_suppressed(
    sha, qfx_day, monarch_day, amount
):
    """Shift both rows past DTEND: same pair, no proven coverage, no merge."""

    resolution = resolve(
        [
            qfx_row(day="2026-08-29", amount=amount),
            monarch_row(day="2026-08-27", amount=amount),
        ],
        [bound_qfx_interval(sha=sha), monarch_over_statement()],
    )
    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2


def test_a_gap_wider_than_the_bound_window_is_never_suppressed():
    resolution = resolve(
        [qfx_row(day="2025-12-14"), monarch_row(day="2025-12-11")],
        [bound_qfx_interval(tolerance=2), monarch_over_statement()],
    )
    assert suppressions(resolution) == []
    assert len(resolution.canonical_events) == 2


def test_the_bound_resolution_is_replay_stable_under_input_order():
    records = [bound_qfx_interval(), monarch_over_statement()]
    rows = [qfx_row(day="2025-12-14"), monarch_row(day="2025-12-12")]
    forward = resolve(rows, records)
    reverse = resolve(list(reversed(rows)), records)
    assert forward.generation_hash == reverse.generation_hash


# ---------------------------------------------------------------------------
# The private reader binds the file, not a hand-written date
# ---------------------------------------------------------------------------


def _statement_record(sha: str) -> dict[str, object]:
    record = bound_qfx_interval(window=False)
    record["source_hashes"] = []
    record["ofxStatementFile"] = {
        "path": "extracts/statement.qfx",
        "sourceSha256": sha,
    }
    return record


def _write_authority(root: Path, record: dict[str, object]) -> None:
    (root / "identity").mkdir(parents=True, exist_ok=True)
    (root / "identity" / "source-authority.json").write_text(
        json.dumps({"coverageIntervals": [record]}, default=str), encoding="utf-8"
    )


def _write_statement(root: Path, text: str) -> str:
    path = root / "extracts" / "statement.qfx"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = text.encode("utf-8")
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def test_private_reader_binds_the_window_from_the_file(tmp_path):
    sha = _write_statement(tmp_path, ofx_document())
    _write_authority(tmp_path, _statement_record(sha))
    interval = declared_source_authority(tmp_path).source_authority.intervals[0]
    assert interval.effective_from == STATEMENT_START
    assert interval.effective_through == STATEMENT_END
    assert sha in interval.evidence.source_hashes


def test_private_reader_never_takes_the_window_from_the_rows(tmp_path):
    sha = _write_statement(
        tmp_path,
        ofx_document(transactions=(("20260101", "-1.00", "ONLY ROW"),)),
    )
    _write_authority(tmp_path, _statement_record(sha))
    interval = declared_source_authority(tmp_path).source_authority.intervals[0]
    assert interval.effective_from == STATEMENT_START
    assert interval.effective_through == STATEMENT_END


def test_private_reader_refuses_a_hash_mismatch(tmp_path):
    _write_statement(tmp_path, ofx_document())
    _write_authority(tmp_path, _statement_record("e" * 64))
    with pytest.raises(ReviewError, match="statement-hash-mismatch"):
        declared_source_authority(tmp_path)


def test_private_reader_refuses_a_missing_statement(tmp_path):
    _write_authority(tmp_path, _statement_record("e" * 64))
    with pytest.raises(ReviewError, match="statement-missing"):
        declared_source_authority(tmp_path)


def test_private_reader_refuses_an_incoherent_statement(tmp_path):
    sha = _write_statement(tmp_path, ofx_document(start="20260827", end="20240827"))
    _write_authority(tmp_path, _statement_record(sha))
    with pytest.raises(ReviewError, match="statement-window-invalid"):
        declared_source_authority(tmp_path)


def test_private_reader_refuses_a_path_outside_the_root(tmp_path):
    record = _statement_record("e" * 64)
    record["ofxStatementFile"] = {
        "path": "../escape.qfx",
        "sourceSha256": "e" * 64,
    }
    _write_authority(tmp_path, record)
    with pytest.raises(ReviewError, match="statement-outside-root"):
        declared_source_authority(tmp_path)


def test_private_reader_refuses_an_incomplete_declaration(tmp_path):
    record = _statement_record("e" * 64)
    record["ofxStatementFile"] = {"path": "extracts/statement.qfx"}
    _write_authority(tmp_path, record)
    with pytest.raises(ReviewError, match="statement-incomplete"):
        declared_source_authority(tmp_path)


def test_private_reader_refuses_a_declaration_of_the_wrong_shape(tmp_path):
    record = _statement_record("e" * 64)
    record["ofxStatementFile"] = "extracts/statement.qfx"
    _write_authority(tmp_path, record)
    with pytest.raises(ReviewError, match="statement-invalid"):
        declared_source_authority(tmp_path)


def test_private_reader_without_a_map_is_unchanged(tmp_path):
    assert declared_source_authority(tmp_path) is DEFAULT_POLICY


def test_private_reader_is_deterministic_across_reads(tmp_path):
    sha = _write_statement(tmp_path, ofx_document())
    _write_authority(tmp_path, _statement_record(sha))
    first = declared_source_authority(tmp_path)
    second = declared_source_authority(tmp_path)
    assert first.policy_hash == second.policy_hash
