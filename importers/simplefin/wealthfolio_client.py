"""Minimal client for a local Wealthfolio server.

Authentication is a session cookie (`wf_session`, HttpOnly) obtained from
`POST /api/v1/auth/login`, so a cookie jar is all that is needed.
Standard library only.
"""

from __future__ import annotations

import getpass
import http.cookiejar
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo


class WealthfolioError(RuntimeError):
    def __init__(self, status: int, path: str, body: str):
        super().__init__(f"{status} from {path}: {body[:400]}")
        self.status = status
        self.path = path
        self.body = body


def read_password() -> str:
    """The login password comes from compose.env; a terminal may type it instead."""
    if os.environ.get("WEALTHFOLIO_PASSWORD"):
        return os.environ["WEALTHFOLIO_PASSWORD"]
    if not sys.stdin.isatty():
        raise ValueError("set WEALTHFOLIO_PASSWORD for an unattended run")
    return getpass.getpass("Wealthfolio password: ")


def source_day_timestamp(value: date, zone: tzinfo = timezone.utc) -> str:
    """Keep the source day identical in UTC accounting and the local UI."""
    start = max(
        datetime.combine(value, time.min, timezone.utc),
        datetime.combine(value, time.min, zone).astimezone(timezone.utc),
    )
    end = min(
        datetime.combine(value + timedelta(days=1), time.min, timezone.utc),
        datetime.combine(value + timedelta(days=1), time.min, zone).astimezone(timezone.utc),
    )
    noon = datetime.combine(value, time(12), zone).astimezone(timezone.utc)
    anchor = noon if start <= noon < end else start + (end - start) / 2
    if anchor.date() != value or anchor.astimezone(zone).date() != value:
        raise ValueError("source day cannot be represented in the display timezone")
    return anchor.isoformat().replace("+00:00", "Z")


class WealthfolioClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8088", timeout: int = 120):
        self.base = base_url.rstrip("/")
        host = urllib.parse.urlsplit(self.base).hostname
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise ValueError("Wealthfolio URL must be loopback")
        self.api = f"{self.base}/api/v1"
        self.timeout = timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))

    def _request(self, method: str, path: str, payload: Any = None) -> Any:
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.api + path, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        # The server enforces an explicit CORS origin allowlist.
        req.add_header("Origin", self.base)
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            raise WealthfolioError(exc.code, path, exc.read().decode()) from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, payload: Any) -> Any:
        return self._request("POST", path, payload)

    def put(self, path: str, payload: Any) -> Any:
        return self._request("PUT", path, payload)

    def login(self, password: str) -> None:
        self.post("/auth/login", {"password": password})
        if not any(c.name == "wf_session" for c in self._jar):
            raise WealthfolioError(401, "/auth/login", "no session cookie returned")

    def list_accounts(self) -> list[dict]:
        return self.get("/accounts") or []

    def display_timezone(self) -> ZoneInfo:
        return ZoneInfo(self.get("/settings")["timezone"])

    def update_account(self, account_id: str, **fields: Any) -> dict:
        return self.put(f"/accounts/{account_id}", {"id": account_id, **fields})

    def save_activities(self, creates: list[dict] | None = None, updates: list[dict] | None = None) -> Any:
        return self.post(
            "/activities/bulk",
            {"creates": creates or [], "updates": updates or [], "deleteIds": []},
        )

    def iter_activities(self, page_size: int = 1000):
        """Yield every activity. Paging is 0-based; starting at 1 skips a page."""
        page = 0
        while True:
            result = self.post("/activities/search", {"page": page, "pageSize": page_size})
            rows = result.get("data") or []
            yield from rows
            if len(rows) < page_size:
                return
            page += 1
