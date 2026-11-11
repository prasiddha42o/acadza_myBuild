"use strict";
// Faculty console. Everything a student or the AI wrote is inserted as text; only question
// HTML is inserted as HTML, after DOMPurify with the MathML profile.

const LANE = {
  needs_you: ["Needs you", "var(--lane-needs)"],
  found: ["Found by audit, not reported", "var(--lane-found)"],
  auto: ["Auto-applied: check or undo", "var(--lane-auto)"],
  no_change: ["No change needed", "var(--lane-none)"],
  done: ["Done", "var(--lane-done)"],
};
const BADGE = { needs_you: "Needs you", found: "Found by audit", auto: "Auto-applied", no_change: "No change", done: "Done" };
const REPAIR = {
  answer_key: "Answer key wrong", type_mismatch: "Stored as the wrong type", unsound: "No valid answer as written",
  wrong_question: "Solution belongs to another question", stem_data: "Value missing from the question",
  option_repair: "Option missing or blank", diagram: "Figure missing", identical_options: "Two options are identical",
  solution_fix: "Solution is wrong", solution_missing: "Solution missing", notation: "Notation lost",
  answer_form: "Unit or answer form not stated", markup_leak: "Raw code visible", wrong_chapter: "Filed under the wrong chapter",
  syllabus: "Claimed out of syllabus", spelling: "Spelling", noise: "Stray text", exam_reference: "Exam tag in the question",
};
const VERDICT = {
  defect: "Defect", no_error: "No error: the student's claim was checked and is wrong",
  display_issue: "Display issue: content is fine", app_issue: "App issue, not the question",
  language_mode: "Language request", already_fixed: "Already fixed after the report", not_actionable: "Nothing to act on",
};

const S = { st: null, sel: null, q: {}, editing: null, drafts: {} };
const $ = (s) => document.querySelector(s);

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return e;
}
function safeHTML(html) {
  const d = el("div", { class: "qhtml" });
  d.innerHTML = DOMPurify.sanitize(html || "", { USE_PROFILES: { html: true, mathMl: true } });
  return d;
}
function toast(msg, err) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
  clearTimeout(toast.h); toast.h = setTimeout(() => (t.hidden = true), err ? 6000 : 2200);
}
const actor = () => ($("#actor").value || "faculty").trim();
const fmtDate = (s) => s ? new Date(s).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

// ---- plain-language reasons -------------------------------------------------------------
function reasonText(r) {
  let m;
  if ((m = r.match(/^(\w+) needs a human by policy/))) return `${REPAIR[m[1]] || m[1]}: a teacher always decides`;
  if (r.includes("independent solves")) return "Solvers and the stored solution don't all agree on the new key";
  if (r.startsWith("biology")) return "Biology key change: nothing to compute, a teacher decides";
  if ((m = r.match(/^confidence ([\d.]+)/))) return `AI confidence ${Math.round(+m[1] * 100)}% is below 90%`;
  if (r.startsWith("find text not found") || r.includes("find must match")) return "The AI's edit didn't match the stored text (blocked)";
  if (r.includes("inserted value")) return "The value added to the question isn't in the stored solution";
  if (r.includes("solution edit needs")) return "Rewritten solution not verified to end at the key";
  if (r === "no edit proposed") return "No concrete fix proposed";
  if (r.includes("found by the audit")) return "Nobody reported this; the audit found it";
  if (r.includes("outside scope") || r.includes("changed numbers") || r.includes("touches stem")) return "Edit broke a safety rule (blocked): " + r;
  return r;
}

function headline(it) {
  if (it.verdict !== "defect") return VERDICT[it.verdict] || it.verdict;
  let h = REPAIR[it.repair] || it.repair || "Defect (repair unknown)";
  const a = (it.views || []).find((v) => v.kind === "answer");
  if (a) h += ` · ${fmtKey(a.old)} → ${fmtKey(a.new)}`;
  const t = (it.views || []).find((v) => v.kind === "type");
  if (t) h += ` · ${t.old} → ${t.new}`;
  return h;
}
// lists shown as stored (JSON) so a malformed key like ["A, B, D"] is visibly different from ["A","B","D"]
const fmtKey = (k) => Array.isArray(k) ? JSON.stringify(k) : (k === "" ? "∅" : String(k));

// ---- data -------------------------------------------------------------------------------
async function load(keepSel = true) {
  const r = await fetch("/api/state");
  S.st = await r.json();
  $("#demo").hidden = !S.st.demo;
  const ids = S.st.groups.map((g) => g.questionId);
  const fromHash = (location.hash.match(/q=([0-9a-f]+)/) || [])[1];
  if (!keepSel && fromHash && ids.includes(fromHash)) S.sel = fromHash;
  else if (!keepSel || !ids.includes(S.sel)) S.sel = ids[0] || null;
  renderQueue(); renderProgress(); await renderDetail();
}
async function getQ(qid, fresh) {
  if (fresh || !S.q[qid]) S.q[qid] = await (await fetch("/api/question/" + qid)).json();
  return S.q[qid];
}
const group = (qid) => S.st.groups.find((g) => g.questionId === qid);

async function act(it, action, extra = {}) {
  const body = { action, actor: actor(), note: (document.getElementById("note-" + it.id) || {}).value || "", ...extra };
  const d = S.drafts[it.id]; if (d && Object.keys(d).length) body.replies = d;
  const r = await fetch("/api/item/" + it.id, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok) { toast(j.error || "Action failed", true); return false; }
  delete S.drafts[it.id]; S.editing = null; delete S.q[it.questionId];
  toast({ approve: "Approved", reject: "Rejected", keep: "Kept", undo: "Undone: back in your queue", reply: "Reply saved" }[action] || "Saved");
  const before = S.sel;
  await load(true);
  const g = group(before);
  const open = g && g.items.some((i) => ["needs_you", "found", "auto"].includes(i.lane));
  if (!open && ["approve", "reject", "keep"].includes(action)) next(1, true);
  return true;
}

// ---- queue ------------------------------------------------------------------------------
function renderProgress() {
  const c = S.st.counts;
  $("#progress").replaceChildren(
    el("b", {}, c.needs_you), " need you · ", el("b", {}, c.found), " found by audit · ",
    el("b", {}, c.auto), " auto-applied to check · ", el("b", {}, c.done), " done");
}
function renderQueue() {
  const q = $("#queue"); q.replaceChildren();
  for (const lane of Object.keys(LANE)) {
    const gs = S.st.groups.filter((g) => g.lane === lane);
    if (!gs.length) continue;
    const box = el("section", { class: "lane-" + lane },
      el("div", { class: "lane-h" }, el("span", { class: "dot", style: "background:" + LANE[lane][1] }), LANE[lane][0], el("span", { class: "n" }, gs.length)));
    for (const g of gs) {
      const top = g.items.filter((i) => i.lane === g.lane).sort((a, b) => b.priority - a.priority)[0];
      const why = top && ["needs_you", "found"].includes(g.lane) && top.reasons.length ? reasonText(top.reasons[0]) : "";
      box.append(el("button", {
        class: "qrow" + (g.questionId === S.sel ? " sel" : ""), "data-q": g.questionId,
        title: `priority ${g.priority} = distinct reporters × severity × confidence`,
        onclick: () => { S.sel = g.questionId; S.editing = null; renderQueue(); renderDetail(); $("#detail").focus(); },
      },
        el("div", { class: "t" }, top ? headline(top) : "", el("span", { class: "pr" }, g.priority ? g.priority.toFixed(1) : "")),
        el("div", { class: "s" }, `${g.subject} · ${g.chapter} · ${g.reporters} student${g.reporters === 1 ? "" : "s"}`),
        why ? el("div", { class: "why" }, why) : null));
    }
    q.append(box);
  }
}
function next(dir, openOnly) {
  const gs = S.st.groups.filter((g) => !openOnly || ["needs_you", "found", "auto"].includes(g.lane));
  if (!gs.length) { renderQueue(); renderDetail(); return; }
  let i = gs.findIndex((g) => g.questionId === S.sel);
  i = i === -1 ? 0 : Math.min(Math.max(i + dir, 0), gs.length - 1);
  S.sel = gs[i].questionId; S.editing = null;
  renderQueue(); renderDetail();
  const row = document.querySelector(`.qrow[data-q="${S.sel}"]`); if (row) row.scrollIntoView({ block: "nearest" });
}

// ---- detail -----------------------------------------------------------------------------
async function renderDetail() {
  const d = $("#detail");
  if (S.sel) history.replaceState(null, "", "#q=" + S.sel);
  const g = S.sel && group(S.sel);
  if (!g) { d.replaceChildren(el("div", { class: "empty" }, "Nothing here. Queue is clear.")); return; }
  const qd = await getQ(g.questionId);
  const order = ["needs_you", "found", "auto", "done", "no_change"];
  const items = [...g.items].sort((a, b) => order.indexOf(a.lane) - order.indexOf(b.lane) || b.priority - a.priority);
  d.replaceChildren(
    el("div", { class: "qhead" }, el("h2", {}, `${g.subject} · ${g.chapter}`),
      el("span", { class: "meta" }, `${g.type} · ${g.questionId} · ${g.reporters} student${g.reporters === 1 ? "" : "s"} reported`)),
    ...items.map((it) => card(it, qd)),
    el("details", { class: "card", open: ["needs_you", "found"].includes(g.lane) }, el("summary", {}, "Question and solution as students see them now"),
      el("div", { class: "cap" }, "Question"), safeHTML(qd.render.question),
      el("div", { class: "cap" }, "Answer key: " + fmtKey(qd.render.answer)),
      el("div", { class: "cap" }, "Solution"), safeHTML(qd.render.solution)),
    el("details", { class: "card" }, el("summary", {}, `History for this question (${qd.audit.length})`),
      el("div", { class: "timeline" }, qd.audit.length ? qd.audit.map(auditLine) : "No actions yet.")));
}
function auditLine(a) {
  const det = a.detail || {};
  return el("div", {}, el("b", {}, a.action), ` by ${a.actor} · ${fmtDate(a.at)} · ${a.item}`,
    a.reportIds.length ? ` · ${a.reportIds.join(", ")}` : "",
    det.edited_by_faculty ? " · edited the AI's fix" : "", det.note ? ` · “${det.note}”` : "");
}

function card(it, qd) {
  const pct = Math.round((it.confidence || 0) * 100);
  const c = el("article", { class: "card lane-" + it.lane },
    el("div", { class: "headline" }, el("span", { class: "badge b-" + it.lane }, BADGE[it.lane]), el("h3", {}, headline(it)),
      el("span", { class: "conf", title: `priority ${it.priority} = ${it.reporters} reporter(s) × severity ${it.severity} × confidence ${it.confidence}` }, `${pct}% confident`)));
  if (["needs_you", "found"].includes(it.lane) && it.reasons.length)
    c.append(el("div", { class: "reasons" }, el("span", { class: "lbl" }, "Needs you because:"), it.reasons.map((r) => el("span", { class: "chip-reason" }, reasonText(r)))));
  for (const v of it.views || []) c.append(view(v));
  if (!it.views.length && it.verdict === "defect") c.append(el("div", { class: "suggest" }, "No edit proposed: decide from the question below. ", el("span", { class: "muted" }, it.repair_label)));
  if (it.suggestion) c.append(el("div", { class: "suggest" }, el("b", {}, "Suggested: "), it.suggestion));
  if (it.evidence) c.append(el("div", { class: "evidence" }, it.evidence));
  if (it.checks && it.checks.normalised) c.append(checks(it));
  if (it.cases.length) {
    const same = it.cases.length > 1 && it.cases.every((x) => x.reply_now === it.cases[0].reply_now && x.reply_follow === it.cases[0].reply_follow);
    const box = el("div", { class: "reports" }, it.cases.map((cs) => reportRow(it, cs, !same)));
    if (same) box.append(replyBlock(it, it.cases, `All ${it.cases.length} students were told`));
    c.append(box);
  }
  if (S.editing === it.id) c.append(editor(it, qd));
  c.append(actions(it));
  return c;
}

function view(v) {
  if (v.kind === "answer") {
    const box = el("div", {});
    if (v.qtype === "integerQuestion" || !v.options.length) {
      box.append(el("div", { class: "keytext" }, el("span", { class: "d" }, fmtKey(v.old)), "→", el("span", { class: "i" }, fmtKey(v.new))));
    } else {
      box.append(el("div", { class: "keyrow" }, v.options.map((o) => {
        const was = v.old_letters.includes(o.letter), now = v.new_letters.includes(o.letter);
        const cls = "opt" + (was && now ? " both" : was ? " old" : now ? " new" : "");
        return el("div", { class: cls }, el("span", { class: "tag" }, was && now ? "key, kept" : was ? "old key" : now ? "new key" : ""),
          el("span", { class: "l" }, o.letter), o.text || "(blank)");
      })));
    }
    return box;
  }
  if (v.kind === "type") return el("div", { class: "keytext" }, "Type ", el("span", { class: "d" }, v.old), "→", el("span", { class: "i" }, v.new));
  if (v.kind === "missing") return el("div", { class: "suggest" }, "This edit no longer matches the live text.");
  const strip = el("div", { class: "strip" }, el("span", { class: "f" }, v.field + ":"),
    el("span", { class: "d" }, v.strip_del), " → ", el("span", { class: "i" }, v.strip_ins));
  return el("div", {}, strip, el("div", { class: "ba" },
    el("div", {}, el("div", { class: "cap" }, "Before"), safeHTML(v.before)),
    el("div", {}, el("div", { class: "cap" }, v.kind === "rewrite" ? "After (whole field rewritten)" : "After"), safeHTML(v.after))));
}

function checks(it) {
  const n = it.checks.normalised;
  const a = (it.views || []).find((v) => v.kind === "answer");
  const target = a ? a.new_letters.join(",") || String(a.new) : null;
  const cell = (k, v) => [el("span", { class: "k" }, k),
    el("span", { class: "v" + (target && v !== "none" ? (v === target || +v === +target ? " agree" : " disagree") : "") }, v)];
  return el("details", { open: !!a }, el("summary", {}, "Independent checks"),
    el("div", { class: "checks" }, cell("Solver A", n.a), cell("Solver B", n.b), cell("Stored solution", n.concl), cell("Current key", n.key)));
}

function reportRow(it, cs, withReply) {
  const box = el("div", { class: "rep" },
    el("span", { class: "id" }, cs.reportId), el("span", { class: "c" }, "“" + (cs.comment || "(empty)") + "”"),
    el("span", { class: "m" }, [cs.source, fmtDate(cs.reportedAt), it.verdict === "defect" ? "" : VERDICT[cs.verdict] || cs.verdict].filter(Boolean).join(" · ")));
  if (withReply) box.append(...replyBlock(it, [cs], "Student was told").childNodes);
  return box;
}
// one reply preview for one or many reports; an edit applies to every report in `cases`
function replyBlock(it, cases, label) {
  const cs = cases[0], rids = cases.map((x) => x.reportId);
  const draft = (S.drafts[it.id] || {})[cs.reportId];
  const follow = followText(it, cs);
  const box = el("div", { class: "rep" }, el("span", {}), el("div", { class: "reply" }, el("span", { class: "w" }, label), cs.reply_now));
  const setDraft = (t) => { S.drafts[it.id] = S.drafts[it.id] || {}; rids.forEach((r) => (S.drafts[it.id][r] = t)); };
  if (follow) {
    const r = el("div", { class: "reply" }, el("span", { class: "w" }, follow.label + (rids.length > 1 ? ` (all ${rids.length})` : "")));
    if (draft !== undefined || S.editing === it.id) {
      r.append(el("textarea", { "aria-label": "Follow-up for " + rids.join(", "), oninput: (e) => setDraft(e.target.value) }, draft !== undefined ? draft : follow.text));
    } else {
      r.append(follow.text, " ", el("button", { class: "ghost", onclick: () => { setDraft(follow.text); renderDetail(); } }, "edit"));
    }
    box.append(r);
  }
  return box;
}
function followText(it, cs) {
  const F = S.st.follow_up || {};
  if (["approved", "rejected", "kept"].includes(it.status) || cs.reply_follow) return cs.reply_follow ? { label: "Follow-up sent", text: cs.reply_follow } : null;
  if (it.status === "pending") return { label: "Follow-up if you approve", text: it.edit ? F.approved_edit : F.approved_no_edit };
  if (it.status === "auto_applied") return { label: "Follow-up if you undo", text: F.undone };
  return null;
}

function actions(it) {
  const row = el("div", { class: "actions" });
  const note = el("input", { class: "note", id: "note-" + it.id, placeholder: "Note for the audit log (optional)", maxlength: 300 });
  if (it.status === "pending") {
    const canEdit = it.repair_fields.length > 0;
    row.append(
      el("button", { class: "primary", onclick: () => act(it, "approve") }, it.edit ? "Approve fix " : "Confirm, close ", el("kbd", {}, "A")),
      canEdit ? el("button", { onclick: () => { S.editing = S.editing === it.id ? null : it.id; renderDetail(); } }, S.editing === it.id ? "Cancel edit" : "Edit fix ", S.editing === it.id ? null : el("kbd", {}, "E")) : null,
      el("button", { class: "danger", onclick: () => act(it, "reject") }, "Reject ", el("kbd", {}, "R")), note);
  } else if (it.status === "auto_applied") {
    row.append(el("button", { class: "primary", onclick: () => act(it, "keep") }, "Looks right, keep"),
      el("button", { class: "danger", onclick: () => act(it, "undo") }, "Undo ", el("kbd", {}, "U")), note);
  } else if (it.status === "approved" && it.edit) {
    row.append(el("span", { class: "status-done" }, `Approved${lastBy(it)}.`), el("button", { class: "danger", onclick: () => act(it, "undo") }, "Undo"), note);
  } else if (["approved", "rejected", "kept"].includes(it.status)) {
    row.append(el("span", { class: "status-done" }, `${it.status[0].toUpperCase() + it.status.slice(1)}${lastBy(it)}.`));
  } else {
    row.append(el("span", { class: "status-done" }, "No change to the question. The student already got the reply above."));
  }
  if (S.drafts[it.id] && !["pending", "auto_applied"].includes(it.status))
    row.append(el("button", { onclick: () => act(it, "reply") }, "Save follow-up"));
  return row;
}
const lastBy = (it) => { const h = it.history[it.history.length - 1]; return h ? ` by ${h.actor}` : ""; };

// ---- editor -----------------------------------------------------------------------------
function editor(it, qd) {
  const box = el("div", { class: "editor" }, el("b", {}, "Edit the fix"), " · the same safety checks run when you save.");
  const edits = JSON.parse(JSON.stringify(Array.isArray(it.edit) ? it.edit : it.edit ? [it.edit] : []));
  const fields = it.repair_fields;
  const ans = edits.find((e) => e.field === "answer") || (fields.includes("answer") ? { field: "answer", find: qd.render.answer, replace: qd.render.answer } : null);
  if (ans && !edits.includes(ans)) edits.push(ans);
  const targetType = (edits.find((e) => e.field === "questionType") || {}).replace || qd.render.type;
  if (ans) {
    ans.find = qd.render.answer;                              // always the live key
    if (targetType === "integerQuestion") {
      box.append(el("label", {}, "New key (number)"), el("input", { value: ans.replace, oninput: (e) => (ans.replace = e.target.value.trim()) }));
    } else {
      let picked = new Set([].concat(ans.replace || []).join(",").split(/\s*,\s*/).filter((x) => /^[A-D]$/.test(x)));
      const row = el("div", { class: "keyrow" });
      const draw = () => row.replaceChildren(..."ABCD".split("").map((l) => el("button", { type: "button", class: "opt" + (picked.has(l) ? " pick" : ""), onclick: () => {
        if (targetType === "scq") picked = new Set([l]); else picked.has(l) ? picked.delete(l) : picked.add(l);
        ans.replace = targetType === "scq" ? l : [...picked].sort(); draw(); } },
        el("span", { class: "l" }, l), (qd.render.options[l] || "(blank)").slice(0, 60))));
      draw();
      box.append(el("label", {}, targetType === "scq" ? "Pick the correct option" : "Pick every correct option"), row);
    }
  }
  const textEdits = edits.filter((e) => e.field === "question" || e.field === "solution");
  const textFields = fields.filter((f) => f === "question" || f === "solution");
  if (!textEdits.length && textFields.length) { const e = { field: textFields[0], find: "", replace: "" }; edits.push(e); textEdits.push(e); }
  for (const e of textEdits) {
    box.append(
      el("label", {}, "Field"), el("select", { onchange: (x) => (e.field = x.target.value) }, textFields.map((f) => el("option", { value: f, selected: f === e.field }, f))),
      el("label", {}, "Find (exact stored text; must appear exactly once)"), el("textarea", { oninput: (x) => (e.find = x.target.value) }, e.find),
      el("label", {}, "Replace with"), el("textarea", { oninput: (x) => (e.replace = x.target.value) }, e.replace));
  }
  if (textFields.length) box.append(el("details", {}, el("summary", {}, "Stored HTML to copy from"),
    textFields.map((f) => el("textarea", { readonly: true, style: "min-height:120px" }, f === "question" ? qd.render.question : qd.render.solution))));
  box.append(el("div", { class: "actions" }, el("button", { class: "primary", onclick: () => {
    const clean = edits.filter((e) => e.field === "answer" || e.field === "questionType" || e.find);
    act(it, "approve", { edit: clean.length === 1 ? clean[0] : clean });
  } }, "Save and approve"), el("button", { onclick: () => { S.editing = null; renderDetail(); } }, "Cancel")));
  return box;
}

// ---- keyboard + header --------------------------------------------------------------------
document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey) { if (e.key === "Escape") e.target.blur(); return; }
  const g = S.sel && group(S.sel); if (!g) return;
  const open = g.items.filter((i) => i.status === "pending").sort((a, b) => b.priority - a.priority)[0];
  const auto = g.items.find((i) => i.status === "auto_applied");
  const k = e.key.toLowerCase();
  if (k === "j") next(1); else if (k === "k") next(-1);
  else if (k === "a" && open) act(open, "approve");
  else if (k === "r" && open) act(open, "reject");
  else if (k === "e" && open && open.repair_fields.length) { S.editing = open.id; renderDetail(); }
  else if (k === "u" && auto) act(auto, "undo");
  else if (k === "escape" && S.editing) { S.editing = null; renderDetail(); }
  else if (k === "?") $("#dlg-keys").showModal();
  else return;
  e.preventDefault();
});
$("#actor").value = localStorage.getItem("actor") || "";
$("#actor").addEventListener("change", (e) => localStorage.setItem("actor", e.target.value.trim()));
$("#btn-keys").onclick = () => $("#dlg-keys").showModal();
$("#btn-audit").onclick = async () => {
  const a = await (await fetch("/api/audit")).json();
  $("#audit-body").replaceChildren(...(a.length ? a.map((x) => el("div", { class: "timeline" }, auditLine(x), " · ", x.questionId)) : [el("p", {}, "No actions yet.")]));
  $("#dlg-audit").showModal();
};
$("#btn-reset").onclick = async () => {
  if (!confirm("Reset the console to the precomputed state? This clears the audit log.")) return;
  await fetch("/api/reset", { method: "POST" }); S.q = {}; S.drafts = {}; await load(false); toast("Reset");
};
load(false);
