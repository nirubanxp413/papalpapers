#!/usr/bin/env python3
"""Generate text-to-image sculpture prompts from encyclical summaries via Gemini.

Reads summaries from data/encyclicals.csv and a shared prompt template
(data/sculpture-prompts/prompt.md). Each run injects the encyclical summary into
{{encyclical}} at request time. Outputs to data/sculpture-prompts/reports/.

    python3 scripts/gemini_sculpture_prompt_batch.py init
    python3 scripts/gemini_sculpture_prompt_batch.py status
    python3 scripts/gemini_sculpture_prompt_batch.py run --concurrency 5 --limit 3
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "data" / "sculpture-prompts"
PROMPT_PATH = OUTPUT_DIR / "prompt.md"
REPORTS_DIR = OUTPUT_DIR / "reports"
CHECKLIST_PATH = OUTPUT_DIR / "checklist.json"
RUN_LOG_PATH = OUTPUT_DIR / "run_log.jsonl"
CSV_PATH = ROOT / "data" / "encyclicals.csv"
ENCYCLOPICAL_DIR = ROOT / "data" / "encyclical"
ENV_PATH = ROOT / ".env"

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
MODEL = "gemini-3.1-flash-lite"
DEFAULT_CONCURRENCY = 5
MIN_PROMPT_CHARS = 80
MAX_PROMPT_CHARS = 2500
MIN_REPORT_BYTES = 200
MAX_RETRIES = 4
MAX_RATE_LIMIT_RETRIES = 500
RETRY_BACKOFF = 10.0
RATE_LIMIT_COOLDOWN = 60.0

STATUSES = ("pending", "in_progress", "complete", "failed", "skipped")

QUOTA_ERROR_MARKERS = (
    "insufficient_quota",
    "exceeded your current quota",
    "resource_exhausted",
)

SYSTEM_INSTRUCTION = (
    "You are a visual metaphor designer for the Papal Papers project. "
    "Given an encyclical summary, you produce one sculptural concept and a single "
    "text-to-image prompt describing that form. Follow the prompt template exactly. "
    "Return only markdown — no preamble."
)

ENCYCLICAL_PLACEHOLDER = "{{encyclical}}"
IMAGE_PROMPT_RE = re.compile(
    r"## Text-to-image prompt\s*\n+```(?:text|txt)?\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
CONCEPT_RE = re.compile(
    r"## Concept\s*\n+(.*?)(?=\n## |\Z)",
    re.DOTALL | re.IGNORECASE,
)


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


def format_date_ddmmyyyy(iso_date: str) -> str:
    if not iso_date or len(iso_date) < 10:
        return "00000000"
    year, month, day = iso_date[:10].split("-")
    return f"{day}{month}{year}"


def sanitize_filename_part(text: str, max_len: int = 100) -> str:
    text = re.sub(r'[\\/:*?"<>|]', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len].rstrip(" .")


def build_stem(row: dict[str, str], used: set[str]) -> str:
    date_part = format_date_ddmmyyyy(row.get("published_date", ""))
    pope_part = sanitize_filename_part(row.get("pope", ""))
    title_part = sanitize_filename_part(row.get("title", ""))
    base = f"{date_part}_{pope_part}_{title_part}"
    if base not in used:
        used.add(base)
        return base

    slug = sanitize_filename_part(Path(row.get("link", "")).stem or "doc")
    n = 2
    while True:
        candidate = f"{base}_{slug}_{n}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1


def normalize_stem_quotes(stem: str) -> str:
    return (
        stem.replace(""", '"')
        .replace(""", '"')
        .replace("'", "'")
        .replace("'", "'")
    )


def resolve_encyclical_path(stem: str) -> Path | None:
    candidates = [
        ENCYCLOPICAL_DIR / f"{stem}.md",
        ENCYCLOPICAL_DIR / normalize_stem_quotes(f"{stem}.md"),
    ]
    for path in candidates:
        if path.exists():
            return path

    date_prefix = stem[:8] if len(stem) >= 8 and stem[:8].isdigit() else None
    if date_prefix:
        matches = sorted(ENCYCLOPICAL_DIR.glob(f"{date_prefix}_*.md"))
        normalized_target = normalize_stem_quotes(stem).casefold()
        for match in matches:
            name = match.name.removesuffix(".md")
            if normalize_stem_quotes(name).casefold() == normalized_target:
                return match
        if len(matches) == 1:
            return matches[0]
    return None


def load_prompt_template() -> str:
    if not PROMPT_PATH.exists():
        raise SystemExit(f"Prompt file not found: {PROMPT_PATH}")
    return PROMPT_PATH.read_text(encoding="utf-8").strip() + "\n"


def build_encyclical_context(item: dict) -> str:
    return "\n".join(
        [
            f"- Pope: {item.get('pope') or 'Unknown'}",
            f"- Title: {item.get('title') or 'Unknown'}",
            f"- Published: {item.get('published_date') or 'Unknown'}",
            f"- Category: {item.get('category') or 'Unknown'}",
            "",
            (item.get("summary") or "").strip(),
        ]
    ).strip()


def build_query(item: dict, template: str) -> str:
    encyclical = build_encyclical_context(item)
    if ENCYCLICAL_PLACEHOLDER in template:
        return template.replace(ENCYCLICAL_PLACEHOLDER, encyclical)
    return f"{template.rstrip()}\n\n{encyclical}\n"


def report_path_for_stem(stem: str) -> Path:
    return REPORTS_DIR / f"run-{stem}.md"


def read_report_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def validate_report(content: str) -> tuple[bool, str | None]:
    concept = CONCEPT_RE.search(content)
    if not concept or len(concept.group(1).strip()) < 20:
        return False, "missing or too short ## Concept section"

    prompt_match = IMAGE_PROMPT_RE.search(content)
    if not prompt_match:
        return False, "missing ## Text-to-image prompt code block"
    prompt_text = prompt_match.group(1).strip()
    if len(prompt_text) < MIN_PROMPT_CHARS:
        return False, f"image prompt too short ({len(prompt_text)} chars)"
    if len(prompt_text) > MAX_PROMPT_CHARS:
        return False, f"image prompt too long ({len(prompt_text)} chars)"
    return True, None


def report_is_valid(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < MIN_REPORT_BYTES:
        return False
    ok, _ = validate_report(read_report_text(path))
    return ok


def item_has_valid_report(item: dict) -> bool:
    return report_is_valid(ROOT / item["report_file"])


def init_checklist(*, force: bool = False, require_summary: bool = True) -> None:
    if CHECKLIST_PATH.exists() and not force:
        raise SystemExit(
            f"{CHECKLIST_PATH} already exists. Use --force to rebuild or `sync` to reconcile."
        )

    if not CSV_PATH.exists():
        raise SystemExit(f"CSV not found: {CSV_PATH}")

    load_prompt_template()
    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8")))
    used_stems: set[str] = set()
    items: list[dict] = []
    skipped_no_summary = 0
    skipped_no_source = 0

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    for row in rows:
        summary = (row.get("summary") or "").strip()
        if require_summary and not summary:
            skipped_no_summary += 1
            continue

        stem = build_stem(row, used_stems)
        source = resolve_encyclical_path(stem)
        if source is None:
            skipped_no_source += 1
            continue

        rel_source = source.relative_to(ROOT).as_posix()
        rel_out = report_path_for_stem(stem).relative_to(ROOT).as_posix()

        status = "complete" if report_is_valid(report_path_for_stem(stem)) else "pending"
        item = {
            "id": len(items) + 1,
            "stem": stem,
            "source_file": rel_source,
            "report_file": rel_out,
            "pope": row.get("pope"),
            "title": row.get("title"),
            "published_date": row.get("published_date"),
            "category": row.get("category"),
            "summary": summary,
            "summary_chars": len(summary),
            "status": status,
            "response_id": None,
            "started_at": None,
            "completed_at": utc_now() if status == "complete" else None,
            "error": None,
        }
        items.append(item)

    if not items:
        raise SystemExit("No items matched init filters.")

    data = {
        "meta": {
            "pass": "sculpture-prompt",
            "model": MODEL,
            "api": "generativelanguage.googleapis.com/v1beta",
            "concurrency": DEFAULT_CONCURRENCY,
            "prompt_file": PROMPT_PATH.relative_to(ROOT).as_posix(),
            "csv_file": CSV_PATH.relative_to(ROOT).as_posix(),
            "created_at": utc_now(),
            "total": len(items),
            "skipped_no_summary": skipped_no_summary,
            "skipped_no_source": skipped_no_source,
        },
        "items": items,
    }
    save_checklist(data)
    append_run_log(
        {
            "event": "init",
            "total": len(items),
            "skipped_no_summary": skipped_no_summary,
            "skipped_no_source": skipped_no_source,
        }
    )
    print(f"Initialized checklist with {len(items)} items → {CHECKLIST_PATH}")
    if skipped_no_summary:
        print(f"  Skipped {skipped_no_summary} (no summary in CSV)")
    if skipped_no_source:
        print(f"  Skipped {skipped_no_source} (encyclical source not found)")


def cmd_status(_: argparse.Namespace) -> None:
    data = load_checklist()
    counts = summarize(data)
    total = data["meta"]["total"]
    print(f"Sculpture prompt checklist — {CHECKLIST_PATH}")
    print(f"Model: {data['meta'].get('model', MODEL)}")
    for status in STATUSES:
        print(f"  {status}: {counts.get(status, 0)}")
    done = counts.get("complete", 0) + counts.get("skipped", 0)
    print(f"Progress: {done}/{total} ({100 * done / total:.1f}%)")


def cmd_sync(_: argparse.Namespace) -> None:
    data = load_checklist()
    changed = 0
    for item in data["items"]:
        if item_has_valid_report(item):
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
    print(f"Sync complete — {changed} item(s) updated from disk validation.")


def is_quota_error(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.lower()
    return any(marker in lowered for marker in QUOTA_ERROR_MARKERS)


def is_rate_limit_error(status_code: int, detail: str) -> bool:
    if status_code == 429:
        return True
    lowered = detail.lower()
    return "resource_exhausted" in lowered or "retry in" in lowered


def cmd_reset_quota_failed(_: argparse.Namespace) -> None:
    data = load_checklist()
    reset = 0
    for item in data["items"]:
        if item["status"] != "failed" or not is_quota_error(item.get("error")):
            continue
        item["status"] = "pending"
        item["error"] = None
        item["completed_at"] = None
        item["started_at"] = None
        reset += 1
    save_checklist(data)
    append_run_log({"event": "reset_quota_failed", "reset_to_pending": reset})
    print(f"Reset {reset} quota-failed item(s) back to pending.")


def cmd_reset_stale(_: argparse.Namespace) -> None:
    data = load_checklist()
    reset = 0
    for item in data["items"]:
        if item["status"] != "in_progress":
            continue
        if item_has_valid_report(item):
            item["status"] = "complete"
            item["completed_at"] = item["completed_at"] or utc_now()
        else:
            item["status"] = "pending"
            item["started_at"] = None
            reset += 1
    save_checklist(data)
    append_run_log({"event": "reset_stale", "reset_to_pending": reset})
    print(f"Reset {reset} stale in_progress item(s) to pending (or complete if report exists).")


def extract_gemini_content(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError("No candidates in Gemini response")
    parts = candidates[0].get("content", {}).get("parts") or []
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
    content = "\n".join(t for t in texts if t).strip()
    if not content:
        raise RuntimeError("Empty response content")
    return content


def write_report(item: dict, content: str, raw: dict[str, Any] | None = None) -> None:
    report_path = ROOT / item["report_file"]
    report_path.parent.mkdir(parents=True, exist_ok=True)

    prompt_match = IMAGE_PROMPT_RE.search(content)
    image_prompt = prompt_match.group(1).strip() if prompt_match else None

    frontmatter = {
        "source_file": item["source_file"],
        "report_file": item["report_file"],
        "pass": "sculpture-prompt",
        "model": MODEL,
        "pope": item.get("pope"),
        "title": item.get("title"),
        "published_date": item.get("published_date"),
        "category": item.get("category"),
        "response_id": item.get("response_id"),
        "image_prompt_chars": len(image_prompt) if image_prompt else None,
        "completed_at": utc_now(),
    }
    fm = "\n".join(
        f"{k}: {json.dumps(v) if v is not None else 'null'}" for k, v in frontmatter.items()
    )
    report_path.write_text(f"---\n{fm}\n---\n\n{content.strip()}\n", encoding="utf-8")

    if raw is not None:
        sidecar = report_path.with_suffix(".json")
        sidecar.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")


def parse_retry_seconds(detail: str) -> float | None:
    match = re.search(r"retry in ([0-9.]+)s", detail, re.IGNORECASE)
    if match:
        return float(match.group(1)) + 1.0
    return None


class GeminiClient:
    def __init__(self, key: str) -> None:
        self._key = key

    async def complete(self, client: httpx.AsyncClient, query: str) -> dict[str, Any]:
        url = f"{API_BASE}/models/{MODEL}:generateContent"
        body: dict[str, Any] = {
            "contents": [{"parts": [{"text": query}]}],
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "generationConfig": {
                "temperature": 0.6,
                "maxOutputTokens": 4096,
            },
        }
        resp = await client.post(
            url,
            params={"key": self._key},
            json=body,
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json()


async def process_item(
    item: dict,
    data: dict,
    client: GeminiClient,
    http: httpx.AsyncClient,
    prompt_template: str,
) -> int:
    item_id = item["id"]
    query = build_query(item, prompt_template)

    item["status"] = "in_progress"
    item["started_at"] = item["started_at"] or utc_now()
    save_checklist(data)

    last_error: str | None = None
    rate_limit_attempts = 0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            payload = await client.complete(http, query)
            content = extract_gemini_content(payload)
            ok, reason = validate_report(content)
            if not ok:
                raise RuntimeError(reason or "invalid report format")

            item["response_id"] = payload.get("responseId")
            write_report(item, content, raw=payload)
            item["status"] = "complete"
            item["completed_at"] = utc_now()
            item["error"] = None
            save_checklist(data)
            append_run_log(
                {
                    "event": "complete",
                    "id": item_id,
                    "title": item.get("title"),
                    "response_id": item.get("response_id"),
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
                    f"Rate limited id={item_id}, waiting {wait:.0f}s "
                    f"(rate-limit retry {rate_limit_attempts})",
                    file=sys.stderr,
                )
                item["status"] = "in_progress"
                item["error"] = f"rate_limited, retrying in {wait:.0f}s"
                save_checklist(data)
                append_run_log(
                    {
                        "event": "rate_limited",
                        "id": item_id,
                        "wait_seconds": wait,
                        "attempt": rate_limit_attempts,
                    }
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


async def run_batch(*, concurrency: int, limit: int | None, dry_run: bool) -> None:
    data = load_checklist()
    api_key()
    prompt_template = load_prompt_template()

    pending = [
        i
        for i in data["items"]
        if i["status"] in ("pending", "in_progress", "failed")
        and not item_has_valid_report(i)
    ]
    if limit is not None:
        pending = pending[:limit]

    if not pending:
        print("Nothing to run.")
        return

    print(f"Queued {len(pending)} item(s), concurrency={concurrency}, model={MODEL}")
    if dry_run:
        for item in pending:
            print(f"  [{item['id']}] {item.get('title') or item['source_file']}")
        return

    client = GeminiClient(api_key())
    sem = asyncio.Semaphore(concurrency)
    completed = 0

    async with httpx.AsyncClient() as http:
        in_flight: dict[int, asyncio.Task[int]] = {}

        async def worker(item: dict) -> int:
            async with sem:
                item_id = await process_item(item, data, client, http, prompt_template)
                return item_id

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

            if limit is not None and completed >= limit:
                for task in in_flight.values():
                    task.cancel()
                break

    cmd_sync(argparse.Namespace())


def count_remaining(data: dict) -> int:
    return sum(
        1
        for i in data["items"]
        if i["status"] in ("pending", "in_progress", "failed")
        and not item_has_valid_report(i)
    )


async def run_until_done(
    *,
    concurrency: int,
    limit: int | None,
    dry_run: bool,
) -> None:
    round_num = 0
    while True:
        round_num += 1
        data = load_checklist()
        remaining = count_remaining(data)
        if remaining == 0:
            print("All items complete.")
            break
        print(f"\n=== Round {round_num}: {remaining} item(s) remaining ===")
        await run_batch(concurrency=concurrency, limit=limit, dry_run=dry_run)
        if dry_run or limit is not None:
            break
        data = load_checklist()
        remaining = count_remaining(data)
        if remaining == 0:
            print("All items complete.")
            break
        print(f"{remaining} item(s) still remaining — continuing in 10s...")
        await asyncio.sleep(10)


def cmd_run(args: argparse.Namespace) -> None:
    if args.until_done and not args.dry_run and args.limit is None:
        asyncio.run(
            run_until_done(
                concurrency=args.concurrency,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        )
    else:
        asyncio.run(
            run_batch(
                concurrency=args.concurrency,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gemini batch: encyclical summary → sculptural text-to-image prompt"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Build checklist from encyclicals.csv summaries")
    p_init.add_argument("--force", action="store_true", help="Rebuild even if checklist exists")
    p_init.add_argument(
        "--include-without-summary",
        action="store_true",
        help="Include CSV rows even when summary is empty",
    )

    sub.add_parser("status", help="Print checklist summary")
    sub.add_parser("sync", help="Mark complete where report files exist on disk")
    sub.add_parser("reset-stale", help="Reset stuck in_progress items")
    sub.add_parser(
        "reset-quota-failed",
        help="Reset failed items caused by insufficient API quota back to pending",
    )

    p_run = sub.add_parser("run", help="Run Gemini sculpture-prompt jobs")
    p_run.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p_run.add_argument("--limit", type=int, default=None, help="Max items this invocation")
    p_run.add_argument("--dry-run", action="store_true", help="List queue without calling API")
    p_run.add_argument(
        "--until-done",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep running until all items complete (default: true)",
    )

    args = parser.parse_args()

    if args.command == "init":
        init_checklist(
            force=args.force,
            require_summary=not args.include_without_summary,
        )
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "sync":
        cmd_sync(args)
    elif args.command == "reset-stale":
        cmd_reset_stale(args)
    elif args.command == "reset-quota-failed":
        cmd_reset_quota_failed(args)
    elif args.command == "run":
        cmd_run(args)


if __name__ == "__main__":
    main()
