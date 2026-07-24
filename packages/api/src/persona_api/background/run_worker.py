"""Background agentic-run worker + in-process event bus (spec 08, T11, D-08-5).

A run executes as an ``asyncio.Task``. Its lifecycle state lives in a per-run
:class:`RunHandle`:

- an **event queue** (``asyncio.Queue[RunEvent | None]``) — the loop's
  ``on_event`` pushes each event; the SSE ``/events`` endpoint drains it; a
  ``None`` sentinel signals end-of-stream.
- a **response queue** (``asyncio.Queue[str]``) — ``/respond`` pushes the user's
  answer; the loop's ``user_respond`` callback awaits it (D-06-10).
- a **CancelToken** — ``/cancel`` flips it; the loop stops at the next step
  boundary (D-06-7).

Each step is persisted to ``runs.steps`` as it accumulates (crash-viewable, not
resumable — S08-2), and the final ``Run`` (status/output) is written on
completion. In-process + single-worker (S08-4); a process restart loses the
task but the persisted steps remain viewable.

The §11 fallback (ask-user terminates the run, user responds as a new run) is
available if the blocking callback proves fragile — not the v0.1 default.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from persona.billing import BillingConfig, credits_charged
from persona.errors import DailySpendCapExceededError
from persona.logging import get_logger
from persona_runtime.agentic.run import CancelToken, RunStatus
from persona_runtime.cost import compute_turn_cost
from sqlalchemy import text, update

from persona_api.db.models import runs as runs_t
from persona_api.middleware.rls_context import current_user_id
from persona_api.sandbox import (
    SandboxRequestContext,
    reset_sandbox_request_context,
    set_sandbox_request_context,
)
from persona_api.services import notifications_service
from persona_api.services.persona_service import persona_name_from_yaml
from persona_api.services.synthesis_trigger import enqueue_run_synthesis

# Spec P6 (D4-c): terminal run status → (bell level, i18n key). Copy is stored
# locale-neutral (P6-D-5); the web resolves the key. A status not here is not
# surfaced (e.g. a non-terminal that slips through).
_RUN_TERMINAL_COPY: dict[str, tuple[str, str]] = {
    "completed": ("success", "notifications.run.completed"),
    "error": ("error", "notifications.run.failed"),
    "cancelled": ("info", "notifications.run.cancelled"),
    "max_steps_reached": ("warning", "notifications.run.maxSteps"),
}


if TYPE_CHECKING:
    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.agentic.loop import AgenticLoop
    from persona_runtime.agentic.run import Run, StepUsage
    from persona_runtime.cost import CostSource
    from sqlalchemy import Engine

    from persona_api.billing import StripeGateway
    from persona_api.editions.credits_policy import CreditsPolicy
    from persona_api.jobs.queue import JobQueue
    from persona_api.realtime.channel import UserEventChannel
    from persona_api.services.within_runtime_origination import WithinRuntimeOriginator

_log = get_logger("api.run_worker")

__all__ = ["RunHandle", "RunRegistry"]


class RunHandle:
    """The in-process state of one running agentic run."""

    def __init__(self, run_id: str, owner_id: str) -> None:
        self.run_id = run_id
        self.owner_id = owner_id
        self.events: asyncio.Queue[RunEvent | None] = asyncio.Queue()
        self.responses: asyncio.Queue[str] = asyncio.Queue()
        self.cancel_token = CancelToken()
        self.task: asyncio.Task[None] | None = None
        #: Spec R7 (R7-D-4): the durable long-op concurrency slot held for this run's
        #: lifetime; reserved in ``start_run`` (pre-persist), released in ``_run``'s
        #: terminal ``finally``. ``None`` = no cap wired (community / uncapped).
        self.op_token: str | None = None

    async def on_event(self, event: RunEvent) -> None:
        """The loop's event callback: publish to the SSE queue."""
        await self.events.put(event)

    async def user_respond(self, _question: str) -> str:
        """The loop's ask-user callback: block until ``/respond`` pushes an answer."""
        return await self.responses.get()


class _RunBillingWatcher:
    """Meters an agentic run per step + cuts it off on exhaustion (Spec M3, T4a).

    The run's ``on_step_usage`` callback (mirrors ``_BoxWatcher``'s ``on_event``,
    D-06-7): each completed step's model-call usage is priced like a chat turn
    (``compute_turn_cost`` — OpenRouter ``usage.cost`` actual preferred; else the
    resolver estimate; else the floor) and CAPTURED incrementally (``capture_up_to``
    — floored, caller-paid, never negative). When the balance can't cover the step
    (a partial capture) or hits 0, the run's :class:`CancelToken` is flipped, so the
    loop stops at the NEXT step boundary — the in-flight step finishes cleanly, and a
    runaway loop cannot consume unbounded uncharged cost. The DB write is offloaded
    off the event loop so the per-step billing never stalls other background tasks.
    """

    def __init__(
        self,
        *,
        policy: CreditsPolicy,
        rls_engine: Engine,
        owner_id: str,
        cancel_token: CancelToken,
        cost_source: CostSource | None,
        billing_config: BillingConfig,
        floor: int,
        gateway: StripeGateway | None = None,
    ) -> None:
        self._policy = policy
        self._engine = rls_engine
        self._owner = owner_id
        self._cancel = cancel_token
        self._cost_source = cost_source
        self._config = billing_config
        self._floor = floor
        # Spec M4 T7b: Pro auto-top-up off the hot path — fired when a per-step capture
        # CROSSES below $2. None → community/flag-off (no auto-top-up).
        self._gateway = gateway
        self._topup_tasks: set[asyncio.Task[None]] = set()

    async def on_step_usage(self, usage: StepUsage) -> None:
        cost_cents, basis = compute_turn_cost(
            provider=usage.provider,
            model=usage.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            actual_cost_usd=usage.cost_usd,
            source=self._cost_source,
        )
        charge = credits_charged(
            provider_cents=cost_cents,
            infra_flat_cents=0.0,  # infra via the per-step floor (D-M3-4 amendment)
            markup=self._config.credit_markup,
            floor=self._floor,
        )
        try:
            captured, new_balance = await asyncio.to_thread(
                self._policy.capture_up_to,
                rls_engine=self._engine,
                user_id=self._owner,
                amount=charge,
                reason=f"agentic_run:{basis}",
                cost_cents=cost_cents,
                cost_basis=basis,
            )
        except DailySpendCapExceededError:
            # The per-UTC-day spend cap was hit (the policy already audited the
            # refusal). Cut the run off at the next step boundary.
            _log.warning(
                "agentic run hit the daily spend cap; cutting off owner={owner}", owner=self._owner
            )
            self._cancel.cancel()
            return
        # Spec M4 T7b: a per-step capture may have CROSSED the $2 floor — fire the Pro
        # auto-top-up OFF the hot path (detached; the next step never waits on Stripe).
        # ``old_balance = new_balance + captured`` (the pre-capture total).
        self._schedule_auto_topup(old_balance=new_balance + captured, new_balance=new_balance)
        if captured < charge or new_balance <= 0:
            # Balance exhausted — capture ≤ balance landed (floored at 0, never
            # negative); flip the token so the run stops at the next step boundary.
            _log.warning(
                "agentic run credits exhausted; cutting off at the next step boundary "
                "owner={owner} captured={captured} charge={charge} balance={balance}",
                owner=self._owner,
                captured=captured,
                charge=charge,
                balance=new_balance,
            )
            self._cancel.cancel()

    def _schedule_auto_topup(self, *, old_balance: int, new_balance: int) -> None:
        """Fire the Pro auto-top-up trigger OFF the hot path (Spec M4, T7b).

        Detaches a task that runs :func:`maybe_auto_topup` on a worker thread so the Stripe
        round-trip never blocks the run's step loop. A no-op for everyone except a Pro,
        opted-in owner who just crossed below $2 (the guards live in the trigger). Community /
        flag-off (``gateway is None``) → not scheduled. Errors are swallowed (a run's billing
        side effect must never break the run); the task is tracked to avoid GC.
        """
        if self._gateway is None:
            return
        gateway = self._gateway
        engine = self._engine
        owner = self._owner

        async def _run() -> None:
            from persona_api.billing.autotopup import maybe_auto_topup  # noqa: PLC0415

            try:
                await asyncio.to_thread(
                    maybe_auto_topup,
                    rls_engine=engine,
                    gateway=gateway,
                    user_id=owner,
                    old_balance=old_balance,
                    new_balance=new_balance,
                )
            except Exception:  # noqa: BLE001 — a billing side effect must never break the run
                _log.opt(exception=True).warning(
                    "auto-top-up task failed for owner={owner}", owner=owner
                )

        try:
            task = asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            return  # no running loop — skip the convenience
        self._topup_tasks.add(task)
        task.add_done_callback(self._topup_tasks.discard)


class RunRegistry:
    """App-scoped registry of in-flight runs. Single-worker, in-process (S08-4)."""

    def __init__(
        self,
        rls_engine: Engine,
        *,
        job_queue: JobQueue | None = None,
        origination: WithinRuntimeOriginator | None = None,
        event_channel: UserEventChannel | None = None,
        credits_policy: CreditsPolicy | None = None,
        gateway: StripeGateway | None = None,
        cost_source: CostSource | None = None,
        billing_config: BillingConfig | None = None,
        agentic_floor: int = 1,
    ) -> None:
        self._engine = rls_engine
        self._handles: dict[str, RunHandle] = {}
        # Spec M4 T7b: the Stripe gateway drives Pro auto-top-up off the hot path (fired
        # after a per-step capture that CROSSES below $2). None → community/flag-off.
        self._gateway = gateway
        # Spec M3 (T4a): incremental caller-paid billing. ``credits_policy`` None →
        # no billing (unit/community-unmetered shape). ``cost_source`` None → the
        # static-only pricing default (OpenRouter actuals pass through; static-table
        # providers priced; catalog-only models fall to the floor). The floor is the
        # per-step minimum (each step is one floored LLM call — infra via the floor,
        # D-M3-4 amendment).
        self._credits_policy = credits_policy
        self._cost_source = cost_source
        self._billing_config = billing_config or BillingConfig()
        self._agentic_floor = agentic_floor
        # Spec A11: the in-process SSE bus. A committed run_terminal notification pings
        # the owner's open tabs live (notification.created); ``None`` → durable-only.
        self._event_channel = event_channel
        # Spec K2 (T8d): durable queue for off-critical-path synthesis at run-end
        # (None until composed → safe no-op). Additive to the registry.
        self._job_queue = job_queue
        # Optional within-runtime origination (Spec C0, T7). ``None`` → no
        # origination (existing behaviour, byte-unchanged — criterion 10); when
        # injected, a completed run originates its conclusion as a delivered
        # message (criterion 7). Additive + best-effort: never fails the run.
        self._origination = origination

    def get(self, run_id: str) -> RunHandle | None:
        return self._handles.get(run_id)

    def start(
        self,
        *,
        run_id: str,
        owner_id: str,
        loop: AgenticLoop,
        task_text: str,
        op_token: str | None = None,
    ) -> RunHandle:
        """Create a handle and launch the run as an ``asyncio.Task``.

        ``op_token`` (R7-D-4) is the durable long-op concurrency slot reserved by the
        caller pre-persist; the worker releases it in its terminal ``finally``.
        """
        handle = RunHandle(run_id, owner_id)
        handle.op_token = op_token
        self._handles[run_id] = handle
        handle.task = asyncio.create_task(self._run(handle, loop, task_text))
        return handle

    async def _run(self, handle: RunHandle, loop: AgenticLoop, task_text: str) -> None:
        """The task body: drive the loop, persist progress + final Run, end the stream.

        The background task runs OUTSIDE any request scope, so it sets the RLS
        contextvar to the run's owner for its own DB writes — otherwise the
        checkout listener scopes to '' and RLS silently blocks the persistence
        UPDATEs (verified: a background UPDATE with no scope affects 0 rows).
        The loop's own store calls (memory_chunks) are likewise scoped to the
        owner via this contextvar — correct, since the run acts as the owner.
        """
        token = current_user_id.set(handle.owner_id)
        # Bind the per-request sandbox context for the run's lifetime, mirroring
        # chat_service.stream_chat. The file tools (file_read / file_write) and
        # code_execution read this contextvar to resolve their scoped root /
        # session: file_read/file_write fail CLOSED when nothing is bound (the
        # post-security-fix behaviour), so a run with no context bound errors on
        # every file tool. The owner is the run's owner (file-tool root →
        # <workspace_root>/<owner_id>/<persona_id>); the conversation_id slot is
        # the run_id so the run gets its OWN sandbox session (session_id =
        # owner_id:run_id), distinct from any chat conversation. Isolation is
        # intact: the root never escapes the run's owner/persona. Reset in the
        # finally regardless of completion / cancellation / error.
        sandbox_token = set_sandbox_request_context(
            SandboxRequestContext(owner_id=handle.owner_id, conversation_id=handle.run_id)
        )
        # Accumulate the per-step event log so a restart mid-run leaves the run
        # VIEWABLE (S08-2: progress visible, not resumable). The final Run (with
        # the authoritative Step objects) overwrites this on completion.
        event_log: list[dict[str, object]] = []

        async def _on_event(event: RunEvent) -> None:
            await handle.on_event(event)
            event_log.append(event.model_dump(mode="json"))
            self._persist_progress(handle.run_id, event_log)

        # Spec M3 (T4a): the per-step billing watcher (caller-paid incremental
        # deduct + exhaustion cutoff). None policy → no billing (unit/community).
        billing_watcher = (
            _RunBillingWatcher(
                policy=self._credits_policy,
                rls_engine=self._engine,
                owner_id=handle.owner_id,
                cancel_token=handle.cancel_token,
                cost_source=self._cost_source,
                billing_config=self._billing_config,
                floor=self._agentic_floor,
                gateway=self._gateway,
            )
            if self._credits_policy is not None
            else None
        )
        try:
            if billing_watcher is not None:
                run = await loop.run(
                    task_text,
                    on_event=_on_event,
                    user_respond=handle.user_respond,
                    cancel_token=handle.cancel_token,
                    on_step_usage=billing_watcher.on_step_usage,
                )
            else:
                # No billing wired — keep the call byte-identical to pre-M3 so a loop
                # double without ``on_step_usage`` (unit tests) is unaffected.
                run = await loop.run(
                    task_text,
                    on_event=_on_event,
                    user_respond=handle.user_respond,
                    cancel_token=handle.cancel_token,
                )
            # Persist by the API's run_id (the DB row), NOT run.id — the loop
            # assigns its own internal id, distinct from the API's row id.
            self._persist_final(handle.run_id, run)
            # Spec K2 (T8d): a completed agentic run feeds synthesis (D-06-8 — what
            # the run revealed about the user enters the graph, criterion 12).
            enqueue_run_synthesis(
                self._job_queue,
                owner_id=handle.owner_id,
                run_id=handle.run_id,
                persona_id=run.persona_id,
            )
            # Within-runtime origination (Spec C0, T7, criterion 7): a completed
            # run originates its conclusion as a delivered message, pushed inline
            # on this run's open stream BEFORE the end sentinel. Best-effort — a
            # failure logs and never flips the completed run to error.
            await self._maybe_originate(handle, run)
        except Exception as exc:  # noqa: BLE001 — a background task must never crash silently
            _log.error("agentic run {rid} failed: {err}", rid=handle.run_id, err=str(exc))
            self._persist_error(handle.run_id, str(exc))
        finally:
            reset_sandbox_request_context(sandbox_token)
            # Spec R7 (R7-D-4): free the durable long-op slot on EVERY terminal path
            # (clean / cancel / error / shutdown). Best-effort — teardown must never
            # crash (the TTL staleness sweep in ``admit_long_op`` is the crash backstop).
            self._release_op_slot(handle)
            current_user_id.reset(token)
            await handle.events.put(None)  # end-of-stream sentinel for SSE

    def _release_op_slot(self, handle: RunHandle) -> None:
        if handle.op_token is None:
            return
        from persona.concurrency import release_long_op  # noqa: PLC0415

        try:
            release_long_op(rls_engine=self._engine, user_id=handle.owner_id, op_id=handle.op_token)
        except Exception as exc:  # noqa: BLE001 — teardown must never crash
            _log.warning(
                "agentic run concurrency-slot release failed rid={rid}: {err}",
                rid=handle.run_id,
                err=str(exc),
            )

    async def _maybe_originate(self, handle: RunHandle, run: Run) -> None:
        """Fire within-runtime origination on a completed run (Spec C0, T7).

        Best-effort and additive: no-op when origination is not wired or the run
        did not complete; a failure logs and never propagates (origination must
        never turn a completed run into an errored one — criterion 10).
        """
        if self._origination is None or run.status is not RunStatus.COMPLETED:
            return
        try:
            await self._origination.originate_run_conclusion(handle, run)
        except Exception as exc:  # noqa: BLE001 — origination is additive; never fail the run
            _log.warning(
                "within-runtime origination failed run={rid}: {err}",
                rid=handle.run_id,
                err=str(exc),
            )

    def _persist_progress(self, run_id: str, event_log: list[dict[str, object]]) -> None:
        """Snapshot the event log to runs.steps as it grows (crash-viewable)."""
        with self._engine.begin() as conn:
            conn.execute(update(runs_t).where(runs_t.c.id == run_id).values(steps=event_log))

    def _persist_final(self, run_id: str, run: Run) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(runs_t)
                .where(runs_t.c.id == run_id)
                .values(
                    status=str(run.status),
                    steps=[s.model_dump(mode="json") for s in run.steps],
                    output=run.output,
                    error=run.error,
                    finished_at=run.finished_at,
                )
            )
        # Server-authored run-terminal notification (P6-D-3/D4-c) — separate,
        # best-effort side-effect AFTER the authoritative persist commits.
        self._notify_run_terminal(run_id, str(run.status))

    def _persist_error(self, run_id: str, message: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(runs_t)
                .where(runs_t.c.id == run_id)
                .values(status=str(RunStatus.ERROR), error=message)
            )
        self._notify_run_terminal(run_id, str(RunStatus.ERROR))

    def _notify_run_terminal(self, run_id: str, status: str) -> None:
        """Write the durable run-terminal bell notification (Spec P6, D4-c).

        Server-authored so the owner is notified even off-view / on another device
        (the cross-device feed). Idempotent via the ``(owner, kind, ref_id)`` key —
        a retry / restart re-run is a no-op (P6-D-11). **Best-effort: NEVER raises**
        — the run persist above is authoritative and must not fail because the bell
        row couldn't be written (D-P6-12). Owner comes from the RLS contextvar the
        worker binds for the run's lifetime; ``persona`` name (for the copy) is read
        RLS-scoped by ``run_id`` so it covers both the final + error paths.
        """
        copy = _RUN_TERMINAL_COPY.get(status)
        if copy is None:
            return
        owner_id = current_user_id.get()
        if not owner_id:
            return
        level, message_key = copy
        try:
            # One owner-scoped transaction, separate from the run persist above:
            # look up the persona name for the copy, then the idempotent write.
            with self._engine.begin() as conn:
                row = conn.execute(
                    text(
                        "SELECT p.yaml FROM runs r "
                        "JOIN personas p ON p.id = r.persona_id AND p.owner_id = r.owner_id "
                        "WHERE r.id = :rid"
                    ),
                    {"rid": run_id},
                ).first()
                params: dict[str, str] = {}
                if row is not None and row.yaml:
                    name = persona_name_from_yaml(row.yaml)
                    if name:
                        params["persona"] = name
                notifications_service.create_notification(
                    conn=conn,
                    owner_id=owner_id,
                    kind="run_terminal",
                    ref_id=run_id,
                    level=level,
                    message_key=message_key,
                    params=params,
                )
            # Spec A11: the write above COMMITS on the `with` block exit; emit the live
            # bell ping AFTER commit so the client's refetch finds the row (post-commit
            # rule). Still inside the advisory guard — a publish never fails the persist.
            notifications_service.publish_notification_created(
                self._event_channel, owner_id=owner_id, kind="run_terminal", ref_id=run_id
            )
        except Exception as exc:  # noqa: BLE001 — advisory; never fail the run persist
            _log.warning(
                "run-terminal notification write failed run={rid}: {err}",
                rid=run_id,
                err=str(exc),
            )

    async def aclose(self) -> None:
        """Cancel all in-flight run tasks on shutdown (S08-2: lost, but viewable)."""
        for handle in self._handles.values():
            if handle.task is not None and not handle.task.done():
                handle.cancel_token.cancel()
                handle.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await handle.task


# Re-export the callback aliases for the service's type hints.
OnEvent = "Callable[[RunEvent], Awaitable[None]]"
UserRespond = "Callable[[str], Awaitable[str]]"
