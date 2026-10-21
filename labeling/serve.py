"""Loopback JSON API and page for the keyboard-first labeler canvas."""

from __future__ import annotations

import json
import signal
import threading
from collections import Counter
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from importers.simplefin.wealthfolio_client import WealthfolioError
from labeling.workspace import Locked, Workspace, write_lock

PAGE = Path(__file__).with_name("labeler.html")
HOLDER = "labeling canvas"


class Labeler:
    def __init__(self, workspace: Workspace):
        self.ws = workspace
        self.backed_up = False
        self.session = 0
        self.version = 0  # bumps on every write, so open pages can tell their card is stale
        self._summary: dict = {}
        self.notice = ""
        try:
            result = self.write(lambda: self.ws.sync(self.ws.rows))
            self.notice = f"Synced: {result['set']} set, {result['cleared']} cleared, {result['reconciled']} edits found in Wealthfolio"
        except Locked as exc:
            self.notice = f"Wealthfolio is locked by another writer; labels are read-only until it is released ({exc})"

    def write(self, change):
        """Change Wealthfolio under the shared lock, then let the retrained model catch up."""
        with write_lock(self.ws.data_dir, HOLDER):
            self.ws.ensure_fresh()
            if not self.backed_up:
                self.ws.client.post("/utilities/database/backup", {})
                self.backed_up = True
            try:
                result = change()
                self.ws.train()
                self.ws.sync(self.ws.rows)
            finally:
                self.ws.mark_fresh()
                self.version += 1
            return result

    def reread(self) -> dict:
        """Pick up changes made outside the labeler (Wealthfolio, other sessions, the private config)."""
        before = self.ws.signature()
        self.ws.load()
        if self.ws.signature() != before:
            self.version += 1
        return {"version": self.version}

    def remove_rule(self, rule_id: str) -> dict:
        """Remove a rule from the Rules view or a card: a Wealthfolio rule, a merchant spender label, or a name."""
        field, _, seq = rule_id.partition(":")
        if field == "spender" and seq.isdigit():
            return self.ws.remove_label(int(seq))
        if field == "merchant-name" and seq.isdigit():
            key = next(e["merchant"] for e in self.ws.ledger.live if e["seq"] == int(seq))
            return self.ws.rename("", row_id=next(i for i, k in self.ws.merchant.items() if k == key))
        return self.ws.remove_rule(rule_id)

    def suggest(self, name: str) -> dict:
        """A clean name to pre-fill: the SimpleFIN payee if any row has one, else a private export's merchant name."""
        ws, rows = self.ws, [self.ws.rows[i] for i, shown in self.ws.display.items() if shown == name]
        for lookup in ((ws.raw.get(r.get("idempotencyKey") or "") or {}).get("payee") for r in rows), \
                      (ws.export_names.get(r.get("idempotencyKey") or "") for r in rows):
            votes = Counter(filter(None, lookup))
            if votes:
                return {"suggestion": votes.most_common(1)[0][0], "names": sorted(set(ws.names.values()))}
        return {"suggestion": name, "names": sorted(set(ws.names.values()))}

    def card(self, mode: str, skip: set[str], order: str = "impact", key: str = "") -> dict:
        """The next card in the queue, or with `key` the card just labeled, so it stays on screen."""
        queue = self.ws.queue(mode, order)
        group = next((g for g in queue if g["key"] not in skip), None)
        if key:
            ids = [key[1:]] if key.startswith("#") and key[1:] in self.ws.rows else [
                i for i, k in self.ws.display.items() if k == key and self.ws.rows[i].get("visibleSpendingAmount", 1) != 0]
            group = next((g for g in queue if g["key"] == key), {"key": key, "ids": ids}) if ids else None
            if group:
                group = {**group, "ids": ids}
        rows = self.ws.rows
        counts = {
            "groups": len(queue), "rows": sum(len(g["ids"]) for g in queue),
            "amount": round(sum(abs(float(rows[i].get("amount") or 0)) for g in queue for i in g["ids"]), 2),
            "session": self.session, "total": len(rows), "notice": self.notice, "version": self.version,
        }
        if group is None:
            return {"card": None, "counts": counts}
        blind = mode == "check"
        ids = sorted(group["ids"], key=lambda i: rows[i]["activityDate"], reverse=True)
        views = [self.ws.view(i, blind=blind) for i in ids]
        merchant = "" if group["key"].startswith("#") else group["key"]
        others = [i for i, key in self.ws.display.items() if merchant and key == merchant and i not in group["ids"]]
        return {"counts": counts, "card": {
            "key": group["key"], "merchant": merchant, "blind": blind,
            "keys": self.ws.keys_of(merchant) if merchant else [],
            "rows": views[:60], "more": max(0, len(ids) - 60),
            # A card is complete when every row has a spender and a category (which gives need/want).
            "missing": {"spender": sum(not v["spender"] for v in views), "category": 0 if blind else sum(not v["category"] for v in views)},
            "labeledElsewhere": len(others),
            "total": round(sum(abs(float(rows[i].get("amount") or 0)) for i in ids), 2),
            # The merchant's predictions and the same confidence the queue sorts by; hidden for blind checks.
            "summary": None if blind else {
                "predicted": Counter(self.ws.guess[i]["value"] for i in ids).most_common(),
                "confidence": round(group["confidence"][0], 3) if "confidence" in group else None,
            },
            # Labels made by hand; defaults show only as each row's source tag.
            "rules": [] if blind else self.ws.card_rules(ids),
            "labeled": Counter(v["spender"] for v in views if v["spender"] and v["source"] in ("user", "merchant")).most_common(),
        }}

    def audit(self, query: dict) -> list[dict]:
        """Labeled rows, filtered by who made the spender or category label (you, an agent, a model, Wealthfolio)."""
        origin = query.get("origin", "")
        low, high = float(query.get("min") or 0), float(query.get("max") or 1)
        text = (query.get("q") or "").upper()
        found = []
        for row_id in self.ws.rows:
            item = self.ws.view(row_id)
            if origin and origin not in (item.get("spenderOrigin"), item.get("categoryOrigin")):
                continue
            if not (item["spender"] or item["category"]) or (item["confidence"] is not None and not low <= item["confidence"] <= high):
                continue
            if text and text not in (item["notes"] + item["account"] + item["name"]).upper():
                continue
            found.append(item)
        found.sort(key=lambda i: (i["confidence"] if i["confidence"] is not None else 1.0, i["name"], i["date"]))
        return found[:1000]

    def origins(self) -> list[str]:
        views = (self.ws.view(i) for i in self.ws.rows)
        return sorted({o for v in views for o in (v.get("spenderOrigin"), v.get("categoryOrigin")) if o})

    def summary(self) -> dict:
        """The model version, its evaluation, and how many labels and merchants teach each spender."""
        ws = self.ws
        if self._summary.get("model") != ws.version:
            merchants = {value: set() for value in ws.spenders}
            counts = dict.fromkeys(ws.spenders, 0)
            for row_id, _, value, _ in ws.examples:
                counts[value] += 1
                merchants[value].add(ws.merchant[row_id])
            classes = {value: {"examples": counts[value], "merchants": len(merchants[value])} for value in ws.spenders}
            self._summary = {**ws.evaluate(), "classes": classes, "autoApply": ws.auto_apply, "warnings": [
                f"{value} is barely learned: {c['examples']} label(s) from {c['merchants']} merchant(s), so its "
                "predictions are unreliable until you label more of it." for value, c in classes.items() if c["merchants"] < 20
            ]}
        return self._summary

    SORTS = {
        "date": lambda ws, i: ws.rows[i]["activityDate"],
        "description": lambda ws, i: (ws.rows[i].get("notes") or "").upper(),
        "account": lambda ws, i: ws.accounts.get(ws.rows[i]["accountId"], {}).get("name", ""),
        "amount": lambda ws, i: abs(float(ws.rows[i].get("amount") or 0)),
        "category": lambda ws, i: ws.view(i)["category"],
        "needWant": lambda ws, i: ws.view(i)["needWant"] or "",
        "value": lambda ws, i: ws.guess[i]["value"],
        "confidence": lambda ws, i: ws.guess[i]["rank"],
    }

    def predictions(self, query: dict) -> dict:
        """Every unlabeled row outside the blind check set, scored by the current model; one page of it."""
        ws = self.ws
        text = (query.get("q") or "").upper()
        value = query.get("value") or ""
        low, high = float(query.get("min") or 0), float(query.get("max") or 1)
        held = set(ws.check_ids())
        ids = [
            i for i, row in ws.rows.items()
            if i not in held and ws.unlabeled(i)
            and (not value or ws.guess[i]["value"] == value) and low <= ws.guess[i]["confidence"] <= high
            and (not text or text in f"{ws.display[i]} {row.get('notes') or ''} {self.SORTS['account'](ws, i)}".upper())
        ]
        key = self.SORTS[query.get("sort") or "confidence"]
        ids.sort(key=lambda i: key(ws, i), reverse=query.get("dir") == "desc")
        offset, limit = int(query.get("offset") or 0), min(int(query.get("limit") or 100), 500)
        return {"total": len(ids), "rows": [ws.view(i) for i in ids[offset:offset + limit]],
                "version": self.version, "threshold": ws.threshold, "model": self.summary()}

    def handle(self, method: str, path: str, query: dict, body: dict):
        ws = self.ws
        if path == "/api/config":
            return {"spenders": ws.config["spenders"], "threshold": ws.threshold, "model": ws.version}
        if path == "/api/version":
            return {"version": self.version}
        if path == "/api/refresh":
            return self.reread()
        if path == "/api/card":
            return self.card(query.get("mode", "queue"), set(filter(None, query.get("skip", "").split("\n"))),
                             query.get("order", "impact"), query.get("key", ""))
        if path == "/api/categories":
            names = {i: c["name"] for i, c in ws.categories.items()}
            return [{"id": i, "name": c["name"], "parent": names.get(c.get("parentId") or "", ""),
                     "needWant": ws.need_want({"categoryIds": [i]})} for i, c in ws.categories.items()]
        if path == "/api/audit":
            return self.audit(query)
        if path == "/api/origins":
            return self.origins()
        if path == "/api/rules":
            return ws.rules_list()
        if path == "/api/predictions":
            return self.predictions(query)
        if path == "/api/category-suggest":
            ids = [query["id"]] if query.get("id") else [i for i, shown in ws.display.items() if shown == query.get("merchant")]
            category, why = ws.suggest_category(ids, query.get("needWant") or None)
            return {"categoryId": category, "why": why, "rows": len(ids)}
        if path == "/api/suggest":
            return self.suggest(query.get("merchant", ""))
        if path == "/api/history":
            row_id = query["id"]
            return ws.ledger.history(row_id, ws.merchant.get(row_id, ""))
        if method != "POST":
            raise KeyError(path)
        if path == "/api/label" and body.get("accept"):
            result = self.write(lambda: ws.accept(body["accept"]))
        elif path == "/api/label":
            result = self.write(lambda: ws.label(body.get("value"), ids=body.get("ids", []), merchant=body.get("merchant", ""),
                                                 note=body.get("note", ""), check=bool(body.get("check"))))
        elif path == "/api/rules/remove":
            result = self.write(lambda: self.remove_rule(body["id"]))
        elif path == "/api/rename":
            result = self.write(lambda: ws.rename(body.get("name", ""), merchant=body.get("merchant", ""), row_id=body.get("id", "")))
        elif path == "/api/category":
            result = self.write(lambda: ws.categorize(body["categoryId"], ids=body.get("ids", []), merchant=body.get("merchant", "")))
        elif path == "/api/undo":
            result = self.write(ws.undo)
            self.session = max(0, self.session - 1)
        else:
            raise KeyError(path)
        if path != "/api/undo":
            self.session += 1
        return result


def make_server(workspace: Workspace, port: int = 0) -> HTTPServer:
    app = Labeler(workspace)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, payload, kind="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _route(self, method: str):
            # Only this page may drive the API: block DNS rebinding and cross-site form posts.
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_address[1]}":
                return self._send(403, {"error": "unexpected Host"})
            if method == "POST" and self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._send(415, {"error": "JSON only"})
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/api/shutdown" and method == "POST":
                # Requests are served one at a time, so any write in flight has finished and released the lock.
                self._send(200, {})
                return threading.Thread(target=self.server.shutdown, daemon=True).start()
            if url.path == "/":
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            query = dict(urllib.parse.parse_qsl(url.query))
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if method == "POST" else {}
            try:
                self._send(200, app.handle(method, url.path, query, body))
            except Locked as exc:
                self._send(423, {"error": f"Wealthfolio is locked by another writer: {exc}"})
            except KeyError as exc:
                self._send(404, {"error": f"unknown {exc}"})
            except (WealthfolioError, ValueError) as exc:
                self._send(400, {"error": str(exc)})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return HTTPServer(("127.0.0.1", port), Handler)


def serve(workspace: Workspace, port: int = 0) -> None:
    server = make_server(workspace, port)

    def stop(*_):
        raise SystemExit(0)  # unwinds a write in progress, which releases the lock
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    print(f"http://127.0.0.1:{server.server_address[1]}/", flush=True)
    server.serve_forever()