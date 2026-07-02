"""Unit tests for the K6 profile surface — name normalisation + request semantics.

Pure (no DB): the normalisation posture (K6-D-8) and the ``UpdateProfileRequest``
PATCH semantics (omitted-vs-null) are the contract the service + route depend on.
The DB round-trip lives in ``tests/integration/test_user_profile.py``.
"""

from __future__ import annotations

import pytest
from persona_api.schemas import UpdateProfileRequest
from persona_api.services import user_service
from pydantic import ValidationError


class TestComposeDisplayName:
    def test_both_parts_join_with_a_space(self) -> None:
        assert user_service.compose_display_name("Ada", "Lovelace") == "Ada Lovelace"

    def test_first_only(self) -> None:
        assert user_service.compose_display_name("Ada", None) == "Ada"

    def test_last_only(self) -> None:
        assert user_service.compose_display_name(None, "Lovelace") == "Lovelace"

    def test_both_none_is_none(self) -> None:
        # A nameless account → None → the prompt omits the name line (null-safe).
        assert user_service.compose_display_name(None, None) is None

    def test_both_blank_is_none(self) -> None:
        assert user_service.compose_display_name("", "") is None


class TestNormalizeName:
    def test_none_stays_none(self) -> None:
        assert user_service.normalize_name(None) is None

    def test_empty_string_becomes_none(self) -> None:
        assert user_service.normalize_name("") is None

    def test_whitespace_only_becomes_none(self) -> None:
        # A name is never a hard requirement — blank input is "unset", not "  ".
        assert user_service.normalize_name("   \t  ") is None

    def test_trims_outer_whitespace(self) -> None:
        assert user_service.normalize_name("  Ada  ") == "Ada"

    def test_strips_newlines_and_tabs(self) -> None:
        # Control chars can never break the prompt structure the name renders into.
        out = user_service.normalize_name("Ada\nLovelace\t")
        assert out is not None
        assert "\n" not in out
        assert "\t" not in out
        assert out == "AdaLovelace"

    def test_strips_c0_control_chars(self) -> None:
        assert user_service.normalize_name("\x00Bob\x07") == "Bob"

    def test_preserves_unicode_and_emoji_and_spaces(self) -> None:
        # Ordinary spaces + letters/emoji/diacritics/RTL are kept as-is.
        assert user_service.normalize_name("José 😀 Löve") == "José 😀 Löve"

    def test_caps_length_at_max(self) -> None:
        out = user_service.normalize_name("x" * 500)
        assert out is not None
        assert len(out) == user_service.NAME_MAX_LENGTH == 100


class TestUpdateProfileRequestSemantics:
    def test_empty_patch_sets_nothing(self) -> None:
        # A no-field PATCH is a valid no-op (a read).
        assert UpdateProfileRequest().model_dump(exclude_unset=True) == {}

    def test_provided_field_is_tracked(self) -> None:
        body = UpdateProfileRequest(first_name="Ada")
        assert body.model_dump(exclude_unset=True) == {"first_name": "Ada"}

    def test_explicit_null_is_a_clear_not_an_omission(self) -> None:
        # Sending null is distinguishable from omitting the field → clears it.
        body = UpdateProfileRequest(first_name=None)
        assert body.model_dump(exclude_unset=True) == {"first_name": None}

    def test_rejects_overlong_name_at_boundary(self) -> None:
        with pytest.raises(ValidationError):
            UpdateProfileRequest(first_name="x" * 101)

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            UpdateProfileRequest(nickname="Ace")  # type: ignore[call-arg]
