"""Runs agents and workflows over a book, chunk by chunk, as background jobs."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from app import knowledge as kg
from app.chunking import chunk_text
from app.config import Config, WorkflowStep
from app.diffing import apply_decisions, count_changes, diff_segments
from app.prompts import (
    RETRY_LONG,
    RETRY_PARAGRAPHS,
    RETRY_SHORT,
    build_prompt,
    build_system,
    clean_edit_output,
    count_paragraphs,
    parse_notes,
)
from app.providers import Provider, ProviderError, make_provider
from app.storage import Conflict, Storage

MAX_CONSECUTIVE_ERRORS = 3
DEFAULT_LENGTH_RATIO = (0.5, 2.5)


def check_answer(original: str, revised: str, agent, truncated: bool) -> str | None:
    """Return why an edit answer looks broken, or None if it looks usable."""
    if truncated:
        return "the answer was cut off by the model's output limit"
    lo, hi = agent.length_ratio or DEFAULT_LENGTH_RATIO
    n = max(1, len(original.strip()))
    ratio = len(revised.strip()) / n
    if n >= 80 and ratio < lo:
        return f"the answer was only {ratio:.0%} as long as the original, so text was left out"
    if n >= 80 and ratio > hi:
        return f"the answer was {ratio:.0%} as long as the original, more than expected"
    if agent.keep_paragraphs and count_paragraphs(original) != count_paragraphs(revised):
        return (f"the answer had {count_paragraphs(revised)} paragraphs instead of "
                f"{count_paragraphs(original)}")
    return None


@dataclass
class Job:
    id: str
    kind: str
    label: str
    status: str = "running"  # running | done | error | cancelled
    progress: int = 0
    total: int = 0
    message: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    cancelled: bool = False
    started: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in
                ("id", "kind", "label", "status", "progress", "total", "message", "result", "error", "started")}


@dataclass
class EditRequest:
    book: str
    chapter: str
    workflow: str | None = None
    agent: str | None = None
    instructions: str = ""
    start: int | None = None  # optional selection within the chapter
    end: int | None = None
    provider: str | None = None  # override for every step
    model: str | None = None
    chunk_tokens: int | None = None
    overlap_tokens: int | None = None
    use_knowledge: bool = True


class Engine:
    def __init__(self, config_getter: Callable[[], Config], storage_getter: Callable[[], Storage]):
        self._config = config_getter
        self._storage = storage_getter
        self.jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task] = set()

    # ---------- helpers ----------
    def provider(self, name: str | None) -> Provider:
        cfg = self._config()
        name = name or cfg.default_provider
        if name not in cfg.providers:
            raise ProviderError(f"Unknown provider '{name}'")
        return make_provider(name, cfg.providers[name])

    def _steps(self, req: EditRequest) -> tuple[list[WorkflowStep], str]:
        cfg = self._config()
        if req.workflow:
            wf = cfg.workflows.get(req.workflow)
            if not wf:
                raise ValueError(f"Unknown workflow '{req.workflow}'")
            return wf.steps, f"Workflow: {wf.name}"
        if req.agent:
            if req.agent not in cfg.agents:
                raise ValueError(f"Unknown agent '{req.agent}'")
            return [WorkflowStep(agent=req.agent)], f"Agent: {req.agent}"
        raise ValueError("Choose an agent or a workflow")

    def _start(self, kind: str, label: str, coro_fn) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], kind=kind, label=label)
        self.jobs[job.id] = job

        async def runner():
            try:
                await coro_fn(job)
                if job.cancelled:
                    job.status = "cancelled"
                elif job.status == "running":
                    job.status = "done"
            except Exception as e:  # noqa: BLE001 - report any failure to the UI
                job.status = "error"
                job.error = str(e)

        task = asyncio.create_task(runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        # forget old jobs
        for old in sorted(self.jobs.values(), key=lambda j: j.started)[:-50]:
            self.jobs.pop(old.id, None)
        return job

    # ---------- editing ----------
    def start_edit(self, req: EditRequest) -> Job:
        steps, label = self._steps(req)  # validate before starting
        storage = self._storage()
        storage.read_chapter(req.book, req.chapter)
        return self._start("edit", label, lambda job: self._run_edit(job, req, steps, label))

    async def _run_edit(self, job: Job, req: EditRequest, steps: list[WorkflowStep], label: str) -> None:
        cfg, storage = self._config(), self._storage()
        text = storage.read_chapter(req.book, req.chapter)
        start = 0 if req.start is None else max(0, min(req.start, len(text)))
        end = len(text) if req.end is None else max(start, min(req.end, len(text)))
        region = text[start:end]
        if not region.strip():
            raise ValueError("There is no text to work on")
        cs = cfg.chunking
        agent_sizes = [cfg.agents[st.agent].chunk_tokens for st in steps if cfg.agents[st.agent].chunk_tokens]
        chunk_tokens = req.chunk_tokens or (min(agent_sizes) if agent_sizes else cs.chunk_tokens)
        overlap = cs.overlap_tokens if req.overlap_tokens is None else req.overlap_tokens
        chunks = chunk_text(region, chunk_tokens, overlap, cs.chars_per_token,
                            prefix=text[:start], suffix=text[end:])
        graph = storage.read_knowledge(req.book) if req.use_knowledge else kg.empty_graph()
        book_notes = storage.get_book(req.book).get("notes", "")
        job.total = len(chunks) * len(steps)

        results: list[dict[str, Any]] = []
        revised_so_far = text[:start]
        consecutive_errors = 0
        for ch in chunks:
            if job.cancelled:
                return
            current = ch.text
            notes: list[dict[str, str]] = []
            errors: list[str] = []
            before = revised_so_far[-len(ch.before):] if ch.before else ""
            for step in steps:
                agent = cfg.agents[step.agent]
                job.message = f"Part {ch.index + 1}/{len(chunks)}: {agent.name}"
                provider = self.provider(req.provider or step.provider or agent.provider)
                instructions = "\n".join(x for x in (req.instructions.strip(), step.instructions.strip()) if x)
                knowledge = kg.render_context(graph, before + current + ch.after,
                                              cs.knowledge_tokens * cs.chars_per_token) if req.use_knowledge else ""
                system = build_system(agent.system, before=before, after=ch.after,
                                      knowledge=knowledge, book_notes=book_notes)
                temperature = step.temperature if step.temperature is not None else agent.temperature
                model = req.model or step.model or agent.model
                extra = ""
                for attempt in range(2):
                    prompt = build_prompt(agent.task, instructions, current, mode=agent.mode, extra=extra)
                    try:
                        answer = await provider.complete(system, prompt, model=model, temperature=temperature)
                        consecutive_errors = 0
                    except ProviderError as e:
                        errors.append(f"{agent.name}: {e}")
                        consecutive_errors += 1
                        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS or len(chunks) == 1:
                            raise
                        break
                    if agent.mode != "edit":
                        notes += [{"agent": agent.name, "text": n} for n in parse_notes(answer)]
                        break
                    revised = clean_edit_output(answer, current, before, ch.after)
                    problem = check_answer(current, revised, agent, provider.truncated)
                    if problem is None:
                        current = revised
                        break
                    if attempt == 0:
                        job.message = f"Part {ch.index + 1}/{len(chunks)}: {agent.name} (retrying)"
                        if "paragraphs" in problem:
                            extra = RETRY_PARAGRAPHS
                        elif "more than expected" in problem:
                            extra = RETRY_LONG
                        else:
                            extra = RETRY_SHORT
                    else:
                        errors.append(f"{agent.name}: kept the original text for this part because {problem}. "
                                      f"Try a smaller chunk size or a larger model.")
                job.progress += 1
            revised_so_far += current
            segments = diff_segments(ch.text, current)
            results.append({"index": ch.index, "segments": segments, "notes": notes, "errors": errors})

        review = {
            "book": req.book,
            "chapter": req.chapter,
            "label": label + (f" — {req.instructions.strip()[:60]}" if req.instructions.strip() else ""),
            "instructions": req.instructions,
            "created": time.strftime("%Y-%m-%d %H:%M"),
            "status": "pending",
            "range": [start, end],
            "original": region,
            "chunks": results,
            "changes": sum(count_changes(c["segments"]) for c in results),
            "notes_count": sum(len(c["notes"]) for c in results),
        }
        job.result = {"review_id": storage.save_review(req.book, review)}
        job.message = f"{review['changes']} changes, {review['notes_count']} notes"

    # ---------- review ----------
    def apply_review(self, book: str, review_id: str, decisions: dict[str, dict[str, bool]] | None,
                     accept_all: bool = False) -> dict[str, Any]:
        storage = self._storage()
        review = storage.read_review(book, review_id)
        if review["status"] != "pending":
            raise Conflict(f"This review was already {review['status']}")
        decisions = decisions or {}
        new_region = "".join(
            apply_decisions(c["segments"],
                            {int(k): v for k, v in decisions.get(str(c["index"]), {}).items()},
                            default=accept_all)
            for c in review["chunks"]
        )
        text = storage.read_chapter(book, review["chapter"])
        s, e = review["range"]
        original = review["original"]
        if text[s:e] != original:
            if text.count(original) == 1:
                s = text.index(original)
                e = s + len(original)
            else:
                raise Conflict("The chapter was edited after this review was created, so the "
                               "changes can no longer be placed safely. Discard it and run again.")
        backup_dir = storage._book_dir(book) / "history"
        backup_dir.mkdir(exist_ok=True)
        (backup_dir / f"{review['chapter'][:-3]}.{time.strftime('%Y%m%d-%H%M%S')}.md").write_text(text, encoding="utf-8")
        storage.write_chapter(book, review["chapter"], text[:s] + new_region + text[e:])
        accepted = sum(1 for c in decisions.values() for v in c.values() if v) if not accept_all else review["changes"]
        review["status"] = "applied"
        review["accepted"] = accepted
        storage.save_review(book, review)
        return {"accepted": accepted, "total": review["changes"]}

    # ---------- knowledge graph ----------
    def start_knowledge(self, book: str, chapter: str | None, provider: str | None, model: str | None) -> Job:
        storage = self._storage()
        chapters = [chapter] if chapter else [c["id"] for c in storage.list_chapters(book)]
        for c in chapters:
            storage.read_chapter(book, c)
        label = "Build knowledge graph" + (f" from {chapter}" if chapter else "")
        return self._start("knowledge", label, lambda job: self._run_knowledge(job, book, chapters, provider, model))

    async def _run_knowledge(self, job: Job, book: str, chapters: list[str], provider_name: str | None,
                             model: str | None) -> None:
        cfg, storage = self._config(), self._storage()
        provider = self.provider(provider_name)
        cs = cfg.chunking
        work = []
        for c in chapters:
            text = storage.read_chapter(book, c)
            for ch in chunk_text(text, cs.chunk_tokens * 2, 0, cs.chars_per_token):
                if ch.text.strip():
                    work.append((c, ch))
        job.total = len(work)
        graph = storage.read_knowledge(book)
        failures = 0
        for i, (c, ch) in enumerate(work):
            if job.cancelled:
                return
            job.message = f"{c}: part {ch.index + 1}"
            known = ", ".join(e["name"] for e in graph["entities"][:150]) or "none yet"
            prompt = kg.EXTRACT_TASK.format(known=known) + "\n\n## Passage\n" + ch.text
            try:
                answer = await provider.complete(kg.EXTRACT_SYSTEM, prompt, model=model, temperature=0.1)
            except ProviderError:
                failures += 1
                if failures >= MAX_CONSECUTIVE_ERRORS:
                    raise
                job.progress = i + 1
                continue
            failures = 0
            kg.merge(graph, kg.parse_json_answer(answer))
            storage.write_knowledge(book, graph)
            job.progress = i + 1
        job.result = {"entities": len(graph["entities"]), "relations": len(graph["relations"])}
        job.message = f"{len(graph['entities'])} entities, {len(graph['relations'])} relations"

    def cancel(self, job_id: str) -> None:
        if job_id in self.jobs:
            self.jobs[job_id].cancelled = True
