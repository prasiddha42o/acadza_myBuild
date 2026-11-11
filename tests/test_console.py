"""Stage 3: console store, priority, actions, audit trail, diff views. Uses demo (gold) data
in a temp DB with a root that has no engine output, so it never reads real results."""
import json
import shutil
from pathlib import Path

import pytest

from console.diff import edit_views, mark
from console.store import FOLLOW_UP, LANES, Store
from engine.common import load_questions, payload

ROOT = Path(__file__).resolve().parent.parent
Q = load_questions()


@pytest.fixture()
def store(tmp_path):
    root = tmp_path / "repo"
    (root / "eval").mkdir(parents=True)
    shutil.copy(ROOT / "eval/gold.json", root / "eval/gold.json")
    return Store(db_path=str(tmp_path / "c.db"), root=root)


def find_item(store, rid):
    return next(i for i in store.items() if rid in i["reportIds"])


def test_demo_is_flagged_and_everything_is_seeded(store):
    st = store.state()
    assert st["demo"] is True
    assert sum(len(i["reportIds"]) for g in st["groups"] for i in g["items"]) == 44


def test_queue_order_needs_you_first_then_priority(store):
    gs = store.state()["groups"]
    lanes = [LANES.index(g["lane"]) for g in gs]
    assert lanes == sorted(lanes)
    needs = [g for g in gs if g["lane"] == "needs_you"]
    assert [g["priority"] for g in needs] == sorted([g["priority"] for g in needs], reverse=True)
    assert gs[0]["lane"] == "needs_you"


def test_duplicate_reports_are_one_fix(store):
    it = find_item(store, "R001")
    assert set(it["reportIds"]) == {"R001", "R002", "R003"}
    assert store.priority(it)[1] == 3


def test_auto_fixes_are_live_and_undo_restores(store):
    it = find_item(store, "R001")
    assert it["status"] == "auto_applied" and payload(store.live(it["questionId"]))["answer"] == "A"
    store.act(it["id"], "undo", actor="dr. k", note="re-check")
    assert payload(store.live(it["questionId"]))["answer"] == "C"
    it = store.item(it["id"])
    assert it["status"] == "pending" and all(c["reply_follow"] == FOLLOW_UP["undone"] for c in it["cases"])
    log = store.audit_log(it["questionId"])
    assert log[0]["action"] == "undo" and log[0]["actor"] == "dr. k" and log[0]["detail"]["note"] == "re-check"


def test_approve_applies_once_and_logs(store):
    it = find_item(store, "R011")           # stricter than gold -> pending
    assert it["status"] == "pending"
    store.act(it["id"], "approve", actor="t")
    assert "1 : 1" in payload(store.live(it["questionId"]))["question"]
    with pytest.raises(Exception):
        store.act(it["id"], "approve")      # cannot approve twice
    assert [a["action"] for a in store.audit_log(it["questionId"])] == ["approve"]


def test_faculty_edit_still_goes_through_guards(store):
    it = find_item(store, "R011")
    bad = {"field": "answer", "find": "C", "replace": "A"}       # option_repair may not touch answer
    with pytest.raises(Exception) as e:
        store.act(it["id"], "approve", edit=bad)
    assert "guards" in str(e.value)
    assert store.item(it["id"])["status"] == "pending"
    assert store.audit_log(it["questionId"]) == []


def test_reject_changes_nothing_and_sets_follow_up(store):
    it = find_item(store, "R015")           # identical_options, human-only
    q_before = store.live(it["questionId"])
    store.act(it["id"], "reject", replies={"R015": "Custom reply."})
    assert store.live(it["questionId"]) == q_before
    assert store.item(it["id"])["cases"][0]["reply_follow"] == "Custom reply."


def test_undo_refuses_when_question_changed_later(store):
    a = find_item(store, "R001")
    q = store.live(a["questionId"])
    q2 = json.loads(json.dumps(q)); payload(q2)["solution"] += " "
    store._set_live(a["questionId"], q2)
    with pytest.raises(Exception) as e:
        store.act(a["id"], "undo")
    assert "changed" in str(e.value)


def test_reset_restores_seed(store):
    it = find_item(store, "R001")
    store.act(it["id"], "undo")
    store.seed()
    assert find_item(store, "R001")["status"] == "auto_applied" and store.audit_log() == []


# --- diff views --------------------------------------------------------------------------------
def test_answer_view_has_option_chips():
    q = next(v for k, v in Q.items() if k.endswith("b4a949"))
    v = edit_views(q, {"field": "answer", "find": "C", "replace": "A"})[0]
    assert v["kind"] == "answer" and v["old_letters"] == ["C"] and v["new_letters"] == ["A"]
    assert [o["letter"] for o in v["options"]] == list("ABCD")


def test_text_view_marks_span():
    q = next(v for k, v in Q.items() if k.endswith("84bd22"))
    v = edit_views(q, {"field": "question", "find": "Epithellial tisue", "replace": "Epithelial tissue"})[0]
    assert '<mark class="del">Epithellial tisue</mark>' in v["before"]
    assert '<mark class="ins">Epithelial tissue</mark>' in v["after"]


def test_mark_never_nests_inside_math():
    html = "x <math><mn>40</mn></math> y"
    s = html.find("40")
    out, how = mark(html, s, s + 2, "del")
    assert how == "math" and out.startswith('x <mark class="del"><math>')


def test_mark_skips_unbalanced_region():
    html = "<b>ab</b>cd"
    out, how = mark(html, 1, 5, "del")       # 'b>ab' crosses a tag
    assert how == "none" and out == html


# --- API smoke --------------------------------------------------------------------------------
def test_api_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DB", str(tmp_path / "api.db"))
    import importlib
    import console.app as appmod
    importlib.reload(appmod)
    from fastapi.testclient import TestClient
    c = TestClient(appmod.app)
    st = c.get("/api/state").json()
    assert st["groups"] and "views" in st["groups"][0]["items"][0]
    assert c.get("/").status_code == 200 and c.get("/static/vendor/purify.min.js").status_code == 200
    qid = st["groups"][0]["questionId"]
    assert "question" in c.get("/api/question/" + qid).json()["render"]
    r = c.post("/api/item/I999", json={"action": "approve"})
    assert r.status_code == 404
