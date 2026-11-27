"""File-based storage. Every book is a folder; every chapter is a Markdown file.

    data/books/<book>/book.yaml        title, notes (style/theme), chapter order
    data/books/<book>/chapters/*.md    the manuscript
    data/books/<book>/knowledge.json   knowledge graph (characters, places, facts)
    data/books/<book>/reviews/*.json   AI change sets waiting for review
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,120}$")


class NotFound(KeyError):
    pass


class Conflict(RuntimeError):
    pass


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").lower()
    return s[:60] or "untitled"


def _safe(name: str) -> str:
    if not _SAFE_RE.match(name) or ".." in name:
        raise ValueError(f"invalid name: {name!r}")
    return name


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


class Storage:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "books"
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------- books ----------
    def _book_dir(self, book: str) -> Path:
        d = self.root / _safe(book)
        if not d.is_dir():
            raise NotFound(f"book '{book}' not found")
        return d

    def _meta(self, book: str) -> dict[str, Any]:
        p = self._book_dir(book) / "book.yaml"
        meta = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
        meta = meta or {}
        meta.setdefault("title", book)
        meta.setdefault("notes", "")
        meta.setdefault("order", [])
        return meta

    def _save_meta(self, book: str, meta: dict[str, Any]) -> None:
        _atomic_write(self._book_dir(book) / "book.yaml", yaml.safe_dump(meta, sort_keys=False, allow_unicode=True))

    def list_books(self) -> list[dict[str, Any]]:
        out = []
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and _SAFE_RE.match(d.name):
                meta = self._meta(d.name)
                out.append({"id": d.name, "title": meta["title"]})
        return out

    def create_book(self, title: str) -> str:
        base = slugify(title)
        book, n = base, 2
        while (self.root / book).exists():
            book, n = f"{base}-{n}", n + 1
        (self.root / book / "chapters").mkdir(parents=True)
        self._save_meta(book, {"title": title, "notes": "", "order": []})
        self.create_chapter(book, "Chapter 1")
        return book

    def get_book(self, book: str) -> dict[str, Any]:
        meta = self._meta(book)
        return {"id": book, "title": meta["title"], "notes": meta["notes"], "chapters": self.list_chapters(book)}

    def update_book(self, book: str, title: str | None = None, notes: str | None = None) -> None:
        meta = self._meta(book)
        if title is not None:
            meta["title"] = title
        if notes is not None:
            meta["notes"] = notes
        self._save_meta(book, meta)

    def delete_book(self, book: str) -> None:
        import shutil

        shutil.rmtree(self._book_dir(book))

    # ---------- chapters ----------
    def _chapter_dir(self, book: str) -> Path:
        d = self._book_dir(book) / "chapters"
        d.mkdir(exist_ok=True)
        return d

    def list_chapters(self, book: str) -> list[dict[str, Any]]:
        meta = self._meta(book)
        files = {p.name for p in self._chapter_dir(book).glob("*.md")}
        order = [f for f in meta["order"] if f in files] + sorted(files - set(meta["order"]))
        out = []
        for f in order:
            text = (self._chapter_dir(book) / f).read_text(encoding="utf-8")
            title = next((ln.lstrip("#").strip() for ln in text.splitlines() if ln.startswith("#")), f[:-3])
            out.append({"id": f, "title": title, "words": len(text.split())})
        return out

    def create_chapter(self, book: str, title: str, text: str | None = None) -> str:
        meta = self._meta(book)
        n = len(list(self._chapter_dir(book).glob("*.md"))) + 1
        base = f"{n:02d}-{slugify(title)}"
        name, k = f"{base}.md", 2
        while (self._chapter_dir(book) / name).exists():
            name, k = f"{base}-{k}.md", k + 1
        _atomic_write(self._chapter_dir(book) / name, text if text is not None else f"# {title}\n\n")
        meta["order"] = [f for f in meta["order"] if f != name] + [name]
        self._save_meta(book, meta)
        return name

    def read_chapter(self, book: str, chapter: str) -> str:
        p = self._chapter_dir(book) / _safe(chapter)
        if not p.exists() or p.suffix != ".md":
            raise NotFound(f"chapter '{chapter}' not found")
        return p.read_text(encoding="utf-8")

    def write_chapter(self, book: str, chapter: str, text: str) -> None:
        self.read_chapter(book, chapter)  # existence check
        _atomic_write(self._chapter_dir(book) / chapter, text)

    def delete_chapter(self, book: str, chapter: str) -> None:
        self.read_chapter(book, chapter)
        (self._chapter_dir(book) / chapter).unlink()
        meta = self._meta(book)
        meta["order"] = [f for f in meta["order"] if f != chapter]
        self._save_meta(book, meta)

    def reorder_chapters(self, book: str, order: list[str]) -> None:
        meta = self._meta(book)
        meta["order"] = [_safe(f) for f in order]
        self._save_meta(book, meta)

    def export_book(self, book: str) -> str:
        parts = [self.read_chapter(book, c["id"]).rstrip() for c in self.list_chapters(book)]
        return "\n\n".join(parts) + "\n"

    # ---------- knowledge ----------
    def read_knowledge(self, book: str) -> dict[str, Any]:
        p = self._book_dir(book) / "knowledge.json"
        if not p.exists():
            return {"entities": [], "relations": []}
        return json.loads(p.read_text(encoding="utf-8"))

    def write_knowledge(self, book: str, data: dict[str, Any]) -> None:
        _atomic_write(self._book_dir(book) / "knowledge.json", json.dumps(data, indent=2, ensure_ascii=False))

    # ---------- reviews ----------
    def _review_dir(self, book: str) -> Path:
        d = self._book_dir(book) / "reviews"
        d.mkdir(exist_ok=True)
        return d

    def save_review(self, book: str, review: dict[str, Any]) -> str:
        review.setdefault("id", time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
        _atomic_write(self._review_dir(book) / f"{review['id']}.json", json.dumps(review, ensure_ascii=False))
        return review["id"]

    def read_review(self, book: str, review_id: str) -> dict[str, Any]:
        p = self._review_dir(book) / f"{_safe(review_id)}.json"
        if not p.exists():
            raise NotFound(f"review '{review_id}' not found")
        return json.loads(p.read_text(encoding="utf-8"))

    def list_reviews(self, book: str) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self._review_dir(book).glob("*.json"), reverse=True):
            r = json.loads(p.read_text(encoding="utf-8"))
            out.append({k: r.get(k) for k in ("id", "chapter", "label", "status", "created", "changes", "notes_count")})
        return out

    def delete_review(self, book: str, review_id: str) -> None:
        p = self._review_dir(book) / f"{_safe(review_id)}.json"
        if p.exists():
            p.unlink()
