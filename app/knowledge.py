"""Knowledge graph ("story bible") of characters, places, objects and facts.

The graph is built by asking the AI to extract entities from each chunk of the
book, and can be edited by hand. When the AI edits a chunk, only the entities
that are mentioned near that chunk (plus any "pinned" ones) are summarised
into a compact block, so vital facts travel with every request while using
few tokens.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

ENTITY_TYPES = ["character", "place", "object", "event", "group", "concept"]

EXTRACT_SYSTEM = (
    "You extract a knowledge graph from book manuscripts. You answer with JSON only."
)

EXTRACT_TASK = """Read the passage and extract the important story knowledge:
characters, places, objects, groups, events and concepts, with short factual
notes (appearance, age, relationships, abilities, rules of the world, timeline
facts). Only record what the text states or clearly implies.

Already known entities (reuse these exact names when they appear): {known}

Answer with JSON in exactly this shape and nothing else:
{{"entities": [{{"name": "...", "type": "character|place|object|event|group|concept",
  "aliases": ["..."], "description": "one sentence", "facts": ["short fact", "..."]}}],
 "relations": [{{"source": "name", "target": "name", "label": "e.g. sister of"}}]}}"""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def empty_graph() -> dict[str, Any]:
    return {"entities": [], "relations": []}


def parse_json_answer(answer: str) -> dict[str, Any]:
    answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.S)
    i, j = answer.find("{"), answer.rfind("}")
    if i < 0 or j < i:
        return empty_graph()
    try:
        data = json.loads(answer[i: j + 1])
    except json.JSONDecodeError:
        return empty_graph()
    return data if isinstance(data, dict) else empty_graph()


def _find(graph: dict[str, Any], name: str) -> dict[str, Any] | None:
    n = _norm(name)
    for e in graph["entities"]:
        if _norm(e["name"]) == n or n in (_norm(a) for a in e.get("aliases", [])):
            return e
    return None


def normalize_entity(raw: dict[str, Any]) -> dict[str, Any]:
    t = str(raw.get("type") or "concept").lower()
    return {
        "id": raw.get("id") or uuid.uuid4().hex[:8],
        "name": str(raw.get("name") or "").strip(),
        "type": t if t in ENTITY_TYPES else "concept",
        "aliases": [str(a).strip() for a in raw.get("aliases") or [] if str(a).strip()],
        "description": str(raw.get("description") or "").strip(),
        "facts": [str(f).strip() for f in raw.get("facts") or [] if str(f).strip()],
        "pinned": bool(raw.get("pinned", False)),
    }


def merge(graph: dict[str, Any], extracted: dict[str, Any]) -> dict[str, Any]:
    """Merge newly extracted knowledge into ``graph`` (in place). Existing text wins."""
    graph.setdefault("entities", [])
    graph.setdefault("relations", [])
    for raw in extracted.get("entities") or []:
        if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
            continue
        new = normalize_entity(raw)
        cur = _find(graph, new["name"])
        if cur is None:
            graph["entities"].append(new)
            continue
        if not cur.get("description"):
            cur["description"] = new["description"]
        known = {_norm(f) for f in cur.setdefault("facts", [])}
        cur["facts"] += [f for f in new["facts"] if _norm(f) not in known]
        names = {_norm(cur["name"]), *(_norm(a) for a in cur.setdefault("aliases", []))}
        cur["aliases"] += [a for a in new["aliases"] if _norm(a) not in names]
    for rel in extracted.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        s, t = _find(graph, str(rel.get("source", ""))), _find(graph, str(rel.get("target", "")))
        label = str(rel.get("label") or "related to").strip()
        if not s or not t or s is t:
            continue
        if any(r["source"] == s["id"] and r["target"] == t["id"] and _norm(r["label"]) == _norm(label)
               for r in graph["relations"]):
            continue
        graph["relations"].append({"source": s["id"], "target": t["id"], "label": label})
    return graph


def _mentions(entity: dict[str, Any], text: str) -> int:
    count = 0
    for name in [entity["name"], *entity.get("aliases", [])]:
        if len(name) < 2:
            continue
        count += len(re.findall(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, flags=re.I))
    return count


def render_context(graph: dict[str, Any], text: str, max_chars: int) -> str:
    """Compact summary of the entities relevant to ``text``."""
    if not graph.get("entities") or max_chars <= 0:
        return ""
    scored = []
    for e in graph["entities"]:
        m = _mentions(e, text)
        if e.get("pinned") or m:
            scored.append((not e.get("pinned"), -m, e))
    scored.sort(key=lambda x: (x[0], x[1], x[2]["name"]))
    by_id = {e["id"]: e for e in graph["entities"]}
    chosen_ids: set[str] = set()
    lines: list[str] = []
    used = 0
    for _, _, e in scored:
        line = f"- {e['name']} ({e['type']})"
        if e.get("aliases"):
            line += f", also called {', '.join(e['aliases'])}"
        if e.get("description"):
            line += f": {e['description']}"
        if e.get("facts"):
            line += " Facts: " + "; ".join(e["facts"])
        if used + len(line) > max_chars:
            if not lines:
                lines.append(line[:max_chars])
            break
        lines.append(line)
        used += len(line) + 1
        chosen_ids.add(e["id"])
    for r in graph.get("relations", []):
        if r["source"] in chosen_ids and r["target"] in chosen_ids:
            line = f"- {by_id[r['source']]['name']} {r['label']} {by_id[r['target']]['name']}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
    return "\n".join(lines)
