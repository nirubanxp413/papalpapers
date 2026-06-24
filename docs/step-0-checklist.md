# Step 0 Checklist

## Setup

- [x] Create project layout (`scripts/`, `data/`, `docs/`)
- [x] Add `requirements.txt` (httpx, beautifulsoup4)
- [x] Add `.gitignore` for cache and venv
- [x] Save plan to `docs/step-0-plan.md`

## Crawler

- [x] Fetch pope registry from `/popelist` via WP API
- [x] Save registry to `data/popes.json`
- [x] Crawl category-based popes via posts API (paginated)
- [x] Crawl page-based popes via pages API (HTML list parsing)
- [x] Normalize rows to CSV schema
- [x] Dedupe by normalized URL
- [x] Write `data/encyclicals.csv`
- [x] Write `data/summary.json` with per-pope counts

## Validation

- [x] Leo XIII row count = 88
- [x] Pius XII row count = 60
- [x] Francis encyclicals = 4
- [x] Leo XIV encyclicals = 1
- [x] 45 popes indexed, 520 total documents
- [ ] Spot-check 3 random rows against live site links (manual)

## Handoff to Step 1

- [x] CSV ready for download pipeline
- [x] `doc_type` column usable for filtering secular-relevant encyclicals

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/crawl_index.py
```

Output: `data/encyclicals.csv`, `data/popes.json`, `data/summary.json`
