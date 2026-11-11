"""Console state in SQLite: seeded from the engine's precomputed output, then changed only by
faculty actions, every one of which writes an audit row.

Data source, in order: engine_out/report_rows.json (full rows) > results.json > DEMO rows built
from eval/gold.json (flagged demo=True; the UI shows a banner). The DB lives at CONSOLE_DB
(default console/console.db). On Render's free tier the disk is ephemeral, so the audit log
resets on every redeploy or restart; that is documented, not hidden."""
import copy
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from engine.common import load_questions, load_reports, payload
from engine.guards import TAX, apply_edit, decide_route, edit_list, validate_edit

ROOT = Path(__file__).resolve().parent.parent

# Why this order: a wrong key or stem costs every student marks and teaches a wrong method;
# a wrong solution teaches a wrong method; cosmetic issues cost seconds.
SEVERITY = {
    "answer_key": 5, "type_mismatch": 5, "unsound": 5, "wrong_question": 5,
    "stem_data": 4, "option_repair": 4, "diagram": 4, "identical_options": 4,
    "solution_fix": 3, "solution_missing": 3, "notation": 3,
    "answer_form": 2, "markup_leak": 2, "wrong_chapter": 2,
    "syllabus": 1, "spelling": 1, "noise": 1, "exam_reference": 1,
}
LANES = ["needs_you", "found", "auto", "no_change", "done"]

FOLLOW_UP = {
    "approved_edit": "Update: a teacher checked your report and the question has now been corrected. Thank you for flagging it.",
    "approved_no_edit": "Update: a teacher confirmed the problem you reported and it has been sent for correction. Thank you.",
    "rejected": "Update: a teacher checked this question carefully and it is correct as it stands. Thanks for looking closely.",
    "undone": "Update: a teacher is re-checking this question, so the earlier change is on hold for now.",
}


class ActionError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# --- seed data -------------------------------------------------------------------------------
def demo_rows_from_gold():
    """Hand labels dressed as rows so the console can be built before the engine run.
    Never shown without the DEMO banner."""
    from engine.run import template_reply
    Q = load_questions()
    out = []
    for g in json.loads((ROOT / "eval/gold.json").read_text(encoding="utf-8")):
        route, reasons = decide_route(g["verdict"], g["repair"], g["edit"], Q[g["questionId"]], 0.95,
                                      key_checks_agree=True, solution_checks_agree=True)
        out.append(dict(reportId=g["reportId"], questionId=g["questionId"], verdict=g["verdict"],
                        repair=g["repair"], edit=g["edit"], route=route, confidence=0.95,
                        evidence="(demo) " + (g.get("note") or "hand label"),
                        student_reply=template_reply(g["verdict"], route), _reasons=reasons, _suggestion=""))
    return out


def load_source(root=ROOT):
    eo = root / "engine_out"
    if (eo / "report_rows.json").exists():
        rows, demo, src = json.loads((eo / "report_rows.json").read_text(encoding="utf-8")), False, "engine_out/report_rows.json"
    elif (root / "results.json").exists():
        rows, demo, src = json.loads((root / "results.json").read_text(encoding="utf-8")), False, "results.json"
    else:
        rows, demo, src = demo_rows_from_gold(), True, "DEMO (eval/gold.json hand labels)"
    unreported = []
    if not demo and (eo / "unreported_findings.json").exists():
        unreported = json.loads((eo / "unreported_findings.json").read_text(encoding="utf-8"))
    audits = {}
    if not demo and (eo / "audits.json").exists():
        for a in json.loads((eo / "audits.json").read_text(encoding="utf-8")):
            audits[a["questionId"]] = dict(solver_a=a.get("solver_a"), solver_b=a.get("solver_b"),
                                           solution_concludes=a.get("solution_concludes"),
                                           normalised=a.get("normalised"), models=a.get("models"))
    return rows, unreported, audits, demo, src


# --- store -------------------------------------------------------------------------------------
class Store:
    def __init__(self, db_path=None, root=ROOT):
        self.root = root
        self.db_path = db_path or os.environ.get("CONSOLE_DB", str(ROOT / "console" / "console.db"))
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, qid TEXT, data TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS questions (qid TEXT PRIMARY KEY, live TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, actor TEXT,"
                          " action TEXT, item TEXT, qid TEXT, reports TEXT, detail TEXT)")
        if self._kv("seeded") is None:
            self.seed()

    # kv / low level
    def _kv(self, k, v=None):
        if v is None:
            r = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
            return json.loads(r[0]) if r else None
        self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))

    def item(self, iid):
        r = self.conn.execute("SELECT data FROM items WHERE id=?", (iid,)).fetchone()
        if not r:
            raise ActionError("no such item %s" % iid, 404)
        return json.loads(r[0])

    def _save_item(self, it):
        self.conn.execute("INSERT OR REPLACE INTO items VALUES (?,?,?)", (it["id"], it["questionId"], json.dumps(it, ensure_ascii=False)))

    def live(self, qid):
        return json.loads(self.conn.execute("SELECT live FROM questions WHERE qid=?", (qid,)).fetchone()[0])

    def _set_live(self, qid, q):
        self.conn.execute("INSERT OR REPLACE INTO questions VALUES (?,?)", (qid, json.dumps(q, ensure_ascii=False)))

    def _audit(self, actor, action, it, detail):
        self.conn.execute("INSERT INTO audit (at, actor, action, item, qid, reports, detail) VALUES (?,?,?,?,?,?,?)",
                          (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), actor or "faculty", action, it["id"],
                           it["questionId"], json.dumps(it["reportIds"]), json.dumps(detail, ensure_ascii=False)))

    # seeding
    def seed(self):
        with self.lock:
            for t in ("kv", "items", "questions", "audit"):
                self.conn.execute("DELETE FROM %s" % t)
            rows, unreported, audits, demo, src = load_source(self.root)
            Q = load_questions()
            reports = {r["reportId"]: r for r in load_reports()}
            for qid, q in Q.items():
                self._set_live(qid, q)
            groups = {}
            for r in rows:
                if r["verdict"] == "defect":
                    key = (r["questionId"], "d%s" % r["_defect_id"]) if r.get("_defect_id") is not None else \
                          (r["questionId"], r.get("repair"), json.dumps(r.get("edit"), sort_keys=True))
                else:
                    key = (r["questionId"], "info", r["reportId"])
                groups.setdefault(key, []).append(r)
            n = 0
            for key, rs in sorted(groups.items(), key=lambda kv: min(x["reportId"] for x in kv[1])):
                n += 1
                f = rs[0]
                cases = []
                for r in sorted(rs, key=lambda r: r["reportId"]):
                    rep = reports.get(r["reportId"], {})
                    cases.append(dict(reportId=r["reportId"], studentId=rep.get("studentId"), comment=rep.get("comment", ""),
                                      reportedAt=rep.get("reportedAt"), source=rep.get("source"), verdict=r["verdict"],
                                      evidence=r.get("evidence", ""), confidence=r.get("confidence", 0),
                                      reply_now=r.get("student_reply", ""), reply_follow=None))
                it = dict(id="I%03d" % n, questionId=f["questionId"], source="report", verdict=f["verdict"],
                          repair=f.get("repair"), edit=f.get("edit"), ai_route=f["route"],
                          confidence=max(c["confidence"] or 0 for c in cases), evidence=f.get("evidence", ""),
                          reasons=f.get("_reasons") or [], suggestion=f.get("_suggestion") or "",
                          reportIds=[c["reportId"] for c in cases], cases=cases, checks=audits.get(f["questionId"]),
                          status="info" if f["verdict"] != "defect" else
                                 ("auto_applied" if f["route"] == "auto_apply" else "pending"),
                          before=None, after=None, history=[])
                self._save_item(it)
            for d in unreported:
                n += 1
                # Unreported findings never auto-apply: nobody corroborated them, a human looks first.
                it = dict(id="I%03d" % n, questionId=d["questionId"], source="audit", verdict="defect", repair=d.get("repair"),
                          edit=d.get("edit"), ai_route="faculty_review", confidence=d.get("confidence", 0),
                          evidence=d.get("evidence", ""), reasons=(d.get("reasons") or []) + ["found by the audit; no student reported it"],
                          suggestion=d.get("suggestion", ""), reportIds=[], cases=[], checks=audits.get(d["questionId"]),
                          status="pending", before=None, after=None, history=[])
                self._save_item(it)
            # apply the engine's auto fixes to the live copy, in item order
            for it in self.items():
                if it["status"] == "auto_applied":
                    q = self.live(it["questionId"])
                    viol = validate_edit(q, it["repair"], it["edit"])
                    if viol:                                  # should not happen; fail closed
                        it["status"], it["reasons"] = "pending", it["reasons"] + ["seed: " + v for v in viol]
                    else:
                        new = apply_edit(q, it["edit"])
                        it["before"], it["after"] = q, new
                        self._set_live(it["questionId"], new)
                    self._save_item(it)
            self._kv("seeded", True)
            self._kv("demo", demo)
            self._kv("source", src)
            self.conn.commit()

    def items(self):
        return [json.loads(r[0]) for r in self.conn.execute("SELECT data FROM items ORDER BY id")]

    # priority and lanes
    @staticmethod
    def lane(it):
        s = it["status"]
        if s == "pending":
            return "needs_you" if it["source"] == "report" else "found"
        if s == "auto_applied":
            return "auto"
        if s == "info":
            return "no_change"
        return "done"

    @staticmethod
    def priority(it):
        reporters = len({c["studentId"] for c in it["cases"]})
        sev = SEVERITY.get(it.get("repair"), 0) if it["verdict"] == "defect" else 0
        return round(max(reporters, 1 if it["source"] == "audit" else 0) * sev * float(it["confidence"] or 0), 2), reporters, sev

    def state(self):
        items = self.items()
        groups = {}
        for it in items:
            p, reporters, sev = self.priority(it)
            it.update(lane=self.lane(it), priority=p, reporters=reporters, severity=sev)
            groups.setdefault(it["questionId"], []).append(it)
        Q = load_questions()
        out = []
        for qid, its in groups.items():
            lane_rank = min(LANES.index(i["lane"]) for i in its)
            active = [i for i in its if LANES.index(i["lane"]) == lane_rank]
            oldest = min((c["reportedAt"] or "9" for i in its for c in i["cases"]), default="9")
            q = Q[qid]
            out.append(dict(questionId=qid, subject=q["subject"], chapter=q["chapter"], type=q["questionType"],
                            lane=LANES[lane_rank], priority=max(i["priority"] for i in active),
                            reporters=len({c["studentId"] for i in its for c in i["cases"]}),
                            oldest=oldest, items=its))
        out.sort(key=lambda g: (LANES.index(g["lane"]), -g["priority"], g["oldest"]))
        return dict(demo=self._kv("demo"), source=self._kv("source"), groups=out, severity=SEVERITY, follow_up=FOLLOW_UP,
                    counts={l: sum(g["lane"] == l for g in out) for l in LANES})

    def question(self, qid):
        orig = load_questions()[qid]
        return dict(original=orig, live=self.live(qid), audit=self.audit_log(qid))

    def audit_log(self, qid=None):
        sql = "SELECT at, actor, action, item, qid, reports, detail FROM audit"
        rows = self.conn.execute(sql + (" WHERE qid=? ORDER BY id DESC" if qid else " ORDER BY id DESC"), (qid,) if qid else ()).fetchall()
        return [dict(at=a, actor=b, action=c, item=d, questionId=e, reportIds=json.loads(f), detail=json.loads(g))
                for a, b, c, d, e, f, g in rows]

    # actions
    def act(self, iid, action, actor="faculty", edit=None, replies=None, note=""):
        with self.lock:
            it = self.item(iid)
            q = self.live(it["questionId"])
            detail = dict(note=note)
            st = it["status"]
            if action == "approve":
                if st != "pending":
                    raise ActionError("only pending items can be approved (this one is %s)" % st, 409)
                use = edit if edit is not None else it["edit"]
                if edit is not None:
                    detail["edited_by_faculty"] = True
                    detail["ai_edit"] = it["edit"]
                if edit_list(use):
                    viol = validate_edit(q, it["repair"], use)
                    if viol:
                        raise ActionError("edit rejected by guards: " + "; ".join(viol), 422)
                    new = apply_edit(q, use)
                    it["before"], it["after"], it["edit"] = q, new, use
                    self._set_live(it["questionId"], new)
                    detail["applied"] = use
                    follow = FOLLOW_UP["approved_edit"]
                else:
                    follow = FOLLOW_UP["approved_no_edit"]
                it["status"] = "approved"
                self._set_follow(it, follow, replies)
            elif action == "reject":
                if st != "pending":
                    raise ActionError("only pending items can be rejected; use undo for auto-applied fixes", 409)
                it["status"] = "rejected"
                self._set_follow(it, FOLLOW_UP["rejected"], replies)
            elif action == "keep":
                if st != "auto_applied":
                    raise ActionError("only auto-applied items can be kept", 409)
                it["status"] = "kept"
            elif action == "undo":
                if st not in ("auto_applied", "approved") or it["after"] is None:
                    raise ActionError("nothing to undo on this item", 409)
                if q != it["after"]:
                    raise ActionError("the question changed after this fix; undo the later change first", 409)
                self._set_live(it["questionId"], it["before"])
                detail["reverted"] = it["edit"]
                it["before"] = it["after"] = None
                it["status"] = "pending"
                it["reasons"] = ["undone by %s%s" % (actor or "faculty", ": " + note if note else "")] + it["reasons"]
                self._set_follow(it, FOLLOW_UP["undone"], replies)
            elif action == "reply":
                if not replies:
                    raise ActionError("no reply text given")
                self._set_follow(it, None, replies, only_given=True)
            else:
                raise ActionError("unknown action %r" % action)
            if replies:
                detail["replies_edited"] = replies
            it["history"].append(dict(action=action, actor=actor, note=note))
            self._save_item(it)
            self._audit(actor, action, it, detail)
            self.conn.commit()
            return it

    @staticmethod
    def _set_follow(it, default, replies, only_given=False):
        replies = replies or {}
        for c in it["cases"]:
            if c["reportId"] in replies:
                txt = (replies[c["reportId"]] or "").strip()
                if len(txt) > 600:
                    raise ActionError("reply too long for %s" % c["reportId"])
                c["reply_follow"] = txt
            elif not only_given:
                c["reply_follow"] = default
