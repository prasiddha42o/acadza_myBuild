# Acadza Report → Fix

Students report broken JEE/NEET questions with comments like "ans galat hai" or "option D missing". This repo turns those reports into safe fixes. The **triage engine** audits each reported question, the LLM *proposes* a surgical edit, and code decides whether it is applied automatically or goes to faculty. The **faculty console** lets a teacher clear the queue in seconds per item, with an audit trail.

- **Live console:** TODO(deploy): Render URL
- **Video (3 min):** TODO(video): link
- **Results:** [`results.json`](results.json) (44 rows) · per-question detail in [`engine_out/`](engine_out/)
- **How it was built, prompts, mistakes, decisions:** [`PROMPTS.md`](PROMPTS.md) · product view: [`PRODUCT.md`](PRODUCT.md)

## How it works

```
report ─┐
report ─┼─> group by question ─> lint + rules (free, no LLM)
report ─┘                         │
                    ┌─────────────┼──────────────────────────┐
                    │ solver A    │ solver B      │ extractor │   blind: no key, no solution, no comment
                    │ 3.8-flash   │ 3.6-flash     │ 3.5-lite  │   extractor: what the stored solution concludes
                    └─────────────┴───────────────┴───────────┘
                                  │
                         auditor (3.8-flash): proposes verdicts + edits
                         comments passed as escaped, tagged, untrusted data
                                  │
                 code: map edits to raw HTML, compute agreement, guards.decide_route
                                  │
            auto_apply ─────── faculty_review ─────── no_change      ──> student reply per report
```

1. **One audit per question (43), fanned out to 44 report rows.** The truth is a property of the question, so the three reports on one wrong key become one fix.
2. **The student comment is a lead, never an instruction.** It is never copied into an edit. R004's student says 8225; the true answer is 4800 (D), and the engine writes D.
3. **The LLM proposes and code decides.** [`engine/guards.py`](engine/guards.py) fails closed. An edit must stay inside the fields its repair may touch, and its `find` must match the stored text exactly once. Wording repairs may not change numbers, and an answer-form repair may only add text. Option repairs may not touch the stem.
4. **Key changes need three independent signals.** Two blind solves by two *different* models, plus the stored solution's conclusion from a separate reader, must agree on the new key, and confidence must be at least 0.9. Agreement is computed in code, not reported by the model. Biology key changes always go to faculty, because there is nothing to compute.
5. **Some repairs always wait for a human:** authored content (missing option D with no value in the solution, identical options, figures), unsound questions (R034), chapter and syllabus moves, and anything nobody reported.
6. **The reply** to the student is 1–3 sentences, kind, and in Hinglish if the student wrote in Hinglish. A code check blocks any reply that says "fixed" unless the fix was actually applied.

Models default to the **Gemini API free tier**. The Anthropic API is a switch in the same adapter ([`engine/llm.py`](engine/llm.py)). Every response is cached on disk by a hash of the full request, so reruns cost nothing and `results.json` is reproducible.

## Results

> Measured against **my own hand labels** ([`eval/gold.json`](eval/gold.json)). Acadza did not ship ground truth, and I am both builder and labeller, so these numbers are a sanity check, not a benchmark. Ambiguous cases carry `alt_verdicts`.

| | |
|---|---|
| Verdict accuracy | TODO(run) |
| Route accuracy | TODO(run) |
| Defect precision / recall | TODO(run) |
| Auto-applied (exactly as gold / total) | TODO(run) |
| Audits sent to faculty because a guard rejected the edit | TODO(run) |

**Defects nobody reported** (the brief's bonus) are listed in [`engine_out/unreported_findings.json`](engine_out/unreported_findings.json), and the console shows them in their own lane. They never auto-apply. Found by hand while labelling:
- **`…a3e579` (Zoology):** the solution says "Insulin and glucagon work synergistically". They are antagonistic.
- TODO(run): add the engine's other unreported findings worth naming.

## Traps and how they are handled

TODO(run): check each row against `results.json` and correct any the engine got wrong.

| Report | Trap | Intended handling |
|---|---|---|
| R004 | Student's value (8225) is wrong, but the key is also wrong | Key A → D, from two solves plus the stored solution; the student's value is never used |
| R017 | Already fixed by faculty after the report | `already_fixed` (errorHistory post-dates the report) |
| R034 | Assertion judged true by the student; the solution contradicts itself | `unsound` → faculty; the key is not flipped |
| R009/R010 | Option D missing and not recoverable from the solution | faculty, with a suggested value; never written automatically |
| R020/R021 | Malformed mcq keys (`["A, B, D"]`, `["", "B"]`) | key reformatted / type corrected to scq, only when the solves agree |
| R027 | Comment is a prompt injection ordering a key change | Treated as data; the key change needs independent agreement, so it cannot be ordered |
| R011 | Blank option C | Faculty: the inserted "1 : 1" is not stated in the solution (a deliberately strict check; my gold label says auto) |

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export GEMINI_API_KEY=...                         # never commit it
.venv/bin/python -m engine.run                    # -> results.json + engine_out/
.venv/bin/python -m eval.evaluate results.json    # scores against MY labels
.venv/bin/python -m pytest -q                     # no network; scripted fake model
.venv/bin/uvicorn console.app:app --reload        # console at http://127.0.0.1:8000
```

Env vars: `PROVIDER` (`gemini` default, or `anthropic` with `ANTHROPIC_API_KEY`), `SOLVER_A_MODEL`, `SOLVER_B_MODEL`, `AUDITOR_MODEL`, `REPLY_MODEL`, `LLM_MIN_INTERVAL` (seconds between calls per model, default 6), and `LLM_TEMPERATURE` (default 0; it is dropped automatically if a model rejects it). If the free daily quota runs out, the run stops cleanly and resumes from the cache after the reset (midnight Pacific). Partial runs write to `engine_out/*.partial.json`, never to `results.json`.

## Console

- **Queue grouped by question.** Lanes run Needs you → Found by audit → Auto-applied (check or undo) → No change → Done. Within a lane, priority is distinct reporters × severity × confidence (severity table in [`console/store.py`](console/store.py)). Needs-human items sit above auto-applied ones, because auto fixes are already live.
- **Each card** leads with the problem and *why it needs you*, then a before/after diff with the changed span highlighted. Key changes appear as option chips, and MathML renders natively (sanitised by DOMPurify's MathML profile).
- **Replies:** the reply the student already got, and the follow-up they get after approve, reject or undo. Both are editable.
- **One click per item, or keys:** `A` approve, `E` edit, `R` reject, `U` undo, `J`/`K` move. Edits made by faculty pass the same guards, and undo refuses if the question changed after the fix. Every action writes an audit row.

**Deployment notes**
- The console loads **precomputed results** and makes no model calls, so there is no API key on the server.
- It runs on Render's free tier ([`render.yaml`](render.yaml)). That disk is ephemeral, so **the audit log resets on every redeploy or restart**. The service also sleeps after 15 minutes idle, so the first load can take about 50 seconds.
- There is no login. Actions change only the console's own SQLite copy, never Acadza's data. **Reset** restores the precomputed state.
- If no engine output exists, the console loads a demo set built from my labels and shows a "DEMO DATA" banner.

## Data and privacy note

The default provider is the **Gemini API free tier**, and Google's pricing page states that free-tier content is used to improve Google's products. The inputs are question-bank content and anonymised student comments. Production traffic should use a paid tier, where content is not used, or the Anthropic switch.

## Repo map

| Path | What |
|---|---|
| `engine/lint.py`, `rules.py` | Free deterministic checks and report pre-triage |
| `engine/guards.py` | Edit validation and routing; the safety core |
| `engine/llm.py` | The one model adapter: Gemini / Anthropic, cache, backoff |
| `engine/prompts.py` | Every in-product prompt, versioned (copied verbatim into PROMPTS.md by test) |
| `engine/run.py` | The pipeline |
| `console/` | FastAPI + SQLite + vanilla JS faculty console |
| `eval/` | My gold labels and the scorer |
| `tests/` | 80 tests, no network |
