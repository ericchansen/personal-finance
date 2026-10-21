"""Spender labeling against an in-memory Wealthfolio. All data is synthetic."""

import copy
import json
import re
import os
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import labeling
from labeling import model as nb
from labeling.__main__ import setup
from labeling.features import features, merchant_key, merchant_pattern
from labeling.ledger import Ledger
from labeling.serve import Labeler, make_server
from labeling.workspace import Locked, Workspace, write_lock

GROUPS = [{"id": "g-needs", "key": "needs"}, {"id": "g-wants", "key": "wants"}]
CATEGORIES = [
    {"id": "cat_food", "name": "Food & Dining", "parentId": None},
    {"id": "cat_food_coffee", "name": "Coffee", "parentId": "cat_food"},
    {"id": "cat_groceries", "name": "Groceries", "parentId": None},
]
ASSIGNMENTS = [{"groupId": "g-wants", "categoryId": "cat_food"}, {"groupId": "g-needs", "categoryId": "cat_groceries"}]
ACCOUNTS = [{"id": f"acct-{name}", "name": f"Synthetic {name}"} for name in ("ann", "ben", "shared", "checking", "card")]


class FakeWealthfolio:
    def __init__(self):
        self.rows, self.event_types, self.events, self.rules, self.backups = {}, [], [], [], 0


    def add(self, row_id, account, notes, amount=20, day="2026-03-04", **extra):
        self.rows[row_id] = {
            "id": row_id, "accountId": f"acct-{account}", "activityDate": f"{day}T17:00:00Z",
            "activityType": "WITHDRAWAL", "amount": str(amount), "notes": notes, "assignments": [],
            "splits": [], "eventId": None, "cashFlowBucket": "spending", "idempotencyKey": None, **extra,
        }

    def list_accounts(self):
        return ACCOUNTS

    def display_timezone(self):
        return ZoneInfo("America/Chicago")

    def get(self, path):
        return {
            "/taxonomies/spending_categories": {"categories": CATEGORIES},
            "/spending/budget": {"state": {"groups": GROUPS, "groupAssignments": ASSIGNMENTS}},
            "/spending/event-types": self.event_types,
            "/spending/events": self.events,
            "/spending/rules": self.rules,
            "/spending/cash-activities": copy.deepcopy(list(self.rows.values())),
        }[path]

    def post(self, path, body):
        if path == "/spending/event-types":
            self.event_types.append({"id": f"type-{body['name']}", **body})
            return self.event_types[-1]
        if path == "/spending/events":
            self.events.append({"id": f"event-{body['name']}", **body})
            return self.events[-1]
        if path == "/spending/rules/upsert":
            self.rules = [r for r in self.rules if r["id"] != body["id"]] + [dict(body)]
            return self.rules[-1]
        if path == "/spending/rules/rerun":
            for row in self.rows.values():
                rule = next((r for r in self.rules if re.search(r["pattern"], row["notes"] or "")), None)
                if rule and not row["assignments"]:
                    row["assignments"] = [{"taxonomyId": rule["taxonomyId"], "categoryId": rule["categoryId"], "source": "rule"}]
            return 0
        if path == "/utilities/database/backup":
            self.backups += 1
            return {}
        raise KeyError(path)

    def put(self, path, body):
        row_id = path.split("/")[3]
        if path.endswith("/event"):
            self.rows[row_id]["eventId"] = body["eventId"]
        else:
            self.rows[row_id]["assignments"] = [{**body, "source": "manual"}]

    def delete(self, path):
        if path.startswith("/spending/rules/"):
            self.rules = [r for r in self.rules if r["id"] != path.rsplit("/", 1)[1]]
        else:
            self.rows[path.split("/")[3]]["assignments"] = []


@pytest.fixture
def world(tmp_path):
    fake = FakeWealthfolio()
    for i, shop in enumerate(["BLUE KETTLE COFFEE", "NORTH COFFEE", "SOUTH COFFEE"] * 4):
        fake.add(f"ann-{i}", "ann", f"SQ *{shop} SPRINGFIELD ZZ", amount=6 + i, day=f"2026-02-{i + 1:02d}")
    for i, gym in enumerate(["RIVER GYM", "IRON GYM", "LAKE GYM"] * 4):
        fake.add(f"ben-{i}", "ben", f"TST* {gym} DUES", amount=40 + i, day=f"2026-02-{i + 1:02d}")
    fake.add("shared-coffee", "shared", "SQ *WEST COFFEE SPRINGFIELD ZZ", amount=7)
    fake.add("shared-hardware-1", "shared", "HARDWARE STORE 0412", amount=55, day="2026-01-03")
    fake.add("shared-hardware-2", "shared", "HARDWARE STORE 0412", amount=12, day="2026-02-03")
    fake.add("atm", "checking", "ATM WITHDRAWAL 1234 MAIN ST", amount=100)
    fake.add("checking-shop", "checking", "Corner Shop", amount=9)
    fake.add("trip", "shared", "HOTEL LAKESIDE", amount=300, eventId="travel-event")
    labels = tmp_path / "labels"
    labels.mkdir()
    (labels / "config.json").write_text(json.dumps({
        "spenders": {"a": "Ann", "b": "Ben", "j": "Joint"},
        "threshold": 0.8,
        "needWant": {"needs": "Need", "wants": "Want"},
        "accounts": {
            "acct-ann": {"default": "Ann"}, "acct-ben": {"default": "Ben"},
            "acct-checking": {"default": "Ben", "patterns": {r"(?i)\bATM\b": "Joint"}},
        },
        "checkAccounts": ["acct-shared"], "checkSize": 2, "minCheckLabels": 0,
    }), encoding="utf-8")
    ws = Workspace(tmp_path, fake, by="test")
    setup(ws)
    return fake, ws


def shown(fake, row_id):
    event = fake.rows[row_id]["eventId"]
    return event and event.removeprefix("event-")


@pytest.mark.parametrize("text, key", [
    ("SQ *BLUE KETTLE COFFEE SPRINGFIELD ZZ", "BLUE KETTLE"),
    ("TST* NORTH STAR DELI 42", "NORTH STAR"),
    ("WEB INITIATED PAYMENT AT PAYPAL PURCHASE RIDE SHARE 1 000000", "RIDE SHARE"),
    ("K Grocer", "K GROCER"),
    ("Paypal", "PAYPAL"),
    ("CAFE 12345 SEATTLE WA", "CAFE SEATTLE"),
    ("CHECKCARD 0415 CORNER CAFE 123", "CORNER CAFE"),
    ("POS 1234 COFFEE SHOP", "COFFEE SHOP"),
    ("RIDES", "RIDES"),
    ("PAYPAL PAYMENT", "PAYPAL PAYMENT"),
    ("ACH Withdrawal DEPT EDUCATION STUDENT LN", "DEPT EDUCATION"),
    ("ACH Deposit ACME PAYROLL", "ACME PAYROLL"),
    ("DEBIT CARD PURCHASE CORNER DELI", "CORNER DELI"),
    (None, ""),
])
def test_merchant_key_and_native_rule_pattern_agree(text, key):
    assert merchant_key(text) == key
    if key:
        assert re.search(merchant_pattern(key), text)
        assert not re.search(merchant_pattern(key), "SOMETHING ELSE ENTIRELY")


@pytest.mark.parametrize("key, other", [("RIDES", "RIDES EATS PENDING"), ("PAYPAL PAYMENT", "PAYPAL PAYMENT STREAMCO")])
def test_whole_name_rules_do_not_match_longer_merchants(key, other):
    assert merchant_key(other) != key
    assert not re.search(merchant_pattern(key), other)


def test_features_use_simplefin_extras_when_present():
    row = {"activityType": "WITHDRAWAL", "notes": "SQ *BLUE KETTLE", "amount": "6", "activityDate": "2026-03-07T17:00:00Z"}
    found = features(row, {"mcc": "5814", "transacted_at": 1772902800}, ZoneInfo("America/Chicago"))
    assert {"m:BLUE KETTLE", "mcc:5814", "dow:Sat", "amt:2", "hr:09"} <= set(found)


def test_ledger_precedence_revert_history_and_reload(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append([{"field": "spender", "merchant": "SHOP", "value": "Ann", "source": "merchant", "by": "t", "action": "1"}])
    ledger.append([{"field": "spender", "activityId": "x", "value": "Ben", "source": "user", "by": "t", "action": "2"}])
    pick = lambda: max(ledger.candidates("spender", "x", "SHOP"), key=lambda e: (e["source"] == "user", e["seq"]))["value"]
    assert pick() == "Ben"
    ledger.append([{"field": "spender", "reverts": e["seq"], "by": "t"} for e in ledger.last_action("t")])
    reloaded = Ledger(tmp_path / "ledger.jsonl")
    assert [e["value"] for e in reloaded.candidates("spender", "x", "SHOP")] == ["Ann"]
    assert [e.get("reverted") for e in reloaded.history("x", "SHOP") if "value" in e] == [False, True]


def test_naive_bayes_calibrates_monotonically_and_explains():
    examples = [(f"{p}{i}", [f"m:{p}{i % 3}", f"w:{word}", "dow:Mon"], p, 1.0)
                for p, word in (("Ann", "COFFEE"), ("Ben", "GYM")) for i in range(30)]
    model, version = nb.train(["Ann", "Ben", "Joint"], examples)
    value, confidence, lift, _ = model.predict(["m:new", "w:COFFEE"])
    assert value == "Ann" and confidence > 0.8 and lift[0][0] == "w:COFFEE" and lift[0][2] == 30
    tiny = nb.NaiveBayes(["Ann", "Joint"]).fit(
        [(["w:A", "w:Z"] if i == 0 else ["w:A"], "Ann", 1.0) for i in range(30)] + [(["w:J"], "Joint", 1.0)])
    best, _, reasons, _ = tiny.predict(["w:J", "w:Z"])
    assert best == "Joint" and [f for f, _, _ in reasons] == ["w:J"]
    platt = nb.NaiveBayes(["Ann", "Ben"])
    platt.fit_calibration([(p / 100, i < p - 20) for p in range(40, 100) for i in range(100)])
    grid = [platt.calibrate(p / 100) for p in range(40, 100)]
    assert grid == sorted(grid) and len(set(grid)) > 40 and abs(platt.calibrate(0.9) - 0.7) < 0.05
    assert version == nb.train(["Ann", "Ben", "Joint"], list(reversed(examples)))[1]
    assert nb.metrics([("Ann", 0.95, "Ann"), ("Ben", 0.5, "Ann")], 0.9)["precisionAtThreshold"] == 1.0


def test_write_lock_is_exclusive_and_never_breaks_another_holder(tmp_path):
    with write_lock(tmp_path, "first"):
        with pytest.raises(Locked, match="first"):
            with write_lock(tmp_path, "second"):
                pass
    path = tmp_path / "maintenance" / "wealthfolio-write.lock"
    assert not path.exists()
    with write_lock(tmp_path, "first"):
        path.write_text("someone else", encoding="utf-8")
    assert path.read_text(encoding="utf-8") == "someone else"


def test_apply_writes_native_events_with_provenance(world):
    fake, ws = world
    assert ws.config["events"] == {"Ann": "event-Ann", "Ben": "event-Ben", "Joint": "event-Joint"}
    assert fake.events[0]["startDate"] == "2026-01-03"
    ws.sync(ws.rows)
    assert shown(fake, "ann-0") == "Ann" and shown(fake, "ben-0") == "Ben"
    assert shown(fake, "atm") == "Joint" and shown(fake, "checking-shop") == "Ben"
    assert shown(fake, "shared-coffee") == "Ann"
    assert fake.rows["shared-hardware-1"]["eventId"] is None
    assert fake.rows["trip"]["eventId"] == "travel-event"
    [entry] = [e for e in ws.ledger.entries if e.get("activityId") == "shared-coffee"]
    assert entry["source"] == "model" and entry["confidence"] >= 0.8 and entry["model"] == ws.version
    assert any("COFFEE" in reason for reason in entry["reasons"])
    assert {g["key"] for g in ws.queue()} == {"HARDWARE STORE"}
    assert ws.sync(ws.rows) == {"reconciled": 0, "set": 0, "cleared": 0}


def test_edits_in_wealthfolio_win_and_are_recorded(world):
    fake, ws = world
    ws.sync(ws.rows)
    fake.rows["ann-0"]["eventId"] = "event-Joint"
    fake.rows["ben-0"]["eventId"] = None
    ws.refresh()
    assert ws.sync(ws.rows)["reconciled"] == 2
    assert shown(fake, "ann-0") == "Joint" and fake.rows["ben-0"]["eventId"] is None
    assert {(e["activityId"], e["value"]) for e in ws.ledger.entries if e["source"] == "wealthfolio-ui"} == {("ann-0", "Joint"), ("ben-0", None)}


def test_merchant_labels_cover_past_and_future_rows_and_undo(world):
    fake, ws = world
    ws.label("Joint", merchant="HARDWARE STORE", note="household")
    assert shown(fake, "shared-hardware-1") == shown(fake, "shared-hardware-2") == "Joint"
    fake.add("shared-hardware-3", "shared", "HARDWARE STORE 0412", amount=30, day="2026-03-20")
    ws.refresh()
    ws.sync(ws.rows)
    assert shown(fake, "shared-hardware-3") == "Joint"
    assert ws.view("shared-hardware-3")["note"] == "household"
    assert ws.undo() == {"undone": 1}
    assert all(fake.rows[f"shared-hardware-{i}"]["eventId"] is None for i in (1, 2, 3))
    assert ws.undo() == {"undone": 0}


def test_merchant_labels_respect_recent_wealthfolio_edits(world):
    fake, ws = world
    ws.sync(ws.rows)
    fake.rows["shared-hardware-1"]["eventId"] = "event-Ben"
    ws.label("Joint", merchant="HARDWARE STORE")
    assert shown(fake, "shared-hardware-1") == "Ben" and shown(fake, "shared-hardware-2") == "Joint"
    assert any(e["source"] == "wealthfolio-ui" and e["activityId"] == "shared-hardware-1" for e in ws.ledger.entries)


def test_another_writer_forces_a_reload_before_writing(world):
    fake, ws = world
    other = Workspace(ws.data_dir, fake, by="daemon")
    other.label("Ben", ids=["shared-hardware-1"])
    ws.ensure_fresh()
    ws.label("Ann", ids=["shared-hardware-2"])
    seqs = [e["seq"] for e in Ledger(ws.dir / "ledger.jsonl").entries]
    assert len(seqs) == len(set(seqs))
    assert json.loads((ws.dir / "applied.json").read_text())["shared-hardware-1"] == "Ben"


def test_check_labels_are_blind_scored_and_never_trained_on(world):
    fake, ws = world
    check = ws.check_ids()
    assert len(check) == 2 and all(fake.rows[i]["accountId"] == "acct-shared" for i in check)
    ws.label("Joint", merchant="HARDWARE STORE")
    ws.label("Joint", ids=check, check=True)
    ws.train()
    assert not {i for i, *_ in ws.examples} & set(check)
    assert ws.evaluate()["checkSet"]["n"] == 2
    assert ws.queue("check") == []


def test_categories_use_native_rules_and_budget_groups(world):
    fake, ws = world
    result = ws.categorize("cat_food_coffee", merchant="BLUE KETTLE")
    assert result["categorized"] == 4 and fake.rules[0]["matchType"] == "regex" and fake.rules[0]["priority"] == 100
    assert ws.need_want(ws.rows["ann-0"]) == "Want"
    ws.categorize("cat_groceries", ids=["shared-hardware-1"])
    assert ws.need_want(ws.rows["shared-hardware-1"]) == "Need"
    ws.undo()
    assert fake.rows["shared-hardware-1"]["assignments"] == []
    ws.categorize("cat_groceries", merchant="BLUE KETTLE")
    assert [r["categoryId"] for r in fake.rules] == ["cat_groceries"]
    ws.undo()
    assert [r["categoryId"] for r in fake.rules] == ["cat_food_coffee"]
    ws.undo()
    assert fake.rules == [] and fake.rows["ann-0"]["assignments"] == []


def test_issuer_file_cardmembers_label_shared_card_rows(tmp_path, world):
    fake, ws = world
    fake.add("card-1", "card", "BOOK SHOP", amount=25.5, day="2026-03-06")
    fake.add("card-2", "card", "BOOK SHOP", amount=25.5, day="2026-03-07")
    fake.add("card-3", "card", "TOOL SHOP", amount=80, day="2026-03-09")
    block = "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>{}<TRNAMT>-{}<MEMO>{}</STMTTRN>"
    (tmp_path / "card.ofx").write_text(
        block.format("20260304", "25.50", "ANN EXAMPLE-11111") + block.format("20260311", "80.00", "BEN EXAMPLE-22222"),
        encoding="utf-8")
    ws.config["cardmembers"] = [{"accountId": "acct-card", "file": "card.ofx", "patterns": {"ANN": "Ann", "BEN": "Ben"}}]
    ws.refresh()
    assert {i: v["value"] for i, v in ws.cardmember.items()} == {"card-3": "Ben"}


def test_canvas_api_labels_audits_and_backs_up_once(world):
    fake, ws = world
    app = Labeler(ws)
    assert fake.backups == 1 and "Synced" in app.notice
    card = app.handle("GET", "/api/card", {}, {})["card"]
    assert card["merchant"] == "HARDWARE STORE" and card["rows"][0]["guess"]["value"] in ws.spenders
    app.handle("POST", "/api/label", {}, {"value": "Ben", "ids": [card["rows"][0]["id"]]})
    assert app.handle("GET", "/api/card", {}, {})["counts"]["session"] == 1
    audit = app.handle("GET", "/api/audit", {"source": "model", "max": "1"}, {})
    assert "shared-coffee" in [r["id"] for r in audit]
    assert all(shown(fake, r["id"]) == r["spender"] for r in audit)
    assert app.handle("GET", "/api/card", {"mode": "check"}, {})["card"]["blind"] is True
    assert fake.backups == 1


def test_canvas_server_refuses_foreign_hosts_and_form_posts(world):
    fake, ws = world
    server = make_server(ws)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def status(path, data=None, headers=None):
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers or {})) as res:
                return res.status
        except urllib.error.HTTPError as exc:
            return exc.code
    try:
        assert status("/api/config") == 200
        assert status("/api/audit", headers={"Host": "attacker.example"}) == 403
        assert status("/api/undo", b"{}", {"Content-Type": "text/plain"}) == 415
        assert status("/api/undo", b"{}", {"Content-Type": "application/json"}) == 200
    finally:
        server.shutdown()

def test_deferred_merchants_wait_at_the_end_of_the_queue(world):
    fake, ws = world
    fake.add("shared-kiosk", "shared", "TINY KIOSK", amount=1)
    ws.refresh()
    assert [g["key"] for g in ws.queue()] == ["HARDWARE STORE", "TINY KIOSK"]
    ws.config["deferMerchants"] = ["^HARDWARE\\b"]
    assert [g["key"] for g in ws.queue()] == ["TINY KIOSK", "HARDWARE STORE"]

def test_every_write_bumps_the_version_open_pages_poll(world):
    fake, ws = world
    app = Labeler(ws)
    start = app.handle("GET", "/api/version", {}, {})["version"]
    assert app.handle("GET", "/api/card", {}, {})["counts"]["version"] == start
    app.handle("POST", "/api/label", {}, {"value": "Joint", "merchant": "HARDWARE STORE"})
    assert app.handle("GET", "/api/version", {}, {})["version"] == start + 1
    app.handle("GET", "/api/card", {}, {})
    assert app.handle("GET", "/api/version", {}, {})["version"] == start + 1
    app.handle("POST", "/api/undo", {}, {})
    assert app.handle("GET", "/api/card", {}, {})["counts"]["version"] == start + 2

def test_predictions_list_unlabeled_rows_sorted_filtered_and_paged(world):
    fake, ws = world
    for i in range(5):
        fake.add(f"shared-misc-{i}", "shared", f"MISC VENDOR {i}", amount=10 + i, day=f"2026-03-0{i + 1}")
    ws.refresh()
    app = Labeler(ws)
    held = set(ws.check_ids())
    expected = {i for i in ws.rows if ws.unlabeled(i)} - held
    page = app.handle("GET", "/api/predictions", {"sort": "amount", "dir": "desc", "limit": "3"}, {})
    assert page["total"] == len(expected) >= 5 and len(page["rows"]) == 3
    amounts = [r["amount"] for r in page["rows"]]
    assert amounts == sorted(amounts, reverse=True) and {r["id"] for r in page["rows"]} <= expected
    rest = app.handle("GET", "/api/predictions", {"sort": "amount", "dir": "desc", "offset": "3", "limit": "100"}, {})
    assert {r["id"] for r in page["rows"] + rest["rows"]} == expected
    target = sorted(i for i in expected if i.startswith("shared-misc"))[0]
    needle = fake.rows[target]["notes"].lower()
    assert [r["id"] for r in app.handle("GET", "/api/predictions", {"q": needle}, {})["rows"]] == [target]
    value = page["rows"][0]["guess"]["value"]
    assert all(r["guess"]["value"] == value for r in app.handle("GET", "/api/predictions", {"value": value}, {})["rows"])
    assert app.handle("GET", "/api/predictions", {"min": "1.01"}, {})["total"] == 0
    model = page["model"]
    assert model["model"] == ws.version and model["classes"]["Joint"]["merchants"] < 20
    assert any(w.startswith("Joint is barely learned") for w in model["warnings"])


def test_accepting_predictions_confirms_them_with_model_provenance(world):
    fake, ws = world
    for i in range(5):
        fake.add(f"shared-misc-{i}", "shared", f"MISC VENDOR {i}", amount=10 + i)
    ws.refresh()
    app = Labeler(ws)
    rows = app.handle("GET", "/api/predictions", {"limit": "2"}, {})["rows"]
    first, second = rows
    other = next(v for v in ws.spenders if v != second["guess"]["value"])
    app.handle("POST", "/api/label", {}, {"accept": [
        {"id": first["id"], "value": first["guess"]["value"]}, {"id": second["id"], "value": other},
    ]})
    entries = {e["activityId"]: e for e in ws.ledger.entries if e.get("accepted")}
    assert entries[first["id"]]["source"] == "user" and entries[first["id"]]["confidence"] == first["guess"]["confidence"]
    assert entries[first["id"]]["reasons"] == first["guess"]["reasons"] and entries[first["id"]]["model"]
    assert "confidence" not in entries[second["id"]]
    assert shown(fake, first["id"]) == first["guess"]["value"] and shown(fake, second["id"]) == other
    assert first["id"] not in {r["id"] for r in app.handle("GET", "/api/predictions", {}, {})["rows"]}
    app.handle("POST", "/api/undo", {}, {})
    assert fake.rows[first["id"]]["eventId"] is None

@pytest.mark.parametrize("order, first", [
    ("impact", "BIG OLD"), ("amount", "BIG OLD"), ("newest", "NEW SMALL"), ("oldest", "BIG OLD"),
    ("merchant", "BIG OLD"), ("low-confidence", None), ("high-confidence", None),
])
def test_queue_orders_keep_deferred_merchants_last(world, order, first):
    fake, ws = world
    fake.add("old", "shared", "BIG OLD 1", amount=900, day="2023-05-01")
    fake.add("new", "shared", "NEW SMALL 1", amount=2, day="2026-04-01")
    fake.add("deferred", "shared", "ZZ DEFER 1", amount=5000, day="2026-05-01")
    ws.refresh()
    ws.config["deferMerchants"] = ["^ZZ DEFER"]
    keys = [g["key"] for g in ws.queue(order=order)]
    assert keys[-1] == "ZZ DEFER" and (first is None or keys[0] == first)
    if order.endswith("confidence"):
        ranks = [g["confidence"] for g in ws.queue(order=order)[:-1]]
        assert ranks == sorted(ranks, reverse=order == "high-confidence")


def test_rows_that_do_not_count_as_spending_need_no_spender(world):
    fake, ws = world
    fake.add("funding", "shared", "WALLET FUNDING 1", amount=50, visibleSpendingAmount=0)
    ws.refresh()
    assert not ws.unlabeled("funding")
    assert "WALLET FUNDING" not in {g["key"] for g in ws.queue()}
    assert "funding" not in {r["id"] for r in Labeler(ws).handle("GET", "/api/predictions", {"limit": "500"}, {})["rows"]}


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_typing_and_open_overlays_swallow_shortcuts():
    page = Path(labeling.__file__).with_name("labeler.html").read_text(encoding="utf-8")
    source = re.search(r"function routeKey\(.*?\n}", page, re.S).group(0)
    cases = [
        ("e", {}, "shortcut"), ("Tab", {}, "shortcut"), ("e", {"modifier": True}, "ignore"),
        ("e", {"typing": True}, "ignore"), ("Enter", {"typing": True}, "ignore"), ("Escape", {"typing": True}, "close"),
        ("e", {"overlay": "note"}, "ignore"), ("Escape", {"overlay": "note"}, "close"),
        ("e", {"overlay": "picker"}, "type"), ("u", {"overlay": "picker", "typing": True}, "type"),
        ("ArrowDown", {"overlay": "picker"}, "picker"), ("Enter", {"overlay": "picker"}, "picker"),
        ("Escape", {"overlay": "picker"}, "close"),
    ]
    script = f"{source}\nconsole.log(JSON.stringify({json.dumps([[k, s] for k, s, _ in cases])}.map(([k, s]) => routeKey(k, s))));"
    routes = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert routes == [expected for *_, expected in cases]


def test_rereading_picks_up_changes_made_outside_the_labeler(world):
    fake, ws = world
    app = Labeler(ws)
    start = app.handle("GET", "/api/version", {}, {})["version"]
    assert app.handle("GET", "/api/refresh", {}, {})["version"] == start
    assert "HARDWARE STORE" in {g["key"] for g in ws.queue()}
    for row_id in ("shared-hardware-1", "shared-hardware-2"):
        fake.rows[row_id]["visibleSpendingAmount"] = 0
    assert app.handle("GET", "/api/refresh", {}, {})["version"] == start + 1
    assert "HARDWARE STORE" not in {g["key"] for g in ws.queue()}
    config = json.loads((ws.dir / "config.json").read_text(encoding="utf-8"))
    (ws.dir / "config.json").write_text(json.dumps({**config, "threshold": 0.99}), encoding="utf-8")
    assert app.handle("GET", "/api/refresh", {}, {})["version"] == start + 2 and ws.threshold == 0.99


def test_confidence_orders_break_calibration_ties_with_the_raw_posterior(world):
    fake, ws = world
    model = nb.NaiveBayes(["Ann", "Ben"]).fit([(["w:A"], "Ann", 1.0)] * 9 + [(["w:A"], "Ben", 1.0), (["w:B"], "Ben", 1.0)])
    model.calibration = [0.0, nb.logit(0.6)]
    sure, unsure = model.predict(["w:A", "w:A"]), model.predict(["w:B"])
    assert sure[1] == unsure[1] == 0.6 and sure[3] != unsure[3]
    for i, raw in ((1, 0.70), (2, 0.55), (3, 0.90)):
        fake.add(f"tie-{i}", "card", f"TIE SHOP{i} 1", amount=10, day="2026-03-0" + str(i))
    ws.refresh()
    app = Labeler(ws)
    for i, raw in ((1, 0.70), (2, 0.55), (3, 0.90)):
        ws.guess[f"tie-{i}"].update(confidence=0.6, rank=(0.6, raw))
    ties = lambda keys: [k for k in keys if k.startswith("TIE")]
    assert ties(g["key"] for g in ws.queue(order="low-confidence")) == ["TIE SHOP2", "TIE SHOP1", "TIE SHOP3"]
    assert ties(g["key"] for g in ws.queue(order="high-confidence")) == ["TIE SHOP3", "TIE SHOP1", "TIE SHOP2"]
    rows = app.handle("GET", "/api/predictions", {"q": "tie shop", "sort": "confidence"}, {})["rows"]
    assert [r["id"] for r in rows] == ["tie-2", "tie-1", "tie-3"]


def test_empty_notes_fall_back_to_a_private_export_description(world):
    fake, ws = world
    fake.add("legacy", "shared", None, amount=73, idempotencyKey="legacy:42")
    fake.add("blank", "shared", "", amount=5, idempotencyKey="legacy:7")
    (ws.data_dir / "export.csv").write_text("Id,Original Statement\n42,ACH Withdrawal RIVER HOMES 0099\n7,\n", encoding="utf-8")
    config = json.loads((ws.dir / "config.json").read_text(encoding="utf-8"))
    config["descriptionFiles"] = [{"file": "export.csv", "key": "legacy:{Id}", "text": "Original Statement"}]
    (ws.dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    ws.load()
    assert ws.merchant["legacy"] == "RIVER HOMES" and ws.view("legacy")["notes"] == "ACH Withdrawal RIVER HOMES 0099"
    assert ws.merchant["blank"] == "" and fake.rows["legacy"]["notes"] is None



def test_filler_words_are_not_features_and_weak_evidence_never_leads():
    row = {"activityType": "WITHDRAWAL", "notes": "Unique Vac Rep of Sand XXXX4115 TO THE CO", "amount": "9", "activityDate": "2026-03-02"}
    words = {f for f in features(row) if f.startswith("w:")}
    assert words == {"w:UNIQUE", "w:VAC", "w:REP", "w:SAND"}
    model = nb.NaiveBayes(["Ann", "Ben"]).fit(
        [(["w:RARE", "w:COMMON"], "Ann", 1.0)] + [(["w:COMMON"], "Ann", 1.0)] * 9 + [(["w:OTHER"], "Ben", 1.0)] * 30)
    _, _, reasons, _ = model.predict(["w:RARE", "w:COMMON"])
    assert [f for f, _, _ in reasons] == ["w:COMMON", "w:RARE"]



def test_model_labels_wait_for_check_set_evidence(world):
    fake, ws = world
    fake.add("card-coffee", "card", "SQ *EAST COFFEE SPRINGFIELD ZZ", amount=6)
    ws.config["minCheckLabels"] = 2
    ws.refresh()
    assert not ws.auto_apply and ws.resolve("card-coffee") is None
    ws.sync(ws.rows)
    assert fake.rows["card-coffee"]["eventId"] is None
    for row_id in ws.check_ids():
        ws.ledger.append([{"field": "spender", "activityId": row_id, "value": ws.guess[row_id]["value"],
                           "source": "user", "check": True, "by": "t"}])
    ws.threshold = min(ws.guess[i]["confidence"] for i in [*ws.check_ids(), "card-coffee"])
    ws.train()
    assert ws.auto_apply and ws.resolve("card-coffee")["source"] == "model"


def test_queue_cards_show_each_row_prediction_and_the_merchant_summary(world):
    fake, ws = world
    for i in range(3):
        fake.add(f"vendor-{i}", "card", f"RIVER VENDOR {i}", amount=10 * (i + 1))
    ws.refresh()
    app = Labeler(ws)
    card = app.handle("GET", "/api/card", {"order": "merchant"}, {})["card"]
    while card["merchant"] != "RIVER VENDOR":
        card = app.handle("GET", "/api/card", {"order": "merchant", "skip": card["key"]}, {})["card"]
    group = next(g for g in ws.queue(order="merchant") if g["key"] == "RIVER VENDOR")
    assert sum(count for _, count in card["summary"]["predicted"]) == len(group["ids"]) == 3
    assert card["summary"]["confidence"] == round(group["confidence"][0], 3)
    assert all(r["guess"]["value"] in ws.spenders and r["guess"]["reasons"] is not None for r in card["rows"])
    blind = app.handle("GET", "/api/card", {"mode": "check"}, {})["card"]
    assert blind["summary"] is None and all(r["guess"] is None for r in blind["rows"])

def test_repeated_labels_are_not_recorded_twice(world):
    fake, ws = world
    first = ws.label("Joint", merchant="HARDWARE STORE")
    again = ws.label("Joint", merchant="HARDWARE STORE")
    assert first["recorded"] == 1 and again["recorded"] == 0
    assert ws.label("Ben", ids=["shared-coffee"])["recorded"] == 1
    assert ws.label("Ben", ids=["shared-coffee"])["recorded"] == 0
    assert ws.label("Ann", ids=["shared-coffee"])["recorded"] == 1
    ws.undo()
    assert ws.label("Ann", ids=["shared-coffee"])["recorded"] == 1
    assert len([e for e in ws.ledger.entries if e.get("merchant") == "HARDWARE STORE"]) == 1


def test_a_labeled_card_stays_on_screen_until_moved_on(world):
    fake, ws = world
    app = Labeler(ws)
    app.handle("POST", "/api/label", {}, {"value": "Joint", "merchant": "HARDWARE STORE"})
    card = app.handle("GET", "/api/card", {"key": "HARDWARE STORE"}, {})["card"]
    assert card["missing"] == {"spender": 0, "category": 2}
    assert "HARDWARE STORE" in {g["key"] for g in ws.queue()}, "a spender without a category stays queued"
    app.handle("POST", "/api/category", {}, {"categoryId": "cat_groceries", "merchant": "HARDWARE STORE"})
    assert "HARDWARE STORE" not in {g["key"] for g in ws.queue()}
    card = app.handle("GET", "/api/card", {"key": "HARDWARE STORE"}, {})["card"]
    assert card["missing"] == {"spender": 0, "category": 0} and {r["needWant"] for r in card["rows"]} == {"Need"}
    assert card["key"] == "HARDWARE STORE" and card["labeled"] == [("Joint", 2)]
    assert all(r["spender"] == "Joint" and r["source"] == "merchant" for r in card["rows"])
    assert app.handle("GET", "/api/card", {"key": "NO SUCH SHOP"}, {})["card"] is None

def test_renaming_merges_and_splits_merchants_without_losing_labels_or_categories(world):
    fake, ws = world
    fake.add("kiosk", "shared", "TINY KIOSK 1", amount=4)
    ws.refresh()
    app = Labeler(ws)
    app.handle("POST", "/api/label", {}, {"value": "Joint", "merchant": "HARDWARE STORE"})
    app.handle("POST", "/api/category", {}, {"categoryId": "cat_groceries", "merchant": "HARDWARE STORE"})
    hardware = [i for i, k in ws.merchant.items() if k == "HARDWARE STORE"]
    before = {i: (ws.resolve(i)["value"], ws.resolve(i)["source"], fake.rows[i]["eventId"], ws.rows[i]["categoryIds"]) for i in hardware}
    rules = copy.deepcopy(fake.rules)

    assert app.handle("POST", "/api/rename", {}, {"name": "Corner Hardware", "merchant": "HARDWARE STORE"})["key"] == "Corner Hardware"
    assert app.handle("POST", "/api/rename", {}, {"name": "Corner Hardware", "merchant": "TINY KIOSK"})["recorded"] == 1
    after = {i: (ws.resolve(i)["value"], ws.resolve(i)["source"], fake.rows[i]["eventId"], ws.rows[i]["categoryIds"]) for i in hardware}
    assert after == before and fake.rules == rules, "a name never touches labels, events, categories, or rules"
    assert "m:CORNER HARDWARE" in ws.features["kiosk"] and "m:CORNER HARDWARE" in ws.features[hardware[0]]
    card = app.handle("GET", "/api/card", {"key": "Corner Hardware"}, {})["card"]
    assert card["keys"] == ["HARDWARE STORE", "TINY KIOSK"] and {r["id"] for r in card["rows"]} == {*hardware, "kiosk"}
    assert {r["name"] for r in card["rows"]} == {"Corner Hardware"} and {r["notes"] for r in card["rows"]} == {"HARDWARE STORE 0412", "TINY KIOSK 1"}
    assert app.handle("GET", "/api/suggest", {"merchant": "Corner Hardware"}, {})["names"] == ["Corner Hardware"]

    app.handle("POST", "/api/label", {}, {"value": "Ben", "merchant": "Corner Hardware"})
    assert {e["merchant"] for e in ws.ledger.live if e.get("value") == "Ben"} == {"HARDWARE STORE", "TINY KIOSK"}

    app.handle("POST", "/api/rename", {}, {"name": "Kiosk", "id": "kiosk"})
    assert ws.display["kiosk"] == "Kiosk" and ws.keys_of("Corner Hardware") == ["HARDWARE STORE"]
    app.handle("POST", "/api/undo", {}, {})
    assert ws.display["kiosk"] == "Corner Hardware"
    app.handle("POST", "/api/rename", {}, {"name": "", "merchant": "Corner Hardware"})
    assert ws.display[hardware[0]] == "HARDWARE STORE" and ws.display["kiosk"] == "TINY KIOSK"
    assert {i: ws.rows[i]["categoryIds"] for i in hardware} == {i: v[3] for i, v in before.items()}


def test_rename_suggestions_prefer_the_payee_then_an_export_name(world):
    fake, ws = world
    fake.add("vac", "shared", "UNIQUE VAC REP 1", amount=900, idempotencyKey="legacy:9")
    (ws.data_dir / "export.csv").write_text("Id,Original Statement,Merchant\n9,UNIQUE VAC REP 1,Island Resorts\n", encoding="utf-8")
    ws.config["descriptionFiles"] = [{"file": "export.csv", "key": "legacy:{Id}", "text": "Original Statement", "merchant": "Merchant"}]
    ws.descriptions, ws.export_names = ws._exports()
    ws.refresh()
    app = Labeler(ws)
    assert app.handle("GET", "/api/suggest", {"merchant": "UNIQUE VAC"}, {})["suggestion"] == "Island Resorts"
    ws.raw["legacy:9"] = {"payee": "Island Resorts Travel"}
    assert app.handle("GET", "/api/suggest", {"merchant": "UNIQUE VAC"}, {})["suggestion"] == "Island Resorts Travel"
    assert app.handle("GET", "/api/suggest", {"merchant": "HARDWARE STORE"}, {})["suggestion"] == "HARDWARE STORE"

def test_uninformative_features_and_reasons_are_dropped(world):
    fake, ws = world
    row = {"activityType": "WITHDRAWAL", "notes": "CORNER DELI", "amount": "9", "activityDate": "2026-03-02"}
    assert not any(f.startswith("type:") for f in features(row))
    assert "type:credit" in features({**row, "activityType": "CREDIT"})
    model = nb.NaiveBayes(["Ann", "Ben"]).fit([(["w:EVERY", "w:ANN"], "Ann", 1.0)] * 20 + [(["w:EVERY"], "Ben", 1.0)] * 20)
    assert [f for f, _, _ in model.predict(["w:EVERY", "w:ANN"])[2]] == ["w:ANN"]


def test_the_labeled_banner_counts_only_hand_labels(world):
    fake, ws = world
    fake.add("ann-hardware", "ann", "HARDWARE STORE 0412", amount=8)
    ws.refresh()
    app = Labeler(ws)
    card = app.handle("GET", "/api/card", {"key": "HARDWARE STORE"}, {})["card"]
    assert card["labeled"] == [] and next(r for r in card["rows"] if r["id"] == "ann-hardware")["source"] == "account"
    app.handle("POST", "/api/label", {}, {"value": "Joint", "ids": ["shared-hardware-1"]})
    assert app.handle("GET", "/api/card", {"key": "HARDWARE STORE"}, {})["card"]["labeled"] == [("Joint", 1)]


def test_merchant_categories_report_rows_that_keep_another_category(world):
    fake, ws = world
    ws.categorize("cat_food_coffee", ids=["shared-hardware-1"])
    result = ws.categorize("cat_groceries", merchant="HARDWARE STORE")
    assert result["kept"] == 1 and result["categorized"] == 1
    assert ws.rows["shared-hardware-1"]["categoryIds"] == ["cat_food_coffee"]


def test_need_and_want_suggest_a_built_in_category(world):
    fake, ws = world
    app = Labeler(ws)
    suggest = lambda **q: app.handle("GET", "/api/category-suggest", q, {})
    assert suggest(merchant="HARDWARE STORE", needWant="Need") == {"categoryId": "cat_groceries", "why": "first Need category", "rows": 2}
    ws.categorize("cat_groceries", ids=["shared-hardware-1"])
    assert suggest(merchant="HARDWARE STORE", needWant="Need")["why"] == "already used on this merchant"
    ws.categorize("cat_food_coffee", ids=["ann-0"])
    assert suggest(id="shared-coffee", needWant="Want") == {
        "categoryId": "cat_food_coffee", "why": "most used on merchants with the same words", "rows": 1}
    assert suggest(id="shared-coffee", needWant="Need")["categoryId"] == "cat_groceries"

@pytest.mark.parametrize("host, alive, reclaimed", [
    ("here", False, True),    # this host, process gone: abandoned
    ("here", True, False),    # this host, process alive
    ("other", False, False),  # another host (say the sync container): its PIDs mean nothing here
    (None, False, False),     # an old lock without a host
])
def test_only_locks_abandoned_on_this_host_are_reclaimed(tmp_path, host, alive, reclaimed):
    path = tmp_path / "maintenance" / "wealthfolio-write.lock"
    path.parent.mkdir()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lock = {"holder": "crashed", "pid": os.getpid() if alive else dead.pid,
            "host": {"here": socket.gethostname(), "other": "container-1"}.get(host)}
    path.write_text(json.dumps({k: v for k, v in lock.items() if v is not None}), encoding="utf-8")
    if reclaimed:
        with write_lock(tmp_path, "next"):
            assert json.loads(path.read_text(encoding="utf-8"))["holder"] == "next"
        assert not path.exists()
    else:
        with pytest.raises(Locked, match="crashed"):
            with write_lock(tmp_path, "next"):
                pass
        assert json.loads(path.read_text(encoding="utf-8"))["holder"] == "crashed"


def test_shutdown_stops_the_server_between_requests(world):
    fake, ws = world
    server = make_server(ws)
    runner = threading.Thread(target=server.serve_forever)
    runner.start()
    request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/api/shutdown", data=b"{}",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        assert response.status == 200
    runner.join(timeout=5)
    assert not runner.is_alive()