"""Load spending rows from Wealthfolio, resolve each row's spender, and write labels back.

Wealthfolio holds the visible label (a spending event per spender, and categories whose
budget group gives need or want). The ledger holds why. Nothing here edits an activity
row, so amounts, dates, transfer links, and balances cannot change.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import socket
import uuid
from collections import Counter
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
NAME = "merchant-name"  # ledger field: a display name for a raw merchant key
RULE_FIELDS = ("id", "name", "pattern", "matchType", "taxonomyId", "categoryId", "activityType", "amountOp", "amountValue",
               "amountValue2", "priority", "isGlobal", "accountId", "presetId", "presetRuleKey", "presetVersion")
AMOUNT_OPS = {"eq": lambda a, v, w: a == v, "gt": lambda a, v, w: a > v, "gte": lambda a, v, w: a >= v,
              "lt": lambda a, v, w: a < v, "lte": lambda a, v, w: a <= v,
              "between": lambda a, v, w: w is not None and v <= a <= w}


def rule_matches(rule: dict, row: dict, notes: str) -> bool:
    """Wealthfolio 3.10's categorization matcher (`match_compiled`), for one rule and one activity."""
    if not rule.get("isGlobal") and rule.get("accountId") != row["accountId"]:
        return False
    if rule.get("activityType") and rule["activityType"] != row["activityType"]:
        return False
    if rule.get("amountOp"):
        if rule.get("amountValue") is None or not AMOUNT_OPS[rule["amountOp"]](
                abs(float(row.get("amount") or 0)), float(rule["amountValue"]),
                None if rule.get("amountValue2") is None else float(rule["amountValue2"])):
            return False
    pattern, kind = rule.get("pattern") or "", rule.get("matchType")
    if kind == "regex":
        try:
            return re.search(pattern, notes) is not None
        except re.error:
            return False  # Rust-only syntax; Wealthfolio would evaluate it, so this is a best effort
    upper, wanted = notes.upper(), pattern.upper()
    return {"contains": wanted in upper, "starts_with": upper.startswith(wanted), "exact": upper == wanted}.get(kind, False)
TRAIN_WEIGHT = {"user": 1.0, "wealthfolio-ui": 1.0, "merchant": 1.0, "cardmember": 0.3, "account": 0.3}


class Locked(RuntimeError):
    pass


def running(pid: int) -> bool:
    """Whether a process exists on this host. Unknowable answers count as running."""
    if os.name == "nt":  # os.kill would terminate the process on Windows
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such process
        code = ctypes.c_ulong()
        alive = not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259  # STILL_ACTIVE
        kernel.CloseHandle(handle)
        return alive
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def abandoned(text: str) -> bool:
    """A lock left by a process that died on this host. Locks from another host (say the sync container,
    whose PIDs mean nothing here) or without a host are never judged abandoned."""
    try:
        lock = json.loads(text)
        return lock.get("host") == socket.gethostname() and isinstance(lock.get("pid"), int) and not running(lock["pid"])
    except ValueError:
        return False


@contextmanager
def write_lock(data_dir: Path, holder: str):
    """The shared write lock: exclusive create, and only ever remove our own or an abandoned one."""
    path = data_dir / LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    token = json.dumps({"holder": holder, "host": socket.gethostname(), "pid": os.getpid(), "at": now()})
    for attempt in range(2):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            held = path.read_text(encoding="utf-8", errors="replace")
            if attempt or not abandoned(held):
                raise Locked(held[:300]) from None
            try:
                if path.read_text(encoding="utf-8", errors="replace") == held:  # not someone's fresh lock
                    path.unlink()
            except FileNotFoundError:
                pass  # exclusive create below still decides between two reclaimers
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
    """One unpaged read (about half a second); the paged search costs over a second per 1,000 rows."""
    return [row for row in client.get("/spending/cash-activities") if row["cashFlowBucket"] == "spending"]


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
        self.raw = raw_index(data_dir)
        self.zone = client.display_timezone()
        self.load()

    # --- Reading -------------------------------------------------------------------------

    def _stamp(self) -> tuple:
        paths = (self.dir / "ledger.jsonl", self.dir / "applied.json")
        return tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in paths)

    def load(self) -> None:
        """Read the private config, ledger, and Wealthfolio rows, then retrain."""
        self.config = json.loads((self.dir / "config.json").read_text(encoding="utf-8"))
        self.spenders: list[str] = list(self.config["spenders"].values())
        self.threshold = float(self.config.get("threshold", 0.9))
        self.descriptions, self.export_names = self._exports()
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
        for row in spending_rows(self.client):
            if row["id"] in wanted and row["id"] in self.rows:
                current = self.rows[row["id"]]
                current["eventId"] = row.get("eventId")
                current["categoryIds"] = self._categories(row)
                current["assignments"], current["splits"] = row["assignments"], row["splits"]
                self.features[row["id"]] = self._row_features(row["id"])

    def signature(self) -> int:
        """Changes whenever anything the labeler shows or decides from changes."""
        return hash((json.dumps(self.config, sort_keys=True), self._stamp(), tuple(
            (i, r.get("updatedAt"), r.get("eventId"), r.get("visibleSpendingAmount"), tuple(r["categoryIds"]))
            for i, r in sorted(self.rows.items()))))

    def _exports(self) -> tuple[dict[str, str], dict[str, str]]:
        """From private exports, keyed like idempotency keys: original descriptions (for rows with empty
        notes) and, when the export has a merchant column, merchant names to suggest when renaming."""
        descriptions, names = {}, {}
        for source in self.config.get("descriptionFiles", []):
            path = self.data_dir / source["file"]
            if path.exists():
                with path.open(encoding="utf-8-sig", newline="") as handle:
                    for line in csv.DictReader(handle):
                        key = source["key"].format(**line)
                        if (line.get(source["text"]) or "").strip():
                            descriptions[key] = line[source["text"]].strip()
                        if (line.get(source.get("merchant", "")) or "").strip():
                            names[key] = line[source["merchant"]].strip()
        return descriptions, names

    def match_rules(self) -> None:
        """Which native rule Wealthfolio would apply to each row (it records only that *a* rule did)."""
        self.rules = sorted(self.client.get("/spending/rules"), key=lambda r: -r.get("priority", 0))
        self.rule_by_id = {r["id"]: r for r in self.rules}
        self.native = {}
        for row_id, row in self.rows.items():
            rule = next((r for r in self.rules if r.get("categoryId") and rule_matches(r, row, row["sourceNotes"])), None)
            if rule:
                self.native[row_id] = rule["id"]

    def _identify(self) -> None:
        """Each row's merchant identity: the name given in the labeler, else its raw key."""
        self.names = {key: entries[-1]["value"] for (field, key), entries in self.ledger.by_merchant.items()
                      if field == NAME and entries[-1]["value"]}
        self.display = {i: self.names.get(key, key) for i, key in self.merchant.items()}
        self.features = {i: self._row_features(i) for i in self.rows}

    def _row_features(self, row_id: str) -> list[str]:
        row = self.rows[row_id]
        return features(row, self.raw.get(row.get("idempotencyKey") or ""), self.zone, self.display[row_id].upper())

    def keys_of(self, name: str) -> list[str]:
        """The raw merchant keys shown under a display name (a raw key is its own name until renamed)."""
        return sorted({self.merchant[i] for i, shown in self.display.items() if shown == name and self.merchant[i]}) or [name]

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
            row["sourceNotes"] = row.get("notes") or ""  # what Wealthfolio's rules see
            if not row["sourceNotes"].strip():
                row["notes"] = self.descriptions.get(row.get("idempotencyKey") or "")
            self.rows[row["id"]] = row
        self.merchant = {i: merchant_key(r.get("notes")) for i, r in self.rows.items()}
        self._identify()
        self.cardmember = self._cardmembers()
        self.match_rules()
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
        if not training and self.auto_apply and row_id in self.guess and self.guess[row_id]["confidence"] >= self.threshold:
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
            value, confidence, lift, raw = self.model.predict(found)
            self.guess[row_id] = {
                "source": "model", "value": value, "confidence": confidence, "model": self.version,
                "rank": (confidence, raw),
                "reasons": [f"{describe(f, names)} (seen {count}x as {value})" for f, _, count in lift],
            }
        self.examples, self.check = examples, check
        # Calibration comes from personal accounts; only the blind shared-card check set can show the
        # model is safe to trust above the threshold, so it applies nothing until that evidence exists.
        sure = [self.guess[i]["value"] == truth for i, truth in check if self.guess[i]["confidence"] >= self.threshold]
        self.auto_apply = len(sure) >= int(self.config.get("minCheckLabels", 30)) and (not sure or sum(sure) / len(sure) >= 0.95)

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

    def repeats(self, entries: list[dict], entry: dict) -> bool:
        """A repeated decision (say a double key press) changes nothing, so it is not recorded again."""
        mine = [e for e in entries if e["source"] == entry["source"]]
        return bool(mine) and all(mine[-1].get(k) == entry.get(k) for k in ("value", "note", "check"))

    def label(self, value: str | None, *, ids=(), merchant: str = "", note: str = "", check: bool = False) -> dict:
        action = uuid.uuid4().hex[:12]
        base = {"field": FIELD, "value": value, "by": self.by, "action": action, **({"note": note} if note else {})}
        if merchant:
            keys = self.keys_of(merchant)
            ids = [i for i, key in self.merchant.items() if key in keys]
            self.refetch(ids)
            entries = [entry for entry in ({**base, "merchant": key, "source": "merchant"} for key in keys)
                       if not self.repeats(self.ledger.by_merchant.get((FIELD, entry["merchant"]), []), entry)]
        else:
            entries = [entry for entry in ({**base, "activityId": i, "source": "user", **({"check": True} if check else {})} for i in ids)
                       if not self.repeats(self.ledger.by_activity.get((FIELD, entry["activityId"]), []), entry)]
        self.ledger.append(entries)
        return {**self.sync(ids), "recorded": len(entries)}

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
            entry = {"field": FIELD, "activityId": row_id, "value": value, "source": "user", "accepted": True,
                     "by": self.by, "action": action, **model}
            if not self.repeats(self.ledger.by_activity.get((FIELD, row_id), []), entry):
                entries.append(entry)
        self.ledger.append(entries)
        return self.sync([e["activityId"] for e in entries])

    def suggest_category(self, ids, need_want: str | None = None) -> tuple[str | None, str]:
        """A built-in category to pre-select, and why: one already used on this merchant, else the one most
        used on rows sharing a word with it, else the first top-level category of that need/want group."""
        fits = lambda c: need_want is None or self.need_want({"categoryIds": [c]}) == need_want
        keys = {self.merchant[i] for i in ids if self.merchant[i]}
        same = [i for i, key in self.merchant.items() if key in keys] or list(ids)
        words = {f for i in ids for f in self.features[i] if f.startswith("w:")}
        similar = [i for i, found in self.features.items() if i not in same and words.intersection(found)]
        for rows, why in ((same, "already used on this merchant"), (similar, "most used on merchants with the same words")):
            votes = Counter(c for i in rows for c in self.rows[i]["categoryIds"] if fits(c))
            if votes:
                return votes.most_common(1)[0][0], why
        top = sorted((c for c in self.categories.values() if not c.get("parentId") and fits(c["id"])), key=lambda c: c.get("sortOrder", 0))
        return (top[0]["id"], f"first {need_want} category" if need_want else "first category") if top else (None, "")

    def categorize(self, category_id: str, *, ids=(), merchant: str = "") -> dict:
        action = uuid.uuid4().hex[:12]
        base = {"field": "category", "value": category_id, "by": self.by, "action": action}
        if merchant:
            # One native rule per raw key, so renaming or merging never changes what a rule matches.
            existing = {r["id"]: r for r in self.client.get("/spending/rules")}
            entries, touched, kept = [], {}, 0
            for key in self.keys_of(merchant):
                pattern = merchant_pattern(key)
                ids = [i for i, r in self.rows.items() if self.merchant[i] == key or re.search(pattern, r.get("notes") or "")]
                self.refetch(ids)
                previous = {i: None for i in ids if not self.rows[i]["categoryIds"]}
                # Rules only fill empty categories; say how many of this merchant's rows keep another one.
                kept += sum(1 for i in ids if self.merchant[i] == key and self.rows[i]["categoryIds"]
                            and category_id not in self.rows[i]["categoryIds"])
                rule_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"spending-labeler/{key}"))
                rule = self.client.post("/spending/rules/upsert", {
                    "id": rule_id, "name": f"Merchant {key}", "pattern": pattern, "matchType": "regex",
                    "taxonomyId": SPENDING, "categoryId": category_id, "priority": 100, "isGlobal": True,
                })
                entries.append({**base, "merchant": key, "source": "merchant", "ruleId": rule["id"],
                                "previousRule": existing.get(rule_id), "previous": previous})
                touched.update(previous)
            self.client.post("/spending/rules/rerun", {"onlyUncategorized": True})
            self.ledger.append(entries)
            self.refetch(touched)
            self.match_rules()
            return {"rules": [e["ruleId"] for e in entries], "kept": kept,
                    "categorized": sum(1 for i in touched if self.rows[i]["categoryIds"])}
        entries = []
        for row_id in ids:
            previous = (self.rows[row_id]["categoryIds"] or [None])[0]
            assigned = self.client.put(f"/spending/activities/{row_id}/assignments", {"taxonomyId": SPENDING, "categoryId": category_id})
            self.rows[row_id]["categoryIds"] = [category_id]
            self.rows[row_id]["assignments"] = [assigned if isinstance(assigned, dict) and assigned.get("categoryId")
                                                else {"taxonomyId": SPENDING, "categoryId": category_id, "source": "manual"}]
            entries.append({**base, "activityId": row_id, "source": "user", "previous": {row_id: previous}})
        self.ledger.append(entries)
        return {"categorized": len(entries)}

    def rename(self, name: str, *, merchant: str = "", row_id: str = "") -> dict:
        """Name a merchant (every raw key under `merchant`) or split one row's raw key out under a new name.

        Only the display name changes: labels and category rules stay keyed by raw key. Keys given the
        same name merge into one merchant; a blank name, or the raw key itself, goes back to the raw key.
        """
        keys = self.keys_of(merchant) if merchant else [self.merchant[row_id]]
        name = " ".join(name.split())
        action = uuid.uuid4().hex[:12]
        entries = [entry for entry in ({"field": NAME, "merchant": key, "value": name if name and name != key else None,
                                         "source": "user", "by": self.by, "action": action} for key in keys if key)
                   if not self.repeats(self.ledger.by_merchant.get((NAME, entry["merchant"]), []), entry)]
        self.ledger.append(entries)
        self._identify()
        self.train()
        return {"recorded": len(entries), "key": self.names.get(keys[0], keys[0])}

    def remove_rule(self, rule_id: str) -> dict:
        """Delete a Wealthfolio rule (undo restores it). Rows it already categorized keep their category."""
        rule = self.rule_by_id[rule_id]
        self.client.delete(f"/spending/rules/{rule_id}")
        self.ledger.append([{"field": "rule-removed", "ruleId": rule_id, "previousRule": rule, "value": None,
                             "source": "user", "by": self.by, "action": uuid.uuid4().hex[:12]}])
        self.match_rules()
        return {"removed": rule_id}

    def remove_label(self, seq: int) -> dict:
        """Withdraw a merchant-wide spender label; rows fall back to their next source (account, model, none)."""
        entry = next(e for e in self.ledger.live if e["seq"] == seq and e.get("merchant") and e["field"] == FIELD)
        self.ledger.append([{"field": FIELD, "reverts": seq, "by": self.by, "note": "removed in the Rules view"}])
        ids = [i for i, key in self.merchant.items() if key == entry["merchant"]]
        self.train()
        return self.sync(ids)

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
            if entry["field"] == "rule-removed":
                self.client.post("/spending/rules/upsert", {k: entry["previousRule"].get(k) for k in RULE_FIELDS})
            if entry["field"] == "category":
                if entry.get("previousRule"):
                    self.client.post("/spending/rules/upsert", {k: entry["previousRule"].get(k) for k in RULE_FIELDS})
                elif entry.get("ruleId"):
                    self.client.delete(f"/spending/rules/{entry['ruleId']}")
                for row_id, previous in entry["previous"].items():
                    if previous:
                        self.client.put(f"/spending/activities/{row_id}/assignments", {"taxonomyId": SPENDING, "categoryId": previous})
                    elif row_id in self.rows and entry["value"] in self.rows[row_id]["categoryIds"]:
                        self.client.delete(f"/spending/activities/{row_id}/assignments/{SPENDING}")
        self.ledger.append([{"field": e["field"], "reverts": e["seq"], "by": self.by} for e in entries])
        if any(e["field"] == NAME for e in entries):
            self._identify()
        if any(e["field"] in ("category", "rule-removed") for e in entries):
            self.match_rules()
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

    def incomplete(self, row_id: str) -> bool:
        """Still needs work: no spender, or a spender set by hand but no category (so no need/want yet).

        Account and cardmember defaults without a category are not queued; there are thousands.
        """
        if self.unlabeled(row_id):
            return True
        row = self.rows[row_id]
        if row.get("visibleSpendingAmount", 1) == 0 or self.foreign_event(row_id) or row["categoryIds"]:
            return False
        return self.resolve(row_id)["source"] in ("user", "merchant", "wealthfolio-ui")

    def unlabeled(self, row_id: str) -> bool:
        """Needs a spender: counts as spending (Wealthfolio's excluded categories do not) and has none."""
        if self.rows[row_id].get("visibleSpendingAmount", 1) == 0:
            return False
        label = self.resolve(row_id)
        return not self.foreign_event(row_id) and not (label and label["value"])

    # Queue orders: the merchant aggregate to sort by, and whether larger comes first.
    ORDERS = {
        "impact": ("value", True), "low-confidence": ("confidence", False), "high-confidence": ("confidence", True),
        "newest": ("last", True), "oldest": ("first", False), "amount": ("total", True), "merchant": ("key", False),
    }

    def queue(self, mode: str = "queue", order: str = "impact") -> list[dict]:
        if mode == "check":
            done = {i for i, _ in self.check}
            return [{"key": f"#{i}", "ids": [i], "value": 0} for i in self.check_ids() if i not in done]
        groups: dict[str, dict] = {}
        for row_id, row in self.rows.items():
            if not self.incomplete(row_id):
                continue
            key = self.display[row_id] or f"#{row_id}"
            day, amount, (confidence, raw) = row["activityDate"][:10], abs(float(row.get("amount") or 0)), self.guess[row_id]["rank"]
            group = groups.setdefault(key, {"key": key, "ids": [], "value": 0.0, "total": 0.0, "weighted": [0.0, 0.0, 0.0],
                                            "first": day, "last": day})
            group["ids"].append(row_id)
            group.setdefault("keys", set()).add(self.merchant[row_id] or key)
            group["value"] += (1 - confidence) * amount if self.unlabeled(row_id) else amount
            group["total"] += amount
            weight = max(amount, 0.01)
            group["weighted"] = [group["weighted"][0] + weight, group["weighted"][1] + weight * confidence, group["weighted"][2] + weight * raw]
            group["first"], group["last"] = min(group["first"], day), max(group["last"], day)
        for group in groups.values():
            # A merchant's confidence: the amount-weighted mean over its rows, calibrated first, raw as tie-break.
            weight, confidence, raw = group.pop("weighted")
            group["confidence"] = (confidence / weight, raw / weight)
        field, descending = self.ORDERS.get(order, self.ORDERS["impact"])
        ordered = sorted(groups.values(), key=lambda g: g[field], reverse=descending)
        defer = [re.compile(p) for p in self.config.get("deferMerchants", [])]
        return sorted(ordered, key=lambda g: any(p.search(k) for p in defer for k in g["keys"]))

    def view(self, row_id: str, *, blind: bool = False) -> dict:
        row = self.rows[row_id]
        label = self.resolve(row_id)
        guess = self.guess[row_id]
        return {
            "id": row_id, "date": row["activityDate"][:10], "amount": float(row.get("amount") or 0),
            "type": row["activityType"], "account": self.accounts.get(row["accountId"], {}).get("name", ""),
            "notes": row.get("notes") or "", "merchant": self.merchant[row_id], "name": self.display[row_id],
            "category": ", ".join(self.categories.get(c, {}).get("name", c) for c in row["categoryIds"]),
            "needWant": self.need_want(row),
            "spender": label["value"] if label else None,
            "source": label["source"] if label else None,
            **self.explain(row_id, label),
            "confidence": label.get("confidence") if label else None,
            "reasons": label.get("reasons", []) if label else [],
            "note": label.get("note") if label else None,
            "guess": None if blind else {k: guess[k] for k in ("value", "confidence", "reasons")},
        }

    def explain(self, row_id: str, label: dict | None) -> dict:
        """Plain-language sources for a row's spender and category, with details for a tooltip."""
        found = {}
        if label:
            source, when = label["source"], f" · set {label['at'][:10]}" if label.get("at") else ""
            found["spenderBy"] = {
                "merchant": f"all {self.display[row_id]}", "user": "this row (accepted prediction)" if label.get("accepted") else "this row",
                "wealthfolio-ui": "set in Wealthfolio", "account": "account default", "cardmember": "cardmember on statement",
                "model": f"model {round((label.get('confidence') or 0) * 100)}%",
            }[source]
            found["spenderWhy"] = {
                "merchant": f"Labeler rule: every {self.display[row_id]} charge, past and future; applied after each daily sync",
                "user": "Labeled by hand for this row only", "wealthfolio-ui": "Edited in Wealthfolio, which wins over the labeler",
                "account": "Default for this account", "cardmember": "The card issuer's file names the cardmember",
                "model": "The model's prediction",
            }[source] + when + "".join(f" · {r}" for r in label.get("reasons", [])) + (f" · note: {label['note']}" if label.get("note") else "")
        row = self.rows[row_id]
        assignment = next((a for a in row["assignments"] if a["taxonomyId"] == SPENDING), None)
        if row["splits"]:
            found["categoryBy"], found["categoryWhy"] = "split", "Split across categories in Wealthfolio"
        elif assignment:
            rule = self.rule_by_id.get(self.native.get(row_id, ""))
            if assignment.get("source") == "rule":
                ours = rule and rule["categoryId"] == assignment["categoryId"]
                found["categoryBy"] = ("preset" if rule.get("presetId") else "rule") if ours else "rule"
                found["categoryWhy"] = (f"Wealthfolio rule '{rule['name']}' ({self.rule_pattern(rule)}), applied by Wealthfolio on import"
                                        if ours else "Set by a Wealthfolio rule that no longer matches this row")
            else:
                found["categoryBy"] = assignment.get("source") or "manual"
                found["categoryWhy"] = "Set by hand, in Wealthfolio or the labeler" if found["categoryBy"] == "manual" else f"Source: {found['categoryBy']}"
        return found

    def rule_pattern(self, rule: dict) -> str:
        """How a rule matches, in words: labeler rules by merchant, others by their own pattern."""
        key = next((e["merchant"] for e in self.ledger.live if e["field"] == "category" and e.get("ruleId") == rule["id"]), None)
        return f"merchant {key}" if key else f"{rule['matchType']} {rule['pattern']}"

    def rules_list(self) -> list[dict]:
        """Every rule that decides labels: Wealthfolio's category rules (with presets) and the labeler's merchant rules."""
        mine = {e.get("ruleId") for e in self.ledger.live if e["field"] == "category" and e.get("ruleId")}
        matched = Counter(self.native.values())
        found = [{
            "id": r["id"], "kind": "Category", "sets": self.categories.get(r.get("categoryId") or "", {}).get("name", r.get("categoryId")),
            "pattern": self.rule_pattern(r), "rows": matched[r["id"]], "name": r["name"],
            "source": "labeler (in Wealthfolio)" if r["id"] in mine else "preset" if r.get("presetId") else "Wealthfolio",
            "where": "Wealthfolio", "at": (r.get("updatedAt") or r.get("createdAt") or "")[:10],
        } for r in self.rules if r.get("categoryId")]
        for (field, key), entries in self.ledger.by_merchant.items():
            if field in (FIELD, NAME) and entries[-1].get("value"):
                entry = entries[-1]
                found.append({
                    "id": f"{field}:{entry['seq']}", "kind": "Spender" if field == FIELD else "Name", "sets": entry["value"],
                    "pattern": f"merchant {key}", "rows": sum(1 for k in self.merchant.values() if k == key), "name": self.names.get(key, key),
                    "source": "labeler", "where": "labeler", "at": entry["at"][:10], "merchant": key,
                })
        return found

    def card_rules(self, ids) -> list[dict]:
        keys, rules = {self.merchant[i] for i in ids}, {self.native[i] for i in ids if i in self.native}
        return [r for r in self.rules_list() if r.get("merchant") in keys or r["id"] in rules]

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
