"""Score a results.json against eval/gold.json (HAND-LABELLED by the builder; Acadza's own
labels were not shipped, so treat these numbers as a sanity check, not a benchmark).
Usage: python -m eval.evaluate results.json"""
import json
import sys
from pathlib import Path

from engine.common import load_questions
from engine.guards import edit_list, validate_edit

HERE = Path(__file__).resolve().parent


def norm(e):
    return sorted(json.dumps(x, sort_keys=True) for x in edit_list(e))


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
                c["edit"] += norm(r.get("edit")) == norm(g["edit"])
        if r["route"] == "auto_apply":
            auto_total += 1
            same = norm(r.get("edit")) == norm(g["edit"]) and g["route"] == "auto_apply"
            auto_exact += same
            viol = validate_edit(Q[r["questionId"]], r.get("repair"), r.get("edit"))
            if not same or viol:
                unsafe.append((r["reportId"], "differs from gold" if not same else "", viol))
    p, rc = prf(tp, fp, fn)
    return dict(
        reports=n,
        verdict_acc=round(c["verdict"] / n, 3),
        route_acc=round(c["route"] / n, 3),
        repair_acc_on_defects=round(c["repair"] / max(c["repair_n"], 1), 3),
        edit_exact_on_editable=round(c["edit"] / max(c["edit_n"], 1), 3),
        defect_precision=p, defect_recall=rc,
        auto_applied=auto_total, auto_applied_exact=auto_exact,
        auto_apply_precision=round(auto_exact / auto_total, 3) if auto_total else None,
        unsafe_or_divergent_autos=unsafe,
    )


if __name__ == "__main__":
    gold = json.loads((HERE / "gold.json").read_text(encoding="utf-8"))
    results = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(json.dumps(evaluate(results, gold), indent=2, ensure_ascii=False))
