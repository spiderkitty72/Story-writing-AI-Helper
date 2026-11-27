"""Style checks that need no AI: fast, private, and the same on any machine.

``analyze(text)`` returns statistics and a list of issues. Every issue has the
character range it refers to, so the editor can select it.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

STOPWORDS = set("""
a about above after again against all am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have
having he her here hers herself him himself his how i if in into is it its itself just me more most
my myself no nor not now of off on once only or other our ours ourselves out over own same she should
so some such than that the their theirs them themselves then there these they this those through to
too under until up very was we were what when where which while who whom why will with would you your
yours yourself yourselves said says say like one back would could get got go went into s t don didn
""".split())

# -ly words that are not adverbs (or are fine)
NOT_ADVERBS = set("""
family only early daily likely lonely lovely ugly holy silly belly bully jelly rally reply supply
apply fly ally italy july emily lily molly polly sally kelly billy holly friendly elderly costly
deadly curly chilly hilly oily wily woolly smelly assembly anomaly monopoly melancholy butterfly
""".split())

FILTER_WORDS = [
    "felt", "feel", "feels", "saw", "see", "sees", "heard", "hear", "hears", "noticed", "notice",
    "realized", "realised", "realize", "realise", "wondered", "wonder", "seemed", "seem", "seems",
    "watched", "looked at", "thought that", "knew that", "decided to", "began to", "started to",
]

WEAK_WORDS = ["very", "really", "just", "quite", "rather", "somewhat", "somehow", "suddenly",
              "actually", "basically", "literally", "totally", "simply", "almost", "nearly"]

BE_VERBS = r"(?:am|is|are|was|were|be|been|being)"
IRREGULAR_PARTICIPLES = (
    "beaten|begun|bitten|blown|broken|brought|built|bought|caught|chosen|done|drawn|driven|eaten|"
    "fallen|felt|found|forgotten|forgiven|frozen|given|gone|grown|heard|held|hidden|hit|hurt|kept|"
    "known|laid|led|left|lost|made|meant|met|paid|put|read|ridden|rung|risen|run|said|seen|sent|set|"
    "shaken|shot|shown|shut|sold|spoken|spent|stolen|struck|sung|sunk|swept|taken|taught|thrown|"
    "told|torn|understood|woken|won|worn|written"
)
PASSIVE_RE = re.compile(
    rf"\b{BE_VERBS}\s+(?:\w+ly\s+)?(?:\w+ed|{IRREGULAR_PARTICIPLES})\b(?:\s+by\b)?", re.I)

WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’]*")
SENT_RE = re.compile(r"[^.!?\n]+(?:[.!?]+[\"'”’)\]]*|\n|$)")

LONG_SENTENCE_WORDS = 35
ECHO_WINDOW_WORDS = 40


def _syllables(word: str) -> int:
    w = word.lower().strip("'’")
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", w)))


def _strip_markdown_mask(text: str) -> str:
    """Blank out Markdown headings so they don't count as sentences (keeps offsets)."""
    return re.sub(r"(?m)^#{1,6}[^\n]*", lambda m: " " * len(m.group(0)), text)


def analyze(text: str) -> dict[str, Any]:
    body = _strip_markdown_mask(text)
    words = [(m.group(0), m.start(), m.end()) for m in WORD_RE.finditer(body)]
    sentences = [(m.group(0), m.start(), m.end()) for m in SENT_RE.finditer(body) if WORD_RE.search(m.group(0))]
    issues: list[dict[str, Any]] = []

    def add(kind: str, start: int, end: int, message: str) -> None:
        issues.append({"type": kind, "start": start, "end": end, "text": text[start:end], "message": message})

    # long sentences
    for s, a, b in sentences:
        n = len(WORD_RE.findall(s))
        if n > LONG_SENTENCE_WORDS:
            lead = len(s) - len(s.lstrip())
            add("long_sentence", a + lead, b, f"{n} words. Consider splitting this sentence.")

    # adverbs
    for w, a, b in words:
        lw = w.lower()
        if lw.endswith("ly") and len(lw) > 4 and lw not in NOT_ADVERBS:
            add("adverb", a, b, "Adverb. A stronger verb often works better.")

    # passive voice
    for m in PASSIVE_RE.finditer(body):
        add("passive", m.start(), m.end(), "Possible passive voice.")

    # filter and weak words
    for phrase in FILTER_WORDS:
        for m in re.finditer(rf"\b{re.escape(phrase)}\b", body, re.I):
            add("filter", m.start(), m.end(),
                "Filter word: it puts distance between the reader and what the character senses.")
    for phrase in WEAK_WORDS:
        for m in re.finditer(rf"\b{re.escape(phrase)}\b", body, re.I):
            add("weak", m.start(), m.end(), "Weak or filler word. Try cutting it.")

    # echoes: the same uncommon word again within a short distance
    last_seen: dict[str, int] = {}
    for i, (w, a, b) in enumerate(words):
        lw = w.lower().strip("'’")
        if len(lw) < 4 or lw in STOPWORDS:
            continue
        if lw in last_seen and i - last_seen[lw] <= ECHO_WINDOW_WORDS:
            add("echo", a, b, f"'{w}' was also used {i - last_seen[lw]} words earlier.")
        last_seen[lw] = i

    # repeated phrases (3 words, 3+ times)
    lower = [w.lower() for w, _, _ in words]
    grams: dict[tuple[str, ...], list[int]] = {}
    for i in range(len(lower) - 2):
        g = tuple(lower[i:i + 3])
        if all(x in STOPWORDS for x in g):
            continue
        grams.setdefault(g, []).append(i)
    repeated = []
    for g, idxs in grams.items():
        if len(idxs) >= 3:
            repeated.append({"phrase": " ".join(g), "count": len(idxs)})
            for i in idxs:
                add("repeated_phrase", words[i][1], words[i + 2][2], f"'{' '.join(g)}' appears {len(idxs)} times.")
    repeated.sort(key=lambda r: -r["count"])

    # overused words
    counts = Counter(w for w in lower if len(w) > 3 and w not in STOPWORDS)
    total = max(1, len(words))
    overused = [{"word": w, "count": c, "per_1000": round(1000 * c / total, 1)}
                for w, c in counts.most_common(15) if c >= 3]

    # readability (Flesch reading ease)
    n_words, n_sent = len(words), max(1, len(sentences))
    syll = sum(_syllables(w) for w, _, _ in words)
    flesch = 206.835 - 1.015 * (n_words / n_sent) - 84.6 * (syll / max(1, n_words)) if n_words else 0.0
    dialogue = sum(len(m.group(0)) for m in re.finditer(r"[\"“][^\"”]*[\"”]", text))

    issues.sort(key=lambda x: (x["start"], x["type"]))
    by_type = Counter(i["type"] for i in issues)
    return {
        "stats": {
            "words": n_words,
            "sentences": len(sentences),
            "avg_sentence_words": round(n_words / n_sent, 1) if n_words else 0,
            "reading_ease": round(flesch, 1),
            "dialogue_percent": round(100 * dialogue / max(1, len(text))),
        },
        "counts": dict(by_type),
        "overused": overused,
        "repeated_phrases": repeated[:15],
        "issues": issues[:2000],
    }
