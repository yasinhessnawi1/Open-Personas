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

__all__ = [
    "canonical_model_id",
    "first_token_sample_model",
    "reorder_primary",
    "resolve_served_model",
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


def resolve_served_model(backend: ChatBackend) -> tuple[str, str]:
    """Return the ``(provider, model)`` that actually served ``backend``'s latest call.

    A :class:`MultiModelChatBackend` reports its PRIMARY through ``provider_name`` /
    ``model_name`` by contract, even when a fallback answered. On a text-only request
    every backend before the winner leaves one record in the wrapper's
    ``last_attempts`` ledger, so the winner is ``backends[len(last_attempts)]``. This
    is the one resolution the text loop's TurnLog attribution (Spec M2, D-M2-2) and the
    voice turn log (R9-214) share.

    Known gap, unchanged here: on an image-bearing request the wrapper walks only its
    vision-capable backends (``_vision_aware_candidates``) and skips the others without
    recording them, so the ledger length no longer indexes the full chain and the pair
    returned can name the wrong backend whenever a non-vision backend precedes the
    winner. This is registered in the R9 register.

    A bare backend has no ledger and no chain, so its own identity is returned. So is
    the wrapper's, defensively, when the ledger is as long as the chain (every backend
    failed, which callers do not reach because the wrapper raised instead).

    Args:
        backend: The backend the call went through; usually a tier's wrapper.

    Returns:
        The served ``(provider, model)`` pair.
    """
    attempts = getattr(backend, "last_attempts", None) or []
    chain = getattr(backend, "backends", None)
    if chain is not None and 0 <= len(attempts) < len(chain):
        winner = chain[len(attempts)]
        return winner.provider_name, winner.model_name
    return backend.provider_name, backend.model_name


def first_token_sample_model(backend: ChatBackend) -> str | None:
    """The model a first-token latency sample for this call belongs to, or ``None``.

    Call it once the call has produced its first reply text, when a chain has
    committed to the backend that is answering.

    What it excludes (R9-226): a FALLBACK call, one where an earlier backend in the
    chain failed and a later one answered. The time measured includes every earlier
    backend's failure, and the chain records no per-attempt timing to take that out,
    so such a call yields no sample at all. The failure is not recorded against the
    backend that failed either: time to a 429 or a timeout is not first-token latency,
    and would make a fast-failing model look like the fastest one to the router's
    latency score.

    What it does NOT exclude: a same-backend RETRY. The chain retries a transient
    failure on the same backend (once, by default) and, when the retry succeeds,
    leaves nothing in ``last_attempts``, so that sample is attributed to the right
    model but includes the failed attempt and the retry wait. Registered alongside
    R9-223, whose per-call served identity is where per-attempt timing belongs.

    Also inherits the known gaps of :func:`resolve_served_model`: the shared ledger
    (R9-223) and, on an image request, a non-vision primary the chain skipped without
    recording it (R9-227).

    Args:
        backend: The backend the call went through; usually a tier's wrapper.

    Returns:
        The model name the latency tracker keys on, or ``None`` when an earlier
        backend in the chain failed before the one that answered.
    """
    if getattr(backend, "last_attempts", None):
        return None
    return resolve_served_model(backend)[1]


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
