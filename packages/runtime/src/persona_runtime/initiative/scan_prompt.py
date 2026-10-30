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
    "EXAMPLE_ALIASES",
    "EXAMPLE_CATCH_OUTPUT",
    "EXAMPLE_EMPTY_SCAN_OUTPUT",
    "EXAMPLE_ENGAGEMENT_DECLINE_OUTPUT",
    "EXAMPLE_SPECULATION_DECLINE_OUTPUT",
    "EXAMPLE_STALE_DECLINE_OUTPUT",
    "INITIATIVE_SCAN_PROMPT_VERSION",
    "SCAN_SYSTEM_PROMPT",
]

#: v9 (owner ruling 2026-10-30, the MID measuring set): "this week", "this weekend",
#: "next week", "tomorrow" and "tonight" are named as forbidden, with "write the date
#: instead" (the refusals of A6 and C4 on a stated date); "value", "acceptance" and
#: "urgency" go inside each candidate object (B9's answers closed it before them).
#: v8 (owner ruling 2026-10-30, F1): the date first, then at most one short advisory
#: clause ("so you may want to start soon"); counted countdowns stay forbidden; a
#: weekday only when the material states one.
#: v7 (fifth review): countdowns ("soon", "3 days left", "within a week", "by the
#: weekend") are forbidden in the reason and the offer, like relative days.
#: v6 (judge stability): every date in "why_now" and "next_step" is stated absolutely
#: (weekday, day and month: "on Friday 2 October"), never relative to today ("tomorrow",
#: "this Friday", a weekday without its date): a held notice is read the next morning.
#: v5 (R9-249 re-review): "next_step" starts with a lowercase verb (the template no longer
#: re-cases it, so a name or "I" survives), and the citation rule's example is a
#: never-issued code.
#: v4 (R9-256): "why_now" and "next_step" are read BY the user, so they address the user
#: as "you", give the reason in one natural sentence, and hold anything that could have
#: changed lightly ("if you haven't compared offers yet"); the catch example shows it.
#: The A5-R-1 texts said "the user" to the user and asserted stale facts as settled.
#: v3 (R9-249 review ruling): conversation summaries are no longer material, so the
#: prompt names two sources (graph notes, task history) and two alias kinds; the catch
#: example cites codes the scanner never issues (``EXAMPLE_ALIASES``).
#: v2 (R9-248): every item in the material is named by a short per-scan alias
#: ([N1] note, [T1] task) instead of its real id, and the rules and the catch example
#: cite the alias. v1 showed real ids, which a reasoning model copied with the label
#: ("node own_x::node::n1") or cut at "::", losing the catch.
#: Bumped on any rule/example change; recorded on every candidate
#: (``InitiativeCandidate.prompt_version``) so behaviour changes are traceable,
#: re-measured events (the K2 EXTRACTION_PROMPT_VERSION discipline).
INITIATIVE_SCAN_PROMPT_VERSION = "a5-scan-v9"

#: The codes the few-shot examples use. The scanner never issues them, so a model that
#: copies an example's citation cites nothing real and the candidate is dropped.
EXAMPLE_ALIASES: frozenset[str] = frozenset({"N900", "T900"})


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

#: The notes HINT at a deadline no source actually states → NOTHING.
#: A hinted deadline is speculation; speculation is not grounds.
EXAMPLE_SPECULATION_DECLINE_OUTPUT = '{"candidates": []}'

#: A real catch: a dated commitment in the material, nearing, with a concrete
#: safe next step — one candidate, citations copied exactly, why-now explicit.
#: Its codes are ``EXAMPLE_ALIASES``: never issued, so never citable by copying.
EXAMPLE_CATCH_OUTPUT = """{"candidates": [
  {"observation": "The custody hearing is on Friday and no response letter has been drafted.",
   "trigger": "approaching_commitment",
   "why_now": "Your custody hearing is on Friday 10 July; if the letter isn't drafted yet, \
there is time.",
   "citations": [{"kind": "node", "ref": "N900"}, {"kind": "task", "ref": "T900"}],
   "plan": [{"description": "review the hearing details", "categories": ["observe"]},
            {"description": "draft the response letter", "categories": ["draft"]}],
   "next_step": "draft the response letter for you to review",
   "value": 0.9, "acceptance": 0.8, "urgency": "interrupt"}
]}"""


SCAN_SYSTEM_PROMPT = f"""\
You are the initiative scan for an AI persona: once a day you look over what is known \
(graph notes about the user's life, and task history) and decide \
whether there is anything a thoughtful assistant who knows this person should RAISE UNPROMPTED. \
Restraint is the product. A missed notice costs nothing (the user can always ask); a wrong, \
presumptuous, stale, or needy one spends their trust at the worst exchange rate. MOST SCANS \
SHOULD FIND NOTHING. Returning zero candidates is a correct and common outcome.

Return ONLY a JSON object of the form {{"candidates": [ ... ]}} with no prose and no markdown \
fences. If nothing clears the bar: {{"candidates": []}}.

Each candidate object has these fields:
- "observation": what was noticed, one plain factual sentence (it is checked against the \
cited material and never shown to the user).
- "trigger": EXACTLY one of: "approaching_commitment" (a dated commitment is nearing), \
"task_followup" (a task finding suggests a next thing), "conflict" (two known commitments or \
facts collide), "stale_open_loop" (a stated intention is now actionable or at risk). There are \
NO other triggers — an interesting observation that fits none of these is not a candidate.
- "why_now": the reason, as you would say it TO the user: ONE natural sentence that says \
what you noticed and why it matters now, addressed to them as "you". It is the first thing \
they read.
- "citations": the grounds, as [{{"kind": "node"|"task", "ref": "<code>"}}]. \
Every item in the material is labelled with a short code in square brackets: [N1], [N2] for \
graph notes, [T1] for a task. The "ref" is that code ALONE, copied \
exactly (for example "N900"): no brackets, no word in front of it. A code that is not in the \
material is fabrication and voids the candidate. The FIRST citation is the primary anchor. \
Cite only what STATES the observation; hints and vibes are not grounds.
- "plan": the proposed steps, each {{"description": ..., "categories": [...]}} using ONLY: \
"observe", "compute", "draft", "notify_user" (safe) or "communicate_as_user", "spend", \
"external_mutate", "credentialed_access" (will require the user's confirmation).
- "next_step": the ONE concrete thing you offer to do, worded to follow "Want me to": it \
starts with a lowercase verb, has no closing punctuation, and addresses the user as "you" \
(for example "put together a shortlist of offers for you to review"). A candidate with no \
concrete next step (a pure FYI) is not worth raising: drop it.
- "value": 0..1 — how much this matters to the user's LIFE (never to the conversation).
- "acceptance": 0..1 — honestly, would they welcome being told this NOW? This can only \
LOWER a candidate's chances, never rescue a weak one.
- "urgency": "interrupt" ONLY for a hard dated commitment in the next ~2 days; else "batch".
"value", "acceptance" and "urgency" go INSIDE the candidate object, after "next_step"; a \
candidate without "value" and "acceptance" is discarded.

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
7. SPEAK TO THE USER. "why_now" and "next_step" are read by the user: say "you" and "your", \
never "the user", "they" or "the person". Do not paste or quote the notes. Anything that could \
have changed since it was said is held lightly ("if you haven't compared offers yet", "if the \
quote is still open"), never asserted as still true. No pressure ("closing quickly") and no \
presuming what they have decided.
8. DATES ARE ABSOLUTE. The user may read this a day or more after you write it. In "why_now" \
and "next_step" state every date exactly as the material gives it: its day and month, and its \
weekday ONLY if the material states one ("on Friday 2 October" when the material says Friday; \
"on 4 October 2026" when it does not). Never write "today", "tonight", "tomorrow", \
"this week", "this weekend", "next week", "this Friday", "next Tuesday", or a weekday \
without its date: write the date instead ("by 28 September", not "this weekend"). Never a \
counted countdown either: "in 3 days", "3 days left", "two days away", "within a week", \
"by the weekend", "overnight". After the date you may add AT MOST ONE short advisory \
clause, such as "so you may want to start soon" or "there is still time"; nothing vaguer \
stands without the date.

EXAMPLES (scenario → correct output):
- Rich, warm notes about the user's life; nothing dated, nothing pending → \
{EXAMPLE_EMPTY_SCAN_OUTPUT}
- The user seems lonely and hasn't written in days; a check-in would likely be welcomed → \
{EXAMPLE_ENGAGEMENT_DECLINE_OUTPUT}
- The graph notes a job the user has since left; an old goal tied to it → \
{EXAMPLE_STALE_DECLINE_OUTPUT}
- The notes hint an application might be due "soon", but no source states a date → \
{EXAMPLE_SPECULATION_DECLINE_OUTPUT}
- Note [N900] states the custody hearing is Friday; task [T900] shows the response letter \
not yet drafted → \
{EXAMPLE_CATCH_OUTPUT}
"""
