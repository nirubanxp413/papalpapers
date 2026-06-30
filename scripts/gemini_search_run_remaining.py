#!/usr/bin/env python3
"""Queue and run all remaining Gemini Step 6 items.

Covers documents not yet covered by a valid Gemini3.1Flash report:
  - 4 pure Mixed docs missing Step 6
  - 24 Mixed-variant theme labels
  - 22 Social docs (including partial/missing gradients)
  - 146 Spiritual docs
  - other Step 3 outliers

Usage:
    python3 scripts/gemini_search_run_remaining.py init
    python3 scripts/gemini_search_run_remaining.py run
    python3 scripts/gemini_search_run_remaining.py init-and-run
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BATCH = ROOT / "scripts" / "gemini_search_batch.py"
DEFAULT_CONCURRENCY = 5


def run_batch_cmd(*args: str) -> None:
    cmd = [sys.executable, str(BATCH), *args]
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True)


def cmd_init(args: argparse.Namespace) -> None:
    extend_args = ["extend-remaining"]
    if args.dry_run:
        extend_args.append("--dry-run")
    run_batch_cmd(*extend_args)


def cmd_run(args: argparse.Namespace) -> None:
    if not args.dry_run:
        run_batch_cmd("reset-stale")
    run_args = [
        "run",
        "--concurrency",
        str(args.concurrency),
    ]
    if args.dry_run:
        run_args.append("--dry-run")
    if args.until_done:
        run_args.append("--until-done")
    else:
        run_args.append("--no-until-done")
    if args.limit is not None:
        run_args.extend(["--limit", str(args.limit)])
    run_batch_cmd(*run_args)
    if not args.dry_run:
        run_batch_cmd("status")


def cmd_init_and_run(args: argparse.Namespace) -> None:
    cmd_init(args)
    if not args.dry_run:
        cmd_run(args)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Queue and run remaining Gemini Step 6 encyclicals"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Parallel API requests (default: {DEFAULT_CONCURRENCY})",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max items to run this invocation")

    sub = parser.add_subparsers(dest="command", required=True)
    p_init = sub.add_parser("init", help="Extend checklist with remaining Step 3 items")
    p_init.add_argument("--dry-run", action="store_true", help="Preview without writing checklist")
    p_run = sub.add_parser("run", help="Run queued remaining items")
    p_run.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Parallel API requests (default: {DEFAULT_CONCURRENCY})",
    )
    p_run.add_argument("--limit", type=int, default=None, help="Max items to run this invocation")
    p_run.add_argument("--dry-run", action="store_true", help="List queue without calling API")
    p_run.add_argument(
        "--until-done",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep running until the remaining queue is complete (default: true)",
    )
    p_all = sub.add_parser("init-and-run", help="Extend checklist, then run until done")
    p_all.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Parallel API requests (default: {DEFAULT_CONCURRENCY})",
    )
    p_all.add_argument("--limit", type=int, default=None, help="Max items to run this invocation")
    p_all.add_argument("--dry-run", action="store_true", help="Preview without writing checklist or calling API")
    p_all.add_argument(
        "--until-done",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep running until the remaining queue is complete (default: true)",
    )

    args = parser.parse_args()
    if args.command == "init":
        cmd_init(args)
    elif args.command == "init-and-run":
        cmd_init_and_run(args)
    elif args.command == "run":
        cmd_run(args)


if __name__ == "__main__":
    main()
