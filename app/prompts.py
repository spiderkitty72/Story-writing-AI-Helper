"""Prompt building and cleanup of model answers."""
from __future__ import annotations

import re

TARGET_OPEN = "<<<TEXT"
TARGET_CLOSE = "TEXT>>>"

EDIT_RULES = (
    "Return ONLY the revised version of the text between <<<TEXT and TEXT>>>. "
    "Do not include the markers, the context sections, explanations, or any "
    "commentary. If nothing needs to change, return the text exactly as given."
)
REPORT_RULES = (
    "Return your notes as a bullet list, one issue per line starting with '- '. "
    "If you find no issues, reply exactly: No issues found."
)


def build_prompt(
    task: str,
    instructions: str,
    text: str,
    *,
    mode: str = "edit",
    before: str = "",
    after: str = "",
    knowledge: str = "",
    book_notes: str = "",
) -> str:
    task_text = task.replace("{instructions}", instructions.strip()).strip()
    if not task_text:
        task_text = "Improve the text."
    sections = []
    if book_notes.strip():
        sections.append(f"## About this book (style, theme, rules)\n{book_notes.strip()}")
    if knowledge.strip():
        sections.append(f"## Story knowledge (reference only)\n{knowledge.strip()}")
    if before.strip():
        sections.append(f"## Text just BEFORE the passage (context only, do not edit or repeat)\n{before}")
    if after.strip():
        sections.append(f"## Text just AFTER the passage (context only, do not edit or repeat)\n{after}")
    sections.append(f"## Your task\n{task_text}")
    sections.append(f"## Passage\n{TARGET_OPEN}\n{text}\n{TARGET_CLOSE}")
    sections.append(EDIT_RULES if mode == "edit" else REPORT_RULES)
    return "\n\n".join(sections)


def extract_target(prompt: str) -> str | None:
    """Pull the passage back out of a prompt (used by the echo provider)."""
    i = prompt.rfind(TARGET_OPEN + "\n")
    j = prompt.rfind("\n" + TARGET_CLOSE)
    if i < 0 or j < i:
        return None
    return prompt[i + len(TARGET_OPEN) + 1: j]


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\n(.*)\n```\s*$", re.S)
_PREAMBLE_RE = re.compile(
    r"^(here is|here's|sure[,!]|certainly[,!]|below is)[^\n]*:\s*\n+", re.I
)


def clean_edit_output(answer: str, original: str) -> str:
    """Strip wrappers models like to add, and keep the original's outer whitespace."""
    out = answer.replace("\r\n", "\n")
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S)  # reasoning models
    if TARGET_OPEN in out:
        out = out.split(TARGET_OPEN, 1)[1]
    if TARGET_CLOSE in out:
        out = out.split(TARGET_CLOSE, 1)[0]
    out = _PREAMBLE_RE.sub("", out.lstrip(), count=1)
    m = _FENCE_RE.match(out)
    if m and "```" not in original:
        out = m.group(1)
    core = out.strip()
    if not core:
        return original
    lead = original[: len(original) - len(original.lstrip())]
    trail = original[len(original.rstrip()):]
    return lead + core + trail


def parse_notes(answer: str) -> list[str]:
    answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.S).strip()
    if not answer or answer.lower().rstrip(".") == "no issues found":
        return []
    notes: list[str] = []
    for line in answer.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^([-*•]|\d+[.)])\s+", line):
            notes.append(re.sub(r"^([-*•]|\d+[.)])\s+", "", line))
        elif notes:
            notes[-1] += " " + line
        else:
            notes.append(line)
    return [n for n in notes if n.lower().rstrip(".") != "no issues found"]
