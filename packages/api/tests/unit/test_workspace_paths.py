"""Where a persona's files live, and the only sanctioned way to name a place inside them.

Every storage key and every workspace path for a persona is ``{owner}/{persona}/...``. The
defect these guard against: the pieces were joined raw, so a persona id of ``..`` made the
persona's folder the whole workspace, and a ref of ``../../other/persona/uploads/x.png``
stayed inside the workspace root (which is all the resolver checks) while leaving the persona.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - used at runtime in fixtures

import pytest
from persona.errors import SandboxViolationError
from persona_api import workspace_paths
from persona_api.services.artifact_storage import artifact_key

_PID = "persona_0123456789abcdef0123456789abcdef"


class TestPersonaIdFormat:
    def test_a_new_id_has_the_format_the_routes_accept(self) -> None:
        minted = workspace_paths.new_persona_id()
        assert workspace_paths.is_persona_id(minted)

    @pytest.mark.parametrize(
        "value",
        [
            "..",
            ".",
            "",
            "astrid",
            "persona_",
            "persona_0123456789ABCDEF0123456789ABCDEF",
            "persona_0123456789abcdef0123456789abcde",
            "persona_0123456789abcdef0123456789abcdef0",
            "persona_0123456789abcdef0123456789abcdef\n",
            "../persona_0123456789abcdef0123456789abcdef",
        ],
    )
    def test_anything_else_is_not_a_persona_id(self, value: str) -> None:
        assert not workspace_paths.is_persona_id(value)


class TestPersonaKey:
    def test_a_plain_relative_path_lands_under_the_persona(self) -> None:
        key = workspace_paths.persona_key("user_a", _PID, "uploads/abc.png")
        assert key == f"user_a/{_PID}/uploads/abc.png"

    def test_artifact_key_is_the_same_door(self) -> None:
        assert artifact_key("user_a", _PID, "uploads/abc.png") == f"user_a/{_PID}/uploads/abc.png"
        with pytest.raises(SandboxViolationError):
            artifact_key("user_a", _PID, "../other/uploads/abc.png")

    @pytest.mark.parametrize(
        "relative",
        [
            "",
            "/etc/passwd",
            "../x",
            "uploads/../../other/persona/x.png",
            "../../user_b/persona_ffffffffffffffffffffffffffffffff/uploads/x.png",
            "uploads/./x.png",
            "uploads//x.png",
            "uploads/x.png/",
            "uploads\\x.png",
            "uploads/x\x00.png",
            ".",
        ],
    )
    def test_a_relative_path_that_leaves_or_bends_is_refused(self, relative: str) -> None:
        with pytest.raises(SandboxViolationError):
            workspace_paths.persona_key("user_a", _PID, relative)

    @pytest.mark.parametrize(("owner", "persona"), [("..", _PID), ("user_a", ".."), ("a/b", _PID)])
    def test_an_owner_or_persona_that_is_not_one_name_is_refused(
        self, owner: str, persona: str
    ) -> None:
        with pytest.raises(SandboxViolationError):
            workspace_paths.persona_key(owner, persona, "uploads/x.png")

    def test_the_prefix_ends_at_the_persona_so_a_longer_id_never_matches(self) -> None:
        assert workspace_paths.persona_prefix("user_a", _PID) == f"user_a/{_PID}/"


class TestPersonaRoot:
    def test_the_root_is_the_owner_then_the_persona(self, tmp_path: Path) -> None:
        assert workspace_paths.persona_root(tmp_path, "user_a", _PID) == tmp_path / "user_a" / _PID

    @pytest.mark.parametrize(
        ("owner", "persona"),
        [("user_a", ".."), ("user_a", "."), ("..", _PID), ("user_a", "x/.."), ("user_a", "")],
    )
    def test_a_root_that_would_be_anything_but_one_persona_folder_is_refused(
        self, tmp_path: Path, owner: str, persona: str
    ) -> None:
        with pytest.raises(SandboxViolationError):
            workspace_paths.persona_root(tmp_path, owner, persona)


class TestConversationFolder:
    def test_a_conversation_id_is_one_name_too(self) -> None:
        from persona_api.services import document_service

        prefix = document_service.conversation_documents_prefix("user_a", _PID, "conv-1")
        assert prefix == f"user_a/{_PID}/conversations/conv-1/documents"
        for bad in ("..", "a/b", "", "."):
            with pytest.raises(SandboxViolationError):
                document_service.conversation_documents_prefix("user_a", _PID, bad)
