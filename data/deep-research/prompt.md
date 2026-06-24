# Deep research prompt

The batch script prepends document metadata (including the encyclical URL), the Step 3 theme analysis, then appends the sections below.

---

## Project context

You are contributing to **Papal Papers**, a research and art pipeline that studies papal encyclicals — especially those that engage the **secular world** (politics, economy, labour, society, international order) rather than purely intra-ecclesial matters.

**Prior pass (Step 3):** Each document has already been analysed for key themes using *only the text of the encyclical*. You receive that analysis as grounding — treat it as a hypothesis to verify, extend, and situate historically. Do not treat it as authoritative fact about the outside world.

**This pass:** Your job is external deep research. Place the encyclical in its historical moment and measure how **reflective** (backward-looking: naming, interpreting, or responding to what already exists) versus **prospective** (forward-looking: warning, prescribing, anticipating, or shaping what may come) it is — in relation to both the Church and society at large.

**Downstream use:** Findings feed an artwork where each encyclical becomes a visual symbol. Two dimensions drive the colour system:

- **Saturation** — how strongly the document acts in reflective or prescriptive mode (1–10).
- **Density** — how far backward or forward in time its gaze reaches (−30 to +30 years from publication; 0 = immediate present at publication).

Write with the rigour of an **investigative journalist** and the contextual sensitivity of an **anthropologist**: evidence-led, specific, sceptical of hagiography, willing to note irrelevance or failed prophecy as well as prescience.

---

## Research instructions

### Primary task

Using the Step 3 theme summary, the encyclical metadata, and the **primary source URL** provided, conduct deep research on this encyclical's relationship to its world.

Determine:

1. **Reflective character** — What existing conditions, events, debates, or tensions (in the Church and/or wider society) does the document look *back* upon, name, interpret, or respond to?
2. **Prospective character** — What warnings, prescriptions, anticipations, or forward-directed themes does the document offer (for the Church and/or wider society)? Distinguish what it *claims* will or should happen from what *actually* happened afterward where evidence allows.

Both dimensions may coexist in the same document. Do not force a binary; score and describe each axis independently where the text supports it.

### Research approach

- Read or consult the encyclical via its URL where possible; cross-check the Step 3 summary against the primary text.
- Prioritise contemporaneous and near-contemporaneous sources (news, parliamentary records, labour reports, diplomatic correspondence, secular press) alongside academic history.
- For **academic and media citation**, search *outside* theological studies: history, political science, economics, sociology, law, journalism, policy archives.
- Note when the encyclical was ignored, contested, or superseded — not only when it was influential.
- Assess whether the **pope's personal authority, reputation, or political situation** at publication shaped the document's tone, reach, or reception.

### Evidence discipline

- **Do not extrapolate.** If media coverage, academic citation, or historical documentation is thin or absent, say so plainly (`insufficient evidence`, `no reliable sources located`) and leave fields sparse. Do not fill gaps with plausible narrative.
- **Distinguish source types** throughout the report — never conflate them:
  - **Historical records / events** — primary documents, parliamentary proceedings, treaties, statistical data, contemporaneous official correspondence, verified dates and acts.
  - **Media** — newspapers, magazines, broadcast, modern digital journalism; report what was *said* in public discourse, not necessarily what *was*.
  - **Academic scholarship** — peer-reviewed or established historiography interpreting past events (label as interpretation, not primary fact).
- When a claim rests only on media, tag it explicitly (e.g. *"reported in the press [3], not confirmed in parliamentary record"*).
- Absence of evidence is a valid finding: *"No non-theological media citation located"* is preferable to inferred influence.

### Definitions (use consistently)

| Term | Meaning |
|------|---------|
| **Reflective** | Backward-looking: describes, interprets, mourns, celebrates, or reacts to what is already occurring or has occurred. |
| **Prospective / Prescriptive** | Forward-looking: warns, commands, predicts, recommends, or attempts to shape future conduct or events. |
| **Saturation** | Intensity of the reflective or prescriptive stance (1 = faint trace, 10 = dominant organising purpose of the document on that axis). |
| **Density** | Temporal distance of the gaze from the publication year (years before/after; 0 = immediate present; negative = past; positive = future). |

---

## Output format

Return the report in **markdown** using this exact structure.

**Critical:** The `## Gradients` section with the JSON block must appear **immediately after the title** — before `## Structured`. This JSON is the canonical machine-readable output for downstream Python extraction.

Do not omit sections. If evidence is insufficient, state that explicitly rather than inventing.

````markdown
# {Title}

## Gradients

```json
{
  "reflective": {
    "saturation": 7,
    "density": -12
  },
  "prescriptive": {
    "saturation": 4,
    "density": 5
  }
}
```

- `saturation`: integer **1–10** per axis.
- `density`: integer **−30 to +30** (years from publication; 0 = immediate present; negative = pastward; positive = futureward).
- Score reflective and prescriptive **independently**; both may be non-zero.
- Replace example values with your assessment. Keep JSON valid — integers only, no comments inside the block.

*Example meaning (Leo XIII, Longinqua, 1895):* reflective 7 / −10 looks back on a century of U.S. Catholic growth; prescriptive 5 / +3 directs bishops on press, labour, and civic conduct in the near term.

---

## Structured

### Context
What was the context in which this encyclical was written? Cover ecclesiastical and secular circumstances at the time of publication (political, economic, social, intellectual). Be specific with dates, places, and actors where possible.

### Reflective
What were the key themes the encyclical **reflected on** — looking backward or inward at what already existed?
- Separate **Church** and **Society** where useful (use sub-bullets or short paragraphs).
- Tie each theme to evidence from the text and/or contemporaneous records.

### Prospective
What were the key **forewarnings, prescriptions, or forward-looking themes** the encyclical actually provided?
- Separate **Church** and **Society** where useful.
- Distinguish explicit predictions/commands from implicit anticipations.

### Temporal gradients

Expand the **Gradients** JSON above with brief justifications (a few sentences each — enough to scan quickly, not long prose). **Integers here must match the JSON block exactly.**

#### Reflective gradient
- **Saturation (1–10):** [integer] — [2–4 sentences: why this score; cite the strongest reflective themes or passages]
- **Density (−30 to +30 years from publication):** [integer] — [2–4 sentences: what past (or present) horizon the backward gaze reaches and why]

#### Prescriptive / prospective gradient
- **Saturation (1–10):** [integer] — [2–4 sentences: why this score; cite commands, warnings, or forward-directed themes]
- **Density (−30 to +30 years from publication):** [integer] — [2–4 sentences: how far ahead (or into what past correction) the prescriptive gaze extends and why]

---

## Unstructured

Write as an **investigative field report**. This section complements and explains the Structured section above. Use clear prose, not bullet dumps except where noted.

### 1. Circumstances that led to the encyclical
What chain of events, crises, requests, or institutional pressures produced this document?

### 2. Relevance — then and now
Why did it matter at publication? Where was it irrelevant, ignored, or wrong? How is it regarded today outside Catholic specialist circles?

### 3. Citation in academia and secular media
How has it been cited or discussed in **non-theological** scholarship and media (history, politics, economics, labour, international relations, culture)? Name specific works and authors where possible.

### 4. Policy and material consequences
Did it influence or reflect major **political, economic, or social policy** decisions (legislation, party programmes, union action, colonial administration, welfare reform, etc.)? Distinguish correlation from demonstrated influence.

### 5. The pope's position and authority
Did the pope's personal influence, reputation, or constrained circumstances at the time affect the encyclical's content, reception, or enforceability — or did the document attempt to change that position?

### Citations

List **all sources consulted** in this structured bibliography format:

| # | Type | Author / Outlet | Title | Year | URL or publication detail |
|---|------|-----------------|-------|------|---------------------------|

**Type** must be one of: `historical_record`, `media`, `academic`, `primary_text`, `archival`, `other`. Do not label media as historical fact or vice versa.

Include primary sources, academic works, news articles, and archival references. Number citations `[1]`, `[2]`, etc. in the Unstructured prose where appropriate.
````

---

## Constraints

- **Primary source:** Always start from the encyclical URL supplied in the Document section. If the URL is unavailable, note this and use the best available alternative (Vatican archive, papalencyclicals.net mirror, etc.).
- **Scope:** Focus on secular engagement and social/political/economic context where the Step 3 analysis indicates Mixed or Social themes; still note purely ecclesial context when it materially shaped reception.
- **Evidence standard:** Prefer cited facts over inference. Mark uncertainty explicitly (`likely`, `unclear`, `disputed`). **Never extrapolate** beyond what sources support — sparse output is correct when evidence is sparse.
- **Source separation:** Keep **media** (reportage, commentary, public discourse) clearly distinct from **historical records and verified events** (acts, data, official documents). Do not treat press coverage as proof of policy impact without corroboration.
- **Neutrality:** No devotional language. No assumption that papal authority implies worldly efficacy.
- **Recency:** Include post-publication reception up to the present where sources exist, but weight analysis toward the document's own era for Context and gradient scoring.
- **Length:** Be thorough but disciplined. The Structured section should be scannable; the Unstructured section may be longer and narrative.
- **Gradients placement:** Never move gradients below Structured. The JSON block under `## Gradients` is mandatory and must be the first section after the title.
