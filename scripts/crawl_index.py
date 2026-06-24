#!/usr/bin/env python3
"""Crawl papalencyclicals.net and build an index CSV (Step 0)."""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

BASE_URL = "https://www.papalencyclicals.net"
API_BASE = f"{BASE_URL}/wp-json/wp/v2"
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
REQUEST_DELAY = 1.0

SKIP_SECTIONS = {
    "additional links",
}

DOC_TYPE_MAP = {
    "encyclicals": "encyclical",
    "other writings": "other",
    "apostolic constitutions": "apostolic_constitution",
    "apostolic exhortations": "apostolic_exhortation",
    "apostolic letters": "apostolic_letter",
    "apostolic letter": "apostolic_letter",
    "motu proprio": "motu_proprio",
    "motu proprios": "motu_proprio",
    "bulls": "bull",
    "addresses": "address",
    "homilies": "homily",
    "messages": "message",
    "speeches": "speech",
}

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


@dataclass
class DocumentRow:
    pope: str
    title: str
    subtitle: str = ""
    published_date: str = ""
    doc_type: str = "document"
    format: str = "html"
    link: str = ""
    source: str = "papalencyclicals"
    pope_url: str = ""
    notes: str = ""


@dataclass
class PopeEntry:
    name: str
    url: str
    slug: str
    source_type: str  # "category" or "page"


class Crawler:
    def __init__(self) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(
            timeout=30.0,
            headers={"User-Agent": "papalpapers-index/1.0 (research project)"},
            follow_redirects=True,
        )
        self._category_by_slug: dict[str, dict[str, Any]] = {}
        self._last_request = 0.0

    def close(self) -> None:
        self.client.close()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        self._last_request = time.monotonic()

    def _cache_path(self, key: str) -> Path:
        safe = re.sub(r"[^\w.-]", "_", key)
        return CACHE_DIR / f"{safe}.json"

    def fetch_json(self, url: str, cache_key: str | None = None) -> Any:
        key = cache_key or url
        path = self._cache_path(key)
        if path.exists():
            return json.loads(path.read_text())

        self._throttle()
        response = self.client.get(url)
        response.raise_for_status()
        data = response.json()
        path.write_text(json.dumps(data, indent=2))
        return data

    def fetch_all_paginated(self, url: str, cache_prefix: str) -> list[dict[str, Any]]:
        page = 1
        items: list[dict[str, Any]] = []
        total_pages = 1

        while page <= total_pages:
            sep = "&" if "?" in url else "?"
            page_url = f"{url}{sep}page={page}"
            cache_key = f"{cache_prefix}_page_{page}"
            path = self._cache_path(cache_key)

            if path.exists():
                batch = json.loads(path.read_text())
                if page == 1:
                    meta_path = self._cache_path(f"{cache_prefix}_meta")
                    if meta_path.exists():
                        total_pages = json.loads(meta_path.read_text())["total_pages"]
            else:
                self._throttle()
                response = self.client.get(page_url)
                response.raise_for_status()
                batch = response.json()
                path.write_text(json.dumps(batch, indent=2))
                if page == 1:
                    total_pages = int(response.headers.get("X-WP-TotalPages", 1))
                    meta_path = self._cache_path(f"{cache_prefix}_meta")
                    meta_path.write_text(json.dumps({"total_pages": total_pages}))

            if not batch:
                break
            items.extend(batch)
            page += 1

        return items

    def load_categories(self) -> None:
        categories = self.fetch_json(f"{API_BASE}/categories?per_page=100", "categories_all")
        for cat in categories:
            self._category_by_slug[cat["slug"]] = cat

    def fetch_popes(self) -> list[PopeEntry]:
        pages = self.fetch_json(f"{API_BASE}/pages?slug=popelist", "page_popelist")
        if not pages:
            raise RuntimeError("Could not fetch popelist page")

        html = pages[0]["content"]["rendered"]
        soup = BeautifulSoup(html, "html.parser")
        popes: list[PopeEntry] = []

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if "papalencyclicals.net" not in href:
                continue
            if any(
                skip in href
                for skip in ("/councils", "/about", "/document-directory", "/popelist", "/feed")
            ):
                continue

            name = clean_pope_name(normalize_text(anchor.get_text()))
            if not name:
                continue

            url = normalize_url(href)
            parsed = urlparse(url)
            path = parsed.path.strip("/")

            if path.startswith("category/"):
                slug = path.split("/", 1)[1]
                source_type = "category"
            else:
                slug = path.split("/")[-1]
                source_type = "page"

            popes.append(PopeEntry(name=name, url=url, slug=slug, source_type=source_type))

        # Dedupe by URL while preserving order
        seen: set[str] = set()
        unique: list[PopeEntry] = []
        for pope in popes:
            if pope.url not in seen:
                seen.add(pope.url)
                unique.append(pope)
        return unique

    def crawl_category_pope(self, pope: PopeEntry) -> list[DocumentRow]:
        cat = self._category_by_slug.get(pope.slug)
        if not cat:
            return []

        posts = self.fetch_all_paginated(
            f"{API_BASE}/posts?categories={cat['id']}&per_page=100",
            f"posts_cat_{pope.slug}",
        )
        rows: list[DocumentRow] = []
        for post in posts:
            link = normalize_url(post["link"])
            title = decode_html(post["title"]["rendered"]).strip()
            if not title:
                title = slug_to_title(post.get("slug", ""))

            rows.append(
                DocumentRow(
                    pope=pope.name,
                    title=title,
                    subtitle="",
                    published_date=normalize_date(post.get("date", "")),
                    doc_type=infer_doc_type_from_url(link),
                    format=infer_format(link),
                    link=link,
                    source="papalencyclicals" if BASE_URL in link else "external",
                    pope_url=pope.url,
                )
            )
        return rows

    def crawl_page_pope(self, pope: PopeEntry) -> list[DocumentRow]:
        pages = self.fetch_json(f"{API_BASE}/pages?slug={pope.slug}", f"page_{pope.slug}")
        if not pages:
            # Some older popes use category archives only; try category page slug
            return []

        html = pages[0]["content"]["rendered"]
        return parse_pope_page_html(html, pope.name, pope.url)

    def crawl_all(self) -> list[DocumentRow]:
        self.load_categories()
        popes = self.fetch_popes()
        (DATA_DIR / "popes.json").write_text(
            json.dumps([asdict(p) for p in popes], indent=2)
        )

        all_rows: list[DocumentRow] = []
        for pope in popes:
            print(f"Crawling {pope.name} ({pope.source_type})...", file=sys.stderr)
            if pope.source_type == "category":
                rows = self.crawl_category_pope(pope)
            else:
                rows = self.crawl_page_pope(pope)
                # Also pull category posts when a matching category exists (e.g. paul06)
                if pope.slug in self._category_by_slug:
                    cat_rows = self.crawl_category_pope(
                        PopeEntry(
                            name=pope.name,
                            url=pope.url,
                            slug=pope.slug,
                            source_type="category",
                        )
                    )
                    rows.extend(cat_rows)

            print(f"  → {len(rows)} raw rows", file=sys.stderr)
            all_rows.extend(rows)

        return dedupe_rows(all_rows)


def normalize_url(url: str) -> str:
    url = url.strip().replace("//franc", "/franc").replace("//ben16", "/ben16")
    url = re.sub(r"(https://www\.papalencyclicals\.net)/+", r"\1/", url)
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_pope_name(name: str) -> str:
    name = normalize_text(name)
    if "(" in name:
        return name.split("(", 1)[0].strip()
    return name


def is_index_page(link: str) -> bool:
    lower = link.lower().rstrip("/")
    if lower.endswith("index.htm") or lower.endswith("index_en.htm"):
        return True
    if ".index.html" in lower:
        return True
    if re.search(r"/index\.html?$", lower):
        return True
    return False


def decode_html(text: str) -> str:
    return BeautifulSoup(text, "html.parser").get_text()


def slug_to_title(slug: str) -> str:
    return slug.replace("-", " ").title()


def infer_format(link: str) -> str:
    lower = link.lower()
    if lower.endswith(".pdf"):
        return "pdf"
    return "html"


def infer_doc_type_from_url(link: str) -> str:
    lower = link.lower()
    if "/councils/" in lower:
        return "council"
    if "/encyclicals/" in lower:
        return "encyclical"
    if "apost_exhortations" in lower:
        return "apostolic_exhortation"
    if "apost_constitutions" in lower:
        return "apostolic_constitution"
    return "document"


def normalize_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def section_to_doc_type(header: str) -> str:
    key = header.strip().lower()
    if key in SKIP_SECTIONS:
        return ""
    return DOC_TYPE_MAP.get(key, re.sub(r"\s+", "_", key))


def parse_published_date(text: str) -> str:
    text = normalize_text(text)

    iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        return iso.group(0)

    # "May 15, 2026" or "3 October 2020" or "Encyclical (3 October 2020)"
    m = re.search(
        r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
        text,
        re.I,
    )
    if m:
        day, month, year = int(m.group(1)), MONTHS[m.group(2).lower()], int(m.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"

    m = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})",
        text,
        re.I,
    )
    if m:
        month, day, year = MONTHS[m.group(1).lower()], int(m.group(2)), int(m.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"

    return ""


def parse_subtitle(text: str) -> str:
    m = re.search(r"\(([^)]+)\)", text)
    if m:
        candidate = m.group(1).strip()
        if not re.search(r"\d{4}", candidate) and "encyclical" not in candidate.lower():
            return candidate
    return ""


def pick_primary_link(anchors: list[Tag]) -> str | None:
    for anchor in anchors:
        href = anchor.get("href", "").strip()
        if not href or href.startswith("mailto:"):
            continue
        if "vatican.va" in href and "/en/" in href:
            return href
    for anchor in anchors:
        href = anchor.get("href", "").strip()
        if href and not href.startswith("mailto:"):
            return href
    return None


def parse_list_item(li: Tag, pope: str, pope_url: str, doc_type: str) -> DocumentRow | None:
    anchors = li.find_all("a", href=True)
    if not anchors:
        return None

    link = pick_primary_link(anchors)
    if not link:
        return None

    link = normalize_url(link)

    # Skip Vatican index pages (not individual documents)
    if is_index_page(link):
        return None
    if any(
        seg in link
        for seg in (
            ".index.html",
            "/angelus.index",
            "/homilies.index",
            "/messages.index",
            "/speeches.index",
        )
    ):
        return None

    title = normalize_text(anchors[0].get_text()).rstrip(":")
    plain = normalize_text(li.get_text())

    subtitle = parse_subtitle(plain)
    published_date = parse_published_date(plain)

    source = "vatican" if "vatican.va" in link else "papalencyclicals"
    if not doc_type or doc_type == "document":
        doc_type = infer_doc_type_from_url(link)

    return DocumentRow(
        pope=pope,
        title=title,
        subtitle=subtitle,
        published_date=published_date,
        doc_type=doc_type,
        format=infer_format(link),
        link=link,
        source=source,
        pope_url=pope_url,
    )


def parse_pope_page_html(html: str, pope: str, pope_url: str) -> list[DocumentRow]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[DocumentRow] = []
    current_type = "document"

    for element in soup.find_all(["h3", "li"]):
        if element.name == "h3":
            current_type = section_to_doc_type(element.get_text()) or "document"
            continue

        if current_type == "":
            continue

        row = parse_list_item(element, pope, pope_url, current_type)
        if row:
            rows.append(row)

    return rows


def link_key(link: str) -> str:
    parsed = urlparse(link.lower().rstrip("/"))
    return f"{parsed.netloc}{parsed.path}"


def dedupe_rows(rows: list[DocumentRow]) -> list[DocumentRow]:
    seen: dict[str, DocumentRow] = {}
    for row in rows:
        key = link_key(row.link)
        if key in seen:
            existing = seen[key]
            if not existing.subtitle and row.subtitle:
                existing.subtitle = row.subtitle
            if not existing.published_date and row.published_date:
                existing.published_date = row.published_date
            existing.notes = "deduped"
        else:
            seen[key] = row
    return sorted(seen.values(), key=lambda r: (r.pope, r.published_date, r.title))


def write_csv(rows: list[DocumentRow], path: Path) -> None:
    fieldnames = [
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
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_summary(rows: list[DocumentRow], path: Path) -> None:
    summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = summary.setdefault(
            row.pope,
            {"count": 0, "doc_types": {}, "earliest": None, "latest": None, "pope_url": row.pope_url},
        )
        entry["count"] += 1
        entry["doc_types"][row.doc_type] = entry["doc_types"].get(row.doc_type, 0) + 1
        if row.published_date:
            if not entry["earliest"] or row.published_date < entry["earliest"]:
                entry["earliest"] = row.published_date
            if not entry["latest"] or row.published_date > entry["latest"]:
                entry["latest"] = row.published_date

    path.write_text(json.dumps(summary, indent=2, sort_keys=True))


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    crawler = Crawler()
    try:
        rows = crawler.crawl_all()
    finally:
        crawler.close()

    csv_path = DATA_DIR / "encyclicals.csv"
    summary_path = DATA_DIR / "summary.json"
    write_csv(rows, csv_path)
    write_summary(rows, summary_path)

    print(f"Wrote {len(rows)} documents to {csv_path}", file=sys.stderr)
    print(f"Wrote summary to {summary_path}", file=sys.stderr)

    # Quick validation hints
    checks = [
        ("Leo XIII", 88),
        ("Pius XII", 60),
    ]
    for name, expected in checks:
        actual = sum(1 for r in rows if r.pope.startswith(name))
        status = "OK" if actual == expected else "CHECK"
        print(f"  [{status}] {name}: {actual} (expected ~{expected})", file=sys.stderr)

    francis_enc = sum(
        1 for r in rows if "Francis" in r.pope and r.doc_type == "encyclical"
    )
    leo14_enc = sum(
        1 for r in rows if "Leo XIV" in r.pope and r.doc_type == "encyclical"
    )
    print(f"  Francis encyclicals: {francis_enc} (expected 4)", file=sys.stderr)
    print(f"  Leo XIV encyclicals: {leo14_enc} (expected 1)", file=sys.stderr)


if __name__ == "__main__":
    main()
