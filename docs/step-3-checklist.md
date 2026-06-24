# Step 3 Checklist — Theme extraction run

**Pass 1:** text-only theme extraction  
**Model:** Sonnet 4.6 medium (`claude-4.6-sonnet-medium-thinking`)  
**Batch size:** 30 subagents  
**Total documents:** 516  
**Batches:** 18  

Live progress: run ` .venv/bin/python scripts/step3_checklist.py status `

Checklist file: `data/subagents/checklist.json`  
Audit log: `data/subagents/run_log.jsonl`

---

## Setup (once)

- [x] Prompt updated: `data/subagents/subagentsprompt.md`
- [x] Plan documented: `docs/step-3-plan.md`
- [x] Checklist script: `scripts/step3_checklist.py`
- [x] Initialize checklist: `python scripts/step3_checklist.py init`
- [x] Create reports dir populated on first run
- [x] Confirm 516 source files in `data/encyclical/`

## Pre-flight (each session)

- [ ] `step3_checklist.py sync`
- [ ] `step3_checklist.py reset-stale` (if recovering from crash)
- [ ] `step3_checklist.py status`

## Batch runs (18 × 30)

Mark each batch when `status` shows no `in_progress` and sync confirms new completes.

| Batch | batch_id (from next-batch) | Spawned | Complete | Notes |
|-------|------------------------------|---------|----------|-------|
| 1 | batch-20260624T104058+0000 | [x] | [x] | 30/30 complete |
| 2–6 | batch-20260624T105154+0000 | [x] | [x] | ids 31–180 complete |
| 7–11 | (wave 3) | [x] | [x] | ids 181–330 complete |
| 12–16 | (wave 4) | [x] | [x] | ids 331–480 (459 retry in wave 5) |
| 17–18 | (wave 5 final) | [x] | [x] | 2 orchestrators; id 509 retried separately; apostrophe fixes on 459, 504 |

## Validation

- [x] `status` → 516 complete, 0 pending, 0 in_progress
- [x] 516 files in `data/subagents/reports/` (prefix `run-`; 1 duplicate straight-apostrophe copy may remain)
- [ ] Spot-check 5 reports: no external historical claims
- [ ] Spot-check 5 reports: Structured fields populated or `Not stated in text`

## Handoff

- [ ] Step 4: merge themes into `data/encyclicals.csv`

## Commands quick reference

```bash
.venv/bin/python scripts/step3_checklist.py init
.venv/bin/python scripts/step3_checklist.py next-batch
.venv/bin/python scripts/step3_checklist.py sync
.venv/bin/python scripts/step3_checklist.py status
.venv/bin/python scripts/step3_checklist.py reset-stale
```
