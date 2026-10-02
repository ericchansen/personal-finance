"""Run the sync against the Wealthfolio image pinned in compose.yml.

Opt in with WEALTHFOLIO_IT=1; needs Docker and argon2-cffi. All data is synthetic.
Run it before changing the pinned image: it proves the sync still works end to end.
"""

import base64
import json
import os
import re
import secrets
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from importers.simplefin import local_sync
from importers.simplefin.wealthfolio_client import WealthfolioClient

pytestmark = pytest.mark.skipif(not os.environ.get("WEALTHFOLIO_IT"), reason="set WEALTHFOLIO_IT=1")

PASSWORD = "synthetic-password"
COMPOSE = Path(__file__).parents[1] / "compose.yml"


def pinned_image() -> str:
    return re.search(r"WF_IMAGE:-([^}\"]+)", COMPOSE.read_text(encoding="utf-8")).group(1)


@pytest.fixture(scope="module")
def wealthfolio():
    hasher = pytest.importorskip("argon2").PasswordHasher()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    container = subprocess.run([
        "docker", "run", "-d", "--rm", "-p", f"127.0.0.1:{port}:8088",
        "-e", "WF_LISTEN_ADDR=0.0.0.0:8088", "-e", "WF_DB_PATH=/data/wealthfolio.db",
        "-e", f"WF_SECRET_KEY={base64.b64encode(secrets.token_bytes(32)).decode()}",
        "-e", f"WF_AUTH_PASSWORD_HASH={hasher.hash(PASSWORD)}",
        "-e", f"WF_CORS_ALLOW_ORIGINS={url}", pinned_image(),
    ], capture_output=True, text=True, check=True).stdout.strip()
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"{url}/api/v1/healthz", timeout=2).close()
                break
            except OSError:
                time.sleep(1)
        client = WealthfolioClient(url)
        client.login(PASSWORD)
        client.put("/settings", {"baseCurrency": "USD", "timezone": "America/New_York", "onboardingCompleted": True})
        yield url, client
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


def epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def test_sync_imports_and_replays_against_real_wealthfolio(wealthfolio, tmp_path, monkeypatch):
    url, client = wealthfolio
    ids = {
        kind: client.post("/accounts", {
            "name": f"Synthetic {kind.title()}", "accountType": kind, "currency": "USD",
            "isActive": True, "isDefault": False, "trackingMode": "TRANSACTIONS",
        })["id"]
        for kind in ("CASH", "CREDIT_CARD", "SECURITIES")
    }
    now = datetime.now(timezone.utc)
    posted = epoch(now - timedelta(days=1))

    def source(id, balance, transactions=(), holdings=None):
        row = {"id": id, "name": id, "org": {"name": "Synthetic Bank"}, "currency": "USD",
               "balance": balance, "balance-date": epoch(now), "transactions": list(transactions)}
        if holdings is not None:
            row["holdings"] = holdings
        return row

    payload = {"errors": [], "accounts": [
        source("src-checking", "1000.00", [
            {"id": "c1", "posted": posted, "amount": "1200.00", "description": "Synthetic Payroll"},
            {"id": "c2", "posted": posted, "amount": "-200.00", "description": "Synthetic Grocer"},
        ]),
        source("src-card", "-150.00", [
            {"id": "k1", "posted": posted, "amount": "-150.00", "description": "Synthetic Cafe"},
        ]),
        source("src-brokerage", "5000.00", holdings=[{
            "id": "h1", "symbol": "SYNTH", "description": "Synthetic Index Fund",
            "shares": "10", "market_value": "4800.00", "currency": "USD", "purchase_price": "400.00",
        }]),
    ]}
    boundary = (now - timedelta(days=30)).date().isoformat()
    mapping = {"version": 1, "accounts": {
        "src-checking": {"action": "import", "wealthfolioAccountId": ids["CASH"], "historyThrough": boundary},
        "src-card": {"action": "import", "wealthfolioAccountId": ids["CREDIT_CARD"], "historyThrough": boundary},
        "src-brokerage": {"action": "import", "wealthfolioAccountId": ids["SECURITIES"]},
    }}
    (tmp_path / "simplefin").mkdir()
    (tmp_path / "simplefin" / "account-map.json").write_text(json.dumps(mapping), encoding="utf-8")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("WEALTHFOLIO_PASSWORD", PASSWORD)
    argv = ["--data-dir", str(tmp_path), "--base-url", url, "--snapshot", str(snapshot)]

    def run():
        assert local_sync.main(argv) == 0
        return json.loads((tmp_path / "simplefin" / "local-last-sync.json").read_text(encoding="utf-8"))

    first = run()
    assert first["errors"] == []
    assert (first["created"], first["positions"]) == (3, 1)
    # wealthfolioBalance is read back from the app after its own recalculation.
    assert {row["name"]: float(row["wealthfolioBalance"]) for row in first["accounts"]} == {
        "Synthetic Cash": 1000.0, "Synthetic Credit_Card": -150.0, "Synthetic Securities": 5000.0,
    }
    second = run()
    assert (second["created"], second["updated"], second["errors"]) == (0, 0, [])
