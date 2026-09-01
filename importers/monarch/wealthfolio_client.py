"""Minimal Wealthfolio REST client.

Authentication is a session cookie (`wf_session`, HttpOnly) obtained from
`POST /api/v1/auth/login`, so a cookie jar is all that is needed.

Only the endpoints this importer uses are wrapped. Standard library only.
"""

from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class WealthfolioError(RuntimeError):
    def __init__(self, status: int, path: str, body: str):
        super().__init__(f"{status} from {path}: {body[:400]}")
        self.status = status
        self.path = path
        self.body = body


class WealthfolioClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8088", timeout: int = 120):
        self.base = base_url.rstrip("/")
        self.api = f"{self.base}/api/v1"
        self.timeout = timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )

    # -- plumbing ---------------------------------------------------------

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

    def delete(self, path: str, payload: Any = None) -> Any:
        # A few Wealthfolio routes (notably budget group deletion) require a
        # JSON body on DELETE, exactly as its own web client sends them.
        return self._request("DELETE", path, payload)

    # -- api --------------------------------------------------------------

    def login(self, password: str) -> None:
        self.post("/auth/login", {"password": password})
        if not any(c.name == "wf_session" for c in self._jar):
            raise WealthfolioError(401, "/auth/login", "no session cookie returned")

    def health(self) -> bool:
        try:
            return self.get("/healthz") == "ok"
        except WealthfolioError:
            return False

    def auth_status(self) -> dict:
        return self.get("/auth/status")

    def list_accounts(self) -> list[dict]:
        return self.get("/accounts") or []

    def create_account(
        self,
        name: str,
        account_type: str,
        currency: str = "USD",
        group: str | None = None,
        is_active: bool = True,
        is_default: bool = False,
        tracking_mode: str = "TRANSACTIONS",
    ) -> dict:
        payload = {
            "name": name,
            "accountType": account_type,
            "currency": currency,
            "isActive": is_active,
            "isDefault": is_default,
            # Holdings are derived from the imported transaction history.
            # Leaving this NOT_SET makes the app flag the account as unconfigured.
            "trackingMode": tracking_mode,
        }
        if group:
            payload["group"] = group
        return self.post("/accounts", payload)

    def update_account(self, account_id: str, **fields: Any) -> dict:
        return self.put(f"/accounts/{account_id}", {"id": account_id, **fields})

    def save_activities(
        self,
        creates: list[dict] | None = None,
        updates: list[dict] | None = None,
        delete_ids: list[str] | None = None,
    ) -> Any:
        """Bulk mutate activities.

        The endpoint takes a grouped request rather than a bare array; the
        whole batch is validated server-side.
        """
        return self.post(
            "/activities/bulk",
            {
                "creates": creates or [],
                "updates": updates or [],
                "deleteIds": delete_ids or [],
            },
        )

    def search_activities(self, page: int = 0, page_size: int = 1) -> Any:
        return self.post("/activities/search", {"page": page, "pageSize": page_size})

    def iter_activities(
        self, page_size: int = 1000, activity_types: list[str] | None = None
    ):
        """Yield every matching activity.

        Paging is **0-based**. Starting at 1 silently skips the first page,
        which is easy to miss because the call still succeeds and returns a
        plausible number of rows.
        """
        page = 0
        while True:
            body: dict[str, Any] = {"page": page, "pageSize": page_size}
            if activity_types:
                body["activityTypeFilter"] = activity_types
            result = self.post("/activities/search", body)
            rows = result.get("data") or []
            if not rows:
                return
            yield from rows
            if len(rows) < page_size:
                return
            page += 1

    def backup_database(self) -> Any:
        return self.post("/utilities/database/backup", {})

    def list_backups(self) -> list[dict]:
        return self.get("/utilities/database/backups") or []
