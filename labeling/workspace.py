"""Load spending rows from Wealthfolio, resolve each row's spender, and write labels back.

Wealthfolio holds the visible label (a spending event per spender, and categories whose
budget group gives need or want). The ledger holds why. Nothing here edits an activity
row, so amounts, dates, transfer links, and balances cannot change.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from importers.simplefin.wealthfolio_client import WealthfolioClient
from labeling import model as nb
from labeling.features import describe, features, merchant_key, merchant_pattern, raw_index
from labeling.ledger import Ledger, now, strongest

SPENDING = "spending_categories"
LOCK = Path("maintenance") / "wealthfolio-write.lock"
FIELD = "spender"
TRAIN_WEIGHT = {"user": 1.0, "wealthfolio-ui": 1.0, "merchant": 1.0, "cardmember": 0.3, "account": 0.3}


class Locked(RuntimeError):
    pass


@contextmanager
def write_lock(data_dir: Path, holder: str):
    """The shared write lock: exclusive create, and only ever remove our own."""
    path = data_dir / LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    token = json.dumps({"holder": holder, "pid": os.getpid(), "at": now()})
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise Locked(path.read_text(encoding="utf-8", errors="replace")[:300]) from None
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(token)
    try:
        yield
    finally:
        try:
            if path.read_text(encoding="utf-8") == token:
                path.unlink()
        except OSError:
            pass


def connect(data_dir: Path, base_url: str) -> WealthfolioClient:
    password = os.environ.get("WEALTHFOLIO_PASSWORD")
    env = data_dir / "compose.env"
    if not password and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("WEALTHFOLIO_PASSWORD="):
                password = line.split("=", 1)[1].strip().strip("\"'")
    if not password:
        raise ValueError("set WEALTHFOLIO_PASSWORD or keep it in <data>/compose.env")
    client = WealthfolioClient(base_url)
    client.login(password)
    return client


def spending_rows(client) -> list[dict]:
    rows, offset = [], 0
    while True:
        items = client.post("/spending/cash-activities/search", {"offset": offset, "limit": 1000})["items"]
        rows += [row for row in items if row["cashFlowBucket"] == "spending"]
        offset += len(items)
        if len(items) < 1000:
            return rows


def ofx_cardmembers(path: Path, patterns: dict[str, str]) -> list[tuple[str, float, str, str]]:
    """(day, amount, spender, memo pattern) for each OFX transaction whose memo names a cardmember."""
    found = []
    for block in re.findall(r"<STMTTRN>(.*?)</STMTTRN>", path.read_text(encoding="latin-1"), re.S):
        tag = lambda name: (re.search(rf"<{name}>([^<\r\n]*)", block) or [None, ""])[1].strip()
        posted, amount, memo = tag("DTPOSTED"), tag("TRNAMT"), tag("MEMO")
        for pattern, spender in patterns.items():
            if posted and amount and re.search(pattern, memo):
                found.append((date(int(posted[:4]), int(posted[4:6]), int(posted[6:8])).isoformat(),
                              abs(float(amount)), spender, pattern))
                break
    return found


class Workspace:
    def __init__(self, data_dir: Path, client, by: str):
        self.data_dir = data_dir
        self.dir = data_dir / "labels"
        self.client = client
        self.by = by
        self.config = json.loads((self.dir / "config.json").read_text(encoding="utf-8"))
        self.spenders: list[str] = list(self.config["spenders"].values())
        self.threshold = float(self.config.get("threshold", 0.9))
        self.raw = raw_index(data_dir)
        self.zone = client.display_timezone()
        self.load()

    # --- Reading -------------------------------------------------------------------------

    def _stamp(self) -> tuple:
        paths = (self.dir / "ledger.jsonl", self.dir / "applied.json")
        return tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in paths)

    def load(self) -> None:
        self.ledger = Ledger(self.dir / "ledger.jsonl")
        applied = self.dir / "applied.json"
        self.applied: dict[str, str | None] = json.loads(applied.read_text(encoding="utf-8")) if applied.exists() else {}
        self.refresh()
        self.loaded = self._stamp()

    def ensure_fresh(self) -> None:
        """Call under the write lock: reload everything if another writer changed the labels."""
        if self._stamp() != self.loaded:
            self.load()

    def mark_fresh(self) -> None:
        self.loaded = self._stamp()

    def refetch(self, ids) -> None:
        """Re-read these rows' current event and categories, so recent UI edits are seen."""
        wanted = set(ids)
        needles = {self.merchant[i].split()[0].lower() for i in wanted if self.merchant.get(i)}
        for needle in needles:
            offset = 0
            while True:
                items = self.client.post("/spending/cash-activities/search",
                                         {"offset": offset, "limit": 1000, "search": needle})["items"]
                for row in items:
                    if row["id"] in wanted:
                        current = self.rows[row["id"]]
                        current["eventId"] = row.get("eventId")
                        current["categoryIds"] = self._categories(row)
                        self.features[row["id"]] = features(current, self.raw.get(current.get("idempotencyKey") or ""), self.zone)
                offset += len(items)
                if len(items) < 1000:
                    break

    @staticmethod
    def _categories(row: dict) -> list[str]:
        found = [a["categoryId"] for a in row["assignments"] if a["taxonomyId"] == SPENDING]
        return found + [s["categoryId"] for s in row["splits"] if s["taxonomyId"] == SPENDING]

    def refresh(self) -> None:
        self.accounts = {a["id"]: a for a in self.client.list_accounts()}
        self.categories = {c["id"]: c for c in self.client.get(f"/taxonomies/{SPENDING}")["categories"]}
        budget = self.client.get("/spending/budget")["state"]
        groups = {g["id"]: g["key"] for g in budget["groups"]}
        self.group_of = {a["categoryId"]: groups[a["groupId"]] for a in budget["groupAssignments"]}
        self.event_value = {event_id: value for value, event_id in self.config.get("events", {}).items()}
        self.rows = {}
        for row in spending_rows(self.client):
            row["categoryIds"] = self._categories(row)
            self.rows[row["id"]] = row
        self.merchant = {i: merchant_key(r.get("notes")) for i, r in self.rows.items()}
        self.features = {i: features(r, self.raw.get(r.get("idempotencyKey") or ""), self.zone) for i, r in self.rows.items()}
        self.cardmember = self._cardmembers()
        self.train()

    def _cardmembers(self) -> dict[str, dict]:
        """Rows whose issuer file names the cardmember: same amount within 3 days, matched one-to-one."""
        labels = {}
        for source in self.config.get("cardmembers", []):
            path = self.data_dir / source["file"]
            if not path.exists():
                continue
            mine = [(i, date.fromisoformat(r["activityDate"][:10]), round(abs(float(r["amount"])), 2))
                    for i, r in self.rows.items() if r["accountId"] == source["accountId"]]
            claims: dict[str, list] = {}
            for day, amount, spender, pattern in ofx_cardmembers(path, source["patterns"]):
                posted = date.fromisoformat(day)
                near = [i for i, when, value in mine if value == round(amount, 2) and abs((when - posted).days) <= 3]
                if len(near) == 1:
                    claims.setdefault(near[0], []).append((spender, pattern))
            for row_id, found in claims.items():
                if len(found) == 1:
                    labels[row_id] = {"source": "cardmember", "value": found[0][0],
                                      "reasons": [f"issuer file names the cardmember ({found[0][1]})"]}
        return labels

    def account_default(self, row_id: str) -> dict | None:
        rule = self.config.get("accounts", {}).get(self.rows[row_id]["accountId"])
        if not rule:
            return None
        notes = self.rows[row_id].get("notes") or ""
        for pattern, value in rule.get("patterns", {}).items():
            if re.search(pattern, notes):
                return {"source": "account", "value": value, "reasons": [f"account rule {pattern}"]}
        name = self.accounts.get(self.rows[row_id]["accountId"], {}).get("name", "account")
        return {"source": "account", "value": rule["default"], "reasons": [f"default for {name}"]}

    def foreign_event(self, row_id: str) -> bool:
        event = self.rows[row_id].get("eventId")
        return bool(event) and event not in self.event_value

    def resolve(self, row_id: str, *, training: bool = False) -> dict | None:
        """The label Wealthfolio should show, or None when it should stay untagged."""
        found = [e for e in self.ledger.candidates(FIELD, row_id, self.merchant[row_id])
                 if e["source"] != "model" and not (training and e.get("check"))]
        found += [x for x in (self.account_default(row_id), self.cardmember.get(row_id)) if x]
        if not training and row_id in self.guess and self.guess[row_id]["confidence"] >= self.threshold:
            found.append(self.guess[row_id])
        best = strongest(found)
        return best if best and (best["value"] is not None or best["source"] in ("user", "wealthfolio-ui")) else None

    def need_want(self, row: dict) -> str | None:
        for category in row["categoryIds"]:
            parent = self.categories.get(category, {}).get("parentId")
            group = self.group_of.get(category) or self.group_of.get(parent)
            if group:
                return self.config.get("needWant", {}).get(group)
        return None

    # --- Model ---------------------------------------------------------------------------

    def train(self) -> None:
        names = {i: c["name"] for i, c in self.categories.items()}
        examples, check, held_out = [], [], set(self.check_ids())
        for row_id in self.rows:
            label = None if row_id in held_out else self.resolve(row_id, training=True)
            if label and label["value"] in self.spenders:
                examples.append((row_id, self.features[row_id], label["value"], TRAIN_WEIGHT[label["source"]]))
            blind = [e for e in self.ledger.by_activity.get((FIELD, row_id), []) if e.get("check")]
            if blind and blind[-1]["value"] in self.spenders:
                check.append((row_id, blind[-1]["value"]))
        self.model, self.version = nb.train(self.spenders, examples)
        self.guess = {}
        for row_id, found in self.features.items():
            value, confidence, lift = self.model.predict(found)
            self.guess[row_id] = {
                "source": "model", "value": value, "confidence": confidence, "model": self.version,
                "reasons": [f"{describe(f, names)} (seen {count}x as {value})" for f, _, count in lift],
            }
        self.examples, self.check = examples, check

    def evaluate(self) -> dict:
        """Honest accuracy: unseen merchants, a later period, and the blind check set."""
        pairs = lambda rows, predict: [(*predict(f), truth) for _, f, truth, _ in rows]
        calibrated = lambda m: lambda f: (m.predict(f)[0], self.model.calibrate(max(m.raw(f).values())))
        report = {"model": self.version, "at": now(), "threshold": self.threshold, "examples": len(self.examples),
                  "note": "checkSet is the honest joint-card number; unseenMerchants calibration is fit on the same folds"}
        for name, sources in (("labels", {1.0}), ("purchaser", {0.3})):
            subset = [e for e in self.examples if e[3] in sources]
            held = nb.out_of_fold(self.spenders, subset) if len({e[2] for e in subset}) > 1 else []
            report[f"unseenMerchants.{name}"] = nb.metrics(
                [(max(p, key=p.get), self.model.calibrate(max(p.values())), t) for p, t in held], self.threshold)
        ordered = sorted(self.examples, key=lambda e: self.rows[e[0]]["activityDate"])
        cut = int(len(ordered) * 0.8)
        if cut:
            older = nb.NaiveBayes(self.spenders).fit([(f, l, w) for _, f, l, w in ordered[:cut]])
            report["laterPeriod"] = nb.metrics(pairs(ordered[cut:], calibrated(older)), self.threshold)
        report["checkSet"] = nb.metrics(
            [(self.guess[i]["value"], self.guess[i]["confidence"], truth) for i, truth in self.check], self.threshold)
        return report

    # --- Writing -------------------------------------------------------------------------

    def _save_applied(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "applied.json.tmp"
        tmp.write_text(json.dumps(self.applied, sort_keys=True), encoding="utf-8")
        tmp.replace(self.dir / "applied.json")

    def shown(self, row_id: str) -> str | None:
        return self.event_value.get(self.rows[row_id].get("eventId") or "")

    def reconcile(self, ids) -> int:
        """Record edits made in Wealthfolio's own UI so they win and stay auditable."""
        entries = []
        for row_id in ids:
            if self.foreign_event(row_id):
                continue
            shown = self.shown(row_id)
            if shown != self.applied.get(row_id):
                entries.append({"field": FIELD, "activityId": row_id, "value": shown,
                                "source": "wealthfolio-ui", "by": self.by})
                self.applied[row_id] = shown
        self.ledger.append(entries)
        self._save_applied()
        return len(entries)

    def sync(self, ids, *, dry_run: bool = False) -> dict:
        """Make Wealthfolio show each row's resolved spender."""
        ids = list(ids)
        result = {"reconciled": 0 if dry_run else self.reconcile(ids), "set": 0, "cleared": 0}
        provenance = []
        try:
            for row_id in ids:
                if self.foreign_event(row_id):
                    continue
                label = self.resolve(row_id)
                desired = label["value"] if label else None
                if desired == self.shown(row_id):
                    continue
                result["set" if desired else "cleared"] += 1
                if dry_run:
                    continue
                event_id = self.config["events"][desired] if desired else None
                self.client.put(f"/spending/cash-activities/{row_id}/event", {"eventId": event_id})
                self.rows[row_id]["eventId"] = event_id
                self.applied[row_id] = desired
                if label and label["source"] == "model":
                    provenance.append({"field": FIELD, "activityId": row_id, "by": self.by,
                                       **{k: label[k] for k in ("source", "value", "confidence", "reasons", "model")}})
        finally:
            if not dry_run:
                self.ledger.append(provenance)
                self._save_applied()
        return result

    def label(self, value: str | None, *, ids=(), merchant: str = "", note: str = "", check: bool = False) -> dict:
        action = uuid.uuid4().hex[:12]
        base = {"field": FIELD, "value": value, "by": self.by, "action": action, **({"note": note} if note else {})}
        if merchant:
            ids = [i for i, key in self.merchant.items() if key == merchant]
            self.refetch(ids)
            self.ledger.append([{**base, "merchant": merchant, "source": "merchant"}])
        else:
            self.ledger.append([{**base, "activityId": i, "source": "user", **({"check": True} if check else {})} for i in ids])
        return self.sync(ids)

    def accept(self, items: list[dict]) -> dict:
        """Confirm predictions as user labels, keeping the model's confidence and reasons as provenance.

        `items` carry the value the user saw, so a retrain in between cannot change what is confirmed.
        """
        action = uuid.uuid4().hex[:12]
        entries = []
        for item in items:
            row_id, value = item["id"], item["value"]
            if row_id not in self.rows or value not in self.spenders:
                continue
            guess = self.guess[row_id]
            model = {k: guess[k] for k in ("confidence", "reasons", "model")} if guess["value"] == value else {}
            entries.append({"field": FIELD, "activityId": row_id, "value": value, "source": "user", "accepted": True,
                            "by": self.by, "action": action, **model})
        self.ledger.append(entries)
        return self.sync([e["activityId"] for e in entries])

    def categorize(self, category_id: str, *, ids=(), merchant: str = "") -> dict:
        action = uuid.uuid4().hex[:12]
        base = {"field": "category", "value": category_id, "by": self.by, "action": action}
        if merchant:
            pattern = merchant_pattern(merchant)
            ids = [i for i, r in self.rows.items() if self.merchant[i] == merchant or re.search(pattern, r.get("notes") or "")]
            self.refetch(ids)
            previous = {i: None for i in ids if not self.rows[i]["categoryIds"]}
            rule_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"spending-labeler/{merchant}"))
            before = next((r for r in self.client.get("/spending/rules") if r["id"] == rule_id), None)
            rule = self.client.post("/spending/rules/upsert", {
                "id": rule_id, "name": f"Merchant {merchant}", "pattern": pattern, "matchType": "regex",
                "taxonomyId": SPENDING, "categoryId": category_id, "priority": 100, "isGlobal": True,
            })
            self.client.post("/spending/rules/rerun", {"onlyUncategorized": True})
            self.ledger.append([{**base, "merchant": merchant, "source": "merchant", "ruleId": rule["id"],
                                 "previousRule": before, "previous": previous}])
            self.refetch(previous)
            return {"rule": rule["id"], "categorized": sum(1 for i in previous if self.rows[i]["categoryIds"])}
        entries = []
        for row_id in ids:
            previous = (self.rows[row_id]["categoryIds"] or [None])[0]
            self.client.put(f"/spending/activities/{row_id}/assignments", {"taxonomyId": SPENDING, "categoryId": category_id})
            self.rows[row_id]["categoryIds"] = [category_id]
            entries.append({**base, "activityId": row_id, "source": "user", "previous": {row_id: previous}})
        self.ledger.append(entries)
        return {"categorized": len(entries)}

    def undo(self) -> dict:
        entries = self.ledger.last_action(self.by)
        if not entries:
            return {"undone": 0}
        touched = set()
        for entry in entries:
            if entry.get("merchant"):
                touched |= {i for i, key in self.merchant.items() if key == entry["merchant"]}
            touched |= set(entry.get("previous", {})) | ({entry["activityId"]} if entry.get("activityId") else set())
        self.refetch(touched & set(self.rows))
        for entry in entries:
            if entry["field"] == "category":
                if entry.get("previousRule"):
                    keep = ("id", "name", "pattern", "matchType", "taxonomyId", "categoryId", "priority", "isGlobal", "accountId")
                    self.client.post("/spending/rules/upsert", {k: entry["previousRule"].get(k) for k in keep})
                elif entry.get("ruleId"):
                    self.client.delete(f"/spending/rules/{entry['ruleId']}")
                for row_id, previous in entry["previous"].items():
                    if previous:
                        self.client.put(f"/spending/activities/{row_id}/assignments", {"taxonomyId": SPENDING, "categoryId": previous})
                    elif row_id in self.rows and entry["value"] in self.rows[row_id]["categoryIds"]:
                        self.client.delete(f"/spending/activities/{row_id}/assignments/{SPENDING}")
        self.ledger.append([{"field": e["field"], "reverts": e["seq"], "by": self.by} for e in entries])
        if any(e["field"] == "category" for e in entries):
            self.refetch(touched & set(self.rows))
        self.train()
        self.sync(touched & set(self.rows))
        return {"undone": len(entries)}

    # --- Views ---------------------------------------------------------------------------

    def check_ids(self) -> list[str]:
        shared = set(self.config.get("checkAccounts", []))
        pool = sorted((i for i, r in self.rows.items() if r["accountId"] in shared),
                      key=lambda i: hashlib.sha1(i.encode()).hexdigest())
        return pool[: int(self.config.get("checkSize", 100))]

    def unlabeled(self, row_id: str) -> bool:
        label = self.resolve(row_id)
        return not self.foreign_event(row_id) and not (label and label["value"])

    def queue(self, mode: str = "queue") -> list[dict]:
        if mode == "check":
            done = {i for i, _ in self.check}
            return [{"key": f"#{i}", "ids": [i], "value": 0} for i in self.check_ids() if i not in done]
        groups: dict[str, dict] = {}
        for row_id, row in self.rows.items():
            if not self.unlabeled(row_id):
                continue
            key = self.merchant[row_id] or f"#{row_id}"
            group = groups.setdefault(key, {"key": key, "ids": [], "value": 0.0})
            group["ids"].append(row_id)
            group["value"] += (1 - self.guess[row_id]["confidence"]) * abs(float(row.get("amount") or 0))
        defer = [re.compile(p) for p in self.config.get("deferMerchants", [])]
        later = lambda key: any(p.search(key) for p in defer)
        return sorted(groups.values(), key=lambda g: (later(g["key"]), -g["value"]))

    def view(self, row_id: str, *, blind: bool = False) -> dict:
        row = self.rows[row_id]
        label = self.resolve(row_id)
        guess = self.guess[row_id]
        return {
            "id": row_id, "date": row["activityDate"][:10], "amount": float(row.get("amount") or 0),
            "type": row["activityType"], "account": self.accounts.get(row["accountId"], {}).get("name", ""),
            "notes": row.get("notes") or "", "merchant": self.merchant[row_id],
            "category": ", ".join(self.categories.get(c, {}).get("name", c) for c in row["categoryIds"]),
            "needWant": self.need_want(row),
            "spender": label["value"] if label else None,
            "source": label["source"] if label else None,
            "confidence": label.get("confidence") if label else None,
            "reasons": label.get("reasons", []) if label else [],
            "note": label.get("note") if label else None,
            "guess": None if blind else {k: guess[k] for k in ("value", "confidence", "reasons")},
        }

    def status(self) -> dict:
        counts: dict[str, int] = {}
        for row_id in self.rows:
            label = self.resolve(row_id)
            if label is None or label["value"] is None:
                band = "unlabeled"
            elif label["source"] == "model":
                band = f"model {int(label['confidence'] * 10) / 10:.1f}+"
            else:
                band = label["source"]
            counts[band] = counts.get(band, 0) + 1
        return {"rows": len(self.rows), "labels": dict(sorted(counts.items())), "model": self.version,
                "trainingExamples": len(self.examples), "checkLabeled": len(self.check)}
