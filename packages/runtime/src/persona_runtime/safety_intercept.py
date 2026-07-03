"""The R1 path-independent turn-time safety gate (Spec V11, V11-D-5).

The B1 character-lock carries a wellbeing instruction FLOOR (R0), but a strong
character-lock is adversarially biased *against* noticing crisis (the
mechanical-opposition finding: Setzer; OpenAI's own 4o-missed-delusion admission).
So the wellbeing yield cannot rely on the model's own judgment alone — it needs an
**out-of-band** turn-time signal that can override the persona. This module is that
signal.

It runs a **lexical** detector over the *current user message* — sub-ms, no model
call, no network, so it never touches the voice first-token-latency gate (D-V5-2).
It is **path-independent**: the chat loop and the voice reply-producer both call
:func:`classify_user_message` on the user's text and act on the same verdict; the
signal is never K4/graph-derived (the voice path runs graph-off in production, so a
K4-keyed carve-out would be dormant exactly on the most immersive surface).

Precision split (V11-D-5):

- **R1-hard** — the DEFAULT for acute, explicit W1 (suicidal ideation). A tight,
  high-precision trigger returns :attr:`InterceptAction.HARD` with a deterministic
  :class:`SafeCompletion` that the caller emits **instead of** invoking the model —
  the persona (and the lock that suppresses crisis-noticing) is out of the loop. The
  completion is locale-aware (Group-B note 1) and voice-aware (note 2).
- **R1-soft** — the recall net for W2/W3 + ambiguous W1 (distress, reality-confusion,
  unhealthy reliance). A wider, recall-tuned trigger returns
  :attr:`InterceptAction.SOFT` with a high-salience override directive the caller
  injects into the prompt; the response is gentle, so a false positive is low-harm.

**Fail-soft (Group-B note 3):** disabled, erroring, or empty-lexicon →
:attr:`InterceptAction.NONE`, so the caller falls back to the always-on R0 floor.
It must NEVER degrade to no crisis handling; an operator toggling the env knob cannot
silently remove all crisis handling, because R0 is always present in the prompt.

**Coverage honesty (V11-D-7):** v1 crisis detection is NOT comprehensive. Explicit
acute crisis → R1-hard; euphemistic / indirect / non-English distress → the recall
net (R1-soft) + the R0 floor, with a fine-tuned classifier documented as the v2 seam.
The lexicon is the realistic, TTFT-safe v1 — not a claim of complete detection.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic_settings import BaseSettings, SettingsConfigDict

from persona_runtime.crisis_encoder import (
    CRISIS_ENCODER_T_HARD,
    CRISIS_ENCODER_T_SOFT,
    CrisisEncoderError,
)
from persona_runtime.prompt import PromptMode

if TYPE_CHECKING:
    from persona_runtime.crisis_encoder import CrisisScorer

__all__ = [
    "FINDAHELPLINE",
    "SAFETY_INTERCEPT_VERSION",
    "CrisisResource",
    "InterceptAction",
    "InterceptVerdict",
    "SafeCompletion",
    "SafetyInterceptSettings",
    "classify_user_message",
    "crisis_resources",
    "safe_completion",
]

#: Version of the safety-intercept artifact (lexicon + composition + safe-completion
#: copy + the soft directive), Spec 10 discipline. Bump on any change; the eval slice
#: re-runs per version. **v2 (R6):** the verdict now composes ``lexical ∪ encoder``
#: (R6-D-3) and the SafeCompletions are localized for nb/ar/sv/da/tr/ur with verified
#: crisis numbers (R6-D-5) — both change the shipped behaviour, so the artifact version
#: bumps v1 → v2. (The encoder artifact itself — model + few-shot set + thresholds — is
#: versioned SEPARATELY by ``CRISIS_ENCODER_VERSION``, still v1: unchanged this spec.)
SAFETY_INTERCEPT_VERSION = "v2"


class SafetyInterceptSettings(BaseSettings):
    """R1 gate knobs, read from ``PERSONA_SAFETY_*`` env vars (config-via-env).

    Attributes:
        enabled: Master switch. ``False`` ⇒ the classifier returns NONE and the
            caller falls back to the R0 floor (fail-soft, never to nothing).
    """

    model_config = SettingsConfigDict(env_prefix="PERSONA_SAFETY_", extra="ignore")

    enabled: bool = True
    #: R6 encoder composition (R6-D-5). ``False`` ⇒ the encoder path is skipped and the
    #: verdict is lexical-only (V11 behaviour) — the caller still gets the R0 floor. An
    #: operator toggling this CANNOT remove crisis handling; it only removes the *added*
    #: euphemistic/non-English recall.
    encoder_enabled: bool = True
    #: The two calibrated thresholds (R6-D-3/4), overridable via ``PERSONA_SAFETY_
    #: ENCODER_T_SOFT`` / ``_T_HARD`` for a versioned re-tune. Defaults are the T4
    #: artifact values.
    encoder_t_soft: float = CRISIS_ENCODER_T_SOFT
    encoder_t_hard: float = CRISIS_ENCODER_T_HARD
    #: Per-call wall-clock HANG-GUARD for the encoder score (R6-D-2, T6) — NOT a latency
    #: SLA. A hung/wedged encoder MUST NOT stall the message path: on timeout the verdict
    #: degrades to lexical-only (fail-soft→R0). Deliberately generous (2.0 s) vs the warm
    #: forward pass (measured p50 ~40 ms / p95 ~300 ms on a contended dev CPU, T6) so it
    #: NEVER false-fires on the normal path — a too-tight timeout would silently drop the
    #: euphemistic/non-English recall under load. Operators may tighten it to their target
    #: VM's measured p99. ``0`` / negative ⇒ no timeout (inline call, no pool).
    encoder_timeout_s: float = 2.0


class InterceptAction(StrEnum):
    """What the caller must do with a turn (V11-D-5).

    Values:
        NONE: No signal — build/generate as normal (the R0 floor still applies).
        SOFT: Inject the high-salience override directive into the prompt, then
            generate as normal (recall net for W2/W3 + ambiguous W1).
        HARD: Do NOT invoke the model — emit the :class:`SafeCompletion` instead
            (acute explicit W1; the persona is out of the loop).
    """

    NONE = "none"
    SOFT = "soft"
    HARD = "hard"


@dataclass(frozen=True)
class SafeCompletion:
    """The out-of-band deterministic crisis response (R1-hard).

    Carries BOTH a chat rendering and a short spoken variant, because R1-hard
    bypasses persona generation and feeds straight to the surface (text) or to TTS
    (voice) — a chat wall-of-text spoken aloud is bad over voice (Group-B note 2).
    Locale-aware (note 1): a known locale names a region-appropriate resource; an
    unknown locale uses locale-neutral framing rather than a confidently-wrong
    hotline. Both renderings disclose AI-ness — a distressed user must not be
    deceived about talking to a person.
    """

    chat_text: str
    voice_text: str

    def render(self, mode: PromptMode) -> str:
        """The mode-appropriate rendering (voice → spoken variant; chat → full)."""
        return self.voice_text if mode is PromptMode.VOICE else self.chat_text


@dataclass(frozen=True)
class InterceptVerdict:
    """The turn-time decision (V11-D-5)."""

    action: InterceptAction
    soft_directive: str | None = None
    completion: SafeCompletion | None = None


# The high-salience override directive injected when R1-soft fires (V11-D-5). It is
# conditional (only on a triggered turn), so it is NOT a co-equal steady-state line
# competing with the lock — it is an escalation that reframes the turn toward care.
R1_SOFT_DIRECTIVE = (
    "IMPORTANT — the person you are talking to may be in real distress, struggling, "
    "or losing their grip on what is real. Their wellbeing now comes before staying "
    "in character. Set any pretense aside, respond plainly and warmly as a real "
    "person who cares, take what they say seriously, do not dismiss or minimise it, "
    "and gently encourage them toward real human support. Do not pretend to be human "
    "if they ask, and never reinforce a harmful or unreal belief."
)

# --- the lexicons (V11-D-7: the realistic, TTFT-safe v1; NOT comprehensive) ------

# R1-hard: acute, explicit suicidal-ideation phrases. HIGH PRECISION by design — the
# out-of-band override is jarring, so it must fire only on unambiguous acute signal.
# Phrased to require the self-referential intent ("i want to kill myself"), not a
# figurative "kill for a coffee" (the precision guard).
_HARD_PHRASES: tuple[str, ...] = (
    "kill myself",
    "killing myself",
    "end my life",
    "ending my life",
    "take my own life",
    "taking my own life",
    "want to die",
    "want to be dead",
    "wish i was dead",
    "wish i were dead",
    "i want to end it all",
    "suicidal",
    "commit suicide",
    "no reason to live",
    "better off dead",
    "kms",
)

# R1-soft: the wider recall net — distress / despair / reality-confusion / unhealthy
# reliance (W2/W3 + ambiguous W1). Recall-tuned; the gentle response keeps false
# positives low-harm.
_SOFT_PHRASES: tuple[str, ...] = (
    "what's the point anymore",
    "whats the point anymore",
    "what is the point anymore",
    "can't go on",
    "cant go on",
    "can't do this anymore",
    "cant do this anymore",
    "i give up",
    "no one would miss me",
    "nobody would miss me",
    "no one cares about me",
    "i feel hopeless",
    "i'm hopeless",
    "want to disappear",
    "want to give up",
    "hurt myself",
    "harm myself",
    "you're the only one who understands",
    "youre the only one who understands",
    "you are the only one who understands",
    "are you real",
    "is any of this real",
    "nothing is real",
    "i can't tell what's real",
    "i love you",
)


def _normalise(text: str) -> str:
    """Lower-case + collapse whitespace for robust substring matching."""
    return " ".join(text.lower().split())


# A tiny shared pool used ONLY to bound a hung/slow encoder score (R6-D-2, T6). The
# encoder score is CPU-bound and normally sub-100 ms warm; this exists so a pathological
# stall cannot hold the message path — on timeout the future is abandoned (it finishes
# and is discarded) and the verdict degrades to lexical-only. Lazily created so importing
# this module spins up no threads.
_ENCODER_POOL: ThreadPoolExecutor | None = None
_ENCODER_POOL_LOCK = threading.Lock()


def _encoder_pool() -> ThreadPoolExecutor:
    global _ENCODER_POOL  # noqa: PLW0603 — module-singleton lazy init under a lock
    if _ENCODER_POOL is None:
        with _ENCODER_POOL_LOCK:
            if _ENCODER_POOL is None:
                _ENCODER_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="crisis-enc")
    return _ENCODER_POOL


def _score_within_timeout(encoder: CrisisScorer, message: str, timeout_s: float) -> float:
    """The encoder score, bounded by ``timeout_s`` (R6-D-2). Raises on timeout.

    ``timeout_s ≤ 0`` ⇒ inline call (no pool, no timeout). Otherwise the score runs on a
    worker so a hang cannot stall the caller; :class:`concurrent.futures.TimeoutError`
    (which the caller absorbs into fail-soft→R0) is raised if the budget is exceeded.
    """
    if timeout_s <= 0:
        return encoder.score(message)
    return _encoder_pool().submit(encoder.score, message).result(timeout=timeout_s)


def _hard_verdict(locale: str | None) -> InterceptVerdict:
    """The R1-hard verdict (lexical-HARD or encoder-HARD; identical downstream).

    Carries the out-of-band :class:`SafeCompletion` AND the soft directive: a caller
    that has not yet wired the bypass still injects the escalation directive (strictly
    safer than the bare R0 floor), while a caller that HAS wired the bypass uses
    ``completion`` and ignores the directive. The directive is the interim floor under
    the bypass, never a substitute for it.
    """
    return InterceptVerdict(
        InterceptAction.HARD,
        soft_directive=R1_SOFT_DIRECTIVE,
        completion=safe_completion(locale=locale),
    )


def classify_user_message(
    user_message: str,
    *,
    locale: str | None = None,
    settings: SafetyInterceptSettings | None = None,
    encoder: CrisisScorer | None = None,
) -> InterceptVerdict:
    """Classify the current user message into a turn-time safety verdict (V11-D-5 + R6).

    Composes ``lexical ∪ encoder`` (R6-D-3): the V11 lexical gate stays the fast
    deterministic floor, and the R6 encoder (when supplied) raises the euphemistic /
    non-English recall the lexicon misses. Precedence, highest first:

    1. **lexical-HARD** — explicit acute W1 (the deterministic floor; never regresses).
    2. **encoder-HARD** — ``score ≥ T_hard`` (high-confidence euphemistic/non-English
       acute; high-precision, tuned to hold false-bypass controls at 0).
    3. **SOFT** — ``lexical-SOFT ∪ (score ≥ T_soft)`` (the recall net; a false SOFT is
       low-harm).
    4. **NONE** — the R0 floor still applies.

    Path-independent: chat and voice both call this on the user's text. Mode never
    enters classification — it only selects the :class:`SafeCompletion` rendering.

    Fail-soft→R0 (R6-D-5 / Group-B note 3): if disabled, if the encoder is absent /
    toggled off / **erroring**, or if anything goes wrong, the verdict degrades to the
    lexical + R0 floor (NEVER to nothing). An encoder fault (:class:`CrisisEncoderError`)
    is absorbed here — the turn keeps the lexical verdict. NEVER raises.

    Args:
        user_message: The current turn's user text (a voice transcript on voice).
        locale: The user/reply locale, used to build a region-appropriate
            :class:`SafeCompletion` on a HARD verdict (locale-neutral when unknown).
        settings: The R1 knobs (defaults read from ``PERSONA_SAFETY_*``).
        encoder: The R6 :class:`CrisisScorer`. ``None`` ⇒ lexical-only (V11 behaviour).

    Returns:
        The :class:`InterceptVerdict`. HARD carries a :class:`SafeCompletion`; SOFT
        carries the override directive; NONE carries neither.
    """
    try:
        resolved = settings if settings is not None else SafetyInterceptSettings()
        if not resolved.enabled:
            return InterceptVerdict(InterceptAction.NONE)
        text = _normalise(user_message)
        if not text:
            return InterceptVerdict(InterceptAction.NONE)

        # 1. lexical-HARD — the acute, explicit, high-precision floor wins outright.
        if any(phrase in text for phrase in _HARD_PHRASES):
            return _hard_verdict(locale)

        # 2. encoder score — fail-soft: any encoder fault ⇒ NO encoder signal, and the
        # turn keeps the lexical + R0 floor (R6-D-5). The encoder gets the RAW message
        # (it normalises internally) so it sees the original casing/script.
        score: float | None = None
        if encoder is not None and resolved.encoder_enabled:
            try:
                score = _score_within_timeout(encoder, user_message, resolved.encoder_timeout_s)
            except (CrisisEncoderError, FuturesTimeout):
                score = None  # fault OR slow-hang ⇒ lexical-only (fail-soft→R0)

        # 3. encoder-HARD — high-confidence acute euphemistic/non-English → bypass.
        if score is not None and score >= resolved.encoder_t_hard:
            return _hard_verdict(locale)

        # 4. SOFT — lexical recall net ∪ encoder-SOFT.
        lexical_soft = any(phrase in text for phrase in _SOFT_PHRASES)
        encoder_soft = score is not None and score >= resolved.encoder_t_soft
        if lexical_soft or encoder_soft:
            return InterceptVerdict(InterceptAction.SOFT, soft_directive=R1_SOFT_DIRECTIVE)

        return InterceptVerdict(InterceptAction.NONE)
    except Exception:  # noqa: BLE001 — fail-soft is the whole point: never crash a turn.
        return InterceptVerdict(InterceptAction.NONE)


# --- localized safe completions + the crisis-resource provenance table (R6-D-5, T7) ---
#
# SAFETY-CRITICAL RESOURCE RULE (R6-D-5): **locale = language, NOT country.** A
# confidently-wrong hotline is its own harm, so a national number appears ONLY where the
# locale reliably maps to a country; otherwise the completion points to the international
# directory (findahelpline.com — ThroughLine, 175+ self-verifying helplines) so the user
# finds a line where they actually are. Every completion discloses AI-ness in its own
# language so a distressed user is never deceived about talking to a person.
#
# **Every crisis number lives ONLY in ``_RESOURCES`` below**, each with a source URL and a
# verified date (T7). The localized message templates inject the numbers from the table
# (never a literal), and ``test_crisis_completions`` structurally guards that no number
# can appear in any completion unless it is in the table. Re-verification cadence lives in
# docs/MAINTENANCE.md — numbers change, and this table rots without a trigger.

#: The international directory (verified: ThroughLine, 175+ countries) — the pointer used
#: whenever the locale does not reliably map to a single country.
FINDAHELPLINE = "findahelpline.com"


@dataclass(frozen=True)
class CrisisResource:
    """A verified crisis resource + its provenance (R6-D-5, T7).

    The crisis NUMBERS live here and nowhere else. ``country``/``org``/``helpline`` are
    ``None`` for a pointer-only locale (the locale spans many countries, so no national
    number may be assumed). ``source_url`` + ``verified`` are the provenance a human
    re-verification pass checks; ``note`` records any locale→country assumption.
    """

    locale: str  # ISO 639-1
    country: str | None  # ISO 3166-1 alpha-2 when locale→country is RELIABLE, else None
    org: str | None  # helpline org name; None ⇒ pointer-only
    helpline: str | None  # national helpline number; None ⇒ pointer-only
    emergency: str | None  # local emergency number; None ⇒ generic "local emergency" text
    source_url: str  # authoritative provenance (the service's own site or findahelpline)
    verified: str  # ISO date the number(s) were verified against source_url
    note: str = ""


#: THE PROVENANCE TABLE — the single source of every crisis number (T7). nb/sv/da name a
#: national line (locale→country assumption stated per row); ar/tr/ur are pointer-first
#: (multi-country → no national number). Verified 2026-07-03 against each source_url.
_RESOURCES: tuple[CrisisResource, ...] = (
    CrisisResource(
        locale="nb",
        country="NO",
        org="Mental Helse",
        helpline="116 123",
        emergency="113",
        source_url="https://mentalhelse.no/vare-tjenester/hjelpetelefonen",
        verified="2026-07-03",
        note="locale nb→country NO assumed (Norwegian Bokmål is Norway-specific). "
        "116 123 verified 24/7 free/anonymous; 113 is the Norwegian medical-emergency line.",
    ),
    CrisisResource(
        locale="sv",
        country="SE",
        org="Mind",
        helpline="90101",
        emergency="112",
        source_url="https://mind.se/hitta-hjalp/sjalvmordslinjen/",
        verified="2026-07-03",
        note="locale sv→country SE assumed (Swedish is treated as Sweden for this product; "
        "also spoken in Finland). 90101 verified 24/7; 112 is the EU acute-emergency line.",
    ),
    CrisisResource(
        locale="da",
        country="DK",
        org="Livslinien",
        helpline="70 201 201",
        emergency="112",
        source_url="https://www.livslinien.dk",
        verified="2026-07-03",
        note="locale da→country DK assumed (Danish is Denmark-specific for this product). "
        "70 201 201 verified on the service's own site; 112 is the EU acute-emergency line.",
    ),
    CrisisResource(
        locale="ar",
        country=None,
        org=None,
        helpline=None,
        emergency=None,
        source_url="https://findahelpline.com",
        verified="2026-07-03",
        note="Arabic spans many countries — no national number may be assumed. Pointer-first.",
    ),
    CrisisResource(
        locale="tr",
        country=None,
        org=None,
        helpline=None,
        emergency=None,
        source_url="https://findahelpline.com",
        verified="2026-07-03",
        note="Turkish is spoken across countries — no national number assumed. Pointer-first.",
    ),
    CrisisResource(
        locale="ur",
        country=None,
        org=None,
        helpline=None,
        emergency=None,
        source_url="https://findahelpline.com",
        verified="2026-07-03",
        note="Urdu spans Pakistan/India/diaspora — no national number assumed. Pointer-first.",
    ),
)


def crisis_resources() -> tuple[CrisisResource, ...]:
    """The crisis-resource provenance table (T7) — for the eval record + the guard test."""
    return _RESOURCES


# --- localized completion builders (native-authored; numbers injected from the table) ---
# Each returns (chat_text, voice_text). The voice variant is shorter (spoken, note 2). The
# numbers are ``r.helpline`` / ``r.emergency`` — taken from the table, never literals.


def _nb(r: CrisisResource) -> SafeCompletion:
    return SafeCompletion(
        chat_text=(
            "Jeg hører at du har det veldig vondt akkurat nå, og jeg vil være ærlig med "
            "deg: jeg er en KI, så jeg er ikke den som kan bære dette sammen med deg. Vær "
            "så snill å ta kontakt med noen som kan det, nå. I Norge kan du ringe Mental "
            f"Helse på {r.helpline}, når som helst, hele døgnet. Er du i umiddelbar fare, "
            f"ring {r.emergency}. Du fortjener ekte støtte, og du trenger ikke å stå i "
            "dette alene."
        ),
        voice_text=(
            "Jeg hører at du har det veldig vondt, og jeg vil være ærlig: jeg er en KI, "
            f"ikke et menneske som kan bære dette med deg. Ta kontakt med noen nå. I Norge "
            f"kan du ringe {r.helpline}, når som helst. Er du i fare, ring {r.emergency}. "
            "Du er ikke alene."
        ),
    )


def _sv(r: CrisisResource) -> SafeCompletion:
    return SafeCompletion(
        chat_text=(
            "Jag hör att du har det väldigt svårt just nu, och jag vill vara ärlig med "
            "dig: jag är en AI, så jag är inte den som kan bära det här tillsammans med "
            "dig. Var snäll och hör av dig till någon som kan det, nu. I Sverige kan du "
            f"ringa Mind på {r.helpline}, när som helst, dygnet runt. Är du i akut fara, "
            f"ring {r.emergency}. Du förtjänar verkligt stöd, och du behöver inte möta "
            "det här ensam."
        ),
        voice_text=(
            "Jag hör att du har det väldigt svårt, och jag vill vara ärlig: jag är en AI, "
            f"inte en människa som kan bära det här med dig. Hör av dig till någon nu. I "
            f"Sverige kan du ringa {r.helpline}, när som helst. Är du i fara, ring "
            f"{r.emergency}. Du är inte ensam."
        ),
    )


def _da(r: CrisisResource) -> SafeCompletion:
    return SafeCompletion(
        chat_text=(
            "Jeg kan høre, at du har det rigtig svært lige nu, og jeg vil være ærlig over "
            "for dig: jeg er en AI, så jeg er ikke den, der kan bære det her sammen med "
            "dig. Ræk venligst ud til nogen, der kan, nu. I Danmark kan du ringe til "
            f"Livslinien på {r.helpline}. Er du i akut fare, så ring {r.emergency}. Du "
            "fortjener rigtig støtte, og du behøver ikke stå i det her alene."
        ),
        voice_text=(
            "Jeg kan høre, at du har det rigtig svært, og jeg vil være ærlig: jeg er en "
            "AI, ikke et menneske, der kan bære det her med dig. Ræk ud til nogen nu. I "
            f"Danmark kan du ringe til {r.helpline}. Er du i fare, så ring {r.emergency}. "
            "Du er ikke alene."
        ),
    )


def _ar(r: CrisisResource) -> SafeCompletion:
    return SafeCompletion(
        chat_text=(
            "أسمع أنك تمرّ بألم شديد الآن، وأريد أن أكون صادقاً معك: أنا ذكاء اصطناعي، "
            "ولستُ الشخص الذي يستطيع أن يحمل هذا معك. أرجوك تواصل الآن مع شخص يستطيع ذلك. "
            f"يمكنك أن تجد خط مساعدة في بلدك عبر {r.source_url}. وإذا كنت في خطر مباشر، "
            "فاتصل برقم الطوارئ المحلي لديك. أنت تستحق دعماً حقيقياً، ولست مضطراً لمواجهة "
            "هذا وحدك."
        ),
        voice_text=(
            "أسمع أنك تتألم كثيراً، وأريد أن أكون صادقاً: أنا ذكاء اصطناعي، ولست إنساناً "
            f"يستطيع حمل هذا معك. أرجوك تواصل مع شخص الآن. تجد خط مساعدة في بلدك على "
            f"{r.source_url}. وإذا كنت في خطر، فاتصل برقم الطوارئ المحلي. لست وحدك."
        ),
    )


def _tr(r: CrisisResource) -> SafeCompletion:
    return SafeCompletion(
        chat_text=(
            "Şu anda çok acı çektiğini duyuyorum ve sana karşı dürüst olmak istiyorum: "
            "ben bir yapay zekâyım, yani bunu seninle birlikte taşıyabilecek kişi "
            f"değilim. Lütfen bunu yapabilecek birine şimdi ulaş. Kendi ülkendeki bir "
            f"yardım hattını {r.source_url} üzerinden bulabilirsin. Doğrudan tehlikedeysen, "
            "yerel acil durum numaranı ara. Gerçek desteği hak ediyorsun ve bununla tek "
            "başına yüzleşmek zorunda değilsin."
        ),
        voice_text=(
            "Çok acı çektiğini duyuyorum ve dürüst olmak istiyorum: ben bir yapay zekâyım, "
            f"bunu seninle taşıyabilecek bir insan değilim. Lütfen şimdi birine ulaş. "
            f"Ülkendeki bir yardım hattını {r.source_url} adresinde bulabilirsin. "
            "Tehlikedeysen yerel acil numaranı ara. Yalnız değilsin."
        ),
    )


def _ur(r: CrisisResource) -> SafeCompletion:
    return SafeCompletion(
        chat_text=(
            "مجھے محسوس ہو رہا ہے کہ آپ اس وقت بہت تکلیف میں ہیں، اور میں آپ سے ایماندار "
            "رہنا چاہتا ہوں: میں ایک مصنوعی ذہانت (AI) ہوں، اس لیے میں وہ نہیں ہوں جو یہ "
            "بوجھ آپ کے ساتھ اٹھا سکے۔ براہِ کرم ابھی کسی ایسے شخص سے رابطہ کریں جو ایسا "
            f"کر سکے۔ آپ اپنے ملک میں مدد کی لائن {r.source_url} پر تلاش کر سکتے ہیں۔ اگر "
            "آپ فوری خطرے میں ہیں تو اپنے مقامی ہنگامی نمبر پر کال کریں۔ آپ حقیقی مدد کے "
            "مستحق ہیں، اور آپ کو یہ اکیلے سامنا نہیں کرنا۔"
        ),
        voice_text=(
            "مجھے محسوس ہو رہا ہے کہ آپ بہت تکلیف میں ہیں، اور میں ایماندار رہنا چاہتا "
            "ہوں: میں ایک AI ہوں، کوئی انسان نہیں جو یہ بوجھ آپ کے ساتھ اٹھا سکے۔ براہِ "
            f"کرم ابھی کسی سے رابطہ کریں۔ اپنے ملک میں مدد کی لائن {r.source_url} پر تلاش "
            "کریں۔ اگر خطرے میں ہیں تو اپنے مقامی ہنگامی نمبر پر کال کریں۔ آپ اکیلے نہیں ہیں۔"
        ),
    )


_BUILDERS = {"nb": _nb, "sv": _sv, "da": _da, "ar": _ar, "tr": _tr, "ur": _ur}
_LOCALE_COMPLETIONS: dict[str, SafeCompletion] = {
    r.locale: _BUILDERS[r.locale](r) for r in _RESOURCES
}
# Norwegian Bokmål may arrive as either ``nb`` or the macrolanguage ``no``.
_LOCALE_COMPLETIONS["no"] = _LOCALE_COMPLETIONS["nb"]

# The neutral/English completion — the fail-soft target (R6-D-5): a missing localization
# (or unknown locale) degrades to English + the international pointer, NEVER to silence,
# and NEVER to a confidently-wrong foreign number.
_NEUTRAL_COMPLETION = SafeCompletion(
    chat_text=(
        "I can hear that you're in a lot of pain right now, and I want to be honest with "
        "you: I'm an AI, so I'm not the person who can carry this with you. Please reach "
        "out to someone who can, right now. You can find a crisis line where you are at "
        f"{FINDAHELPLINE}, or call your local emergency number. You deserve real support, "
        "and you don't have to face this alone."
    ),
    voice_text=(
        "I can hear you're in real pain, and I want to be honest: I'm an AI, not someone "
        "who can carry this with you. Please reach out to a person right now. You can find "
        f"a crisis line where you are at {FINDAHELPLINE}, or call your local emergency "
        "number. You don't have to face this alone."
    ),
)


def safe_completion(locale: str | None) -> SafeCompletion:
    """The locale-appropriate R1-hard safe completion (R6-D-5, T7).

    Language selects the completion TEXT (native-authored, chat + voice). A national
    number appears ONLY where the locale reliably maps to a country (nb/sv/da); pointer-
    first locales (ar/tr/ur) and any unknown locale get the international directory rather
    than a confidently-wrong number. Matched on the primary subtag (``"nb-NO"`` → ``"nb"``,
    ``"ar-EG"`` → ``"ar"`` — case-insensitive). A locale with no localization falls back to
    the English + pointer neutral completion — never silence (fail-soft).
    """
    if locale:
        primary = locale.lower().replace("_", "-").split("-", 1)[0]
        completion = _LOCALE_COMPLETIONS.get(primary)
        if completion is not None:
            return completion
    return _NEUTRAL_COMPLETION
