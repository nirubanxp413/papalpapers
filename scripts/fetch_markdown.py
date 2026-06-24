#!/usr/bin/env python3
"""Download encyclicals from CSV URLs and save as markdown (Step 1)."""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import html2text
import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CSV_PATH = DATA_DIR / "encyclicals.csv"
OUTPUT_DIR = DATA_DIR / "encyclical"
LOG_PATH = DATA_DIR / "fetch_log.json"
API_BASE = "https://www.papalencyclicals.net/wp-json/wp/v2"
REQUEST_DELAY = 1.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class Row:
    pope: str
    title: str
    subtitle: str
    published_date: str
    doc_type: str
    format: str
    link: str
    source: str


def format_date_ddmmyyyy(iso_date: str) -> str:
    if not iso_date or len(iso_date) < 10:
        return "00000000"
    year, month, day = iso_date[:10].split("-")
    return f"{day}{month}{year}"


def sanitize_filename_part(text: str, max_len: int = 100) -> str:
    text = BeautifulSoup(text, "html.parser").get_text()
    text = re.sub(r'[\\/:*?"<>|]', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len].rstrip(" .")


def build_filename(row: Row, used: set[str]) -> str:
    date_part = format_date_ddmmyyyy(row.published_date)
    pope_part = sanitize_filename_part(row.pope)
    title_part = sanitize_filename_part(row.title)
    base = f"{date_part}_{pope_part}_{title_part}"
    candidate = f"{base}.md"
    if candidate not in used:
        used.add(candidate)
        return candidate

    slug = sanitize_filename_part(Path(row.link).stem or "doc")
    n = 2
    while True:
        candidate = f"{base}_{slug}_{n}.md"
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1


def make_converter() -> html2text.HTML2Text:
    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_links = False
    converter.ignore_images = True
    converter.ignore_emphasis = False
    converter.single_line_break = False
    converter.protect_links = True
    converter.unicode_snob = True
    return converter


def strip_noise(root: Tag) -> None:
    for tag in root.find_all(["script", "style", "nav", "header", "footer", "form", "noscript"]):
        tag.decompose()

    noise_classes = re.compile(
        r"(share|social|breadcrumb|menu|sidebar|comment|related|language|utility|"
        r"va-menu|va-search|translation-field|translation|doc-copyright|copyright|"
        r"entry-meta|continue-reading|post-navigation|tags|author)",
        re.I,
    )
    for tag in root.find_all(class_=noise_classes):
        tag.decompose()

    for tag in root.find_all("a", string=re.compile(r"Continue reading", re.I)):
        tag.decompose()


def extract_vatican(soup: BeautifulSoup) -> Tag | None:
    root = soup.select_one(".testo") or soup.select_one(".documento")
    if root is None:
        return soup.body

    strip_noise(root)
    return root


def extract_papalencyclicals(soup: BeautifulSoup) -> Tag | None:
    for selector in ("article.post", "article.hentry", "article", ".entry-content", "main"):
        node = soup.select_one(selector)
        if node and node.get_text(strip=True):
            return node
    return soup.body


def slug_from_papalencyclicals_url(url: str) -> str:
    path = Path(urlparse(url).path)
    stem = path.stem
    return stem


def fetch_wp_api_html(client: httpx.Client, url: str) -> str | None:
    if "papalencyclicals.net" not in url:
        return None
    slug = slug_from_papalencyclicals_url(url)
    response = client.get(f"{API_BASE}/posts", params={"slug": slug})
    response.raise_for_status()
    posts = response.json()
    if not posts:
        return None
    content = posts[0].get("content", {}).get("rendered", "")
    return content if len(content) > 100 else None


def extract_content_html(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if "vatican.va" in url:
        root = extract_vatican(soup)
    else:
        root = extract_papalencyclicals(soup)

    if root is None:
        raise ValueError("Could not locate document content")

    strip_noise(root)

    for tag in root.find_all(["h1", "h2"]):
        classes = " ".join(tag.get("class", []))
        if re.search(r"entry-title|page-title|site-title", classes):
            tag.decompose()

    return str(root)


def html_to_markdown(html_fragment: str) -> str:
    converter = make_converter()
    return converter.handle(html_fragment).strip()


def build_markdown(row: Row, body_md: str, *, alternate_source: str | None = None) -> str:
    lines = [f"# {row.title}"]
    if row.subtitle:
        lines.append("")
        lines.append(f"*{row.subtitle}*")
    lines.extend(["", "---", f"pope: {row.pope}", f"title: {row.title}"])
    if row.subtitle:
        lines.append(f"subtitle: {row.subtitle}")
    if row.published_date:
        lines.append(f"published_date: {row.published_date}")
    lines.extend([f"doc_type: {row.doc_type}", f"source: {row.link}"])
    if alternate_source:
        lines.append(f"alternate_source: {alternate_source}")
    lines.extend(["---", ""])
    lines.append(body_md)
    return "\n".join(lines)


def fetch_document_from_url(
    client: httpx.Client,
    row: Row,
    url: str,
    *,
    alternate_source: str | None = None,
) -> str:
    response = client.get(url, follow_redirects=True)
    response.raise_for_status()

    content_type = response.headers.get("content-type", "").lower()
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        raise ValueError("PDF documents are not supported yet")

    html_fragment = extract_content_html(response.text, str(response.url))
    body_md = html_to_markdown(html_fragment)
    if len(body_md) < 100:
        raise ValueError("No document body on alternate source page")

    resolved_source = alternate_source or url
    return build_markdown(row, body_md, alternate_source=resolved_source)


def fetch_document(client: httpx.Client, row: Row) -> str:
    html_fragment: str | None = None

    if "papalencyclicals.net" in row.link:
        html_fragment = fetch_wp_api_html(client, row.link)

    if not html_fragment:
        response = client.get(row.link, follow_redirects=True)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        if "pdf" in content_type or row.link.lower().endswith(".pdf"):
            raise ValueError("PDF documents are not supported yet")

        html_fragment = extract_content_html(response.text, str(response.url))

    body_md = html_to_markdown(html_fragment)
    if len(body_md) < 100:
        raise ValueError("No document body on source page (empty or stub entry)")

    return build_markdown(row, body_md)


def load_rows() -> list[Row]:
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        return [
            Row(
                pope=r["pope"],
                title=r["title"],
                subtitle=r.get("subtitle", ""),
                published_date=r.get("published_date", ""),
                doc_type=r.get("doc_type", ""),
                format=r.get("format", "html"),
                link=r["link"],
                source=r.get("source", ""),
            )
            for r in csv.DictReader(f)
        ]


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = load_rows()
    used_names: set[str] = set()
    log: dict[str, list] = {"success": [], "skipped": [], "failed": []}
    last_request = 0.0

    with httpx.Client(headers=HEADERS, timeout=60.0) as client:
        for i, row in enumerate(rows, 1):
            filename = build_filename(row, used_names)
            out_path = OUTPUT_DIR / filename

            if out_path.exists() and out_path.stat().st_size > 200:
                log["skipped"].append({"file": filename, "link": row.link})
                print(f"[{i}/{len(rows)}] skip {filename}", file=sys.stderr)
                continue

            elapsed = time.monotonic() - last_request
            if elapsed < REQUEST_DELAY:
                time.sleep(REQUEST_DELAY - elapsed)

            try:
                print(f"[{i}/{len(rows)}] fetch {row.title[:50]}...", file=sys.stderr)
                markdown = fetch_document(client, row)
                out_path.write_text(markdown, encoding="utf-8")
                log["success"].append({"file": filename, "link": row.link})
            except Exception as exc:  # noqa: BLE001
                log["failed"].append(
                    {"file": filename, "link": row.link, "error": str(exc)}
                )
                print(f"  FAILED: {exc}", file=sys.stderr)
            finally:
                last_request = time.monotonic()

    LOG_PATH.write_text(json.dumps(log, indent=2))
    print(
        f"\nDone: {len(log['success'])} saved, "
        f"{len(log['skipped'])} skipped, {len(log['failed'])} failed",
        file=sys.stderr,
    )
    if log["failed"]:
        print(f"See {LOG_PATH} for failures", file=sys.stderr)


if __name__ == "__main__":
    main()
