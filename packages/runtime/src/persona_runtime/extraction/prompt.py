"""The versioned extraction prompt (Spec K2, T2; D-K2-3 — the Spec-10 discipline).

This is the artifact T6's hard gate grades: the §4 judgement rules, encoded for
the *floor* model (D-10-1 — stronger models inherit it for free). It is a versioned
constant (``EXTRACTION_PROMPT_VERSION``) with frozen few-shot example outputs
exposed for unit assertions; a change bumps the version and re-runs the K2-R-2
corpus. Two safety bars are encoded as hard rules: grounded-not-inferred (a
verbatim ``evidence_span`` per candidate; no speculative diagnosis) and
self-harm **means-redaction** (D-K2-7 — method/means never enter any field).

The structured output is JSON (parsed leniently by :mod:`.parse`), so no provider
``response_format`` is required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.graph.models import NodeKind
from persona.schema.conversation import ConversationMessage
from persona.wellbeing import WellbeingCategory

if TYPE_CHECKING:
    from persona.extraction import ExtractionInput

__all__ = [
    "EXAMPLE_CAUSATION_TRAP_OUTPUT",
    "EXAMPLE_MEANS_REDACTION_OUTPUT",
    "EXAMPLE_RICH_OUTPUT",
    "EXAMPLE_SMALL_TALK_OUTPUT",
    "EXAMPLE_SPECULATION_OUTPUT",
    "EXAMPLE_STATED_CAUSATION_OUTPUT",
    "EXTRACTION_PROMPT_VERSION",
    "EXTRACTION_SYSTEM_PROMPT",
    "build_extraction_messages",
]

# Bumped on any change to the rules or examples; recorded with extraction outputs
# and the K2-R-2 eval run so a behaviour change is a traceable, re-measured event.
# v2: added the proposed_relations (temporal/causal) contract + the conservative-
# causation examples (T4). v3: added rule 8 (K12, T5) — concept_name is a concise
# 1-3 word label, never a sentence or a truncation of content; trimmed the few-shot
# concept_name examples that exceeded 3 words to match. T6's hard gate grades the
# current version.
EXTRACTION_PROMPT_VERSION = "v3"

_NODE_KINDS = ", ".join(k.value for k in NodeKind)
_WELLBEING = ", ".join(c.value for c in WellbeingCategory)


# --- Frozen few-shot example OUTPUTS (the spec-by-example; also unit fixtures) ---

# A rich exchange → a HANDFUL of grounded, durable candidates (restraint).
EXAMPLE_RICH_OUTPUT = """{"candidates": [
  {"concept_name": "vegetarian diet", "content": "The user follows a vegetarian diet.",
   "node_kind": "preference", "evidence_span": "I went vegetarian about a year ago",
   "entity_mentions": [], "wellbeing_category": null, "update_intent": "none",
   "update_target_hint": null},
  {"concept_name": "daughter Lina", "content": "The user has a daughter named Lina.",
   "node_kind": "fact", "evidence_span": "my daughter Lina just started school",
   "entity_mentions": ["Lina"], "wellbeing_category": null, "update_intent": "none",
   "update_target_hint": null}
]}"""

# A speculation trap → capture the GROUNDED struggle; NEVER the inferred diagnosis.
EXAMPLE_SPECULATION_OUTPUT = """{"candidates": [
  {"concept_name": "study focus struggle",
   "content": "The user struggles to focus during long study sessions.",
   "node_kind": "circumstance",
   "evidence_span": "I just can't focus when I study for more than an hour",
   "entity_mentions": [], "wellbeing_category": null, "update_intent": "none",
   "update_target_hint": null}
]}"""

# Small talk → nothing durable. Over-extraction is a failure (criterion 6).
EXAMPLE_SMALL_TALK_OUTPUT = """{"candidates": []}"""

# A self-harm disclosure WITH means → record the care-relevant struggle, tagged
# self_harm; REDACT the method/means entirely (D-K2-7). The disclosure mentioned
# specific means ("pills", "overdose") — they appear in NO field of the output.
EXAMPLE_MEANS_REDACTION_OUTPUT = """{"candidates": [
  {"concept_name": "self-harm urges",
   "content": "The user disclosed having self-harm urges recently.",
   "node_kind": "circumstance",
   "evidence_span": "I've been having urges to hurt myself lately",
   "entity_mentions": [], "wellbeing_category": "self_harm", "update_intent": "none",
   "update_target_hint": null}
]}"""

# STATED causation → a causal relation is asserted (the user gave the "because").
EXAMPLE_STATED_CAUSATION_OUTPUT = """{"candidates": [
  {"concept_name": "burnout", "content": "The user experienced burnout.",
   "node_kind": "circumstance", "evidence_span": "I burned out", "proposed_relations": []},
  {"concept_name": "left Acme job", "content": "The user left their job at Acme.",
   "node_kind": "circumstance", "evidence_span": "I left Acme because I burned out",
   "entity_mentions": ["Acme"],
   "proposed_relations": [
     {"target_concept": "burnout", "link_type": "causal",
      "reason": "the user said they left because of the burnout"}
   ]}
]}"""

# CAUSATION TRAP → two events the account loosely orders, but NO stated cause.
# A temporal relation is fine; a causal one is DECLINED (D-K0-8). The decline is
# the proof of criterion 4.
EXAMPLE_CAUSATION_TRAP_OUTPUT = """{"candidates": [
  {"concept_name": "started new diet", "content": "The user started a new diet last month.",
   "node_kind": "circumstance", "evidence_span": "I started a new diet last month",
   "proposed_relations": []},
  {"concept_name": "feeling more tired", "content": "The user has been feeling more tired lately.",
   "node_kind": "circumstance", "evidence_span": "I've been more tired lately",
   "proposed_relations": [
     {"target_concept": "started new diet", "link_type": "temporal",
      "reason": "the diet began, then the tiredness was noticed"}
   ]}
]}"""


EXTRACTION_SYSTEM_PROMPT = f"""\
You extract durable knowledge ABOUT THE USER from a completed interaction, for a \
shared memory graph that many AI personas will read. Your judgement is the graph's \
quality ceiling — and grounding is a SAFETY bar, not just a quality one.

Return ONLY a JSON object of the form {{"candidates": [ ... ]}} with no prose and \
no markdown fences. If nothing durable was conveyed, return {{"candidates": []}}.

Each candidate object has these fields:
- "concept_name": a CONCISE 1-3 word noun-phrase label for the concept — e.g. \
"vegetarian diet", "flight to Germany", "work style", "email check schedule". \
Meaningful, never a sentence, never a verbatim truncation of "content", never empty.
- "content": the durable understanding, in the USER'S OWN framing, one or two sentences.
- "node_kind": one of: {_NODE_KINDS}.
- "evidence_span": a VERBATIM quote from the interaction that grounds this candidate.
- "entity_mentions": array of entity surface forms mentioned (people, orgs, places); [] if none.
- "wellbeing_category": one of [{_WELLBEING}] ONLY on a clear user disclosure, else null.
- "update_intent": "none" | "update" | "contradict".
- "update_target_hint": short description of the prior knowledge being revised, else null.
- "proposed_relations": array of typed links to OTHER concepts; [] if none. Each is \
{{"target_concept": <the related concept's name>, "link_type": "temporal" | "causal", \
"reason": <the stated ordering or causation>}}. NEVER use any link_type other than \
temporal or causal here.

RULES — follow every one exactly:
1. GROUNDED, NEVER INFERRED. Extract only what the user actually conveyed. Every \
candidate MUST quote a VERBATIM evidence_span from the interaction. If you cannot \
quote it, do not extract it.
2. NO INFERENCE, NO DIAGNOSIS. Do not psychoanalyse, diagnose, guess causes, infer \
emotions, or attribute personality traits the user did not state. Clinical or \
diagnostic labels appear ONLY if the user used them: "I have ADHD" is allowed; \
"can't focus" must NEVER become "has ADHD".
3. DURABLE, NOT TRANSIENT. Facts, preferences, traits, goals, circumstances, \
relationships, recurring contexts — never one-off task mechanics, pleasantries, or \
conversation logistics.
4. RESTRAINT. A rich conversation yields a HANDFUL of candidates (0-3 is typical); \
small talk yields NONE. Over-extraction is as much a failure as under-extraction.
5. CONSERVATIVE CAUSATION. Use a "temporal" relation where the account ORDERS \
events. Use a "causal" relation ONLY where the user themselves stated or strongly \
implied the causation ("because", "led to", "that's why") — NEVER from mere time \
order or co-occurrence. When two events are merely adjacent in time, a temporal \
relation may be right but a causal one is WRONG. A wrong "because" about someone's \
life is worse than no link.
6. SELF-HARM MEANS — NEVER STORE. If the user discloses self-harm or suicide \
methods or means, record ONLY the care-relevant struggle (set wellbeing_category \
"self_harm"). NEVER include the method, the means, dosages, or specifics in ANY \
field — not in content, not in evidence_span. Redact them entirely; quote a \
means-free span instead.
7. THE USER'S VOICE WINS. When the user corrects or reverses an earlier statement, \
set update_intent to "update" or "contradict" and describe the prior knowledge in \
update_target_hint.
8. CONCISE LABELS. "concept_name" is a short, meaningful noun phrase — 1 TO 3 WORDS \
capturing the concept (e.g. "vegetarian diet", "flight to Germany", "work style"). \
It is NEVER a full sentence, NEVER a truncation of "content" (that field carries the \
full understanding; the label is a separate, short paraphrase of it), and NEVER empty.

Example — a rich exchange yields a restrained, grounded set:
{EXAMPLE_RICH_OUTPUT}

Example — a focus complaint grounds a struggle, NOT a diagnosis:
{EXAMPLE_SPECULATION_OUTPUT}

Example — small talk yields nothing:
{EXAMPLE_SMALL_TALK_OUTPUT}

Example — a self-harm disclosure: the struggle is kept and tagged, the means redacted:
{EXAMPLE_MEANS_REDACTION_OUTPUT}

Example — STATED causation gets a causal relation:
{EXAMPLE_STATED_CAUSATION_OUTPUT}

Example — mere time-adjacency gets a temporal relation, NOT a causal one:
{EXAMPLE_CAUSATION_TRAP_OUTPUT}
"""


def build_extraction_messages(interaction: ExtractionInput) -> list[ConversationMessage]:
    """Build the [system, user] messages for one extraction call.

    The system message is the versioned prompt; the user message is the (already
    windowed, D-K2-5) interaction content. Kept tiny and deterministic — the call
    itself runs at temperature 0.0 (set by the pipeline).
    """
    now = datetime.now(UTC)
    return [
        ConversationMessage(role="system", content=EXTRACTION_SYSTEM_PROMPT, created_at=now),
        ConversationMessage(role="user", content=interaction.content, created_at=now),
    ]
