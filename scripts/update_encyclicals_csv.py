#!/usr/bin/env python3
"""Merge Step 3 themes/summaries and Step 6 gradients into data/encyclicals.csv."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "encyclicals.csv"
STEP3_REPORTS_DIR = ROOT / "data" / "subagents" / "reports"
DEEP_RESEARCH_REPORTS_DIR = ROOT / "data" / "deep-research" / "reports"

KEY_THEME_RE = re.compile(r"^\s*-\s*\*\*Key theme:\*\*\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
SUMMARY_RE = re.compile(r"^\s*-\s*\*\*Summary:\*\*\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE | re.DOTALL)
GRADIENTS_JSON_RE = re.compile(
    r"## Gradients\s*\n+(?:.*?\n+)?```json\s*\n(\{.*?\})\s*\n```",
    re.DOTALL | re.IGNORECASE,
)

BASE_COLUMNS = [
    "pope",
    "title",
    "subtitle",
    "published_date",
    "doc_type",
    "format",
    "link",
    "source",
    "pope_url",
    "notes",
]

ANALYSIS_COLUMNS = [
    "category",
    "summary",
    "reflective_saturation",
    "reflective_density",
    "prescriptive_saturation",
    "prescriptive_density",
]


@dataclass
class AnalysisFields:
    category: str = ""
    summary: str = ""
    reflective_saturation: str = ""
    reflective_density: str = ""
    prescriptive_saturation: str = ""
    prescriptive_density: str = ""


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


def report_path_for_stem(stem: str, reports_dir: Path) -> Path | None:
    candidates = [
        reports_dir / f"run-{stem}.md",
        reports_dir / f"run-{stem}.md.md",  # Step 6 batch uses source.name (includes .md)
        reports_dir / f"{stem}.md",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def parse_step3_report(text: str) -> AnalysisFields:
    fields = AnalysisFields()
    theme_match = KEY_THEME_RE.search(text)
    if theme_match:
        fields.category = theme_match.group(1).strip()

    summary_match = SUMMARY_RE.search(text)
    if summary_match:
        fields.summary = summary_match.group(1).strip()
    return fields


def parse_gradients(text: str) -> dict[str, dict[str, int]] | None:
    match = GRADIENTS_JSON_RE.search(text)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def apply_gradients(fields: AnalysisFields, gradients: dict[str, Any]) -> None:
    axis_aliases = {
        "reflective": ("reflective",),
        "prescriptive": ("prescriptive", "prospective"),
    }

    for target_axis, source_axes in axis_aliases.items():
        for source_axis in source_axes:
            axis_data = gradients.get(source_axis)
            if isinstance(axis_data, dict):
                saturation = axis_data.get("saturation")
                density = axis_data.get("density")
                if saturation is not None:
                    setattr(fields, f"{target_axis}_saturation", str(saturation))
                if density is not None:
                    setattr(fields, f"{target_axis}_density", str(density))
                break

            flat_saturation = gradients.get(f"{source_axis}_saturation")
            flat_density = gradients.get(f"{source_axis}_density")
            if flat_saturation is not None or flat_density is not None:
                if flat_saturation is not None:
                    setattr(fields, f"{target_axis}_saturation", str(flat_saturation))
                if flat_density is not None:
                    setattr(fields, f"{target_axis}_density", str(flat_density))
                break


def load_analysis_for_stem(stem: str) -> AnalysisFields:
    fields = AnalysisFields()

    step3_path = report_path_for_stem(stem, STEP3_REPORTS_DIR)
    if step3_path:
        step3_text = step3_path.read_text(encoding="utf-8", errors="replace")
        fields = parse_step3_report(step3_text)

    deep_path = report_path_for_stem(stem, DEEP_RESEARCH_REPORTS_DIR)
    if deep_path:
        deep_text = deep_path.read_text(encoding="utf-8", errors="replace")
        gradients = parse_gradients(deep_text)
        if gradients:
            apply_gradients(fields, gradients)

    return fields


def ordered_fieldnames(existing_rows: list[dict[str, str]]) -> list[str]:
    if not existing_rows:
        return BASE_COLUMNS + ANALYSIS_COLUMNS

    existing = list(existing_rows[0].keys())
    fieldnames: list[str] = []
    for column in BASE_COLUMNS:
        if column in existing:
            fieldnames.append(column)
    for column in existing:
        if column not in fieldnames and column not in ANALYSIS_COLUMNS:
            fieldnames.append(column)
    for column in ANALYSIS_COLUMNS:
        fieldnames.append(column)
    return fieldnames


def update_csv(*, dry_run: bool = False) -> dict[str, int]:
    if not CSV_PATH.exists():
        raise SystemExit(f"CSV not found: {CSV_PATH}")

    with CSV_PATH.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    used_stems: set[str] = set()
    stats = {
        "rows": len(rows),
        "category": 0,
        "summary": 0,
        "gradients": 0,
        "missing_reports": 0,
    }

    updated_rows: list[dict[str, str]] = []
    for row in rows:
        stem = build_stem(row, used_stems)
        analysis = load_analysis_for_stem(stem)

        if not report_path_for_stem(stem, STEP3_REPORTS_DIR):
            stats["missing_reports"] += 1

        if analysis.category:
            stats["category"] += 1
        if analysis.summary:
            stats["summary"] += 1
        if analysis.reflective_saturation or analysis.prescriptive_saturation:
            stats["gradients"] += 1

        updated = dict(row)
        updated["category"] = analysis.category
        updated["summary"] = analysis.summary
        updated["reflective_saturation"] = analysis.reflective_saturation
        updated["reflective_density"] = analysis.reflective_density
        updated["prescriptive_saturation"] = analysis.prescriptive_saturation
        updated["prescriptive_density"] = analysis.prescriptive_density
        updated_rows.append(updated)

    fieldnames = ordered_fieldnames(rows)

    if dry_run:
        print(f"Dry run — would update {CSV_PATH}")
    else:
        with CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(updated_rows)
        print(f"Updated {CSV_PATH}")

    print(f"Rows: {stats['rows']}")
    print(f"Category filled: {stats['category']}")
    print(f"Summary filled: {stats['summary']}")
    print(f"Gradient sets filled: {stats['gradients']}")
    print(f"Rows without Step 3 report: {stats['missing_reports']}")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Update encyclicals.csv with category, summary, and gradient columns."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse reports and print stats without writing the CSV.",
    )
    args = parser.parse_args()

    try:
        update_csv(dry_run=args.dry_run)
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - CLI guardrail
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
