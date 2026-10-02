"""Run the local sync once a day inside the compose stack.

The scheduler starts only after a first manual sync, so a new mapping is always
previewed before the app is written. Then the sync runs at the first poll after
SYNC_AT (in TZ) once per day, so a sleeping or rebooted host catches up when it
wakes. A crash retries hourly, replaying the day's saved SimpleFIN response
rather than requesting another, and never uses more than half the daily quota.
A run that finishes with account errors waits for the next day. State lives in
files, so a container restart keeps these limits. SYNC_PING_URL, when set,
receives a healthchecks.io-style ping with only the exit status, so a missing
ping alerts even when the host is off.
"""

from __future__ import annotations

import json
import os
import sys
import time as clock
import urllib.request
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from importers.simplefin import local_sync
from importers.simplefin.client import MAX_REQUESTS_PER_DAY

BASE_URL = "http://127.0.0.1:8088"
POLL_SECONDS = 300
RETRY = timedelta(hours=1)
RETRY_REQUEST_LIMIT = MAX_REQUESTS_PER_DAY // 2
STALE = timedelta(hours=26)


def mtime(path: Path, zone) -> datetime | None:
    return datetime.fromtimestamp(path.stat().st_mtime, zone) if path.exists() else None


def due(now: datetime, last: datetime | None, at: time) -> bool:
    start = datetime.combine(now.date(), at, now.tzinfo)
    if now < start:
        start -= timedelta(days=1)
    return last is None or last < start


def healthy(data_dir: Path, now: datetime) -> bool:
    """A recent, error-free sync; used as the container healthcheck."""
    path = data_dir / "simplefin" / "local-last-sync.json"
    if not path.exists() or now.timestamp() - path.stat().st_mtime > STALE.total_seconds():
        return False
    return not json.loads(path.read_text(encoding="utf-8")).get("errors")


def todays_requests(data_dir: Path, now: datetime) -> tuple[list[Path], int]:
    """Today's saved responses, oldest first, and the request slots used today."""
    folder = data_dir / "raw" / "simplefin" / now.astimezone(timezone.utc).date().isoformat()
    responses = sorted(folder.glob("simplefin-*.json"), key=lambda path: path.stat().st_mtime)
    return responses, len(list(folder.glob("request-??")))


def ping(url: str | None, code: int) -> None:
    if not url:
        return
    try:
        urllib.request.urlopen(f"{url.rstrip('/')}/{code}", timeout=10).close()
    except Exception as exc:  # noqa: BLE001 - an alert must never stop the scheduler
        print(f"ping failed: {type(exc).__name__}", file=sys.stderr)


def reachable() -> bool:
    try:
        urllib.request.urlopen(f"{BASE_URL}/api/v1/healthz", timeout=10).close()
        return True
    except OSError:
        return False


def run_once(data_dir: Path, extra: list[str]) -> int:
    try:
        return local_sync.main(["--data-dir", str(data_dir), "--base-url", BASE_URL, *extra])
    except Exception as exc:  # noqa: BLE001 - the scheduler must survive any failure
        print(f"sync failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def poll(data_dir: Path, now: datetime, at: time, url: str | None) -> None:
    last = mtime(data_dir / "simplefin" / "local-last-sync.json", now.tzinfo)
    crash_marker = data_dir / "simplefin" / "local-sync-crashed"
    crashed = mtime(crash_marker, now.tzinfo)
    if last is None or not due(now, last, at) or (crashed and now - crashed < RETRY):
        return
    extra = []
    if crashed:
        responses, used = todays_requests(data_dir, now)
        if responses and responses[-1].stat().st_mtime > last.timestamp():
            extra = ["--snapshot", str(responses[-1])]
        elif used >= RETRY_REQUEST_LIMIT:
            return
    if not reachable():
        # This container shares Wealthfolio's network namespace. If Wealthfolio
        # restarted, exiting lets Docker restart this container and rejoin it.
        print("Wealthfolio is unreachable; restarting", file=sys.stderr)
        raise SystemExit(1)
    code = run_once(data_dir, extra)
    if code == 1:
        crash_marker.touch()
        os.utime(crash_marker, (now.timestamp(), now.timestamp()))
    else:
        crash_marker.unlink(missing_ok=True)
    ping(url, code)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    data_dir = Path(os.environ.get("FINANCE_DATA", "/finance"))
    zone = ZoneInfo(os.environ.get("TZ") or "UTC")
    if argv == ["--check"]:
        return 0 if healthy(data_dir, datetime.now(zone)) else 1
    at = time.fromisoformat(os.environ.get("SYNC_AT") or "06:00")
    url = os.environ.get("SYNC_PING_URL")
    while True:
        poll(data_dir, datetime.now(zone), at, url)
        clock.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
