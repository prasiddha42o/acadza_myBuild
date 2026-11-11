"""Stage 2 triage engine: one audit per question, fanned out to one row per report.

  python -m engine.run                 # all 43 questions -> results.json + engine_out/*
  python -m engine.run --only e565c4   # one question (id suffix) -> engine_out/results.partial.json

Per question:  lint + rules (free)  ->  solver A, solver B (blind: no key, no solution, no comment)
  ->  extractor (what the stored solution concludes; no key)  ->  auditor (proposes)
  ->  code builds edits, computes agreement, guards.decide_route (decides)  ->  reply per report.
The LLM never sets a route. Every doubt fails closed to faculty_review."""
import argparse
import json
import re
import sys
from pathlib import Path

from . import prompts as P
from .common import clean_html, get_field, load_questions, load_reports, map_find, payload
from .guards import SOLUTION_WRITES, TAX, apply_edit, decide_route, validate_edit
from .lint import fixed_after_report, lint_question, valid_answer
from .llm import LLM, LLMError, QuotaExhausted
from .rules import triage_report

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "engine_out"
TYPE_DESC = {"scq": "scq (exactly one correct option)", "mcq": "mcq (one or more correct options)",
             "integerQuestion": "integerQuestion (numeric answer)"}
REL_TOL = 1e-3


# --- answer normalisation and agreement (pure code) ---------------------------------------
def norm_answer(qtype, a):
    """Letters -> frozenset (so scq 'B' == mcq ['B']); integer -> float; unusable -> None."""
    if a is None:
        return None
    s = ",".join(str(x) for x in a) if isinstance(a, list) else str(a)
    s = s.strip()
    if not s or s.upper() in ("UNSOLVABLE", "NONE"):
        return None
    if qtype in ("scq", "mcq"):
        letters = frozenset(re.findall(r"(?<![A-Za-z])([A-D])(?![A-Za-z])", s.upper()))
        return letters or None
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(" ", ""))
    return float(m.group()) if m else None


def same(a, b):
    if a is None or b is None:
        return False
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= max(1e-6, REL_TOL * max(abs(a), abs(b)))
    return a == b


def show(a):
    if a is None:
        return "none"
    if isinstance(a, frozenset):
        return ",".join(sorted(a))
    return ("%g" % a)


def to_stored_answer(qtype, s):
    """Model's key text -> the stored format for that type (validated later by guards)."""
    s = (s or "").strip()
    letters = sorted(set(re.findall(r"(?<![A-Za-z])([A-D])(?![A-Za-z])", s.upper())))
    if qtype == "scq":
        return letters[0] if len(letters) == 1 else s
    if qtype == "mcq":
        return letters if letters else [s]
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(" ", ""))
    return m.group() if m else s


# --- prompt inputs --------------------------------------------------------------------------
def _esc(s):
    """Comments may not open or close tags in our prompt."""
    return (s or "").replace("<", "&lt;").replace(">", "&gt;")


def solver_input(q):
    p = payload(q)
    return ("Subject: %s | Chapter: %s | Type: %s\n<question>\n%s\n</question>"
            % (q["subject"], q["chapter"], TYPE_DESC[q["questionType"]], clean_html(p["question"])))


def extractor_input(q, solution_html):
    p = payload(q)
    return ("Type: %s\n<question>\n%s\n</question>\n<solution>\n%s\n</solution>"
            % (TYPE_DESC[q["questionType"]], clean_html(p["question"]), clean_html(solution_html)))


def auditor_input(q, reports, checks, lint, hints):
    p = payload(q)
    rep = []
    for r in reports:
        fixed = fixed_after_report(q, r["reportedAt"])
        rep.append('<student_report id="%s" reported_at="%s" pre_triage_hint="%s" faculty_edit_after_report="%s">\n%s\n</student_report>'
                   % (r["reportId"], r["reportedAt"], hints[r["reportId"]], "yes" if fixed else "no", _esc(r["comment"])))
    return "\n".join([
        "<question_record>",
        "subject: %s | chapter: %s | type: %s" % (q["subject"], q["chapter"], TYPE_DESC[q["questionType"]]),
        "stored_answer_key: %s" % json.dumps(p["answer"], ensure_ascii=False),
        "<question_html>\n%s\n</question_html>" % clean_html(p["question"]),
        "<solution_html>\n%s\n</solution_html>" % clean_html(p["solution"]),
        "</question_record>",
        "<checks>",
        "solver_a: answer=%s | missing_info=%s | working=%s" % (checks["a"]["answer"], checks["a"]["missing_info"] or "-", checks["a"]["working"]),
        "solver_b: answer=%s | missing_info=%s | working=%s" % (checks["b"]["answer"], checks["b"]["missing_info"] or "-", checks["b"]["working"]),
        "stored_solution_concludes: %s (%s)" % (checks["concl"]["answer"], checks["concl"]["note"]),
        "summary: solvers agree with each other=%s; with the key=%s; with the stored solution=%s"
        % (checks["solvers_agree"], checks["solvers_match_key"], checks["solvers_match_solution"]),
        "lint_findings: %s" % (json.dumps(lint, ensure_ascii=False) if lint else "none"),
        "edit_history: %s" % (json.dumps(q.get("errorHistory", []), ensure_ascii=False) or "none"),
        "</checks>",
        "<student_reports>\n%s\n</student_reports>" % "\n".join(rep),
    ])


# --- edits: model output -> raw edits the guards understand ---------------------------------
def build_edits(q, proposed):
    """Returns (edits, errors). Code fills `find` for answer/questionType from the record and
    maps text finds from the clean view back to raw HTML. Nothing the model wrote is trusted."""
    p = payload(q)
    target_type = q["questionType"]
    for e in proposed:
        if e.get("field") == "questionType":
            target_type = (e.get("replace") or "").strip()
    out, errs = [], []
    for e in proposed:
        f, find, rep = e.get("field"), e.get("find") or "", e.get("replace") or ""
        if f == "questionType":
            out.append({"field": "questionType", "find": q["questionType"], "replace": rep.strip()})
        elif f == "answer":
            out.append({"field": "answer", "find": p["answer"], "replace": to_stored_answer(target_type, rep)})
        elif f in ("question", "solution"):
            if find.strip() == "*":
                out.append({"field": f, "find": "*", "replace": rep})
                continue
            m = map_find(get_field(q, f), find, rep)
            if m is None:
                errs.append("find text not found exactly once in %s: %r" % (f, find[:60]))
            else:
                out.append({"field": f, "find": m[0], "replace": m[1]})
        else:
            errs.append("unknown field %r" % f)
    return out, errs


def one_or_list(edits):
    if not edits:
        return None
    return edits[0] if len(edits) == 1 else edits


# --- reply ------------------------------------------------------------------------------------
FIXED_WORDS = re.compile(r"\b(fixed|corrected|updated|changed the (?:answer|key)|theek kar (?:diya|di)|sudhaar)\b", re.I)
TEMPLATES = {
    "FIXED": "Thank you, you were right. We found the problem and corrected it; please check the question again.",
    "WITH_FACULTY": "Thank you for flagging this. A teacher is checking it now, and the question will be updated if needed.",
    "no_error": "Thanks for checking so carefully. We re-solved it and the stored answer is correct; have another look at the solution steps.",
    "display_issue": "Thanks for telling us. The question itself is fine; this looks like a display problem on your device, and we're looking into it.",
    "app_issue": "Thanks for telling us. This looks like an app problem, not a problem with the question; our tech team will look into it.",
    "language_mode": "Thanks for asking. This question is not available in that language yet; we've noted your request.",
    "already_fixed": "Thank you for reporting this. It has already been corrected, so please reload the question.",
    "not_actionable": "Thanks for writing to us. We checked the question and couldn't find a problem; if something looks wrong, tell us what and where.",
}


HINDI_WORDS = re.compile(r"\b(hai|hain|nahi|nahin|kya|kyu|kyun|galat|sahi|chahiye|chahie|aana|aa|raha|rahi|ki|ka|ke|me|mein|hona|toh|to|kar|karu|karna|di|diya|bhi|ho|hi|aur|wala|kaise)\b", re.I)


def student_language(comment):
    """Two or more common Hindi words in Roman script -> Hinglish. Decided in code, not by the model."""
    return "Hinglish" if len(HINDI_WORDS.findall(comment or "")) >= 2 else "English"


def status_of(route):
    return {"auto_apply": "FIXED", "faculty_review": "WITH_FACULTY"}.get(route, "NO_CHANGE")


def template_reply(verdict, route):
    st = status_of(route)
    return TEMPLATES[st] if st != "NO_CHANGE" else TEMPLATES.get(verdict, TEMPLATES["not_actionable"])


def reply_ok(text, status):
    if not text or len(text) > 420:
        return False
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    if not 1 <= len(sentences) <= 3:
        return False
    if status != "FIXED" and FIXED_WORDS.search(text):
        return False
    return True


def student_reply(llm, report, row, found):
    st = status_of(row["route"])
    facts = "\n".join([
        "status: %s" % st,
        "student_language: %s" % student_language(report["comment"]),
        "verdict: %s (%s)" % (row["verdict"], TAX["verdicts"][row["verdict"]]),
        "what we found: %s" % found,
        "<student_comment>\n%s\n</student_comment>" % _esc(report["comment"]),
    ])
    try:
        out = llm.json("reply", P.REPLY_V2, facts, P.REPLY_SCHEMA, effort="low")
        text = (out.get("reply") or "").strip()
    except LLMError:
        text = ""
    if reply_ok(text, st):
        return text, "llm"
    return template_reply(row["verdict"], row["route"]), "template"


# --- the audit ----------------------------------------------------------------------------------
def solve(llm, role, q):
    out = llm.json(role, P.SOLVER_V1, solver_input(q), P.SOLVER_SCHEMA, effort="high")
    return {k: out.get(k, "") for k in ("answer", "missing_info", "working", "confidence")}


def extract(llm, q, solution_html):
    out = llm.json("reply", P.EXTRACTOR_V1, extractor_input(q, solution_html), P.EXTRACTOR_SCHEMA, effort="low")
    return {"answer": out.get("answer", ""), "note": out.get("note", "")}


def reason_kind(r):
    if r.startswith("find text") or "find must match" in r or "outside scope" in r or "changed numbers" in r \
            or "invalid for type" in r or "!= stored" in r or "additive" in r or "rewrite not allowed" in r \
            or "touches stem" in r or "grows text" in r or "may not carry an edit" in r or "unknown field" in r \
            or "empty find" in r or "may not change questionType" in r or "bad target type" in r:
        return "guard_rejected_edit"
    if "independent solves" in r or "solution edit needs" in r:
        return "checks_disagree"
    if r.startswith("confidence"):
        return "low_confidence"
    if "by policy" in r or "biology" in r:
        return "policy"
    if "inserted value" in r:
        return "inserted_value_unseen"
    if "no edit proposed" in r:
        return "no_edit"
    return "other"


def route_defect(llm, q, d, sv):
    """sv: normalised solver/solution/key values. Returns the routed defect record."""
    repair = d.get("repair")
    edits, errs = build_edits(q, d.get("edits") or [])
    if repair in TAX["repairs"] and not TAX["repairs"][repair]["fields"]:
        edits = []                                   # human-only repair: suggestion text only
    conf = max(0.0, min(1.0, float(d.get("confidence") or 0)))
    target_type = next((e["replace"] for e in edits if e["field"] == "questionType"), q["questionType"])
    ans_edit = next((e for e in edits if e["field"] == "answer"), None)
    new_ans = norm_answer(target_type, ans_edit["replace"]) if ans_edit else None
    key_malformed = not valid_answer(q["questionType"], payload(q)["answer"])

    key_agree = None
    if ans_edit:
        key_agree = (sv["solvers_agree"] and same(sv["a"], new_ans) and same(sv["concl"], new_ans)
                     and (key_malformed or not same(new_ans, sv["key"])))
        if not sv["solvers_agree"]:
            conf = min(conf, 0.6)

    sol_agree, edited_concl = None, None
    if repair in SOLUTION_WRITES and edits and not errs and not validate_edit(q, repair, edits):
        final_key = new_ans if ans_edit else sv["key"]
        if sv["solvers_agree"] and same(sv["a"], final_key):
            new_q = apply_edit(q, edits)
            edited_concl = extract(llm, new_q, payload(new_q)["solution"])
            sol_agree = same(norm_answer(new_q["questionType"], edited_concl["answer"]), final_key)
        else:
            sol_agree = False

    route, reasons = decide_route("defect", repair, edits or None, q, conf,
                                  key_checks_agree=key_agree, solution_checks_agree=sol_agree)
    if errs:
        route, reasons = "faculty_review", errs + reasons
    return dict(id=d.get("id"), repair=repair, edit=one_or_list(edits), proposed_edits=d.get("edits") or [],
                suggestion=d.get("suggestion", ""), evidence=d.get("evidence", ""), confidence=round(conf, 2),
                model_confidence=d.get("confidence"), reported=bool(d.get("reported")), route=route,
                reasons=reasons, reason_kinds=sorted({reason_kind(r) for r in reasons}),
                key_checks_agree=key_agree, solution_checks_agree=sol_agree, edited_solution_concludes=edited_concl)


def facts_line(sv):
    return "Checks: solver A=%s, solver B=%s, stored solution=%s, key=%s." % (
        show(sv["a"]), show(sv["b"]), show(sv["concl"]), show(sv["key"]))


def audit_question(llm, q, reports):
    t = q["questionType"]
    lint = lint_question(q)
    pre = {r["reportId"]: triage_report(r["comment"]) for r in reports}
    live = [r for r in reports if not pre[r["reportId"]]["short_circuit"]]

    a, b = solve(llm, "solver_a", q), solve(llm, "solver_b", q)
    concl = extract(llm, q, payload(q)["solution"])
    sv = dict(a=norm_answer(t, a["answer"]), b=norm_answer(t, b["answer"]),
              concl=norm_answer(t, concl["answer"]), key=norm_answer(t, payload(q)["answer"]))
    sv["solvers_agree"] = same(sv["a"], sv["b"])
    checks = dict(a=a, b=b, concl=concl, solvers_agree="yes" if sv["solvers_agree"] else "no",
                  solvers_match_key="yes" if sv["solvers_agree"] and same(sv["a"], sv["key"]) else "no",
                  solvers_match_solution="yes" if sv["solvers_agree"] and same(sv["a"], sv["concl"]) else "no")

    raw, error = None, None
    try:
        raw = llm.json("auditor", P.AUDITOR_V1, auditor_input(q, live or reports, checks, lint,
                                                            {k: v["hint"] for k, v in pre.items()}),
                       P.auditor_schema(), effort="high")
    except LLMError as e:
        error = str(e)
    defects = [route_defect(llm, q, d, sv) for d in (raw or {}).get("defects", []) if isinstance(d, dict)]
    by_id = {d["id"]: d for d in defects}
    verdicts = {r.get("reportId"): r for r in (raw or {}).get("reports", []) if isinstance(r, dict)}

    rows = []
    for rep in reports:
        rid = rep["reportId"]
        base = dict(reportId=rid, questionId=q["_id"])
        if pre[rid]["short_circuit"]:
            row = dict(base, verdict="not_actionable", repair=None, edit=None, route="no_change", confidence=0.9,
                       evidence="Comment has no readable content (pre-triage: %s); no claim to check." % pre[rid]["hint"])
            row["student_reply"], row["_reply_src"] = template_reply("not_actionable", "no_change"), "template"
            rows.append(row)
            continue
        v = verdicts.get(rid)
        if v is None or v.get("verdict") not in TAX["verdicts"]:
            row = dict(base, verdict="not_actionable", repair=None, edit=None, route="faculty_review", confidence=0.0,
                       evidence="The auditor returned no verdict for this report%s; a human must look." %
                                (" (%s)" % error[:80] if error else ""))
            found = "we could not finish checking this automatically"
        elif v["verdict"] == "defect":
            d = by_id.get(v.get("defect_id"))
            if d is None:
                row = dict(base, verdict="defect", repair=None, edit=None, route="faculty_review",
                           confidence=0.0, evidence=(v.get("evidence") or "") + " (no matching defect; needs a human)")
            else:
                conf = round(min(d["confidence"], max(0.0, min(1.0, float(v.get("confidence") or 0)))), 2)
                ev = d["evidence"] or v.get("evidence", "")
                if d["edit"] and any(e["field"] == "answer" for e in (d["edit"] if isinstance(d["edit"], list) else [d["edit"]])):
                    ev += " " + facts_line(sv)
                if d["route"] == "faculty_review" and d["reasons"]:
                    ev += " Sent to faculty: " + "; ".join(d["reasons"][:2]) + "."
                row = dict(base, verdict="defect", repair=d["repair"], edit=d["edit"], route=d["route"],
                           confidence=conf, evidence=ev.strip(), _defect_id=d["id"], _reasons=d["reasons"],
                           _suggestion=d["suggestion"])
            found = v.get("evidence") or row["evidence"]
        else:
            row = dict(base, verdict=v["verdict"], repair=None, edit=None, route="no_change",
                       confidence=round(max(0.0, min(1.0, float(v.get("confidence") or 0))), 2),
                       evidence=(v.get("evidence") or "").strip())
            found = row["evidence"]
        row["student_reply"], row["_reply_src"] = student_reply(llm, rep, row, found)
        rows.append(row)

    linked = {verdicts[r].get("defect_id") for r in verdicts if verdicts[r].get("verdict") == "defect"}
    unreported = [dict(d, questionId=q["_id"]) for d in defects if d["id"] not in linked]
    audit = dict(questionId=q["_id"], subject=q["subject"], chapter=q["chapter"], type=t, lint=lint,
                 pre_triage=pre, solver_a=a, solver_b=b, solution_concludes=concl,
                 normalised={k: show(v) if k != "solvers_agree" else v for k, v in sv.items()},
                 defects=defects, auditor_error=error, auditor_raw=raw,
                 models={r: llm.model(r) for r in ("solver_a", "solver_b", "auditor", "reply")},
                 prompt_versions={k: v[0] for k, v in P.PROMPTS.items()})
    return rows, audit, unreported


RESULT_KEYS = ["reportId", "questionId", "verdict", "repair", "edit", "route", "confidence", "evidence", "student_reply"]


def summarise(rows, audits):
    from collections import Counter
    defects = [d for a in audits for d in a["defects"]]
    proposed_edit = [d for d in defects if d["proposed_edits"] and TAX["repairs"].get(d["repair"], {}).get("fields")]
    guard_rej = [d for d in proposed_edit if "guard_rejected_edit" in d["reason_kinds"]]
    return dict(
        questions=len(audits), reports=len(rows),
        routes=Counter(r["route"] for r in rows), verdicts=Counter(r["verdict"] for r in rows),
        defects_found=len(defects), defects_auto=sum(d["route"] == "auto_apply" for d in defects),
        defects_faculty_by_reason=Counter(k for d in defects if d["route"] == "faculty_review" for k in d["reason_kinds"]),
        audits_with_guard_rejection=len({a["questionId"] for a in audits for d in a["defects"] if d in guard_rej}),
        guard_rejected_defects=[dict(questionId=a["questionId"], repair=d["repair"], reasons=d["reasons"])
                                for a in audits for d in a["defects"] if d in guard_rej],
        proposed_auto_eligible_edits=len(proposed_edit),
        auditor_errors=[a["questionId"] for a in audits if a["auditor_error"]],
        replies_from_template=sum(r.get("_reply_src") == "template" for r in rows),
    )


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="question id suffixes to run")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)
    Q, R = load_questions(), load_reports()
    by_q = {}
    for r in sorted(R, key=lambda r: r["reportId"]):
        by_q.setdefault(r["questionId"], []).append(r)
    qids = sorted(Q)
    if args.only:
        qids = [k for k in qids if any(k.endswith(s) for s in args.only)]
    if args.limit:
        qids = qids[:args.limit]
    llm = LLM()
    key_var = "GEMINI_API_KEY" if llm.provider == "gemini" else "ANTHROPIC_API_KEY"
    import os
    if not os.environ.get(key_var):
        print("%s is not set. export it first (never commit it)." % key_var)
        return 1
    print("provider=%s models=%s" % (llm.provider, {r: llm.model(r) for r in ("solver_a", "solver_b", "auditor", "reply")}))
    rows, audits, unreported, stopped = [], [], [], None
    for i, qid in enumerate(qids, 1):
        print("[%d/%d] %s %s (%d reports)" % (i, len(qids), qid, Q[qid]["subject"], len(by_q.get(qid, []))))
        try:
            r, a, u = audit_question(llm, Q[qid], by_q.get(qid, []))
        except QuotaExhausted as e:
            stopped = str(e)
            print("STOPPED: %s\nFinished calls are cached; rerun after the quota resets (midnight Pacific)." % stopped)
            break
        except LLMError as e:
            print("  solver/extractor failed: %s -> question skipped this run" % e)
            continue
        rows += r; audits.append(a); unreported += u

    OUT.mkdir(exist_ok=True)
    complete = not stopped and len(audits) == len(Q) and len(rows) == len(R)
    clean_rows = [{k: r[k] for k in RESULT_KEYS} for r in sorted(rows, key=lambda r: r["reportId"])]
    target = ROOT / "results.json" if complete else OUT / "results.partial.json"
    target.write_text(json.dumps(clean_rows, indent=1, ensure_ascii=False), encoding="utf-8")
    suffix = "" if complete else ".partial"
    (OUT / ("audits%s.json" % suffix)).write_text(json.dumps(audits, indent=1, ensure_ascii=False, default=list), encoding="utf-8")
    (OUT / ("report_rows%s.json" % suffix)).write_text(json.dumps(sorted(rows, key=lambda r: r["reportId"]), indent=1, ensure_ascii=False), encoding="utf-8")
    (OUT / ("unreported_findings%s.json" % suffix)).write_text(json.dumps(unreported, indent=1, ensure_ascii=False), encoding="utf-8")
    summary = dict(summarise(rows, audits), llm=llm.stats, complete=complete, stopped=stopped)
    (OUT / ("run_summary%s.json" % suffix)).write_text(json.dumps(summary, indent=1, ensure_ascii=False, default=list), encoding="utf-8")
    print(json.dumps(summary, indent=1, ensure_ascii=False, default=list))
    print("wrote", target.relative_to(ROOT))
    return 0 if complete or args.only or args.limit else 2


if __name__ == "__main__":
    sys.exit(main())
