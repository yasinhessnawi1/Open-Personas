"""The model-selection seam — cheap per-turn re-wrap (Spec 23 T10; D-23-X-seam-shape).

:func:`reorder_primary` is the load-bearing seam: given a tier's cached
:class:`~persona.backends.multi_model.MultiModelChatBackend` and a chosen model
id, it returns a FRESH wrapper over the SAME already-constructed sub-backends with
the chosen one first and the rest preserved in fallback order (Spec 20 D-20-9
chain intact). It NEVER mutates the cached wrapper (concurrency-safe — one wrapper
serves many conversations) and NEVER reconstructs a client (the
``MultiModelChatBackend.__init__`` is allocation-only — confirmed, so the re-wrap
is microseconds, well inside the Spec 18 D-18-4 ~30ms bound).

Short-circuits (D-23-X-seam-shape refinement 1): a non-wrapper backend, an
unknown chosen id, or chosen-already-primary all return the input unchanged with
zero allocation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.backends.multi_model import MultiModelChatBackend

if TYPE_CHECKING:
    from persona.backends.protocol import ChatBackend
    from persona.backends.types import ServedCall

__all__ = [
    "canonical_model_id",
    "first_token_sample_model",
    "reorder_primary",
    "served_identity",
]


def canonical_model_id(provider: str, model: str) -> str:
    """Return the canonical provider-prefixed id for ``(provider, model)``.

    A model name that already carries a provider prefix (e.g. the OpenRouter
    slug ``"anthropic/claude-3.5-sonnet"``) is returned verbatim; a bare model
    name is prefixed with its provider (``"anthropic"``, ``"claude-sonnet-4-6"``
    → ``"anthropic/claude-sonnet-4-6"``). This is the id the metadata resolvers
    are keyed on.
    """
    return model if "/" in model else f"{provider}/{model}"


def served_identity(backend: ChatBackend, served: ServedCall | None) -> tuple[str, str]:
    """The ``(provider, model)`` that served ONE call, from that call's own result.

    ``served`` is the :class:`~persona.backends.types.ServedCall` a multi-model chain
    stamps on its response and on every chunk it yields (R9-223): the identity travels
    with the call, so a concurrent call on the same shared chain cannot change it, and
    an image request whose walk skipped a backend still names the one that answered
    (R9-227). ``None`` means a bare backend, which is its own served identity. (A chain
    reports its PRIMARY through ``provider_name`` / ``model_name`` by contract, which is
    why the served identity must come from the call.)

    Args:
        backend: The backend the call went through.
        served: The call's ``served`` field (``ChatResponse.served`` or
            ``StreamChunk.served``).

    Returns:
        The served ``(provider, model)`` pair.
    """
    if served is not None:
        return served.provider, served.model
    return backend.provider_name, backend.model_name


def first_token_sample_model(backend: ChatBackend, served: ServedCall | None) -> str | None:
    """The model a first-token latency sample for this call belongs to, or ``None``.

    Call it once the call has produced its first reply text, with that chunk's
    ``served`` field. A sample only when the call was answered with no failed try
    before it (D-R9-226 option ii):

    * not a FALLBACK call (R9-226): the time includes every earlier backend's failure.
      Nor is the failure recorded against the backend that failed: time to a 429 or a
      timeout is not first-token latency, and would make a fast-failing model look
      like the fastest one to the router's latency score;
    * not a same-backend RETRY (R9-229): the time includes the failed try and the
      retry wait.

    Args:
        backend: The backend the call went through; usually a tier's chain.
        served: The call's ``served`` field; ``None`` for a bare backend.

    Returns:
        The model name the latency tracker keys on, or ``None`` when a failed try
        preceded the answer.
    """
    if served is None:
        return backend.model_name
    return served.model if served.first_attempt else None


def reorder_primary(backend: ChatBackend, chosen_model_id: str) -> ChatBackend:
    """Return a backend whose PRIMARY is ``chosen_model_id`` (D-23-X-seam-shape).

    Args:
        backend: The tier's backend from
            :meth:`~persona_runtime.tier.TierRegistry.get` — usually a
            :class:`MultiModelChatBackend`.
        chosen_model_id: The canonical id the IntelligentRouter picked.

    Returns:
        ``backend`` unchanged when it is not a multi-model wrapper, the chosen id
        is not among its sub-backends, or the chosen model is already the primary
        (slot 0). Otherwise a fresh :class:`MultiModelChatBackend` over the same
        sub-backend instances, chosen first, the rest in their original relative
        order (fallback chain preserved).
    """
    if not isinstance(backend, MultiModelChatBackend):
        return backend
    subs = backend.backends
    chosen_index: int | None = None
    for i, sub in enumerate(subs):
        if canonical_model_id(sub.provider_name, sub.model_name) == chosen_model_id:
            chosen_index = i
            break
    if chosen_index is None or chosen_index == 0:
        # Unknown id, or already primary → no re-wrap, no allocation.
        return backend
    reordered = [subs[chosen_index], *subs[:chosen_index], *subs[chosen_index + 1 :]]
    return MultiModelChatBackend(reordered, tier_name=backend.tier_name)
