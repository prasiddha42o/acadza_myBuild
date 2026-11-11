import json
from pathlib import Path

import pytest

from engine.common import load_questions, load_reports, load_taxonomy, parse_options, payload
from engine.guards import apply_edit, decide_route, edit_list, validate_edit
from engine.lint import fixed_after_report, lint_question
from engine.rules import duplicates, triage_report
from eval.evaluate import evaluate

ROOT = Path(__file__).resolve().parent.parent
GOLD = json.loads((ROOT / "eval/gold.json").read_text(encoding="utf-8"))
Q = load_questions()
TAX = load_taxonomy()


def test_gold_covers_all_reports_once():
    ids = [r["reportId"] for r in load_reports()]
    assert sorted(g["reportId"] for g in GOLD) == sorted(ids)
    for g in GOLD:
        assert g["verdict"] in TAX["verdicts"]
        if g["repair"]:
            assert g["repair"] in TAX["repairs"]


@pytest.mark.parametrize("g", [g for g in GOLD if g["edit"]], ids=lambda g: g["reportId"])
def test_every_gold_edit_passes_guards_and_applies(g):
    q = Q[g["questionId"]]
    assert validate_edit(q, g["repair"], g["edit"]) == []
    new = apply_edit(q, g["edit"])
    assert new != q
    # the original is never mutated
    assert q is Q[g["questionId"]]


# Stage 2 guard: values inserted into a stem must appear in the stored solution (weak check).
# R011's solution only says |F12| = |F21| (numbers 12, 21), so "1 : 1" is not seen -> faculty.
# Gold keeps auto_apply as the label; the engine is deliberately stricter here.
STRICTER_THAN_GOLD = {"R011": "faculty_review"}


def test_gold_routes_match_policy():
    for g in GOLD:
        expected = STRICTER_THAN_GOLD.get(g["reportId"], g["route"])
        route, why = decide_route(g["verdict"], g["repair"], g["edit"], Q[g["questionId"]], 0.95,
                                  key_checks_agree=True, solution_checks_agree=True)
        assert route == expected, (g["reportId"], route, why)


def test_scope_violation_rejected():
    q = Q["61ee6c9d90a1a968a168686b"]
    bad = {"field": "answer", "find": "B", "replace": "A"}
    assert any("outside scope" in v for v in validate_edit(q, "spelling", bad))


def test_spelling_cannot_change_numbers():
    q = Q["61dffbec08489156b684bd22"]
    bad = {"field": "question", "find": "Epithellial tisue", "replace": "Epithelial tissue 2"}
    assert any("changed numbers" in v for v in validate_edit(q, "spelling", bad))


def test_find_must_be_unique_and_present():
    q = Q["61dffbec08489156b684bd22"]
    assert validate_edit(q, "spelling", {"field": "question", "find": "NOPE", "replace": "x"})
    assert validate_edit(q, "spelling", {"field": "question", "find": "tissue", "replace": "tissues"})  # matches >1 or 0


def test_human_only_repairs_cannot_carry_edit():
    q = Q["6956d79788d7701e5617bc29"]
    e = {"field": "question", "find": "0 W", "replace": "5 W"}
    assert validate_edit(q, "identical_options", e)
    assert decide_route("defect", "identical_options", None, q, 0.99)[0] == "faculty_review"


def test_answer_key_needs_independent_agreement():
    q = Q["60f744b3f1b9bc7cc7b4a949"]
    e = {"field": "answer", "find": "C", "replace": "A"}
    assert decide_route("defect", "answer_key", e, q, 0.99, key_checks_agree=None)[0] == "faculty_review"
    assert decide_route("defect", "answer_key", e, q, 0.99, key_checks_agree=True)[0] == "auto_apply"
    assert decide_route("defect", "answer_key", e, q, 0.80, key_checks_agree=True)[0] == "faculty_review"


def test_answer_key_cannot_use_student_stale_find():
    q = Q["68e870ef42c06737d6e565c4"]  # key is A; student claims B/8225
    assert validate_edit(q, "answer_key", {"field": "answer", "find": "B", "replace": "D"})
    assert validate_edit(q, "answer_key", {"field": "answer", "find": "A", "replace": "Z"})


def test_option_repair_cannot_touch_stem():
    q = Q["68e872546e45ba37dcc16bc0"]
    e = {"field": "question", "find": "ratio of coulombic forces", "replace": "ratio of forces"}
    assert any("before options" in v for v in validate_edit(q, "option_repair", e))


def test_lint_catches_known_defects():
    def checks(qid):
        return {f["check"] for f in lint_question(Q[qid])}
    assert "option_blank" in checks("68e872546e45ba37dcc16bc0")
    assert "option_duplicate" in checks("6956d79788d7701e5617bc29")
    miss = [f for f in lint_question(Q["622f08d7b70e4b50e4500a1b"]) if f["check"] == "option_missing"]
    assert miss and miss[0]["detail"] == "missing D"   # exactly D, not A-D
    assert "exam_reference" in checks("617fa18efd53ff1629ed890f")
    assert "markup_leak" in checks("63765a28f1d8416ae0408d89")
    assert "answer_malformed" in checks("68f21caf941cda588bc04365")
    assert "figure_missing" in checks("622df25ab70e4b50e44ff837")


def test_lint_clean_on_good_questions():
    for qid in ("60ca4feb6420652745f55522", "68c9ae5afb6b2f16e2d43b1c", "6208e298fa399567e0878700"):
        bad = {f["check"] for f in lint_question(Q[qid])} - {"zero_width"}
        assert not bad, (qid, bad)


def test_already_fixed_detected_from_history():
    q = Q["6208e80a9cb2ab67dac61bd0"]
    rep = next(r for r in load_reports() if r["reportId"] == "R017")
    assert fixed_after_report(q, rep["reportedAt"])


def test_rule_triage_short_circuits_only_on_empty_or_gibberish():
    reps = {r["reportId"]: triage_report(r["comment"]) for r in load_reports()}
    assert reps["R041"]["short_circuit"] and reps["R041"]["hint"] == "gibberish"
    sc = [k for k, v in reps.items() if v["short_circuit"]]
    assert sc == ["R041"], sc          # nothing real is dropped without an LLM look
    assert reps["R039"]["hint"] == "language_request"
    assert reps["R038"]["hint"] == "app_behaviour"
    assert reps["R037"]["hint"] == "display" and reps["R036"]["hint"] == "display"


def test_duplicates_grouping():
    g = duplicates(load_reports())
    assert sorted(g["60f744b3f1b9bc7cc7b4a949"]) == ["R001", "R002", "R003"]
    assert len(g) == 39


def test_parse_options_handles_assertion_reason():
    opts = parse_options(payload(Q["695ee00b88d7701e561aabc5"])["question"])
    assert sorted(opts) == list("ABCD") and opts["D"].startswith("A is false")


def test_gold_scores_itself_perfectly():
    results = [dict(g, confidence=0.95, evidence="", student_reply="") for g in GOLD]
    m = evaluate(results, GOLD)
    assert m["verdict_acc"] == m["route_acc"] == m["edit_match_on_editable"] == 1.0
    assert m["auto_apply_precision"] == 1.0 and not m["autos_not_exact"]
