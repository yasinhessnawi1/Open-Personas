"""Spec A11 T4b — ``publish_notification_created`` (the post-commit bell ping).

The helper the notification-write sites call AFTER their durable write commits: it
publishes a data-only ``notification.created`` (kind + ref_id) onto the owner's open
tabs; the client refetches ``/v1/me/notifications`` to reconcile (A11-D-2). Best-
effort: a ``None`` channel or no open tab is a no-op.
"""

from __future__ import annotations

import json

import pytest
from persona_api.realtime.channel import UserEventChannel
from persona_api.services.notifications_service import (
    publish_notification_created,
    publish_task_updated,
)


@pytest.mark.asyncio
async def test_publish_emits_notification_created_to_the_open_tab() -> None:
    ch = UserEventChannel(epoch="pub")
    tab = ch.subscribe("u1")
    publish_notification_created(ch, owner_id="u1", kind="run_terminal", ref_id="run1")
    frame = (await tab.next()).decode()
    assert "event: notification.created\n" in frame
    body = json.loads(frame.split("data: ", 1)[1].rstrip("\n"))
    assert body["kind"] == "run_terminal"
    assert body["ref_id"] == "run1"
    assert body["notification_id"] is None  # data-only ping; client refetches


def test_publish_with_none_channel_is_a_noop() -> None:
    publish_notification_created(None, owner_id="u1", kind="run_terminal", ref_id="run1")


def test_publish_with_no_open_tab_is_a_noop() -> None:
    ch = UserEventChannel(epoch="pub")
    publish_notification_created(ch, owner_id="offline", kind="persona_ready", ref_id="p1")
    assert ch.has_subscribers("offline") is False


@pytest.mark.asyncio
async def test_publish_is_owner_scoped() -> None:
    ch = UserEventChannel(epoch="pub")
    a = ch.subscribe("tenant_a")
    ch.subscribe("tenant_b")
    publish_notification_created(ch, owner_id="tenant_b", kind="run_terminal", ref_id="r")
    assert a.qsize() == 0  # A never sees B's bell ping


# --- task.updated (Spec A11/A6 W8) -----------------------------------------


@pytest.mark.asyncio
async def test_publish_task_updated_emits_to_the_open_tab() -> None:
    ch = UserEventChannel(epoch="pub")
    tab = ch.subscribe("u1")
    publish_task_updated(ch, owner_id="u1", task_id="t1", state="completed")
    frame = (await tab.next()).decode()
    assert "event: task.updated\n" in frame
    body = json.loads(frame.split("data: ", 1)[1].rstrip("\n"))
    assert body["task_id"] == "t1"
    assert body["state"] == "completed"  # data-only; the surface refetches by task_id


def test_publish_task_updated_with_none_channel_is_a_noop() -> None:
    publish_task_updated(None, owner_id="u1", task_id="t1", state="cancelled")


@pytest.mark.asyncio
async def test_publish_task_updated_is_owner_scoped() -> None:
    ch = UserEventChannel(epoch="pub")
    a = ch.subscribe("tenant_a")
    ch.subscribe("tenant_b")
    publish_task_updated(ch, owner_id="tenant_b", task_id="t9", state="waiting")
    assert a.qsize() == 0  # A never sees B's task ping
