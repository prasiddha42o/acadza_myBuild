"""Hard guards. The LLM only PROPOSES; this module decides what is allowed and how it routes.
Principle: a wrong auto-applied fix is far worse than a slow human review, so every check
fails closed (violation -> faculty_review)."""
import copy
import re

from .common import flat, get_field, load_taxonomy, payload
from .lint import valid_answer

TAX = load_taxonomy()
WHOLE = "*"                       # find marker: replace the whole field (solution rewrites only)
REWRITE_OK = {"solution_fix", "solution_missing"}
DIGIT_PRESERVING = {"spelling", "noise", "exam_reference", "markup_leak"}
MAX_GROWTH = 300                  # chars a non-rewrite edit may add


def _digits(s):
    return sorted(re.findall(r"\d+(?:\.\d+)?", flat(s)))


def edit_list(edit):
    if edit is None:
        return []
    return edit if isinstance(edit, list) else [edit]


def validate_edit(q, repair, edit):
    """Return a list of violation strings (empty list == edit is allowed)."""
    v = []
    if repair not in TAX["repairs"]:
        return ["unknown repair %r" % repair]
    spec = TAX["repairs"][repair]
    edits = edit_list(edit)
    if not edits:
        return v
    if not spec["fields"]:
        return ["repair %r is human-only and may not carry an edit" % repair]
    for e in edits:
        field, find, rep = e.get("field"), e.get("find"), e.get("replace")
        if field not in spec["fields"]:
            v.append("field %r outside scope %s of %s" % (field, spec["fields"], repair))
            continue
        if field == "answer":
            cur = payload(q)["answer"]
            if find != cur:
                v.append("answer find %r != stored %r" % (find, cur))
            target_type = next((x["replace"] for x in edits if x.get("field") == "questionType"), q["questionType"])
            if not valid_answer(target_type, rep):
                v.append("new answer %r invalid for type %s" % (rep, target_type))
            continue
        if field == "questionType":
            if find != q["questionType"]:
                v.append("questionType find %r != stored %r" % (find, q["questionType"]))
            if rep not in ("scq", "mcq", "integerQuestion"):
                v.append("bad target type %r" % rep)
            continue
        text = get_field(q, field)
        if find == WHOLE:
            if repair not in REWRITE_OK:
                v.append("whole-field rewrite not allowed for %s" % repair)
            continue
        if not isinstance(find, str) or not find:
            v.append("empty find")
            continue
        n = text.count(find)
        if n != 1:
            v.append("find must match exactly once in %s (matched %d)" % (field, n))
            continue
        if len(rep) - len(find) > MAX_GROWTH and repair not in REWRITE_OK:
            v.append("edit grows text by >%d chars" % MAX_GROWTH)
        if repair in DIGIT_PRESERVING:
            removed_ok = repair in ("noise", "exam_reference")
            a, b = _digits(find), _digits(rep)
            if (b != a) and not (removed_ok and all(x in a for x in b)):
                v.append("%s edit changed numbers: %s -> %s" % (repair, a, b))
        if repair in ("answer_form",) and not all(ch in rep for ch in set(flat(find))):
            v.append("answer_form must be additive")
        if repair == "option_repair":
            stem = payload(q)["question"]
            first_opt = stem.find("(A)")
            if first_opt == -1 or stem.find(find) < first_opt:
                v.append("option_repair touches stem text before options")
    if repair == "answer_key" and any(e.get("field") == "questionType" for e in edits):
        v.append("answer_key may not change questionType")
    return v


def apply_edit(q, edit):
    """Return a deep copy of q with the edit(s) applied. Caller must have validated first."""
    out = copy.deepcopy(q)
    for e in edit_list(edit):
        f, find, rep = e["field"], e["find"], e["replace"]
        if f == "questionType":
            old = out["questionType"]
            out["questionType"] = rep
            out[rep] = out.pop(old)          # payload key follows the type
        elif f == "answer":
            payload(out)["answer"] = rep
        elif find == WHOLE:
            payload(out)[f] = rep
        else:
            payload(out)[f] = payload(out)[f].replace(find, rep, 1)
    return out


def decide_route(verdict, repair, edit, q, confidence, key_checks_agree=None, threshold=0.9):
    """Final routing. Returns (route, reasons). Anything doubtful -> faculty_review."""
    if verdict != "defect":
        return "no_change", []
    reasons = []
    spec = TAX["repairs"].get(repair)
    if spec is None:
        return "faculty_review", ["unknown repair"]
    if not spec["auto"]:
        return "faculty_review", ["%s needs a human by policy" % repair]
    reasons += validate_edit(q, repair, edit)
    if not edit_list(edit):
        reasons.append("no edit proposed")
    if confidence < threshold:
        reasons.append("confidence %.2f < %.2f" % (confidence, threshold))
    if repair == "answer_key" and key_checks_agree is not True:
        reasons.append("answer_key needs 2 independent solves + stored solution to agree")
    return ("faculty_review", reasons) if reasons else ("auto_apply", [])
