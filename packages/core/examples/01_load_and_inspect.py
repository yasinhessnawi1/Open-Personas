"""Load a persona YAML and look inside it — no model key required.

A persona is a typed document, not a prompt string: identity (immutable at
runtime), constraints, self-facts with confidence, and worldview claims with
epistemic tags. This example loads one and prints what it's made of.

Run from ``packages/core/examples/``:

    uv run python 01_load_and_inspect.py
"""

from __future__ import annotations

from pathlib import Path

from persona.schema.persona import Persona

persona = Persona.from_yaml(Path(__file__).parent / "astrid_tenancy_law.yaml")

print(f"{persona.identity.name} — {persona.identity.role}")
print(f"language: {persona.identity.language_default}")

print(f"\nconstraints ({len(persona.identity.constraints)}):")
for c in persona.identity.constraints:
    print(f"  · {c}")

print(f"\nself-facts ({len(persona.self_facts)}):")
for f in persona.self_facts:
    print(f"  · [{f.confidence:.2f}] {f.fact}")

print(f"\nworldview ({len(persona.worldview)}):")
for w in persona.worldview:
    print(f"  · ({w.epistemic}, {w.confidence:.2f}) {w.claim}")

print(f"\ntools: {list(persona.tools)}")
print(f"skills: {list(persona.skills)}")
