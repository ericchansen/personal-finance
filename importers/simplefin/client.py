"""Fetch account data from a SimpleFIN Bridge.

SimpleFIN is a read-only protocol -- the spec defines no write, payment or
transfer endpoints -- which makes it the first source in this project that can
run unattended without handing an application a bank password.

Two credentials exist and they are easy to confuse:

**Setup token** -- one-time use, base64 of a claim URL. POSTing to that URL
exchanges it for an access URL and burns the token.

**Access URL** -- permanent, embeds HTTP Basic credentials, revocable from the
Bridge dashboard. This is a secret: anyone holding it can read every connected
account. It belongs in the data directory, never the repository.

The Bridge caps requests at roughly 24 per day and serves about 90 days of
history; anything older must already be in Wealthfolio.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

CLAIM_TIMEOUT = 60
FETCH_TIMEOUT = 180
MAX_HISTORY_DAYS = 90
DEFAULT_HISTORY_DAYS = 45
MAX_REQUESTS_PER_DAY = 24

# The Bridge sits behind Cloudflare, which rejects urllib's default
# ``Python-urllib/3.x`` User-Agent with error 1010 before the request ever
# reaches SimpleFIN. The failure surfaces as a bare 403 and looks exactly like
# an already-claimed token, which is a costly thing to misdiagnose: a setup
# token is one-time use, so a wrong guess sends you back for a new one.
HEADERS = {"User-Agent": "personal-finance-importer/1.0 (+simplefin client)"}


class SimpleFinError(RuntimeError):
    pass


@dataclass(frozen=True)
class SimpleFinTransaction:
    id: str
    posted: date
    amount: Decimal
    description: str
    pending: bool = False
    memo: str = ""


@dataclass(frozen=True)
class SimpleFinAccount:
    id: str
    name: str
    org: str
    currency: str
    balance: Decimal
    balance_date: date | None
    available_balance: Decimal | None = None
    transactions: list[SimpleFinTransaction] = field(default_factory=list)
    holdings: list[dict] | None = None

    @property
    def is_currency(self) -> bool:
        """False when the account is denominated in a security or crypto.

        SimpleFIN allows ``currency`` to hold a URL identifying a non-fiat
        unit, in which case ``balance`` counts units rather than dollars.
        """
        return "://" not in self.currency


def decode_setup_token(token: str) -> str:
    """Return the claim URL a setup token encodes."""
    cleaned = "".join(token.split())
    if not cleaned:
        raise SimpleFinError("empty setup token")
    try:
        # Bridge tokens are standard base64 but are not always padded.
        padded = cleaned + "=" * (-len(cleaned) % 4)
        url = base64.b64decode(padded).decode("utf-8").strip()
    except Exception as exc:  # noqa: BLE001 - any decode failure means the same thing
        raise SimpleFinError(f"setup token is not valid base64: {exc}") from None
    if not url.startswith("https://"):
        raise SimpleFinError("decoded setup token is not an https URL")
    return url


def claim_access_url(setup_token: str, *, opener=urllib.request.urlopen) -> str:
    """Exchange a setup token for a permanent access URL.

    The token is consumed by this call and cannot be replayed, so the result
    must be persisted immediately or the user has to generate another.
    """
    claim_url = decode_setup_token(setup_token)
    request = _request(claim_url, method="POST", data=b"")
    try:
        with opener(request, timeout=CLAIM_TIMEOUT) as response:
            access_url = response.read().decode("utf-8").strip()
    except Exception as exc:  # noqa: BLE001
        raise SimpleFinError(f"could not claim setup token: {exc}") from None
    if not access_url.startswith("https://"):
        raise SimpleFinError(f"unexpected claim response: {access_url[:80]!r}")
    return access_url


def split_credentials(access_url: str) -> tuple[str, str | None]:
    """Separate an access URL into a plain URL and a Basic auth header value.

    SimpleFIN embeds credentials in the URL itself
    (``https://user:pass@host/path``). urllib cannot fetch that form -- it
    reads the text after the first colon as a port number and fails with
    ``nonnumeric port`` -- so the userinfo is lifted out and sent as an
    ``Authorization`` header instead, which is what the URL form is shorthand
    for anyway.
    """
    parsed = urllib.parse.urlsplit(access_url)
    if "@" not in parsed.netloc:
        return access_url, None
    userinfo, _, host = parsed.netloc.rpartition("@")
    token = base64.b64encode(urllib.parse.unquote(userinfo).encode()).decode()
    clean = urllib.parse.urlunsplit(
        (parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)
    )
    return clean, f"Basic {token}"


def _request(url: str, *, method: str = "GET", data: bytes | None = None):
    """Build a request, moving any embedded credentials into a header."""
    clean, auth = split_credentials(url)
    headers = dict(HEADERS)
    if auth:
        headers["Authorization"] = auth
    return urllib.request.Request(clean, data=data, method=method, headers=headers)


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


def _date(epoch) -> date | None:
    if epoch in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).date()
    except (ValueError, OSError, TypeError):
        return None


def parse_accounts(payload: dict) -> tuple[list[SimpleFinAccount], list[str]]:
    """Convert a ``/accounts`` response into accounts plus connection errors.

    Errors are returned rather than raised: a single failed institution comes
    back alongside working ones, and discarding the good data because one bank
    is down would be wrong. The error list is what tells you an institution
    needs re-authenticating.
    """
    errors = [str(e) for e in (payload.get("errors") or [])]
    accounts: list[SimpleFinAccount] = []
    for raw in payload.get("accounts") or []:
        org = raw.get("org") or {}
        transactions = []
        for txn in raw.get("transactions") or []:
            posted = _date(txn.get("posted"))
            if posted is None:
                continue
            transactions.append(
                SimpleFinTransaction(
                    id=str(txn.get("id") or ""),
                    posted=posted,
                    amount=_decimal(txn.get("amount")),
                    description=str(txn.get("description") or ""),
                    pending=bool(txn.get("pending")),
                    memo=str(txn.get("memo") or ""),
                )
            )
        available = raw.get("available-balance")
        accounts.append(
            SimpleFinAccount(
                id=str(raw.get("id") or ""),
                name=str(raw.get("name") or ""),
                org=str(org.get("name") or org.get("domain") or ""),
                currency=str(raw.get("currency") or "USD"),
                balance=_decimal(raw.get("balance")),
                balance_date=_date(raw.get("balance-date")),
                available_balance=_decimal(available) if available is not None else None,
                transactions=sorted(transactions, key=lambda t: t.posted),
                holdings=raw.get("holdings"),
            )
        )
    return accounts, errors


def build_url(access_url: str, start: date | None = None, end: date | None = None,
              pending: bool = False, balances_only: bool = False) -> str:
    """Compose an ``/accounts`` request.

    Dates are unix seconds. ``start-date`` is inclusive of the day given, so a
    caller wanting "since the last import" should pass the day after it to
    avoid re-fetching a boundary day it already has.
    """
    url = access_url.rstrip("/") + "/accounts"
    params = []
    if start is not None:
        params.append(f"start-date={int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())}")
    if end is not None:
        params.append(f"end-date={int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp())}")
    if pending:
        params.append("pending=1")
    if balances_only:
        # Cheap request: skips transactions entirely, useful for assertions.
        params.append("balances-only=1")
    return url + ("?" + "&".join(params) if params else "")


def access_path(data_dir: Path) -> Path:
    return data_dir / "simplefin" / "access-url.txt"


def read_access_url(data_dir: Path) -> str:
    path = access_path(data_dir)
    if not path.exists():
        raise SimpleFinError(
            f"no access URL at {path}; run: python -m importers.simplefin.cli claim --token <setup-token>"
        )
    return path.read_text(encoding="utf-8").strip()


def load_mapping(data_dir: Path) -> dict[str, dict]:
    """Load the private, source-id keyed account map."""
    path = data_dir / "simplefin" / "account-map.json"
    if not path.exists():
        raise SimpleFinError(f"missing private account mapping: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SimpleFinError(f"could not read private account mapping: {exc}") from None
    accounts = payload.get("accounts")
    if payload.get("version") != 1 or not isinstance(accounts, dict):
        raise SimpleFinError("account-map.json must have version 1 and an accounts object")
    return accounts


def _snapshot_path(data_dir: Path, now: datetime) -> Path:
    folder = data_dir / "raw" / "simplefin" / now.date().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    # Exclusively created slots enforce the Bridge's daily quota even when runs
    # race. A slot stays used after a failed request, which may also cost quota.
    for slot in range(1, MAX_REQUESTS_PER_DAY + 1):
        try:
            os.close(os.open(folder / f"request-{slot:02d}", os.O_WRONLY | os.O_CREAT | os.O_EXCL))
        except FileExistsError:
            continue
        return folder / f"simplefin-{now.strftime('%H%M%S-%f')}.json"
    raise SimpleFinError("daily SimpleFIN request limit reached; no request was made")


def fetch_snapshot(
    data_dir: Path,
    access_url: str,
    *,
    days: int = DEFAULT_HISTORY_DAYS,
    now: datetime | None = None,
    opener=urllib.request.urlopen,
) -> tuple[Path, dict]:
    """Fetch once and keep the unmodified response as a read-only file for replay."""
    if not 1 <= days <= MAX_HISTORY_DAYS:
        raise SimpleFinError(f"days must be between 1 and {MAX_HISTORY_DAYS}")
    now = now or datetime.now(timezone.utc)
    path = _snapshot_path(data_dir, now)
    # The protocol's bounds are inclusive, so 90 days starts 89 days before today.
    url = build_url(access_url, start=now.date() - timedelta(days=days - 1), pending=True)
    try:
        with opener(_request(url), timeout=FETCH_TIMEOUT) as response:
            body = response.read()
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - report transport/JSON uniformly
        raise SimpleFinError(f"SimpleFIN fetch failed: {exc}") from None
    if not isinstance(payload, dict):
        raise SimpleFinError("SimpleFIN response must be a JSON object")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    path.chmod(0o444)
    return path, payload


def fetch(access_url: str, start: date | None = None, end: date | None = None,
          pending: bool = False, balances_only: bool = False,
          *, opener=urllib.request.urlopen) -> tuple[list[SimpleFinAccount], list[str]]:
    """Fetch accounts. Credentials ride in the access URL itself."""
    url = build_url(access_url, start, end, pending, balances_only)
    request = _request(url)
    try:
        with opener(request, timeout=FETCH_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SimpleFinError(f"fetch failed: {exc}") from None
    return parse_accounts(payload)
