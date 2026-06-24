#!/usr/bin/env python3
"""Step 3 checklist: init, batch, sync, and status for theme-extraction subagent runs."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENCYCLOPICAL_DIR = ROOT / "data" / "encyclical"
SUBAGENTS_DIR = ROOT / "data" / "subagents"
REPORTS_DIR = SUBAGENTS_DIR / "reports"
CHECKLIST_PATH = SUBAGENTS_DIR / "checklist.json"
RUN_LOG_PATH = SUBAGENTS_DIR / "run_log.jsonl"
PROMPT_PATH = SUBAGENTS_DIR / "subagentsprompt.md"

DEFAULT_MODEL = "claude-4.6-sonnet-medium-thinking"
DEFAULT_BATCH_SIZE = 30
MIN_REPORT_BYTES = 500

STATUSES = ("pending", "in_progress", "complete", "failed", "skipped")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def report_name_for(source_name: str) -> str:
    return f"run-{source_name}"


def load_checklist() -> dict:
    if not CHECKLIST_PATH.exists():
        raise SystemExit(f"Checklist not found: {CHECKLIST_PATH}. Run: init")
    return json.loads(CHECKLIST_PATH.read_text(encoding="utf-8"))


def save_checklist(data: dict) -> None:
    SUBAGENTS_DIR.mkdir(parents=True, exist_ok=True)
    CHECKLIST_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def append_run_log(event: dict) -> None:
    SUBAGENTS_DIR.mkdir(parents=True, exist_ok=True)
    event = {"ts": utc_now(), **event}
    with RUN_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def init_checklist(*, force: bool = False) -> None:
    if CHECKLIST_PATH.exists() and not force:
        raise SystemExit(
            f"{CHECKLIST_PATH} already exists. Use --force to rebuild or `sync` to reconcile."
        )

    sources = sorted(ENCYCLOPICAL_DIR.glob("*.md"))
    if not sources:
        raise SystemExit(f"No markdown files in {ENCYCLOPICAL_DIR}")

    items = []
    for idx, source in enumerate(sources, 1):
        rel_source = source.relative_to(ROOT).as_posix()
        report = REPORTS_DIR / report_name_for(source.name)
        rel_report = report.relative_to(ROOT).as_posix()
        status = "pending"
        completed_at = None
        if report.exists() and report.stat().st_size >= MIN_REPORT_BYTES:
            status = "complete"
            completed_at = utc_now()

        items.append(
            {
                "id": idx,
                "source_file": rel_source,
                "report_file": rel_report,
                "status": status,
                "batch_id": None,
                "started_at": None,
                "completed_at": completed_at,
                "error": None,
                "agent_id": None,
            }
        )

    data = {
        "meta": {
            "step": 3,
            "pass": "theme-extraction",
            "model": DEFAULT_MODEL,
            "batch_size": DEFAULT_BATCH_SIZE,
            "prompt_file": PROMPT_PATH.relative_to(ROOT).as_posix(),
            "created_at": utc_now(),
            "total": len(items),
        },
        "items": items,
    }
    save_checklist(data)
    append_run_log({"event": "init", "total": len(items)})
    print(f"Initialized checklist with {len(items)} items → {CHECKLIST_PATH}")


def summarize(data: dict) -> dict[str, int]:
    counts: dict[str, int] = {s: 0 for s in STATUSES}
    for item in data["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts


def cmd_status(_: argparse.Namespace) -> None:
    data = load_checklist()
    counts = summarize(data)
    total = data["meta"]["total"]
    print(f"Step 3 checklist — {CHECKLIST_PATH}")
    print(f"Model: {data['meta'].get('model', DEFAULT_MODEL)}")
    print(f"Batch size: {data['meta'].get('batch_size', DEFAULT_BATCH_SIZE)}")
    for status in STATUSES:
        print(f"  {status}: {counts.get(status, 0)}")
    done = counts.get("complete", 0) + counts.get("skipped", 0)
    print(f"Progress: {done}/{total} ({100 * done / total:.1f}%)")


def cmd_sync(_: argparse.Namespace) -> None:
    data = load_checklist()
    changed = 0
    for item in data["items"]:
        report = ROOT / item["report_file"]
        if report.exists() and report.stat().st_size >= MIN_REPORT_BYTES:
            if item["status"] != "complete":
                item["status"] = "complete"
                item["completed_at"] = item["completed_at"] or utc_now()
                item["error"] = None
                changed += 1
    save_checklist(data)
    append_run_log({"event": "sync", "marked_complete": changed})
    print(f"Sync complete — {changed} item(s) marked complete from disk.")


def cmd_reset_stale(args: argparse.Namespace) -> None:
    data = load_checklist()
    reset = 0
    for item in data["items"]:
        if item["status"] != "in_progress":
            continue
        report = ROOT / item["report_file"]
        if report.exists() and report.stat().st_size >= MIN_REPORT_BYTES:
            item["status"] = "complete"
            item["completed_at"] = item["completed_at"] or utc_now()
        else:
            item["status"] = "pending"
            item["batch_id"] = None
            item["started_at"] = None
            item["agent_id"] = None
            reset += 1
    save_checklist(data)
    append_run_log({"event": "reset_stale", "reset_to_pending": reset})
    print(f"Reset {reset} stale in_progress item(s) to pending (or complete if report exists).")


def cmd_next_batch(args: argparse.Namespace) -> None:
    data = load_checklist()
    size = args.size or data["meta"].get("batch_size", DEFAULT_BATCH_SIZE)
    pending = [item for item in data["items"] if item["status"] == "pending"]
    batch = pending[:size]
    if not batch:
        print("No pending items.", file=sys.stderr)
        return

    batch_id = f"batch-{utc_now().replace(':', '').replace('-', '')}"
    for item in batch:
        item["status"] = "in_progress"
        item["batch_id"] = batch_id
        item["started_at"] = utc_now()

    save_checklist(data)
    append_run_log(
        {
            "event": "next_batch",
            "batch_id": batch_id,
            "size": len(batch),
            "ids": [item["id"] for item in batch],
        }
    )

    payload = {
        "batch_id": batch_id,
        "model": data["meta"].get("model", DEFAULT_MODEL),
        "prompt_file": data["meta"].get("prompt_file"),
        "items": batch,
    }
    print(json.dumps(payload, indent=2))


def cmd_mark(args: argparse.Namespace) -> None:
    data = load_checklist()
    item = next((i for i in data["items"] if i["id"] == args.id), None)
    if item is None:
        raise SystemExit(f"No item with id {args.id}")

    item["status"] = args.status
    if args.status == "complete":
        item["completed_at"] = utc_now()
        item["error"] = None
    elif args.status == "failed":
        item["error"] = args.error or "unknown error"
        item["completed_at"] = utc_now()
    if args.agent_id:
        item["agent_id"] = args.agent_id

    save_checklist(data)
    append_run_log(
        {
            "event": "mark",
            "id": args.id,
            "status": args.status,
            "error": args.error,
            "agent_id": args.agent_id,
        }
    )
    print(f"Marked id={args.id} as {args.status}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 3 subagent checklist manager")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Build checklist from data/encyclical/*.md")
    p_init.add_argument("--force", action="store_true", help="Rebuild even if checklist exists")

    sub.add_parser("status", help="Print checklist summary")
    sub.add_parser("sync", help="Mark complete where report files exist on disk")
    sub.add_parser("reset-stale", help="Reset in_progress items without reports to pending")

    p_batch = sub.add_parser("next-batch", help="Claim next batch (marks in_progress)")
    p_batch.add_argument("--size", type=int, default=None, help="Batch size (default 30)")

    p_mark = sub.add_parser("mark", help="Mark a checklist item by id")
    p_mark.add_argument("id", type=int)
    p_mark.add_argument("status", choices=["complete", "failed", "pending", "skipped"])
    p_mark.add_argument("--error", default=None)
    p_mark.add_argument("--agent-id", default=None)

    args = parser.parse_args()
    commands = {
        "init": init_checklist,
        "status": cmd_status,
        "sync": cmd_sync,
        "reset-stale": cmd_reset_stale,
        "next-batch": cmd_next_batch,
        "mark": cmd_mark,
    }
    if args.command == "init":
        init_checklist(force=args.force)
    else:
        commands[args.command](args)


if __name__ == "__main__":
    main()
