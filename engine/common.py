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


# --- Model-facing HTML ---------------------------------------------------------------
# Models read MathML directly (flat() loses exponents and fractions: r<sup>2</sup> -> "r 2").
# We only strip presentation noise: <span> wrappers and style/class/xmlns attributes.
# clean_map() keeps an index map so a `find` the model copies from the CLEAN text can be
# mapped back to the exact RAW substring the guards and apply_edit work on.
_NOISE = re.compile(r'</?span\b[^>]*>|\s(?:style|class|xmlns)="[^"]*"')
_SPAN = re.compile(r'</?span\b[^>]*>')


def clean_map(raw):
    """Return (clean_text, idx, removed): idx[i] = raw index of clean char i;
    removed = [(raw_start, raw_end, text)] of stripped pieces."""
    out, idx, removed, pos = [], [], [], 0
    for m in _NOISE.finditer(raw):
        for j in range(pos, m.start()):
            out.append(raw[j]); idx.append(j)
        removed.append((m.start(), m.end(), m.group(0)))
        pos = m.end()
    for j in range(pos, len(raw)):
        out.append(raw[j]); idx.append(j)
    return "".join(out), idx, removed


def clean_html(raw):
    return clean_map(raw)[0]


def map_find(raw, find_clean, replace_clean):
    """Map a (find, replace) written against clean_html(raw) back onto raw.
    Returns (raw_find, raw_replace) or None if find is absent or not unique in the clean text.
    Span tags stripped from inside the region are re-appended to the replacement so the
    surrounding markup stays balanced."""
    clean, idx, removed = clean_map(raw)
    if not find_clean or clean.count(find_clean) != 1:
        return None
    s = clean.find(find_clean)
    rs, re_ = idx[s], idx[s + len(find_clean) - 1] + 1
    inner_spans = "".join(t for a, b, t in removed if a >= rs and b <= re_ and _SPAN.fullmatch(t))
    return raw[rs:re_], replace_clean + inner_spans
