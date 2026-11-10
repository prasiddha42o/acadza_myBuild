"""Shared helpers: data loading, HTML flattening, option parsing."""
import html
import json
import re
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"


def load_questions():
    return {q["_id"]: q for q in json.loads((DATA / "questions.json").read_text(encoding="utf-8"))}


def load_reports():
    return json.loads((DATA / "reports.json").read_text(encoding="utf-8"))


def load_taxonomy():
    return json.loads((DATA / "taxonomy.json").read_text(encoding="utf-8"))


def payload(q):
    """The type-specific object (scq / mcq / integerQuestion) holding the content."""
    return q[q["questionType"]]


def get_field(q, field):
    if field == "questionType":
        return q["questionType"]
    return payload(q)[field]


def flat(s):
    """HTML -> readable text (tags stripped, entities decoded, zero-width removed)."""
    s = re.sub(r"<br\s*/?>|</h\d>|</p>|</div>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\u200b", "")
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def parse_options(stem_html):
    """Return {letter: text} using the last increasing chain of (A)..(D) markers.
    The 'last chain' rule survives assertion-reason stems that contain '(A)' earlier."""
    t = flat(stem_html)
    pos = {}
    nxt = len(t) + 1
    for letter in "DCBA":
        hits = [m.start() for m in re.finditer(r"\(%s\)" % letter, t) if m.start() < nxt]
        if not hits:
            continue                      # a missing option must not hide the ones before it
        pos[letter] = hits[-1]
        nxt = hits[-1]
    letters = sorted(pos)
    out = {}
    for i, l in enumerate(letters):
        end = pos[letters[i + 1]] if i + 1 < len(letters) else len(t)
        out[l] = t[pos[l] + 3:end].strip()
    return out
