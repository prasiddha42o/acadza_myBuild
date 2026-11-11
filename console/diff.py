"""Before/after views for one proposed edit, with the changed span highlighted.

<mark> may not sit inside MathML, and wrapping a region with unbalanced tags breaks the page.
So: plain or balanced region outside math -> <mark> around it; region inside a <math> -> mark
the whole expression; anything else -> no inline mark, the one-line text strip still shows it."""
import re

from engine.common import flat, get_field, parse_options, payload
from engine.guards import WHOLE, edit_list

TAG = re.compile(r"<(/?)([a-zA-Z][\w:-]*)[^>]*?(/?)>")
VOID = {"br", "img", "hr", "input", "meta", "link", "wbr", "mspace", "none", "mprescripts"}


def _balanced(region):
    stack = []
    for m in TAG.finditer(region):
        closing, name, selfclose = m.group(1), m.group(2).lower(), m.group(3)
        if selfclose or name in VOID:
            continue
        if closing:
            if not stack or stack[-1] != name:
                return False
            stack.pop()
        else:
            stack.append(name)
    rest = TAG.sub("", region)
    return not stack and "<" not in rest and ">" not in rest


def _math_span(html, s, e):
    """(start, end) of the <math> element containing [s, e), or None."""
    ms = html.rfind("<math", 0, s + 1)
    if ms == -1 or html.rfind("</math>", 0, s) > ms:
        return None
    me = html.find("</math>", ms)
    if me == -1 or me + 7 < e:
        return None
    return ms, me + 7


def mark(html, s, e, cls):
    """Return (html_with_mark, how) where how is 'inline' | 'math' | 'none'."""
    if s == e:
        return html, "none"
    m = _math_span(html, s, e)
    if m:
        a, b = m
        return html[:a] + '<mark class="%s">' % cls + html[a:b] + "</mark>" + html[b:], "math"
    region = html[s:e]
    if _balanced(region):
        return html[:s] + '<mark class="%s">' % cls + region + "</mark>" + html[e:], "inline"
    return html, "none"


def _letters(a):
    if isinstance(a, list):
        a = ",".join(a)
    return sorted(set(re.findall(r"(?<![A-Za-z])([A-D])(?![A-Za-z])", str(a or "").upper())))


def edit_views(q, edit):
    """q: the record the edit applies to (live, before applying)."""
    out = []
    target_type = next((e["replace"] for e in edit_list(edit) if e.get("field") == "questionType"), q["questionType"])
    for e in edit_list(edit):
        f, find, rep = e.get("field"), e.get("find"), e.get("replace")
        if f == "answer":
            opts = parse_options(payload(q)["question"]) if target_type in ("scq", "mcq") else {}
            out.append(dict(kind="answer", field=f, qtype=target_type, old=find, new=rep,
                            old_letters=_letters(find), new_letters=_letters(rep),
                            options=[dict(letter=l, text=opts.get(l, "")[:80]) for l in "ABCD"] if opts or target_type != "integerQuestion" else []))
        elif f == "questionType":
            out.append(dict(kind="type", field=f, old=find, new=rep))
        elif f in ("question", "solution"):
            text = get_field(q, f)
            if find == WHOLE:
                out.append(dict(kind="rewrite", field=f, before=text, after=rep, strip_del=flat(text)[:160], strip_ins=flat(rep)[:160]))
                continue
            s = text.find(find)
            if s == -1:
                out.append(dict(kind="missing", field=f, find=find, replace=rep))
                continue
            before, how_b = mark(text, s, s + len(find), "del")
            after_text = text[:s] + rep + text[s + len(find):]
            after, how_a = mark(after_text, s, s + len(rep), "ins")
            out.append(dict(kind="text", field=f, before=before, after=after, how=[how_b, how_a],
                            strip_del=flat(find) or "(nothing)", strip_ins=flat(rep) or "(removed)"))
    return out
