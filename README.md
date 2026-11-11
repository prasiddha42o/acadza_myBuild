# Acadza Report → Fix

Students report broken JEE/NEET questions; this engine audits each reported question, proposes surgical fixes, and lets code (not the model) decide what is auto-applied and what waits for faculty.

> Full README (architecture, console, deploy) lands in Stage 4. This file holds the notes that already apply.

## Run the engine

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export GEMINI_API_KEY=...            # never commit it
.venv/bin/python -m engine.run       # -> results.json + engine_out/
.venv/bin/python -m eval.evaluate results.json
.venv/bin/python -m pytest -q
```

Env vars: `PROVIDER` (`gemini` default, or `anthropic` with `ANTHROPIC_API_KEY`), `SOLVER_A_MODEL`, `SOLVER_B_MODEL`, `AUDITOR_MODEL`, `REPLY_MODEL`, `LLM_MIN_INTERVAL` (seconds between calls per model, default 6), `LLM_TEMPERATURE` (default 0).

Every model response is cached in `.cache/llm/` by a hash of the full request, so reruns are free and reproducible. If the free daily quota runs out mid-run, the engine stops cleanly; rerun after the reset (midnight Pacific) and it resumes from the cache.

## Run the console

```bash
.venv/bin/uvicorn console.app:app --reload      # http://127.0.0.1:8000
```

Loads `engine_out/report_rows.json` (or `results.json`) plus `engine_out/audits.json` and `unreported_findings.json`. If no engine output exists it loads a **demo set built from my hand labels** and shows a "DEMO DATA" banner. State and the audit log live in SQLite (`CONSOLE_DB`, default `console/console.db`). **On Render's free tier the disk is ephemeral: the audit log resets on every redeploy or restart.** The Reset button restores the precomputed state (set `CONSOLE_ALLOW_RESET=0` to disable). There is no login: actions only change the console's own copy of the data, never Acadza's database.

Keys: `J`/`K` next/previous, `A` approve, `R` reject, `E` edit, `U` undo. `#q=<questionId>` in the URL opens a question directly.

## Notes

- **Free-tier data use.** The default provider is the Gemini API free tier. Google's pricing page states that free-tier content is used to improve Google's products. The inputs here are question-bank content and anonymised student comments; for production traffic use a paid tier (content not used) or the Anthropic switch.
- **The console loads precomputed results.** The deployed faculty console reads the committed `results.json` and `engine_out/*.json`; it does not call any model at request time, so no API key lives on the server.
- **Evaluation numbers are against my own labels.** Acadza did not ship ground truth; `eval/gold.json` is hand-labelled by me, so `eval/evaluate.py` is a sanity check, not a benchmark.
