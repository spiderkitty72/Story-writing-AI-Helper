"""API routes for the writing tools (outline, snapshots, find and replace,
style checks, timeline). Kept apart from main.py so the app stays easy to read."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter
from pydantic import BaseModel

from app import style, tools
from app.storage import Storage


class OutlineBody(BaseModel):
    synopsis: str | None = None
    status: str | None = None


class SnapshotBody(BaseModel):
    label: str = ""


class SearchBody(BaseModel):
    query: str
    case: bool = False
    whole_word: bool = False
    regex: bool = False


class ReplaceBody(SearchBody):
    replacement: str = ""
    chapters: list[str] | None = None


class StyleBody(BaseModel):
    text: str


def make_router(storage: Callable[[], Storage]) -> APIRouter:
    r = APIRouter()

    # outline / corkboard
    @r.get("/api/books/{book}/outline")
    async def outline(book: str):
        return {"statuses": tools.STATUSES, "chapters": tools.get_outline(storage(), book)}

    @r.patch("/api/books/{book}/outline/{chapter}")
    async def set_outline(book: str, chapter: str, body: OutlineBody):
        tools.set_outline(storage(), book, chapter, body.synopsis, body.status)
        return {"ok": True}

    # snapshots
    @r.get("/api/books/{book}/chapters/{chapter}/snapshots")
    async def snapshots(book: str, chapter: str):
        return tools.list_snapshots(storage(), book, chapter)

    @r.post("/api/books/{book}/chapters/{chapter}/snapshots")
    async def new_snapshot(book: str, chapter: str, body: SnapshotBody):
        return {"id": tools.save_snapshot(storage(), book, chapter, body.label.strip() or "Saved by hand")}

    @r.get("/api/books/{book}/chapters/{chapter}/snapshots/{sid}")
    async def snapshot(book: str, chapter: str, sid: str):
        return tools.read_snapshot(storage(), book, chapter, sid)

    @r.post("/api/books/{book}/chapters/{chapter}/snapshots/{sid}/restore")
    async def restore(book: str, chapter: str, sid: str):
        tools.restore_snapshot(storage(), book, chapter, sid)
        return {"ok": True}

    # find and replace
    @r.post("/api/books/{book}/search")
    async def search(book: str, body: SearchBody):
        return tools.search(storage(), book, body.query, body.case, body.whole_word, body.regex)

    @r.post("/api/books/{book}/replace")
    async def replace(book: str, body: ReplaceBody):
        return tools.replace(storage(), book, body.query, body.replacement, body.case, body.whole_word,
                             body.regex, body.chapters)

    # style checks
    @r.post("/api/style")
    async def style_check(body: StyleBody):
        return style.analyze(body.text)

    # timeline / plot threads
    @r.get("/api/books/{book}/timeline")
    async def timeline(book: str):
        return tools.read_timeline(storage(), book)

    @r.put("/api/books/{book}/timeline")
    async def put_timeline(book: str, body: dict[str, Any]):
        return tools.write_timeline(storage(), book, body)

    return r
