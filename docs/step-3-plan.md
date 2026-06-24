# Step 3: Theme extraction (Pass 1)

Extract key themes from each encyclical markdown file using subagents. Themes must come **only from the document text** — no outside historical context. This pass feeds later steps (CSV `themes` column, secular scoring, temporal gradients).

See also: `brief.md` Step 3–4.

## Goal

For each of **516** files in `data/encyclical/`:

1. Subagent reads one encyclical.
2. Subagent writes a theme report to `data/subagents/reports/run-{filename}.md`.
3. Checklist item marked `complete` (or `failed` with error trace).

**Model:** `claude-4.6-sonnet-medium-thinking` (Sonnet 4.6 medium)  
**Concurrency:** 30 subagents per batch  
**Estimated batches:** 18 (516 ÷ 30, rounded up)

## Folder layout

```
data/subagents/
├── subagentsprompt.md      # Subagent instructions (with {INPUT_PATH} / {OUTPUT_PATH})
├── checklist.json          # Master state — one row per encyclical
├── run_log.jsonl           # Append-only audit log (init, batches, marks, sync)
└── reports/
    └── run-{encyclical}.md # One report per source file

docs/
├── step-3-plan.md          # This file
└── step-3-checklist.md     # Human run checklist

scripts/
└── step3_checklist.py      # Init, status, batch, sync, mark
```

## Checklist schema (`checklist.json`)

Each item:

| Field | Description |
|-------|-------------|
| `id` | Stable integer 1…516 |
| `source_file` | e.g. `data/encyclical/06011895_Leo XIII_Longinqua.md` |
| `report_file` | e.g. `data/subagents/reports/run-06011895_Leo XIII_Longinqua.md` |
| `status` | `pending` \| `in_progress` \| `complete` \| `failed` \| `skipped` |
| `batch_id` | Set when claimed by `next-batch` |
| `started_at` / `completed_at` | ISO timestamps |
| `error` | Set on failure |
| `agent_id` | Optional Cursor subagent id for traceability |

## Resumability

Runs are safe to interrupt and resume:

1. **`sync`** — If a report file exists (≥500 bytes), mark `complete` even if checklist was stale.
2. **`reset-stale`** — After a crash, reset `in_progress` items with no report back to `pending`.
3. **`next-batch`** — Only claims `pending` items; never re-queues `complete`.
4. **`run_log.jsonl`** — Every init, batch, mark, and sync is appended for audit.

### Recovery workflow

```bash
# After an interrupted run:
.venv/bin/python scripts/step3_checklist.py sync
.venv/bin/python scripts/step3_checklist.py reset-stale
.venv/bin/python scripts/step3_checklist.py status
# Then continue with next-batch + spawn agents
```

## CLI reference

```bash
# First-time setup (builds checklist from data/encyclical/)
.venv/bin/python scripts/step3_checklist.py init

# Progress summary
.venv/bin/python scripts/step3_checklist.py status

# Claim next 30 pending items (prints JSON for orchestrator)
.venv/bin/python scripts/step3_checklist.py next-batch

# Reconcile disk vs checklist
.venv/bin/python scripts/step3_checklist.py sync

# After crash: unstick in_progress rows
.venv/bin/python scripts/step3_checklist.py reset-stale

# Manual mark (if subagent failed but you fixed by hand)
.venv/bin/python scripts/step3_checklist.py mark 42 complete
.venv/bin/python scripts/step3_checklist.py mark 42 failed --error "timeout"
```

## Main agent orchestration loop

The **parent agent** (not the subagent prompt file) drives batches:

```
loop until status shows 0 pending and 0 in_progress:
  1. sync
  2. next-batch  → JSON with up to 30 items
  3. For each item in parallel (max 30 Task calls, single message):
       - subagent_type: generalPurpose
       - model: claude-4.6-sonnet-medium-thinking
       - readonly: false
       - prompt: [see template below]
  4. After all 30 finish:
       - sync (pick up written reports)
       - mark any in_progress still missing reports as failed
  5. status → log progress
```

### Subagent Task prompt template

Substitute paths from `next-batch` JSON:

```
Read the shared instructions in:
  /Users/niruban/superflat-projects/prj-papalpapers/data/subagents/subagentsprompt.md

Apply them to this document:
  INPUT_PATH:  {source_file absolute path}
  OUTPUT_PATH: {report_file absolute path}

Steps:
1. Read INPUT_PATH.
2. Follow subagentsprompt.md exactly (text-only themes, no outside context).
3. Write the report to OUTPUT_PATH.
4. Reply with ONLY: "OK {id}" or "FAILED {id}: {reason}"

Checklist id: {id}
Batch id: {batch_id}
```

### After each batch

```bash
.venv/bin/python scripts/step3_checklist.py sync
.venv/bin/python scripts/step3_checklist.py status
```

Update `docs/step-3-checklist.md` batch checkboxes manually or via parent agent.

## Out of scope for Pass 1

- Comparing texts to external historical facts (Step 6–7).
- Scoring reflective vs prescriptive accuracy.
- Writing themes back to `data/encyclicals.csv` (Step 4 — separate script after all reports exist).

## Success criteria

- [ ] 516 / 516 checklist items `complete` or intentionally `skipped`
- [ ] Each report has Structured + Unstructured sections and YAML frontmatter
- [ ] `run_log.jsonl` shows full batch history
- [ ] Spot-check 5 reports for “no outside context” compliance

## Handoff to Step 4

Parse `data/subagents/reports/*.md` structured fields → add `themes` column to `data/encyclicals.csv` (future script).
