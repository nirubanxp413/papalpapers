#!/usr/bin/env python3
"""Batch Gemini 3.1 Flash research for encyclicals (Step 6, Gemini tier).

Uses the Google Generative Language API. Loads GEMINI_PRO from the environment
(or project .env). Outputs to data/Gemini3.1Flash/.

    python3 scripts/gemini_search_batch.py init --include-themes Mixed
    python3 scripts/gemini_search_batch.py status
    python3 scripts/gemini_search_batch.py run --concurrency 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
SEARCH_DIR = ROOT / "data" / "Gemini3.1Flash"
PROMPT_PATH = SEARCH_DIR / "prompt.md"
REPORTS_DIR = SEARCH_DIR / "reports"
CHECKLIST_PATH = SEARCH_DIR / "checklist.json"
RUN_LOG_PATH = SEARCH_DIR / "run_log.jsonl"
ENV_PATH = ROOT / ".env"
PERPLEXITY_PROMPT = ROOT / "data" / "perplexity-search" / "prompt.md"

STEP3_REPORTS_DIR = ROOT / "data" / "subagents" / "reports"
ENCYCLOPICAL_DIR = ROOT / "data" / "encyclical"

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
MODEL = "gemini-3.1-flash-lite"
DEFAULT_CONCURRENCY = 5
MIN_REPORT_BYTES = 500
MAX_RETRIES = 4
MAX_RATE_LIMIT_RETRIES = 500
RETRY_BACKOFF = 10.0
REQUEST_INTERVAL = 0.0
RATE_LIMIT_COOLDOWN = 60.0

STATUSES = ("pending", "in_progress", "complete", "failed", "skipped")

QUOTA_ERROR_MARKERS = (
    "insufficient_quota",
    "exceeded your current quota",
    "resource_exhausted",
)

SYSTEM_INSTRUCTION = (
    "You are an investigative research analyst contributing to the Papal Papers project. "
    "Follow the exact markdown output template in the user message. "
    "The ## Gradients JSON block must appear immediately after the title with nested keys "
    "reflective and prescriptive (each with saturation 1-10 and density -30 to +30). "
    "Include all Structured and Unstructured sections. Never exceed gradient density bounds."
)

GRADIENT_CORRECTION = (
    "\n\nCORRECTION REQUIRED: density must be an integer from -30 to +30 — "
    "years from the publication date, NOT calendar years or total historical span. "
    "If the backward gaze exceeds 30 years before publication, use -30 as the cap."
)

FORMAT_REMINDER = """
---
## Final reminders (mandatory)

1. Return ONLY the markdown report — no preamble.
2. `## Gradients` with valid JSON must be the first section after the title.
3. JSON shape:
```json
{
  "reflective": {"saturation": 7, "density": -12},
  "prescriptive": {"saturation": 4, "density": 5}
}
```
4. Include `## Structured` with `### Context`, `### Reflective`, `### Prospective`, `### Temporal gradients`.
5. Include `## Unstructured` sections 1–5 and `### Citations` table.
6. Density integers must be between -30 and +30 inclusive.
"""


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
GRADIENTS_JSON_RE = re.compile(
    r"## Gradients\s*\n+```json\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
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
    for name in ("GEMINI_PRO", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        key = os.environ.get(name, "").strip()
        if key:
            return key
    raise SystemExit(
        "GEMINI_PRO not set. Add it to .env or export GEMINI_PRO / GEMINI_API_KEY."
    )


def ensure_prompt_file() -> None:
    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    if PROMPT_PATH.exists():
        return
    if not PERPLEXITY_PROMPT.exists():
        raise SystemExit(f"Prompt source not found: {PERPLEXITY_PROMPT}")
    PROMPT_PATH.write_text(PERPLEXITY_PROMPT.read_text(encoding="utf-8"), encoding="utf-8")


def append_run_log(event: dict) -> None:
    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    event = {"ts": utc_now(), **event}
    with RUN_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def load_checklist() -> dict:
    if not CHECKLIST_PATH.exists():
        raise SystemExit(f"Checklist not found: {CHECKLIST_PATH}. Run: init")
    return json.loads(CHECKLIST_PATH.read_text(encoding="utf-8"))


def save_checklist(data: dict) -> None:
    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
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
    ensure_prompt_file()
    body = PROMPT_PATH.read_text(encoding="utf-8")

    def section(pattern: re.Pattern[str]) -> str:
        match = pattern.search(body)
        return strip_todo_placeholders(match.group(1)) if match else ""

    return {
        "project_context": section(PROJECT_CONTEXT_RE),
        "instructions": section(PROMPT_SECTION_RE),
        "output_format": section(OUTPUT_SECTION_RE),
        "constraints": section(CONSTRAINTS_SECTION_RE),
    }


def parse_step3_key_theme(report_text: str) -> str | None:
    match = KEY_THEME_RE.search(report_text)
    return match.group(1).strip() if match else None


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
    return FRONTMATTER_RE.sub("", text, count=1).strip()


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
        "# Gemini research request",
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
        parts.extend(["## Project context", "", sections["project_context"], ""])
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
    parts.append(FORMAT_REMINDER)
    return "\n".join(parts).strip() + "\n"


def report_name_for(stem: str) -> str:
    return f"run-{stem}"


def report_paths_for_stem(stem: str, reports_dir: Path) -> list[Path]:
    return [
        reports_dir / f"run-{stem}.md",
        reports_dir / f"run-{stem}.md.md",
        reports_dir / f"{stem}.md",
    ]


def read_report_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def report_is_valid(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < MIN_REPORT_BYTES:
        return False
    ok, _ = validate_gradients(read_report_text(path))
    return ok


def has_valid_gemini_report(stem: str) -> bool:
    for path in report_paths_for_stem(stem, REPORTS_DIR):
        if report_is_valid(path):
            return True
    return False


def item_has_valid_report(item: dict) -> bool:
    report = ROOT / item["report_file"]
    return report_is_valid(report)


def classify_theme_bucket(theme: str | None) -> str:
    theme = (theme or "").strip()
    if not theme:
        return "empty"
    if theme == "Mixed":
        return "mixed-pure"
    if theme == "Social":
        return "social"
    if theme == "Spiritual":
        return "spiritual"
    if theme.startswith("Mixed") or "Mixed" in theme:
        return "mixed-variant"
    return "other"


def normalize_stem_quotes(stem: str) -> str:
    return (
        stem.replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )


def resolve_encyclical_path(stem: str) -> Path | None:
    candidates = [
        ENCYCLOPICAL_DIR / stem,
        ENCYCLOPICAL_DIR / normalize_stem_quotes(stem),
    ]
    for path in candidates:
        if path.exists():
            return path

    date_prefix = stem[:8] if len(stem) >= 8 and stem[:8].isdigit() else None
    if date_prefix:
        matches = sorted(ENCYCLOPICAL_DIR.glob(f"{date_prefix}_*.md"))
        normalized_target = normalize_stem_quotes(stem).casefold()
        for match in matches:
            if normalize_stem_quotes(match.name).casefold() == normalized_target:
                return match
        if len(matches) == 1:
            return matches[0]
    return None


def encyclical_stem(path: Path) -> str:
    return path.name


def build_item_from_stem(
    *,
    item_id: int,
    stem: str,
    key_theme: str | None,
    sections: dict[str, str],
    step3_report: Path | None = None,
) -> dict:
    source = resolve_encyclical_path(stem)
    if source is None:
        raise FileNotFoundError(f"Encyclical source not found for stem: {stem}")
    stem = encyclical_stem(source)
    rel_source = source.relative_to(ROOT).as_posix()
    step3_report = step3_report or (STEP3_REPORTS_DIR / report_name_for(stem))
    rel_step3 = step3_report.relative_to(ROOT).as_posix()
    out_report = REPORTS_DIR / f"{report_name_for(stem)}.md"
    rel_out = out_report.relative_to(ROOT).as_posix()

    meta = read_encyclical_meta(source)
    summary = extract_step3_summary(step3_report) if step3_report.exists() else ""
    query = build_query(
        meta=meta,
        source_file=rel_source,
        step3_report_file=rel_step3,
        step3_summary=summary,
        sections=sections,
    )

    status = "complete" if has_valid_gemini_report(stem) else "pending"
    completed_at = utc_now() if status == "complete" else None

    return {
        "id": item_id,
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
        "response_id": None,
        "batch_id": None,
        "started_at": None,
        "completed_at": completed_at,
        "error": None,
        "query_chars": len(query),
        "theme_bucket": classify_theme_bucket(key_theme),
    }


def extend_checklist_remaining(*, dry_run: bool = False) -> dict[str, int]:
    """Append or re-queue Step 3 items that lack a valid Gemini report."""
    if not CHECKLIST_PATH.exists():
        raise SystemExit(f"Checklist not found: {CHECKLIST_PATH}. Run init first.")

    ensure_prompt_file()
    sections = load_prompt_sections()
    data = load_checklist()
    items = data["items"]
    by_source = {item["source_file"]: item for item in items}

    stats = {
        "scanned": 0,
        "already_complete": 0,
        "requeued": 0,
        "added": 0,
        "missing_source": 0,
        "by_bucket": {},
    }

    next_id = max((item["id"] for item in items), default=0) + 1
    queries_dir = SEARCH_DIR / "queries"
    queries_dir.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    for step3_report in sorted(STEP3_REPORTS_DIR.glob("run-*.md")):
        stats["scanned"] += 1
        stem = step3_report.name.removeprefix("run-")
        step3_text = step3_report.read_text(encoding="utf-8", errors="replace")
        key_theme = parse_step3_key_theme(step3_text)
        bucket = classify_theme_bucket(key_theme)
        bucket_stats = stats["by_bucket"].setdefault(
            bucket, {"queued": 0, "complete": 0}
        )

        source = resolve_encyclical_path(stem)
        if source is None:
            stats["missing_source"] += 1
            continue

        stem = encyclical_stem(source)
        rel_source = source.relative_to(ROOT).as_posix()
        if has_valid_gemini_report(stem):
            stats["already_complete"] += 1
            bucket_stats["complete"] += 1
            existing = by_source.get(rel_source)
            if existing and existing["status"] != "complete":
                existing["status"] = "complete"
                existing["completed_at"] = existing.get("completed_at") or utc_now()
                existing["error"] = None
            continue

        existing = by_source.get(rel_source)
        if existing:
            existing["status"] = "pending"
            existing["error"] = None
            existing["completed_at"] = None
            existing["started_at"] = None
            existing["response_id"] = None
            existing["key_theme"] = key_theme
            existing["theme_bucket"] = bucket
            existing["step3_report_file"] = step3_report.relative_to(ROOT).as_posix()
            item = existing
            stats["requeued"] += 1
        else:
            item = build_item_from_stem(
                item_id=next_id,
                stem=stem,
                key_theme=key_theme,
                sections=sections,
                step3_report=step3_report,
            )
            items.append(item)
            by_source[rel_source] = item
            next_id += 1
            stats["added"] += 1

        bucket_stats["queued"] += 1
        query_path = queries_dir / f"{item['id']:04d}.md"
        if not dry_run:
            query_path.write_text(
                build_query(
                    meta=read_encyclical_meta(source),
                    source_file=item["source_file"],
                    step3_report_file=item["step3_report_file"],
                    step3_summary=extract_step3_summary(step3_report),
                    sections=sections,
                ),
                encoding="utf-8",
            )
        item["query_file"] = query_path.relative_to(ROOT).as_posix()
        item["query_chars"] = query_path.stat().st_size if query_path.exists() else item.get("query_chars", 0)

    data["items"] = items
    data["meta"]["total"] = len(items)
    data["meta"]["concurrency"] = DEFAULT_CONCURRENCY
    data["meta"]["extended_at"] = utc_now()

    if dry_run:
        print("Dry run — remaining queue preview:")
    else:
        save_checklist(data)
        append_run_log({"event": "extend_remaining", **stats})
        print(f"Extended checklist → {CHECKLIST_PATH}")

    print(f"Scanned Step 3 reports: {stats['scanned']}")
    print(f"Already complete (valid Gemini): {stats['already_complete']}")
    print(f"Re-queued existing items: {stats['requeued']}")
    print(f"Added new items: {stats['added']}")
    print(f"Missing encyclical source: {stats['missing_source']}")
    print(f"Total queued this pass: {stats['requeued'] + stats['added']}")
    print("By bucket:")
    for bucket, bucket_stats in sorted(stats["by_bucket"].items()):
        if bucket_stats["queued"] or bucket_stats["complete"]:
            print(f"  {bucket}: queued={bucket_stats['queued']} already_complete={bucket_stats['complete']}")
    return stats


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

    ensure_prompt_file()
    sections = load_prompt_sections()
    items: list[dict] = []
    skipped_no_step3 = 0
    skipped_theme = 0

    encyclical_files = sorted(ENCYCLOPICAL_DIR.glob("*.md"))
    if not encyclical_files:
        raise SystemExit(f"No markdown files in {ENCYCLOPICAL_DIR}")

    for source in encyclical_files:
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
        if has_valid_gemini_report(stem):
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
                "response_id": None,
                "batch_id": None,
                "started_at": None,
                "completed_at": completed_at,
                "error": None,
                "query_chars": len(query),
            }
        )

    if not items:
        raise SystemExit("No items matched init filters.")

    queries_dir = SEARCH_DIR / "queries"
    queries_dir.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    for item in items:
        query_path = queries_dir / f"{item['id']:04d}.md"
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
            "pass": "gemini-3.1-flash",
            "model": MODEL,
            "api": "generativelanguage.googleapis.com/v1beta",
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
    print(f"Gemini search checklist — {CHECKLIST_PATH}")
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
        report = ROOT / item["report_file"]
        if item_has_valid_report(item):
            item["status"] = "complete"
            item["completed_at"] = item["completed_at"] or utc_now()
        else:
            item["status"] = "pending"
            item["batch_id"] = None
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


def _axis_values(payload: dict[str, Any], *names: str) -> tuple[int | None, int | None]:
    for name in names:
        axis = payload.get(name)
        if isinstance(axis, dict):
            sat = axis.get("saturation")
            den = axis.get("density")
            if isinstance(sat, int) and isinstance(den, int):
                return sat, den
        flat_sat = payload.get(f"{name}_saturation")
        flat_den = payload.get(f"{name}_density")
        if isinstance(flat_sat, int) and isinstance(flat_den, int):
            return flat_sat, flat_den
    return None, None


def validate_gradients(content: str) -> tuple[bool, str | None]:
    match = GRADIENTS_JSON_RE.search(content)
    if not match:
        return False, "missing ## Gradients JSON block"
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        return False, f"invalid gradients JSON: {exc}"

    if not isinstance(payload, dict):
        return False, "gradients JSON must be an object"

    for axis_label, aliases in (
        ("reflective", ("reflective",)),
        ("prescriptive", ("prescriptive", "prospective")),
    ):
        sat, den = _axis_values(payload, *aliases)
        if sat is None or den is None:
            return False, f"missing {axis_label} saturation/density"
        if not 1 <= sat <= 10:
            return False, f"{axis_label} saturation out of range: {sat}"
        if not -30 <= den <= 30:
            return False, f"{axis_label} density out of range: {den}"

    return True, None


def write_report(item: dict, content: str, raw: dict[str, Any] | None = None) -> None:
    report_path = ROOT / item["report_file"]
    report_path.parent.mkdir(parents=True, exist_ok=True)

    frontmatter = {
        "source_file": item["source_file"],
        "step3_report_file": item["step3_report_file"],
        "report_file": item["report_file"],
        "pass": "step-6-gemini-3.1-flash",
        "model": MODEL,
        "pope": item.get("pope"),
        "title": item.get("title"),
        "published_date": item.get("published_date"),
        "source_url": item.get("source_url"),
        "alternate_source_url": item.get("alternate_source_url"),
        "key_theme": item.get("key_theme"),
        "response_id": item.get("response_id"),
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
                "temperature": 0.2,
                "maxOutputTokens": 16384,
            },
        }
        resp = await client.post(
            url,
            params={"key": self._key},
            json=body,
            timeout=300.0,
        )
        resp.raise_for_status()
        return resp.json()


async def process_item(
    item: dict,
    data: dict,
    client: GeminiClient,
    http: httpx.AsyncClient,
) -> int:
    item_id = item["id"]
    query_path = ROOT / item["query_file"]
    query = query_path.read_text(encoding="utf-8")

    item["status"] = "in_progress"
    item["started_at"] = item["started_at"] or utc_now()
    save_checklist(data)

    last_error: str | None = None
    rate_limit_attempts = 0
    gradient_correction_used = False
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            payload = await client.complete(http, query)
            content = extract_gemini_content(payload)
            ok, reason = validate_gradients(content)
            if not ok:
                if "density out of range" in (reason or "") and not gradient_correction_used:
                    gradient_correction_used = True
                    query = query + GRADIENT_CORRECTION
                    last_error = reason
                    continue
                raise RuntimeError(reason or "invalid gradients")

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
                item_id = await process_item(item, data, client, http)
                if REQUEST_INTERVAL > 0:
                    await asyncio.sleep(REQUEST_INTERVAL)
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


def cmd_extend_remaining(args: argparse.Namespace) -> None:
    extend_checklist_remaining(dry_run=args.dry_run)


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
        description="Gemini 3.1 Flash batch runner (Step 6, Gemini tier)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Build checklist from encyclicals + Step 3 reports")
    p_init.add_argument("--force", action="store_true", help="Rebuild even if checklist exists")
    p_init.add_argument(
        "--include-themes",
        default="Mixed",
        help="Comma-separated Key theme values (default: Mixed). Use 'all' for every Step 3 report.",
    )
    p_init.add_argument(
        "--no-require-step3",
        action="store_true",
        help="Include encyclicals even if Step 3 report is missing",
    )

    sub.add_parser("status", help="Print checklist summary")
    sub.add_parser("sync", help="Mark complete where report files exist on disk")
    sub.add_parser("reset-stale", help="Reset stuck in_progress items")
    sub.add_parser(
        "reset-quota-failed",
        help="Reset failed items caused by insufficient API quota back to pending",
    )
    p_extend = sub.add_parser(
        "extend-remaining",
        help="Queue Step 3 items lacking a valid Gemini report (Spiritual, variants, gaps)",
    )
    p_extend.add_argument("--dry-run", action="store_true", help="Preview without writing checklist")

    p_run = sub.add_parser("run", help="Run Gemini research jobs (sync API, batched)")
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
    elif args.command == "reset-quota-failed":
        cmd_reset_quota_failed(args)
    elif args.command == "extend-remaining":
        cmd_extend_remaining(args)
    elif args.command == "run":
        cmd_run(args)


if __name__ == "__main__":
    main()
