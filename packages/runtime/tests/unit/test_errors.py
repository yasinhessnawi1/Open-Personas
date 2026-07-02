"""Unit tests for persona_runtime.errors (T02, D-05-2).

These are also the first tests in the runtime test tree — passing them confirms
``import persona_runtime`` works under the root ``conftest.py`` sys.path
injection. NOTE: the runtime test tree has NO ``__init__.py`` files (see
research.md §implementation findings) — adding them makes ``tests.conftest`` /
``tests.unit`` collide with the core package's identically-named test packages
(``ImportPathMismatchError``). Each runtime test file is collected as a
top-level module instead, which is collision-free across packages.
"""

from __future__ import annotations

import persona_runtime
import pytest
from persona.errors import PersonaError
from persona_runtime.errors import TierNotConfiguredError


class TestTierNotConfiguredError:
    def test_is_a_persona_error(self) -> None:
        assert issubclass(TierNotConfiguredError, PersonaError)

    def test_constructs_with_message_and_context(self) -> None:
        exc = TierNotConfiguredError(
            "no tier resolvable",
            context={"requested": "frontier", "configured": ""},
        )
        assert exc.message == "no tier resolvable"
        assert exc.context == {"requested": "frontier", "configured": ""}

    def test_str_includes_context(self) -> None:
        exc = TierNotConfiguredError("no tier", context={"requested": "mid"})
        rendered = str(exc)
        assert "no tier" in rendered
        assert "requested=mid" in rendered

    def test_constructs_without_context(self) -> None:
        exc = TierNotConfiguredError("bare")
        assert exc.context == {}
        assert str(exc) == "bare"

    def test_catchable_as_persona_error(self) -> None:
        with pytest.raises(PersonaError) as exc_info:
            raise TierNotConfiguredError("x", context={"requested": "small"})
        assert exc_info.value.context["requested"] == "small"


class TestRuntimeImportable:
    """The runtime package and its test tree import cleanly (gotcha #1 closed)."""

    def test_package_imports(self) -> None:
        assert persona_runtime is not None

    def test_errors_module_exports_the_runtime_exceptions(self) -> None:
        # Originally the runtime defined exactly one exception (D-05-2). Spec 21
        # (T04, D-21-9) adds ``InvalidQuestionAnswerError`` for the one new
        # genuinely-runtime failure mode — a proactive-question answer that
        # matches neither an option nor free-form. Spec A4 (T2) adds
        # ``ScheduleParseError`` — an unrepresentable schedule phrase declined
        # honestly rather than coerced (parse-honesty). All are exported.
        from persona_runtime import errors

        assert errors.__all__ == [
            "InvalidQuestionAnswerError",
            "ScheduleParseError",
            "TierNotConfiguredError",
        ]
