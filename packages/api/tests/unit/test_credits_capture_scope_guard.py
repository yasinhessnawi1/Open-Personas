"""Default-lane always-on guard: ``capture_up_to`` stays scoped to POST-SUCCESS billing.

Spec M2 review, C1's CRITICAL CONSTRAINT: the all-or-nothing ``deduct``
semantics (the atomic conditional floor) must remain untouched for the PRE-flight
/ guard metered callers. Spec M3 (T3a) AMENDS the C1 scope: partial-capture is
the correct primitive for POST-SUCCESS billing of already-completed work — where
an exhausted balance must neither crash nor overdraw. Approved ``capture_up_to``
callers are therefore the chat-turn worker (post-success shortfall) AND the
image-gen true-up (``imagegen/service.py`` — the ceiling pre-deduct's real-cost
reconciliation; the delta arm floors at 0). The PRE-flight callers — authoring +
avatar (``routes/personas.py``) and sandbox code execution
(``sandbox/runtime_tool.py``) — must still use all-or-nothing ``deduct`` /
``deduct_idempotent``, NEVER ``capture_up_to``.

Mirrors ``test_credits_balance_floor_guard.py``'s style: cheap, no-DB,
source-level assertions so a future refactor that widens the opt-in (or
weakens ``deduct``'s own conditional floor) fails here, in the default lane,
without needing a live Postgres.
"""

from __future__ import annotations

import inspect

#: The PRE-flight/guard metered callers that must stay all-or-nothing (never
#: partial-capture). imagegen is NOT here — its post-success true-up legitimately
#: uses ``capture_up_to`` (Spec M3, T3a).
_DEDUCT_ONLY_CALL_SITES = (
    "persona_api.routes.personas",
    "persona_api.sandbox.runtime_tool",
)

#: The approved ``capture_up_to`` callers (post-success / incremental billing of
#: completed work): the chat-turn worker (post-success shortfall), the image-gen
#: true-up (T3a), and the agentic-run per-step watcher (T4a — passes the bound
#: method to ``asyncio.to_thread``, so no trailing paren).
_CAPTURE_UP_TO_CALLERS = (
    "persona_api.background.chat_turn_worker",
    "persona_api.imagegen.service",
    "persona_api.background.run_worker",
    # T4b: the task-leg handler uses ``capture_up_to_idempotent`` (post-success,
    # CAS-ridden owner billing) — floored, so a completed leg is never hard-failed.
    "persona_api.tasks.handler",
)


def test_deduct_still_uses_a_conditional_all_or_nothing_decrement() -> None:
    """``deduct``'s all-or-nothing floor (R2-D-3) is untouched by C1's addition.

    Spec M4 T1a re-expressed the floor over the two-bucket draw: deduct uses the
    STRICT (``allow_partial=False``) arm of ``_draw_from_buckets``, whose over-
    spendable path raises ``CreditsExhaustedError`` and writes nothing."""
    from persona.credits import service

    src = inspect.getsource(service.deduct)
    assert "allow_partial=False" in src, "deduct must use the STRICT (all-or-nothing) draw"
    assert "CreditsExhaustedError" in inspect.getsource(service._draw_from_buckets), (  # noqa: SLF001
        "an overdraw must still raise CreditsExhaustedError"
    )


def test_capture_up_to_is_additive_not_a_deduct_rewrite() -> None:
    """``capture_up_to`` is a NEW, separate function — it does not replace or
    alias ``deduct`` (both must independently exist and differ).

    Spec M4 T1a: both delegate to the shared ``_draw_from_buckets`` but with
    OPPOSITE ``allow_partial`` — deduct strict (``False``), capture partial-floored
    (``True``); the partial floor is ``min(allowance, take)`` + the FIFO lot walk."""
    from persona.credits import service

    assert service.deduct is not service.capture_up_to
    capture_src = inspect.getsource(service.capture_up_to)
    deduct_src = inspect.getsource(service.deduct)
    # capture is the PARTIAL (floored) arm; deduct is the STRICT arm — never swapped.
    assert "allow_partial=True" in capture_src, "capture_up_to must use the PARTIAL (floored) draw"
    assert "allow_partial=False" in deduct_src, "deduct must use the STRICT draw (never partial)"


def test_preflight_callers_still_call_deduct_not_capture_up_to() -> None:
    """Source-level pin (CRITICAL CONSTRAINT): the PRE-flight metered callers —
    authoring/avatar (``routes/personas.py``) and sandbox — each still reference
    ``.deduct`` (or ``.deduct_idempotent``) on their injected ``CreditsPolicy``
    and NONE reference ``.capture_up_to``. Grepped at the source level so a
    future PRE-flight call site that mistakenly reaches for the partial-capture
    method fails here. ``.deduct`` (no trailing paren) because
    ``sandbox/runtime_tool.py`` passes the bound method to
    ``asyncio.to_thread(policy.deduct, ...)`` rather than calling it directly."""
    import importlib

    for module_name in _DEDUCT_ONLY_CALL_SITES:
        module = importlib.import_module(module_name)
        src = inspect.getsource(module)
        assert ".deduct" in src, f"{module_name} must still reference .deduct"
        assert ".capture_up_to" not in src, (
            f"{module_name} must NOT reference .capture_up_to — partial-capture is "
            f"reserved for POST-SUCCESS billing (chat turn / image true-up) only"
        )


def test_capture_up_to_stays_scoped_to_the_approved_post_success_callers() -> None:
    """The flip side of the pin: ``.capture_up_to`` appears ONLY in the approved
    post-success callers (the chat-turn worker + the image-gen true-up), never in
    a pre-flight caller (Spec M3, T3a widened the C1 scope)."""
    import importlib

    for module_name in _CAPTURE_UP_TO_CALLERS:
        src = inspect.getsource(importlib.import_module(module_name))
        # ``.capture_up_to`` (no trailing paren required): the run-worker watcher
        # passes the bound method to ``asyncio.to_thread`` rather than calling it.
        assert ".capture_up_to" in src, f"{module_name} should use .capture_up_to (post-success)"

    for module_name in _DEDUCT_ONLY_CALL_SITES:
        other = importlib.import_module(module_name)
        assert ".capture_up_to" not in inspect.getsource(other)


def test_credits_policy_protocol_declares_capture_up_to() -> None:
    """The Protocol surface itself carries the new method (both concrete
    editions implement it — ``MeteredCreditsPolicy`` delegates to the core
    function; ``UnlimitedCreditsPolicy`` no-ops)."""
    from persona_api.editions.credits_policy import (
        CreditsPolicy,
        MeteredCreditsPolicy,
        UnlimitedCreditsPolicy,
    )

    assert hasattr(CreditsPolicy, "capture_up_to")
    assert hasattr(MeteredCreditsPolicy, "capture_up_to")
    assert hasattr(UnlimitedCreditsPolicy, "capture_up_to")


def test_credits_policy_stays_amount_agnostic() -> None:
    """F7 precedent (thread the opt-in the way F7 kept the policy untouched):
    ``MeteredCreditsPolicy.capture_up_to`` is a semantics-free passthrough —
    it must not itself compute ``min``/``LEAST``, floors, or construct the
    shortfall marker; that arithmetic lives entirely in the core service.
    (The word "shortfall" legitimately appears in the method's OWN docstring
    explaining this constraint — the load-bearing check is that the literal
    ``":shortfall"`` string-construction never appears in the CODE.)"""
    from persona_api.editions import credits_policy as policy_module

    src = inspect.getsource(policy_module.MeteredCreditsPolicy.capture_up_to)
    assert "min(" not in src
    assert "LEAST" not in src
    assert '"shortfall"' not in src
    assert "'shortfall'" not in src
    assert "_capture_up_to(" in src, "must delegate to the core service function"
