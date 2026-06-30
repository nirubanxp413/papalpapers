# Papal Papers

A research pipeline that discovers, ingests, classifies, and enriches a corpus of papal documents — built as a reusable **search → scrape → classify → index** template.

## Documentation

| Document | Purpose |
|----------|---------|
| [`HOW_IT_WAS_BUILT.md`](HOW_IT_WAS_BUILT.md) | How the project was built — architecture, subagent orchestration, starter template |
| [`RESEARCH_APPROACH.md`](RESEARCH_APPROACH.md) | Operational playbook with step-by-step commands |
| [`brief.md`](brief.md) | Original project vision and downstream art goals |

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python scripts/crawl_index.py          # Step 0: build index CSV
python scripts/fetch_markdown.py       # Steps 1–2: download corpus
python scripts/recover_failed.py       # recover broken links
python scripts/step3_checklist.py init # Step 3: prepare subagent checklist
```

## Pipeline at a glance

```
crawl index → fetch markdown → classify (subagents) → merge CSV → deep research (filtered subset)
```

520 indexed documents · 516 markdown files · 516 classification reports · gradient scoring in progress
