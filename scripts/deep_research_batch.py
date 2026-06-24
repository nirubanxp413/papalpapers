#!/usr/bin/env python3
"""Batch Perplexity Sonar Deep Research for encyclicals (Step 6).

Loads PERPLEXITY_API_KEY from project .env. Does not run on import — invoke explicitly:

    .venv/bin/python scripts/deep_research_batch.py init
    .venv/bin/python scripts/deep_research_batch.py run --concurrency 5

Planning only until you execute run yourself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEEP_RESEARCH_DIR = ROOT / "data" / "deep-research"
PROMPT_PATH = DEEP_RESEARCH_DIR / "prompt.md"
REPORTS_DIR = DEEP_RESEARCH_DIR / "reports"
CHECKLIST_PATH = DEEP_RESEARCH_DIR / "checklist.json"
RUN_LOG_PATH = DEEP_RESEARCH_DIR / "run_log.jsonl"
ENV_PATH = ROOT / ".env"

STEP3_REPORTS_DIR = ROOT / "data" / "subagents" / "reports"
ENCYCLOPICAL_DIR = ROOT / "data" / "encyclical"

API_BASE = "https://api.perplexity.ai/v1"
MODEL = "sonar-deep-research"
DEFAULT_CONCURRENCY = 5
DEFAULT_POLL_INTERVAL = 15.0
MAX_POLL_INTERVAL = 120.0
MIN_REPORT_BYTES = 500

STATUSES = ("pending", "in_progress", "complete", "failed", "skipped")

PROJECT_CONTEXT_RE = re.compile(
    r"## Project context\s*\n(.*?)(?=\n---|\n## |\Z)",
    re.DOTALL | re.IGNORECASE,
)
PROMPT_SECTION_RE = re.compile(
    r"## Research instructions\s*\n(.*?)(?=\n---|\n## |\Z)",
    re.DOTALL | re.IGNORECASE,
)
OUTPUT_SECTION_RE = re.compile(
    r"## Output format\s*\n(.*?)(?=\n---|\n## |\Z)",
    re.DOTALL | re.IGNORECASE,
)
CONSTRAINTS_SECTION_RE = re.compile(
    r"## Constraints\s*\n(.*?)(?=\n---|\n## |\Z)",
    re.DOTALL | re.IGNORECASE,
)
KEY_THEME_RE = re.compile(r"\*\*Key theme:\*\*\s*(.+)", re.IGNORECASE)
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
ENCYCLOPICAL_META_RE = {
    "pope": re.compile(r"^pope:\s*(.+)$", re.MULTILINE),
    "title": re.compile(r"^title:\s*(.+)$", re.MULTILINE),
    "published_date": re.compile(r"^published_date:\s*(.+)$", re.MULTILINE),
    "source": re.compile(r"^source:\s*(.+)$", re.MULTILINE),
    "alternate_source": re.compile(r"^alternate_source:\s*(.+)$", re.MULTILINE),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_dotenv() -> None:
    if not ENV_PATH.exists():
        return
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def api_key() -> str:
    load_dotenv()
    key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "PERPLEXITY_API_KEY not set. Add it to .env at the project root."
        )
    return key


def append_run_log(event: dict) -> None:
    DEEP_RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    event = {"ts": utc_now(), **event}
    with RUN_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def load_checklist() -> dict:
    if not CHECKLIST_PATH.exists():
        raise SystemExit(f"Checklist not found: {CHECKLIST_PATH}. Run: init")
    return json.loads(CHECKLIST_PATH.read_text(encoding="utf-8"))


def save_checklist(data: dict) -> None:
    DEEP_RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    CHECKLIST_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def summarize(data: dict) -> dict[str, int]:
    counts: dict[str, int] = {s: 0 for s in STATUSES}
    for item in data["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts


def strip_todo_placeholders(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("(TODO"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def load_prompt_sections() -> dict[str, str]:
    if not PROMPT_PATH.exists():
        raise SystemExit(f"Prompt file not found: {PROMPT_PATH}")
    body = PROMPT_PATH.read_text(encoding="utf-8")

    def section(pattern: re.Pattern[str]) -> str:
        match = pattern.search(body)
        return strip_todo_placeholders(match.group(1)) if match else ""

    sections = {
        "project_context": section(PROJECT_CONTEXT_RE),
        "instructions": section(PROMPT_SECTION_RE),
        "output_format": section(OUTPUT_SECTION_RE),
        "constraints": section(CONSTRAINTS_SECTION_RE),
    }
    if not sections["instructions"]:
        print(
            "Warning: Research instructions section in prompt.md is empty or still TODO.",
            file=sys.stderr,
        )
    return sections


def parse_step3_key_theme(report_text: str) -> str | None:
    match = KEY_THEME_RE.search(report_text)
    return match.group(1).strip() if match else None


def encyclical_stem_from_report_name(report_name: str) -> str:
    if report_name.startswith("run-"):
        return report_name[len("run-") :]
    return report_name


def read_encyclical_meta(source_path: Path) -> dict[str, str]:
    if not source_path.exists():
        return {}
    text = source_path.read_text(encoding="utf-8", errors="replace")
    meta: dict[str, str] = {}
    for key, pattern in ENCYCLOPICAL_META_RE.items():
        match = pattern.search(text)
        if match:
            meta[key] = match.group(1).strip()
    return meta


def extract_step3_summary(report_path: Path) -> str:
    text = report_path.read_text(encoding="utf-8", errors="replace")
    body = FRONTMATTER_RE.sub("", text, count=1).strip()
    return body


def build_query(
    *,
    meta: dict[str, str],
    source_file: str,
    step3_report_file: str,
    step3_summary: str,
    sections: dict[str, str],
) -> str:
    pope = meta.get("pope", "Unknown")
    title = meta.get("title", Path(source_file).stem)
    published = meta.get("published_date", "Unknown")
    source_url = meta.get("source", "")
    alternate_url = meta.get("alternate_source", "")

    parts = [
        "# Deep research request",
        "",
        "## Document",
        f"- Pope: {pope}",
        f"- Title: {title}",
        f"- Published: {published}",
        f"- Primary URL: {source_url or 'Not available'}",
    ]
    if alternate_url:
        parts.append(f"- Alternate URL: {alternate_url}")
    parts.extend(
        [
            f"- Source file: {source_file}",
            f"- Step 3 theme report: {step3_report_file}",
            "",
        ]
    )
    if sections["project_context"]:
        parts.extend(
            [
                "## Project context",
                "",
                sections["project_context"],
                "",
            ]
        )
    parts.extend(
        [
            "## Step 3 theme summary (from prior pass — use as framing, verify externally)",
            "",
            step3_summary,
            "",
            "## Research instructions",
            "",
            sections["instructions"] or "(No instructions provided yet.)",
        ]
    )
    if sections["output_format"]:
        parts.extend(["", "## Required output format", "", sections["output_format"]])
    if sections["constraints"]:
        parts.extend(["", "## Constraints", "", sections["constraints"]])
    return "\n".join(parts).strip() + "\n"


def report_name_for(stem: str) -> str:
    return f"run-{stem}"


def init_checklist(
    *,
    force: bool = False,
    include_themes: set[str] | None = None,
    require_step3: bool = True,
) -> None:
    if CHECKLIST_PATH.exists() and not force:
        raise SystemExit(
            f"{CHECKLIST_PATH} already exists. Use --force to rebuild or `sync` to reconcile."
        )

    sections = load_prompt_sections()
    items: list[dict] = []
    skipped_no_step3 = 0
    skipped_theme = 0

    encyclical_files = sorted(ENCYCLOPICAL_DIR.glob("*.md"))
    if not encyclical_files:
        raise SystemExit(f"No markdown files in {ENCYCLOPICAL_DIR}")

    for idx, source in enumerate(encyclical_files, 1):
        rel_source = source.relative_to(ROOT).as_posix()
        stem = source.name
        step3_report = STEP3_REPORTS_DIR / report_name_for(stem)
        rel_step3 = step3_report.relative_to(ROOT).as_posix()
        out_report = REPORTS_DIR / f"{report_name_for(stem)}.md"
        rel_out = out_report.relative_to(ROOT).as_posix()

        if require_step3 and not step3_report.exists():
            skipped_no_step3 += 1
            continue

        step3_text = step3_report.read_text(encoding="utf-8") if step3_report.exists() else ""
        key_theme = parse_step3_key_theme(step3_text) if step3_text else None

        if include_themes and key_theme not in include_themes:
            skipped_theme += 1
            continue

        meta = read_encyclical_meta(source)
        summary = extract_step3_summary(step3_report) if step3_report.exists() else ""
        query = build_query(
            meta=meta,
            source_file=rel_source,
            step3_report_file=rel_step3,
            step3_summary=summary,
            sections=sections,
        )

        status = "pending"
        completed_at = None
        if out_report.exists() and out_report.stat().st_size >= MIN_REPORT_BYTES:
            status = "complete"
            completed_at = utc_now()

        items.append(
            {
                "id": len(items) + 1,
                "source_file": rel_source,
                "step3_report_file": rel_step3,
                "report_file": rel_out,
                "key_theme": key_theme,
                "pope": meta.get("pope"),
                "title": meta.get("title"),
                "published_date": meta.get("published_date"),
                "source_url": meta.get("source"),
                "alternate_source_url": meta.get("alternate_source"),
                "status": status,
                "request_id": None,
                "batch_id": None,
                "started_at": None,
                "completed_at": completed_at,
                "error": None,
                "query_chars": len(query),
            }
        )

    if not items:
        raise SystemExit("No items matched init filters.")

    # Persist rendered queries separately for inspection / diff
    queries_dir = DEEP_RESEARCH_DIR / "queries"
    queries_dir.mkdir(parents=True, exist_ok=True)
    for item in items:
        query_path = queries_dir / f"{item['id']:04d}.md"
        # Rebuild query for disk (same as init logic)
        source = ROOT / item["source_file"]
        step3 = ROOT / item["step3_report_file"]
        meta = read_encyclical_meta(source)
        summary = extract_step3_summary(step3) if step3.exists() else ""
        query_path.write_text(
            build_query(
                meta=meta,
                source_file=item["source_file"],
                step3_report_file=item["step3_report_file"],
                step3_summary=summary,
                sections=sections,
            ),
            encoding="utf-8",
        )
        item["query_file"] = query_path.relative_to(ROOT).as_posix()

    data = {
        "meta": {
            "step": 6,
            "pass": "deep-research",
            "model": MODEL,
            "concurrency": DEFAULT_CONCURRENCY,
            "prompt_file": PROMPT_PATH.relative_to(ROOT).as_posix(),
            "created_at": utc_now(),
            "total": len(items),
            "skipped_no_step3": skipped_no_step3,
            "skipped_theme_filter": skipped_theme,
            "include_themes": sorted(include_themes) if include_themes else None,
        },
        "items": items,
    }
    save_checklist(data)
    append_run_log(
        {
            "event": "init",
            "total": len(items),
            "skipped_no_step3": skipped_no_step3,
            "skipped_theme_filter": skipped_theme,
        }
    )
    print(f"Initialized checklist with {len(items)} items → {CHECKLIST_PATH}")
    if skipped_no_step3:
        print(f"  Skipped {skipped_no_step3} (no Step 3 report)")
    if skipped_theme:
        print(f"  Skipped {skipped_theme} (theme filter)")


def cmd_status(_: argparse.Namespace) -> None:
    data = load_checklist()
    counts = summarize(data)
    total = data["meta"]["total"]
    print(f"Deep research checklist — {CHECKLIST_PATH}")
    print(f"Model: {data['meta'].get('model', MODEL)}")
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


def cmd_reset_stale(_: argparse.Namespace) -> None:
    data = load_checklist()
    reset = 0
    for item in data["items"]:
        if item["status"] != "in_progress":
            continue
        report = ROOT / item["report_file"]
        if report.exists() and report.stat().st_size >= MIN_REPORT_BYTES:
            item["status"] = "complete"
            item["completed_at"] = item["completed_at"] or utc_now()
        elif not item.get("request_id"):
            item["status"] = "pending"
            item["batch_id"] = None
            item["started_at"] = None
            reset += 1
    save_checklist(data)
    append_run_log({"event": "reset_stale", "reset_to_pending": reset})
    print(f"Reset {reset} stale in_progress item(s) to pending (or complete if report exists).")


def extract_message_content(payload: dict[str, Any]) -> str:
    """Best-effort extraction across Perplexity response shapes."""
    if "choices" in payload:
        choice = payload["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            return "\n".join(p for p in parts if p)
    if "output" in payload:
        chunks: list[str] = []
        for block in payload["output"]:
            for content in block.get("content", []) or []:
                if content.get("type") == "output_text":
                    chunks.append(content.get("text", ""))
        if chunks:
            return "\n".join(chunks)
    if "response" in payload and isinstance(payload["response"], dict):
        return extract_message_content(payload["response"])
    return json.dumps(payload, indent=2)


def write_report(item: dict, content: str, raw: dict[str, Any] | None = None) -> None:
    report_path = ROOT / item["report_file"]
    report_path.parent.mkdir(parents=True, exist_ok=True)

    frontmatter = {
        "source_file": item["source_file"],
        "step3_report_file": item["step3_report_file"],
        "report_file": item["report_file"],
        "pass": "step-6-deep-research",
        "model": MODEL,
        "pope": item.get("pope"),
        "title": item.get("title"),
        "published_date": item.get("published_date"),
        "source_url": item.get("source_url"),
        "alternate_source_url": item.get("alternate_source_url"),
        "key_theme": item.get("key_theme"),
        "request_id": item.get("request_id"),
        "completed_at": utc_now(),
    }
    fm = "\n".join(f"{k}: {json.dumps(v) if v is not None else 'null'}" for k, v in frontmatter.items())
    report_path.write_text(f"---\n{fm}\n---\n\n{content.strip()}\n", encoding="utf-8")

    if raw is not None:
        sidecar = report_path.with_suffix(".json")
        sidecar.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")


class PerplexityClient:
    def __init__(self, key: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    async def submit(self, client: httpx.AsyncClient, query: str) -> str:
        resp = await client.post(
            f"{API_BASE}/async/sonar",
            headers=self._headers,
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": query}],
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        request_id = data.get("request_id") or data.get("id")
        if not request_id:
            raise RuntimeError(f"No request_id in submit response: {data}")
        return str(request_id)

    async def poll(self, client: httpx.AsyncClient, request_id: str) -> dict[str, Any]:
        resp = await client.get(
            f"{API_BASE}/async/sonar/{request_id}",
            headers=self._headers,
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()


async def process_item(
    item: dict,
    data: dict,
    client: PerplexityClient,
    http: httpx.AsyncClient,
    poll_interval: float,
) -> int:
    item_id = item["id"]
    query_path = ROOT / item["query_file"]
    query = query_path.read_text(encoding="utf-8")

    if not item.get("request_id"):
        item["request_id"] = await client.submit(http, query)
        item["status"] = "in_progress"
        item["started_at"] = item["started_at"] or utc_now()
        save_checklist(data)
        append_run_log({"event": "submitted", "id": item_id, "request_id": item["request_id"]})

    interval = poll_interval
    while True:
        payload = await client.poll(http, item["request_id"])
        status = (payload.get("status") or payload.get("state") or "").lower()

        if status in {"completed", "complete", "succeeded", "success"}:
            content = extract_message_content(payload)
            if not content.strip():
                content = extract_message_content(payload.get("response", {}))
            write_report(item, content, raw=payload)
            item["status"] = "complete"
            item["completed_at"] = utc_now()
            item["error"] = None
            save_checklist(data)
            append_run_log({"event": "complete", "id": item_id})
            return item_id

        if status in {"failed", "error", "cancelled", "canceled"}:
            error = payload.get("error") or payload.get("message") or status
            item["status"] = "failed"
            item["error"] = str(error)
            item["completed_at"] = utc_now()
            save_checklist(data)
            append_run_log({"event": "failed", "id": item_id, "error": item["error"]})
            return item_id

        await asyncio.sleep(interval)
        interval = min(interval * 1.5, MAX_POLL_INTERVAL)


async def run_batch(
    *,
    concurrency: int,
    limit: int | None,
    poll_interval: float,
    dry_run: bool,
) -> None:
    data = load_checklist()
    if not dry_run:
        api_key()  # validate early

    pending = [
        i
        for i in data["items"]
        if i["status"] in ("pending", "in_progress")
        and not (
            (ROOT / i["report_file"]).exists()
            and (ROOT / i["report_file"]).stat().st_size >= MIN_REPORT_BYTES
        )
    ]
    if limit is not None:
        pending = pending[:limit]

    if not pending:
        print("Nothing to run.")
        return

    print(f"Queued {len(pending)} item(s), concurrency={concurrency}")
    if dry_run:
        for item in pending:
            print(f"  [{item['id']}] {item.get('title') or item['source_file']}")
        return

    client = PerplexityClient(api_key())
    sem = asyncio.Semaphore(concurrency)
    completed = 0

    async with httpx.AsyncClient() as http:
        in_flight: dict[int, asyncio.Task[int]] = {}

        async def worker(item: dict) -> int:
            async with sem:
                return await process_item(item, data, client, http, poll_interval)

        while pending or in_flight:
            while pending and len(in_flight) < concurrency:
                item = pending.pop(0)
                in_flight[item["id"]] = asyncio.create_task(worker(item))

            if not in_flight:
                break

            done, _ = await asyncio.wait(
                in_flight.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                item_id = task.result()
                del in_flight[item_id]
                completed += 1
                print(f"Finished id={item_id} ({completed}/{completed + len(pending) + len(in_flight)})")

            if limit is not None and completed >= limit:
                for task in in_flight.values():
                    task.cancel()
                break

    cmd_sync(argparse.Namespace())


def cmd_run(args: argparse.Namespace) -> None:
    asyncio.run(
        run_batch(
            concurrency=args.concurrency,
            limit=args.limit,
            poll_interval=args.poll_interval,
            dry_run=args.dry_run,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Perplexity deep research batch runner (Step 6)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Build checklist from encyclicals + Step 3 reports")
    p_init.add_argument("--force", action="store_true", help="Rebuild even if checklist exists")
    p_init.add_argument(
        "--include-themes",
        default="Social,Mixed",
        help="Comma-separated Key theme values to include (default: Social,Mixed). Use 'all' for every item with a Step 3 report.",
    )
    p_init.add_argument(
        "--no-require-step3",
        action="store_true",
        help="Include encyclicals even if Step 3 report is missing",
    )

    sub.add_parser("status", help="Print checklist summary")
    sub.add_parser("sync", help="Mark complete where report files exist on disk")
    sub.add_parser("reset-stale", help="Reset stuck in_progress items")

    p_run = sub.add_parser("run", help="Submit and poll Perplexity jobs (async, batched)")
    p_run.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p_run.add_argument("--limit", type=int, default=None, help="Max items this invocation")
    p_run.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    p_run.add_argument("--dry-run", action="store_true", help="List queue without calling API")

    args = parser.parse_args()

    if args.command == "init":
        themes = None
        if args.include_themes.lower() != "all":
            themes = {t.strip() for t in args.include_themes.split(",") if t.strip()}
        init_checklist(
            force=args.force,
            include_themes=themes,
            require_step3=not args.no_require_step3,
        )
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "sync":
        cmd_sync(args)
    elif args.command == "reset-stale":
        cmd_reset_stale(args)
    elif args.command == "run":
        cmd_run(args)


if __name__ == "__main__":
    main()
