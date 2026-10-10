"""Tests for ``persona.skills._tokens`` (T02, D-04-2).

Pin the encoder choice (``cl100k_base``), confirm thread-safety, and
exercise the standard token-counting edge cases.
"""

# ruff: noqa: ANN401, ARG001, ARG002

from __future__ import annotations

import threading

import pytest
import tiktoken
from persona.skills import _tokens
from persona.skills._tokens import count_tokens


class TestCountTokensBasics:
    def test_empty_string_is_zero(self) -> None:
        assert count_tokens("") == 0

    def test_simple_ascii(self) -> None:
        # Anchor against the live encoder rather than a hard-coded number —
        # the cl100k_base table is fixed but pinning the integer would be
        # brittle if tiktoken ever rebased.
        ref = tiktoken.get_encoding("cl100k_base")
        text = "the quick brown fox jumps over the lazy dog"
        assert count_tokens(text) == len(ref.encode(text))

    def test_unicode_safe(self) -> None:
        # Accents / non-ASCII produce more tokens than chars; just confirm
        # the call doesn't raise and returns > char-count.
        assert count_tokens("héllo wörld") > 0

    def test_emoji_safe(self) -> None:
        assert count_tokens("🎉 test") > 0

    def test_deterministic_across_calls(self) -> None:
        text = "stability check"
        counts = {count_tokens(text) for _ in range(50)}
        assert len(counts) == 1


class TestEncoderCaching:
    """The module-level ``_ENCODER`` should be loaded once at import.

    The C layer caches by name, so even if we call
    ``tiktoken.get_encoding("cl100k_base")`` from outside, the same object
    is returned. Pin both invariants.
    """

    def test_module_level_encoder_is_cl100k_base(self) -> None:
        assert _tokens._ENCODER.name == "cl100k_base"

    def test_module_level_encoder_is_same_as_fresh_lookup(self) -> None:
        # tiktoken caches at the C layer; the module's encoder IS the one
        # returned by a fresh lookup. This guards against accidentally
        # re-binding ``_ENCODER`` somewhere.
        fresh = tiktoken.get_encoding("cl100k_base")
        assert _tokens._ENCODER is fresh


class TestThreadSafety:
    def test_concurrent_count_calls_give_identical_results(self) -> None:
        # The encoder is stateless; eight threads tokenising the same text
        # must all observe the same count.
        text = "concurrency check " * 100
        results: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            n = count_tokens(text)
            with lock:
                results.append(n)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(results)) == 1


class TestPerformanceProperties:
    """Loose sanity checks. Not benchmarks — just guard against accidental
    O(N²) regressions."""

    def test_short_string_under_50_ms(self) -> None:
        import time

        text = "short text"
        start = time.perf_counter()
        for _ in range(100):
            count_tokens(text)
        elapsed = time.perf_counter() - start
        # 100 short tokenisations should finish in well under 50 ms on any
        # remotely sane machine. If this ever fires, something is wrong.
        assert elapsed < 0.5

    def test_50kb_string_tokenises_under_500ms(self) -> None:
        import time

        big = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 1000
        start = time.perf_counter()
        n = count_tokens(big)
        elapsed = time.perf_counter() - start
        assert n > 1000
        assert elapsed < 0.5


def test_import_does_not_raise() -> None:
    # Belt-and-braces guard: the hard-import at module load must succeed
    # whenever the test suite runs (tiktoken is a core dep). If this ever
    # raises ImportError, a packaging change broke spec 04's contract.
    import importlib

    importlib.reload(_tokens)
    assert _tokens.count_tokens("smoke") > 0


def test_pure_function_no_side_effects(tmp_path: object) -> None:
    # No I/O, no clock, no logging — count_tokens is a pure function.
    # Calling it should not change any module-level state we can observe.
    snapshot_id = id(_tokens._ENCODER)
    for _ in range(20):
        count_tokens("x")
    assert id(_tokens._ENCODER) == snapshot_id


# Sanity: pytest collected something
@pytest.mark.parametrize(
    ("text", "expected_positive"),
    [
        ("", False),
        ("a", True),
        ("hello world", True),
        ("hello world\n\nlong text", True),
    ],
)
def test_count_tokens_positive_for_nonempty(text: str, expected_positive: bool) -> None:
    n = count_tokens(text)
    assert (n > 0) == expected_positive


# --- Third security review, finding 6: content never makes the counter raise ---------

_CORE = __import__("pathlib").Path(__file__).resolve().parents[3]
_SKILL_TEXTS = sorted(
    [
        *(_CORE / "src" / "persona" / "skills" / "builtin").rglob("*.md"),
        *(_CORE / "tests" / "unit" / "skills" / "_fixtures").rglob("*.md"),
    ]
)


def _reference_count(text: str) -> int:
    """The count before the change: tiktoken's default ``encode`` (raises on special tokens)."""
    return len(tiktoken.get_encoding("cl100k_base").encode(text))


def _ordinary_count(text: str) -> int:
    """The whole text in one call, special-token strings as ordinary text."""
    return len(tiktoken.get_encoding("cl100k_base").encode_ordinary(text))


class TestTheSkillBudgetCountIsUnchanged:
    """Skill budgets count with this function too, so its counts must not move for normal text."""

    def test_the_fixtures_are_there(self) -> None:
        assert len(_SKILL_TEXTS) >= 7

    @pytest.mark.parametrize("path", _SKILL_TEXTS, ids=lambda p: f"{p.parent.name}/{p.name}")
    def test_each_skill_file_counts_as_before(self, path: object) -> None:
        text = path.read_text(encoding="utf-8")  # type: ignore[attr-defined]
        assert count_tokens(text) == _reference_count(text)

    def test_text_longer_than_a_segment_counts_as_before(self) -> None:
        """Segment-wise counting gives the whole-text count: the cuts change nothing."""
        text = "\n\n".join(p.read_text(encoding="utf-8") for p in _SKILL_TEXTS) * 4
        assert len(text) > 8 * _tokens.SEGMENT_CHARS
        assert count_tokens(text) == _reference_count(text)


def _rule_cut(text: str, c: int) -> bool:
    """Whether ``c`` meets one of :func:`safe_cut`'s two proved rules."""
    after_word = text[c] == " " and not text[c - 1].isspace()
    after_line_break = text[c - 1] in "\r\n" and not text[c].isspace()
    return after_word or after_line_break


def _pieces(text: str) -> list[str]:
    """cl100k's own pre-tokeniser pieces, from tiktoken's own pattern."""
    import regex  # noqa: PLC0415  (tiktoken's own dependency)

    return list(regex.findall(_tokens._ENCODER._pat_str, text))  # noqa: SLF001


_AWKWARD = [
    "word", " word", "  two", "\tTab", "\r\n", "\n\n", "\n", "\r", "it's", "it' s", "'ll",
    "'LL", " 12345", "123", " 3.14", "émigré", " naïve", " Ωmega", " 日本語", "中文", " 🎉",
    "<|endoftext|>", "--", " (x)", "a,b", " 　x", " nbsp", " ", "\x85", " ''",
    "_x", " .x", ".", " ", "  ", "\t", "\x1c", "é", " ́", "\n ", " \n", ")\n",
    "!\r\n\r\n", "| `frame`         | `html`",
]  # fmt: skip


def _svg_attribute_table(rows: int) -> str:
    """A markdown table shaped like the one that forced hard cuts before (padded cells)."""
    names = [f"{word}{n}" for n in range(rows // 20 + 1) for word in dir(str)][:rows]
    return "".join(
        f"| `{name}`{' ' * (28 - len(name))} | `{name.lower()}`{' ' * (30 - len(name))} | "
        f"`{'svg' if n % 3 else 'html'}`        |\n"
        for n, name in enumerate(names)
    )


class TestSafeCuts:
    """Every cut :func:`safe_cut` makes by its rules leaves the two sides' counts exact."""

    def test_every_rule_position_in_awkward_text_is_a_piece_boundary_of_both_sides(self) -> None:
        """Not only the cuts safe_cut picks: every position either rule allows, exhaustively."""
        import random  # noqa: PLC0415

        rng = random.Random(1406)
        checked = 0
        for _ in range(600):
            text = "".join(rng.choice(_AWKWARD) for _ in range(rng.randint(2, 40)))
            whole = _pieces(text)
            count = _ordinary_count(text)
            for c in range(1, len(text)):
                if _rule_cut(text, c):
                    checked += 1
                    assert _pieces(text[:c]) + _pieces(text[c:]) == whole, repr(text[c - 8 : c + 8])
                    assert _ordinary_count(text[:c]) + _ordinary_count(text[c:]) == count
        assert checked > 2000

    def test_a_space_after_a_non_space_is_a_cut_whatever_follows(self) -> None:
        assert _tokens.safe_cut("a 1 2 3 4 5", 0, 10) == 9
        assert _tokens.safe_cut("x ( y", 0, 3) == 3

    def test_a_cut_never_lands_inside_a_run_of_spaces(self) -> None:
        assert _tokens.safe_cut("a  b  c  d", 0, 9) == 7  # before the run, not in it

    def test_a_line_break_before_a_non_space_is_a_cut(self) -> None:
        assert _tokens.safe_cut("ab\ncd", 0, 4) == 3
        assert _tokens.safe_cut("ab\n\ncd", 0, 5) == 4  # after the run of line breaks
        assert _tokens.safe_cut("a\r\nbc", 0, 4) == 3
        assert _tokens.safe_cut("ab\rcd", 0, 4) == 3  # a lone carriage return too

    def test_the_whole_window_is_searched_before_a_hard_cut(self) -> None:
        assert _tokens.safe_cut("ab " + "x" * 100, 0, 90) == 2
        assert _tokens.safe_cut("ab\n" + "x" * 100, 0, 90) == 3

    def test_a_window_with_no_rule_position_is_cut_hard(self) -> None:
        assert _tokens.safe_cut("x" * 100, 0, 50) == 50
        assert _tokens.safe_cut("  \t  " * 20, 0, 50) == 50
        assert _tokens.safe_cut("日本語" * 30, 0, 50) == 50


class TestLongRepositoryTextCountsExactly:
    """The orchestrator's check, ported: long text from the repository, many segments each.

    Before the line-break rule, the table above forced hard cuts and counts were off by 3.
    """

    @staticmethod
    def _corpus() -> list[str]:
        core = _CORE / "src" / "persona"
        texts = [p.read_text(encoding="utf-8") for p in sorted(core.rglob("*.py"))[:300]]
        texts += [p.read_text(encoding="utf-8") for p in _SKILL_TEXTS]
        return [*texts, _svg_attribute_table(600)]

    def test_counts_match_a_whole_encode_and_no_cut_is_hard(self) -> None:
        import random  # noqa: PLC0415

        corpus = self._corpus()
        table = corpus[-1]
        # One name per line and no spaces at all: only the line-break rule can cut here.
        word_list = "\n".join(f"{name}{n}" for n in range(200) for name in dir(str)[:20])
        assert len(word_list) > 2 * _tokens.SEGMENT_CHARS
        rng = random.Random(2026)
        for _ in range(12):
            parts = [table, word_list, *rng.sample(corpus, k=12)]
            rng.shuffle(parts)
            text = "".join(parts)
            ends = []
            start = 0
            while start < len(text):
                start = _tokens.safe_cut(text, start, start + _tokens.SEGMENT_CHARS)
                ends.append(start)
            assert len(ends) >= 4, "multi-segment by construction"
            assert all(_rule_cut(text, c) for c in ends[:-1]), "a hard cut"
            assert count_tokens(text) == _ordinary_count(text)

    def test_the_residual_with_no_rule_position_is_a_few_tokens_per_hard_cut(self) -> None:
        """Stated, not hidden: one unbroken line with no spaces is cut hard and may miscount."""
        import random  # noqa: PLC0415

        rng = random.Random(3)
        digits = "".join(str(rng.randint(0, 9)) for _ in range(100_000))
        hard_cuts = len(range(0, len(digits), _tokens.SEGMENT_CHARS)) - 1
        difference = abs(count_tokens(digits) - _ordinary_count(digits))
        assert 0 < difference <= 2 * hard_cuts


class TestContentNeverRaises:
    def test_a_special_token_string_counts_as_text(self) -> None:
        text = "The end marker is <|endoftext|> in GPT models."
        expected = len(tiktoken.get_encoding("cl100k_base").encode_ordinary(text))
        assert count_tokens(text) == expected

    def test_a_million_character_whitespace_run_counts(self) -> None:
        """tiktoken alone fails on this ("Max stack size exceeded for backtracking")."""
        text = "a" + " " * 1_500_000 + "b"
        assert 10_000 < count_tokens(text) < 15_000

    def test_the_encoder_never_sees_more_than_a_segment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        longest = [0]
        real = _tokens.encode

        def _spy(text: str) -> list[int]:
            longest[0] = max(longest[0], len(text))
            return real(text)

        monkeypatch.setattr(_tokens, "encode", _spy)
        count_tokens("word " * 200_000)
        count_tokens(" " * 300_000)
        assert 0 < longest[0] <= _tokens.SEGMENT_CHARS


class TestCappedCount:
    def test_exact_at_or_under_the_cap(self) -> None:
        text = "the quick brown fox " * 40
        n = count_tokens(text)
        assert _tokens.count_tokens_capped(text, n) == n
        assert _tokens.count_tokens_capped(text, n + 100) == n

    def test_over_the_cap_reports_more_than_the_cap(self) -> None:
        text = "the quick brown fox " * 40
        assert _tokens.count_tokens_capped(text, count_tokens(text) - 1) > count_tokens(text) - 1
        assert _tokens.count_tokens_capped(text * 1000, 512) > 512

    def test_it_stops_after_the_first_segment_past_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = [0]
        real = _tokens.encode

        def _spy(text: str) -> list[int]:
            seen[0] += len(text)
            return real(text)

        monkeypatch.setattr(_tokens, "encode", _spy)
        text = "word " * 400_000  # two million characters
        assert _tokens.count_tokens_capped(text, 512) > 512
        assert seen[0] <= _tokens.SEGMENT_CHARS

    def test_a_first_segment_exactly_at_the_cap_does_not_end_the_count(self) -> None:
        text = "word " * 20_000  # several segments
        first = _tokens.safe_cut(text, 0, _tokens.SEGMENT_CHARS)
        cap = count_tokens(text[:first])
        assert _tokens.count_tokens_capped(text, cap) > cap
