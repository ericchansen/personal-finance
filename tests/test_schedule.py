import json
import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from importers.simplefin import schedule

ZONE = ZoneInfo("America/Chicago")
SIX = time(6)


def at(day, hour, minute=0):
    return datetime(2026, 3, day, hour, minute, tzinfo=ZONE)


@pytest.mark.parametrize("now, last, expected", [
    (at(10, 5), at(9, 7), False),        # before today's slot; yesterday ran
    (at(10, 6, 5), at(9, 7), True),      # today's slot has passed
    (at(10, 6, 5), at(10, 6, 1), False),  # already ran today
    (at(10, 5), at(8, 7), True),         # missed yesterday: catch up on wake
    (at(8, 6, 5), at(7, 7), True),       # daylight-saving start day
])
def test_due(now, last, expected):
    assert schedule.due(now, last, SIX) is expected


def write(path, when, text="{}"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (when.timestamp(), when.timestamp()))
    return path


def last_sync(data_dir, when, errors=()):
    return write(data_dir / "simplefin" / "local-last-sync.json", when, json.dumps({"errors": list(errors)}))


def test_healthy_requires_a_recent_error_free_sync(tmp_path):
    now = datetime.now(ZONE)
    assert not schedule.healthy(tmp_path, now)
    last_sync(tmp_path, now)
    assert schedule.healthy(tmp_path, now)
    last_sync(tmp_path, now, errors=["synthetic error"])
    assert not schedule.healthy(tmp_path, now)
    last_sync(tmp_path, now - timedelta(hours=27))
    assert not schedule.healthy(tmp_path, now)


def test_ping_reports_only_the_exit_status_and_never_raises(monkeypatch):
    urls = []

    class Response:
        def close(self):
            pass

    monkeypatch.setattr(schedule.urllib.request, "urlopen", lambda url, timeout: urls.append(url) or Response())
    schedule.ping("https://ping.example.test/check/", 2)
    schedule.ping(None, 0)
    assert urls == ["https://ping.example.test/check/2"]
    monkeypatch.undo()
    schedule.ping("ping.example.test/no-scheme", 1)


@pytest.fixture
def calls(monkeypatch):
    calls = []
    monkeypatch.setattr(schedule, "reachable", lambda: True)
    monkeypatch.setattr(schedule, "ping", lambda url, code: None)
    return calls


def fake_run(monkeypatch, calls, code, effect=lambda: None):
    def run_once(data_dir, extra):
        calls.append(extra)
        effect()
        return code
    monkeypatch.setattr(schedule, "run_once", run_once)


def test_waits_for_a_first_manual_sync(monkeypatch, tmp_path, calls):
    fake_run(monkeypatch, calls, 0)
    schedule.poll(tmp_path, at(10, 6, 5), SIX, None)
    assert calls == []


def test_crash_retries_hourly_and_replays_the_days_response(monkeypatch, tmp_path, calls):
    last_sync(tmp_path, at(9, 7))
    folder = tmp_path / "raw" / "simplefin" / "2026-03-10"

    def fetch_once():
        if len(calls) == 1:
            write(folder / "request-01", at(10, 6, 5), "")
            write(folder / "simplefin-synthetic.json", at(10, 6, 5))

    fake_run(monkeypatch, calls, 1, fetch_once)
    schedule.poll(tmp_path, at(10, 6, 5), SIX, None)
    schedule.poll(tmp_path, at(10, 6, 30), SIX, None)
    schedule.poll(tmp_path, at(10, 7, 10), SIX, None)
    schedule.poll(tmp_path, at(10, 8, 15), SIX, None)
    replay = ["--snapshot", str(folder / "simplefin-synthetic.json")]
    assert calls == [[], replay, replay]


def test_failed_requests_stop_at_half_the_daily_quota(monkeypatch, tmp_path, calls):
    last_sync(tmp_path, at(9, 7))
    folder = tmp_path / "raw" / "simplefin" / "2026-03-10"
    fake_run(monkeypatch, calls, 1, lambda: write(folder / f"request-{len(calls):02d}", at(10, 6), ""))
    for hour in range(6, 19):
        schedule.poll(tmp_path, at(10, hour, 5), SIX, None)
    assert len(calls) == schedule.RETRY_REQUEST_LIMIT == 12


def test_account_errors_wait_for_the_next_day(monkeypatch, tmp_path, calls):
    last_sync(tmp_path, at(9, 7))
    fake_run(monkeypatch, calls, 2, lambda: last_sync(tmp_path, at(10, 6, 5), ["synthetic error"]))
    schedule.poll(tmp_path, at(10, 6, 5), SIX, None)
    schedule.poll(tmp_path, at(10, 8), SIX, None)
    assert calls == [[]]


def test_success_clears_a_previous_crash(monkeypatch, tmp_path, calls):
    last_sync(tmp_path, at(9, 7))
    write(tmp_path / "simplefin" / "local-sync-crashed", at(9, 6), "")
    fake_run(monkeypatch, calls, 0, lambda: last_sync(tmp_path, at(10, 6, 5)))
    schedule.poll(tmp_path, at(10, 6, 5), SIX, None)
    assert not (tmp_path / "simplefin" / "local-sync-crashed").exists()


def test_unreachable_wealthfolio_restarts_before_any_request(monkeypatch, tmp_path, calls):
    last_sync(tmp_path, at(9, 7))
    fake_run(monkeypatch, calls, 0)
    monkeypatch.setattr(schedule, "reachable", lambda: False)
    with pytest.raises(SystemExit):
        schedule.poll(tmp_path, at(10, 6, 5), SIX, None)
    assert calls == []
