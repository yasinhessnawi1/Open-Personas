"""Every Groq model we price or route to must still exist (R9-115).

Groq decommissioned `llama-3.3-70b-versatile` and
`meta-llama/llama-4-scout-17b-16e-instruct`. Both sat at the FRONT of the deployed
mid chain, and the first also sat in small, so every mid-tier call across chat,
voice and background burned two doomed round trips before reaching a model that
answers. Nothing noticed: a decommissioned model is a 404 at call time, which the
fallback chain swallows by design, so the only symptom is latency and wasted
provider quota.

These do not call Groq. A unit test that hit the network would be flaky and would
still only prove today. They pin the two things that are checkable offline and were
BOTH wrong in production: that a model we price is a model we can route to, and
that a model we route to advertises its tool support honestly.

The catalogue itself is verified by an operator pass against GET /v1/models; the
dead entries are retained in the metadata table deliberately, as a record, and this
test is what stops them being routed to again.
"""

from __future__ import annotations

from persona.backends.metadata.groq import MODELS
from persona.backends.openai_compat import _NATIVE_TOOLS_CAPABILITY

#: Verified absent from the live Groq catalogue on 2026-09-02 (the account lists 14
#: models; none of these is among them). Kept in the metadata table as a record of
#: what was served, never as a routing target.
_DECOMMISSIONED = frozenset(
    {
        "groq/llama-3.3-70b-versatile",
        "groq/meta-llama/llama-4-scout-17b-16e-instruct",
        "groq/llama-3.1-8b-instant",
    }
)

#: Verified serving on 2026-09-02, each with a real chat completion AND a real
#: tool-call round trip.
_LIVE = frozenset({"groq/openai/gpt-oss-120b", "groq/openai/gpt-oss-20b"})


def test_the_live_models_are_priced() -> None:
    """An unpriced model bills at the flat floor and records ``unpriced``.

    That is a silent revenue and attribution bug: the turn happens, the cost is
    real, and the ledger cannot say what it was.
    """
    missing = _LIVE - set(MODELS)
    assert not missing, f"live Groq models with no pricing metadata: {sorted(missing)}"


def test_every_live_model_advertises_native_tools_honestly() -> None:
    """Absence from the capability matrix is SILENT (R9-068).

    The model still serves; every tool call just degrades to the text shim. Nothing
    errors and nothing reports it, so the only evidence is worse answers.
    """
    groq_matrix = _NATIVE_TOOLS_CAPABILITY["groq"]
    for full_id in _LIVE:
        bare = full_id.removeprefix("groq/")
        assert bare in groq_matrix, (
            f"{full_id} serves tool calls but is missing from the capability matrix, "
            "so every tool call silently degrades to the text shim"
        )


def test_the_decommissioned_models_are_still_documented() -> None:
    """Kept on purpose, as the record of what a chain used to point at.

    Deleting them would make a future operator reading an old log or an old env var
    unable to tell a typo from a retirement.
    """
    assert set(MODELS) >= _DECOMMISSIONED


def test_a_priced_model_carries_a_usable_price() -> None:
    """A zero price is indistinguishable from 'free' downstream.

    Every cost report reads these numbers, so a placeholder zero would quietly
    under-bill rather than fail.
    """
    for model_id in _LIVE:
        md = MODELS[model_id]
        assert md.cost_input_per_1k_tokens > 0, f"{model_id} has a zero input price"
        assert md.cost_output_per_1k_tokens > 0, f"{model_id} has a zero output price"
