"""N5 A2–A6 — the streaming-safe feeling-tag converter (N5-D-1, N5-D-2, N5-D-7).

The criterion-3 core: **a raw ``{{#…}}`` must NEVER reach the user** — mid-stream,
split across chunks, malformed, unknown, or left unclosed at stream end — on either
the chat (substitute→emoji) or voice (strip) path. Each property below is its own
red→green gate (the non-negotiable embarrassing-bug gate).

Invariant helper: for ANY chunking of ANY input, the concatenation of ``feed`` outputs
plus ``flush`` must contain no ``{{#`` sequence.
"""

from __future__ import annotations

import pytest
from persona_runtime.emotional.converter import ConvertMode, FeelingTagConverter


def _run(mode: ConvertMode, chunks: list[str]) -> str:
    conv = FeelingTagConverter(mode)
    out = [conv.feed(c) for c in chunks]
    out.append(conv.flush())
    return "".join(out)


def _no_raw_tag(text: str) -> bool:
    return "{{#" not in text


# --- A2: whole tag, mid-stream (EMOJI) -------------------------------------


def test_a2_whole_tag_converts_to_emoji() -> None:
    assert _run(ConvertMode.EMOJI, ["I am {{#happy}} today"]) == "I am 😊 today"


def test_a2_multiple_tags_in_one_delta() -> None:
    assert _run(ConvertMode.EMOJI, ["{{#proud_of_you}} and {{#grateful}}"]) == "😌 and 🙏"


def test_a2_no_tags_is_identity() -> None:
    assert (
        _run(ConvertMode.EMOJI, ["just plain text, no tags here"])
        == "just plain text, no tags here"
    )


# --- A3: split across chunks -----------------------------------------------


def test_a3_tag_split_across_three_chunks() -> None:
    assert _run(ConvertMode.EMOJI, ["I feel {{#", "hap", "py}} now"]) == "I feel 😊 now"


def test_a3_sentinel_prefix_split_at_every_boundary() -> None:
    # Split "{{#happy}}" at EVERY index — all must yield the same converted result,
    # and no intermediate emission may contain a raw sentinel.
    s = "before {{#happy}} after"
    for i in range(len(s)):
        conv = FeelingTagConverter(ConvertMode.EMOJI)
        emitted = []
        a = conv.feed(s[:i])
        emitted.append(a)
        b = conv.feed(s[i:])
        emitted.append(b)
        emitted.append(conv.flush())
        assert "".join(emitted) == "before 😊 after", f"split at {i}"
        assert all(_no_raw_tag(e) for e in emitted), f"raw sentinel leaked at split {i}"


def test_a3_char_by_char_never_leaks() -> None:
    s = "a {{#warm}} b {{#curious}} c"
    conv = FeelingTagConverter(ConvertMode.EMOJI)
    pieces = [conv.feed(ch) for ch in s]
    pieces.append(conv.flush())
    assert all(_no_raw_tag(p) for p in pieces)
    assert "".join(pieces) == "a 🤗 b 🤔 c"


# --- A4: malformed / unknown stripped --------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "{{#}}",
        "{{# }}",
        "{{##happy}}",
        "{{#no space}}",
        "{{#UPPER}}",
        "{{#nonsense}}",
        "{{#happy!}}",
    ],
)
def test_a4_malformed_or_unknown_is_stripped(bad: str) -> None:
    out = _run(ConvertMode.EMOJI, [f"x{bad}y"])
    assert out == "xy", out
    assert _no_raw_tag(out)


# --- A5: flush contract -----------------------------------------------------


def test_a5_flush_unclosed_tag_is_stripped() -> None:
    # Stream ends mid-tag → the partial tag is dropped, never leaked.
    assert _run(ConvertMode.EMOJI, ["hello {{#hap"]) == "hello "


def test_a5_flush_bare_sentinel_stripped() -> None:
    assert _run(ConvertMode.EMOJI, ["hello {{#"]) == "hello "


def test_a5_flush_literal_braces_preserved() -> None:
    # Trailing braces that never became a sentinel are real text — preserve them.
    assert _run(ConvertMode.EMOJI, ["a set literal {"]) == "a set literal {"
    assert _run(ConvertMode.EMOJI, ["double {{"]) == "double {{"


def test_a5_literal_braces_not_a_feeling_tag() -> None:
    # "{a" and "{{ x" are not sentinels — pass through verbatim.
    assert _run(ConvertMode.EMOJI, ["{a and {{ x }}"]) == "{a and {{ x }}"


# --- A6: STRIP mode (voice) -------------------------------------------------


def test_a6_strip_mode_removes_tag_entirely() -> None:
    assert _run(ConvertMode.STRIP, ["I am {{#happy}} today"]) == "I am  today"


def test_a6_strip_mode_split_across_chunks_never_speaks_a_tag() -> None:
    out_pieces = []
    conv = FeelingTagConverter(ConvertMode.STRIP)
    for ch in ["feeling {{#", "grate", "ful}} for it"]:
        out_pieces.append(conv.feed(ch))
    out_pieces.append(conv.flush())
    assert all(_no_raw_tag(p) for p in out_pieces)
    assert "".join(out_pieces) == "feeling  for it"


def test_a6_strip_mode_malformed_stripped() -> None:
    assert _run(ConvertMode.STRIP, ["x{{#nonsense}}y"]) == "xy"


# --- Fuzz-ish invariant: no chunking of any of these ever leaks -------------


@pytest.mark.parametrize(
    "text",
    [
        "plain",
        "{{#happy}}",
        "a {{#happy}} b {{#unknown}} c",
        "trailing {{#",
        "braces { {{ {{# {{#h",
        "nested {{#a{{#happy}}",
        "{{#happy}}{{#sad}}",
    ],
)
@pytest.mark.parametrize("mode", [ConvertMode.EMOJI, ConvertMode.STRIP])
def test_invariant_no_raw_sentinel_for_any_split(text: str, mode: ConvertMode) -> None:
    for i in range(len(text) + 1):
        conv = FeelingTagConverter(mode)
        parts = [conv.feed(text[:i]), conv.feed(text[i:]), conv.flush()]
        assert all(_no_raw_tag(p) for p in parts), f"leak: {text!r} split {i} mode {mode}"


# --- V12 T1: the on_feeling capture callback (V12-D-2) ----------------------
#
# The callback observes a RECOGNISED feeling-tag as a PURE post-decision
# notification: it must NOT perturb the strip/substitute output or the
# criterion-3 buffering (N5's floor is preserved by reuse). Unknown/malformed
# tags are never captured. The V12 caller passes a pure appender (MUST NOT raise).


def _run_capture(mode: ConvertMode, chunks: list[str]) -> tuple[str, list[str]]:
    captured: list[str] = []
    conv = FeelingTagConverter(mode, on_feeling=captured.append)
    out = [conv.feed(c) for c in chunks]
    out.append(conv.flush())
    return "".join(out), captured


def test_v12_callback_fires_on_known_tag_strip_mode() -> None:
    out, captured = _run_capture(ConvertMode.STRIP, ["I am {{#happy}} today"])
    assert out == "I am  today"  # strip output UNCHANGED (tag consumed)
    assert captured == ["happy"]  # the recognised tag name observed


def test_v12_callback_fires_on_known_tag_emoji_mode() -> None:
    out, captured = _run_capture(ConvertMode.EMOJI, ["I am {{#happy}} today"])
    assert out == "I am 😊 today"  # emoji substitution UNCHANGED
    assert captured == ["happy"]


def test_v12_callback_never_fires_on_unknown_tag() -> None:
    out, captured = _run_capture(ConvertMode.STRIP, ["a {{#nonsense}} b"])
    assert out == "a  b"  # unknown still stripped (criterion 3)
    assert captured == []  # unknown/malformed never captured


def test_v12_callback_never_fires_on_malformed_tag() -> None:
    _, captured = _run_capture(ConvertMode.STRIP, ["{{#}}", "{{# }}", "{{##happy}}"])
    assert captured == []


def test_v12_callback_fires_once_per_tag_split_across_chunks() -> None:
    out, captured = _run_capture(ConvertMode.STRIP, ["lead {{#", "sa", "d}} tail"])
    assert out == "lead  tail"
    assert captured == ["sad"]  # one fire, full name, despite the split


def test_v12_callback_captures_multiple_tags_in_order() -> None:
    _, captured = _run_capture(ConvertMode.STRIP, ["{{#proud_of_you}} and {{#grateful}}"])
    assert captured == ["proud_of_you", "grateful"]


def test_v12_callback_default_none_is_byte_identical_to_no_callback() -> None:
    """Unset callback ⇒ output identical to today (the additive-safety proof)."""
    for mode in (ConvertMode.EMOJI, ConvertMode.STRIP):
        for text in ("plain", "{{#happy}} x", "a {{#unknown}} b", "trailing {{#"):
            base = _run(mode, [text])
            with_cb, _ = _run_capture(mode, [text])
            assert with_cb == base, f"{mode} {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "plain",
        "{{#happy}}",
        "a {{#happy}} b {{#unknown}} c",
        "trailing {{#",
        "braces { {{ {{# {{#h",
        "nested {{#a{{#happy}}",
        "{{#happy}}{{#sad}}",
    ],
)
@pytest.mark.parametrize("mode", [ConvertMode.EMOJI, ConvertMode.STRIP])
def test_v12_capture_does_not_perturb_the_leak_invariant(text: str, mode: ConvertMode) -> None:
    """The observe-don't-strip change keeps N5's fuzz-invariant: for ANY split,
    output with the callback == output without it, and never leaks a raw sentinel."""
    for i in range(len(text) + 1):
        base_conv = FeelingTagConverter(mode)
        base = [base_conv.feed(text[:i]), base_conv.feed(text[i:]), base_conv.flush()]
        cap_conv = FeelingTagConverter(mode, on_feeling=lambda _n: None)
        cap = [cap_conv.feed(text[:i]), cap_conv.feed(text[i:]), cap_conv.flush()]
        assert cap == base, f"perturbed: {text!r} split {i} mode {mode}"
        assert all(_no_raw_tag(p) for p in cap)
