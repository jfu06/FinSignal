"""Paragraph/section chunking of 10-K plain text (design-doc §4).

Pure functions, no I/O — covered by unit tests. Strategy:

- Split the filing text into paragraphs on blank lines.
- Track the current section via ``Item N.`` headings (``Item 1A.`` etc.), so
  every chunk carries the section it came from.
- Greedily pack consecutive paragraphs into chunks of roughly
  ``TARGET_CHARS`` (never crossing a section boundary), because single
  paragraphs are often too small to be a useful retrieval unit.
- Drop tiny fragments (page numbers, table debris) below ``MIN_CHARS``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Chunk sizing (characters). ~1500 chars ≈ 300-400 tokens per chunk.
TARGET_CHARS = 1500
MAX_CHARS = 2400
MIN_CHARS = 200

# "Item 1.", "Item 1A.", "ITEM 7A —", "Item 9C." as a standalone heading line
# (optionally with a short title after it).
_ITEM_RE = re.compile(r"^\s*item\s+(\d{1,2}[A-C]?)\s*[.:—-]?\s*(.{0,120})$", re.IGNORECASE)

# Inline-XBRL debris: unbroken 80+ char runs (context refs, member tags, GUIDs)
# that survive HTML stripping. Real filing prose never contains these.
_NOISE_RUN_RE = re.compile(r"\S{80,}")


@dataclass
class RawChunk:
    """A chunk before ids/embeddings are attached."""

    section: str | None
    text: str


# Standard 10-K section names some filers (Intel) print WITHOUT the
# "Item N." prefix — the body uses bare headings / running page headers.
# Exact whole-line matches only; used by the second chunking pass when the
# Item-prefix pass leaves the document essentially unlabeled.
_BARE_HEADINGS = {
    "business": "Item 1. Business",
    "risk factors": "Item 1A. Risk Factors",
    "unresolved staff comments": "Item 1B. Unresolved Staff Comments",
    "properties": "Item 2. Properties",
    "legal proceedings": "Item 3. Legal Proceedings",
    "management's discussion and analysis of financial condition and "
    "results of operations":
        "Item 7. Management's Discussion and Analysis",
    "quantitative and qualitative disclosures about market risk":
        "Item 7A. Quantitative and Qualitative Disclosures About Market Risk",
    "financial statements and supplementary data":
        "Item 8. Financial Statements and Supplementary Data",
    "controls and procedures": "Item 9A. Controls and Procedures",
}


def detect_section(paragraph: str, bare_headings: bool = False) -> str | None:
    """Return a normalized section label if the paragraph is an Item heading."""

    # Headings are short; a 3-page paragraph starting with "Item 1..." is body.
    first_line = paragraph.strip().splitlines()[0] if paragraph.strip() else ""
    if len(first_line) > 150:
        return None
    m = _ITEM_RE.match(first_line)
    if not m:
        if bare_headings:
            key = first_line.strip().rstrip(".:").lower().replace("’", "'")
            return _BARE_HEADINGS.get(key)
        return None
    number = m.group(1).upper()
    title = m.group(2).strip().rstrip(".")
    label = f"Item {number}"
    if title:
        label += f". {title}"
    return label


def is_noise(paragraph: str) -> bool:
    """True for XBRL tag-soup fragments that would pollute retrieval."""

    return bool(_NOISE_RUN_RE.search(paragraph))


def split_paragraphs(text: str) -> list[str]:
    """Split on blank lines; keep non-empty stripped paragraphs."""

    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def chunk_text(text: str) -> list[RawChunk]:
    """Chunk one filing's plain text into section-tagged chunks.

    Two passes: the Item-prefix pass first. If it leaves the document
    essentially unlabeled (Intel prints bare headings, no "Item 1A."
    prefixes in the body), rechunk recognizing known bare section names.
    The second pass only ever runs on documents the first pass failed,
    so normally-formatted filings keep byte-identical chunk boundaries.
    """

    chunks = _chunk_text_pass(text, bare_headings=False)
    labeled = sum(1 for c in chunks if c.section)
    if chunks and labeled / len(chunks) < 0.3:
        rechunked = _chunk_text_pass(text, bare_headings=True)
        relabeled = sum(1 for c in rechunked if c.section)
        if relabeled > labeled:
            return rechunked
    return chunks


def _chunk_text_pass(text: str, bare_headings: bool) -> list[RawChunk]:
    chunks: list[RawChunk] = []
    section: str | None = None
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            merged = "\n\n".join(buf)
            if len(merged) >= MIN_CHARS:
                chunks.append(RawChunk(section=section, text=merged))
            buf, buf_len = [], 0

    for para in split_paragraphs(text):
        if is_noise(para):
            continue
        new_section = detect_section(para, bare_headings=bare_headings)
        if new_section:
            flush()
            section = new_section
            # heading itself is kept: it gives the chunk useful context
        # very long single paragraph: hard-split so no chunk exceeds MAX_CHARS
        pieces = (
            [para[i : i + MAX_CHARS] for i in range(0, len(para), MAX_CHARS)]
            if len(para) > MAX_CHARS
            else [para]
        )
        for piece in pieces:
            if buf_len + len(piece) > TARGET_CHARS and buf:
                flush()
            buf.append(piece)
            buf_len += len(piece)
    flush()
    return chunks
