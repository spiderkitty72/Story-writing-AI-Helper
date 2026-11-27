"""Writing tools that work on the files of a book: outline, snapshots,
find and replace, and the plot timeline. None of these need the AI.

    data/books/<book>/book.yaml           outline: synopsis and status per chapter
    data/books/<book>/history/*.md        snapshots (also made before AI changes are applied)
    data/books/<book>/history/labels.json names of snapshots
    data/books/<book>/timeline.json       plot threads and events
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from app.diffing import diff_segments
from app.storage import NotFound, Storage, _atomic_write, _safe

STATUSES = ["idea", "outline", "draft", "revised", "done"]
THREAD_COLORS = ["#d9735b", "#4f9a6a", "#3b8fb5", "#c2a03a", "#8a6bc4", "#b85c8a", "#5f8f8f"]
_TS_RE = re.compile(r"^\d{8}-\d{6}(?:-\d+)?$")


# ---------- outline ----------
def get_outline(storage: Storage, book: str) -> list[dict[str, Any]]:
    meta = storage._meta(book)
    outline = meta.get("outline") or {}
    summaries = storage.read_knowledge(book).get("summaries", {})
    out = []
    for c in storage.list_chapters(book):
        o = outline.get(c["id"]) or {}
        out.append({**c, "synopsis": o.get("synopsis", ""), "status": o.get("status", "draft"),
                    "summary": summaries.get(c["id"], "")})
    return out


def set_outline(storage: Storage, book: str, chapter: str, synopsis: str | None, status: str | None) -> None:
    storage.read_chapter(book, chapter)
    meta = storage._meta(book)
    outline = meta.setdefault("outline", {})
    entry = outline.setdefault(chapter, {})
    if synopsis is not None:
        entry["synopsis"] = synopsis.strip()
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
        entry["status"] = status
    storage._save_meta(book, meta)


def synopses(storage: Storage, book: str) -> dict[str, str]:
    outline = storage._meta(book).get("outline") or {}
    return {k: v.get("synopsis", "") for k, v in outline.items() if isinstance(v, dict) and v.get("synopsis")}


# ---------- snapshots ----------
def _history(storage: Storage, book: str) -> Path:
    d = storage._book_dir(book) / "history"
    d.mkdir(exist_ok=True)
    return d


def _labels(storage: Storage, book: str) -> dict[str, str]:
    p = _history(storage, book) / "labels.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except json.JSONDecodeError:
        return {}


def save_snapshot(storage: Storage, book: str, chapter: str, label: str = "", text: str | None = None) -> str:
    """Save a copy of the chapter (or of ``text``) and return the snapshot id."""
    text = storage.read_chapter(book, chapter) if text is None else text
    stem, d = chapter[:-3], _history(storage, book)
    ts = time.strftime("%Y%m%d-%H%M%S")
    sid, n = ts, 2
    while (d / f"{stem}.{sid}.md").exists():
        sid, n = f"{ts}-{n}", n + 1
    _atomic_write(d / f"{stem}.{sid}.md", text)
    if label:
        labels = _labels(storage, book)
        labels[f"{stem}.{sid}.md"] = label
        _atomic_write(d / "labels.json", json.dumps(labels, ensure_ascii=False, indent=1))
    return sid


def list_snapshots(storage: Storage, book: str, chapter: str) -> list[dict[str, Any]]:
    storage.read_chapter(book, chapter)
    stem, labels = chapter[:-3], _labels(storage, book)
    out = []
    for p in _history(storage, book).glob(f"{stem}.*.md"):
        sid = p.name[len(stem) + 1:-3]
        if not _TS_RE.match(sid):
            continue
        text = p.read_text(encoding="utf-8")
        when = f"{sid[0:4]}-{sid[4:6]}-{sid[6:8]} {sid[9:11]}:{sid[11:13]}:{sid[13:15]}"
        out.append({"id": sid, "created": when, "words": len(text.split()),
                    "label": labels.get(p.name, "Before AI changes were applied")})
    out.sort(key=lambda s: s["id"], reverse=True)
    return out


def _snapshot_path(storage: Storage, book: str, chapter: str, sid: str) -> Path:
    if not _TS_RE.match(sid):
        raise ValueError("invalid snapshot id")
    p = _history(storage, book) / f"{_safe(chapter)[:-3]}.{sid}.md"
    if not p.exists():
        raise NotFound(f"snapshot '{sid}' not found")
    return p


def read_snapshot(storage: Storage, book: str, chapter: str, sid: str) -> dict[str, Any]:
    text = _snapshot_path(storage, book, chapter, sid).read_text(encoding="utf-8")
    current = storage.read_chapter(book, chapter)
    return {"id": sid, "text": text, "segments": diff_segments(text, current)}


def restore_snapshot(storage: Storage, book: str, chapter: str, sid: str) -> None:
    text = _snapshot_path(storage, book, chapter, sid).read_text(encoding="utf-8")
    save_snapshot(storage, book, chapter, "Before restoring an older version")
    storage.write_chapter(book, chapter, text)


# ---------- find and replace ----------
def _pattern(query: str, case: bool, whole_word: bool, regex: bool) -> re.Pattern:
    if not query:
        raise ValueError("Type something to find")
    body = query if regex else re.escape(query)
    if whole_word:
        body = rf"(?<!\w){body}(?!\w)"
    try:
        return re.compile(body, 0 if case else re.I)
    except re.error as e:
        raise ValueError(f"Invalid pattern: {e}") from e


def search(storage: Storage, book: str, query: str, case: bool = False, whole_word: bool = False,
           regex: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    pat = _pattern(query, case, whole_word, regex)
    out = []
    for c in storage.list_chapters(book):
        text = storage.read_chapter(book, c["id"])
        hits = []
        count = 0
        for m in pat.finditer(text):
            if m.end() == m.start():
                continue
            count += 1
            if len(hits) < limit:
                hits.append({"start": m.start(), "end": m.end(),
                             "before": text[max(0, m.start() - 40): m.start()],
                             "match": m.group(0), "after": text[m.end(): m.end() + 40]})
        if count:
            out.append({"chapter": c["id"], "title": c["title"], "count": count, "hits": hits})
    return out


def replace(storage: Storage, book: str, query: str, replacement: str, case: bool = False,
            whole_word: bool = False, regex: bool = False, chapters: list[str] | None = None) -> dict[str, Any]:
    pat = _pattern(query, case, whole_word, regex)
    changed: dict[str, int] = {}
    for c in storage.list_chapters(book):
        if chapters and c["id"] not in chapters:
            continue
        text = storage.read_chapter(book, c["id"])
        try:
            new, n = pat.subn(replacement if regex else replacement.replace("\\", "\\\\"), text)
        except (re.error, IndexError) as e:
            raise ValueError(f"Invalid replacement: {e}") from e
        if n and new != text:
            save_snapshot(storage, book, c["id"], f"Before replacing '{query}' with '{replacement}'", text)
            storage.write_chapter(book, c["id"], new)
            changed[c["id"]] = n
    return {"replaced": sum(changed.values()), "chapters": changed}


# ---------- timeline / plot threads ----------
def empty_timeline() -> dict[str, Any]:
    return {"threads": [{"id": "main", "name": "Main plot", "color": THREAD_COLORS[0]}], "events": []}


def read_timeline(storage: Storage, book: str) -> dict[str, Any]:
    p = storage._book_dir(book) / "timeline.json"
    if not p.exists():
        return empty_timeline()
    return json.loads(p.read_text(encoding="utf-8"))


def write_timeline(storage: Storage, book: str, data: dict[str, Any]) -> dict[str, Any]:
    threads = []
    for i, t in enumerate(data.get("threads") or []):
        if not isinstance(t, dict) or not str(t.get("name", "")).strip():
            continue
        threads.append({"id": str(t.get("id") or uuid.uuid4().hex[:8]), "name": str(t["name"]).strip(),
                        "color": str(t.get("color") or THREAD_COLORS[i % len(THREAD_COLORS)])})
    if not threads:
        threads = empty_timeline()["threads"]
    ids = {t["id"] for t in threads}
    events = []
    for e in data.get("events") or []:
        if not isinstance(e, dict) or not str(e.get("title", "")).strip():
            continue
        events.append({
            "id": str(e.get("id") or uuid.uuid4().hex[:8]),
            "title": str(e["title"]).strip(),
            "notes": str(e.get("notes") or "").strip(),
            "when": str(e.get("when") or "").strip(),
            "thread": e.get("thread") if e.get("thread") in ids else threads[0]["id"],
            "chapter": str(e.get("chapter") or ""),
            "entities": [str(x) for x in e.get("entities") or []],
            "order": float(e.get("order") or 0),
        })
    out = {"threads": threads, "events": events}
    _atomic_write(storage._book_dir(book) / "timeline.json", json.dumps(out, ensure_ascii=False, indent=1))
    return out


def render_plan(timeline: dict[str, Any], chapter: str | None, max_chars: int) -> str:
    """The planned events for one chapter, for the AI prompt."""
    if not chapter or max_chars <= 0:
        return ""
    names = {t["id"]: t["name"] for t in timeline.get("threads", [])}
    events = sorted((e for e in timeline.get("events", []) if e.get("chapter") == chapter),
                    key=lambda e: e.get("order", 0))
    lines, used = [], 0
    for e in events:
        line = f"- [{names.get(e.get('thread'), 'Plot')}] {e['title']}"
        if e.get("when"):
            line += f" ({e['when']})"
        if e.get("notes"):
            line += f": {e['notes']}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
