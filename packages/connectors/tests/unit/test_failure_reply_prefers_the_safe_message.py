"""A connector failure reply carries the api's sanitised sentence (R9-097 remainder).

R9-097 stopped the web app printing a tier exhaustion verbatim (our provider names,
model ids, tier and error classes) and replaced it with a sentence that says the
models are busy and to try again shortly. The connector never got that improvement.
It receives the SAME message on its error frame and threw every one away in favour
of "Sorry, something went wrong on my end", because from inside the connector a
sanitised sentence and a raw exception are both just a string.

So the identical capacity blip read as a transient hiccup in the web app and as a
broken product on Telegram. The fix carries a marker with the message; these pin
that only a marked message is forwarded, since forwarding an unmarked one would
reintroduce exactly the leak R9-097 closed.
"""

from __future__ import annotations

import pytest
from persona_connectors.domain.flow import _failure_text
from persona_connectors.domain.system_replies import TURN_FAILED_MESSAGE
from persona_connectors.errors import ConnectorError, TurnFailedError

_SAFE = "The models are unusually busy right now, so this one didn't get through."
_RAW = (
    "every backend in MultiModelChatBackend exhausted [tier=frontier attempt_count=2 "
    "attempts_json=[{'provider': 'openrouter', 'model': 'openai/gpt-oss-20b:free'}]]"
)


def _failure(detail: str, *, user_facing: str) -> TurnFailedError:
    return TurnFailedError(
        "the persona turn failed",
        context={"conversation_id": "c1", "detail": detail, "user_facing": user_facing},
    )


def test_a_marked_message_reaches_the_user() -> None:
    """THE regression: the useful sentence must survive the trip across processes."""
    assert _failure_text(_failure(_SAFE, user_facing="true")) == _SAFE


def test_an_unmarked_message_is_never_forwarded() -> None:
    """The guard: an unmarked detail is the raw exception, which is the leak itself."""
    text = _failure_text(_failure(_RAW, user_facing="false"))
    assert text == TURN_FAILED_MESSAGE
    assert "openrouter" not in text
    assert "MultiModelChatBackend" not in text


def test_a_missing_marker_is_treated_as_unsafe() -> None:
    """Fail closed: an older api, or any frame without the key, must not leak.

    The two processes deploy separately, so a connector running this code against
    an api that predates the marker is a real state, not a hypothetical.
    """
    exc = TurnFailedError("the persona turn failed", context={"detail": _RAW})
    assert _failure_text(exc) == TURN_FAILED_MESSAGE


@pytest.mark.parametrize(
    "exc",
    [
        ConnectorError("something else entirely"),
        RuntimeError("not a connector error at all"),
        TurnFailedError("failed", context={"detail": "   ", "user_facing": "true"}),
    ],
    ids=["other-connector-error", "plain-exception", "marked-but-empty"],
)
def test_everything_else_keeps_the_generic_apology(exc: Exception) -> None:
    """The flow catches EVERY exception, so the helper must answer for all of them.

    Including a marked-but-empty detail: an empty apology is worse than a generic
    one, and the send would otherwise be a blank message.
    """
    assert _failure_text(exc) == TURN_FAILED_MESSAGE
