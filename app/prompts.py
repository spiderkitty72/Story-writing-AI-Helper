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


WRITE_MARK = "Write only the NEW text"
WRITE_RULES = (
    "Output rules:\n"
    "- " + WRITE_MARK + " that goes at the insertion point.\n"
    "- Do not repeat, summarise or rewrite the story text before or after it.\n"
    "- Keep the same tense, point of view, characters and style as the story.\n"
    "- Use blank lines between paragraphs.\n"
    "- Output only the story text itself: no title, no notes, no explanation."
)
RETRY_EMPTY = (
    "IMPORTANT: your previous answer did not contain any new story text. Write the new "
    "text now, as story prose, without repeating the text before it."
)


def build_system(
    agent_system: str,
    *,
    before: str = "",
    after: str = "",
    knowledge: str = "",
    book_notes: str = "",
    story_so_far: str = "",
    plan: str = "",
    where: str = "the passage",
) -> str:
    """System message: the agent's role plus labelled read-only background.

    ``where`` names what the background surrounds ("the passage" for edits,
    "the insertion point" when writing new text).
    """
    parts = [agent_system.strip()]
    bg = []
    if book_notes.strip():
        bg.append(f"About this book (style, theme, rules):\n{book_notes.strip()}")
    if story_so_far.strip():
        bg.append(f"The story so far (chapter summaries):\n{story_so_far.strip()}")
    if plan.strip():
        bg.append(f"The author's plan for this chapter (planned events):\n{plan.strip()}")
    if knowledge.strip():
        bg.append(f"Story knowledge (facts that must stay consistent):\n{knowledge.strip()}")
    if before.strip():
        bg.append(f"The story text that comes just BEFORE {where}:\n\"\"\"\n{before.strip()}\n\"\"\"")
    if after.strip():
        bg.append(f"The story text that comes just AFTER {where}:\n\"\"\"\n{after.strip()}\n\"\"\"")
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


def build_write_prompt(task: str, instructions: str, *, words: int, has_before: bool,
                       has_after: bool, extra: str = "") -> str:
    """User message for agents that write NEW text at an insertion point."""
    task_text = task.replace("{instructions}", instructions.strip()).strip()
    if not task_text:
        task_text = "Continue the story."
    where = []
    if has_before:
        where.append("Your text starts right after the text BEFORE ends, so pick up exactly where it stops.")
    if has_after:
        where.append("Your text must lead naturally into the text AFTER.")
    sections = [f"TASK:\n{task_text}"]
    if where:
        sections.append(" ".join(where))
    sections.append(f"Length: about {words} words.")
    sections.append(WRITE_RULES)
    if extra:
        sections.append(extra)
    sections.append("Now write the new text.")
    return "\n\n".join(sections)


def clean_write_output(answer: str, before: str = "", after: str = "") -> str:
    """Strip wrappers and any copy of the surrounding story from new text."""
    out = answer.replace("\r\n", "\n")
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
    first, _, rest = out.partition("\n")
    if rest.strip() and len(first) < 120 and (
        first.rstrip().endswith(":") or re.match(r"^(here is|here's|here are|below is|sure[,!.]|certainly[,!.])", first, re.I)
    ):
        out = rest.strip()
    m = _FENCE_RE.match(out)
    if m:
        out = m.group(1)
    m = _POSTSCRIPT_RE.search(out)
    if m:
        out = out[: m.start()]
    out = _strip_echo(out, "", before, after).strip()
    if out.startswith('"""') and out.endswith('"""'):
        out = out[3:-3].strip()
    # a model that repeats the last sentence of the text before
    b = before.strip()
    if b:
        last = re.split(r"(?<=[.!?\"\u201d])\s+", b)[-1].strip()
        if len(last) >= 20 and out.startswith(last):
            out = out[len(last):].lstrip()
    return out


def join_insert(before: str, new: str, after: str) -> str:
    """Add the whitespace needed to put ``new`` between ``before`` and ``after``."""
    new = new.strip()
    if not new:
        return ""
    paragraph_mode = not before or not after or before.endswith("\n") or after.startswith("\n")
    if paragraph_mode:
        if not before or before.endswith("\n\n"):
            lead = ""
        elif before.endswith("\n"):
            lead = "\n"
        else:
            lead = "\n\n"
        if not after:
            trail = "\n"
        elif after.startswith("\n\n"):
            trail = ""
        elif after.startswith("\n"):
            trail = "\n"
        else:
            trail = "\n\n"
    else:  # cursor in the middle of a line
        lead = "" if before[-1:].isspace() else " "
        trail = "" if after[:1].isspace() or after[:1] in ".,;:!?" else " "
    return lead + new + trail


CHAT_SYSTEM = (
    "You are a helpful writing partner for the author of the book described below. "
    "You answer questions about the story, brainstorm ideas, suggest names and plot "
    "twists, and draft text when asked. Base what you say about the existing story on "
    "the background. If the background does not say, tell the author you do not know "
    "instead of inventing facts about the story. Keep answers short and clear unless "
    "the author asks for a long piece of writing."
)


def build_chat_prompt(history: list[dict[str, str]], question: str, max_chars: int = 6000) -> str:
    """Flatten a short chat history into one user message (works with any provider)."""
    lines: list[str] = []
    used = 0
    for m in reversed(history):
        role = "Author" if m.get("role") == "user" else "You"
        text = str(m.get("content", "")).strip()
        if len(text) > 1500:
            text = text[:1500] + " [...]"
        if used + len(text) > max_chars:
            break
        lines.append(f"{role}: {text}")
        used += len(text)
    parts = []
    if lines:
        parts.append("CONVERSATION SO FAR:\n" + "\n\n".join(reversed(lines)))
    parts.append(f"THE AUTHOR NOW SAYS:\n{question.strip()}")
    parts.append("Reply to the author.")
    return "\n\n".join(parts)


def clean_chat_output(answer: str) -> str:
    return re.sub(r"<think>.*?</think>", "", answer.replace("\r\n", "\n"), flags=re.S).strip()


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
