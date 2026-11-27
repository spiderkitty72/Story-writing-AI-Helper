"""Splitting text into a rolling window of chunks.

The text is cut into contiguous chunks on paragraph (or, for very long
paragraphs, sentence) boundaries so that ``"".join(c.text for c in chunks)``
is exactly the original text. Each chunk also carries read-only context taken
from just before and just after it, so the AI sees what surrounds the part it
is editing without being allowed to change it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_PARA_SEP_RE = re.compile(r"(\n[ \t]*\n\s*)")
_SENT_RE = re.compile(r".+?(?:[.!?]+[\"'”’)\]]*\s+|$)", re.S)


@dataclass
class Chunk:
    index: int
    start: int  # offset within the region being processed
    end: int
    text: str
    before: str  # read-only context
    after: str  # read-only context


def estimate_tokens(text: str, chars_per_token: int = 4) -> int:
    return max(1, (len(text) + chars_per_token - 1) // chars_per_token) if text else 0


def _paragraphs(text: str) -> list[str]:
    bits = _PARA_SEP_RE.split(text)
    # bits alternates paragraph, separator, paragraph, ...; glue each
    # separator onto the paragraph before it.
    parts = [bits[i] + (bits[i + 1] if i + 1 < len(bits) else "") for i in range(0, len(bits), 2)]
    return [p for p in parts if p]


def _split_long(piece: str, max_chars: int) -> list[str]:
    if len(piece) <= max_chars:
        return [piece]
    sentences = [m.group(0) for m in _SENT_RE.finditer(piece) if m.group(0)]
    if "".join(sentences) != piece:  # fall back to hard splitting
        sentences = [piece]
    out: list[str] = []
    buf = ""
    for s in sentences:
        while len(s) > max_chars:  # one enormous "sentence"
            cut = s.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            if buf:
                out.append(buf)
                buf = ""
            out.append(s[:cut])
            s = s[cut:]
        if buf and len(buf) + len(s) > max_chars:
            out.append(buf)
            buf = ""
        buf += s
    if buf:
        out.append(buf)
    return out


def _tail(text: str, max_chars: int) -> str:
    if max_chars <= 0 or not text:
        return ""
    if len(text) <= max_chars:
        return text
    cut = text[-max_chars:]
    sp = cut.find(" ")
    return cut[sp + 1:] if 0 <= sp < 40 else cut


def _head(text: str, max_chars: int) -> str:
    if max_chars <= 0 or not text:
        return ""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    sp = cut.rfind(" ")
    return cut[:sp] if sp > len(cut) - 40 else cut


def chunk_text(
    text: str,
    chunk_tokens: int = 1200,
    overlap_tokens: int = 300,
    chars_per_token: int = 4,
    prefix: str = "",
    suffix: str = "",
) -> list[Chunk]:
    """Split ``text`` into chunks.

    ``prefix``/``suffix`` is surrounding text outside the region (for example
    the rest of the chapter when only a selection is edited); it is used only
    as context.
    """
    max_chars = max(200, chunk_tokens * chars_per_token)
    ctx_before = overlap_tokens * chars_per_token
    ctx_after = ctx_before // 2

    pieces: list[str] = []
    for p in _paragraphs(text):
        pieces.extend(_split_long(p, max_chars))

    groups: list[str] = []
    buf = ""
    for p in pieces:
        if buf and len(buf) + len(p) > max_chars:
            groups.append(buf)
            buf = ""
        buf += p
    if buf:
        groups.append(buf)

    chunks: list[Chunk] = []
    pos = 0
    for i, g in enumerate(groups):
        start, end = pos, pos + len(g)
        before_src = prefix + text[:start]
        after_src = text[end:] + suffix
        chunks.append(Chunk(i, start, end, g, _tail(before_src, ctx_before), _head(after_src, ctx_after)))
        pos = end
    return chunks
