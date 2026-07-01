"""Fidelity-aware rendering of the ``document_generation`` SKILL.md (Spec P5, P5-D-4).

The composition root computes ``docgen_full_fidelity`` from ``PERSONA_SANDBOX_TEMPLATE``
(is the custom doc-gen template configured?) and passes it to this **pure, core-side
consumer** — persona-core never reads the env, so the api→core decoupling holds.

The SKILL.md carries ONE fidelity-fenced block, the ``pptx`` section, because pptx is
the only format whose availability is an **up-front offer** decision (``python-pptx``
is the sole route to ``.pptx``; a try-import inside the code is too late — the model
has already committed to the format). The fence has two variants::

    <!--fidelity:full-->   … python-pptx present → produce .pptx …
    <!--fidelity:else-->   … not present → offer .docx/.md up-front …
    <!--fidelity:end-->

:func:`apply_docgen_fidelity` resolves the fence to exactly one variant, so the model
sees the branch matched to the deployment. The ``pdf`` section is deliberately NOT
fenced — it teaches a template-agnostic try-import (reportlab → matplotlib), so it
needs no signal. This is the single, minimal consumption point P5-D-4 names: it reuses
the existing skill-composition path (the factory maps scanned specs through it); it is
NOT a parallel config channel and adds no env read to core.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from persona.skills._tokens import count_tokens

if TYPE_CHECKING:
    from persona.schema.skills import SkillSpec

__all__ = ["DOCGEN_SKILL_NAME", "apply_docgen_fidelity", "resolve_fidelity_markers"]

DOCGEN_SKILL_NAME = "document_generation"

# One fenced block: keep the ``full`` body when the custom template is active, else
# the ``else`` body; strip the markers either way. DOTALL so bodies span lines;
# non-greedy so multiple fences (future formats) each resolve independently.
_FENCE = re.compile(
    r"<!--fidelity:full-->\n?(?P<full>.*?)"
    r"<!--fidelity:else-->\n?(?P<els>.*?)"
    r"<!--fidelity:end-->\n?",
    re.DOTALL,
)


def resolve_fidelity_markers(content: str, *, full_fidelity: bool) -> str:
    """Resolve every fidelity fence in ``content`` to the active variant.

    Returns ``content`` unchanged when there is no fence. Idempotent on already-
    resolved content (no markers ⇒ no-op).
    """

    def _pick(match: re.Match[str]) -> str:
        chosen = match.group("full") if full_fidelity else match.group("els")
        return chosen.strip("\n") + "\n"

    return _FENCE.sub(_pick, content)


def apply_docgen_fidelity(spec: SkillSpec, *, full_fidelity: bool) -> SkillSpec:
    """Return ``spec`` with the ``document_generation`` SKILL.md fidelity-resolved.

    A no-op for any other skill, or a doc-gen spec with no fence (defensive). The
    token count is recomputed so the injector budgets the delivered variant, not the
    fenced source. Non-doc-gen skills pass through untouched.
    """
    if spec.name != DOCGEN_SKILL_NAME or "<!--fidelity:" not in spec.content:
        return spec
    new_content = resolve_fidelity_markers(spec.content, full_fidelity=full_fidelity)
    return spec.model_copy(
        update={"content": new_content, "content_token_count": count_tokens(new_content)}
    )
