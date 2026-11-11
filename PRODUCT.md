# PRODUCT.md

## Who loses most when a question is broken

**The student who got it right and was marked wrong, and never reported it.** Most students who meet a broken question don't report it. They assume they are wrong, lose marks, and some "learn" the wrong method to match the key. The reporter is one of many people affected, and often not the one most hurt. Every hour the question stays broken, it hurts the next student who meets it.

How the design reflects that:
- **We fix the question, not the ticket.** Each question gets one audit and one fix, however many reports came in. The audit checks the whole question, so it also catches defects nobody reported.
- **The queue is ordered by harm.** Priority = distinct reporters × severity × confidence. A wrong key or stem (severity 5) costs every student marks. A wrong solution step (3) teaches a wrong method. A typo (1) costs seconds.
- **Safe, checkable fixes go live in minutes, not days.** For example, a key that two blind solves and the stored solution all contradict is fixed automatically. Faculty time goes to the cases that need judgement.
- **Replies tell the truth.** The reporter gets a short, kind reply in their own register (Hinglish if they wrote Hinglish). If they were wrong, the reply gives them the one idea they missed. It never says "fixed" unless the fix is actually live.

## What the AI must never auto-apply

| Never auto-apply | Why |
|---|---|
| A key change that independent checks don't support | A wrong auto-fix turns a visible bug into an invisible one, and marks the students who were right as wrong. |
| A value a student suggested | Students are often wrong. The R004 student would have swapped one wrong key for another. |
| Authored content: a new distractor, a missing option the solution doesn't state, a figure | This is writing a question, not repairing one. |
| Biology / fact-recall key changes | There is nothing to compute, so "agreement" is just two models sharing the same wrong belief. |
| Unsound questions (no correct option, or a self-contradicting solution) | Flipping the key would hide the real problem. The question needs rewriting (R034). |
| Chapter or syllabus moves | These are curriculum decisions, not data fixes. |
| Anything the audit found that no student reported | Nobody corroborated it. A human looks first. |

Every one of these is enforced in code (`engine/guards.py`, `console/store.py`), not just asked for in a prompt.

## The one metric for week 1

**Undo rate on auto-applied fixes:** the share of auto-applied fixes that faculty undo within 7 days. This is the cost of trusting the machine. Target: under 2%. If it goes higher, raise the confidence bar or move that repair type to faculty-only. If it stays near zero while the faculty queue stays long, the bar can safely come down. Read it next to median time from first report to fix, so we don't buy safety by slowing everything down.

## One idea beyond the brief

**Find broken questions before anyone reports them, using attempt data Acadza already has.** A wrong key leaves a statistical signature. Strong students (high overall accuracy) "fail" the question at an unusual rate, and they all pick the same other option. Questions with that signature can be found with a cheap SQL query over lakhs of questions, without calling any LLM. Only the flagged ones go through this audit. When a key fix lands, re-grade the past attempts and send affected students one line: "Q.23 had a wrong key; your answer was right, +4 restored." That reaches the silent majority who never reported, and it is the cheapest way to earn their trust back.
