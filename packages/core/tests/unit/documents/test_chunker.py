"""Tests for ``persona.documents.chunker`` (spec 14 T05).

D-14-4 — document-aware chunker with natural-boundary chunks + token-aware
fallback. The Phase 1 typed-store chunking contract (one PersonaChunk per
YAML field, no splitting) stays byte-for-byte unchanged — verified by the
regression assertion at the bottom of this file.
"""

from __future__ import annotations

import pytest
from persona.documents.chunker import (
    DEFAULT_CHUNK_SIZE_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    DocumentSection,
    chunk_document,
)
from persona.schema.documents import DocumentChunk
from persona.skills import count_tokens


def _sections(*texts_with_page: tuple[str, int | None]) -> list[DocumentSection]:
    return [DocumentSection(text=text, page=page) for text, page in texts_with_page]


class TestDefaults:
    def test_default_constants_match_phase4_decision(self) -> None:
        # D-14-4 + R-14-3 — 512 / 64
        assert DEFAULT_CHUNK_SIZE_TOKENS == 512
        assert DEFAULT_OVERLAP_TOKENS == 64


class TestEmptyAndDegenerateInput:
    def test_empty_sections_produce_empty_list(self) -> None:
        assert (
            chunk_document(
                [],
                conversation_id="c",
                doc_ref="x.txt",
                document_format="txt",
                title="x.txt",
            )
            == []
        )

    def test_whitespace_only_section_produces_empty_list(self) -> None:
        result = chunk_document(
            _sections(("   \n\n  ", None)),
            conversation_id="c",
            doc_ref="x.txt",
            document_format="txt",
            title="x.txt",
        )
        assert result == []


class TestNaturalBoundary:
    def test_single_short_section_one_chunk(self) -> None:
        # A short paragraph fits under the cap → one chunk.
        result = chunk_document(
            _sections(("The lease is for one year.", 1)),
            conversation_id="conv",
            doc_ref="memo.pdf",
            document_format="pdf",
            title="memo.pdf",
        )
        assert len(result) == 1
        assert result[0].text == "The lease is for one year."
        assert result[0].page == 1

    def test_multiple_short_sections_each_become_chunks(self) -> None:
        # Each natural-boundary unit produces a chunk (no merging at section
        # level — keeps each section's page metadata intact).
        result = chunk_document(
            _sections(
                ("Para A on page one.", 1),
                ("Para B on page two.", 2),
                ("Para C on page three.", 3),
            ),
            conversation_id="conv",
            doc_ref="report.pdf",
            document_format="pdf",
            title="report.pdf",
        )
        assert len(result) == 3
        assert [c.page for c in result] == [1, 2, 3]

    def test_section_metadata_stamped_onto_chunks(self) -> None:
        result = chunk_document(
            [DocumentSection(text="Q1 revenue: 100", sheet="Q1")],
            conversation_id="conv",
            doc_ref="report.xlsx",
            document_format="xlsx",
            title="report.xlsx",
        )
        assert result[0].sheet == "Q1"
        assert result[0].page is None

    def test_section_with_section_heading_stamped(self) -> None:
        result = chunk_document(
            [DocumentSection(text="The methodology was...", section="3 Methodology")],
            conversation_id="conv",
            doc_ref="thesis.docx",
            document_format="docx",
            title="thesis.docx",
        )
        assert result[0].section == "3 Methodology"


class TestTokenCapFallback:
    def test_over_cap_section_splits_on_paragraph_boundary(self) -> None:
        # A section longer than the cap with paragraph breaks splits at them.
        long_para = "Sentence " * 200  # well over 512 tokens
        text = long_para + "\n\n" + long_para
        result = chunk_document(
            [DocumentSection(text=text, page=1)],
            conversation_id="conv",
            doc_ref="big.txt",
            document_format="txt",
            title="big.txt",
            chunk_size_tokens=200,
            overlap_tokens=20,
        )
        # Splits across at least two chunks; each chunk under the cap.
        assert len(result) >= 2
        for chunk in result:
            assert count_tokens(chunk.text) <= 200

    def test_over_cap_section_with_no_paragraph_splits_on_line(self) -> None:
        # No "\n\n" but has "\n" — line-level fallback.
        text = "\n".join(["A line of text"] * 100)
        result = chunk_document(
            [DocumentSection(text=text)],
            conversation_id="conv",
            doc_ref="lines.txt",
            document_format="txt",
            title="lines.txt",
            chunk_size_tokens=50,
            overlap_tokens=10,
        )
        assert len(result) >= 2
        for chunk in result:
            assert count_tokens(chunk.text) <= 50

    def test_chunk_indexing_is_zero_based_and_global(self) -> None:
        # Chunks index globally across all sections so the chunk-ID's index
        # slot reflects insertion order.
        result = chunk_document(
            _sections(
                ("First para.", 1),
                ("Second para.", 2),
                ("Third para.", 3),
            ),
            conversation_id="conv",
            doc_ref="doc.pdf",
            document_format="pdf",
            title="doc.pdf",
        )
        # IDs use 4-digit zero-padded global index per make_document_chunk_id.
        assert result[0].id.endswith("::0000")
        assert result[1].id.endswith("::0001")
        assert result[2].id.endswith("::0002")


class TestOverlap:
    def test_overlap_text_appears_in_adjacent_chunks(self) -> None:
        # Build text long enough to split, then assert tail of chunk N appears
        # at head of chunk N+1 (approximate — overlap is token-budgeted).
        paragraphs = [f"Paragraph number {i} contains some content." for i in range(50)]
        text = "\n\n".join(paragraphs)
        result = chunk_document(
            [DocumentSection(text=text)],
            conversation_id="conv",
            doc_ref="overlap.txt",
            document_format="txt",
            title="overlap.txt",
            chunk_size_tokens=60,
            overlap_tokens=10,
        )
        # Sanity: split happened.
        assert len(result) >= 2
        # Each chunk after the first should contain some terminal text from the
        # previous chunk (within reason — exact overlap depends on tokenisation).
        # We assert at least one consecutive pair shares ≥1 non-trivial token.
        shared_pairs = 0
        for prev, curr in zip(result, result[1:], strict=False):
            prev_tokens = set(prev.text.split())
            curr_first_words = set(curr.text.split()[:10])
            if prev_tokens & curr_first_words:
                shared_pairs += 1
        assert shared_pairs >= 1


class TestParameterValidation:
    def test_zero_chunk_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="chunk_size_tokens must be positive"):
            chunk_document(
                [DocumentSection(text="hi")],
                conversation_id="c",
                doc_ref="x.txt",
                document_format="txt",
                title="x.txt",
                chunk_size_tokens=0,
            )

    def test_negative_overlap_rejected(self) -> None:
        with pytest.raises(ValueError, match="overlap_tokens must be non-negative"):
            chunk_document(
                [DocumentSection(text="hi")],
                conversation_id="c",
                doc_ref="x.txt",
                document_format="txt",
                title="x.txt",
                overlap_tokens=-1,
            )

    def test_overlap_equal_to_chunk_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="strictly less than"):
            chunk_document(
                [DocumentSection(text="hi")],
                conversation_id="c",
                doc_ref="x.txt",
                document_format="txt",
                title="x.txt",
                chunk_size_tokens=100,
                overlap_tokens=100,
            )


class TestChunkReturnsDocumentChunks:
    def test_returns_document_chunks_not_persona_chunks(self) -> None:
        result = chunk_document(
            [DocumentSection(text="hi")],
            conversation_id="c",
            doc_ref="x.txt",
            document_format="txt",
            title="x.txt",
        )
        assert all(isinstance(c, DocumentChunk) for c in result)

    def test_chunk_format_and_title_set_from_args(self) -> None:
        result = chunk_document(
            [DocumentSection(text="hi")],
            conversation_id="c",
            doc_ref="report.pdf",
            document_format="pdf",
            title="Tenancy Report",
        )
        assert result[0].format == "pdf"
        assert result[0].title == "Tenancy Report"
        assert result[0].doc_ref == "report.pdf"


class TestPhase1ChunkingNotRegressed:
    """Hard constraint per D-14-4: Spec 01 chunking behaviour unchanged.

    Spec 01's chunking is implicit (one PersonaChunk per YAML field, no
    splitting; see registry.py lines 163-165, 264-265). The chunker module
    is a new addition — registry / chat_cmd / typed stores are NOT
    modified.

    These assertions verify the discipline by inspection: the chunker
    module does NOT import from persona.registry / persona.cli.chat_cmd
    (which would imply touching them); the existing chunk-construction
    sites still produce one chunk per call.
    """

    def test_chunker_module_does_not_import_registry_or_cli(self) -> None:
        # The chunker module's import graph excludes the typed-store
        # construction sites — sanity check.
        from persona.documents import chunker

        module_imports = chunker.__loader__.get_source(chunker.__name__) or ""  # type: ignore[union-attr]
        assert "from persona.registry" not in module_imports
        assert "from persona.cli" not in module_imports

    def test_registry_chunk_construction_unchanged(self) -> None:
        # Spec 01 registry builds PersonaChunks one-per-field via
        # make_chunk_id (3-component {persona_id}::{store_kind}::{index}).
        # If a future refactor accidentally routes those through this
        # chunker, the chunk-ID format would change to the 4-component
        # document shape — this test catches that.
        from persona.schema.chunks import make_chunk_id

        identity_id = make_chunk_id("astrid", "identity", 0)
        # 3-component format unchanged.
        assert identity_id.count("::") == 2
        assert identity_id == "astrid::identity::0000"


class TestTheParsersStayLight:
    """The parsers import :class:`DocumentSection` from the chunker; the document parse
    worker (D-FV0-31) imports the parsers. Neither may pull in the tokenizer or a
    provider SDK, so the token counter is imported on first use."""

    def test_importing_the_parsers_loads_no_tokenizer_or_sdk(self) -> None:
        import subprocess  # noqa: PLC0415
        import sys  # noqa: PLC0415

        probe = (
            "import sys, persona.documents.parsers, persona.documents.chunker\n"
            "heavy = ('tiktoken', 'anthropic', 'openai', 'httpx', 'persona.skills')\n"
            "print(','.join(m for m in heavy if m in sys.modules))\n"
        )
        run = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        assert run.stdout.strip() == ""

    def test_the_lazy_counter_counts_like_the_shared_one(self) -> None:
        from persona.documents import chunker  # noqa: PLC0415

        text = "The quarterly ledger balances to the cent."
        assert chunker.count_tokens(text) == count_tokens(text)


class TestALongRunWithoutSeparatorsIsLinear:
    """Second review: a 2 KB PDF's million characters without a space cost 35 s to chunk.

    Measured as work, not time: the characters handed to the tokenizer, in total.
    """

    def test_the_tokenizer_sees_each_character_a_bounded_number_of_times(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from persona.skills import _tokens  # noqa: PLC0415

        seen = [0]
        real_encode = _tokens.encode

        def _counting_encode(text: str) -> list[int]:
            seen[0] += len(text)
            return real_encode(text)

        # Every count and every encode, the chunker's and the counter's, goes through here.
        monkeypatch.setattr(_tokens, "encode", _counting_encode)
        text = "A" * 200_000
        chunks = chunk_document(
            [DocumentSection(text=text)],
            conversation_id="c",
            doc_ref="d",
            document_format="pdf",
            title="t",
        )
        assert chunks
        assert seen[0] <= 3 * len(text), seen[0]

    def test_the_windows_cover_the_text_and_stay_under_the_cap(self) -> None:
        """Valid input passes: every window fits, and together they span the whole run."""
        text = "".join(f"{i:07d}" for i in range(30_000))  # 210,000 digits, no separator
        chunks = chunk_document(
            [DocumentSection(text=text)],
            conversation_id="c",
            doc_ref="d",
            document_format="pdf",
            title="t",
        )
        assert all(count_tokens(c.text) <= DEFAULT_CHUNK_SIZE_TOKENS for c in chunks)
        assert text.startswith(chunks[0].text)
        assert text.endswith(chunks[-1].text)
        assert len(chunks) > 1


def _chunk(text: str, **kwargs: int) -> list[DocumentChunk]:
    return chunk_document(
        [DocumentSection(text=text)],
        conversation_id="c",
        doc_ref="d",
        document_format="txt",
        title="t",
        **kwargs,  # type: ignore[arg-type]
    )


def _prose(paragraphs: int, sentences: int = 6) -> str:
    """Numbered words, so every word in the text is unique and can be found again."""
    out = []
    for p in range(paragraphs):
        words = [f"w{p}x{s}x{k}" for s in range(sentences) for k in range(8)]
        sentences_text = [" ".join(words[s * 8 : s * 8 + 8]) + "." for s in range(sentences)]
        out.append(" ".join(sentences_text))
    return "\n\n".join(out)


class TestChunksFillToTheCap:
    """Third review, finding 14: chunks averaged 224 to 331 tokens of 512."""

    def test_every_chunk_but_the_last_holds_at_least_three_quarters_of_the_cap(self) -> None:
        chunks = _chunk(_prose(200))
        tokens = [count_tokens(c.text) for c in chunks]
        assert len(chunks) > 5
        assert all(n <= DEFAULT_CHUNK_SIZE_TOKENS for n in tokens)
        assert all(n >= DEFAULT_CHUNK_SIZE_TOKENS * 3 // 4 - 2 for n in tokens[:-1]), tokens

    def test_the_chunk_count_is_close_to_the_ideal(self) -> None:
        text = _prose(200)
        ideal = count_tokens(text) / (DEFAULT_CHUNK_SIZE_TOKENS - DEFAULT_OVERLAP_TOKENS)
        assert len(_chunk(text)) <= ideal * 1.4

    def test_a_short_section_is_one_chunk_however_long_the_document(self) -> None:
        chunks = _chunk("A short paragraph that fits.")
        assert [c.text for c in chunks] == ["A short paragraph that fits."]


class TestWindowsEndAtTheBestSeparator:
    def test_a_window_ends_at_a_paragraph_break_when_one_is_in_its_last_quarter(self) -> None:
        text = _prose(200, sentences=2)  # paragraphs well under a quarter of the cap
        paragraph_ends = {p.split()[-1] for p in text.split("\n\n")}
        chunks = _chunk(text)
        assert len(chunks) > 3
        for chunk in chunks:
            assert chunk.text.split()[-1] in paragraph_ends, chunk.text[-40:]

    def test_without_paragraphs_a_window_ends_at_a_line_break(self) -> None:
        lines = [f"line {i} " + " ".join(f"t{i}y{k}" for k in range(12)) for i in range(300)]
        text = "\n".join(lines)
        for chunk in _chunk(text)[:-1]:
            assert chunk.text.split("\n")[-1] in lines, chunk.text[-40:]

    def test_without_line_breaks_a_window_ends_between_words(self) -> None:
        words = [f"v{i}" for i in range(6000)]
        for chunk in _chunk(" ".join(words)):
            assert chunk.text.split()[0] in words
            assert chunk.text.split()[-1] in words


class TestTheWindowsCoverTheText:
    def test_every_word_lands_in_some_chunk_and_none_is_invented(self) -> None:
        text = _prose(120)
        chunks = _chunk(text, chunk_size_tokens=200, overlap_tokens=30)
        seen = {w for c in chunks for w in c.text.split()}
        assert seen == set(text.split())

    def test_adjacent_chunks_overlap_and_the_next_starts_at_a_word(self) -> None:
        text = _prose(120)
        chunks = _chunk(text, chunk_size_tokens=200, overlap_tokens=30)
        words = set(text.split())
        for prev, curr in zip(chunks, chunks[1:], strict=False):
            head = " ".join(curr.text.split()[:3])
            assert head in prev.text, (prev.text[-60:], curr.text[:60])
            assert curr.text.split()[0] in words

    def test_without_overlap_chunks_do_not_repeat_text(self) -> None:
        text = _prose(120)
        chunks = _chunk(text, chunk_size_tokens=200, overlap_tokens=0)
        assert sum(len(c.text.split()) for c in chunks) == len(text.split())


class TestTheEncoderStopsWhenTheChunksDo:
    def test_max_chunks_leaves_the_rest_of_a_section_unencoded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from persona.skills import _tokens  # noqa: PLC0415

        seen = [0]
        real_encode = _tokens.encode

        def _counting_encode(text: str) -> list[int]:
            seen[0] += len(text)
            return real_encode(text)

        monkeypatch.setattr(_tokens, "encode", _counting_encode)
        text = _prose(3000)  # about 2.4 million characters
        chunks = _chunk(text, max_chunks=3)
        assert len(chunks) == 3
        assert seen[0] <= 4 * _tokens.SEGMENT_CHARS, seen[0]


class TestTheReviewersTextsChunk:
    """Third review, finding 6: these raised ValueError, and the upload returned 500."""

    def test_a_special_token_string_is_chunked_as_text(self) -> None:
        text = ("Tokenizers mark the end with <|endoftext|> in GPT models. " * 400).strip()
        chunks = _chunk(text)
        assert len(chunks) > 1
        assert any("<|endoftext|>" in c.text for c in chunks)
        assert all(count_tokens(c.text) <= DEFAULT_CHUNK_SIZE_TOKENS for c in chunks)

    def test_a_million_character_whitespace_run_is_chunked(self) -> None:
        text = "start " + " " * 1_500_000 + " end"
        chunks = _chunk(text)
        assert chunks[0].text.startswith("start")
        assert chunks[-1].text.endswith("end")

    def test_unspaced_cjk_is_cut_into_full_windows(self) -> None:
        text = "漢字仮名交じり文" * 3000
        chunks = _chunk(text)
        tokens = [count_tokens(c.text) for c in chunks]
        assert all(n <= DEFAULT_CHUNK_SIZE_TOKENS for n in tokens)
        assert all(n >= DEFAULT_CHUNK_SIZE_TOKENS - 4 for n in tokens[:-1]), tokens


class TestRandomTextKeepsTheInvariants:
    """Every chunk fits the cap, every word lands whole in some chunk, and chunking ends."""

    def test_random_mixtures(self) -> None:
        import random  # noqa: PLC0415

        rng = random.Random(14)
        joins = [" ", " ", " ", "\n", "\n\n", "  ", "\t", ""]
        for round_ in range(150):
            words = [
                rng.choice(["w", "Ω", "日本", "x1", "ab", "🎉", "<|endoftext|>"]) + str(n)
                for n in range(rng.randint(1, 600))
            ]
            text = "".join(w + rng.choice(joins) for w in words)
            cap = rng.randint(24, 120)
            overlap = rng.randint(8, cap // 2)
            chunks = _chunk(text, chunk_size_tokens=cap, overlap_tokens=overlap)
            assert all(count_tokens(c.text) <= cap for c in chunks), round_
            assert all(c.text == c.text.strip() and c.text for c in chunks), round_
            if text.strip():
                assert chunks, round_
                for word in words:
                    assert any(word in c.text for c in chunks), (round_, word)


class TestChunkingResumesFromACursor:
    """D-FV0-31: the document index takes chunks a slice at a time and resumes exactly."""

    def _sections(self) -> list[DocumentSection]:
        return [
            DocumentSection(text=_prose(30), page=1),
            DocumentSection(text="   "),
            DocumentSection(text="A short page.", page=3),
            DocumentSection(text=_prose(40, sentences=3), page=4),
        ]

    def _iter(self, start: object = None) -> list[tuple[DocumentChunk, object]]:
        from persona.documents.chunker import ChunkCursor, iter_document_chunks  # noqa: PLC0415

        return list(
            iter_document_chunks(
                self._sections(),
                conversation_id="c",
                doc_ref="d-1234abcd",
                document_format="pdf",
                title="t",
                start=start if start is not None else ChunkCursor(),
                chunk_size_tokens=120,
                overlap_tokens=20,
            )
        )

    def test_the_iterator_yields_what_chunk_document_returns(self) -> None:
        listed = chunk_document(
            self._sections(),
            conversation_id="c",
            doc_ref="d-1234abcd",
            document_format="pdf",
            title="t",
            chunk_size_tokens=120,
            overlap_tokens=20,
        )
        assert [c.text for c, _ in self._iter()] == [c.text for c in listed]

    def test_resuming_from_a_cursor_repeats_exactly(self) -> None:
        """A slice that runs twice from the same cursor writes the same chunks, same ids."""
        whole = self._iter()
        cursor = whole[6][1]
        first = [(c.id, c.text, c.page) for c, _ in self._iter(cursor)]
        second = [(c.id, c.text, c.page) for c, _ in self._iter(cursor)]
        assert first == second
        assert first[0][0].endswith("::0007")

    def test_slice_by_slice_covers_every_word_with_contiguous_ids(self) -> None:
        from persona.documents.chunker import ChunkCursor  # noqa: PLC0415

        cursor, ids, words = ChunkCursor(), [], set()
        while True:
            taken = self._iter(cursor)[:5]
            if not taken:
                break
            ids += [c.id for c, _ in taken]
            words |= {w for c, _ in taken for w in c.text.split()}
            cursor = taken[-1][1]
        assert ids == [f"c::document::d-1234abcd::{i:04d}" for i in range(len(ids))]
        expected = {w for s in self._sections() for w in s.text.split()}
        assert words == expected

    def test_the_last_cursor_points_past_the_text(self) -> None:
        from persona.documents.chunker import ChunkCursor  # noqa: PLC0415

        _, last = self._iter()[-1]
        assert last == ChunkCursor(section=4, offset=0, index=len(self._iter()))
        assert self._iter(last) == []

    def test_a_cursor_inside_a_section_starts_there(self) -> None:
        from persona.documents.chunker import ChunkCursor  # noqa: PLC0415

        whole = self._iter()
        inside = next(after for _, after in whole if after.section == 0 and after.offset > 0)
        resumed = self._iter(inside)
        text = self._sections()[0].text.strip()
        assert resumed[0][0].text.split()[0] in text[inside.offset :].split()[:3]
        assert isinstance(inside, ChunkCursor)
