"""Prompt building and cleanup of model answers.

Prompts are laid out for small local models as well as large ones:

* Background (book notes, knowledge graph, the text just before and after the
  passage) goes into the *system* message, clearly labelled as reference
  material that must not be output. Keeping it out of the user message stops
  small models from editing or copying the surrounding text.
* The user message holds only the task, the passage between markers, and a
  short reminder of the output rules placed *after* the passage, where small
  models pay the most attention.
"""
from __future__ import annotations

import re

TARGET_OPEN = "<<<PASSAGE"
TARGET_CLOSE = "PASSAGE>>>"

EDIT_RULES = (
    "Output rules:\n"
    "- Output the COMPLETE revised passage, from its first sentence to its last sentence.\n"
    "- Keep the same paragraphs, in the same order, separated by blank lines.\n"
    "- Output only the passage itself: no markers, no title, no notes, no explanation."
)
REPORT_RULES = (
    "Output rules:\n"
    "- Write your notes as a bullet list, one issue per line starting with '- '.\n"
    "- If you find no issues, reply exactly: No issues found."
)
RETRY_SHORT = (
    "IMPORTANT: your previous answer was much shorter than the passage and left text out. "
    "This time output every paragraph of the passage, revised as asked."
)
RETRY_LONG = (
    "IMPORTANT: your previous answer was much longer than expected. Revise only the passage "
    "between the markers. Do not continue the story and do not repeat the surrounding text."
)
RETRY_PARAGRAPHS = (
    "IMPORTANT: your previous answer changed the number of paragraphs. Keep exactly the same "
    "paragraphs as the passage and only correct the text inside them."
)


def build_system(
    agent_system: str,
    *,
    before: str = "",
    after: str = "",
    knowledge: str = "",
    book_notes: str = "",
) -> str:
    parts = [agent_system.strip()]
    bg = []
    if book_notes.strip():
        bg.append(f"About this book (style, theme, rules):\n{book_notes.strip()}")
    if knowledge.strip():
        bg.append(f"Story knowledge (facts that must stay consistent):\n{knowledge.strip()}")
    if before.strip():
        bg.append(f"The story text that comes just BEFORE the passage:\n\"\"\"\n{before.strip()}\n\"\"\"")
    if after.strip():
        bg.append(f"The story text that comes just AFTER the passage:\n\"\"\"\n{after.strip()}\n\"\"\"")
    if bg:
        parts.append(
            "BACKGROUND (reference only). Use it to stay consistent. Never edit it, never "
            "repeat it, never include it in your answer.\n\n" + "\n\n".join(bg)
        )
    return "\n\n".join(p for p in parts if p)


def build_prompt(task: str, instructions: str, text: str, *, mode: str = "edit", extra: str = "") -> str:
    task_text = task.replace("{instructions}", instructions.strip()).strip() or "Improve the text."
    rules = EDIT_RULES if mode == "edit" else REPORT_RULES
    sections = [
        f"TASK:\n{task_text}",
        f"PASSAGE:\n{TARGET_OPEN}\n{text.strip()}\n{TARGET_CLOSE}",
        rules,
    ]
    if extra:
        sections.append(extra)
    if mode == "edit":
        sections.append("Now write the revised passage.")
    return "\n\n".join(sections)


def extract_target(prompt: str) -> str | None:
    """Pull the passage back out of a prompt (used by the echo provider)."""
    i = prompt.rfind(TARGET_OPEN + "\n")
    j = prompt.rfind("\n" + TARGET_CLOSE)
    if i < 0 or j < i:
        return None
    return prompt[i + len(TARGET_OPEN) + 1: j]


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\n(.*?)\n```\s*$", re.S)
_PREAMBLE_RE = re.compile(
    r"^(here is|here's|here are|sure[,!.]|certainly[,!.]|below is|okay[,!.]|ok[,!.]|revised passage|"
    r"the revised|corrected passage|the corrected)[^\n]*\n+",
    re.I,
)
_POSTSCRIPT_RE = re.compile(
    r"\n+[ \t]*(\*\*)?(notes?|changes( made)?|corrections( made)?|explanation|edits( made)?)(\*\*)?[ \t]*:.*$",
    re.I | re.S,
)


def _strip_echo(out: str, original: str, before: str, after: str) -> str:
    """Remove copies of the surrounding context that a model pasted into its answer."""
    b_tail = before.strip()[-60:]
    if len(b_tail) >= 25 and b_tail not in original:
        k = out.find(b_tail)
        if k >= 0:
            out = out[k + len(b_tail):]
    a_head = after.strip()[:60]
    if len(a_head) >= 25 and a_head not in original:
        k = out.find(a_head)
        if k > 0:
            out = out[:k]
    return out


def clean_edit_output(answer: str, original: str, before: str = "", after: str = "") -> str:
    """Strip wrappers models like to add, and keep the original's outer whitespace."""
    out = answer.replace("\r\n", "\n")
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S)  # reasoning models
    for marker in (TARGET_OPEN, "<<<TEXT"):
        if marker in out:
            out = out.split(marker, 1)[1]
    for marker in (TARGET_CLOSE, "TEXT>>>"):
        if marker in out:
            out = out.split(marker, 1)[0]
    out = out.lstrip()
    m = _PREAMBLE_RE.match(out)
    if m and m.group(0).strip() not in original:
        out = out[m.end():]
    m = _FENCE_RE.match(out)
    if m and "```" not in original:
        out = m.group(1)
    m = _POSTSCRIPT_RE.search(out)
    if m and m.group(0).strip().splitlines()[0] not in original:
        out = out[: m.start()]
    out = _strip_echo(out, original, before, after)
    core = out.strip()
    if core.startswith('"""') and core.endswith('"""'):
        core = core[3:-3].strip()
    if not core:
        return original
    lead = original[: len(original) - len(original.lstrip())]
    trail = original[len(original.rstrip()):]
    return lead + core + trail


def count_paragraphs(text: str) -> int:
    return len([p for p in re.split(r"\n[ \t]*\n", text.strip()) if p.strip()])


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
