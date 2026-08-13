"""Unit tests for the chunking logic (pure functions, no I/O, no network)."""

from __future__ import annotations

from app.chunking import (
    MAX_CHARS,
    MIN_CHARS,
    RawChunk,
    chunk_text,
    detect_section,
    is_noise,
    split_paragraphs,
)


def _para(n_chars: int, ch: str = "x") -> str:
    """One paragraph of roughly n_chars printable characters."""
    word = ch * 9 + " "
    return (word * (n_chars // 10 + 1))[:n_chars].strip()


class TestDetectSection:
    def test_plain_item_heading(self):
        assert detect_section("Item 1. Business") == "Item 1. Business"

    def test_sub_item_and_case_insensitive(self):
        assert detect_section("ITEM 1A. Risk Factors") == "Item 1A. Risk Factors"

    def test_item_with_dash(self):
        assert detect_section("Item 7A — Quantitative Disclosures").startswith("Item 7A")

    def test_bare_item_number(self):
        assert detect_section("Item 9C.") == "Item 9C"

    def test_body_text_mentioning_item_is_not_heading(self):
        long_body = "Item 1 of this report describes " + _para(300)
        assert detect_section(long_body) is None

    def test_non_heading(self):
        assert detect_section("Revenue increased 8% year over year.") is None


class TestChunkText:
    def test_paragraphs_merge_toward_target(self):
        text = "\n\n".join(_para(400) for _ in range(10))
        chunks = chunk_text(text)
        assert all(len(c.text) <= MAX_CHARS for c in chunks)
        # 10 * 400 chars should merge into a handful of chunks, not 10
        assert 2 <= len(chunks) < 10

    def test_section_carries_over_and_boundary_flushes(self):
        text = (
            "Item 1. Business\n\n"
            + _para(600, "a")
            + "\n\n"
            + _para(600, "b")
            + "\n\nItem 1A. Risk Factors\n\n"
            + _para(600, "c")
        )
        chunks = chunk_text(text)
        sections = {c.section for c in chunks}
        assert "Item 1. Business" in sections
        assert "Item 1A. Risk Factors" in sections
        # no chunk mixes content from both sections
        for c in chunks:
            assert not ("aaa" in c.text and "ccc" in c.text)

    def test_tiny_fragments_dropped(self):
        text = "42\n\n" + _para(MIN_CHARS + 100)  # page number + real paragraph
        chunks = chunk_text(text)
        assert all(len(c.text) >= MIN_CHARS for c in chunks)

    def test_huge_paragraph_hard_split(self):
        text = _para(3 * MAX_CHARS)
        chunks = chunk_text(text)
        assert len(chunks) >= 3
        assert all(len(c.text) <= MAX_CHARS for c in chunks)

    def test_empty_input(self):
        assert chunk_text("") == []


class TestNoiseFilter:
    def test_xbrl_tag_soup_is_noise(self):
        soup = ("8605tsla:AutomotiveSegmentMember2024-01-012024-12-31000131"
                "8605tsla:EnergyGenerationAndStorageSegmentMember2025-01-01")
        assert is_noise(soup)

    def test_normal_prose_is_not_noise(self):
        assert not is_noise("Revenue increased 8% year over year, driven by Services.")

    def test_noise_paragraphs_excluded_from_chunks(self):
        soup = "x" * 300  # one unbroken 300-char run
        text = soup + "\n\n" + _para(600, "a")
        chunks = chunk_text(text)
        assert chunks and all("xxx" not in c.text for c in chunks)


def test_split_paragraphs_strips_and_drops_empties():
    parts = split_paragraphs("a\n\n\n  \n\nb\n\nc  ")
    assert parts == ["a", "b", "c"]
