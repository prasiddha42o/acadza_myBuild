"""Score a results.json against eval/gold.json (HAND-LABELLED by the builder; Acadza's own
labels were not shipped, so treat these numbers as a sanity check, not a benchmark).
Usage: python -m eval.evaluate results.json"""
import json
import sys
from pathlib import Path

from engine.common import load_questions
from engine.guards import WHOLE, apply_edit, edit_list, validate_edit

HERE = Path(__file__).resolve().parent


def norm(e):
    return sorted(json.dumps(x, sort_keys=True) for x in edit_list(e))


def outcome(q, e):
    """The question as it would be after the edit; None if the edit cannot apply."""
    try:
        return json.dumps(apply_edit(q, e), sort_keys=True)
    except Exception:
        return None


def is_rewrite(e):
    return any(x["find"] == WHOLE for x in edit_list(e))


def match(q, r_edit, g_edit):
    """exact: same find/replace. same_result: different span, identical resulting question.
    rewrite: both replace a whole field (wording always differs; judge by eye). differs: otherwise."""
    if norm(r_edit) == norm(g_edit):
        return "exact"
    if g_edit and r_edit and outcome(q, r_edit) is not None and outcome(q, r_edit) == outcome(q, g_edit):
        return "same_result"
    if g_edit and r_edit and is_rewrite(r_edit) and is_rewrite(g_edit):
        return "rewrite"
    return "differs"


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return round(p, 3), round(r, 3)


def evaluate(results, gold):
    Q = load_questions()
    res = {r["reportId"]: r for r in results}
    n = len(gold)
    c = dict(verdict=0, route=0, repair=0, repair_n=0, edit=0, edit_n=0)
    tp = fp = fn = 0
    auto_total = auto_exact = 0
    auto_kind = dict(exact=0, same_result=0, rewrite=0, differs=0)
    unsafe = []
    for g in gold:
        r = res.get(g["reportId"])
        if r is None:
            fn += g["verdict"] == "defect"
            continue
        ok_v = r["verdict"] == g["verdict"] or r["verdict"] in g.get("alt_verdicts", [])
        c["verdict"] += ok_v
        c["route"] += r["route"] == g["route"]
        gd, rd = g["verdict"] == "defect", r["verdict"] == "defect"
        tp += gd and rd
        fp += (not gd) and rd
        fn += gd and (not rd)
        if gd:
            c["repair_n"] += 1
            c["repair"] += r.get("repair") == g["repair"]
            if g["edit"] is not None:
                c["edit_n"] += 1
                c["edit"] += match(Q[r["questionId"]], r.get("edit"), g["edit"]) in ("exact", "same_result")
        if r["route"] == "auto_apply":
            auto_total += 1
            kind = match(Q[r["questionId"]], r.get("edit"), g["edit"]) if g["route"] == "auto_apply" else "differs"
            auto_kind[kind] += 1
            auto_exact += kind in ("exact", "same_result")
            viol = validate_edit(Q[r["questionId"]], r.get("repair"), r.get("edit"))
            if kind != "exact" or viol:
                unsafe.append((r["reportId"], kind, viol))
    p, rc = prf(tp, fp, fn)
    return dict(
        reports=n,
        verdict_acc=round(c["verdict"] / n, 3),
        route_acc=round(c["route"] / n, 3),
        repair_acc_on_defects=round(c["repair"] / max(c["repair_n"], 1), 3),
        edit_match_on_editable=round(c["edit"] / max(c["edit_n"], 1), 3),
        defect_precision=p, defect_recall=rc,
        auto_applied=auto_total, auto_applied_same_result=auto_exact, auto_by_match=auto_kind,
        auto_apply_precision=round(auto_exact / auto_total, 3) if auto_total else None,
        autos_not_exact=unsafe,  # (reportId, match kind, guard violations); "differs" and violations need a look
    )


if __name__ == "__main__":
    gold = json.loads((HERE / "gold.json").read_text(encoding="utf-8"))
    results = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(json.dumps(evaluate(results, gold), indent=2, ensure_ascii=False))
