"""Spender labeling commands.

    python -m labeling setup    # create the native Spender event type and events
    python -m labeling apply    # reconcile, retrain, and auto-apply labels (also runs daily)
    python -m labeling status   # labels by source and confidence, plus the latest evaluation
    python -m labeling serve    # the labeler canvas backend (prints its loopback URL)

Private inputs and outputs live in <data>/labels/; see docs/runbooks/labeling.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from labeling.workspace import Locked, Workspace, connect, write_lock

FAR_FUTURE = "2099-12-31"


def setup(ws: Workspace) -> dict:
    """Create (or find) the Spender event type and one all-time event per spender."""
    types = {t["name"]: t for t in ws.client.get("/spending/event-types")}
    name = ws.config.get("eventTypeName", "Spender")
    event_type = types.get(name) or ws.client.post("/spending/event-types", {"name": name, "color": "#4F6B92"})
    events = {e["name"]: e for e in ws.client.get("/spending/events") if e["eventTypeId"] == event_type["id"]}
    start = min((r["activityDate"][:10] for r in ws.rows.values()), default="2000-01-01")
    for spender in ws.spenders:
        if spender not in events:
            events[spender] = ws.client.post("/spending/events", {
                "name": spender, "eventTypeId": event_type["id"], "startDate": start, "endDate": FAR_FUTURE,
                "description": "Whose spending this is; set by the spender labeler",
            })
    ws.config["eventTypeId"] = event_type["id"]
    ws.config["events"] = {spender: events[spender]["id"] for spender in ws.spenders}
    path = ws.dir / "config.json"
    path.write_text(json.dumps(ws.config, indent=2) + "\n", encoding="utf-8")
    ws.refresh()
    return {"eventType": event_type["id"], "events": ws.config["events"]}


def apply(ws: Workspace, dry_run: bool) -> dict:
    result = ws.sync(ws.rows, dry_run=dry_run)
    report = ws.evaluate()
    if not dry_run:
        (ws.dir / f"eval-{ws.version}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        (ws.dir / "model.json").write_text(json.dumps({"version": ws.version, **ws.model.to_json()}) + "\n", encoding="utf-8")
    return {"sync": result, "eval": report}


def latest_eval(ws: Workspace) -> dict | None:
    reports = sorted(ws.dir.glob("eval-*.json"), key=lambda p: p.stat().st_mtime)
    return json.loads(reports[-1].read_text(encoding="utf-8")) if reports else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m labeling", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("setup", "apply", "status", "serve"))
    parser.add_argument("--data-dir", default=os.environ.get("FINANCE_DATA"), required=not os.environ.get("FINANCE_DATA"))
    parser.add_argument("--base-url", default=os.environ.get("WEALTHFOLIO_URL", "http://127.0.0.1:8088"))
    parser.add_argument("--dry-run", action="store_true", help="apply: report what would change")
    parser.add_argument("--port", type=int, default=0, help="serve: loopback port (0 picks a free one)")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)
    if not (data_dir / "labels" / "config.json").exists():
        print("labeling is not configured: <data>/labels/config.json is missing", file=sys.stderr)
        return 0 if args.command == "apply" else 2
    ws = Workspace(data_dir, connect(data_dir, args.base_url), by=f"labeling {args.command}")
    if args.command in ("apply", "serve") and "events" not in ws.config:
        print("run `python -m labeling setup` first", file=sys.stderr)
        return 0 if args.command == "apply" else 2
    if args.command == "serve":
        from labeling.serve import serve
        serve(ws, args.port)
        return 0
    if args.command == "status":
        print(json.dumps({**ws.status(), "eval": latest_eval(ws)}, indent=2))
        return 0
    if args.command == "apply" and args.dry_run:
        print(json.dumps(apply(ws, True), indent=2))
        return 0
    try:
        with write_lock(data_dir, f"labeling {args.command}"):
            ws.ensure_fresh()
            if args.command == "setup":
                ws.client.post("/utilities/database/backup", {})
                print(json.dumps(setup(ws), indent=2))
            print(json.dumps(apply(ws, False), indent=2))
    except Locked as exc:
        print(f"skipped: Wealthfolio write lock is held: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
