"""Spender labeling against an in-memory Wealthfolio. All data is synthetic."""

import copy
import json
import re
import threading
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

import pytest

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
        self.rows, self.event_types, self.events, self.rules, self.backups, self.searches = {}, [], [], [], 0, 0

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
        }[path]

    def post(self, path, body):
        if path == "/spending/cash-activities/search":
            self.searches += 1
            rows = [r for r in self.rows.values() if body.get("search", "") in (r["notes"] or "").lower()]
            return {"items": copy.deepcopy(rows[body["offset"]: body["offset"] + body["limit"]])}
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
        "checkAccounts": ["acct-shared"], "checkSize": 2,
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
    value, confidence, lift = model.predict(["m:new", "w:COFFEE"])
    assert value == "Ann" and confidence > 0.8 and lift[0][0] == "w:COFFEE" and lift[0][2] == 30
    tiny = nb.NaiveBayes(["Ann", "Joint"]).fit(
        [(["w:A", "w:Z"] if i == 0 else ["w:A"], "Ann", 1.0) for i in range(30)] + [(["w:J"], "Joint", 1.0)])
    best, _, reasons = tiny.predict(["w:J", "w:Z"])
    assert best == "Joint" and [f for f, _, _ in reasons] == ["w:J"]
    assert model.calibration == sorted(model.calibration)
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