"""Readable diffs with accept/reject per change ("hunk").

The diff works top-down so that rewrites stay readable:

1. Paragraphs are aligned first. Unchanged paragraphs are left alone.
2. A changed paragraph that is still mostly the same text (a proofreading fix)
   gets a word-level diff, so a corrected typo shows up as one small change.
3. A paragraph that was largely rewritten is shown as one change (old
   paragraph struck out, new one inserted) instead of a soup of tiny edits
   that match on common words like "the" and "and".
4. Word-level changes separated only by a word or two are merged.
"""
from __future__ import annotations

import difflib
import re

_TOKEN_RE = re.compile(r"\s+|\w+|[^\w\s]", re.U)
_PARA_SEP_RE = re.compile(r"(\n[ \t]*\n\s*)")

# A changed paragraph pair is diffed word by word only if at least this share
# of its words is unchanged; otherwise it is shown as a whole-paragraph change.
WORD_DIFF_MIN_SIMILARITY = 0.6
# Equal runs of at most this many words between two changes are folded in.
MERGE_GAP_WORDS = 1


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def paragraphs(text: str) -> list[str]:
    """Split into paragraphs, each keeping its trailing blank lines."""
    bits = _PARA_SEP_RE.split(text)
    parts = [bits[i] + (bits[i + 1] if i + 1 < len(bits) else "") for i in range(0, len(bits), 2)]
    return [p for p in parts if p]


def similarity(a: str, b: str) -> float:
    wa, wb = re.findall(r"\w+", a.lower()), re.findall(r"\w+", b.lower())
    if not wa and not wb:
        return 1.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def _word_segments(a_text: str, b_text: str) -> list[dict]:
    a, b = tokenize(a_text), tokenize(b_text)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    segs: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            segs.append({"type": "equal", "text": "".join(a[i1:i2])})
        else:
            segs.append({"type": "change", "old": "".join(a[i1:i2]), "new": "".join(b[j1:j2])})
    return _merge_small_gaps(segs)


def _merge_small_gaps(segs: list[dict]) -> list[dict]:
    out: list[dict] = []
    i = 0
    while i < len(segs):
        s = segs[i]
        if (
            s["type"] == "equal"
            and out and out[-1]["type"] == "change"
            and i + 1 < len(segs) and segs[i + 1]["type"] == "change"
            and len(re.findall(r"\w+", s["text"])) <= MERGE_GAP_WORDS
            and "\n\n" not in s["text"]
        ):
            nxt = segs[i + 1]
            out[-1] = {"type": "change", "old": out[-1]["old"] + s["text"] + nxt["old"],
                       "new": out[-1]["new"] + s["text"] + nxt["new"]}
            i += 2
            continue
        if s["type"] == "change" and out and out[-1]["type"] == "change":
            out[-1] = {"type": "change", "old": out[-1]["old"] + s["old"], "new": out[-1]["new"] + s["new"]}
        else:
            out.append(dict(s))
        i += 1
    return out


def _whole_change(old: str, new: str) -> list[dict]:
    """One change, but keep shared leading/trailing whitespace outside it."""
    lead = len(old) - len(old.lstrip())
    lead = min(lead, len(new) - len(new.lstrip()))
    lead = lead if old[:lead] == new[:lead] else 0
    trail_o, trail_n = old[len(old.rstrip()):], new[len(new.rstrip()):]
    trail = trail_o if trail_o == trail_n else ""
    core_old = old[lead: len(old) - len(trail)]
    core_new = new[lead: len(new) - len(trail)]
    segs = []
    if lead:
        segs.append({"type": "equal", "text": old[:lead]})
    segs.append({"type": "change", "old": core_old, "new": core_new})
    if trail:
        segs.append({"type": "equal", "text": trail})
    return segs


def _pair_segments(old: str, new: str) -> list[dict]:
    if similarity(old, new) >= WORD_DIFF_MIN_SIMILARITY:
        return _word_segments(old, new)
    return _whole_change(old, new)


def diff_segments(original: str, revised: str) -> list[dict]:
    """Return a list of segments.

    Each segment is ``{"type": "equal", "text": ...}`` or
    ``{"type": "change", "id": n, "old": ..., "new": ...}``. Joining the
    ``text``/``old`` parts gives the original; ``text``/``new`` gives the revision.
    """
    pa, pb = paragraphs(original), paragraphs(revised)
    sm = difflib.SequenceMatcher(None, [p.strip() for p in pa], [p.strip() for p in pb], autojunk=False)
    raw: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                if pa[i1 + k] == pb[j1 + k]:
                    raw.append({"type": "equal", "text": pa[i1 + k]})
                else:  # same words, different spacing
                    raw += _word_segments(pa[i1 + k], pb[j1 + k])
        elif tag == "replace" and i2 - i1 == j2 - j1:
            for k in range(i2 - i1):
                raw += _pair_segments(pa[i1 + k], pb[j1 + k])
        else:
            old, new = "".join(pa[i1:i2]), "".join(pb[j1:j2])
            if old and new:
                raw += _pair_segments(old, new)
            else:
                raw.append({"type": "change", "old": old, "new": new})

    # merge neighbours of the same kind and number the changes
    segments: list[dict] = []
    for s in raw:
        if s["type"] == "change" and not s["old"] and not s["new"]:
            continue
        if segments and segments[-1]["type"] == s["type"]:
            if s["type"] == "equal":
                segments[-1]["text"] += s["text"]
            else:
                segments[-1]["old"] += s["old"]
                segments[-1]["new"] += s["new"]
        else:
            segments.append(dict(s))
    n = 0
    for s in segments:
        if s["type"] == "change":
            s["id"] = n
            n += 1
    return segments


def apply_decisions(segments: list[dict], decisions: dict[int, bool] | None = None, default: bool = False) -> str:
    """Build text from segments; ``decisions[id]`` True keeps the AI's version."""
    decisions = decisions or {}
    out = []
    for s in segments:
        if s["type"] == "equal":
            out.append(s["text"])
        else:
            out.append(s["new"] if decisions.get(s["id"], default) else s["old"])
    return "".join(out)


def count_changes(segments: list[dict]) -> int:
    return sum(1 for s in segments if s["type"] == "change")
