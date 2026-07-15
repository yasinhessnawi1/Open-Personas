"""Unit tests for the per-provider voice-memory guard (Spec V14-T5b, review finding I1).

No DB, no model — exercises ``_remember_voice_by_provider`` directly, mirroring
``test_persona_safety_guard.py``'s shape: the floor that guarantees a persona's
voice choice is recorded into ``identity.voice_by_provider`` on BOTH the
in-memory persona and the *stored* YAML, regardless of which surface set the
voice (builder-authored YAML at create, or a manual re-pick at update — the two
paths that do NOT go through ``persona_service.set_voice``'s own recording).
"""

from __future__ import annotations

import yaml
from persona.schema.persona import Persona
from persona_api.services.persona_service import _remember_voice_by_provider, load_persona_from_yaml

_NO_VOICE_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Tenancy assistant
  background: Helps tenants understand the law.
"""

_WITH_VOICE_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Tenancy assistant
  background: Helps tenants understand the law.
  voice: cartesia:v1
"""

_ALREADY_REMEMBERED_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Tenancy assistant
  background: Helps tenants understand the law.
  voice: cartesia:v1
  voice_by_provider:
    cartesia: v1
"""

_WITH_OTHER_PROVIDER_MEMORY_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Tenancy assistant
  background: Helps tenants understand the law.
  voice: elevenlabs:el1
  voice_by_provider:
    cartesia: v1
"""


def _load(yaml_str: str) -> Persona:
    return load_persona_from_yaml(yaml_str, persona_id="p_test", owner_id="u_test")


def test_voiceless_persona_is_returned_unchanged_no_yaml_churn() -> None:
    persona = _load(_NO_VOICE_YAML)
    guarded, stored_yaml = _remember_voice_by_provider(persona, _NO_VOICE_YAML)
    assert guarded is persona
    assert stored_yaml == _NO_VOICE_YAML


def test_voiced_persona_gets_the_memory_in_the_stored_yaml() -> None:
    persona = _load(_WITH_VOICE_YAML)
    guarded, stored_yaml = _remember_voice_by_provider(persona, _WITH_VOICE_YAML)
    assert guarded.identity.voice_by_provider == {"cartesia": "v1"}
    reloaded = yaml.safe_load(stored_yaml)
    assert reloaded["identity"]["voice_by_provider"] == {"cartesia": "v1"}
    # And it round-trips back through the validator as a real persona.
    assert _load(stored_yaml).identity.voice_by_provider == {"cartesia": "v1"}


def test_already_remembered_voice_is_a_full_noop() -> None:
    persona = _load(_ALREADY_REMEMBERED_YAML)
    guarded, stored_yaml = _remember_voice_by_provider(persona, _ALREADY_REMEMBERED_YAML)
    assert guarded is persona
    assert stored_yaml == _ALREADY_REMEMBERED_YAML


def test_a_new_provider_pick_is_merged_not_replaced() -> None:
    """The manual re-pick twin of set_voice's own merge: switching to elevenlabs
    in the editor must not erase the persona's remembered cartesia voice."""
    persona = _load(_WITH_OTHER_PROVIDER_MEMORY_YAML)
    guarded, stored_yaml = _remember_voice_by_provider(persona, _WITH_OTHER_PROVIDER_MEMORY_YAML)
    assert guarded.identity.voice_by_provider == {"cartesia": "v1", "elevenlabs": "el1"}
    reloaded = yaml.safe_load(stored_yaml)
    assert reloaded["identity"]["voice_by_provider"] == {"cartesia": "v1", "elevenlabs": "el1"}


def test_guard_is_idempotent_when_run_twice() -> None:
    persona = _load(_WITH_VOICE_YAML)
    guarded_once, yaml_once = _remember_voice_by_provider(persona, _WITH_VOICE_YAML)
    guarded_twice, yaml_twice = _remember_voice_by_provider(guarded_once, yaml_once)
    assert guarded_twice is guarded_once
    assert yaml_twice == yaml_once


def test_additive_invariant_a_persona_without_the_field_loads_unaffected() -> None:
    """Spec V14-T5b criterion: a persona authored before this field existed loads
    with ``voice_by_provider is None`` and behaves exactly as today."""
    persona = _load(_NO_VOICE_YAML)
    assert persona.identity.voice_by_provider is None
    persona_with_voice = _load(_WITH_VOICE_YAML)
    # A voice WITHOUT prior memory still loads fine — the field defaults to None,
    # not an empty dict (the guard is what populates it, not the loader).
    assert persona_with_voice.identity.voice_by_provider is None
