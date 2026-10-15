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
from decimal import Decimal
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
def wealthfolio(request):
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
        if request.session.testsfailed:
            logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
            print("\n".join(line for line in (logs.stdout + logs.stderr).splitlines()
                            if " ERROR " in line or " WARN " in line))
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
        }, {
            "id": "cash", "symbol": "", "description": "Cash", "shares": "0",
            "market_value": "200.00", "currency": "USD",
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
    assert next(row for row in client.list_accounts() if row["id"] == ids["CASH"])["trackingMode"] == "TRANSACTIONS"
    security = next(row for row in client.get(f"/holdings?accountId={ids['SECURITIES']}")
                    if row["holdingType"] == "security")
    assert security["instrument"]["pricingMode"] == "MANUAL"
    quotes = client.get(f"/market-data/quotes/history?symbol={security['instrument']['id']}")
    assert quotes and all(quote["dataSource"] == "MANUAL" for quote in quotes)
    second = run()
    assert (second["created"], second["updated"], second["errors"]) == (0, 0, [])
    rows = list(client.iter_activities())
    assert len(rows) == 3
    assert not any((row.get("idempotencyKey") or "").startswith("simplefin-balance:") for row in rows)
    client.put("/spending/settings", {"enabled": True, "accountIds": [ids["CASH"], ids["CREDIT_CARD"]]})
    spending = client.post("/spending/cash-activities/search", {"limit": 1000, "offset": 0})
    assert {row["id"] for row in spending["items"]} == {row["id"] for row in rows}
    report = client.post("/spending/report", {
        "startDate": (now - timedelta(days=3)).isoformat(),
        "endDate": datetime.now(timezone.utc).isoformat(),
    })
    assert report["current"]["outflow"] == 350.0
    native_verify = local_sync.verify_balances
    monkeypatch.setattr(local_sync, "verify_balances",
                        lambda *args: native_verify(*args, attempts=3, sleeper=lambda _: None))
    for balance in ("-125.00", "-175.00"):
        payload["accounts"][1]["balance"] = balance
        snapshot.write_text(json.dumps(payload), encoding="utf-8")
        assert local_sync.main(argv) == 2
        result = json.loads((tmp_path / "simplefin" / "local-last-sync.json").read_text(encoding="utf-8"))
        assert any("Synthetic Credit_Card" in error for error in result["errors"])
        assert (result["created"], result["updated"]) == (0, 0)
        assert {row["id"] for row in client.iter_activities()} == {row["id"] for row in rows}


@pytest.mark.parametrize(("method", "remaining_basis"), [
    ("FIFO", "20"), ("LIFO", "10"), ("HIFO", "10"), ("WAC", "15"),
])
def test_native_cost_basis_methods_calculate_actual_lots(wealthfolio, method, remaining_basis):
    _, client = wealthfolio
    account = client.post("/accounts", {
        "name": f"Synthetic {method}", "accountType": "SECURITIES", "currency": "USD",
        "isActive": True, "isDefault": False, "trackingMode": "TRANSACTIONS",
        "meta": json.dumps({"accounting": {"costBasisMethod": method}}),
    })
    creates = []
    for index, (kind, amount) in enumerate([("DEPOSIT", "30"), ("BUY", "10"), ("BUY", "20"), ("SELL", "30")]):
        row = {
            "accountId": account["id"], "activityType": kind, "amount": amount, "currency": "USD",
            "activityDate": (datetime.now(timezone.utc) - timedelta(days=5 - index)).replace(hour=18).isoformat(),
        }
        if kind != "DEPOSIT":
            row.update(quantity="1", unitPrice=amount, asset={
                "symbol": "SYN-NATIVE-COST", "name": "Synthetic Native Cost Test", "quoteMode": "MANUAL",
            })
        creates.append(row)
    result = client.save_activities(creates=creates)
    assert not result["errors"]
    assert len(result["created"]) == 4
    client.post("/portfolio/recalculate", {"marketSyncMode": {"type": "incremental", "asset_ids": []}})
    for _ in range(30):
        securities = [row for row in client.get(f"/holdings?accountId={account['id']}")
                      if row["holdingType"] == "security" and Decimal(str(row["quantity"])) == 1]
        if securities:
            break
        time.sleep(0.2)
    assert len(securities) == 1
    assert Decimal(str(securities[0]["costBasis"]["local"])) == Decimal(remaining_basis)
    capital = client.save_activities(creates=[{
        "accountId": account["id"], "activityType": "DIVIDEND", "subtype": "RETURN_OF_CAPITAL",
        "asset": {"id": securities[0]["instrument"]["id"]}, "amount": "2", "currency": "USD",
        "activityDate": (datetime.now(timezone.utc) - timedelta(days=1)).replace(hour=18).isoformat(),
    }])
    assert not capital["errors"]
    client.post("/portfolio/recalculate", {"marketSyncMode": {"type": "incremental", "asset_ids": []}})
    expected = Decimal(remaining_basis) - 2
    for _ in range(30):
        holding = next(row for row in client.get(f"/holdings?accountId={account['id']}")
                       if row["holdingType"] == "security")
        if Decimal(str(holding["costBasis"]["local"])) == expected:
            break
        time.sleep(0.2)
    assert Decimal(str(holding["costBasis"]["local"])) == expected
    assert Decimal(str(holding["income"]["local"])) == 0


def test_native_loan_preview_and_valuation_preserve_activity(wealthfolio):
    _, client = wealthfolio
    before = {row["id"] for row in client.iter_activities()}
    setup = {
        "originalAmount": 1200, "originationDate": "2026-01-01", "interestRate": 0,
        "schedule": {"frequency": "monthly", "amortizationMonths": 12},
    }
    preview = client.post("/loans/preview", {"setup": setup})
    assert preview["paymentCount"] == 12
    assert preview["paymentAmount"] == 100
    loan = client.post("/alternative-assets", {
        "kind": "liability", "name": "Synthetic Loan", "currency": "USD",
        "currentValue": "1200", "valueDate": "2026-01-01", "loan": setup,
    })
    client.put(f"/alternative-assets/{loan['assetId']}/valuation", {
        "value": "1150", "date": "2026-01-15", "notes": "Synthetic provider observation",
    })
    native = next(row for row in client.get("/alternative-holdings") if row["id"] == loan["assetId"])
    assert native["kind"] == "liability"
    assert native["loan"] is not None
    assert {row["id"] for row in client.iter_activities()} == before


def test_native_value_only_history_does_not_require_cash_coins_or_purchase_basis(wealthfolio):
    _, client = wealthfolio
    activity_ids = {row["id"] for row in client.iter_activities()}
    account = client.post("/accounts", {
        "name": "Synthetic Legacy Balance Shell", "accountType": "CRYPTOCURRENCY", "currency": "USD",
        "isActive": True, "isDefault": False, "trackingMode": "TRANSACTIONS",
    })
    last = (datetime.now(timezone.utc) - timedelta(days=2)).date()
    days = [last - timedelta(days=2), last - timedelta(days=1), last]
    opening = client.post("/activities", {
        "accountId": account["id"], "activityType": "DEPOSIT", "currency": "USD", "amount": "100",
        "activityDate": local_sync.source_day_timestamp(last, client.display_timezone()),
        "idempotencyKey": "synthetic-invalid-balance-as-deposit",
    })
    for _ in range(30):
        cash = sum((Decimal(str(row["marketValue"]["local"]))
                    for row in client.get(f"/holdings?accountId={account['id']}")), Decimal(0))
        if cash == 100:
            break
        time.sleep(1)
    assert cash == 100
    before = client.get("/net-worth")
    history_path = f"/net-worth/history?startDate={days[0]}&endDate={last}"
    history_before = {row["date"]: row for row in client.get(history_path)}
    accounts_before = client.list_accounts()
    saved = client.post("/alternative-assets", {
        "kind": "other", "name": "Synthetic Historical Account Valuation", "currency": "USD",
        "currentValue": "100", "valueDate": last.isoformat(),
        "metadata": {"valuation_scope": "historical_account_total", "positions_status": "unknown"},
    })
    assert client.list_accounts() == accounts_before
    result = client.post("/market-data/quotes/import", {
        "quotes": [
            {"symbol": saved["assetId"], "date": day.isoformat(), "close": value, "currency": "USD"}
            for day, value in zip(days, ("60", "80", "100"))
        ], "overwriteExisting": True,
    })
    assert len(result) == 3 and all(row["validationStatus"] == "valid" for row in result)
    removed = client.post("/activities/bulk", {
        "creates": [], "updates": [], "deleteIds": [opening["id"]],
    })
    assert not removed["errors"]
    for _ in range(30):
        cash = sum((Decimal(str(row["marketValue"]["local"]))
                    for row in client.get(f"/holdings?accountId={account['id']}")), Decimal(0))
        if cash == 0:
            break
        time.sleep(1)
    assert cash == 0
    full = next(row for row in client.list_accounts() if row["id"] == account["id"])
    client.update_account(account["id"], **{
        key: value for key, value in {**full, "isArchived": True, "isActive": False}.items() if key != "id"
    })
    assert account["id"] not in {row["id"] for row in client.list_accounts()}
    archived = next(row for row in client.get("/accounts?includeArchived=true") if row["id"] == account["id"])
    assert archived["name"] == full["name"] and archived["isArchived"] and not archived["isActive"]
    actual = next(row for row in client.get("/alternative-holdings") if row["id"] == saved["assetId"])
    assert actual["kind"] == "other" and Decimal(actual["marketValue"]) == 100
    assert actual["purchasePrice"] is None and actual["purchaseDate"] is None
    assert actual["unrealizedGain"] is None and actual["unrealizedGainPct"] is None
    assert actual["valuationDate"][:10] == last.isoformat()
    assert {row["id"] for row in client.iter_activities()} == activity_ids
    quotes = client.get(f"/market-data/quotes/history?symbol={saved['assetId']}")
    assert {row["timestamp"][:10]: Decimal(str(row["close"])) for row in quotes} == {
        day.isoformat(): Decimal(value) for day, value in zip(days, ("60", "80", "100"))
    }
    after = client.get("/net-worth")
    assert abs(Decimal(str(after["netWorth"])) - Decimal(str(before["netWorth"]))) < Decimal("0.005")
    cash_before = sum(Decimal(str(row["value"])) for row in before["assets"]["breakdown"] if row["category"] == "cash")
    cash_after = sum(Decimal(str(row["value"])) for row in after["assets"]["breakdown"] if row["category"] == "cash")
    assert abs(cash_before - cash_after - 100) < Decimal("0.005")
    history = client.get(history_path)
    expected_history = {day.isoformat(): Decimal(value) for day, value in zip(days, ("60", "80", "100"))}
    for row in history:
        old = Decimal(str(history_before.get(row["date"], {}).get("alternativeAssetsValue", 0)))
        assert Decimal(str(row["alternativeAssetsValue"])) - old == expected_history[row["date"]]
    assert {row["date"] for row in history} == {day.isoformat() for day in days}


def test_verified_contribution_account_can_use_native_cash_observations(wealthfolio):
    _, client = wealthfolio
    account = client.post("/accounts", {
        "name": "Synthetic Contribution Plan", "accountType": "SECURITIES",
        "currency": "USD", "isActive": True, "isDefault": False,
        "group": "Retirement", "trackingMode": "HOLDINGS",
    })
    client.update_account(
        account["id"], name="Synthetic Plan Contributions", accountType="CASH",
        group="Investments", isActive=True, isDefault=False, trackingMode="HOLDINGS",
    )
    actual = next(row for row in client.list_accounts() if row["id"] == account["id"])
    assert actual["accountType"] == "CASH"
    assert actual["trackingMode"] == "HOLDINGS"
    now = datetime.now(timezone.utc)
    sources, errors = local_sync.read_accounts({"accounts": [{
        "id": "synthetic-contributions", "name": "Synthetic Contribution Balance",
        "currency": "USD", "balance": "75", "balance-date": epoch(now),
        "transactions": [], "holdings": [],
    }]}, balance_zone=client.display_timezone())
    assert errors == []
    mapping = {sources[0].id: {
        "wealthfolioAccountId": account["id"],
        "historyThrough": (now - timedelta(days=30)).date().isoformat(),
    }}
    result = local_sync.sync(client, sources, mapping)
    assert result["errors"] == []
    assert result["created"] == result["updated"] == result["positions"] == 0
    holdings = client.get(f"/holdings?accountId={account['id']}")
    assert not any(row["holdingType"] == "security" for row in holdings)
    assert sum(Decimal(str(row["quantity"])) for row in holdings) == Decimal("75")
    assert not any(row["accountId"] == account["id"] for row in client.iter_activities())
    replay = local_sync.sync(client, sources, mapping)
    assert (replay["created"], replay["updated"], replay["errors"]) == (0, 0, [])


def test_source_card_repayment_uses_native_transfer_semantics(wealthfolio):
    _, client = wealthfolio
    accounts = {
        kind: client.post("/accounts", {
            "name": f"Synthetic Repayment {kind}", "accountType": kind, "currency": "USD",
            "isActive": True, "isDefault": False,
            "trackingMode": "HOLDINGS" if kind == "CASH" else "TRANSACTIONS",
        }) for kind in ("CASH", "CREDIT_CARD")
    }
    now = datetime.now(timezone.utc)
    payload = {"accounts": [
        {"id": "synthetic-repayment-cash", "name": "Synthetic Checking", "currency": "USD",
         "balance": "1000", "balance-date": epoch(now), "transactions": [
             {"id": "payment-out", "posted": epoch(now - timedelta(days=3)), "amount": "-50",
              "description": "Credit card payment to Synthetic Issuer"},
         ]},
        {"id": "synthetic-repayment-card", "name": "Synthetic Card", "currency": "USD",
         "balance": "-100", "balance-date": epoch(now), "transactions": [
             {"id": "purchase", "posted": epoch(now - timedelta(days=4)), "amount": "-150",
              "description": "Synthetic Store"},
             {"id": "payment-in", "posted": epoch(now - timedelta(days=2)), "amount": "50",
              "description": "ONLINE PAYMENT, THANK YOU"},
         ]},
    ]}
    sources, errors = local_sync.read_accounts(payload, balance_zone=client.display_timezone())
    assert errors == []
    boundary = (now - timedelta(days=30)).date().isoformat()
    mapping = {
        source.id: {"action": "import", "wealthfolioAccountId": accounts[kind]["id"],
                    "historyThrough": boundary}
        for source, kind in zip(sources, ("CASH", "CREDIT_CARD"))
    }
    result = local_sync.sync(client, sources, mapping)
    assert result["errors"] == []
    ids = {account["id"] for account in accounts.values()}
    rows = [row for row in client.iter_activities() if row["accountId"] in ids]
    outgoing = next(row for row in rows if row["activityType"] == "TRANSFER_OUT")
    incoming = next(row for row in rows if row["activityType"] == "TRANSFER_IN")
    before_dates = {row["id"]: row["date"] for row in (outgoing, incoming)}
    candidates = client.post("/activities/transfer-match-candidates", {"activityId": outgoing["id"]})
    assert incoming["id"] in {item["activity"]["id"] for item in candidates}
    client.post("/activities/link", {"activityAId": outgoing["id"], "activityBId": incoming["id"]})
    client.put("/spending/settings", {"enabled": True, "accountIds": list(ids)})
    items = client.post("/spending/cash-activities/search", {"limit": 1000, "offset": 0})["items"]
    payments = [row for row in items if row["id"] in before_dates]
    assert len(payments) == 2
    assert all(row["cashFlowBucket"] == "neutral" and row["transferLinkStatus"] == "linked"
               for row in payments)
    linked = {row["id"]: row for row in client.iter_activities() if row["id"] in before_dates}
    assert all(linked[id]["date"] == stamp for id, stamp in before_dates.items())
    assert local_sync.sync(client, sources, mapping)["created"] == 0
    group = linked[incoming["id"]]["sourceGroupId"]
    for correct_both in (False, True):
        payload["accounts"][1]["transactions"][1]["description"] += " corrected memo"
        if correct_both:
            payload["accounts"][0]["transactions"][0]["description"] += " corrected memo"
        corrected, errors = local_sync.read_accounts(payload, balance_zone=client.display_timezone())
        assert errors == []
        result = local_sync.sync(client, corrected, mapping)
        assert result["errors"] == []
        assert result["updated"] == (2 if correct_both else 1)
        actual = {row["id"]: row for row in client.iter_activities() if row["id"] in before_dates}
        assert all(actual[id]["date"] == stamp for id, stamp in before_dates.items())
        assert all(row["sourceGroupId"] == group for row in actual.values())
        assert actual[outgoing["id"]]["comment"] == payload["accounts"][0]["transactions"][0]["description"]
        assert actual[incoming["id"]]["comment"] == payload["accounts"][1]["transactions"][1]["description"]
        assert actual[outgoing["id"]]["amount"] == actual[incoming["id"]]["amount"] == "50"
        replay = local_sync.sync(client, corrected, mapping)
        assert (replay["created"], replay["updated"], replay["errors"]) == (0, 0, [])
    payload["accounts"][1]["transactions"][1]["posted"] = epoch(now - timedelta(days=1))
    corrected, errors = local_sync.read_accounts(payload, balance_zone=client.display_timezone())
    assert errors == []
    result = local_sync.sync(client, corrected, mapping)
    assert result["errors"] == []
    assert result["updated"] == 1
    actual = {row["id"]: row for row in client.iter_activities() if row["id"] in before_dates}
    assert actual[outgoing["id"]]["date"] == before_dates[outgoing["id"]]
    assert actual[incoming["id"]]["date"][:10] == corrected[1].transactions[1].posted.isoformat()
    assert all(row["sourceGroupId"] == group for row in actual.values())
    before_dates[incoming["id"]] = actual[incoming["id"]]["date"]
    payload["accounts"][1]["transactions"][1]["amount"] = "60"
    payload["accounts"][1]["balance"] = "-90"
    corrected, errors = local_sync.read_accounts(payload, balance_zone=client.display_timezone())
    assert errors == []
    result = local_sync.sync(client, corrected, mapping)
    assert result["errors"] == []
    assert result["updated"] == 1
    actual = {row["id"]: row for row in client.iter_activities() if row["id"] in before_dates}
    assert all(actual[id]["date"] == stamp for id, stamp in before_dates.items())
    assert all(not row.get("sourceGroupId") for row in actual.values())
    assert actual[outgoing["id"]]["amount"] == "50"
    assert actual[incoming["id"]]["amount"] == "60"
    assert actual[outgoing["id"]]["comment"] == payload["accounts"][0]["transactions"][0]["description"]
    assert actual[incoming["id"]]["comment"] == payload["accounts"][1]["transactions"][1]["description"]
    replay = local_sync.sync(client, corrected, mapping)
    assert (replay["created"], replay["updated"], replay["errors"]) == (0, 0, [])


def test_native_cashout_matching_and_linking_keep_posting_dates(wealthfolio):
    _, client = wealthfolio
    accounts = [client.post("/accounts", {
        "name": f"Synthetic Transfer {index}", "accountType": "CASH", "currency": "USD",
        "isActive": True, "isDefault": False, "trackingMode": "TRANSACTIONS",
    }) for index in range(2)]
    cashout = local_sync.SimpleFinTransaction(
        "synthetic-cashout", (datetime.now(timezone.utc) - timedelta(days=3)).date(),
        Decimal("25"), "VENMO CASHOUT", False,
    )
    kinds = ("TRANSFER_OUT", local_sync.cash_type(cashout, "CASH"))
    creates = [{
        "accountId": account["id"], "activityType": kind, "amount": "25", "currency": "USD",
        "activityDate": (datetime.now(timezone.utc) - timedelta(days=2 + index)).replace(hour=18).isoformat(),
        "comment": "Synthetic account transfer", "metadata": json.dumps({"flow": {"is_external": True}}),
    } for index, (account, kind) in enumerate(zip(accounts, kinds))]
    result = client.save_activities(creates=creates)
    assert not result["errors"]
    left, right = result["created"]
    candidates = client.post("/activities/transfer-match-candidates", {"activityId": left["id"]})
    assert right["id"] in {item["activity"]["id"] for item in candidates}
    client.post("/activities/link", {"activityAId": left["id"], "activityBId": right["id"]})
    linked = {row["id"]: row for row in client.iter_activities() if row["id"] in {left["id"], right["id"]}}
    assert linked[left["id"]]["sourceGroupId"] == linked[right["id"]]["sourceGroupId"]
    assert linked[left["id"]]["sourceGroupId"]
    for row in (left, right):
        assert datetime.fromisoformat(linked[row["id"]]["date"].replace("Z", "+00:00")) == (
            datetime.fromisoformat(row["activityDate"].replace("Z", "+00:00"))
        )
    client.put("/spending/settings", {"enabled": True, "accountIds": [account["id"] for account in accounts]})
    spending = client.post("/spending/cash-activities/search", {"limit": 1000, "offset": 0})
    assert len(spending["items"]) == 2
    assert all(row["cashFlowBucket"] == "neutral" and row["transferLinkStatus"] == "linked"
               for row in spending["items"])


@pytest.mark.parametrize(("account_type", "activity_type"), [
    ("CASH", "WITHDRAWAL"), ("CASH", "FEE"), ("CREDIT_CARD", "INTEREST"),
])
def test_native_cash_amount_already_includes_fee_tax_attribution(wealthfolio, account_type, activity_type):
    _, client = wealthfolio
    account = client.post("/accounts", {
        "name": f"Synthetic Final Cash {activity_type}", "accountType": account_type, "currency": "USD",
        "isActive": True, "isDefault": False, "trackingMode": "TRANSACTIONS",
    })
    row = client.post("/activities", {
        "accountId": account["id"], "activityType": activity_type, "amount": "10",
        "fee": "2", "tax": "1", "currency": "USD",
        "activityDate": (datetime.now(timezone.utc) - timedelta(days=1)).replace(hour=18).isoformat(),
    })
    client.put("/spending/settings", {"enabled": True, "accountIds": [account["id"]]})
    native = client.post("/spending/cash-activities/search", {"limit": 10, "offset": 0})["items"]
    assert len(native) == 1 and native[0]["id"] == row["id"]
    assert native[0]["netAmount"] == -10
    assert local_sync.activity_amount(
        {"activityType": activity_type, "amount": "10"}, account_type,
    ) == Decimal(str(native[0]["netAmount"]))


def test_native_crypto_transfers_preserve_base_units_without_inventing_cash_or_purchase_cost(wealthfolio):
    _, client = wealthfolio
    account = client.post("/accounts", {
        "name": "Synthetic Wallet Source", "accountType": "SECURITIES", "currency": "USD",
        "isActive": True, "isDefault": False, "trackingMode": "TRANSACTIONS",
    })
    now = datetime.now(timezone.utc).replace(hour=18)
    old = client.save_activities(creates=[{
        "accountId": account["id"], "activityType": "DEPOSIT", "amount": "10", "currency": "USD",
        "activityDate": (now - timedelta(days=1)).isoformat(),
    }, {
        "accountId": account["id"], "activityType": "BUY", "quantity": "0.1",
        "unitPrice": "100", "amount": "10", "currency": "USD",
        "activityDate": (now - timedelta(days=1)).isoformat(),
        "asset": {"symbol": "SYNWALLET-USD", "name": "Synthetic Wallet Coin",
                  "quoteMode": "MANUAL", "quoteCcy": "USD", "instrumentType": "CRYPTO"},
    }])
    assert not old["errors"]
    asset_id = next(row["assetId"] for row in old["created"] if row["activityType"] == "BUY")
    client.put(f"/assets/pricing-mode/{asset_id}", {"quoteMode": "MARKET"})
    client.post("/market-data/quotes/import", {"quotes": [{
        "symbol": asset_id, "date": (now - timedelta(days=3)).date().isoformat(),
        "close": "100", "currency": "USD",
    }], "overwriteExisting": True})
    creates = [{
        "accountId": account["id"], "activityType": kind, "quantity": quantity,
        "subtype": "external_transfer", "status": "POSTED", "needsReview": True,
        "unitPrice": "0", "currency": "USD", "asset": {"id": asset_id},
        "activityDate": (now - timedelta(days=3 - index)).isoformat(),
        "sourceSystem": "LEDGER_LIVE", "sourceRecordId": f"synthetic-operation-{index}",
        "idempotencyKey": f"synthetic-wallet:{index}",
        "metadata": json.dumps({
            "flow": {"is_external": True},
            "walletSource": {"basisStatus": "unknown", "feeSatoshi": index * 7},
        }),
    } for index, (kind, quantity) in enumerate((
        ("TRANSFER_IN", "0.12345678"), ("TRANSFER_OUT", "0.02345670"),
    ))]
    result = client.post("/activities/bulk", {
        "creates": creates, "updates": [], "deleteIds": [row["id"] for row in old["created"]],
    })
    assert not result["errors"] and len(result["created"]) == 2
    client.post("/portfolio/recalculate", {
        "accountIds": [account["id"]],
        "marketSyncMode": {"type": "incremental", "assetIds": []},
    })
    for _ in range(30):
        holdings = client.get(f"/holdings?accountId={account['id']}")
        security = next((row for row in holdings if (row.get("instrument") or {}).get("id") == asset_id), None)
        if security and Decimal(str(security["quantity"])) == Decimal("0.10000008"):
            break
        time.sleep(1)
    assert security and Decimal(str(security["quantity"])) == Decimal("0.10000008"), holdings
    assert Decimal(str(security["costBasis"]["local"])) == 0
    assert security["instrument"]["pricingMode"] == "MARKET"
    assert sum((Decimal(str(row["marketValue"]["local"])) for row in holdings
                if row["holdingType"] == "cash"), Decimal(0)) == 0
    actual = [row for row in client.iter_activities() if row["accountId"] == account["id"]]
    assert len(actual) == 2 and {row["activityType"] for row in actual} == {"TRANSFER_IN", "TRANSFER_OUT"}
    assert all(row["amount"] is None and row["sourceSystem"] == "LEDGER_LIVE" and row["needsReview"]
               for row in actual)
    assert {row["idempotencyKey"] for row in actual} == {row["idempotencyKey"] for row in creates}
    assert all(Decimal(str(quote["close"])) > 0
               for quote in client.get(f"/market-data/quotes/history?symbol={asset_id}"))


def test_native_observed_coin_quantity_keeps_market_pricing_without_invented_transfer(wealthfolio):
    _, client = wealthfolio
    account = client.post("/accounts", {
        "name": "Synthetic Observed Coin", "accountType": "CRYPTOCURRENCY", "currency": "USD",
        "isActive": True, "isDefault": False, "trackingMode": "TRANSACTIONS",
    })
    observed = (datetime.now(timezone.utc) - timedelta(days=10)).date()
    today = datetime.now(timezone.utc).date()
    quantity = "0.123456789012"
    row = client.post("/activities", {
        "accountId": account["id"], "activityType": "TRANSFER_IN", "quantity": quantity,
        "unitPrice": "0", "currency": "USD", "subtype": "external_transfer", "needsReview": True,
        "activityDate": local_sync.source_day_timestamp(observed, client.display_timezone()),
        "asset": {"symbol": "SYNOBS-USD", "name": "Synthetic Observed Coin",
                  "quoteMode": "MARKET", "quoteCcy": "USD", "instrumentType": "CRYPTO"},
        "idempotencyKey": "synthetic-unverified-observation-as-transfer",
    })
    asset_id = row["assetId"]
    imported = client.post("/market-data/quotes/import", {
        "quotes": [
            {"symbol": asset_id, "date": day.isoformat(), "close": price, "currency": "USD"}
            for day, price in ((observed, "10"), (today, "20"))
        ], "overwriteExisting": True,
    })
    assert all(item["validationStatus"] == "valid" for item in imported)
    quotes_before = client.get(f"/market-data/quotes/history?symbol={asset_id}")
    removed = client.post("/activities/bulk", {"creates": [], "updates": [], "deleteIds": [row["id"]]})
    assert not removed["errors"]
    for _ in range(30):
        if not client.get(f"/holdings?accountId={account['id']}"):
            break
        time.sleep(1)
    assert not client.get(f"/holdings?accountId={account['id']}")
    snapshot = {
        "accountId": account["id"], "snapshotDate": observed.isoformat(), "cashBalances": {"USD": "0"},
        "holdings": [{"assetId": asset_id, "symbol": "SYNOBS-USD", "quantity": quantity, "currency": "USD"}],
    }
    local_sync.use_holdings(client, account, snapshot, [])
    holding = next(item for item in client.get(f"/holdings?accountId={account['id']}")
                   if item["holdingType"] == "security")
    assert Decimal(str(holding["quantity"])) == Decimal(quantity)
    assert holding["instrument"]["id"] == asset_id
    assert holding["instrument"]["pricingMode"] == "MARKET"
    assert holding["instrument"]["instrumentType"] == "CRYPTO"
    assert Decimal(str(holding["price"])) == 20
    assert abs(Decimal(str(holding["marketValue"]["local"])) - Decimal(quantity) * 20) < Decimal("0.0000000001")
    assert holding["costBasis"]["local"] == 0
    assert client.get(f"/market-data/quotes/history?symbol={asset_id}") == quotes_before
    assert local_sync.has_source_snapshot(client, snapshot)
    assert not [item for item in client.iter_activities() if item["accountId"] == account["id"]]


def test_native_observed_cash_history_does_not_create_opening_income(wealthfolio):
    _, client = wealthfolio
    account = client.post("/accounts", {
        "name": "Synthetic Observed Wallet", "accountType": "CASH", "currency": "USD",
        "isActive": True, "isDefault": False, "trackingMode": "TRANSACTIONS",
    })
    now = datetime.now(timezone.utc).replace(hour=18)
    saved = client.save_activities(creates=[{
        "accountId": account["id"], "activityType": "DEPOSIT", "amount": "1000", "currency": "USD",
        "activityDate": (now - timedelta(days=3)).isoformat(),
    }, {
        "accountId": account["id"], "activityType": "WITHDRAWAL", "amount": "25", "currency": "USD",
        "activityDate": (now - timedelta(days=1)).isoformat(), "comment": "Synthetic real payment",
    }])
    assert not saved["errors"]
    opening = next(row for row in saved["created"] if row["activityType"] == "DEPOSIT")
    payment = next(row for row in saved["created"] if row["activityType"] == "WITHDRAWAL")
    removed = client.post("/activities/bulk", {"creates": [], "updates": [], "deleteIds": [opening["id"]]})
    assert not removed["errors"]
    observed = [
        {"accountId": account["id"], "snapshotDate": (now - timedelta(days=days)).date().isoformat(),
         "holdings": [], "cashBalances": {"USD": value}}
        for days, value in ((2, "7"), (1, "9"))
    ]
    local_sync.use_holdings(client, account, observed[0], [])
    local_sync.publish_holdings(client, observed[1], [])
    client.post("/portfolio/recalculate", {
        "accountIds": [account["id"]], "marketSyncMode": {"type": "incremental", "assetIds": []},
    })
    for _ in range(30):
        holdings = client.get(f"/holdings?accountId={account['id']}")
        value = sum((Decimal(str(row["marketValue"]["local"])) for row in holdings), Decimal(0))
        if value == Decimal("9"):
            break
        time.sleep(1)
    assert value == Decimal("9")
    snapshots = client.get(f"/snapshots?accountId={account['id']}")
    by_day = {row["snapshotDate"]: row for row in snapshots}
    for observation in observed:
        assert Decimal(by_day[observation["snapshotDate"]]["cashTotalAccountCurrency"]) == Decimal(
            observation["cashBalances"]["USD"])
    assert all(Decimal(row["cashTotalAccountCurrency"]) < Decimal("1000") for row in snapshots)
    actual = [row for row in client.iter_activities() if row["accountId"] == account["id"]]
    assert len(actual) == 1 and actual[0]["id"] == payment["id"] and actual[0]["amount"] == "25"
    client.put("/spending/settings", {"enabled": True, "accountIds": [account["id"]]})
    report = client.post("/spending/report", {
        "startDate": (now - timedelta(days=5)).isoformat(), "endDate": (now + timedelta(days=1)).isoformat(),
    })
    assert report["current"]["income"] == 0 and report["current"]["outflow"] == 25


def test_native_money_market_classification_preserves_securities_and_bank_cash(wealthfolio):
    _, client = wealthfolio
    account = client.post("/accounts", {
        "name": "Synthetic Fund Classification", "accountType": "SECURITIES", "currency": "USD",
        "isActive": True, "isDefault": False, "trackingMode": "HOLDINGS",
    })
    sources, _ = local_sync.read_accounts({"accounts": [{
        "id": "synthetic-fund-classification", "name": "Synthetic Fund", "currency": "USD",
        "balance": "100", "balance-date": epoch(datetime.now(timezone.utc)), "holdings": [{
            "id": "synthetic-fund", "symbol": "SYN-LIQUID", "description": "Synthetic Money Market Fund",
            "shares": "90", "market_value": "90", "currency": "USD",
        }, {
            "id": "synthetic-cash", "symbol": "", "description": "Cash",
            "shares": "0", "market_value": "10", "currency": "USD",
        }],
    }]}, balance_zone=client.display_timezone())
    mapping = {sources[0].id: {"wealthfolioAccountId": account["id"]}}
    activity_ids = {row["id"] for row in client.iter_activities()}
    assert local_sync.sync(client, sources, mapping)["errors"] == []
    before = client.get(f"/holdings?accountId={account['id']}")
    fund = next(row for row in before if row["holdingType"] == "security")
    asset_id = fund["instrument"]["id"]
    assert fund["instrument"]["classifications"]["assetType"]["id"] == "STOCK_COMMON"
    profile = client.get(f"/assets/profile?assetId={asset_id}")
    for taxonomy, category in (("instrument_type", "FUND_MUTUAL"), ("asset_classes", "CASH_MONEY_MARKET")):
        result = client.put(f"/taxonomies/assignments/asset/{asset_id}/taxonomy/{taxonomy}", [{
            "assetId": asset_id, "taxonomyId": taxonomy, "categoryId": category,
            "weight": 10000, "source": "MANUAL",
        }])
        assert len(result) == 1 and result[0]["categoryId"] == category
    for check in (False, True):
        result = local_sync.sync(client, sources, mapping, check=check)
        assert result["errors"] == [] and result["created"] == result["updated"] == 0
        holdings = client.get(f"/holdings?accountId={account['id']}")
        fund = next(row for row in holdings if row["holdingType"] == "security")
        classes = fund["instrument"]["classifications"]
        assert classes["assetType"]["id"] == "FUND_MUTUAL"
        assert [(row["category"]["id"], row["topLevelCategory"]["id"], row["weight"])
                for row in classes["assetClasses"]] == [("CASH_MONEY_MARKET", "CASH", 100.0)]
        for old in before:
            actual = next(row for row in holdings if row["id"] == old["id"])
            for field in ("holdingType", "quantity", "marketValue", "costBasis", "price", "localCurrency"):
                assert actual[field] == old[field]
        assert sum(Decimal(str(row["marketValue"]["local"])) for row in holdings
                   if row["holdingType"] == "cash") == 10
        allocation = client.get(f"/allocations?accountId={account['id']}")
        assert allocation["totalValue"] == 100
        assert [(row["categoryId"], row["value"]) for row in allocation["assetClasses"]["categories"]] == [
            ("CASH", 100.0),
        ]
        assert next(row for row in allocation["securityTypes"]["categories"]
                    if row["categoryId"] == "FUND")["value"] == 90
        updated_profile = client.get(f"/assets/profile?assetId={asset_id}")
        assert {key: value for key, value in updated_profile.items() if key != "updatedAt"} == {
            key: value for key, value in profile.items() if key != "updatedAt"
        }
        assert {row["id"] for row in client.iter_activities()} == activity_ids


@pytest.mark.parametrize(("instrument_category", "clear_allocation"), [
    ("FUND", True), ("DEPOSITARY_RECEIPT", False),
])
def test_native_wrapper_types_and_unclassified_exposure_survive_replay(
    wealthfolio, instrument_category, clear_allocation,
):
    _, client = wealthfolio
    account = client.post("/accounts", {
        "name": f"Synthetic Wrapper {instrument_category}", "accountType": "SECURITIES",
        "currency": "USD", "isActive": True, "isDefault": False, "trackingMode": "HOLDINGS",
    })
    sources, _ = local_sync.read_accounts({"accounts": [{
        "id": f"synthetic-wrapper-{instrument_category}", "name": "Synthetic Wrapper", "currency": "USD",
        "balance": "100", "balance-date": epoch(datetime.now(timezone.utc)), "holdings": [{
            "id": "synthetic-wrapper-position", "symbol": "SYN-WRAPPER",
            "description": "Synthetic Pooled Investment", "shares": "2",
            "market_value": "100", "currency": "USD",
        }],
    }]}, balance_zone=client.display_timezone())
    mapping = {sources[0].id: {"wealthfolioAccountId": account["id"]}}
    assert local_sync.sync(client, sources, mapping)["errors"] == []
    before = next(row for row in client.get(f"/holdings?accountId={account['id']}")
                  if row["holdingType"] == "security")
    asset_id = before["instrument"]["id"]
    client.put(f"/taxonomies/assignments/asset/{asset_id}/taxonomy/instrument_type", [{
        "assetId": asset_id, "taxonomyId": "instrument_type", "categoryId": instrument_category,
        "weight": 10000, "source": "MANUAL",
    }])
    if clear_allocation:
        client.put(f"/taxonomies/assignments/asset/{asset_id}/taxonomy/asset_classes", [])
    for check in (False, True):
        result = local_sync.sync(client, sources, mapping, check=check)
        assert result["errors"] == [] and result["created"] == result["updated"] == 0
        actual = next(row for row in client.get(f"/holdings?accountId={account['id']}")
                      if row["holdingType"] == "security")
        assert actual["instrument"]["classifications"]["assetType"]["id"] == instrument_category
        for field in ("holdingType", "quantity", "marketValue", "costBasis", "price", "localCurrency"):
            assert actual[field] == before[field]
        if clear_allocation:
            assert actual["instrument"]["classifications"]["assetClasses"] == []
            allocation = client.get(f"/allocations?accountId={account['id']}")
            assert [(row["categoryId"], row["value"]) for row in allocation["assetClasses"]["categories"]] == [
                ("__UNKNOWN__", 100.0),
            ]
        else:
            assert actual["instrument"]["classifications"]["assetClasses"] == (
                before["instrument"]["classifications"]["assetClasses"]
            )


def test_native_missing_basis_warns_without_inventing_acquisition_cost(wealthfolio):
    _, client = wealthfolio
    account = client.post("/accounts", {
        "name": "Synthetic Unknown Basis", "accountType": "SECURITIES", "currency": "USD",
        "isActive": True, "isDefault": False, "trackingMode": "HOLDINGS",
    })
    sources, _ = local_sync.read_accounts({"accounts": [{
        "id": "synthetic-unknown-basis", "name": "Synthetic Unknown Basis", "currency": "USD",
        "balance": "100", "balance-date": epoch(datetime.now(timezone.utc)),
        "holdings": [{
            "id": "synthetic-position", "symbol": "SYN-UNKNOWN", "description": "Synthetic Fund",
            "shares": "2", "market_value": "100", "cost_basis": "0", "purchase_price": "0",
            "currency": "USD",
        }],
    }]}, balance_zone=client.display_timezone())
    mapping = {sources[0].id: {"wealthfolioAccountId": account["id"]}}
    for check in (False, True):
        result = local_sync.sync(client, sources, mapping, check=check)
        assert result["errors"] == []
        assert result["created"] == result["updated"] == 0
        assert any("acquisition cost unavailable for 1 position(s)" in warning
                   and "gains are unverified" in warning for warning in result["warnings"])
        holding = next(row for row in client.get(f"/holdings?accountId={account['id']}")
                       if row["holdingType"] == "security")
        assert holding["quantity"] == 2 and holding["marketValue"]["local"] == 100
        assert holding["costBasis"]["local"] == 0
        assert not [row for row in client.iter_activities() if row["accountId"] == account["id"]]


def test_calculated_snapshot_cannot_silently_replace_a_source_observation(wealthfolio):
    _, client = wealthfolio
    account = client.post("/accounts", {
        "name": "Synthetic Snapshot Ownership", "accountType": "CASH", "currency": "USD",
        "isActive": True, "isDefault": False, "trackingMode": "TRANSACTIONS",
    })
    moment = (datetime.now(timezone.utc) - timedelta(days=1)).replace(hour=18)
    activity = client.post("/activities", {
        "accountId": account["id"], "activityType": "DEPOSIT", "amount": "9", "currency": "USD",
        "activityDate": moment.isoformat(),
    })
    client.post("/portfolio/recalculate", {
        "accountIds": [account["id"]], "marketSyncMode": {"type": "incremental", "assetIds": []},
    })
    for _ in range(30):
        snapshots = client.get(f"/snapshots?accountId={account['id']}")
        if any(row["snapshotDate"] == moment.date().isoformat()
               and row["source"] == "CALCULATED" for row in snapshots):
            break
        time.sleep(1)
    assert any(row["snapshotDate"] == moment.date().isoformat() and row["source"] == "CALCULATED"
               for row in snapshots)
    observation = {
        "accountId": account["id"], "snapshotDate": moment.date().isoformat(),
        "holdings": [], "cashBalances": {"USD": "9"},
    }
    try:
        local_sync.use_holdings(client, account, observation, [])
    except local_sync.SnapshotObservationError:
        actual = next(row for row in client.list_accounts() if row["id"] == account["id"])
        assert actual["trackingMode"] == "TRANSACTIONS"
    else:
        assert local_sync.has_source_snapshot(client, observation)
    holdings = client.get(f"/holdings?accountId={account['id']}")
    assert sum((Decimal(str(row["marketValue"]["local"])) for row in holdings), Decimal(0)) == 9
    actual = [row for row in client.iter_activities() if row["accountId"] == account["id"]]
    assert len(actual) == 1 and actual[0]["id"] == activity["id"] and actual[0]["amount"] == "9"
