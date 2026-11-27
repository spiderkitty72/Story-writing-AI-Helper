"""FastAPI web app: JSON API plus the single-page UI in app/static."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import markdown as md
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import config as config_mod
from app import knowledge as kg
from app.engine import EditRequest, Engine
from app.providers import ProviderError
from app.storage import Conflict, NotFound, Storage

STATIC = Path(__file__).parent / "static"


class State:
    def __init__(self):
        self.config = config_mod.load_config()
        self.storage = Storage(self.config.data_dir)

    def reload(self, cfg=None):
        self.config = cfg or config_mod.load_config()
        self.storage = Storage(self.config.data_dir)


state = State()
engine = Engine(lambda: state.config, lambda: state.storage)
app = FastAPI(title="Story Writing AI Helper")


@app.exception_handler(NotFound)
async def _nf(_: Request, e: NotFound):
    return JSONResponse({"detail": str(e).strip("'\"")}, status_code=404)


@app.exception_handler(Conflict)
async def _cf(_: Request, e: Conflict):
    return JSONResponse({"detail": str(e)}, status_code=409)


@app.exception_handler(ValueError)
async def _ve(_: Request, e: ValueError):
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.exception_handler(ProviderError)
async def _pe(_: Request, e: ProviderError):
    return JSONResponse({"detail": str(e)}, status_code=502)


# ---------- UI ----------
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------- config ----------
@app.get("/api/config")
async def get_config():
    c = state.config
    return {
        "default_provider": c.default_provider,
        "providers": [{"name": n, "type": p["type"], "model": p.get("model", "")} for n, p in c.providers.items()],
        "agents": [{"name": a.name, "description": a.description, "mode": a.mode} for a in c.agents.values()],
        "workflows": [{"name": w.name, "description": w.description, "steps": [s.agent for s in w.steps]}
                      for w in c.workflows.values()],
        "chunking": vars(c.chunking),
    }


@app.get("/api/config/files/{name}", response_class=PlainTextResponse)
async def get_config_file(name: str):
    return config_mod.read_raw(name)


class TextBody(BaseModel):
    text: str


@app.put("/api/config/files/{name}")
async def put_config_file(name: str, body: TextBody):
    cfg = config_mod.write_raw(name, body.text)
    state.reload(cfg)
    return {"ok": True}


@app.get("/api/providers/{name}/check")
async def check_provider(name: str):
    p = engine.provider(name)
    try:
        return {"ok": True, "status": await p.check(), "model": p.model}
    except ProviderError as e:
        return {"ok": False, "status": str(e), "model": p.model}


# ---------- books & chapters ----------
class NewBook(BaseModel):
    title: str


class BookUpdate(BaseModel):
    title: str | None = None
    notes: str | None = None


class NewChapter(BaseModel):
    title: str
    text: str | None = None


class Order(BaseModel):
    order: list[str]


@app.get("/api/books")
async def list_books():
    return state.storage.list_books()


@app.post("/api/books")
async def create_book(body: NewBook):
    return {"id": state.storage.create_book(body.title or "Untitled")}


@app.get("/api/books/{book}")
async def get_book(book: str):
    return state.storage.get_book(book)


@app.patch("/api/books/{book}")
async def update_book(book: str, body: BookUpdate):
    state.storage.update_book(book, body.title, body.notes)
    return {"ok": True}


@app.delete("/api/books/{book}")
async def delete_book(book: str):
    state.storage.delete_book(book)
    return {"ok": True}


@app.get("/api/books/{book}/export", response_class=PlainTextResponse)
async def export_book(book: str):
    return PlainTextResponse(state.storage.export_book(book), media_type="text/markdown",
                             headers={"Content-Disposition": f'attachment; filename="{book}.md"'})


@app.post("/api/books/{book}/chapters")
async def create_chapter(book: str, body: NewChapter):
    return {"id": state.storage.create_chapter(book, body.title or "New chapter", body.text)}


@app.put("/api/books/{book}/order")
async def reorder(book: str, body: Order):
    state.storage.reorder_chapters(book, body.order)
    return {"ok": True}


@app.get("/api/books/{book}/chapters/{chapter}")
async def read_chapter(book: str, chapter: str):
    return {"id": chapter, "text": state.storage.read_chapter(book, chapter)}


@app.put("/api/books/{book}/chapters/{chapter}")
async def write_chapter(book: str, chapter: str, body: TextBody):
    state.storage.write_chapter(book, chapter, body.text)
    return {"ok": True}


@app.delete("/api/books/{book}/chapters/{chapter}")
async def delete_chapter(book: str, chapter: str):
    state.storage.delete_chapter(book, chapter)
    return {"ok": True}


@app.post("/api/render")
async def render(body: TextBody):
    return {"html": md.markdown(body.text, extensions=["extra", "sane_lists"])}


# ---------- AI jobs ----------
class RunBody(BaseModel):
    chapter: str
    workflow: str | None = None
    agent: str | None = None
    instructions: str = ""
    start: int | None = None
    end: int | None = None
    provider: str | None = None
    model: str | None = None
    chunk_tokens: int | None = None
    overlap_tokens: int | None = None
    use_knowledge: bool = True


@app.post("/api/books/{book}/run")
async def run(book: str, body: RunBody):
    job = engine.start_edit(EditRequest(book=book, **body.model_dump()))
    return job.public()


@app.get("/api/jobs")
async def jobs():
    return [j.public() for j in sorted(engine.jobs.values(), key=lambda j: -j.started)]


@app.get("/api/jobs/{job_id}")
async def job(job_id: str):
    if job_id not in engine.jobs:
        raise HTTPException(404, "job not found")
    return engine.jobs[job_id].public()


@app.post("/api/jobs/{job_id}/cancel")
async def cancel(job_id: str):
    engine.cancel(job_id)
    return {"ok": True}


# ---------- reviews ----------
class ApplyBody(BaseModel):
    decisions: dict[str, dict[str, bool]] = {}
    accept_all: bool = False


@app.get("/api/books/{book}/reviews")
async def list_reviews(book: str):
    return state.storage.list_reviews(book)


@app.get("/api/books/{book}/reviews/{review_id}")
async def get_review(book: str, review_id: str):
    return state.storage.read_review(book, review_id)


@app.post("/api/books/{book}/reviews/{review_id}/apply")
async def apply_review(book: str, review_id: str, body: ApplyBody):
    return engine.apply_review(book, review_id, body.decisions, body.accept_all)


@app.delete("/api/books/{book}/reviews/{review_id}")
async def delete_review(book: str, review_id: str):
    state.storage.delete_review(book, review_id)
    return {"ok": True}


# ---------- knowledge graph ----------
class KnowledgeBuild(BaseModel):
    chapter: str | None = None
    provider: str | None = None
    model: str | None = None


@app.get("/api/books/{book}/knowledge")
async def get_knowledge(book: str):
    return state.storage.read_knowledge(book)


@app.put("/api/books/{book}/knowledge")
async def put_knowledge(book: str, body: dict[str, Any]):
    graph = {
        "entities": [kg.normalize_entity(e) for e in body.get("entities", []) if str(e.get("name", "")).strip()],
        "relations": [r for r in body.get("relations", []) if isinstance(r, dict)],
    }
    ids = {e["id"] for e in graph["entities"]}
    graph["relations"] = [
        {"source": r["source"], "target": r["target"], "label": str(r.get("label", "related to"))}
        for r in graph["relations"] if r.get("source") in ids and r.get("target") in ids
    ]
    state.storage.write_knowledge(book, graph)
    return graph


@app.post("/api/books/{book}/knowledge/build")
async def build_knowledge(book: str, body: KnowledgeBuild):
    return engine.start_knowledge(book, body.chapter, body.provider, body.model).public()


@app.post("/api/books/{book}/knowledge/preview")
async def preview_knowledge(book: str, body: TextBody):
    """Show exactly what knowledge would be sent with a given passage."""
    c = state.config.chunking
    return {"context": kg.render_context(state.storage.read_knowledge(book), body.text,
                                         c.knowledge_tokens * c.chars_per_token)}
