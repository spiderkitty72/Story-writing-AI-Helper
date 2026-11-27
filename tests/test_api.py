"""End-to-end test of the API with a fake provider standing in for the AI."""
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import load_config
from app.prompts import REPORT_RULES, extract_target
from app.providers import PROVIDER_TYPES, Provider
from app.storage import Storage


class FakeProvider(Provider):
    type_name = "fake"

    async def complete(self, system, prompt, *, model=None, temperature=None):
        target = extract_target(prompt)
        if target is None:  # knowledge extraction
            return '{"entities": [{"name": "Mara", "type": "character", "facts": ["walks with a limp"]}], "relations": []}'
        if REPORT_RULES in prompt:
            return "- Mara cannot run if she has a limp."
        return target.replace("markt", "market")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    PROVIDER_TYPES["fake"] = FakeProvider
    cfg = load_config()
    cfg.providers = {"fake": {"type": "fake"}}
    cfg.default_provider = "fake"
    for a in cfg.agents.values():
        a.provider = None
    cfg.data_dir = tmp_path
    main.state.config = cfg
    main.state.storage = Storage(tmp_path)
    with TestClient(main.app) as c:
        yield c


def wait(c, job_id):
    for _ in range(100):
        j = c.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_full_flow(client):
    book = client.post("/api/books", json={"title": "Test Book"}).json()["id"]
    chapters = client.get(f"/api/books/{book}").json()["chapters"]
    ch = chapters[0]["id"]
    text = "# Chapter 1\n\nMara walked to the markt.\n\nShe ran home.\n"
    client.put(f"/api/books/{book}/chapters/{ch}", json={"text": text})

    job = client.post(f"/api/books/{book}/run", json={"chapter": ch, "workflow": "full_edit"}).json()
    job = wait(client, job["id"])
    assert job["status"] == "done", job
    rid = job["result"]["review_id"]
    review = client.get(f"/api/books/{book}/reviews/{rid}").json()
    assert review["changes"] == 1
    assert review["notes_count"] >= 1

    # Reject everything first? No: accept all and check the chapter changed.
    r = client.post(f"/api/books/{book}/reviews/{rid}/apply", json={"accept_all": True}).json()
    assert r["accepted"] == 1
    assert "market" in client.get(f"/api/books/{book}/chapters/{ch}").json()["text"]
    # cannot apply twice
    assert client.post(f"/api/books/{book}/reviews/{rid}/apply", json={}).status_code == 409


def test_selection_and_reject(client):
    book = client.post("/api/books", json={"title": "Sel"}).json()["id"]
    ch = client.get(f"/api/books/{book}").json()["chapters"][0]["id"]
    text = "the markt one. the markt two."
    client.put(f"/api/books/{book}/chapters/{ch}", json={"text": text})
    start = text.index("the markt two")
    job = wait(client, client.post(f"/api/books/{book}/run",
                                   json={"chapter": ch, "agent": "spelling", "start": start, "end": len(text)}).json()["id"])
    rid = job["result"]["review_id"]
    review = client.get(f"/api/books/{book}/reviews/{rid}").json()
    change_id = next(s["id"] for s in review["chunks"][0]["segments"] if s["type"] == "change")
    client.post(f"/api/books/{book}/reviews/{rid}/apply", json={"decisions": {"0": {str(change_id): True}}})
    assert client.get(f"/api/books/{book}/chapters/{ch}").json()["text"] == "the markt one. the market two."


def test_knowledge_build_and_export(client):
    book = client.post("/api/books", json={"title": "KG"}).json()["id"]
    ch = client.get(f"/api/books/{book}").json()["chapters"][0]["id"]
    client.put(f"/api/books/{book}/chapters/{ch}", json={"text": "# One\n\nMara limps.\n"})
    job = wait(client, client.post(f"/api/books/{book}/knowledge/build", json={}).json()["id"])
    assert job["status"] == "done"
    graph = client.get(f"/api/books/{book}/knowledge").json()
    assert graph["entities"][0]["name"] == "Mara"
    prev = client.post(f"/api/books/{book}/knowledge/preview", json={"text": "Mara smiled."}).json()
    assert "walks with a limp" in prev["context"]
    assert "Mara limps." in client.get(f"/api/books/{book}/export").text


def test_config_validation(client):
    bad = "agents:\n  x:\n    mode: nonsense\n"
    r = client.put("/api/config/files/agents.yaml", json={"text": bad})
    assert r.status_code == 400
    assert client.get("/api/config/files/nope.yaml").status_code == 400


def test_path_traversal_rejected(client):
    assert client.get("/api/books/..%2F..%2Fetc").status_code in (400, 404)
    book = client.post("/api/books", json={"title": "P"}).json()["id"]
    assert client.get(f"/api/books/{book}/chapters/..%2Fbook.yaml").status_code in (400, 404)


class LazyProvider(Provider):
    """Behaves like a small model that drops most of the passage."""

    type_name = "lazy"
    calls: list[str] = []

    async def complete(self, system, prompt, *, model=None, temperature=None):
        LazyProvider.calls.append(prompt)
        target = extract_target(prompt)
        if "IMPORTANT" in prompt and "fixed-on-retry" in system:
            return target.replace("markt", "market")
        return target.split("\n\n")[0]  # only the first paragraph


def _use_provider(client, type_name, system_marker=""):
    PROVIDER_TYPES[type_name] = LazyProvider
    main.state.config.providers = {type_name: {"type": type_name}}
    main.state.config.default_provider = type_name
    for a in main.state.config.agents.values():
        a.system += system_marker


def _chapter(client, text):
    book = client.post("/api/books", json={"title": "Guard"}).json()["id"]
    ch = client.get(f"/api/books/{book}").json()["chapters"][0]["id"]
    client.put(f"/api/books/{book}/chapters/{ch}", json={"text": text})
    return book, ch


STORY = "\n\n".join(f"Paragraph {i}: Mara walked to the markt and bought some bread there." for i in range(4)) + "\n"


def test_short_answer_is_retried_then_original_kept(client):
    _use_provider(client, "lazy")
    LazyProvider.calls = []
    book, ch = _chapter(client, STORY)
    job = wait(client, client.post(f"/api/books/{book}/run", json={"chapter": ch, "agent": "grammar"}).json()["id"])
    assert job["status"] == "done", job
    review = client.get(f"/api/books/{book}/reviews/{job['result']['review_id']}").json()
    assert review["changes"] == 0  # nothing of the story is deleted
    assert any("kept the original" in e for c in review["chunks"] for e in c["errors"])
    assert len(LazyProvider.calls) == 2 and "IMPORTANT" in LazyProvider.calls[1]


def test_retry_can_succeed(client):
    _use_provider(client, "lazy", system_marker=" fixed-on-retry")
    book, ch = _chapter(client, STORY)
    job = wait(client, client.post(f"/api/books/{book}/run", json={"chapter": ch, "agent": "grammar"}).json()["id"])
    review = client.get(f"/api/books/{book}/reviews/{job['result']['review_id']}").json()
    assert review["changes"] == 4
    assert not any(c["errors"] for c in review["chunks"])


def test_agent_chunk_size_is_used(client):
    PROVIDER_TYPES["fake"] = FakeProvider
    book, ch = _chapter(client, STORY * 20)
    job = wait(client, client.post(f"/api/books/{book}/run", json={"chapter": ch, "agent": "spelling"}).json()["id"])
    review = client.get(f"/api/books/{book}/reviews/{job['result']['review_id']}").json()
    chunk_tokens = main.state.config.agents["spelling"].chunk_tokens
    assert len(review["chunks"]) >= len(STORY * 20) // (chunk_tokens * 4)


def test_ollama_context_grows_to_fit_prompt():
    import asyncio
    import json as _json

    import httpx

    from app.providers import OllamaProvider

    seen = {}

    def handler(request):
        seen.update(_json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "ok"}, "done_reason": "length"})

    p = OllamaProvider("o", {"model": "m", "num_ctx": 2048, "num_predict": 1000})
    p._client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(p.complete("sys", "x" * 30000))
    assert seen["options"]["num_ctx"] >= 10000 + 1000
    assert p.truncated is True
