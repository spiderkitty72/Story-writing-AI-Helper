import asyncio

from app import knowledge as kg
from app.chunking import chunk_text
from app.config import load_config
from app.diffing import apply_decisions, diff_segments
from app.prompts import build_prompt, clean_edit_output, extract_target, parse_notes

TEXT = """# Chapter One

Mara walked to the markt. The rain was falling.

She bought bread from Tomas, who was her brother.

""" + "\n\n".join(f"Paragraph {i} is here with some words to pad it out a bit more." for i in range(40)) + "\n"


def test_chunks_are_lossless_and_bounded():
    chunks = chunk_text(TEXT, chunk_tokens=100, overlap_tokens=20)
    assert "".join(c.text for c in chunks) == TEXT
    assert len(chunks) > 3
    assert all(len(c.text) <= 100 * 4 + 80 for c in chunks)
    assert chunks[0].before == ""
    assert chunks[1].before and TEXT[: chunks[1].start].endswith(chunks[1].before)
    assert chunks[-1].after == ""


def test_long_paragraph_is_split_on_sentences():
    para = " ".join(f"Sentence number {i} ends here." for i in range(200))
    chunks = chunk_text(para, chunk_tokens=50, overlap_tokens=0)
    assert "".join(c.text for c in chunks) == para
    assert len(chunks) > 5


def test_selection_uses_prefix_as_context():
    chunks = chunk_text("Edit me.", chunk_tokens=100, overlap_tokens=50, prefix="Earlier text. ", suffix=" Later.")
    assert chunks[0].before == "Earlier text. "
    assert chunks[0].after == " Later."


def test_diff_roundtrip_and_partial_accept():
    a = "Mara walked to the markt. The rain was falling."
    b = "Mara walked to the market. The rain fell."
    segs = diff_segments(a, b)
    assert apply_decisions(segs, default=False) == a
    assert apply_decisions(segs, default=True) == b
    changes = [s for s in segs if s["type"] == "change"]
    assert len(changes) == 2
    partial = apply_decisions(segs, {changes[0]["id"]: True})
    assert partial == "Mara walked to the market. The rain was falling."


def test_prompt_and_cleanup():
    p = build_prompt("Fix spelling. {instructions}", "Use UK spelling", "colour", before="x", knowledge="- Mara")
    assert "Use UK spelling" in p and "Story knowledge" in p
    assert extract_target(p) == "colour"
    assert clean_edit_output("Here is the corrected text:\n\n```\nHello world.\n```", "\nHelo world.\n\n") == "\nHello world.\n\n"
    assert clean_edit_output("", "keep") == "keep"
    assert parse_notes("- one\n- two\n  continued") == ["one", "two continued"]
    assert parse_notes("No issues found.") == []


def test_knowledge_merge_and_context():
    g = kg.empty_graph()
    kg.merge(g, {"entities": [{"name": "Mara", "type": "character", "facts": ["has a limp"]},
                              {"name": "Tomas", "type": "character", "description": "Baker"}],
                 "relations": [{"source": "Tomas", "target": "Mara", "label": "brother of"}]})
    kg.merge(g, {"entities": [{"name": "mara", "facts": ["has a limp", "age 30"], "aliases": ["Mar"]}]})
    assert len(g["entities"]) == 2
    mara = g["entities"][0]
    assert mara["facts"] == ["has a limp", "age 30"] and mara["aliases"] == ["Mar"]
    ctx = kg.render_context(g, "Mara and Tomas talk.", 1000)
    assert "Mara (character)" in ctx and "brother of" in ctx
    assert kg.render_context(g, "Nobody here.", 1000) == ""
    assert kg.parse_json_answer('Sure! {"entities": []} ok') == {"entities": []}


def test_default_config_loads():
    cfg = load_config()
    assert {"ollama", "claude", "gemini", "echo"} <= set(cfg.providers)
    assert {"spelling", "grammar", "logic", "expand", "custom"} <= set(cfg.agents)
    assert cfg.workflows["proofread"].steps[0].agent == "spelling"
