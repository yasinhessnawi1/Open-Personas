"""The embedder that costs nothing to start, and the notice that says what it costs you.

Spec K13, T6 (D-K13-14, D-K13-18). Writing one chunk through the real embedder loads torch
and sentence-transformers, about a hundred seconds on a cold cache, inside the first write.
An adopter who only writes and reads by id pays that for nothing. ``HashEmbedder`` is for
them, and the price is that similarity search stops working SILENTLY: ``query`` still returns
chunks, they are simply arbitrary ones.

So there are two things to get right and both are tested here. The vectors must be usable by
both transports (never NaN, never the zero vector, always 384 unless asked otherwise), and a
person who selects it must be told, at startup, in words that name the consequence.
"""

from __future__ import annotations

import math

import pytest
from persona.config import PersonaCoreConfig
from persona.stores import HashEmbedder, build_embedder
from persona.stores.embedder import DEFAULT_EMBEDDING_DIM, SentenceTransformerEmbedder
from persona.stores.postgres import EMBEDDING_DIM


def test_it_matches_the_dimension_both_transports_expect() -> None:
    """The Postgres column is ``vector(384)`` and fails fast on anything else."""
    assert HashEmbedder().dimension == EMBEDDING_DIM == DEFAULT_EMBEDDING_DIM


def test_the_same_text_always_gives_the_same_vector() -> None:
    embedder = HashEmbedder()
    assert embedder.encode(["a memory"]) == embedder.encode(["a memory"])


def test_different_texts_give_different_vectors() -> None:
    first, second = HashEmbedder().encode(["one thing", "another thing"])
    assert first != second


def test_every_lane_is_finite() -> None:
    """Reinterpreting hash bytes as floats can land on NaN, which pgvector rejects.

    The test embedder this grew out of decoded raw IEEE floats and then patched up the NaNs
    afterwards, which is a bug waiting for a new input. Deriving lanes from 16 bit integers
    cannot produce one.
    """
    texts = ["", "a", "æøå", "x" * 10_000, "\x00\x01", "🙂"]
    for vector in HashEmbedder().encode(texts):
        assert all(math.isfinite(lane) for lane in vector)


def test_it_never_returns_the_zero_vector() -> None:
    """pgvector's cosine distance is undefined over zeros and Chroma's index is no happier."""
    for vector in HashEmbedder().encode(["", "a", "the zero vector would break both"]):
        assert any(lane != 0.0 for lane in vector)


def test_the_vectors_are_normalised() -> None:
    """Cosine similarity is a dot product only for unit vectors, which the stores assume."""
    for vector in HashEmbedder().encode(["one", "two", "three"]):
        assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0, rel_tol=1e-9)


def test_empty_input_gives_empty_output() -> None:
    assert HashEmbedder().encode([]) == []


def test_the_dimension_can_be_chosen_for_a_store_that_wants_another() -> None:
    assert HashEmbedder(dimension=32).dimension == 32
    assert len(HashEmbedder(dimension=32).encode(["x"])[0]) == 32


def test_a_nonsense_dimension_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="dimension"):
        HashEmbedder(dimension=0)


def test_it_loads_no_model() -> None:
    """The whole point: no torch, no sentence-transformers, no download, no wait.

    In a subprocess, because ``sys.modules`` in the test process says nothing: some other
    test in the run has almost certainly imported torch already, so an in-process assertion
    would be either a tautology or a flake. A clean interpreter is the only honest answer.
    """
    import subprocess
    import sys

    script = (
        "import sys\n"
        "from persona.stores import HashEmbedder\n"
        "HashEmbedder().encode(['some text', 'and another'])\n"
        "print('TORCH', 'torch' in sys.modules)\n"
        "print('ST', 'sentence_transformers' in sys.modules)\n"
    )
    done = subprocess.run(  # noqa: S603 - fixed argv, this interpreter
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=180, check=True
    )

    assert "TORCH False" in done.stdout, f"the hash embedder pulled in torch: {done.stdout}"
    assert "ST False" in done.stdout, f"it pulled in sentence-transformers: {done.stdout}"


# --- the knob and the notice ---------------------------------------------------------------


def test_the_default_is_the_real_embedder() -> None:
    """Nobody gets a silently non searching store by accident."""
    assert PersonaCoreConfig().embedder == "sentence-transformers"
    assert isinstance(build_embedder("sentence-transformers"), SentenceTransformerEmbedder)


def test_the_knob_selects_the_hash_embedder() -> None:
    assert isinstance(build_embedder("hash", on_notice=lambda _: None), HashEmbedder)


def test_the_config_reads_the_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_EMBEDDER", "hash")
    assert PersonaCoreConfig().embedder == "hash"


def test_an_unknown_embedder_name_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown embedder"):
        build_embedder("word2vec")


def test_selecting_it_produces_a_notice_that_names_the_consequence() -> None:
    """Not "hash embedder enabled". The person has to learn that search is off."""
    said: list[str] = []

    build_embedder("hash", on_notice=said.append)

    assert len(said) == 1
    notice = said[0].lower()
    assert "search" in notice
    assert "off" in notice or "disabled" in notice
    # And what still works, because otherwise the reader assumes the worst.
    assert "written" in notice or "read" in notice
    # And how to undo it.
    assert "persona_embedder" in notice


def test_the_real_embedder_says_nothing() -> None:
    """A notice on the ordinary path is a notice people learn to scroll past."""
    said: list[str] = []

    build_embedder("sentence-transformers", on_notice=said.append)

    assert said == []
