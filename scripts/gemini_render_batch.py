#!/usr/bin/env python3
"""Render encyclical sculptures with Gemini Nano Banana 2 (gemini-3.1-flash-image).

Combines data/sculpture-prompts/prompt.md (with encyclical summary injected) and
data/render-1-gemini/prompt.md. Saves images and a ## Why report per item.

    python3 scripts/gemini_render_batch.py init
    python3 scripts/gemini_render_batch.py status
    python3 scripts/gemini_render_batch.py run --limit 20 --concurrency 2
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from gemini_sculpture_prompt_batch import (  # noqa: E402
    build_query,
    load_prompt_template as load_sculpture_template,
)

OUTPUT_DIR = ROOT / "data" / "render-1-gemini"
RENDER_PROMPT_PATH = OUTPUT_DIR / "prompt.md"
IMAGES_DIR = OUTPUT_DIR / "images"
REPORTS_DIR = OUTPUT_DIR / "reports"
CHECKLIST_PATH = OUTPUT_DIR / "checklist.json"
RUN_LOG_PATH = OUTPUT_DIR / "run_log.jsonl"
SCULPTURE_CHECKLIST_PATH = ROOT / "data" / "sculpture-prompts" / "checklist.json"
ENV_PATH = ROOT / ".env"

INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
MODEL = "gemini-3.1-flash-image"
MODEL_LABEL = "nano-banana-2"
DEFAULT_CONCURRENCY = 2
DEFAULT_RUN_LIMIT = 20
MIN_IMAGE_BYTES = 40_000
MIN_WHY_CHARS = 40
MAX_RETRIES = 3
MAX_RATE_LIMIT_RETRIES = 200
RETRY_BACKOFF = 15.0
RATE_LIMIT_COOLDOWN = 60.0
REQUEST_TIMEOUT = 300.0

STATUSES = ("pending", "in_progress", "complete", "failed", "skipped")

QUOTA_ERROR_MARKERS = (
    "insufficient_quota",
    "exceeded your current quota",
    "resource_exhausted",
)

WHY_SECTION_RE = re.compile(
    r"##\s*Why\s*\n+(.*?)(?=\n##\s|\Z)",
    re.DOTALL | re.IGNORECASE,
)

RESPONSE_FORMAT = {
    "type": "image",
    "mime_type": "image/jpeg",
    "aspect_ratio": "1:1",
    "image_size": "1K",
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
    for name in ("GEMINI_PRO", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        key = os.environ.get(name, "").strip()
        if key:
            return key
    raise SystemExit(
        "GEMINI_PRO not set. Add it to .env or export GEMINI_PRO / GEMINI_API_KEY."
    )


def append_run_log(event: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    event = {"ts": utc_now(), **event}
    with RUN_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def load_checklist() -> dict:
    if not CHECKLIST_PATH.exists():
        raise SystemExit(f"Checklist not found: {CHECKLIST_PATH}. Run: init")
    return json.loads(CHECKLIST_PATH.read_text(encoding="utf-8"))


def save_checklist(data: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CHECKLIST_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def summarize(data: dict) -> dict[str, int]:
    counts: dict[str, int] = {s: 0 for s in STATUSES}
    for item in data["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts


def load_render_footer() -> str:
    if not RENDER_PROMPT_PATH.exists():
        raise SystemExit(f"Render prompt not found: {RENDER_PROMPT_PATH}")
    return RENDER_PROMPT_PATH.read_text(encoding="utf-8").strip()


def build_render_input(item: dict) -> str:
    sculpture = load_sculpture_template()
    base = build_query(item, sculpture)
    footer = load_render_footer()
    return f"{base.rstrip()}\n\n---\n\n{footer}\n"


def image_path_for_item(item: dict) -> Path:
    return IMAGES_DIR / f"{item['id']:04d}_{item['stem']}.jpg"


def report_path_for_item(item: dict) -> Path:
    return REPORTS_DIR / f"{item['id']:04d}_{item['stem']}.md"


def extract_why(text: str) -> str | None:
    match = WHY_SECTION_RE.search(text)
    if match:
        return match.group(1).strip()
    cleaned = text.strip()
    if len(cleaned) >= MIN_WHY_CHARS:
        return cleaned
    return None


def parse_interaction(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    texts: list[str] = []
    images: list[dict[str, Any]] = []

    for step in payload.get("steps") or []:
        blocks = step.get("content") or step.get("summary") or []
        for block in blocks:
            if block.get("type") == "text" and block.get("text"):
                texts.append(block["text"].strip())
            elif block.get("type") == "image" and block.get("data"):
                images.append(block)

    combined_text = "\n\n".join(t for t in texts if t)
    image_block = images[-1] if images else payload.get("output_image")
    if isinstance(image_block, dict) and image_block.get("data"):
        return combined_text, image_block
    return combined_text, None


def item_is_complete(item: dict) -> bool:
    image_path = ROOT / item["image_file"]
    report_path = ROOT / item["report_file"]
    if not image_path.exists() or image_path.stat().st_size < MIN_IMAGE_BYTES:
        return False
    if not report_path.exists():
        return False
    why = extract_why(read_report_text(report_path))
    return bool(why and len(why) >= MIN_WHY_CHARS)


def read_report_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def init_checklist(*, force: bool = False) -> None:
    if CHECKLIST_PATH.exists() and not force:
        raise SystemExit(
            f"{CHECKLIST_PATH} already exists. Use --force to rebuild or `sync` to reconcile."
        )
    if not SCULPTURE_CHECKLIST_PATH.exists():
        raise SystemExit(
            f"Sculpture checklist not found: {SCULPTURE_CHECKLIST_PATH}. "
            "Run: python3 scripts/gemini_sculpture_prompt_batch.py init"
        )

    load_sculpture_template()
    load_render_footer()
    sculpture = json.loads(SCULPTURE_CHECKLIST_PATH.read_text(encoding="utf-8"))
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    items: list[dict] = []
    for src in sculpture["items"]:
        if not (src.get("summary") or "").strip():
            continue
        item = {
            "id": len(items) + 1,
            "stem": src["stem"],
            "source_file": src["source_file"],
            "pope": src.get("pope"),
            "title": src.get("title"),
            "published_date": src.get("published_date"),
            "category": src.get("category"),
            "summary": src["summary"],
            "summary_chars": src.get("summary_chars", len(src["summary"])),
            "image_file": image_path_for_item(
                {"id": len(items) + 1, "stem": src["stem"]}
            ).relative_to(ROOT).as_posix(),
            "report_file": report_path_for_item(
                {"id": len(items) + 1, "stem": src["stem"]}
            ).relative_to(ROOT).as_posix(),
            "status": "pending",
            "interaction_id": None,
            "started_at": None,
            "completed_at": None,
            "error": None,
        }
        if item_is_complete(item):
            item["status"] = "complete"
            item["completed_at"] = utc_now()
        items.append(item)

    data = {
        "meta": {
            "pass": "render-1-gemini",
            "model": MODEL,
            "model_label": MODEL_LABEL,
            "api": "generativelanguage.googleapis.com/v1beta/interactions",
            "sculpture_prompt": "data/sculpture-prompts/prompt.md",
            "render_prompt": RENDER_PROMPT_PATH.relative_to(ROOT).as_posix(),
            "concurrency": DEFAULT_CONCURRENCY,
            "default_run_limit": DEFAULT_RUN_LIMIT,
            "created_at": utc_now(),
            "total": len(items),
        },
        "items": items,
    }
    save_checklist(data)
    append_run_log({"event": "init", "total": len(items)})
    print(f"Initialized render checklist with {len(items)} items → {CHECKLIST_PATH}")


def cmd_status(_: argparse.Namespace) -> None:
    data = load_checklist()
    counts = summarize(data)
    total = data["meta"]["total"]
    print(f"Render checklist — {CHECKLIST_PATH}")
    print(f"Model: {data['meta'].get('model_label', MODEL_LABEL)} ({MODEL})")
    for status in STATUSES:
        print(f"  {status}: {counts.get(status, 0)}")
    done = counts.get("complete", 0) + counts.get("skipped", 0)
    print(f"Progress: {done}/{total} ({100 * done / total:.1f}%)")


def cmd_sync(_: argparse.Namespace) -> None:
    data = load_checklist()
    changed = 0
    for item in data["items"]:
        if item_is_complete(item):
            if item["status"] != "complete":
                item["status"] = "complete"
                item["completed_at"] = item["completed_at"] or utc_now()
                item["error"] = None
                changed += 1
        elif item["status"] == "complete":
            item["status"] = "pending"
            item["completed_at"] = None
            changed += 1
    save_checklist(data)
    append_run_log({"event": "sync", "marked_complete": changed})
    print(f"Sync complete — {changed} item(s) updated.")


def is_rate_limit_error(status_code: int, detail: str) -> bool:
    if status_code == 429:
        return True
    lowered = detail.lower()
    return "resource_exhausted" in lowered or "retry in" in lowered


def parse_retry_seconds(detail: str) -> float | None:
    match = re.search(r"retry in ([0-9.]+)s", detail, re.IGNORECASE)
    if match:
        return float(match.group(1)) + 1.0
    return None


def write_outputs(
    item: dict,
    *,
    why_text: str,
    image_block: dict[str, Any],
    raw: dict[str, Any],
) -> None:
    image_path = ROOT / item["image_file"]
    report_path = ROOT / item["report_file"]
    image_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    image_bytes = base64.b64decode(image_block["data"])
    image_path.write_bytes(image_bytes)

    frontmatter = {
        "pass": "render-1-gemini",
        "model": MODEL,
        "model_label": MODEL_LABEL,
        "interaction_id": item.get("interaction_id"),
        "pope": item.get("pope"),
        "title": item.get("title"),
        "published_date": item.get("published_date"),
        "category": item.get("category"),
        "image_file": item["image_file"],
        "image_bytes": len(image_bytes),
        "completed_at": utc_now(),
    }
    fm = "\n".join(
        f"{k}: {json.dumps(v) if v is not None else 'null'}" for k, v in frontmatter.items()
    )
    body = (
        f"# Render: {item.get('title') or item['stem']}\n\n"
        f"## Why\n\n{why_text.strip()}\n\n"
        f"## Image\n\n`{item['image_file']}`\n"
    )
    report_path.write_text(f"---\n{fm}\n---\n\n{body}", encoding="utf-8")

    sidecar = report_path.with_suffix(".json")
    sidecar.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")


class NanoBananaClient:
    def __init__(self, key: str) -> None:
        self._key = key

    async def render(self, client: httpx.AsyncClient, prompt: str) -> dict[str, Any]:
        body = {
            "model": MODEL,
            "input": prompt,
            "response_format": RESPONSE_FORMAT,
        }
        resp = await client.post(
            INTERACTIONS_URL,
            headers={
                "x-goog-api-key": self._key,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()


async def process_item(
    item: dict,
    data: dict,
    client: NanoBananaClient,
    http: httpx.AsyncClient,
) -> int:
    item_id = item["id"]
    prompt = build_render_input(item)

    item["status"] = "in_progress"
    item["started_at"] = item["started_at"] or utc_now()
    save_checklist(data)

    last_error: str | None = None
    rate_limit_attempts = 0

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            payload = await client.render(http, prompt)
            item["interaction_id"] = payload.get("id")

            text, image_block = parse_interaction(payload)
            why = extract_why(text)
            if not why:
                raise RuntimeError("missing ## Why text in model response")
            if not image_block:
                raise RuntimeError("no image returned in interaction steps")

            write_outputs(item, why_text=why, image_block=image_block, raw=payload)
            item["status"] = "complete"
            item["completed_at"] = utc_now()
            item["error"] = None
            save_checklist(data)
            append_run_log(
                {
                    "event": "complete",
                    "id": item_id,
                    "title": item.get("title"),
                    "interaction_id": item.get("interaction_id"),
                    "image_bytes": (ROOT / item["image_file"]).stat().st_size,
                }
            )
            return item_id
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:800]
            last_error = f"HTTP {exc.response.status_code}: {detail}"
            if is_rate_limit_error(exc.response.status_code, detail):
                rate_limit_attempts += 1
                if rate_limit_attempts > MAX_RATE_LIMIT_RETRIES:
                    break
                wait = parse_retry_seconds(detail) or RATE_LIMIT_COOLDOWN
                print(
                    f"Rate limited id={item_id}, waiting {wait:.0f}s",
                    file=sys.stderr,
                )
                await asyncio.sleep(wait)
                attempt -= 1
                continue
            if exc.response.status_code in {500, 502, 503, 504} and attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            break
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            break

    item["status"] = "failed"
    item["error"] = last_error or "unknown error"
    item["completed_at"] = utc_now()
    save_checklist(data)
    append_run_log({"event": "failed", "id": item_id, "error": item["error"]})
    return item_id


async def run_batch(
    *,
    concurrency: int,
    limit: int | None,
    dry_run: bool,
    all_items: bool,
) -> None:
    data = load_checklist()
    api_key()

    pending = [
        i
        for i in data["items"]
        if i["status"] in ("pending", "in_progress", "failed")
        and not item_is_complete(i)
    ]
    if not all_items:
        run_limit = DEFAULT_RUN_LIMIT if limit is None else limit
        pending = pending[:run_limit]

    if not pending:
        print("Nothing to run.")
        return

    print(
        f"Queued {len(pending)} render(s), concurrency={concurrency}, "
        f"model={MODEL_LABEL} ({MODEL})"
    )
    if dry_run:
        for item in pending:
            print(f"  [{item['id']}] {item.get('title') or item['stem']}")
            print(f"      → {item['image_file']}")
        return

    client = NanoBananaClient(api_key())
    sem = asyncio.Semaphore(concurrency)
    completed = 0

    async with httpx.AsyncClient() as http:
        in_flight: dict[int, asyncio.Task[int]] = {}

        async def worker(item: dict) -> int:
            async with sem:
                return await process_item(item, data, client, http)

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
                print(
                    f"Finished id={item_id} "
                    f"({completed}/{completed + len(pending) + len(in_flight)})"
                )

    cmd_sync(argparse.Namespace())


def cmd_run(args: argparse.Namespace) -> None:
    asyncio.run(
        run_batch(
            concurrency=args.concurrency,
            limit=args.limit,
            dry_run=args.dry_run,
            all_items=args.all,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render encyclical sculptures with Gemini Nano Banana 2"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Build checklist from sculpture-prompts")
    p_init.add_argument("--force", action="store_true")

    sub.add_parser("status", help="Print checklist summary")
    sub.add_parser("sync", help="Mark complete where image + report exist on disk")

    p_run = sub.add_parser("run", help="Render images (default: first 20 pending)")
    p_run.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p_run.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"Max items this run (default: {DEFAULT_RUN_LIMIT}; use --all for full batch)",
    )
    p_run.add_argument(
        "--all",
        action="store_true",
        help="Run all pending items (ignore default 20-item cap)",
    )
    p_run.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.command == "init":
        init_checklist(force=args.force)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "sync":
        cmd_sync(args)
    elif args.command == "run":
        cmd_run(args)


if __name__ == "__main__":
    main()
