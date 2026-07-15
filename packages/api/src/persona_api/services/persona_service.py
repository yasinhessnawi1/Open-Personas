"""Persona CRUD + memory-store population (spec 08, T07, D-08-8).

Business logic for the persona endpoints, decoupled from FastAPI. Each function
takes the RLS-scoped resources the route resolves (the connection + the RLS
engine), so every DB access — the ``personas`` row AND the memory-chunk writes
through the typed stores — runs under the authenticated tenant's scope (D-08-1).

On create, the persona's identity/self_facts/worldview entries are embedded and
written to ``memory_chunks`` via the four typed stores composing
``PostgresBackend`` — exactly the CLI's composition, but RLS-scoped and on
Postgres (D-08-8). Episodic starts empty (runtime-written).
"""

from __future__ import annotations

import shutil
import uuid
from typing import TYPE_CHECKING

import yaml
from persona.audit import AuditLogger, JSONLAuditLogger
from persona.errors import PersonaError, PersonaNotFoundError
from persona.language_capability import serviceability_warning
from persona.logging import get_logger
from persona.registry import PersonaRegistry
from persona.schema.defaults import ensure_default_capabilities
from persona.schema.persona import Persona
from persona.schema.safety import SAFETY_CONSTRAINT, ensure_safety_constraint
from persona.stores import (
    EpisodicStore,
    IdentityStore,
    SelfFactsStore,
    WorldviewStore,
)
from persona.stores.embedder import SentenceTransformerEmbedder
from persona.stores.postgres import PostgresBackend
from sqlalchemy import delete, func, insert, select, update

from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import personas as personas_t
from persona_api.schemas import PersonaSummary
from persona_api.services import notifications_service

if TYPE_CHECKING:
    from pathlib import Path

    from persona.stores.backend import Backend
    from persona.stores.embedder import Embedder
    from sqlalchemy import Connection, Engine

__all__ = [
    "create_persona",
    "default_embedder",
    "delete_persona",
    "get_persona",
    "list_personas",
    "load_persona_from_yaml",
    "notify_persona_ready",
    "persona_display_name",
    "persona_name_from_yaml",
    "set_avatar_url",
    "summary_of",
    "update_persona",
    "write_persona_ready",
]


def persona_name_from_yaml(raw: str) -> str | None:
    """Best-effort extract ``identity.name`` from a persona YAML blob (None on any miss).

    Shared by the server-authored notification copy (Spec P6, D4-c/d) — used for
    both the run-terminal and persona-ready notification ``params``.
    """
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    if isinstance(data, dict):
        identity = data.get("identity")
        if isinstance(identity, dict):
            name = identity.get("name")
            if isinstance(name, str):
                return name
    return None


def write_persona_ready(conn: Connection, persona_id: str) -> None:
    """Write the "persona is ready" notification on an existing owner-scoped conn.

    The non-swallowing core shared by BOTH avatar paths (P6-D-4 convergence): the
    in-process :func:`set_avatar_url` and the durable-queue handler. Reads owner +
    name RLS-scoped, then the idempotent create (``(owner, kind, ref_id)`` → one row
    even if both paths fire). NOT best-effort itself — the CALLER owns the
    transaction and the try/except, so a failure rolls back only this write's own
    transaction and never touches the avatar write (D-P6-12).
    """
    row = conn.execute(
        select(personas_t.c.owner_id, personas_t.c.yaml).where(personas_t.c.id == persona_id)
    ).first()
    if row is None:
        return  # persona gone (concurrent delete) — nothing to announce.
    name = persona_name_from_yaml(row.yaml) if row.yaml else None
    params = {"persona": name} if name else {}
    notifications_service.create_notification(
        conn=conn,
        owner_id=row.owner_id,
        kind="persona_ready",
        ref_id=persona_id,
        level="success",
        message_key="notifications.persona.ready",
        params=params,
    )


def notify_persona_ready(*, rls_engine: Engine, persona_id: str) -> None:
    """Best-effort persona-ready notification via a fresh owner-scoped transaction.

    The engine-based convenience for callers that hold an ``Engine`` (the in-process
    avatar hook, :func:`set_avatar_url`). Isolated: its OWN ``begin()`` transaction,
    so a feed-write failure rolls back only this write and NEVER fails the avatar
    write (D-P6-12). The queue handler, which holds only a ``Connection``, wraps
    :func:`write_persona_ready` directly with the same best-effort guard.
    """
    try:
        with rls_engine.begin() as conn:
            write_persona_ready(conn, persona_id)
    except Exception as exc:  # noqa: BLE001 — advisory; never fail the avatar write
        _LOG.warning(
            "persona-ready notification write failed", persona_id=persona_id, error=str(exc)
        )


_LOG = get_logger("services.persona")


def _warn_if_language_unserviceable(persona: Persona) -> None:
    """Author-time voice-language warning (Spec 32 D-32-4).

    Non-blocking: a persona whose declared language the configured voice
    providers can't serve still saves, but the author is warned (the persona's
    calls will fall back to English) before a call rather than during one. The
    complement to the call-time soft-fallback.
    """
    warning = serviceability_warning(persona.identity.language_default)
    if warning is not None:
        _LOG.warning(
            "persona declares an unserviceable voice language (persona_id={pid}): {msg}",
            pid=persona.persona_id or "",
            msg=warning,
        )


def load_persona_from_yaml(yaml_str: str, *, persona_id: str, owner_id: str) -> Persona:
    """Validate a YAML string into a Persona, assigning id + owner (D-08-8).

    Raises ``PersonaError`` on malformed YAML and ``pydantic.ValidationError``
    (mapped to 422 by the handlers) on a schema-shape mismatch.
    """
    try:
        raw = yaml.safe_load(yaml_str)
    except yaml.YAMLError as exc:
        raise PersonaError("invalid YAML", context={"reason": str(exc)[:200]}) from exc
    if not isinstance(raw, dict):
        raise PersonaError(
            "persona YAML must be a top-level mapping",
            context={"actual_type": type(raw).__name__},
        )
    # The API owns id + owner; the YAML's own values (if any) are overridden so
    # a tenant can't author another owner's persona.
    raw["persona_id"] = persona_id
    raw["owner_id"] = owner_id
    return Persona.model_validate(raw)  # ValidationError → 422


def _guard_safety(persona: Persona, yaml_str: str) -> tuple[Persona, str]:
    """Guarantee the mandatory safety constraint on the persona AND the stored YAML.

    Spec 36, D-36-safety-server: direct-create posts a structured YAML with no
    model in the loop, so the drafter prompt's instruction to include the safety
    constraint cannot be relied on. This is the enforcement floor for *every*
    create/update path. The constraint must end up in the **stored** YAML — the
    runtime re-loads the persona from it, so guarding only the in-memory object
    would leave the persona unsafe on its next load.

    Idempotent + churn-free: when the constraint is already present (every
    prebuilt starter and drafter output), the original ``yaml_str`` is returned
    unchanged. Only a submission that stripped the constraint is re-dumped — and
    only then does the stored representation change — re-injecting it as the
    first constraint of the original mapping (authored shape preserved).
    """
    guarded = ensure_safety_constraint(persona)
    if guarded is persona:
        return persona, yaml_str
    raw = yaml.safe_load(yaml_str)
    identity = raw.setdefault("identity", {})
    existing = identity.get("constraints") or []
    identity["constraints"] = [SAFETY_CONSTRAINT, *existing]
    guarded_yaml = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    _LOG.warning(
        "re-asserted the mandatory safety constraint on a persona that omitted it "
        "(persona_id={pid})",
        pid=persona.persona_id or "",
    )
    return guarded, guarded_yaml


def _guard_default_capabilities(persona: Persona, yaml_str: str) -> tuple[Persona, str]:
    """Guarantee the default capability floor on the persona AND the stored YAML.

    A persona created without baseline tools/skills can't read uploaded files,
    run code, search the web, or generate documents — so it answers from
    imagination and hallucinates. The authoring prompt suggests a capability set,
    but direct-create posts a structured YAML with no model in the loop, so that
    suggestion cannot be relied on. This is the enforcement floor for *every*
    create/update path. The defaults must end up in the **stored** YAML — the
    runtime re-loads the persona from it, so guarding only the in-memory object
    would leave the persona under-equipped on its next load.

    Idempotent + churn-free: when every default is already present (the common
    case), the original ``yaml_str`` is returned unchanged. Only a submission
    missing one or more defaults is re-dumped — and only then does the stored
    representation change — appending the missing defaults after the persona's
    existing entries (authored order preserved).
    """
    guarded = ensure_default_capabilities(persona)
    if guarded is persona:
        return persona, yaml_str
    raw = yaml.safe_load(yaml_str)
    raw["tools"] = list(guarded.tools)
    raw["skills"] = list(guarded.skills)
    guarded_yaml = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    _LOG.warning(
        "added the default capability floor to a persona that omitted some "
        "(persona_id={pid}, tools={tools}, skills={skills})",
        pid=persona.persona_id or "",
        tools=list(guarded.tools),
        skills=list(guarded.skills),
    )
    return guarded, guarded_yaml


def _stored_voice_by_provider(yaml_str: str) -> dict[str, str]:
    """Best-effort extract ``identity.voice_by_provider`` from a CURRENTLY
    STORED persona YAML (read fresh from the row, before it is overwritten).

    The merge base :func:`_remember_voice_by_provider` needs on the update
    path: the incoming PATCH body (already validated into a ``Persona`` by
    the caller) rarely carries this internal memory field forward — the
    editor's ``VoiceSelector`` -> ``PersonaForm`` -> ``PersonaEditor`` autosave
    PATCHes only the fields its form model knows about — so merging against
    the incoming YAML's OWN (usually absent) map silently drops every
    provider entry the PATCH didn't mention (V14-T5c: the bug this fixes).
    Reading the row still in the database and folding it into the merge base
    is what makes the manual re-pick lossless, matching :func:`set_voice`'s
    proven approach of reading the current stored map before merging in the
    new entry. Fails soft (malformed/missing YAML or field → ``{}``) since
    this is a memory *enhancement*, never a reason to fail the write.
    """
    try:
        raw = yaml.safe_load(yaml_str)
    except yaml.YAMLError:
        return {}
    if not isinstance(raw, dict):
        return {}
    identity = raw.get("identity")
    if not isinstance(identity, dict):
        return {}
    by_provider = identity.get("voice_by_provider")
    if not isinstance(by_provider, dict):
        return {}
    return {k: v for k, v in by_provider.items() if isinstance(k, str) and isinstance(v, str)}


def _remember_voice_by_provider(
    persona: Persona, yaml_str: str, *, stored_by_provider: dict[str, str] | None = None
) -> tuple[Persona, str]:
    """Record ``identity.voice`` into the persona's per-provider voice memory.

    Spec V14-T5b (review finding I1's fix): the manual re-pick surface (the
    persona editor's ``VoiceSelector``) PATCHes the persona's FULL YAML via
    :func:`update_persona` — it never calls :func:`set_voice`, so that choke
    point alone cannot see a manual pick. This guard is the create/update-path
    twin: whenever the incoming/authored YAML declares a voice, it is merged
    into ``identity.voice_by_provider[voice.provider]`` on BOTH the persona
    object and the stored YAML — so every surface that can set a voice
    (builder-authored, auto-pick, auto-remap, or a manual re-pick) keeps the
    memory complete, and a later provider flip can restore whichever voice
    this persona last used under that provider.

    V14-T5c fix: the merge base is the union of ``stored_by_provider`` (the
    map still sitting in the database, passed by :func:`update_persona` — see
    :func:`_stored_voice_by_provider`) AND whatever ``persona.identity`` itself
    declares, mirroring :func:`set_voice`'s merge-never-replace guarantee.
    Merging only against the incoming YAML's own (usually absent, on the
    manual re-pick path) map silently dropped every OTHER provider's
    remembered voice — exactly the bug this fixes. :func:`create_persona` has
    no prior stored row, so it omits ``stored_by_provider`` (defaults to
    ``None``), unaffected.

    Idempotent + churn-free, mirroring :func:`_guard_safety`'s shape: a
    voiceless persona, or one whose merged memory already matches what the
    incoming YAML itself declares, is returned unchanged (same object, same
    YAML string) — only an actual new fact (a new pick, or memory recovered
    from the stored row that the incoming YAML didn't carry) triggers a
    re-dump.
    """
    voice = persona.identity.voice
    if voice is None:
        return persona, yaml_str
    declared = persona.identity.voice_by_provider or {}
    merged = {**(stored_by_provider or {}), **declared}
    updated = {**merged, voice.provider: voice.voice_id}
    if updated == declared:
        return persona, yaml_str
    remembered_identity = persona.identity.model_copy(update={"voice_by_provider": updated})
    remembered = persona.model_copy(update={"identity": remembered_identity})

    raw = yaml.safe_load(yaml_str)
    identity = raw.setdefault("identity", {})
    identity["voice_by_provider"] = updated
    remembered_yaml = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    return remembered, remembered_yaml


def _build_registry(
    engine: Engine,
    embedder: Embedder,
    audit_root: Path,
    memory_backend: Backend | None = None,
    audit_logger: AuditLogger | None = None,
) -> PersonaRegistry:
    """Compose the four typed stores over the edition's memory backend.

    Cloud injects ``PostgresBackend`` (RLS-scoped engine); community injects a
    file-based ``ChromaBackend`` (Spec 33 D-33-X-memory-chroma-community). When no
    backend is injected, defaults to ``PostgresBackend`` (the historical CLI-style
    composition) so existing callers are unaffected. A hardcoded ``PostgresBackend``
    here would fail on the community SQLite path with ``no such table:
    memory_chunks`` — the create/update routes pass the app's edition backend.
    """
    backend = memory_backend or PostgresBackend(engine=engine, embedder=embedder)
    # R5-D-2: the app-selected audit backend (Postgres when multi-worker), else
    # the JSONL default (byte-unchanged for CLI / tests that pass no logger).
    audit = audit_logger or JSONLAuditLogger(audit_root)
    stores = {
        "identity": IdentityStore(backend=backend, audit_logger=audit),
        "self_facts": SelfFactsStore(backend=backend, audit_logger=audit),
        "worldview": WorldviewStore(backend=backend, audit_logger=audit),
        "episodic": EpisodicStore(backend=backend, audit_logger=audit),
    }
    return PersonaRegistry(stores=stores, audit_logger=audit)


def create_persona(
    *,
    rls_engine: Engine,
    embedder: Embedder,
    audit_root: Path,
    owner_id: str,
    yaml_str: str,
    avatar_url: str | None = None,
    memory_backend: Backend | None = None,
    audit_logger: AuditLogger | None = None,
) -> str:
    """Create a persona: insert the row + populate memory stores (D-08-8).

    Returns the generated ``persona_id`` (always set).

    ``rls_engine`` is RLS-scoped to ``owner_id`` via the request contextvar (the
    pool checkout listener, D-08-1). The ``personas`` row is committed FIRST (its
    own transaction) so the subsequent memory-chunk writes — which the typed
    stores' ``PostgresBackend`` runs on its own pooled connection — satisfy the
    ``memory_chunks.persona_id`` FK (a separate connection can't see an
    uncommitted row; verified in research). Both transactions carry the tenant
    GUC, so RLS holds throughout.
    """
    persona_id = f"persona_{uuid.uuid4().hex}"
    persona = load_persona_from_yaml(yaml_str, persona_id=persona_id, owner_id=owner_id)
    persona, yaml_str = _guard_safety(persona, yaml_str)
    persona, yaml_str = _guard_default_capabilities(persona, yaml_str)
    persona, yaml_str = _remember_voice_by_provider(persona, yaml_str)
    _warn_if_language_unserviceable(persona)

    # Synthetic-media provenance (Spec R3, R3-D-3): a user-supplied ``avatar_url`` at
    # create IS an upload, so co-write ``avatar_source='uploaded'`` in the SAME INSERT.
    # When ``avatar_url`` is null the row stays ``avatar_source=NULL`` (unknown) — the
    # avatar auto-generation hook (B1/B2) sets ``'generated'`` afterwards if it runs, or
    # it remains unknown (the honest floor). No NULL window for the upload case.
    avatar_source = "uploaded" if avatar_url is not None else None
    with rls_engine.begin() as conn:
        conn.execute(
            insert(personas_t).values(
                id=persona_id,
                owner_id=owner_id,
                yaml=yaml_str,
                schema_version=persona.schema_version,
                avatar_url=avatar_url,
                avatar_source=avatar_source,
            )
        )
    # Persona row committed → its FK target is visible to the store connections.
    registry = _build_registry(rls_engine, embedder, audit_root, memory_backend, audit_logger)
    registry.load_persona(persona)
    return persona_id


def update_persona(
    *,
    rls_engine: Engine,
    embedder: Embedder,
    audit_root: Path,
    owner_id: str,
    persona_id: str,
    yaml_str: str,
    avatar_url: str | None = None,
    memory_backend: Backend | None = None,
    audit_logger: AuditLogger | None = None,
) -> None:
    """Replace a persona's YAML (re-validated) and re-index its memory.

    ``avatar_url`` is updated only when provided (``None`` leaves it untouched —
    a PATCH semantics for the presentation field). When a new ``avatar_url`` is
    supplied this is the **upload-to-change** path: the bytes came from the user,
    so ``avatar_source`` is co-written ``'uploaded'`` in the SAME ``UPDATE`` (Spec
    R3, R3-D-3) — unforgeable synthetic-media provenance, no NULL window. The Art.
    50 disclosure derives from this stored signal.

    V14-T5c: the row's CURRENTLY STORED YAML is read first, in the SAME
    transaction as the write, so :func:`_remember_voice_by_provider` can merge
    the incoming voice into whatever ``voice_by_provider`` memory already sits
    in the database — not just what the PATCH body happens to declare (the
    manual re-pick path's PATCH usually declares none, which previously
    clobbered the memory instead of merging into it).
    """
    persona = load_persona_from_yaml(yaml_str, persona_id=persona_id, owner_id=owner_id)
    persona, yaml_str = _guard_safety(persona, yaml_str)
    persona, yaml_str = _guard_default_capabilities(persona, yaml_str)
    with rls_engine.begin() as conn:
        current = conn.execute(
            select(personas_t.c.yaml).where(personas_t.c.id == persona_id)
        ).first()
        if current is None:
            raise PersonaNotFoundError("persona not found", context={"id": persona_id})
        stored_by_provider = _stored_voice_by_provider(current[0])
        persona, yaml_str = _remember_voice_by_provider(
            persona, yaml_str, stored_by_provider=stored_by_provider
        )
        _warn_if_language_unserviceable(persona)
        values: dict[str, object] = {"yaml": yaml_str, "schema_version": persona.schema_version}
        if avatar_url is not None:
            values["avatar_url"] = avatar_url
            values["avatar_source"] = "uploaded"
        result = conn.execute(
            update(personas_t)
            .where(personas_t.c.id == persona_id)
            .values(**values)
            .returning(personas_t.c.id)
        )
        if result.first() is None:
            raise PersonaNotFoundError("persona not found", context={"id": persona_id})
    registry = _build_registry(rls_engine, embedder, audit_root, memory_backend, audit_logger)
    registry.load_persona(persona)


def set_avatar_url(
    *, rls_engine: Engine, persona_id: str, avatar_url: str, avatar_source: str = "generated"
) -> None:
    """Set a persona's ``avatar_url`` + provenance presentation fields (Spec 29 D-29-3).

    A narrow, RLS-scoped write for the build-time avatar auto-generation hook:
    unlike :func:`update_persona` it does NOT re-validate the YAML or re-index
    memory — it touches only the ``avatar_url`` + ``avatar_source`` columns. Silent
    if the row is absent (the create transaction just committed it; a concurrent
    delete is a no-op rather than an error, per the idempotency standard). The
    auto-gen hook runs only when ``avatar_url`` was null at create, so this never
    overwrites a user-supplied avatar (D-29 criterion 6).

    ``avatar_source`` is co-written in the SAME ``UPDATE`` as ``avatar_url`` (Spec
    R3, R3-D-3) so the synthetic-media provenance is unforgeable — there is no
    window where the url is set but provenance is NULL. Defaults to ``'generated'``
    because the only caller is the avatar auto-generation hook (the bytes came from
    the image-gen path); the Art. 50 disclosure derives from this stored signal.
    """
    with rls_engine.begin() as conn:
        conn.execute(
            update(personas_t)
            .where(personas_t.c.id == persona_id)
            .values(avatar_url=avatar_url, avatar_source=avatar_source)
        )
    # Spec P6 (D4-d): the persona is now visually complete — announce it (the
    # cross-device "persona is ready" bell). Best-effort; never fails the write.
    notify_persona_ready(rls_engine=rls_engine, persona_id=persona_id)


def set_voice(*, rls_engine: Engine, persona_id: str, provider: str, voice_id: str) -> None:
    """Inject ``identity.voice`` into a persona's stored YAML (Issue 1, narrow write).

    Reads the current YAML, sets ``identity.voice`` to the ``"provider:voice_id"``
    shorthand the schema accepts (normalised to a ``CatalogueVoice`` at load), and
    rewrites ONLY the ``yaml`` column — no memory re-index, since the voice is read
    from the persona definition at synthesis time, never retrieved semantically.
    Silent if the row is absent or the YAML has no ``identity`` mapping. Mirrors
    :func:`set_avatar_url`'s narrow-write shape; the build-time voice
    auto-assignment hook and the provider-flip auto-remap (below) are the
    programmatic callers, and both run without overwriting the OTHER provider's
    remembered voice.

    Spec V14-T5b (review finding I1's fix): this is the single choke point every
    PROGRAMMATIC voice write flows through (auto-pick at create, auto-remap on a
    provider flip), so it ALSO records the choice into
    ``identity.voice_by_provider[provider]`` — merged, never replaced wholesale,
    so a persona's memory of its OTHER providers' voices survives. A later flip
    back to ``provider`` can then restore this exact ``voice_id`` (lossless
    rollback) instead of losing it or falling to a shared default. The manual
    re-pick path (the persona editor) does not call this function — it PATCHes
    the full YAML via :func:`update_persona`, which records the same memory via
    :func:`_remember_voice_by_provider` so the memory stays complete regardless
    of which surface set the voice.
    """
    with rls_engine.begin() as conn:
        row = conn.execute(select(personas_t.c.yaml).where(personas_t.c.id == persona_id)).first()
        if row is None:
            return
        raw = yaml.safe_load(row[0])
        if not isinstance(raw, dict) or not isinstance(raw.get("identity"), dict):
            return
        raw["identity"]["voice"] = f"{provider}:{voice_id}"
        by_provider = raw["identity"].get("voice_by_provider")
        if not isinstance(by_provider, dict):
            by_provider = {}
        by_provider[provider] = voice_id
        raw["identity"]["voice_by_provider"] = by_provider
        new_yaml = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
        conn.execute(update(personas_t).where(personas_t.c.id == persona_id).values(yaml=new_yaml))


def get_persona(*, rls_engine: Engine, persona_id: str) -> dict[str, object]:
    """Return a persona row (RLS-scoped → 404 if not the caller's)."""
    with rls_engine.begin() as conn:
        row = (
            conn.execute(select(personas_t).where(personas_t.c.id == persona_id)).mappings().first()
        )
    if row is None:
        raise PersonaNotFoundError("persona not found", context={"id": persona_id})
    return dict(row)


def persona_display_name(*, rls_engine: Engine, persona_id: str) -> str | None:
    """Resolve a persona's display name from its id (RLS-scoped), or ``None`` if unknown.

    The K5 Memory panel's "learned by <persona>" attribution (R-K5-PROV-PERSONA): reads
    the caller's persona row and extracts ``identity.name`` from its YAML. Owner-scoped by
    the RLS engine, so another tenant's id resolves to ``None`` (never leaks a name). Kept
    fail-soft — a parse miss or absent persona degrades to the source-based fallback, never
    an error on a read path.
    """
    with rls_engine.begin() as conn:
        row = (
            conn.execute(select(personas_t.c.yaml).where(personas_t.c.id == persona_id))
            .mappings()
            .first()
        )
    if row is None:
        return None
    return persona_name_from_yaml(row["yaml"])


def list_personas(*, rls_engine: Engine, limit: int, offset: int) -> list[dict[str, object]]:
    """List the caller's personas (RLS-scoped), paginated."""
    with rls_engine.begin() as conn:
        rows = (
            conn.execute(
                select(personas_t)
                .order_by(personas_t.c.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def conversation_counts(*, rls_engine: Engine) -> dict[str, int]:
    """Conversations per persona for the caller (one RLS-scoped GROUP BY).

    Spec 35: feeds ``PersonaSummary.conversation_count`` for the whole library
    page in a single query — never per-persona. RLS scopes it to the owner.
    """
    with rls_engine.begin() as conn:
        rows = conn.execute(
            select(conversations_t.c.persona_id, func.count().label("n")).group_by(
                conversations_t.c.persona_id
            )
        ).all()
    return {str(persona_id): int(n) for persona_id, n in rows}


def conversation_count_for(*, rls_engine: Engine, persona_id: str) -> int:
    """Conversation count for one persona (RLS-scoped) — the episodic source."""
    with rls_engine.begin() as conn:
        n = conn.execute(
            select(func.count())
            .select_from(conversations_t)
            .where(conversations_t.c.persona_id == persona_id)
        ).scalar_one()
    return int(n)


def delete_persona(
    *, rls_engine: Engine, persona_id: str, workspace_root: Path | None = None, owner_id: str
) -> None:
    """Delete a persona (cascades conversations + memory via FK + workspace files).

    Spec 13 D-13-4 cascade-before-DB: rmtree the persona's workspace subtree
    BEFORE the DB DELETE fires, so a partial failure leaves orphan DB rows
    (recoverable by re-running the delete) rather than orphan files (silently
    leaking storage). Per D-13-4-v0.1-coarse-cascade, this is the only
    workspace cleanup point in v0.1 — per-conversation cleanup defers to the
    ``messages.images`` JSONB column migration.
    """
    if workspace_root is not None:
        persona_root = workspace_root / owner_id / persona_id
        if persona_root.exists():
            shutil.rmtree(persona_root, ignore_errors=True)
    with rls_engine.begin() as conn:
        result = conn.execute(
            delete(personas_t).where(personas_t.c.id == persona_id).returning(personas_t.c.id)
        )
        if result.first() is None:
            raise PersonaNotFoundError("persona not found", context={"id": persona_id})


def default_embedder(model_name: str) -> SentenceTransformerEmbedder:
    """The production embedder (bge-small-en-v1.5, 384-dim).

    Pinned to CPU (matching persona-voice's agent embedder): ``device="auto"``
    selects Apple MPS on a Mac, where a lazy/threaded device-move can raise
    "Cannot copy out of meta tensor" and the load is slower; bge-small encodes in
    <10ms on CPU, so CPU is both robust and fast enough for the create-time
    memory-population pass.
    """
    return SentenceTransformerEmbedder(model_name=model_name, device="cpu")


def summary_of(row: dict[str, object], *, conversation_count: int = 0) -> PersonaSummary:
    """Build a list-view summary from a persona row.

    name/role + the Spec-35 capability glance (language + tools/skills/
    constraints counts) all come from the SAME stored YAML this row already
    carries — no extra query. ``conversation_count`` is supplied by the caller
    (one GROUP-BY for the whole page, never per persona).
    """
    name, role = "", ""
    language = "en"
    tools_count = skills_count = constraints_count = 0
    try:
        parsed = yaml.safe_load(str(row["yaml"]))
        if isinstance(parsed, dict):
            identity = parsed.get("identity", {})
            identity = identity if isinstance(identity, dict) else {}
            name = str(identity.get("name", ""))
            role = str(identity.get("role", ""))
            language = _language_of(identity.get("language_default"))
            constraints_count = _len_of(identity.get("constraints"))
            tools_count = _len_of(parsed.get("tools"))
            skills_count = _len_of(parsed.get("skills"))
    except (yaml.YAMLError, AttributeError):
        pass  # a malformed stored YAML still lists, just without the extras
    avatar = row.get("avatar_url")
    return PersonaSummary(
        id=str(row["id"]),
        name=name,
        role=role,
        avatar_url=str(avatar) if avatar is not None else None,
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
        language=language,
        tools_count=tools_count,
        skills_count=skills_count,
        constraints_count=constraints_count,
        conversation_count=conversation_count,
    )


def _len_of(value: object) -> int:
    """Length of a YAML list field, or 0 when absent/malformed."""
    return len(value) if isinstance(value, list) else 0


def _language_of(value: object) -> str:
    """ISO-639-1 language code from the YAML ``language_default``.

    YAML's bool keywords bite here: an unquoted ``no`` (Norwegian's own ISO
    code!) parses to ``False`` and ``yes`` to ``True``. Map those back so a
    Norwegian persona reads as ``no``, not ``False``. Empty/None → ``en``.
    """
    if value is False:
        return "no"
    if value is True:
        return "yes"
    return str(value) if value else "en"
