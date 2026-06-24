#!/usr/bin/env python3
"""Recover failed encyclical downloads via Vatican and other alternate sources."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_markdown import (  # noqa: E402
    CSV_PATH,
    HEADERS,
    LOG_PATH,
    OUTPUT_DIR,
    REQUEST_DELAY,
    Row,
    build_markdown,
    fetch_document_from_url,
    html_to_markdown,
    load_rows,
    strip_noise,
)

CACHE_PATH = ROOT / "data" / "cache" / "vatican_index.json"
RECOVERY_LOG_PATH = ROOT / "data" / "recovery_log.json"

SKIP_LINKS = {
    "http://w2.vatican.va/content/john-paul-ii/en/speeches.html",
    "http://w2.vatican.va/content/john-paul-ii/en/homilies.html",
    "https://www.papalencyclicals.net/paul06/1293.htm",
}

MANUAL_BY_LINK: dict[str, str] = {
    "https://www.papalencyclicals.net/leo13/longinqua.htm": (
        "http://www.vatican.va/content/leo-xiii/en/encyclicals/documents/"
        "hf_l-xiii_enc_06011895_longinqua.html"
    ),
    "https://www.papalencyclicals.net/paul06/caritas-in-veritate.htm": (
        "http://www.vatican.va/holy_father/benedict_xvi/encyclicals/documents/"
        "hf_ben-xvi_enc_20090629_caritas-in-veritate_en.html"
    ),
    "https://www.papalencyclicals.net/pius10/septimo-iam.htm": (
        "https://www.vatican.va/content/pius-x/la/apost_letters/documents/"
        "hf_p-x_apl_19090709_septimo-iam.html"
    ),
    "https://www.papalencyclicals.net/ben14/celebationem-magni.htm": (
        "https://www.vatican.va/content/benedictus-xiv/it/documents/"
        "enciclica--i-celebrationem-magni--i---1-gennaio-1751--dopo-aver-.html"
    ),
    "https://www.papalencyclicals.net/greg13/inter-gravissimas.htm": (
        "https://www.thelatinlibrary.com/gravissimas.html"
    ),
    "https://www.papalencyclicals.net/alex04/clara-claris-praeclara.htm": (
        "https://franciscan-archive.org/bullarium/clara.html"
    ),
    "https://www.papalencyclicals.net/pius09/dives-misericordia-deus.htm": (
        "https://www.vatican.va/content/john-paul-ii/en/encyclicals/documents/"
        "hf_jp-ii_enc_30111980_dives-in-misericordia.html"
    ),
    "https://www.papalencyclicals.net/pius11/rerum-condicio.htm": (
        "https://franciscan-archive.org/bullarium/rerumcon.html"
    ),
    "https://www.papalencyclicals.net/ben14/sollicita-ac-provida.htm": (
        "dco:https://documentacatholicaomnia.eu/04z/"
        "z_1753-07-09__SS_Benedictus_XIV__Sollicita_ac_Provida__LT.doc.html"
    ),
}

VATICAN_INDEX: dict[str, list[str]] = {
    "Benedict XIV": ["https://www.vatican.va/content/benedictus-xiv/it.html"],
    "Bl. Pius IX": ["https://www.vatican.va/content/pius-ix/it.html"],
    "Clement XIII": ["https://www.vatican.va/content/clemens-xiii/it.html"],
    "Leo XII": ["https://www.vatican.va/content/leo-xii/it.html"],
    "Paul VI": ["https://www.vatican.va/content/paul-vi/it.html"],
    "Pius VI": ["https://www.vatican.va/content/pius-vi/it.html"],
    "Pius VII": ["https://www.vatican.va/content/pius-vii/it.html"],
    "Pius XI": ["https://www.vatican.va/content/pius-xi/it.html"],
    "St. Pius X": [
        "https://www.vatican.va/content/pius-x/it.html",
        "https://www.vatican.va/content/pius-x/la/apost_letters.html",
        "https://www.vatican.va/content/pius-x/en/apost_letters.html",
    ],
}

TITLE_ALIASES = {
    "celebationem": "celebrationem",
}


@dataclass
class Resolved:
    url: str
    method: str
    note: str = ""


def normalize_title(text: str) -> str:
    text = BeautifulSoup(text, "html.parser").get_text()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    for src, dst in TITLE_ALIASES.items():
        text = text.replace(src, dst)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_words(text: str) -> set[str]:
    words = normalize_title(text).split()
    return {w for w in words if len(w) > 2}


def scrape_vatican_index(client: httpx.Client, url: str) -> list[tuple[str, str]]:
    response = client.get(url, follow_redirects=True)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    links: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if ".html" not in href:
            continue
        text = anchor.get_text(" ", strip=True)
        if len(text) < 4:
            continue
        links.append((text, urljoin(str(response.url), href)))
    return links


def load_vatican_cache(client: httpx.Client) -> dict[str, list[tuple[str, str]]]:
    if CACHE_PATH.exists():
        raw = json.loads(CACHE_PATH.read_text())
        return {pope: [tuple(item) for item in items] for pope, items in raw.items()}

    cache: dict[str, list[tuple[str, str]]] = {}
    for pope, urls in VATICAN_INDEX.items():
        merged: list[tuple[str, str]] = []
        seen: set[str] = set()
        for url in urls:
            try:
                for text, doc_url in scrape_vatican_index(client, url):
                    if doc_url not in seen:
                        seen.add(doc_url)
                        merged.append((text, doc_url))
            except httpx.HTTPError as exc:
                print(f"  index fetch failed for {pope}: {exc}", file=sys.stderr)
        cache[pope] = merged
        print(f"Indexed {pope}: {len(merged)} links", file=sys.stderr)

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2))
    return cache


def match_vatican_title(title: str, links: list[tuple[str, str]]) -> str | None:
    norm_title = normalize_title(title)
    words = title_words(title)
    if not words:
        return None

    best_url: str | None = None
    best_score = 0
    for text, url in links:
        norm_text = normalize_title(text)
        score = len(words & title_words(text))
        if norm_title in norm_text:
            score += 10
        elif norm_text.startswith(norm_title.split()[0]):
            score += 3
        if score > best_score:
            best_score = score
            best_url = url

    if best_score >= 2 or (len(words) == 1 and best_score >= 1):
        return best_url
    return None


def resolve_url(row: Row, vatican_cache: dict[str, list[tuple[str, str]]]) -> Resolved | None:
    if row.link in SKIP_LINKS:
        return None

    if row.link in MANUAL_BY_LINK:
        return Resolved(MANUAL_BY_LINK[row.link], "manual")

    links = vatican_cache.get(row.pope, [])
    matched = match_vatican_title(row.title, links)
    if matched:
        return Resolved(matched, "vatican_match")

    return None


def extract_simple_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.body or soup
    strip_noise(root)
    return str(root)


def fetch_dco_doc(client: httpx.Client, landing_url: str) -> str:
    response = client.get(landing_url, follow_redirects=True)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    doc_href = None
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if href.endswith(".doc") and "../01p/" in href:
            doc_href = urljoin(str(response.url), href)
            break
    if not doc_href:
        raise ValueError("Could not locate DCO .doc download link")

    doc_response = client.get(doc_href, follow_redirects=True)
    doc_response.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as tmp:
        tmp.write(doc_response.content)
        tmp_path = Path(tmp.name)

    try:
        result = subprocess.run(
            ["textutil", "-convert", "txt", str(tmp_path), "-stdout"],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    body = result.stdout.strip()
    body = re.sub(r'^.*?Sollicita ac provida\s*', 'SOLLICITA AC PROVIDA ', body, flags=re.I)
    body = re.sub(r'HYPERLINK "[^"]+"\s*', '', body)
    if len(body) < 200:
        raise ValueError("DCO .doc conversion produced too little text")
    return body


def fetch_recovered_markdown(client: httpx.Client, row: Row, resolved: Resolved) -> str:
    url = resolved.url
    if url.startswith("dco:"):
        body_md = fetch_dco_doc(client, url.removeprefix("dco:"))
        return build_markdown(row, body_md, alternate_source=url.removeprefix("dco:"))

    if "vatican.va" in url:
        return fetch_document_from_url(client, row, url, alternate_source=url)

    response = client.get(url, follow_redirects=True)
    response.raise_for_status()
    html_fragment = extract_simple_html(response.text)
    body_md = html_to_markdown(html_fragment)
    if len(body_md) < 100:
        raise ValueError("Alternate source page has insufficient text")
    return build_markdown(row, body_md, alternate_source=url)


def row_by_link(rows: list[Row]) -> dict[str, Row]:
    return {row.link: row for row in rows}


def main() -> None:
    if not LOG_PATH.exists():
        print(f"No fetch log at {LOG_PATH}", file=sys.stderr)
        sys.exit(1)

    log = json.loads(LOG_PATH.read_text())
    failed_items = log.get("failed", [])
    if not failed_items:
        print("No failed items to recover.", file=sys.stderr)
        return

    rows = load_rows()
    links = row_by_link(rows)
    recovery_log: dict[str, list] = {
        "recovered": [],
        "skipped": [],
        "still_failed": [],
    }
    last_request = 0.0

    with httpx.Client(headers=HEADERS, timeout=60.0) as client:
        vatican_cache = load_vatican_cache(client)

        for item in failed_items:
            filename = item["file"]
            link = item["link"]
            out_path = OUTPUT_DIR / filename
            row = links.get(link)
            if row is None:
                recovery_log["still_failed"].append(
                    {"file": filename, "link": link, "error": "Row not found in CSV"}
                )
                continue

            if link in SKIP_LINKS:
                recovery_log["skipped"].append(
                    {"file": filename, "link": link, "reason": "index_or_stub_entry"}
                )
                print(f"skip {filename}", file=sys.stderr)
                continue

            if out_path.exists() and out_path.stat().st_size > 200:
                recovery_log["recovered"].append(
                    {"file": filename, "link": link, "method": "existing_file"}
                )
                continue

            resolved = resolve_url(row, vatican_cache)
            if resolved is None:
                recovery_log["still_failed"].append(
                    {"file": filename, "link": link, "error": "No alternate source found"}
                )
                print(f"unresolved {filename}", file=sys.stderr)
                continue

            elapsed = time.monotonic() - last_request
            if elapsed < REQUEST_DELAY:
                time.sleep(REQUEST_DELAY - elapsed)

            try:
                print(
                    f"recover {row.title[:40]} via {resolved.method}...",
                    file=sys.stderr,
                )
                markdown = fetch_recovered_markdown(client, row, resolved)
                out_path.write_text(markdown, encoding="utf-8")
                recovery_log["recovered"].append(
                    {
                        "file": filename,
                        "link": link,
                        "alternate_source": resolved.url,
                        "method": resolved.method,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                recovery_log["still_failed"].append(
                    {"file": filename, "link": link, "error": str(exc)}
                )
                print(f"  FAILED: {exc}", file=sys.stderr)
            finally:
                last_request = time.monotonic()

    recovered_files = {entry["file"] for entry in recovery_log["recovered"]}
    skipped_files = {entry["file"] for entry in recovery_log["skipped"]}

    existing_success = log.get("success", [])
    existing_success_files = {entry["file"] for entry in existing_success}

    log["failed"] = [
        item
        for item in failed_items
        if item["file"] not in recovered_files and item["file"] not in skipped_files
    ]
    for entry in recovery_log["recovered"]:
        if entry["file"] not in existing_success_files:
            log.setdefault("success", []).append(
                {"file": entry["file"], "link": entry["link"], "recovered": True}
            )

    LOG_PATH.write_text(json.dumps(log, indent=2))
    RECOVERY_LOG_PATH.write_text(json.dumps(recovery_log, indent=2))

    print(
        f"\nRecovery: {len(recovery_log['recovered'])} saved, "
        f"{len(recovery_log['skipped'])} skipped, "
        f"{len(recovery_log['still_failed'])} still failed",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
