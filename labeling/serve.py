"""Loopback JSON API and page for the keyboard-first labeler canvas."""

from __future__ import annotations

import json
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

    def card(self, mode: str, skip: set[str]) -> dict:
        queue = self.ws.queue(mode)
        group = next((g for g in queue if g["key"] not in skip), None)
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
        merchant = "" if group["key"].startswith("#") else group["key"]
        others = [i for i, key in self.ws.merchant.items() if merchant and key == merchant and i not in group["ids"]]
        return {"counts": counts, "card": {
            "key": group["key"], "merchant": merchant, "blind": blind,
            "rows": [self.ws.view(i, blind=blind) for i in ids[:60]], "more": max(0, len(ids) - 60),
            "labeledElsewhere": len(others),
            "total": round(sum(abs(float(rows[i].get("amount") or 0)) for i in ids), 2),
        }}

    def audit(self, query: dict) -> list[dict]:
        source = query.get("source", "")
        low, high = float(query.get("min") or 0), float(query.get("max") or 1)
        text = (query.get("q") or "").upper()
        found = []
        for row_id in self.ws.rows:
            item = self.ws.view(row_id)
            conf = item["confidence"] if item["source"] == "model" else 1.0
            if source == "unlabeled":
                if item["spender"]:
                    continue
                conf = item["guess"]["confidence"]
            elif source and item["source"] != source:
                continue
            if not low <= conf <= high or (text and text not in (item["notes"] + item["account"]).upper()):
                continue
            found.append(item)
        found.sort(key=lambda i: (i["confidence"] if i["confidence"] is not None else 1.0, i["date"]))
        return found[:300]

    def handle(self, method: str, path: str, query: dict, body: dict):
        ws = self.ws
        if path == "/api/config":
            return {"spenders": ws.config["spenders"], "threshold": ws.threshold, "model": ws.version}
        if path == "/api/version":
            return {"version": self.version}
        if path == "/api/card":
            return self.card(query.get("mode", "queue"), set(filter(None, query.get("skip", "").split("\n"))))
        if path == "/api/categories":
            names = {i: c["name"] for i, c in ws.categories.items()}
            return [{"id": i, "name": c["name"], "parent": names.get(c.get("parentId") or "", ""),
                     "needWant": ws.need_want({"categoryIds": [i]})} for i, c in ws.categories.items()]
        if path == "/api/audit":
            return self.audit(query)
        if path == "/api/history":
            row_id = query["id"]
            return ws.ledger.history(row_id, ws.merchant.get(row_id, ""))
        if method != "POST":
            raise KeyError(path)
        if path == "/api/label":
            result = self.write(lambda: ws.label(body.get("value"), ids=body.get("ids", []), merchant=body.get("merchant", ""),
                                                 note=body.get("note", ""), check=bool(body.get("check"))))
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
    print(f"http://127.0.0.1:{server.server_address[1]}/", flush=True)
    server.serve_forever()