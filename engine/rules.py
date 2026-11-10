"""Cheap rule-based pre-triage of the report text. Output is a HINT for the classifier,
except 'empty' which short-circuits (no LLM call, no question change)."""
import re

LANG = re.compile(r"(hindi|marathi|tamil|telugu|bengali|gujarati|language|hinglish)", re.I)
APP = re.compile(r"(clicked|tapped|pressed|saved|didn.?t save|keyboard|lag(gy)?|crash|stuck|button|submit)", re.I)
DISPLAY = re.compile(r"(load|slow|cut(?: off)?|screen|layout|overlap|zoom|font|blurry|scroll)", re.I)


def looks_gibberish(c):
    s = re.sub(r"[^a-z]", "", c.lower())
    if len(s) < 3:
        return True
    vowels = sum(ch in "aeiou" for ch in s)
    return len(set(s)) / len(s) > 0.9 and vowels / len(s) < 0.2 or bool(re.fullmatch(r"(asdf|qwer|zxcv|hjkl).*", s))


def triage_report(comment):
    c = (comment or "").strip()
    if not c:
        return dict(hint="empty", short_circuit=True)
    if looks_gibberish(c):
        return dict(hint="gibberish", short_circuit=True)
    if LANG.search(c) and re.search(r"(chahiye|chahie|in|me|mein)\b", c, re.I):
        return dict(hint="language_request", short_circuit=False)
    if APP.search(c) and not re.search(r"(answer|ans|key|option|galat|wrong)", c, re.I):
        return dict(hint="app_behaviour", short_circuit=False)
    if APP.search(c) and re.search(r"(saved|clicked)", c, re.I):
        return dict(hint="app_behaviour", short_circuit=False)
    if DISPLAY.search(c) and not re.search(r"(answer|key|wrong|galat)", c, re.I):
        return dict(hint="display", short_circuit=False)
    return dict(hint="needs_audit", short_circuit=False)


def duplicates(reports):
    """Group reports by question: {questionId: [reportId,...]}."""
    g = {}
    for r in reports:
        g.setdefault(r["questionId"], []).append(r["reportId"])
    return g
