"""Unit — the deterministic Fly Machine name (Spec N6, T2b, N6-D-7a condition 1).

The name is the crash-window adoption anchor: it MUST be a pure function of
``(owner_id, server_id)`` so reconciliation recomputes the same name with nothing stored.
"""

from __future__ import annotations

import re

from persona_api.mcp.fly_runtime import derive_machine_name


class TestDeriveMachineName:
    def test_is_deterministic(self) -> None:
        a = derive_machine_name("owner-1", "server-1")
        b = derive_machine_name("owner-1", "server-1")
        assert a == b

    def test_differs_by_owner_and_by_server(self) -> None:
        base = derive_machine_name("owner-1", "server-1")
        assert derive_machine_name("owner-2", "server-1") != base
        assert derive_machine_name("owner-1", "server-2") != base

    def test_owner_server_pair_is_not_ambiguous(self) -> None:
        # A naive ``f"{owner}-{server}"`` would collide ("a","b-c") vs ("a-b","c").
        # The hash of the joined pair must not.
        assert derive_machine_name("a", "b-c") != derive_machine_name("a-b", "c")

    def test_is_fly_name_safe_and_bounded(self) -> None:
        # Long UUID-shaped inputs still yield a short, lowercase, letter-initial name.
        name = derive_machine_name(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
        assert re.fullmatch(r"[a-z][a-z0-9-]{1,40}", name)
        assert len(name) <= 40
