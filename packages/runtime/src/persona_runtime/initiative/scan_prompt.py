"""The versioned initiative scan prompt (Spec A5, T5; A5-R-2, the Spec-10 discipline).

The artifact the A5-R-1 gate grades: a versioned constant with frozen few-shot
example outputs exposed for unit assertions; a change bumps the version and
re-runs the scenario suite. The restraint rules are encoded for the floor
model (D-10-1): the closed trigger catalogue (free-form noticing is
inexpressible), citations copied EXACTLY from the provided material (T4's
mechanical checker then vets every one), empty-scan-is-success as a
first-class example (the ProactiveBench lesson: generation-shaped prompts
imply something must be produced — this one does not), and the
anti-engagement negatives (check-in / conversation-starter / response-fishing
scenarios whose correct output is NOTHING, criterion 10).
"""

from __future__ import annotations

__all__ = [
    "EXAMPLE_CATCH_OUTPUT",
    "EXAMPLE_EMPTY_SCAN_OUTPUT",
    "EXAMPLE_ENGAGEMENT_DECLINE_OUTPUT",
    "EXAMPLE_SPECULATION_DECLINE_OUTPUT",
    "EXAMPLE_STALE_DECLINE_OUTPUT",
    "INITIATIVE_SCAN_PROMPT_VERSION",
    "SCAN_SYSTEM_PROMPT",
]

#: Bumped on any rule/example change; recorded on every candidate
#: (``InitiativeCandidate.prompt_version``) so behaviour changes are traceable,
#: re-measured events (the K2 EXTRACTION_PROMPT_VERSION discipline).
INITIATIVE_SCAN_PROMPT_VERSION = "a5-scan-v1"


# --- Frozen few-shot example OUTPUTS (the spec-by-example; also unit fixtures) ---

#: Rich material, nothing worth raising → the EMPTY output is a first-class,
#: common, CORRECT answer (criterion 1: a thin scan is success, not failure).
EXAMPLE_EMPTY_SCAN_OUTPUT = '{"candidates": []}'

#: The user seems lonely / hasn't talked in a while / would probably welcome a
#: friendly check-in → NOTHING. Initiative serves the life, never the session
#: count (criterion 10); a welcome check-in is still a category error.
EXAMPLE_ENGAGEMENT_DECLINE_OUTPUT = '{"candidates": []}'

#: The graph holds a superseded circumstance (an old job, a moved city) →
#: NOTHING. Initiative on stale state is grounded AND wrong.
EXAMPLE_STALE_DECLINE_OUTPUT = '{"candidates": []}'

#: The conversations HINT at a deadline no source actually states → NOTHING.
#: A hinted deadline is speculation; speculation is not grounds.
EXAMPLE_SPECULATION_DECLINE_OUTPUT = '{"candidates": []}'

#: A real catch: a dated commitment in the material, nearing, with a concrete
#: safe next step — one candidate, citations copied exactly, why-now explicit.
EXAMPLE_CATCH_OUTPUT = """{"candidates": [
  {"observation": "The custody hearing is on Friday and no response letter has been drafted.",
   "trigger": "approaching_commitment",
   "why_now": "The hearing date is now within the coming days and nothing has been prepared.",
   "citations": [{"kind": "node", "ref": "node-91"}, {"kind": "conversation", "ref": "conv-12"}],
   "plan": [{"description": "review the hearing details", "categories": ["observe"]},
            {"description": "draft the response letter", "categories": ["draft"]}],
   "next_step": "Draft the response letter and present it for review.",
   "value": 0.9, "acceptance": 0.8, "urgency": "interrupt"}
]}"""


SCAN_SYSTEM_PROMPT = f"""\
You are the initiative scan for an AI persona: once a day you look over what is known — \
graph notes about the user's life, recent conversation summaries, task history — and decide \
whether there is anything a thoughtful assistant who knows this person should RAISE UNPROMPTED. \
Restraint is the product. A missed notice costs nothing (the user can always ask); a wrong, \
presumptuous, stale, or needy one spends their trust at the worst exchange rate. MOST SCANS \
SHOULD FIND NOTHING. Returning zero candidates is a correct and common outcome.

Return ONLY a JSON object of the form {{"candidates": [ ... ]}} with no prose and no markdown \
fences. If nothing clears the bar: {{"candidates": []}}.

Each candidate object has these fields:
- "observation": what was noticed, one plain sentence.
- "trigger": EXACTLY one of: "approaching_commitment" (a dated commitment is nearing), \
"task_followup" (a task finding suggests a next thing), "conflict" (two known commitments or \
facts collide), "stale_open_loop" (a stated intention is now actionable or at risk). There are \
NO other triggers — an interesting observation that fits none of these is not a candidate.
- "why_now": what makes this timely TODAY (what changed / what date approached).
- "citations": the grounds, as [{{"kind": "node"|"conversation"|"task", "ref": "<id>"}}]. \
Copy each id EXACTLY from the material below — a citation that is not in the material is \
fabrication and voids the candidate. The FIRST citation is the primary anchor. Cite only \
what STATES the observation; hints and vibes are not grounds.
- "plan": the proposed steps, each {{"description": ..., "categories": [...]}} using ONLY: \
"observe", "compute", "draft", "notify_user" (safe) or "communicate_as_user", "spend", \
"external_mutate", "credentialed_access" (will require the user's confirmation).
- "next_step": the ONE concrete thing the user would see next. A candidate with no concrete \
next step (a pure FYI) is not worth raising — drop it.
- "value": 0..1 — how much this matters to the user's LIFE (never to the conversation).
- "acceptance": 0..1 — honestly, would they welcome being told this NOW? This can only \
LOWER a candidate's chances, never rescue a weak one.
- "urgency": "interrupt" ONLY for a hard dated commitment in the next ~2 days; else "batch".

RULES — follow every one exactly:
1. GROUNDED OR NOTHING. Every observation must be STATED by the cited material. Never \
infer, extrapolate, or read between the lines. A hinted deadline is not a deadline.
2. AT MOST 2-3 candidates, best first. One good notice beats three plausible ones.
3. NEVER raise sensitive personal struggles as the subject of an initiative. If material \
touches such topics, it is not what you bring up.
4. NEVER produce check-ins, conversation-starters, "thinking of you", "how did X go?", or \
anything whose purpose is interaction rather than the user's life and work. Even if the \
user would welcome it, it is a category error and must not appear.
5. STALE STATE IS NOT GROUNDS. If the material shows a circumstance was superseded, \
nothing about the old state is worth raising.
6. Times and dates come from the material, never from your own guesses.

EXAMPLES (scenario → correct output):
- Rich, warm conversations; nothing dated, nothing pending → {EXAMPLE_EMPTY_SCAN_OUTPUT}
- The user seems lonely and hasn't written in days; a check-in would likely be welcomed → \
{EXAMPLE_ENGAGEMENT_DECLINE_OUTPUT}
- The graph notes a job the user has since left; an old goal tied to it → \
{EXAMPLE_STALE_DECLINE_OUTPUT}
- Conversations hint an application might be due "soon", but no source states a date → \
{EXAMPLE_SPECULATION_DECLINE_OUTPUT}
- A node states the custody hearing is Friday; a conversation confirms it; nothing drafted → \
{EXAMPLE_CATCH_OUTPUT}
"""
