"""Faculty review console. Loads precomputed engine output; makes no model calls.
  .venv/bin/uvicorn console.app:app --reload        # http://127.0.0.1:8000"""
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.common import parse_options, payload
from engine.guards import TAX

from .diff import edit_views
from .store import ActionError, Store

HERE = Path(__file__).resolve().parent
app = FastAPI(title="Acadza report review")
store = Store()


class Action(BaseModel):
    action: str
    actor: str = "faculty"
    edit: object = None           # dict or list of edits, only when faculty changed the fix
    replies: dict | None = None   # {reportId: text}
    note: str = ""


def _decorate(state):
    """Attach diff views (computed against the record the edit applies to)."""
    for g in state["groups"]:
        live = store.live(g["questionId"])
        for it in g["items"]:
            base = it["before"] if it.get("before") is not None else live
            it["views"] = edit_views(base, it["edit"]) if it.get("edit") else []
            it["repair_label"] = TAX["repairs"].get(it.get("repair") or "", {}).get("label", "")
            it["repair_fields"] = TAX["repairs"].get(it.get("repair") or "", {}).get("fields", [])
            it.pop("before", None); it.pop("after", None)
    return state


@app.get("/api/state")
def state():
    return _decorate(store.state())


@app.get("/api/question/{qid}")
def question(qid):
    try:
        d = store.question(qid)
    except Exception:
        raise HTTPException(404, "no such question")
    live = d["live"]
    d["render"] = dict(type=live["questionType"], question=payload(live)["question"], solution=payload(live)["solution"],
                       answer=payload(live)["answer"],
                       options=parse_options(payload(live)["question"]) if live["questionType"] != "integerQuestion" else {})
    d.pop("original"); d.pop("live")
    return d


@app.post("/api/item/{iid}")
def act(iid: str, a: Action):
    try:
        store.act(iid, a.action, actor=(a.actor or "faculty")[:40], edit=a.edit, replies=a.replies, note=(a.note or "")[:300])
    except ActionError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    return {"ok": True}


@app.get("/api/audit")
def audit():
    return store.audit_log()


@app.post("/api/reset")
def reset():
    if os.environ.get("CONSOLE_ALLOW_RESET", "1") != "1":
        raise HTTPException(403, "reset disabled")
    store.seed()
    return {"ok": True}


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")
