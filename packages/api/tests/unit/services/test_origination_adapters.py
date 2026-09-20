"""Unit tests for the A4 origination store adapters (Spec A4, composition-root wiring).

The adapters give :class:`OriginationService` its idempotent ``create_if_absent`` + ``get_optional``
over the real store shapes. These pin the two behaviours the service's invariants rest on: a miss
reads as ``None`` (not an error), and a unique-violation on create is swallowed (a racing replay is
a no-op, so origination converges on one row). The :class:`OriginatorFailureNotifier`'s durable
persistence is proven end-to-end on the real stack in the live composition test.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.approvals import ActionProposal
from persona.errors import ScheduleNotFoundError, TaskNotFoundError
from persona.tasks import Contract, Task
from persona.tools import ActionCategory
from persona_api.approvals.failure import (
    FailureAccount,
    account_for_cancel_failure,
    account_for_origination_failure,
)
from persona_api.services.origination_adapters import (
    ScheduleCreatorAdapter,
    TaskCreatorAdapter,
    render_approval_message,
    render_failure_account,
)
from sqlalchemy.exc import IntegrityError

_NOW = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


def _task(task_id: str = "t1", owner_id: str = "user-a") -> Task:
    return Task(
        id=task_id,
        owner_id=owner_id,
        persona_id="astrid",
        contract=Contract(goal="g"),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _integrity_error() -> IntegrityError:
    return IntegrityError("INSERT ...", {}, Exception("duplicate key"))


class _FakeTaskStore:
    def __init__(self, *, existing: Task | None = None, raise_on_create: bool = False) -> None:
        self._existing = existing
        self._raise = raise_on_create
        self.created: list[Task] = []

    def get(self, owner_id: str, task_id: str) -> Task:  # noqa: ARG002
        if self._existing is None:
            raise TaskNotFoundError("nope", context={"task_id": task_id})
        return self._existing

    def create(self, task: Task) -> Task:
        if self._raise:
            raise _integrity_error()
        self.created.append(task)
        return task


class _FakeScheduleStore:
    def __init__(self, *, raise_on_create: bool = False, raise_on_delete: bool = False) -> None:
        self._raise_create = raise_on_create
        self._raise_delete = raise_on_delete
        self.deleted: list[str] = []

    def create(self, schedule: object, *, now: datetime) -> object:  # noqa: ARG002
        if self._raise_create:
            raise _integrity_error()
        return schedule

    def delete(self, owner_id: str, schedule_id: str) -> None:  # noqa: ARG002
        if self._raise_delete:
            raise ScheduleNotFoundError("gone", context={"schedule_id": schedule_id})
        self.deleted.append(schedule_id)


def test_get_optional_returns_none_on_miss() -> None:
    adapter = TaskCreatorAdapter(_FakeTaskStore())  # type: ignore[arg-type]
    assert adapter.get_optional("user-a", "t1") is None


def test_get_optional_returns_the_task_when_present() -> None:
    task = _task()
    adapter = TaskCreatorAdapter(_FakeTaskStore(existing=task))  # type: ignore[arg-type]
    assert adapter.get_optional("user-a", "t1") is task


def test_create_if_absent_persists_when_new() -> None:
    store = _FakeTaskStore()
    TaskCreatorAdapter(store).create_if_absent(_task())  # type: ignore[arg-type]
    assert len(store.created) == 1


def test_create_if_absent_swallows_unique_violation() -> None:
    # A racing replay hits the PK — the adapter must treat it as an idempotent no-op, not raise.
    store = _FakeTaskStore(raise_on_create=True)
    TaskCreatorAdapter(store).create_if_absent(_task())  # type: ignore[arg-type]  # must not raise


class _StubSchedule:
    id = "sched-1"


def test_schedule_create_if_absent_swallows_unique_violation() -> None:
    store = _FakeScheduleStore(raise_on_create=True)
    ScheduleCreatorAdapter(store).create_if_absent(_StubSchedule(), now=_NOW)  # type: ignore[arg-type]


def test_schedule_delete_swallows_missing() -> None:
    store = _FakeScheduleStore(raise_on_delete=True)
    ScheduleCreatorAdapter(store).delete("user-a", "sched-x")  # type: ignore[arg-type]  # no raise


def test_schedule_delete_removes_present() -> None:
    store = _FakeScheduleStore()
    ScheduleCreatorAdapter(store).delete("user-a", "sched-x")  # type: ignore[arg-type]
    assert store.deleted == ["sched-x"]


def test_render_failure_account_carries_cause_and_options() -> None:
    account = account_for_origination_failure("task-1", cause="DB was down")
    text = render_failure_account(account)
    assert "DB was down" in text  # the honest cause, never disguised
    for option in account.options:
        assert option in text  # every concrete next step surfaced (no dead end)


@pytest.mark.parametrize("cause", ["", "   "])
def test_render_handles_the_builder_fallback_cause(cause: str) -> None:
    # The builder substitutes a fallback for an empty cause; render must still be non-empty.
    account = account_for_origination_failure("task-1", cause=cause)
    assert render_failure_account(account).strip()


def _proposal() -> ActionProposal:
    return ActionProposal(
        proposal_id="p1",
        owner_id="user_a",
        task_id="t1",
        persona_id="persona_a",
        categories=frozenset({ActionCategory.COMMUNICATE_AS_USER}),
        tool_name="send_email",
        arguments={"to": "bob@example.com"},
        description="send the appeal email to bob@example.com",
        created_at=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# R9-120: this is product copy now. A persona can say it unprompted in a chat app.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "account",
    [
        account_for_origination_failure("task-1", cause="the schedule store was unreachable"),
        account_for_origination_failure("task-1", cause=""),
        account_for_cancel_failure("task-1", cause="the leg was already running"),
    ],
)
def test_a_failure_account_reads_as_sentences(account: FailureAccount) -> None:
    """The cause begins a sentence, so it has to look like one.

    The old template ran the cause on after a full stop exactly as stored, which produced
    "I couldn't set up the task you just confirmed. the task couldn't be created just now."
    That is a visible defect anywhere and it is the persona's own voice in someone's chat
    app, so it is worth a test rather than a careful author.
    """
    text = render_failure_account(account)

    sentences = [part.strip() for part in text.split(". ") if part.strip()]
    assert sentences, "the render produced nothing"
    for sentence in sentences:
        assert sentence[0].isupper() or sentence[0].isdigit(), (
            f"a sentence starts lower-case in: {text!r}"
        )
    assert ";" not in text, "the options are read aloud by a person, not parsed from a list"


def test_every_approval_line_says_how_to_answer_it() -> None:
    """A question a person cannot tell how to answer is not a question, it is a status line.

    In the web app the buttons said how to reply. In a chat app the words have to. This
    also pins that ``expired`` offers a way forward: it used to report the closed door and
    stop, which is the dead end the failure-account rule already forbids everywhere else.
    """
    proposal = _proposal()
    for kind in ("ask", "reconfirm", "clarify", "remind", "expired"):
        line = render_approval_message(kind, proposal)
        assert proposal.description.lower()[:20] in line.lower(), (
            f"{kind} does not say what the persona actually wants to do"
        )
        assert "yes" in line.lower() or "ask me again" in line.lower(), (
            f"{kind} gives the person no way to answer or move on: {line!r}"
        )


@pytest.mark.asyncio
async def test_no_delivery_text_claims_the_person_will_see_it() -> None:
    """Saved is a claim about our records. Seen is a claim about someone's attention.

    Since R9-120 the web home is also the fallback for a connector that did not take, so
    "present on next open" quietly assumed a reader who returns to a conversation a chat
    app user has no reason to open. Asserted on the OUTCOME the operator reads rather than
    on the source, so a comment mentioning the old phrasing cannot green or red it.
    """
    from persona.schema.origination import OriginatedMessage, PersonaIdentityTag
    from persona_api.services.web_deliverer import WebAppDeliverer

    class _NoSessions:
        def lookup(self, message: OriginatedMessage) -> None:  # noqa: ARG002 - Protocol shape
            return None

    deliverer = WebAppDeliverer(
        rls_engine=object(),  # type: ignore[arg-type] - the injected sink ignores it
        sessions=_NoSessions(),
        record=lambda **_kwargs: None,
    )
    result = await deliverer.deliver(
        OriginatedMessage(
            persona=PersonaIdentityTag(persona_id="p1", display_name="Ada"),
            owner_user_id="user_a",
            content="I have something for you.",
            conversation_id="conv_1",
            created_at=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
        )
    )

    assert result.detail is not None
    assert "not yet seen" in result.detail
    assert "present on next open" not in result.detail, (
        "the delivery text promises the person will see it somewhere they may never look"
    )
