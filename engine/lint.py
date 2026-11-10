"""Deterministic, free checks run on every question BEFORE any LLM call.
Each finding: {check, field, detail}. These are leads for the classifier, never fixes."""
import re

from .common import flat, get_field, parse_options, payload

EXAM_TAG = re.compile(r"\((?:JEE|NEET|AIPMT|AIEEE|IIT|CBSE|AIIMS)[^)]*\d{4}[^)]*\)\s*", re.I)
LATEX_LEAK = re.compile(r"\\(?:frac|sqrt|times|alpha|theta|pi|cdot|mathrm)\b|\$[^$\n]{1,40}\$")
NOISE = re.compile(r"space for rough work|rough work|^\s*Q\.?\s*\d+[\).]", re.I | re.M)
FIG_REF = re.compile(r"as shown in (?:the )?(?:figure|fig)|in the figure|figure shown|given figure", re.I)


def valid_answer(qtype, ans):
    if qtype == "scq":
        return isinstance(ans, str) and ans in list("ABCD")
    if qtype == "mcq":
        return isinstance(ans, list) and len(ans) > 0 and all(isinstance(a, str) and a in list("ABCD") for a in ans)
    if qtype == "integerQuestion":
        return isinstance(ans, str) and re.fullmatch(r"-?\d+(\.\d+)?", ans.strip() or "x") is not None
    return False


def lint_question(q):
    p, t = payload(q), q["questionType"]
    f = []
    stem, sol = p["question"], p["solution"]

    for fld in ("question", "solution"):
        txt = p[fld]
        if EXAM_TAG.search(txt):
            f.append(dict(check="exam_reference", field=fld, detail=EXAM_TAG.search(txt).group(0).strip()))
        if LATEX_LEAK.search(txt):
            f.append(dict(check="markup_leak", field=fld, detail=LATEX_LEAK.search(txt).group(0)))
        if NOISE.search(flat(txt)):
            f.append(dict(check="noise", field=fld, detail=NOISE.search(flat(txt)).group(0)))
        if "\u200b" in txt:
            f.append(dict(check="zero_width", field=fld, detail="%d zero-width chars" % txt.count("\u200b")))

    if not valid_answer(t, p["answer"]):
        f.append(dict(check="answer_malformed", field="answer", detail=repr(p["answer"])))

    if t in ("scq", "mcq"):
        opts = parse_options(stem)
        if sorted(opts) != list("ABCD"):
            missing = [l for l in "ABCD" if l not in opts]
            f.append(dict(check="option_missing", field="question", detail="missing " + ",".join(missing)))
        blanks = [l for l, v in opts.items() if not v.strip()]
        if blanks:
            f.append(dict(check="option_blank", field="question", detail=",".join(blanks)))
        norm = {l: re.sub(r"\s+", "", v) for l, v in opts.items() if v.strip()}
        seen = {}
        for l, v in norm.items():
            seen.setdefault(v, []).append(l)
        for v, ls in seen.items():
            if len(ls) > 1:
                f.append(dict(check="option_duplicate", field="question", detail="/".join(ls)))
        keys = [p["answer"]] if t == "scq" else (p["answer"] if isinstance(p["answer"], list) else [])
        for k in keys:
            if isinstance(k, str) and k in "ABCD" and k and k in "ABCD" and k not in opts and opts:
                f.append(dict(check="key_not_in_options", field="answer", detail=k))
        if t == "mcq" and isinstance(p["answer"], list) and len(p["answer"]) == 1:
            f.append(dict(check="mcq_single_key", field="answer", detail="mcq with one key; may be scq"))
        if t == "mcq" and not re.search(r"(?i)statement|one or more|multiple|select the correct", flat(stem)):
            f.append(dict(check="mcq_stem_not_multi", field="questionType", detail="stem does not say multiple answers"))

    if FIG_REF.search(flat(stem)) and not p.get("quesImages"):
        f.append(dict(check="figure_missing", field="question", detail="stem refers to a figure; quesImages empty"))
    if not flat(sol):
        f.append(dict(check="solution_empty", field="solution", detail=""))
    return f


def fixed_after_report(q, reported_at):
    """errorHistory entries by faculty that post-date the report -> the problem may already be fixed."""
    return [h for h in q.get("errorHistory", []) if h.get("at", "") > reported_at]
