"""Runs agents and workflows over a book, chunk by chunk, as background jobs."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from app import knowledge as kg
from app import tools
from app.auth import current_user
from app.chunking import chunk_text
from app.config import Config, WorkflowStep
from app.diffing import apply_decisions, count_changes, diff_segments
from app.prompts import (
    CHAT_SYSTEM,
    RETRY_EMPTY,
    RETRY_LONG,
    RETRY_PARAGRAPHS,
    RETRY_SHORT,
    build_chat_prompt,
    build_prompt,
    build_system,
    build_write_prompt,
    clean_chat_output,
    clean_edit_output,
    clean_write_output,
    count_paragraphs,
    join_insert,
    parse_notes,
)
from app.providers import Provider, ProviderError, make_provider
from app.storage import Conflict, Storage

MAX_CONSECUTIVE_ERRORS = 3
DEFAULT_LENGTH_RATIO = (0.5, 2.5)
DEFAULT_WRITE_WORDS = 250
ANCHOR_CHARS = 80


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
    owner: int | None = None  # id of the user who started it

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
    words: int | None = None  # length of new text for "write" agents


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

    def _story_so_far(self, book: str, graph: dict[str, Any], chapter: str | None) -> str:
        """Chapter summaries (AI summary, else the author's outline synopsis)."""
        cs = self._config().chunking
        storage = self._storage()
        chapters = storage.list_chapters(book)
        merged = {**tools.synopses(storage, book), **(graph.get("summaries") or {})}
        return kg.render_story_so_far({"summaries": merged}, chapters, cs.summary_tokens * cs.chars_per_token,
                                      until=chapter)

    def _plan(self, book: str, chapter: str | None) -> str:
        cs = self._config().chunking
        return tools.render_plan(tools.read_timeline(self._storage(), book), chapter,
                                 cs.knowledge_tokens * cs.chars_per_token // 2)

    def _start(self, kind: str, label: str, coro_fn) -> Job:
        user = current_user.get()
        job = Job(id=uuid.uuid4().hex[:10], kind=kind, label=label, owner=user.id if user else None)
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
        for old in sorted(self.jobs.values(), key=lambda j: j.started)[:-200]:
            self.jobs.pop(old.id, None)
        return job

    # ---------- editing ----------
    def start_edit(self, req: EditRequest) -> Job:
        steps, label = self._steps(req)  # validate before starting
        storage = self._storage()
        storage.read_chapter(req.book, req.chapter)
        if self._config().agents[steps[0].agent].mode == "write":
            return self._start("edit", label, lambda job: self._run_write(job, req, steps, label))
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
        story_so_far = self._story_so_far(req.book, graph, req.chapter) if req.use_knowledge else ""
        book_notes = storage.get_book(req.book).get("notes", "")
        job.total = len(chunks) * len(steps)

        results: list[dict[str, Any]] = []
        revised_so_far = text[:start]
        ctx = {"graph": graph, "book_notes": book_notes, "story_so_far": story_so_far,
               "plan": self._plan(req.book, req.chapter) if req.use_knowledge else "",
               "errors_in_a_row": 0, "single": len(chunks) == 1}
        for ch in chunks:
            if job.cancelled:
                return
            before = revised_so_far[-len(ch.before):] if ch.before else ""
            current, notes, errors = await self._apply_steps(
                job, req, steps, ch.text, before, ch.after, ctx, f"Part {ch.index + 1}/{len(chunks)}")
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

    async def _apply_steps(self, job: Job, req: EditRequest, steps: list[WorkflowStep], current: str,
                           before: str, after: str, ctx: dict[str, Any], where: str):
        """Run edit/report steps over one piece of text. Returns (text, notes, errors)."""
        cs = self._config().chunking
        notes: list[dict[str, str]] = []
        errors: list[str] = []
        for step in steps:
            agent = self._config().agents[step.agent]
            job.message = f"{where}: {agent.name}"
            provider = self.provider(req.provider or step.provider or agent.provider)
            instructions = "\n".join(x for x in (req.instructions.strip(), step.instructions.strip()) if x)
            knowledge = kg.render_context(ctx["graph"], before + current + after,
                                          cs.knowledge_tokens * cs.chars_per_token) if req.use_knowledge else ""
            system = build_system(agent.system, before=before, after=after, knowledge=knowledge,
                                  book_notes=ctx["book_notes"], story_so_far=ctx["story_so_far"],
                                  plan=ctx.get("plan", ""))
            temperature = step.temperature if step.temperature is not None else agent.temperature
            model = req.model or step.model or agent.model
            extra = ""
            for attempt in range(2):
                prompt = build_prompt(agent.task, instructions, current, mode=agent.mode, extra=extra)
                try:
                    answer = await provider.complete(system, prompt, model=model, temperature=temperature)
                    ctx["errors_in_a_row"] = 0
                except ProviderError as e:
                    errors.append(f"{agent.name}: {e}")
                    ctx["errors_in_a_row"] += 1
                    if ctx["errors_in_a_row"] >= MAX_CONSECUTIVE_ERRORS or ctx["single"]:
                        raise
                    break
                if agent.mode != "edit":
                    notes += [{"agent": agent.name, "text": n} for n in parse_notes(answer)]
                    break
                revised = clean_edit_output(answer, current, before, after)
                problem = check_answer(current, revised, agent, provider.truncated)
                if problem is None:
                    current = revised
                    break
                if attempt == 0:
                    job.message = f"{where}: {agent.name} (retrying)"
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
        return current, notes, errors

    # ---------- writing new text ----------
    async def _run_write(self, job: Job, req: EditRequest, steps: list[WorkflowStep], label: str) -> None:
        """Write new text at the cursor (``req.end``, else ``req.start``, else the chapter end)."""
        cfg, storage = self._config(), self._storage()
        cs = cfg.chunking
        text = storage.read_chapter(req.book, req.chapter)
        pos = req.end if req.end is not None else req.start
        pos = len(text) if pos is None else max(0, min(pos, len(text)))
        ctx_chars = cs.write_context_tokens * cs.chars_per_token
        before_full, after_full = text[:pos], text[pos:]
        before = before_full[-ctx_chars:]
        if len(before_full) > ctx_chars and "\n\n" in before:  # start on a paragraph
            before = before[before.index("\n\n") + 2:]
        after = after_full[: ctx_chars // 3]
        graph = storage.read_knowledge(req.book) if req.use_knowledge else kg.empty_graph()
        story_so_far = self._story_so_far(req.book, graph, req.chapter) if req.use_knowledge else ""
        book_notes = storage.get_book(req.book).get("notes", "")
        job.total = len(steps)

        agent = cfg.agents[steps[0].agent]
        step = steps[0]
        instructions = "\n".join(x for x in (req.instructions.strip(), step.instructions.strip()) if x)
        if "{instructions}" in agent.task and not agent.task.replace("{instructions}", "").strip() \
                and not instructions:
            raise ValueError("Type what you want the AI to write")
        words = req.words or agent.words or DEFAULT_WRITE_WORDS
        provider = self.provider(req.provider or step.provider or agent.provider)
        knowledge = kg.render_context(graph, before + after + "\n" + instructions,
                                      cs.knowledge_tokens * cs.chars_per_token) if req.use_knowledge else ""
        plan = self._plan(req.book, req.chapter) if req.use_knowledge else ""
        system = build_system(agent.system, before=before, after=after, knowledge=knowledge,
                              book_notes=book_notes, story_so_far=story_so_far, plan=plan,
                              where="the insertion point")
        temperature = step.temperature if step.temperature is not None else agent.temperature
        model = req.model or step.model or agent.model
        job.message = f"Writing: {agent.name}"
        errors: list[str] = []
        new = ""
        extra = ""
        for attempt in range(2):
            prompt = build_write_prompt(agent.task, instructions, words=words, has_before=bool(before.strip()),
                                        has_after=bool(after.strip()), extra=extra)
            answer = await provider.complete(system, prompt, model=model, temperature=temperature)
            new = clean_write_output(answer, before, after)
            if len(new.split()) >= 3:
                break
            job.message = f"Writing: {agent.name} (retrying)"
            extra = RETRY_EMPTY
        if len(new.split()) < 3:
            raise ValueError("The model did not write any new text. Try again or use a larger model.")
        if provider.truncated:
            errors.append(f"{agent.name}: the answer was cut off by the model's output limit, so the "
                          f"last sentence may be unfinished.")
        job.progress = 1
        notes: list[dict[str, str]] = []
        if len(steps) > 1:
            ctx = {"graph": graph, "book_notes": book_notes, "story_so_far": story_so_far, "plan": plan,
                   "errors_in_a_row": 0, "single": True}
            new, notes, more = await self._apply_steps(job, req, steps[1:], new, before, after, ctx, "New text")
            errors += more
        inserted = join_insert(before_full, new, after_full)
        segments = diff_segments("", inserted)
        review = {
            "book": req.book,
            "chapter": req.chapter,
            "label": label + (f" — {req.instructions.strip()[:60]}" if req.instructions.strip() else ""),
            "instructions": req.instructions,
            "created": time.strftime("%Y-%m-%d %H:%M"),
            "status": "pending",
            "kind": "insert",
            "range": [pos, pos],
            "original": "",
            "anchor": before_full[-ANCHOR_CHARS:],
            "anchor_after": after_full[:ANCHOR_CHARS],
            "chunks": [{"index": 0, "segments": segments, "notes": notes, "errors": errors}],
            "changes": count_changes(segments),
            "notes_count": len(notes),
        }
        job.result = {"review_id": storage.save_review(req.book, review)}
        job.message = f"Wrote {len(new.split())} words"

    def create_insert_review(self, book: str, chapter: str, position: int | None, new: str,
                             label: str = "Inserted from chat") -> str:
        """Turn text (e.g. a chat answer) into a review that inserts it at ``position``."""
        storage = self._storage()
        text = storage.read_chapter(book, chapter)
        pos = len(text) if position is None else max(0, min(position, len(text)))
        inserted = join_insert(text[:pos], new, text[pos:])
        if not inserted:
            raise ValueError("There is no text to insert")
        segments = diff_segments("", inserted)
        review = {
            "book": book, "chapter": chapter, "label": label, "instructions": "",
            "created": time.strftime("%Y-%m-%d %H:%M"), "status": "pending", "kind": "insert",
            "range": [pos, pos], "original": "",
            "anchor": text[:pos][-ANCHOR_CHARS:], "anchor_after": text[pos:][:ANCHOR_CHARS],
            "chunks": [{"index": 0, "segments": segments, "notes": [], "errors": []}],
            "changes": count_changes(segments), "notes_count": 0,
        }
        return storage.save_review(book, review)

    # ---------- chat ----------
    async def chat(self, book: str, chapter: str | None, position: int | None, history: list[dict[str, str]],
                   message: str, provider_name: str | None = None, model: str | None = None,
                   use_knowledge: bool = True) -> str:
        if not message.strip():
            raise ValueError("Type a message")
        cfg, storage = self._config(), self._storage()
        cs = cfg.chunking
        before = after = ""
        if chapter:
            text = storage.read_chapter(book, chapter)
            pos = len(text) if position is None else max(0, min(position, len(text)))
            ctx_chars = cs.write_context_tokens * cs.chars_per_token
            before, after = text[:pos][-ctx_chars:], text[pos:][: ctx_chars // 4]
        graph = storage.read_knowledge(book) if use_knowledge else kg.empty_graph()
        recent = " ".join(m.get("content", "") for m in history[-4:])
        knowledge = kg.render_context(graph, before + after + recent + "\n" + message,
                                      cs.knowledge_tokens * cs.chars_per_token) if use_knowledge else ""
        story = self._story_so_far(book, graph, None) if use_knowledge else ""
        plan = self._plan(book, chapter) if use_knowledge else ""
        system = build_system(CHAT_SYSTEM, before=before, after=after, knowledge=knowledge,
                              book_notes=storage.get_book(book).get("notes", ""), story_so_far=story,
                              plan=plan, where="the author's cursor")
        provider = self.provider(provider_name)
        answer = await provider.complete(system, build_chat_prompt(history, message), model=model, temperature=0.7)
        return clean_chat_output(answer)

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
        if review.get("kind") == "insert":
            s = e = self._find_insert_point(text, s, review.get("anchor", ""), review.get("anchor_after", ""))
        elif text[s:e] != original:
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

    @staticmethod
    def _find_insert_point(text: str, pos: int, anchor: str, anchor_after: str) -> int:
        """Where an insertion goes now, even if the chapter changed elsewhere."""
        if text[:pos].endswith(anchor) and text[pos:].startswith(anchor_after):
            return pos
        if anchor and text.count(anchor) == 1:
            return text.index(anchor) + len(anchor)
        if not anchor and text.startswith(anchor_after):
            return 0
        if anchor_after and text.count(anchor_after) == 1:
            return text.index(anchor_after)
        raise Conflict("The text around the insertion point was edited after this review was "
                       "created, so it can no longer be placed safely. Discard it and run again.")

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
        summaries = graph.setdefault("summaries", {})
        parts: dict[str, list[str]] = {}
        failures = 0
        for i, (c, ch) in enumerate(work):
            if job.cancelled:
                return
            job.message = f"{c}: part {ch.index + 1}"
            known = ", ".join(e["name"] for e in graph["entities"][:150]) or "none yet"
            prompt = kg.EXTRACT_TASK.format(known=known) + "\n\n## Passage\n" + ch.text
            provider.json_mode = True  # Ollama: force valid JSON, a big help for small models
            try:
                answer = await provider.complete(kg.EXTRACT_SYSTEM, prompt, model=model, temperature=0.1)
            except ProviderError:
                failures += 1
                if failures >= MAX_CONSECUTIVE_ERRORS:
                    raise
                job.progress = i + 1
                continue
            failures = 0
            data = kg.parse_json_answer(answer)
            kg.merge(graph, data)
            summary = data.get("summary")
            if isinstance(summary, str) and summary.strip():
                parts.setdefault(c, []).append(summary.strip())
                summaries[c] = " ".join(parts[c])  # a rebuilt chapter replaces its old summary
            storage.write_knowledge(book, graph)
            job.progress = i + 1
        job.result = {"entities": len(graph["entities"]), "relations": len(graph["relations"])}
        job.message = f"{len(graph['entities'])} entities, {len(graph['relations'])} relations"

    def cancel(self, job_id: str) -> None:
        if job_id in self.jobs:
            self.jobs[job_id].cancelled = True
