"""Build-time voice auto-assignment (Issue 1, fail-soft).

At persona create the global TTS default is a single English voice, so a female
persona spoke with a male voice (and vice-versa). This service gives a freshly
created persona a *fitting* catalogue voice: a small-tier model reads the
persona's identity and the language-filtered voice catalogue and picks the voice
whose gender and character best match — the audible analogue of the avatar
auto-generation hook next to it in the create flow.

Everything fail-softs. A persona that cannot be voiced (TTS unconfigured, the
catalogue fetch fails, the model returns nothing usable, the builder already
chose a voice) keeps the global default — exactly the pre-Issue-1 behaviour, so
create never fails on a voice-pick problem.

The voice catalogue lives in the separate ``persona-voice`` service; this module
reaches it over its public ``GET /v1/voices`` endpoint, forwarding the caller's
bearer token (the same any-signed-in-user auth the web voice-selector uses), so
no service credential is introduced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from persona.logging import get_logger
from persona.schema.conversation import ConversationMessage
from sqlalchemy import select

from persona_api.db.models import personas as personas_t
from persona_api.middleware.rls_context import current_user_id
from persona_api.services import persona_service
from persona_api.services.background_billing import bill_background_llm
from persona_api.services.llm_usage_collector import UsageCollectingBackend, collect_llm_usage

if TYPE_CHECKING:
    from persona.backends.protocol import ChatBackend
    from persona.schema.persona import Persona
    from persona_runtime.tier import TierRegistry
    from sqlalchemy import Engine
    from starlette.requests import Request

    from persona_api.config import APIConfig

__all__ = [
    "choose_voice",
    "maybe_assign_voice",
    "maybe_remap_voice",
    "reconcile_voice_assignments",
]

_LOG = get_logger("api.voice_assignment")

#: Wall-clock bound on the cross-service catalogue fetch. The voice service
#: re-fetches the full provider catalogue (with preview URLs) from Cartesia per
#: call, which can take several seconds — and longer when its event loop is
#: briefly busy (e.g. an agent embedder load during a concurrent call). Matches
#: the build-time avatar budget; a slower/absent voice service fail-softs to the
#: global default rather than timing out at 10s.
_CATALOGUE_TIMEOUT_S = 25.0
#: Cap on how many voices reach the model prompt — keeps the prompt bounded for
#: a large provider catalogue while leaving a healthy gender mix to choose from.
_MAX_CATALOGUE = 60
#: The pick is a single voice_id; a tight cap keeps the call cheap.
_PICK_MAX_TOKENS = 64


@dataclass(frozen=True)
class _VoiceOption:
    """One catalogue voice as the picker sees it (the model-relevant fields)."""

    voice_id: str
    name: str
    gender: str
    description: str


#: Persona gender presentations the picker reasons about. ``feminine`` /
#: ``masculine`` are the values we hard-constrain the voice gender to; the rest
#: leave the choice to character fit.
_GENDERED = frozenset({"feminine", "masculine"})


def _pick_messages(persona: Persona, options: list[_VoiceOption]) -> list[ConversationMessage]:
    """Build the voice-pick prompt: persona identity + the compact catalogue.

    Asks the model to FIRST commit to the persona's gender presentation, then
    pick a voice — both are returned so the caller can hard-enforce the gender
    match (the model occasionally picks a good-character voice of the wrong
    gender; the catalogue's per-voice ``gender`` lets us correct that).
    """
    catalogue = "\n".join(
        f"- {o.voice_id} | {o.gender} | {o.name}: {o.description}".rstrip(": ") for o in options
    )
    system = (
        "You assign a text-to-speech voice to an AI persona. You are given the "
        "persona's identity and a catalogue of voices, each line as "
        "'voice_id | gender | name: description'. FIRST decide the persona's most "
        "likely gender presentation, THEN choose the voice whose gender MATCHES "
        "that and whose character best fits. When a voice's description notes an "
        "accent or dialect (e.g. 'egyptian accent') that suits the persona's "
        "language or region, PREFER it — a dialect-appropriate voice sounds more "
        "natural (Spec V14 D-V14-4). Reply with EXACTLY two lines and nothing "
        "else:\n"
        "GENDER: <one of: feminine | masculine | neutral | unknown>\n"
        "VOICE: <the chosen voice_id, exactly as written>"
    )
    user = (
        f"Persona name: {persona.identity.name}\n"
        f"Role: {persona.identity.role}\n"
        f"Background: {persona.identity.background}\n"
        f"Language: {persona.identity.language_default}\n\n"
        f"Available voices:\n{catalogue}\n\n"
        "Your answer:"
    )
    now = datetime.now(UTC)
    return [
        ConversationMessage(role="system", content=system, created_at=now),
        ConversationMessage(role="user", content=user, created_at=now),
    ]


def _parse_pick(text: str) -> tuple[str | None, str]:
    """Split the model reply into ``(inferred_gender, voice_text)``.

    Tolerant of a model that ignores the format: a missing ``GENDER:``/``VOICE:``
    line yields ``None`` / the whole reply, so the caller falls back to scanning
    the full text for a voice id (the historical single-line behaviour).
    """
    gender: str | None = None
    voice_text = ""
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("gender:"):
            value = lowered.split(":", 1)[1].strip()
            if value in {"feminine", "masculine", "neutral", "unknown"}:
                gender = value
        elif lowered.startswith("voice:"):
            voice_text = stripped.split(":", 1)[1].strip()
    return gender, voice_text


def _extract_choice(text: str, valid_ids: list[str]) -> str | None:
    """Resolve the model's reply to a catalogue voice_id, or ``None``.

    Prefers an exact match (the instructed reply shape); falls back to any
    catalogue id that appears verbatim in the reply, so a chatty model that
    wraps the id in quotes or prose still resolves. Returns ``None`` when the
    reply names no known voice — the caller then leaves the persona unvoiced.
    """
    stripped = text.strip()
    for vid in valid_ids:
        if stripped == vid:
            return vid
    for vid in valid_ids:
        if vid in text:
            return vid
    return None


async def choose_voice(
    *, persona: Persona, backend: ChatBackend, options: list[_VoiceOption]
) -> str | None:
    """Ask ``backend`` to pick the best-fitting voice from ``options``.

    Pure of I/O beyond the model call — the orchestration seam the unit tests
    drive with a fake backend. Returns the chosen ``voice_id`` (guaranteed to be
    one of ``options``) or ``None`` when nothing usable came back.
    """
    catalogue = options[:_MAX_CATALOGUE]
    if not catalogue:
        return None
    response = await backend.chat(
        _pick_messages(persona, catalogue), temperature=0.0, max_tokens=_PICK_MAX_TOKENS
    )
    gender, voice_text = _parse_pick(response.content)
    # The voice id from the VOICE: line, else scanned from the whole reply.
    voice_id = _extract_choice(voice_text or response.content, [o.voice_id for o in catalogue])

    # Hard-enforce the gender match: when the persona reads clearly feminine or
    # masculine, the picked voice MUST share that gender. The model's per-voice
    # gender is right far more often than its final pick, so if it chose a
    # mismatched (or no) voice, snap to a voice of the inferred gender.
    if gender in _GENDERED:
        by_id = {o.voice_id: o for o in catalogue}
        chosen = by_id.get(voice_id) if voice_id is not None else None
        if chosen is None or chosen.gender != gender:
            corrected = next((o for o in catalogue if o.gender == gender), None)
            if corrected is not None:
                return corrected.voice_id
    return voice_id


async def _fetch_catalogue(
    base_url: str, *, bearer: str | None, language: str | None
) -> tuple[str | None, list[_VoiceOption]]:
    """Fetch the language-filtered voice catalogue from the persona-voice service.

    Forwards the caller's bearer token to ``GET /v1/voices`` (the endpoint
    authorises any signed-in user). Returns ``(provider, options)``; ``provider``
    is ``None`` when TTS is unconfigured there (empty catalogue).
    """
    headers = {"authorization": bearer} if bearer else {}
    params = {"language": language} if language else {}
    url = f"{base_url.rstrip('/')}/v1/voices"
    async with httpx.AsyncClient(timeout=_CATALOGUE_TIMEOUT_S) as client:
        response = await client.get(url, headers=headers, params=params)
    response.raise_for_status()
    data = response.json()
    provider = data.get("provider")
    options = [
        _VoiceOption(
            voice_id=str(entry["voice_id"]),
            name=str(entry.get("name") or ""),
            gender=str(entry.get("gender") or "unspecified"),
            description=str(entry.get("description") or ""),
        )
        for entry in data.get("voices", [])
        if isinstance(entry, dict) and entry.get("voice_id")
    ]
    return provider, options


async def maybe_assign_voice(
    request: Request, *, owner_id: str, persona_id: str, yaml_str: str
) -> None:
    """Auto-assign a fitting voice to a freshly created persona (fail-soft).

    A no-op when the feature is unconfigured (no ``voice_service_url``), the tier
    registry is absent, the persona already declares a voice (the builder chose —
    never overridden), the catalogue is empty/unreachable, or the model returns
    nothing usable. Never raises into the create path — a voice-pick problem must
    never break persona creation (the avatar hook's fail-soft contract).
    """
    state = request.app.state
    config = getattr(state, "config", None)
    base_url = getattr(config, "voice_service_url", "") if config is not None else ""
    registry = getattr(state, "tier_registry", None)
    rls_engine = getattr(state, "rls_engine", None)
    if not base_url or registry is None or rls_engine is None:
        return

    try:
        persona = persona_service.load_persona_from_yaml(
            yaml_str, persona_id=persona_id, owner_id=owner_id
        )
    except Exception:  # noqa: BLE001 — create already validated; defensive only
        return
    if persona.identity.voice is not None:
        return  # the builder picked a voice — auto-pick never overrides it

    bearer = request.headers.get("authorization")
    try:
        provider, options = await _fetch_catalogue(
            base_url, bearer=bearer, language=persona.identity.language_default
        )
    except Exception as exc:  # noqa: BLE001 — network/provider error → keep default
        # Surface WHY at WARNING (R9-025 reopen leg A — the months-silent-failure
        # lesson): this exact catch swallowed the SAME auth misconfiguration the
        # tts/stt proxies hit (a 401 from the voice service's forwarded bearer) at
        # INFO for months, with nobody noticing personas kept coming out voiceless.
        # An HTTP error's response carries the real reason (e.g. an expired/invalid
        # token); the upstream status is pulled out explicitly so it is
        # grep/alert-able without parsing the exception's free-form repr.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        body = getattr(getattr(exc, "response", None), "text", "")
        _LOG.warning(
            "voice auto-pick skipped: catalogue unavailable "
            "(persona_id={pid}, status={status}): {err} {body}",
            pid=persona_id,
            status=status,
            err=repr(exc)[:300],  # repr carries the exception type (empty for timeouts)
            body=str(body)[:200],
        )
        return
    if provider is None or not options:
        return

    try:
        # Spec M3 (T5b): wrap the pick model's backend so the real usage of the
        # auto-pick call is captured for owner billing.
        backend = UsageCollectingBackend(registry.get(getattr(config, "voice_pick_tier", "small")))
        with collect_llm_usage() as usage:
            choice = await choose_voice(persona=persona, backend=backend, options=options)
    except Exception as exc:  # noqa: BLE001 — model/routing error → keep default
        _LOG.warning("voice auto-pick failed at model selection", persona_id=persona_id)
        _LOG.debug("voice pick model error", error=str(exc)[:200])
        return
    # Spec M3 (T5b, D-M3-8): owner-bill the auto-pick's real model cost, idempotent
    # (once per persona) + fail-soft — charged whether or not the pick was usable
    # (the LLM cost was incurred). No model call (empty catalogue path handled above)
    # → charges nothing.
    totals = usage.totals()
    bill_background_llm(
        credits_policy=getattr(state, "credits_policy", None),
        rls_engine=rls_engine,
        owner_id=owner_id,
        provider=totals.provider,
        model=totals.model,
        prompt_tokens=totals.prompt_tokens,
        completion_tokens=totals.completion_tokens,
        cost_usd=totals.cost_usd,
        surface="voice_pick",
        billing_key=f"voice_pick:{persona_id}",
        cost_source=getattr(state, "metadata_resolver", None),
        floor=getattr(config, "agentic_credit_floor", 1),
    )
    if choice is None:
        return

    try:
        persona_service.set_voice(
            rls_engine=rls_engine, persona_id=persona_id, provider=provider, voice_id=choice
        )
    except Exception as exc:  # noqa: BLE001 — persist error → keep default
        _LOG.warning("voice auto-pick failed to persist", persona_id=persona_id)
        _LOG.debug("voice persist error", error=str(exc)[:200])
        return
    _LOG.info("voice auto-assigned", persona_id=persona_id, provider=provider, voice_id=choice)


async def _remap_voice_for(
    persona: Persona,
    *,
    persona_id: str,
    config: APIConfig | None,
    registry: TierRegistry | None,
    rls_engine: Engine | None,
    bearer: str | None,
) -> bool:
    """The shared AUTO-REMAP core (Spec V14 D-V14-13/T5b) — ``persona`` already resolved.

    Factored out of :func:`maybe_remap_voice` so both the per-request path (which
    forwards the caller's bearer token) and :func:`reconcile_voice_assignments`
    (the boot-time reconciliation pass, which has no caller — a bearer of
    ``None``) share one fail-soft implementation. A community/no-auth voice
    service accepts the anonymous read; a cloud/auth-required voice service
    fail-softs the exact same way an unreachable catalogue does.

    LOSSLESS + BIDIRECTIONAL (T5b, review finding I1's fix): when the persona
    already has a voice REMEMBERED for the active provider
    (``identity.voice_by_provider[provider]``), that exact ``voice_id`` is
    RESTORED rather than auto-picked fresh — a provider flip away and back
    returns the persona to precisely the voice it had before, never a shared
    default and never a losing swap. Only a persona with no memory for the
    active provider (voiced under it for the first time) falls through to the
    model auto-pick, which then records the pick as new memory via
    :func:`persona_service.set_voice`'s choke point.
    """
    base_url = getattr(config, "voice_service_url", "") if config is not None else ""
    if not base_url or registry is None or rls_engine is None:
        return False

    try:
        provider, options = await _fetch_catalogue(
            base_url, bearer=bearer, language=persona.identity.language_default
        )
    except Exception as exc:  # noqa: BLE001 — network/provider error → keep current
        status = getattr(getattr(exc, "response", None), "status_code", None)
        _LOG.warning(
            "voice auto-remap skipped: catalogue unavailable (persona_id={pid}, "
            "status={status}): {err}",
            pid=persona_id,
            status=status,
            err=repr(exc)[:200],
        )
        return False
    if provider is None:
        return False

    # Idempotent: a persona already voiced on the ACTIVE provider needs no remap.
    current = persona.identity.voice
    if current is not None and current.provider == provider:
        return False

    # Spec V14-T5b (review finding I1's fix — LOSSLESS + BIDIRECTIONAL): before
    # picking a fresh voice, check whether this persona already has a REMEMBERED
    # voice for the active provider (it was voiced under this provider before, at
    # some earlier point — e.g. before a prior flip away from it). If so, restore
    # that EXACT voice_id rather than auto-picking a new one — a Cartesia→
    # ElevenLabs→Cartesia round-trip must return the persona's ORIGINAL Cartesia
    # voice, not a fresh pick or the shared default. A persona with no memory for
    # this provider (e.g. created entirely under a different one) falls through
    # to the auto-pick below, exactly as before. Deliberately does NOT require
    # ``options`` (the language-filtered catalogue) to be non-empty — restoring a
    # known-good voice_id never needed catalogue membership before either
    # (``resolve_voice`` at synthesis time trusts a configured id the same way).
    remembered = (persona.identity.voice_by_provider or {}).get(provider)
    if remembered is not None:
        try:
            persona_service.set_voice(
                rls_engine=rls_engine, persona_id=persona_id, provider=provider, voice_id=remembered
            )
        except Exception as exc:  # noqa: BLE001 — persist error → keep current
            _LOG.warning("voice auto-remap restore failed to persist", persona_id=persona_id)
            _LOG.debug("voice remap restore persist error", error=str(exc)[:200])
            return False
        _LOG.info(
            "voice restored from per-provider memory on remap",
            persona_id=persona_id,
            provider=provider,
            voice_id=remembered,
            from_provider=current.provider if current is not None else None,
        )
        return True
    if not options:
        return False

    try:
        backend = registry.get(getattr(config, "voice_pick_tier", "small"))
        choice = await choose_voice(persona=persona, backend=backend, options=options)
    except Exception as exc:  # noqa: BLE001 — model/routing error → keep current
        _LOG.warning("voice auto-remap failed at model selection", persona_id=persona_id)
        _LOG.debug("voice remap model error", error=str(exc)[:200])
        return False
    if choice is None:
        return False

    try:
        persona_service.set_voice(
            rls_engine=rls_engine, persona_id=persona_id, provider=provider, voice_id=choice
        )
    except Exception as exc:  # noqa: BLE001 — persist error → keep current
        _LOG.warning("voice auto-remap failed to persist", persona_id=persona_id)
        _LOG.debug("voice remap persist error", error=str(exc)[:200])
        return False
    _LOG.info(
        "voice auto-remapped to active provider",
        persona_id=persona_id,
        provider=provider,
        voice_id=choice,
        from_provider=current.provider if current is not None else None,
    )
    return True


async def maybe_remap_voice(
    request: Request, *, owner_id: str, persona_id: str, yaml_str: str
) -> bool:
    """Re-pick a persona's voice under the ACTIVE TTS provider (Spec V14 D-V14-13).

    The AUTO-REMAP the owner ruled (supersedes default-until-re-picked): when the
    active TTS provider differs from the provider a persona's stored voice is
    addressed to (a provider switch — e.g. Cartesia → ElevenLabs), assign the
    persona a fitting voice on the ACTIVE provider so it keeps a distinct,
    provider-correct voice rather than falling to the shared default.

    Unlike :func:`maybe_assign_voice` (create-time; skips already-voiced personas),
    this re-picks a voiced persona when its provider is stale — AND covers the
    voiceless case (re-picks from nothing). It is idempotent: a persona already on
    the active provider is a no-op (returns ``False``). Fail-soft throughout —
    never raises into the caller; returns ``True`` iff it actually re-picked.

    The trigger (who calls this, and when) is deliberately left to the caller —
    the active provider is only known after the catalogue fetch, so this function
    learns it, compares, and acts. Fully self-contained + RLS-scoped to the
    request's owner. (The boot-time trigger is :func:`reconcile_voice_assignments`,
    which shares this same core via :func:`_remap_voice_for`.)
    """
    state = request.app.state
    config = getattr(state, "config", None)
    registry = getattr(state, "tier_registry", None)
    rls_engine = getattr(state, "rls_engine", None)

    try:
        persona = persona_service.load_persona_from_yaml(
            yaml_str, persona_id=persona_id, owner_id=owner_id
        )
    except Exception:  # noqa: BLE001 — defensive only
        return False

    bearer = request.headers.get("authorization")
    return await _remap_voice_for(
        persona,
        persona_id=persona_id,
        config=config,
        registry=registry,
        rls_engine=rls_engine,
        bearer=bearer,
    )


async def reconcile_voice_assignments(
    *,
    config: APIConfig | None,
    registry: TierRegistry | None,
    sweep_engine: Engine | None,
    rls_engine: Engine | None,
) -> dict[str, int]:
    """Boot-time AUTO-REMAP reconciliation over every persona (Spec V14 D-V14-13 trigger).

    The wiring the T4b/T4c batch report flagged as unwired: this is the (a)
    API-startup reconciliation the owner ruled — a NON-BLOCKING background task
    started at API lifespan start (never on the readiness/request path, the
    R9-027 lesson), that walks every persona and re-picks the ones whose stored
    voice no longer matches the active TTS provider.

    **SYMMETRIC as of Spec V14-T5b** (review finding I1's fix): this pass runs
    for whichever provider is active, ``cartesia`` included — a flip TO
    ``elevenlabs`` and a flip BACK to ``cartesia`` are handled by the exact same
    code path. The earlier version cheap-skipped the entire pass whenever the
    active provider was ``cartesia`` (reasoning: the default can never mismatch
    a Cartesia-addressed voice) — true only BEFORE any remap had ever happened;
    once a persona had been auto-remapped to ElevenLabs, that same persona was
    now ElevenLabs-addressed, so flipping back to Cartesia left it mismatched
    and permanently un-reconciled — the exact rollback data-loss the review
    flagged. There is no purely provider-name-based shortcut that stays correct
    in both directions, so the shortcut is gone; efficiency instead comes from
    two remaining cheap layers:

    - The active provider is still read from ``config.voice_tts_provider`` (a
      plain env mirror of ``PERSONA_TTS_PROVIDER`` — no network call) and the
      feature-unconfigured checks below still short-circuit with zero DB reads.
    - The persona listing itself (below) is ONE cross-tenant read on
      ``sweep_engine`` — the RLS-bypassing engine, mirroring
      :func:`persona_api.background.restart_sweep.reconcile_in_flight_on_startup`
      — local to the API's own Postgres, not a network hop. Each persona is
      then compared CHEAPLY (its stored ``identity.voice.provider`` parsed from
      its own YAML, no network) against the active provider — only a
      mismatched (or voiceless) persona pays the catalogue-fetch + (memory
      restore or model-pick) cost that :func:`_remap_voice_for` performs. A
      normal boot where every persona already matches the active provider —
      the common case in both directions — does exactly one local SQL read and
      zero catalogue fetches.
    - RLS-scoped writes: the sweep loop carries no ambient tenant context, so the
      owner scope is set via the ``current_user_id`` contextvar per persona (the
      same pattern :class:`~persona_api.tasks.dead_leg_sweep.DeadLegSweeper` uses)
      before calling :func:`_remap_voice_for` on ``rls_engine`` and reset in a
      ``finally`` — no scope leak across personas.
    - Fully fail-soft: any unexpected error (e.g. the persona listing itself
      failing) is caught, logged ONCE at WARNING, and the pass returns its
      partial counts — it never crashes or delays boot, and a failed persona
      simply keeps :func:`_remap_voice_for`'s (T4b) fail-soft default voice.

    Returns:
        ``{"scanned", "remapped", "skipped", "failed"}`` counts (all zero when
        the feature is unconfigured) — logged by the caller / assertable by
        tests.
    """
    counts = {"scanned": 0, "remapped": 0, "skipped": 0, "failed": 0}
    if config is None or registry is None or sweep_engine is None or rls_engine is None:
        return counts
    if not getattr(config, "voice_service_url", ""):
        return counts
    active_provider = getattr(config, "voice_tts_provider", "cartesia")

    try:
        with sweep_engine.begin() as conn:
            rows = conn.execute(
                select(personas_t.c.id, personas_t.c.owner_id, personas_t.c.yaml)
            ).all()
    except Exception as exc:  # noqa: BLE001 — never block/crash boot on a listing failure
        _LOG.warning(
            "voice remap reconciliation skipped: persona listing failed: {err}",
            err=repr(exc)[:200],
        )
        return counts

    for row in rows:
        counts["scanned"] += 1
        try:
            persona = persona_service.load_persona_from_yaml(
                row.yaml, persona_id=row.id, owner_id=row.owner_id
            )
        except Exception:  # noqa: BLE001 — corrupt/legacy row, skip
            counts["failed"] += 1
            continue

        # Cheap check BEFORE any catalogue fetch: a persona already addressed to
        # the active provider needs no network call at all.
        voice = persona.identity.voice
        if voice is not None and voice.provider == active_provider:
            counts["skipped"] += 1
            continue

        token = current_user_id.set(row.owner_id)
        try:
            remapped = await _remap_voice_for(
                persona,
                persona_id=row.id,
                config=config,
                registry=registry,
                rls_engine=rls_engine,
                bearer=None,  # no per-request caller at boot; community/no-auth accepts it
            )
        finally:
            current_user_id.reset(token)
        if remapped:
            counts["remapped"] += 1
        else:
            counts["skipped"] += 1

    if counts["remapped"] or counts["failed"]:
        _LOG.info(
            "voice remap reconciliation complete",
            scanned=counts["scanned"],
            remapped=counts["remapped"],
            skipped=counts["skipped"],
            failed=counts["failed"],
        )
    return counts
