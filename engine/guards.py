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


BIOLOGY = {"Biology", "Botany", "Zoology"}
SOLUTION_WRITES = {"solution_fix", "solution_missing"}
INSERTS_VALUES = {"stem_data", "option_repair"}


def inserted_numbers_unseen(q, edit):
    """Numbers an edit ADDS to the stem that never appear in the stored solution.
    Weak consistency check only (digits from subscripts or unrelated steps also count as
    'seen'); it catches invented values, it does not prove the inserted value is right."""
    sol = _digits(payload(q)["solution"])
    out = []
    for e in edit_list(edit):
        if e.get("field") != "question" or e.get("find") == WHOLE:
            continue
        before = _digits(e.get("find", ""))
        for n in _digits(e.get("replace", "")):
            if n in before:
                before.remove(n)
            elif n not in sol:
                out.append(n)
    return out


def decide_route(verdict, repair, edit, q, confidence, key_checks_agree=None, threshold=0.9,
                 solution_checks_agree=None):
    """Final routing. Returns (route, reasons). Anything doubtful -> faculty_review.
    key_checks_agree: 2 blind solves + stored solution agree on the NEW key (computed in run.py).
    solution_checks_agree: 2 blind solves agree with the key AND the edited solution ends there."""
    if verdict != "defect":
        return "no_change", []
    reasons = []
    spec = TAX["repairs"].get(repair)
    if spec is None:
        return "faculty_review", ["unknown repair"]
    if not spec["auto"]:
        return "faculty_review", ["%s needs a human by policy" % repair]
    reasons += validate_edit(q, repair, edit)
    edits = edit_list(edit)
    if not edits:
        reasons.append("no edit proposed")
    if confidence < threshold:
        reasons.append("confidence %.2f < %.2f" % (confidence, threshold))
    touches_answer = any(e.get("field") == "answer" for e in edits)
    if (repair == "answer_key" or touches_answer) and key_checks_agree is not True:
        reasons.append("answer change needs 2 independent solves + stored solution to agree")
    if touches_answer and q.get("subject") in BIOLOGY:
        reasons.append("biology key change: nothing to compute, faculty decides")
    if repair in SOLUTION_WRITES and solution_checks_agree is not True:
        reasons.append("solution edit needs both solves = key and the edited solution to end at the key")
    if repair in INSERTS_VALUES:
        unseen = inserted_numbers_unseen(q, edit)
        if unseen:
            reasons.append("inserted value(s) %s not found anywhere in the stored solution" % unseen)
    return ("faculty_review", reasons) if reasons else ("auto_apply", [])
