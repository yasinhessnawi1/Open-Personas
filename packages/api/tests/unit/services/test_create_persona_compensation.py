"""A create that fails after its row committed is undone before the error is raised (R9-293).

``create_persona`` commits the personas row before ``registry.load_persona`` indexes the
identity. A failure there used to leave a half-made persona in the owner's list; now the
caller's ``compensate`` runs with the new persona's id, and the original error is raised.
"""

from __future__ import annotations

from typing import Any

import pytest
from persona_api.services import persona_service

_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy law assistant
  background: |
    Helps tenants understand husleieloven.
  language_default: en
self_facts:
  - fact: Specialised in Norwegian residential tenancy.
    confidence: 1.0
worldview: []
"""


class _Begin:
    def __enter__(self) -> _Begin:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, *_a: Any) -> None:  # noqa: ANN401
        return None


class _Engine:
    def begin(self) -> _Begin:
        return _Begin()


class _IndexFails:
    def load_persona(self, persona: Any) -> None:  # noqa: ANN401, ARG002
        msg = "the identity index went away"
        raise RuntimeError(msg)


def _create(monkeypatch: pytest.MonkeyPatch, compensate: Any) -> None:  # noqa: ANN401
    monkeypatch.setattr(persona_service, "_build_registry", lambda *_a: _IndexFails())
    persona_service.create_persona(
        rls_engine=_Engine(),  # type: ignore[arg-type]
        embedder=object(),  # type: ignore[arg-type]
        audit_root=None,  # type: ignore[arg-type]
        owner_id="u1",
        yaml_str=_YAML,
        compensate=compensate,
    )


def test_a_failing_index_is_undone_and_its_error_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    undone: list[str] = []

    with pytest.raises(RuntimeError, match="identity index"):
        _create(monkeypatch, undone.append)

    assert len(undone) == 1
    assert undone[0].startswith("persona_")


def test_an_undo_that_fails_never_hides_the_create_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _undo_fails(_pid: str) -> None:
        msg = "the erasure failed too"
        raise OSError(msg)

    with pytest.raises(RuntimeError, match="identity index"):
        _create(monkeypatch, _undo_fails)


def test_a_create_that_succeeds_undoes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    undone: list[str] = []

    class _Indexes:
        def load_persona(self, persona: Any) -> None:  # noqa: ANN401, ARG002
            return None

    monkeypatch.setattr(persona_service, "_build_registry", lambda *_a: _Indexes())
    persona_id = persona_service.create_persona(
        rls_engine=_Engine(),  # type: ignore[arg-type]
        embedder=object(),  # type: ignore[arg-type]
        audit_root=None,  # type: ignore[arg-type]
        owner_id="u1",
        yaml_str=_YAML,
        compensate=undone.append,
    )

    assert persona_id.startswith("persona_")
    assert undone == []
