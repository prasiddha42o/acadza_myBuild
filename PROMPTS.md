# PROMPTS.md

Running log. Sections marked **TODO(Prasiddha)** need your own words / verbatim paste.

## A. Prompts inside the product (verbatim + versions)
_Added in Stage 2 when the LLM steps exist. Each entry: version, full text, what changed from the previous version, why._

## B. Prompts I gave my AI coding assistant (verbatim)

**TODO(Prasiddha): paste your prompts below exactly as you typed them. Do not tidy them.**

### B1. Kickoff (Tue 10 Nov 2026, 13:38 NPT)
> TODO: paste the full kickoff message here (the one starting "I'm Prasiddha, a final-year BCS (Hons) AI student...").
> Key directions in it: read README + inspect data first; propose architecture and wait for OK; build in small stages; challenge weak decisions; flag anything that could silently apply a wrong fix; no scope creep.

### B2. Answers to the architecture proposal (Tue 10 Nov 2026, 21:58 NPT)
> TODO: paste verbatim.
> Substance: Claude Pro question on the API key; "choose the better option" on auto-applying key changes; OK on routing the missing option D to faculty; start date immediate, 6 months, open to extending.

## C. Three moments the AI was wrong, and how I caught it
_Only real ones are logged here._

1. **Stage 1, option parser.** The option parser stopped at the first missing letter, so on the question with option D missing the lint reported "missing A,B,C,D" instead of "missing D". The first test only asserted that a missing-option finding existed, so it passed for the wrong reason. Caught by printing lint output for all 43 questions and reading it against my hand analysis (not by the green test run). Fix: skip absent letters instead of stopping; the test now asserts the detail is exactly "missing D".
2. _(open: to be filled by a real event in Stage 2/3)_
3. _(open)_

## D. Design decisions log (tried / dropped / why)

| # | Decision | Tried or considered, then dropped | Why |
|---|---|---|---|
| 1 | Audit **per question**, fan out to per-report rows | One LLM call per report | Truth is a property of the question. Three reports on the LCR question are one fix. Also surfaces defects nobody reported (the brief's bonus). 43 audits instead of 44 independent guesses. |
| 2 | Comment is a **lead, never an instruction**; the student's suggested value is never copied | Pre-filling the edit from the comment | R004: the student says 8225 (option B). The true answer is 4800 (D). Copying the student would have swapped one wrong key for another. |
| 3 | **Routing and edit validity are decided in code** (`engine/guards.py`), not by the model | Letting the model output `route` directly | The model can be confidently wrong. Guards fail closed: scope, unique `find`, numbers unchanged for wording repairs, size cap, additive-only for answer form, option repair only after the first option marker. |
| 4 | `answer_key` auto-applies only when two blind solves **and** the stored solution agree, confidence at least 0.9 | Auto-applying on one solve; never auto-applying keys | The taxonomy permits auto, and never-auto would throw away easy wins (R001/R004/R005). Agreement of three independent signals is the compromise. Biology keys always go to faculty because there is nothing to compute. |
| 5 | Missing option D (R009/R010) goes to **faculty** with a suggested value | Auto-writing a distractor | The solution does not contain it. Inventing a distractor is authored content, the same reason `identical_options` is human-only. |
| 6 | Render **native MathML**, sanitise the HTML | KaTeX | The data is MathML, not LaTeX. KaTeX would add a conversion step and a failure mode. DOMPurify (MathML profile) stops production HTML from running script in the console. |
| 7 | Evaluate against a **hand-labelled gold set**, say so | Claiming precision/recall as a benchmark | No ground truth was shipped. I labelled the 44 reports myself (`eval/gold.json`), so the numbers are a sanity check and I am both builder and labeller. Ambiguous cases carry `alt_verdicts`. |
| 8 | Free host: Render + SQLite seeded from `results.json`; console loads precomputed results | Hosting the LLM engine live | README allows precomputed results. Cheaper, no key on the server, no cold-path failures at 10 PM. Audit log resets on redeploy (documented). |

## E. Open items
- Stage 2: LLM audit + classifier prompts, `results.json`.
- Stage 3: console. Stage 4: deploy, `PRODUCT.md`, video script, email.
