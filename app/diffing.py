"""Word-level diffs with accept/reject per change ("hunk")."""
from __future__ import annotations

import difflib
import re

_TOKEN_RE = re.compile(r"\s+|\w+|[^\w\s]", re.U)


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def diff_segments(original: str, revised: str) -> list[dict]:
    """Return a list of segments.

    Each segment is ``{"type": "equal", "text": ...}`` or
    ``{"type": "change", "id": n, "old": ..., "new": ...}``. Joining the
    ``text``/``old`` parts gives the original; ``text``/``new`` gives the revision.
    """
    a, b = tokenize(original), tokenize(revised)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ops = sm.get_opcodes()

    # Merge changes separated only by a single whitespace token, which reads
    # much better ("the quick" -> "a fast" is one change, not two).
    merged: list[list] = []
    for op in ops:
        tag, i1, i2, j1, j2 = op
        if (
            tag == "equal"
            and merged
            and merged[-1][0] != "equal"
            and i2 - i1 == 1
            and a[i1].isspace()
            and op is not ops[-1]
        ):
            merged[-1] = ["replace", merged[-1][1], i2, merged[-1][3], j2]
            continue
        if tag != "equal" and merged and merged[-1][0] != "equal":
            merged[-1] = ["replace", merged[-1][1], i2, merged[-1][3], j2]
            continue
        merged.append([tag, i1, i2, j1, j2])

    segments: list[dict] = []
    n = 0
    for tag, i1, i2, j1, j2 in merged:
        if tag == "equal":
            segments.append({"type": "equal", "text": "".join(a[i1:i2])})
        else:
            segments.append({"type": "change", "id": n, "old": "".join(a[i1:i2]), "new": "".join(b[j1:j2])})
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
