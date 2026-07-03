"""Image-generation composition (spec 15 T15).

This module is the hosted-service composition root for image generation.
It glues together the four cost-discipline + safety primitives the
preceding tasks delivered:

* **Concurrency cap** (T14 :func:`persona_api.imagegen.concurrency.acquire_user_concurrency`)
  — Postgres advisory transactional lock keyed by ``hash(user_id)``;
  multi-worker-correct from day one (D-15-X-concurrency-cap).
* **Pre-deduct credits** (T13 :func:`persona_api.services.credits_service.deduct`)
  — credits acquired BEFORE the provider call so a parallel-fire
  denial-of-wallet attacker cannot burn N generations before the first
  deduction lands (D-15-X-pre-deduct-credits).
* **Backend dispatch** (T06 / T07 :class:`persona.imagegen.protocol.ImageBackend`)
  — provider-agnostic generation call. SDK exceptions are caught and
  re-raised as :mod:`persona.imagegen.errors` domain types at the adapter
  boundary; the service layer maps them through the refund-on-failure
  funnel.
* **Workspace persistence** (D-13-4 layout via
  :func:`persona.tools._sandbox.resolve_sandbox_path`) — bytes land at
  ``{workspace_root}/{owner_id}/{persona_id}/uploads/{blake2b}.{ext}``,
  the same layout :func:`persona_api.services.image_service.upload`
  produces so the existing ``GET /v1/personas/:id/uploads/:ref`` route
  serves generated images provenance-blindly (D-15-X-workspace-coordination).
* **Refund-on-failure** (T13 :func:`persona_api.services.credits_service.refund`)
  — reverse-deduct ledger entry on backend or post-gen moderation failure;
  pattern (a) per D-15-X-credit-flow-semantics (no Alembic migration
  required; ``credit_transactions.delta`` is ``Integer, nullable=False``
  with no ``CheckConstraint`` so positive deltas are physically allowed).

**Transactional shape.** The cap acquisition and the pre-deduct land in a
single ``rls_engine.begin()`` transaction so the cap is HELD while the
deduction is observed by Postgres; the backend ``await`` happens *inside*
that same context manager so the lock persists across the provider call
(the lock auto-releases on commit/rollback at the surrounding ``with``
exit). When the backend raises, we exit the original transaction (which
rolls back — releasing the lock) and then open a *separate*
``credits_service.refund`` transaction to issue the reverse-deduct
ledger entry. The two-transaction shape is intentional: the refund must
NOT share a transaction with the cap lock because rolling back the
original transaction is what releases the lock — collapsing the refund
into that transaction would either keep the lock during the refund
(wrong) or commit the deduct prematurely (wrong).

**Why credits_service.deduct/refund instead of inline SQL?** T13 owns
the ledger semantics; this module composes. Going through
:mod:`persona_api.services.credits_service` ensures the two ledger
writes (deduct + refund) are byte-for-byte symmetric with the rest of
the credits machinery — a future audit query summing ``delta`` by user
keeps reconstructing the running balance correctly.

References:
    docs/specs/phase2/spec_15/decisions.md gate paragraph #3 (cost
    discipline = pre-deduct + cap + refund); D-15-X-pre-deduct-credits,
    D-15-X-credit-flow-semantics, D-15-X-concurrency-cap,
    D-15-X-workspace-coordination, D-13-4 (workspace layout);
    docs/specs/phase2/spec_15/tasks.md §T15.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.imagegen import (
    ContentRejectedError,
    GeneratedImage,
    GenerationResult,
    ImageGenError,
    ImageGenOptions,
    hash_prompt_for_audit,
    is_hard_line_violation,
)
from persona.imagegen._merge import merge_visual_style
from persona.logging import get_logger
from persona.tools.audit import ToolAuditEvent

from persona_api.editions import MeteredCreditsPolicy
from persona_api.errors import ConcurrencyCappedError
from persona_api.imagegen.concurrency import acquire_user_concurrency
from persona_api.services.artifact_storage import artifact_key

if TYPE_CHECKING:
    from persona.imagegen.protocol import ImageBackend
    from persona.imagegen.result import ImageMediaType
    from persona.tools.audit import ToolAuditLogger
    from sqlalchemy import Engine

    from persona_api.editions import CreditsPolicy
    from persona_api.storage import FileStorage

__all__ = ["DEFAULT_COST_PER_IMAGE_CREDITS", "generate", "generate_avatar"]


#: The avatar tool-name recorded on the audit event. Distinct from
#: ``generate_image`` so an operator can separate build-time auto-avatar
#: events from model-/operator-requested image generation.
_AVATAR_TOOL_NAME: str = "generate_avatar"

#: Fixed avatar generation preset (D-29-3). A single square portrait —
#: ``count=1`` (one face per persona), ``1024x1024`` (square avatar), and
#: the ``standard`` quality preset. Deterministic + minimal (the avatar is
#: a stable function of the persona; multi-image variation is out of scope).
_AVATAR_OPTIONS: ImageGenOptions = ImageGenOptions(size="1024x1024", count=1, quality="standard")


_LOG = get_logger("imagegen.service")


#: Default per-image credit cost (D-15-3 cost containment). 100 credits per
#: image at ``count <= 4`` caps a single tool invocation at 400 credits
#: total — still a small fraction of the 100_000-credit default balance,
#: leaving headroom for chat turns while keeping image generation visibly
#: more expensive than per-token chat usage (research §1.1: OpenAI
#: gpt-image-1 medium is ~$0.042/image, so 100 credits ≈ ~4 cents in the
#: hypothetical 1¢-per-1000-credits accounting).
DEFAULT_COST_PER_IMAGE_CREDITS: int = 100


#: Mapping from supported IANA media types to filename extensions used in
#: the workspace layout. Mirrors :data:`persona_api.services.image_service._EXT_BY_MEDIA_TYPE`
#: (intentionally redeclared here so the imagegen service does not import a
#: private symbol from the upload service — both modules write to the same
#: ``uploads/`` directory but neither is the other's authority on what an
#: extension means; the value is the same in both modules and any drift
#: would surface as a route-layer 404 because the serve path maps ext back
#: to media type).
_EXT_BY_MEDIA_TYPE: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


#: Workspace sub-directory holding a persona's image assets (D-13-4 +
#: D-15-X-workspace-coordination). Generated images and user uploads share
#: this directory provenance-blindly; provenance lives in the audit log
#: (``ToolAuditEvent.metadata["outcome"]`` ∈
#: ``{ok, content_rejected_provider, content_rejected_hard_line, error}``)
#: and the per-turn observability surface (``turn_logs.metadata`` with
#: ``kind=image_generation`` per D-15-X-observability-shape), NOT the path.
_UPLOAD_DIR_NAME: str = "uploads"


async def generate(
    *,
    rls_engine: Engine,
    credits_policy: CreditsPolicy | None = None,
    file_storage: FileStorage,
    backend: ImageBackend,
    user_id: str,
    persona_id: str,
    persona_visual_style: str | None,
    prompt: str,
    options: ImageGenOptions,
    cost_per_image_credits: int = DEFAULT_COST_PER_IMAGE_CREDITS,
) -> GenerationResult:
    """Run one full image-generation flow: cap → deduct → backend → persist.

    On the happy path the returned :class:`GenerationResult` carries
    persisted images: each :attr:`persona.imagegen.result.GeneratedImage.workspace_path`
    is populated with the workspace-relative path
    (``uploads/<blake2b><ext>``) and :attr:`image_bytes` is zeroed so the
    response envelope does not double-carry the payload. The credits
    ledger shows one ``-count*cost_per_image_credits`` entry; the audit
    log will carry the persona-layer
    :class:`persona.tools.audit.ToolAuditEvent` emitted by the
    ``generate_image`` tool factory (T12 owns that emission — the
    service layer composes the cap + deduct + persistence around the
    backend call but the audit is the tool's; the route layer's
    ``audit_service.record`` adds the HTTP-layer event).

    On a backend failure (:class:`ContentRejectedError` from provider
    moderation or :class:`ImageGenError` family for auth / rate / etc.)
    the credits are refunded via :func:`credits_service.refund` in a
    fresh transaction (the original transaction rolled back, releasing
    the cap lock) and the original exception is re-raised so the route
    layer can map it to the appropriate HTTP status.

    On a concurrency-capped path
    (:class:`ConcurrencyCappedError`) NO credits are deducted — the
    cap fires before the deduct in the same transaction so the rollback
    leaves the balance untouched.

    Args:
        rls_engine: The per-request RLS-scoped SQLAlchemy
            :class:`~sqlalchemy.Engine`. Both the cap+deduct transaction
            and the refund-on-failure transaction open through this
            engine so the per-request user_id RLS scope is preserved.
        workspace_root: Per-deployment workspace root (typically
            ``settings.persona_workspace_root``). Generated bytes land
            at ``workspace_root / user_id / persona_id / uploads / <blake2b><ext>``.
        backend: A concrete
            :class:`persona.imagegen.protocol.ImageBackend` implementation
            (OpenAI gpt-image-1 via
            :class:`persona.imagegen.openai_image.OpenAIImageBackend` or
            Flux 1.1 [pro] via
            :class:`persona.imagegen.fal_image.FalImageBackend` for v0.1;
            composition root selects via
            :func:`persona.imagegen.load_image_backend`).
        user_id: Authenticated tenant identifier — feeds the advisory-
            lock key (hashed via md5 in the concurrency helper) AND the
            workspace path's ``owner_id`` segment AND the credits ledger
            ``user_id`` column.
        persona_id: Persona owning this generation. Workspace path
            segment + audit payload.
        persona_visual_style: The persona's
            :attr:`persona.schema.persona.PersonaIdentity.visual_style`
            (T10). Merged into the prompt via :func:`merge_visual_style`
            per D-15-4; ``None`` / empty / whitespace-only yields the
            identity branch (no merge).
        prompt: The user / runtime prompt as supplied. The visual-style
            merge runs at the service layer (here) so the backend sees
            the merged prompt; this is structurally important because a
            misconfigured tool wrapper would otherwise let the model
            bypass the merge by calling the backend directly.
        options: Closed-preset image-generation knobs
            (:class:`persona.imagegen.result.ImageGenOptions`). The
            ``count <= 4`` cap (D-15-3) is already enforced by the
            Pydantic field; this service trusts that.
        cost_per_image_credits: Credits per image. Defaults to
            :data:`DEFAULT_COST_PER_IMAGE_CREDITS` (100). Override is
            available for future per-quality pricing if D-15-3 gains
            tiers — v0.1 ships flat.

    Returns:
        :class:`GenerationResult` with images whose ``workspace_path``
        is populated and ``image_bytes`` is zeroed.

    Raises:
        ConcurrencyCappedError: The per-user cap is already held by a
            concurrent in-flight generation for this ``user_id`` — the
            route layer surfaces 429 + ``Retry-After``. No credits are
            touched.
        ContentRejectedError: Provider moderation refused the prompt
            (input stage) or flagged the generated bytes (output stage,
            D-15-X-flagged-image-policy). Credits are refunded before
            the exception propagates.
        ImageGenError: Other backend failure (auth, rate limit,
            transient, timeout, unsupported option). Credits are refunded
            before the exception propagates.
    """
    # Spec 33 (D-33-X-creditspolicy-di): production passes the edition's policy;
    # default to the metered policy so a direct call keeps today's behavior.
    credits_policy = credits_policy or MeteredCreditsPolicy()
    total_cost = options.count * cost_per_image_credits
    merged_prompt = merge_visual_style(prompt, persona_visual_style)

    # Phase 1: cap + pre-deduct + backend call inside one transaction.
    # The advisory lock is held by the transaction; the await suspends
    # the coroutine but the underlying connection's transaction stays
    # open (sync SQLAlchemy on psycopg3 — the connection is a Python
    # object holding a server-side transaction; nothing about ``await``
    # closes it). On either branch the ``with`` exits, releasing the
    # lock — commit on success, rollback on exception.
    deduct_succeeded = False
    try:
        with (
            rls_engine.begin() as conn,
            acquire_user_concurrency(conn=conn, user_id=user_id) as acquired,
        ):
            if not acquired:
                # Cap held — raise BEFORE pre-deduct so the rollback
                # leaves credits untouched.
                raise ConcurrencyCappedError(
                    "image generation already in flight for this user",
                    context={"user_id": user_id, "retry_after_s": "5"},
                )

            # Pre-deduct credits inside the same transaction. The
            # deduct opens its own ``rls_engine.begin()`` internally;
            # that nested transaction commits immediately because
            # ``credits_service.deduct`` uses a fresh connection from
            # the pool so the deduct lands in its own transaction. The
            # TWO transactions share the same engine + the same RLS
            # scope; if the backend call later raises, we manually
            # refund (the deduct's transaction already committed).
            credits_policy.deduct(
                rls_engine=rls_engine,
                user_id=user_id,
                amount=total_cost,
                reason="image_gen_pre",
            )
            deduct_succeeded = True

            # Backend dispatch happens inside the cap context so the
            # lock holds for the full provider latency (else parallel-
            # fire bypasses the cap by deducting concurrently while
            # the first generation is in flight). The deduct above
            # already committed on its own connection, so even if the
            # backend raises the deduct is observable; we refund
            # explicitly in the failure branch.
            result = await backend.generate(merged_prompt, options=options)
    except ConcurrencyCappedError:
        # Cap-capped path: no deduct happened (the raise is BEFORE the
        # deduct call) — re-raise unchanged. The ``with`` already exited
        # and the lock auto-released on rollback.
        raise
    except (ContentRejectedError, ImageGenError):
        # Backend-failure path: the deduct committed (own transaction),
        # the cap lock auto-released on rollback. Refund the credits in
        # a fresh transaction so the ledger captures both legs of the
        # round trip.
        if deduct_succeeded:
            credits_policy.refund(
                rls_engine=rls_engine,
                user_id=user_id,
                amount=total_cost,
                reason="image_gen_refund:backend_failure",
            )
        raise

    # Phase 2: persist bytes to the workspace (D-13-4 layout) and
    # rewrite the result so ``workspace_path`` is populated and
    # ``image_bytes`` is zeroed for the response envelope.
    # R5-D-4: persistence now routes through file_storage (the backend owns the
    # path resolution + dir creation); no local sandbox_root needed here.

    stored_images: list[GeneratedImage] = []
    for img in result.images:
        relative = _persist_bytes(
            file_storage=file_storage,
            owner_id=user_id,
            persona_id=persona_id,
            image_bytes=img.image_bytes,
            media_type=img.media_type,
            conversation_id=None,  # Spec 15 generate doesn't thread conv_id at v0.1
        )
        stored_images.append(
            img.model_copy(
                update={
                    "workspace_path": relative,
                    "image_bytes": b"",
                }
            )
        )

    _LOG.info(
        "image generation completed",
        user_id=user_id,
        persona_id=persona_id,
        provider=result.provider,
        model=result.model,
        image_count=len(stored_images),
        latency_ms=result.latency_ms,
    )

    return result.model_copy(update={"images": stored_images})


def _persist_bytes(
    *,
    file_storage: FileStorage,
    owner_id: str,
    persona_id: str,
    image_bytes: bytes,
    media_type: ImageMediaType,
    conversation_id: str | None = None,
) -> str:
    """Write image bytes to the persona workspace; return the workspace-relative path.

    Layout per D-13-4 (reused unchanged via D-15-X-workspace-coordination):
    ``{sandbox_root}/uploads/{blake2b(bytes)}{ext}``. The blake2b digest
    is content-addressed so two generations producing the same bytes
    collapse to one file (idempotent — same property the upload path
    relies on). Write goes through ``resolve_sandbox_path`` (the Spec 03
    sandbox resolver) plus ``O_NOFOLLOW`` to close the TOCTOU window
    between resolution and open.

    Args:
        sandbox_root: ``{workspace_root}/{user_id}/{persona_id}``.
        image_bytes: Raw decoded image bytes from the backend.
        media_type: IANA media type of the bytes (drives extension).

    Returns:
        Workspace-relative path (``uploads/<blake2b><ext>``) suitable for
        :attr:`GeneratedImage.workspace_path`.
    """
    ext = _EXT_BY_MEDIA_TYPE[media_type]
    ref = hashlib.blake2b(image_bytes, digest_size=16).hexdigest()
    relative = f"{_UPLOAD_DIR_NAME}/{ref}{ext}"

    # R5-D-4: write through the FileStorage backend (LocalFileStorage keeps the
    # exact resolve_sandbox_path + O_NOFOLLOW byte-identical write; S3 uploads the
    # object). The content-addressed key stays idempotent-overwrite (never O_EXCL).
    key = artifact_key(owner_id, persona_id, relative)
    file_storage.put(key, image_bytes, content_type=media_type)

    # F5 T05 — D-F5-X-artifact-metadata-convention: write the sidecar so the
    # F5 artifact-list endpoint can filter generated images. Best-effort —
    # failure logs but does not abort the generation (the bytes are the
    # primary deliverable; metadata is enrichment).
    from persona_api.services.artifact_metadata import WorkspaceArtifactMetadata, utcnow
    from persona_api.services.artifact_storage import write_sidecar

    # Sidecar routes through the SAME backend as a sibling object (R5-D-4), so
    # listing works on S3. ``write_sidecar`` is best-effort (suppresses failures).
    write_sidecar(
        file_storage,
        key,
        WorkspaceArtifactMetadata(
            source="generated",
            type="image",
            producing_spec="15",
            conversation_id=conversation_id,
            created_at=utcnow(),
            original_name=None,
        ),
    )

    return relative


async def generate_avatar(
    *,
    file_storage: FileStorage,
    backend: ImageBackend,
    user_id: str,
    persona_id: str,
    prompt: str,
    audit_logger: ToolAuditLogger | None = None,
) -> GenerationResult:
    """Generate + persist a persona avatar at build time (Spec 29, D-29-2/3).

    The build-time sibling of :func:`generate`. It composes the SAME backend
    + persistence + hard-line filter primitives but is **free** — no
    ``credits_service.deduct``/``refund`` and **no** per-user concurrency
    advisory lock (D-29-2): auto-avatar-gen is system-initiated at create,
    not a user-requested generation, so neither the user's ledger nor their
    image-gen concurrency state moves. Each call emits exactly one JSONL
    :class:`~persona.tools.audit.ToolAuditEvent` (no migration) tagged
    ``system_initiated=true`` / ``credits_charged=0`` so an operator can
    count auto-gen outcomes without touching the ledger.

    Unlike :func:`generate`, the prompt is **already crafted** by
    :func:`persona.imagegen.craft_avatar_prompt` (which has merged
    ``visual_style`` per D-29-1) — so this entry does NOT re-merge a visual
    style. It DOES run the hard-line categorical filter explicitly: the
    operator/service path does not otherwise run it (only the runtime tool
    factory does), so this is the D-29-1 defense-in-depth backstop that
    catches an adversarial *declared* ``visual_style`` reflected verbatim
    into the prompt (the crafter is clean by construction; the filter is the
    backstop for whatever a user declares).

    The function **raises** the imagegen domain exceptions on failure
    (``ContentRejectedError`` for the hard-line trigger or provider
    moderation; ``ImageGenError`` family for auth/rate/transient/timeout) —
    the build-time hook (Spec 29 T3) catches the full surface and fail-softs
    to ``avatar_url=null`` so persona-create never fails (D-29-X-fail-soft).
    The ``backend is None`` and wall-clock-timeout cases are the hook's to
    handle (the hook guards ``app.state.image_backend`` and wraps this call
    in ``asyncio.wait_for``); this entry requires a live backend.

    Args:
        workspace_root: Per-deployment workspace root. Bytes land at
            ``workspace_root / user_id / persona_id / uploads / <blake2b><ext>``,
            served by the existing uploads route (provenance-blind).
        backend: A live :class:`~persona.imagegen.protocol.ImageBackend`.
        user_id: Authenticated owner — workspace path segment + audit.
        persona_id: The persona being built — workspace path + audit.
        prompt: The already-crafted avatar prompt (incl. the ``visual_style``
            merge). Passed to the backend verbatim; NOT re-merged.
        audit_logger: Optional JSONL tool-audit sink. When provided, one
            ``ToolAuditEvent`` is emitted per call recording the outcome.

    Returns:
        A :class:`GenerationResult` whose single image has
        ``workspace_path`` populated and ``image_bytes`` zeroed.

    Raises:
        ContentRejectedError: The hard-line filter tripped (prompt never
            persisted — hash only) OR provider moderation refused the
            prompt/bytes.
        ImageGenError: Other backend failure (auth/rate/transient/timeout/
            unsupported option).
    """
    options = _AVATAR_OPTIONS

    # 1. Hard-line categorical filter — the D-29-1 defense-in-depth backstop.
    #    Runs BEFORE the provider call; on trigger the provider is never
    #    consulted, the prompt is NEVER persisted (hash only), and we raise
    #    so the hook fail-softs to null.
    triggered, category = is_hard_line_violation(prompt)
    if triggered:
        prompt_sha256 = hash_prompt_for_audit(prompt)
        _emit_avatar_audit(
            audit_logger=audit_logger,
            persona_id=persona_id,
            provider=backend.provider_name,
            model=backend.model_name,
            outcome="content_rejected_hard_line",
            is_error=True,
            resource=f"sha256:{prompt_sha256}",
            extra={"category": category or "", "prompt_sha256": prompt_sha256},
        )
        _LOG.warning(
            "generate_avatar hard-line trigger; provider not called",
            persona_id=persona_id,
            category=category or "",
            prompt_sha256=prompt_sha256,
        )
        raise ContentRejectedError(
            "avatar prompt rejected by hard-line filter",
            context={
                "reason": "hard_line",
                "category": category or "",
                "prompt_sha256": prompt_sha256,
            },
        )

    # 2. Backend dispatch — free (no pre-deduct) + cap-free (no advisory
    #    lock). Provider exceptions are audited then re-raised for the hook.
    try:
        result = await backend.generate(prompt, options=options)
    except ContentRejectedError as exc:
        _emit_avatar_audit(
            audit_logger=audit_logger,
            persona_id=persona_id,
            provider=backend.provider_name,
            model=backend.model_name,
            outcome="content_rejected_provider",
            is_error=True,
            resource=backend.provider_name,
            extra={
                "stage": exc.context.get("stage", ""),
                "reason": exc.context.get("reason", "provider_moderation"),
            },
        )
        raise
    except ImageGenError as exc:
        error_type = type(exc).__name__
        _emit_avatar_audit(
            audit_logger=audit_logger,
            persona_id=persona_id,
            provider=backend.provider_name,
            model=backend.model_name,
            outcome="error",
            is_error=True,
            resource=backend.provider_name,
            extra={"error_type": error_type, "reason": exc.context.get("reason", error_type)},
        )
        raise

    # 3. Persist bytes to the workspace (D-13-4 layout) — same content-
    #    addressed write as ``generate``; the avatar is one square image.
    # R5-D-4: persistence now routes through file_storage (the backend owns the
    # path resolution + dir creation); no local sandbox_root needed here.
    stored_images: list[GeneratedImage] = []
    for img in result.images:
        relative = _persist_bytes(
            file_storage=file_storage,
            owner_id=user_id,
            persona_id=persona_id,
            image_bytes=img.image_bytes,
            media_type=img.media_type,
            conversation_id=None,
        )
        stored_images.append(
            img.model_copy(update={"workspace_path": relative, "image_bytes": b""})
        )

    # 4. Success — one zero-cost system audit event.
    _emit_avatar_audit(
        audit_logger=audit_logger,
        persona_id=persona_id,
        provider=result.provider,
        model=result.model,
        outcome="ok",
        is_error=False,
        resource=result.provider,
        extra={"image_count": str(len(stored_images)), "latency_ms": f"{result.latency_ms:.1f}"},
    )
    _LOG.info(
        "avatar generation completed",
        user_id=user_id,
        persona_id=persona_id,
        provider=result.provider,
        model=result.model,
    )
    return result.model_copy(update={"images": stored_images})


def _emit_avatar_audit(
    *,
    audit_logger: ToolAuditLogger | None,
    persona_id: str,
    provider: str,
    model: str,
    outcome: str,
    is_error: bool,
    resource: str,
    extra: dict[str, str] | None = None,
) -> None:
    """Emit one avatar-gen :class:`ToolAuditEvent` (JSONL, no migration).

    Tagged ``system_initiated=true`` / ``credits_charged=0`` so the free,
    system-initiated nature of build-time avatar generation (D-29-2) is
    visible in the audit trail. ``outcome`` mirrors the ``generate_image``
    tool-factory vocabulary (``ok`` / ``content_rejected_hard_line`` /
    ``content_rejected_provider`` / ``error``). No-ops when no logger is
    wired (CLI / tests that don't assert on audit).
    """
    if audit_logger is None:
        return
    metadata: dict[str, str] = {
        "outcome": outcome,
        "provider": provider,
        "model": model,
        "system_initiated": "true",
        "credits_charged": "0",
    }
    if extra:
        metadata.update(extra)
    audit_logger.emit(
        ToolAuditEvent(
            timestamp=datetime.now(UTC),
            persona_id=persona_id,
            tool_name=_AVATAR_TOOL_NAME,
            action="execute",
            resource=resource,
            is_error=is_error,
            metadata=metadata,
        )
    )
