"""Default-lane always-on guard: ``capture_up_to`` stays an OPT-IN for ONE caller.

Spec M2 review, C1's CRITICAL CONSTRAINT: the existing all-or-nothing
``deduct`` semantics must remain untouched for every other metered caller —
authoring (``routes/personas.py``, 1000 credits), imagegen
(``imagegen/service.py``, 100 credits/image), and sandbox code execution
(``sandbox/runtime_tool.py``, 1 credit). ``capture_up_to`` exists ONLY for the
chat-turn worker's post-success billing.

Mirrors ``test_credits_balance_floor_guard.py``'s style: cheap, no-DB,
source-level assertions so a future refactor that widens the opt-in (or
weakens ``deduct``'s own conditional floor) fails here, in the default lane,
without needing a live Postgres.
"""

from __future__ import annotations

import inspect

_OTHER_METERED_CALL_SITES = (
    "persona_api.imagegen.service",
    "persona_api.routes.personas",
    "persona_api.sandbox.runtime_tool",
)


def test_deduct_still_uses_a_conditional_all_or_nothing_decrement() -> None:
    """``deduct``'s conditional floor (R2-D-3) is untouched by C1's addition."""
    from persona.credits import service

    src = inspect.getsource(service.deduct)
    assert "balance >= amount" in src.replace("_credits_t.c.", ""), (
        "deduct must still carry a `balance >= amount` WHERE predicate (the atomic floor)"
    )
    assert "CreditsExhaustedError" in src, "an overdraw must still raise CreditsExhaustedError"


def test_capture_up_to_is_additive_not_a_deduct_rewrite() -> None:
    """``capture_up_to`` is a NEW, separate function — it does not replace or
    alias ``deduct`` (both must independently exist and differ)."""
    from persona.credits import service

    assert service.deduct is not service.capture_up_to
    capture_src = inspect.getsource(service.capture_up_to)
    # The hard-reject predicate lives only in ``deduct`` — never here.
    assert "balance >= amount" not in capture_src.replace("_credits_t.c.", "")
    # The partial-capture floor lives in the atomic SQL: LEAST(balance, amount).
    assert "LEAST" in str(service._CAPTURE_UP_TO_SQL)  # noqa: SLF001


def test_other_metered_callers_still_call_deduct_not_capture_up_to() -> None:
    """Source-level pin (CRITICAL CONSTRAINT): authoring / imagegen / sandbox
    each still reference ``.deduct`` on their injected ``CreditsPolicy`` and
    NONE of them reference ``.capture_up_to`` — the opt-in stays scoped to the
    chat-turn worker alone. Grepped at the source level so a future call site
    that mistakenly reaches for the new method fails here. ``.deduct`` (no
    trailing paren required) because ``sandbox/runtime_tool.py`` passes the
    bound method to ``asyncio.to_thread(policy.deduct, ...)`` rather than
    calling it directly."""
    import importlib

    for module_name in _OTHER_METERED_CALL_SITES:
        module = importlib.import_module(module_name)
        src = inspect.getsource(module)
        assert ".deduct" in src, f"{module_name} must still reference .deduct"
        assert ".capture_up_to" not in src, (
            f"{module_name} must NOT reference .capture_up_to — that opt-in is "
            f"reserved for the chat-turn worker's post-success billing only"
        )


def test_chat_turn_worker_is_the_only_capture_up_to_call_site() -> None:
    """The flip side of the pin above: exactly one production module
    references ``.capture_up_to`` — the chat-turn worker."""
    import importlib

    module = importlib.import_module("persona_api.background.chat_turn_worker")
    src = inspect.getsource(module)
    assert ".capture_up_to(" in src

    for module_name in _OTHER_METERED_CALL_SITES:
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
