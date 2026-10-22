"""Spender labeling commands.

    python -m labeling setup    # create the native Spender event type and events
    python -m labeling apply    # reconcile, retrain, and auto-apply labels (also runs daily)
    python -m labeling status   # labels by source and confidence, plus the latest evaluation
    python -m labeling serve    # the labeler canvas backend (prints its loopback URL)
    python -m labeling llm      # propose categories for uncategorized merchants with a local model (--eval first)

Private inputs and outputs live in <data>/labels/; see docs/runbooks/labeling.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, time
from pathlib import Path

from labeling.workspace import Locked, Workspace, connect, write_lock

FAR_FUTURE = "2099-12-31"
SYNC_WINDOW = (time(5, 45), time(6, 30))  # the daily sync's local time, when writes would collide


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


def llm_write(ws: Workspace, data_dir: Path, *, undo: bool, dry_run: bool, batch: int = 50) -> dict:
    """Write confident local-model categories as native rules: one native backup, then a batch per lock."""
    from labeling import llm
    if SYNC_WINDOW[0] <= datetime.now(ws.zone).time() <= SYNC_WINDOW[1]:
        return {"skipped": "the daily sync runs now"}
    if undo:
        with write_lock(data_dir, "labeling llm"):
            ws.ensure_fresh()
            return ws.undo()
    todo = llm.chosen(ws)
    if dry_run:
        return {"wouldCategorize": len(todo), "choices": todo}
    results = []
    for start in range(0, len(todo), batch):
        with write_lock(data_dir, "labeling llm"):
            ws.ensure_fresh()
            if not results:
                ws.client.post("/utilities/database/backup", {})
            still = {key for key, _, _ in llm.chosen(ws)}  # someone may have categorized one meanwhile
            part = [choice for choice in todo[start:start + batch] if choice[0] in still]
            results.append(ws.categorize_keys(part, prefix="LLM: ") if part else {"rules": [], "categorized": 0})
            ws.mark_fresh()
    return {"batches": len(results), "rules": sum(len(r["rules"]) for r in results),
            "categorized": sum(r["categorized"] for r in results)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m labeling", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("setup", "apply", "status", "serve", "llm"))
    parser.add_argument("--data-dir", default=os.environ.get("FINANCE_DATA"), required=not os.environ.get("FINANCE_DATA"))
    parser.add_argument("--base-url", default=os.environ.get("WEALTHFOLIO_URL", "http://127.0.0.1:8088"))
    parser.add_argument("--dry-run", action="store_true", help="apply: report what would change")
    parser.add_argument("--port", type=int, default=0, help="serve: loopback port (0 picks a free one)")
    parser.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"), help="llm: loopback only")
    parser.add_argument("--model", default=os.environ.get("LABELING_MODEL", "qwen3.8:27b"), help="llm: a local Ollama model")
    parser.add_argument("--eval", action="store_true", help="llm: score merchants whose category is known")
    parser.add_argument("--limit", type=int, help="llm: ask about at most this many merchants")
    parser.add_argument("--apply", action="store_true", help="llm: write confident proposals as native rules")
    parser.add_argument("--undo", action="store_true", help="llm: undo the last batch written by --apply")
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
    if args.command == "llm" and (args.apply or args.undo):
        try:
            print(json.dumps(llm_write(ws, data_dir, undo=args.undo, dry_run=args.dry_run), indent=2))
        except Locked as exc:
            print(f"skipped: Wealthfolio write lock is held: {exc}", file=sys.stderr)
            return 3
        return 0
    if args.command == "llm":
        from labeling import llm
        client = llm.Ollama(args.ollama_url, args.model)
        log = lambda line: print(line, file=sys.stderr, flush=True)
        print(json.dumps(llm.run(ws, client, evaluate_only=args.eval, limit=args.limit, log=log), indent=2))
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
