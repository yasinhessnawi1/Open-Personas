"""Domain exceptions for the backend layer.

Every error raised by a :class:`persona.backends.protocol.ChatBackend`
implementation is a subclass of :class:`ProviderError`, which in turn is a
subclass of :class:`persona.errors.PersonaError`. Provider-specific exceptions
from third-party SDKs (``anthropic``, ``openai``, ``httpx``) are caught at the
adapter boundary and re-raised through this hierarchy so callers depend on
our types rather than on a transitive dependency.

See ``docs/specs/spec_02/decisions.md`` D-02-1 for the
``BackendTimeoutError`` rationale and D-02-8 for ``retry_after_s`` semantics.

Error class hierarchy partition (Spec 20 D-20-16 — settled)
-----------------------------------------------------------

This module ships three distinct error-class families. The partition matters
for callers writing ``except`` clauses — ``except ProviderError`` catches
HTTP/SDK failures but does NOT catch wrapper/config failures; that is
intentional, since wrapper/config errors should fail-loud at the application
layer rather than be swept into generic retry handlers.

1. **Provider-layer errors** root at :class:`ProviderError(PersonaError)`.
   Backends raise these for HTTP/SDK failures: :class:`AuthenticationError`,
   :class:`RateLimitError`, :class:`ModelNotFoundError`,
   :class:`ModelUnavailableError`, :class:`BackendTimeoutError`. The
   :class:`MultiModelChatBackend` classifier (Spec 20 T15) buckets these per
   D-20-9 into SURFACE / RETRY-THEN-FALLBACK / FALLBACK-NO-RETRY.

2. **Wrapper-layer + configuration-layer errors** root at
   :class:`PersonaError` directly (NOT :class:`ProviderError`):
   :class:`AllModelsFailedError` (wrapper-layer, Spec 20 T15/T16),
   :class:`ProviderCredentialMissingError` (config-layer, D-20-15),
   :class:`LocalProviderInModelsListError` (config-layer, D-20-18),
   :class:`MalformedTierModelsError` (config-layer, D-20-17),
   :class:`IncompleteTierConfigError` (config-layer, D-20-17),
   :class:`TierNotConfiguredError` (config-layer, D-20-15 ALL-fail branch).
   These represent failures of the composition layer
   (:class:`persona.backends.credentials.ProviderCredentialResolver`,
   :class:`persona.backends.tier_registry.TierRegistry`,
   :class:`persona.backends.multi_model_chat.MultiModelChatBackend`)
   where the failure is not provider-side.

3. **Router-vision errors** root at
   :class:`RoutingConstraintsUnsatisfiableError(PersonaError)` (Spec 18
   generalisation): :class:`NoVisionTierConfiguredError`, plus
   :class:`BackendVisionNotSupportedError(PersonaError)` sibling for
   backend-dispatch failures.

The partition is cemented by parametrized contract tests in
``packages/core/tests/unit/backends/test_errors_hierarchy.py`` — any
future amendment that reparents a wrapper/config error to
:class:`ProviderError` will trip those tests immediately.
"""

from __future__ import annotations

from persona.errors import PersonaError

__all__ = [
    "AllModelsFailedError",
    "AuthenticationError",
    "BackendTimeoutError",
    "BackendVisionNotSupportedError",
    "BudgetExceededError",
    "DegenerateCompletionError",
    "EmptyCompletionError",
    "IncompleteTierConfigError",
    "IntelligentRoutingError",
    "LocalProviderInModelsListError",
    "MalformedTierModelsError",
    "ModelNotFoundError",
    "ModelUnavailableError",
    "NoVisionCapableModelError",
    "NoVisionTierConfiguredError",
    "OpenRouterBalanceProbeError",
    "OpenRouterCatalogError",
    "ProviderCredentialMissingError",
    "ProviderError",
    "RateLimitError",
    "RoutingConstraintsUnsatisfiableError",
    "TierNotConfiguredError",
]


class ProviderError(PersonaError):
    """Base for every backend-raised error.

    Non-retryable by default. Subclasses signal specific retry semantics
    (``RateLimitError`` carries an optional ``retry_after_s`` in ``context``;
    ``BackendTimeoutError`` is the canonical retry target).

    Implementations should always populate ``context`` with at least
    ``provider`` and (when known) ``model`` so log messages are structured.
    """


class AuthenticationError(ProviderError):
    """Raised when an API key is missing, invalid, or rejected by the provider.

    Backends raise this at construction time when the configured key is
    missing or empty (fail fast — see spec §10 #8), and at call time when
    the provider returns 401 / 403.
    """


class RateLimitError(ProviderError):
    """Raised when the provider returns 429 (or equivalent).

    When the provider supplies a ``retry-after`` header, it is recorded in
    ``context["retry_after_s"]`` as a string of integer seconds. The header
    is the only source — we never invent a default (D-02-8).
    """


class ModelNotFoundError(ProviderError):
    """Raised when the configured model name is unknown to the provider.

    Maps Anthropic / OpenAI ``NotFoundError`` (model variant) and Ollama's
    ``404 {"error": "model 'xxx' not found"}`` response.
    """


class ModelUnavailableError(ProviderError):
    """Raised when a provider refuses to serve the configured model at all (R9-073a).

    Maps a 403 ``PermissionDeniedError`` from the ``anthropic`` / ``openai``
    SDKs (both used across every ``OpenAICompatibleBackend`` provider,
    including Cloudflare) — the account-level "this model requires a
    different plan / entitlement" rejection, e.g. Cloudflare Workers AI's
    ``AiError 5035: Model ... is not available on the Workers Free plan``.

    Distinct from :class:`AuthenticationError` (401 — the key itself is
    missing/invalid) and from :class:`ModelNotFoundError` (404 — the model
    name is unknown to the provider): here the key is valid and the model
    exists, but THIS account may never call THIS model. Retrying the same
    model is always pointless (no amount of retrying upgrades the plan), but
    an operator who configured multiple models in a tier's fallback list
    almost certainly did so anticipating exactly this — a bad model choice
    should not take the whole tier down. :class:`MultiModelChatBackend`
    (Spec 20 D-20-9) therefore buckets this FALLBACK-NO-RETRY, same as
    :class:`AuthenticationError` / :class:`ModelNotFoundError`, logging the
    fallback at WARNING so the misconfiguration stays visible to operators.

    Context: ``{"provider", "model"}`` (the standard :class:`ProviderError`
    fields).
    """


class BackendTimeoutError(ProviderError):
    """Raised when an HTTP request to the provider times out.

    Maps ``httpx.TimeoutException``, ``anthropic.APITimeoutError``, and
    ``openai.APITimeoutError``. Distinct from :class:`ProviderError` because
    timeouts are the most common transient failure callers retry on
    (D-02-1).
    """


class EmptyCompletionError(ProviderError):
    """Raised when a backend's completion carried no reply at all (R9-033).

    An "empty completion" is a response with **no non-whitespace text AND no
    tool calls** — a provider flake (observed live 2026-07-13: a frontier
    stream ended after zero content chunks). Nothing-at-all is never a valid
    reply, so the condition is a provider failure: raising it lets
    :class:`persona.backends.multi_model.MultiModelChatBackend` engage the
    same retry-then-fallback walk any transient ``ProviderError`` triggers,
    and lets the runtime loop fail a bare-backend turn loudly instead of
    persisting a silent empty assistant message.

    ``context`` carries ``provider`` and ``model`` (the standard
    :class:`ProviderError` fields) so fallback logs stay structured.
    """


class DegenerateCompletionError(ProviderError):
    """Raised when a completion collapsed into a repetition loop (R9-090).

    A sibling of :class:`EmptyCompletionError`, and for the same reason: a reply
    that is not a reply is a provider failure, not content. Where "empty" is
    nothing at all, this is the opposite failure with the same worthlessness --
    the model cycling until it hits the output cap. Observed live 2026-08-01: a
    free-tier model returned roughly 4000 tokens of salad in the persona's own
    voice, which reads to the user as the product being broken rather than busy.

    Raising it engages the same retry-then-fallback walk any transient
    :class:`ProviderError` triggers, so the turn is answered by the next model in
    the chain instead of delivering the garbage.

    ``context`` carries ``provider`` and ``model`` (the standard
    :class:`ProviderError` fields) plus ``words`` / ``distinct_words``, so a log
    line shows how badly the completion collapsed without quoting the text back.
    """


class OpenRouterCatalogError(ProviderError):
    """Raised when the OpenRouter model catalog cannot be fetched or parsed.

    Spec 22 D-22-1 + D-20-16 partition: a **provider-layer** error (slots
    under :class:`ProviderError` — it describes a live HTTP call to
    ``GET /api/v1/models``, not a wrapper/config gap). Fired by
    :class:`persona.backends.openrouter_catalog.OpenRouterCatalogClient`
    when the catalog request fails (network / 5xx / unparseable body).

    Per D-22-1 the catalog fetch **fails open**: the resolver catches this
    at construction, logs a WARN, and falls back to tier-3 underlying-model
    capability inference rather than taking the backend down. The
    distinct class lets that fail-open handler catch catalog failures
    without also swallowing balance-probe failures.

    Context: ``{"provider": "openrouter", "reason", ...}`` where ``reason``
    is one of ``http_error`` / ``timeout`` / ``malformed_response``.
    """


class OpenRouterBalanceProbeError(ProviderError):
    """Raised when the OpenRouter subscription-mode probe fails non-fatally.

    Spec 22 D-22-3 + D-20-16 partition: a **provider-layer** error (live
    HTTP call to ``GET /api/v1/key``). Fired by
    :class:`persona.backends.openrouter_catalog.OpenRouterCatalogClient`
    on timeout / 5xx of the probe — per D-22-3 the resolver treats this as
    a conservative fall-back to **free-mode** (cannot confirm paid credits),
    WARNs, and continues.

    A 401 on the probe is NOT this class — an invalid key is a fail-loud
    :class:`AuthenticationError` (D-22-9), distinct from a transient probe
    failure that degrades to free-mode.

    Context: ``{"provider": "openrouter", "reason", ...}`` where ``reason``
    is one of ``http_error`` / ``timeout`` / ``malformed_response``.
    """


# Spec 13 vision errors (D-13-X-error-hierarchy) + Spec 18 generalisation
# (D-18-X-constraint-failure-shape). Spec 13 originally placed both classes
# directly under PersonaError per D-03-1's flat-hierarchy rule. Spec 18
# generalises NoVisionTierConfiguredError to land below
# RoutingConstraintsUnsatisfiableError — a true second class (the third would
# be a context-window or tool-strength constraint failure) that now justifies
# the intermediate parent. BackendVisionNotSupportedError stays a sibling
# under PersonaError: it is a backend-dispatch failure (specific backend
# cannot accept the request), not a router-side configuration failure.


class BackendVisionNotSupportedError(PersonaError):
    """Raised when a vision-incapable backend is asked to serialise an image.

    Fired by the backend message serialisers (Spec 13 T05/T06/T07) when
    a :class:`persona.schema.content.ImageContent` block is present and
    either the backend's ``supports_vision`` property is ``False`` or
    no ``workspace_root`` was configured (so the image bytes cannot be
    resolved).

    The structured ``context`` carries:

    * ``backend`` — the provider/runtime name (e.g. ``"anthropic"``).
    * ``model`` — the configured model name.
    * ``image_count`` — string-formatted count of ImageContent blocks
      in the offending message.
    * ``reason`` — present and set to ``"missing_workspace_root"`` for
      the no-workspace variant; absent for the supports_vision=False
      variant.

    The runtime layer (T11/T12) consumes this on the way back up and
    re-dispatches to the configured vision tier, so the structured
    context is the API contract.
    """


class NoVisionCapableModelError(PersonaError):
    """Raised when an image-bearing request hits a tier with no vision model.

    Wrapper-layer failure (slots under :class:`PersonaError`, NOT
    :class:`ProviderError` — same D-20-16 partition as
    :class:`AllModelsFailedError`). Fired by
    :class:`persona.backends.multi_model.MultiModelChatBackend` when the
    request messages carry at least one
    :class:`persona.schema.content.ImageContent` block but NONE of the
    composed candidate backends report ``supports_vision``.

    This is the honest-failure branch of the image-workspace cascade: rather
    than silently dispatch the turn text-only (dropping the user's image), the
    wrapper fails loud so the runtime loop can surface a clear "no
    vision-capable model is configured" notice to the user. Distinct from
    :class:`RoutingConstraintsUnsatisfiableError` /
    :class:`NoVisionTierConfiguredError`, which are router-side (Layer 1) tier
    configuration failures; this is the in-tier candidate-walk failure.

    The structured ``context`` carries:

    * ``tier`` — the configured tier label (may be empty).
    * ``image_count`` — string-formatted count of ImageContent blocks in the
      offending request.
    * ``candidate_count`` — number of composed candidate backends, none of
      which were vision-capable.
    """


class RoutingConstraintsUnsatisfiableError(PersonaError):
    """Raised when Layer 1's hard filter empties the candidate set (Spec 18).

    Generalises the Spec 13 ``NoVisionTierConfiguredError`` pattern: when
    a turn carries hard requirements (vision / context window / strong
    tool-calling) and no configured tier satisfies them, the router fails
    loud rather than silently picking an incapable model
    (D-18-X-constraint-failure-shape).

    The structured ``context`` carries:

    * ``reason`` — short token identifying which constraint emptied the
      set (e.g., ``"no_vision_tier"``, ``"context_window_exceeded"``,
      ``"no_strong_tools_tier"``).
    * ``configured_tiers`` — comma-joined list of configured tier names.
    * ``required`` — short token describing the unmet requirement
      (e.g., ``"vision"``, ``"context_window>=64000"``, ``"strong_tools"``).
      Present on new raise sites (T09); absent for back-compat raises
      via :class:`NoVisionTierConfiguredError` (the existing Spec 13
      raise site at ``router.py:202`` keeps its two-field context shape).

    Catching this class catches **every** Layer 1 failure mode; catching
    :class:`NoVisionTierConfiguredError` continues to catch only the
    vision-specific case (subclass relationship — the existing
    :class:`isinstance` checks at ``test_router_vision.py:200-203`` and any
    downstream callers keep working).
    """


class ProviderCredentialMissingError(PersonaError):
    """Raised when a provider in a MODELS list has no API key configured.

    Spec 20 D-20-15 + D-20-16: wrapper/configuration-layer error (slots under
    :class:`PersonaError`, NOT under :class:`ProviderError` — provider-layer
    errors describe a live API call, this describes a configuration gap).

    The ``ProviderCredentialResolver`` raises this when an API-keyed provider's
    ``PERSONA_<PROVIDER>_API_KEY`` env var is absent. ``TierRegistry`` catches
    per-slot at construction; if at least one provider in the tier's MODELS
    list resolves, it WARNs and skips this slot. If every slot fails, the
    registry re-raises as :class:`TierNotConfiguredError`.

    Context: ``{"provider", "env_var"}``.
    """


class LocalProviderInModelsListError(PersonaError):
    """Raised when ``local`` or ``ollama`` appears in ``PERSONA_<TIER>_MODELS``.

    Spec 20 D-20-18 + D-20-16: wrapper/configuration-layer error. Three
    converging justifications drive the EXPLICIT REJECT: (1) the Provider
    Literal vs ``DEFAULT_BASE_URLS`` asymmetry — ``local`` has no HTTP
    transport; (2) GPU-memory exclusivity makes cross-provider fallback
    semantics unsound for in-process weights; (3) no operator demand.

    The ``hint`` context key routes operators to the correct single-backend
    fast path (``PERSONA_LOCAL_MODEL_ID`` for ``local``, the per-tier
    ``PERSONA_<TIER>_PROVIDER=ollama`` triplet for ``ollama``).

    Context: ``{"tier", "position", "hint"}``.
    """


class MalformedTierModelsError(PersonaError):
    """Raised when ``PERSONA_<TIER>_MODELS`` cannot be parsed.

    Spec 20 D-20-17 case (d) + D-20-16: wrapper/configuration-layer error.
    Parser surfaces a structured ``reason`` so operators see which entry
    failed and why. Reasons: ``empty_after_strip`` / ``empty_csv_entry`` /
    ``missing_slash`` / ``unknown_provider`` / ``empty_model``.

    Context: ``{"tier", "value", "reason"}`` (plus ``"position"`` when the
    failure is a specific CSV slot).
    """


class IncompleteTierConfigError(PersonaError):
    """Raised when 1-2 of the 3 triplet vars are set with no MODELS list.

    Spec 20 D-20-17 + D-20-16: wrapper/configuration-layer error. The
    backward-compat path (case (b)) requires *all three* of
    ``PERSONA_<TIER>_PROVIDER``, ``PERSONA_<TIER>_MODEL``,
    ``PERSONA_<TIER>_API_KEY`` to be set. A partial set is almost certainly
    an operator mid-migration mistake; failing loud at construction beats
    silently dropping the partial config.

    Context: ``{"tier", "missing_vars"}``.
    """


class TierNotConfiguredError(PersonaError):
    """Raised when every provider in a tier's MODELS list fails to resolve.

    Spec 20 D-20-15 ALL-fail branch + D-20-16: wrapper/configuration-layer
    error. Mirrors Spec 05 D-05-3's fail-fast-at-construction discipline:
    a tier with no usable backend is a startup error, not a runtime error.

    Context: ``{"tier", "missing_providers", "configured_models",
    "consulted_env_vars"}``.
    """


class AllModelsFailedError(PersonaError):
    """Raised by :class:`MultiModelChatBackend` when every backend exhausts.

    Spec 20 D-20-16: wrapper-layer error class (slots under
    :class:`PersonaError`, NOT under :class:`ProviderError` — the wrapper
    has no live API call of its own; it composes provider-layer attempts).

    Emitted only after the wrapper has walked the full ordered backend list
    per the D-20-9 three-bucket classifier, applying D-20-10 N=1 same-model
    retry to RETRY-THEN-FALLBACK errors and falling through immediately on
    FALLBACK-NO-RETRY errors. SURFACE-bucket errors short-circuit the walk
    (the wrapper re-raises and never reaches this class).

    Context shape:

    * ``tier`` — configured tier name passed at construction (empty string
      if unnamed).
    * ``attempt_count`` — string-formatted number of attempts.
    * ``attempts_json`` — string repr of the per-attempt
      :class:`AttemptRecord` dicts (provider, model, last_error_class,
      last_error_status_code, retried_same_model).
    * ``final_error_class`` — class name of the last backend's terminal
      exception, repeated for fast log filtering.
    """


class NoVisionTierConfiguredError(RoutingConstraintsUnsatisfiableError):
    """Raised by the runtime router when an image message has no vision tier.

    Fired by the Spec 13 router (T11) when a ConversationMessage carries
    one or more ImageContent blocks and no tier in the configuration has
    ``supports_vision=True``. Distinct from
    :class:`BackendVisionNotSupportedError` because this is a
    *configuration* failure (no tier exists) rather than a *dispatch*
    failure (a specific backend cannot accept the request).

    Spec 18 (T03) moves this class under
    :class:`RoutingConstraintsUnsatisfiableError` so the generalised
    constraint-failure shape applies; the existing context shape is
    preserved:

    * ``reason`` — always ``"no_vision_tier"`` so log filters can match.
    * ``configured_tiers`` — comma-joined list of configured tier names.

    The Spec 18 ``required`` field is OPTIONAL on this subclass for
    back-compat — the existing Spec 13 raise site at ``router.py:202``
    does not set it; the new Spec 18 raise sites (T09) do.
    """


class IntelligentRoutingError(PersonaError):
    """Base for Spec 23 intelligent-routing (model-within-tier) failures.

    Spec 23 D-20-16 partition: a **wrapper-layer** error (roots at
    :class:`PersonaError` directly, NOT :class:`ProviderError` — intelligent
    routing composes metadata + scoring, it has no live API call of its own).

    The normal Spec 23 path is **graceful** (a metadata miss degrades to
    rule-based slot-0 selection, never raising — criterion 9). This base class
    therefore covers the *fail-loud* exceptions only; today the sole subclass
    is :class:`BudgetExceededError` (a hard per-turn budget cap). It exists as a
    family root so ``except IntelligentRoutingError`` catches every
    intelligent-routing fail-loud case in one clause.
    """


class BudgetExceededError(IntelligentRoutingError):
    """Raised when a per-turn hard budget cap admits no candidate model (D-23-7).

    Fail-loud (criterion 7): when ``routing.budget.max_cents_per_turn`` is set
    and every capability-passing candidate's estimated per-turn cost exceeds the
    cap, the turn fails rather than silently routing to an over-budget model. The
    per-session / per-day **soft** caps never raise — they re-weight scoring
    toward cost (D-23-7); only the per-turn hard cap reaches this class.

    Context shape:

    * ``tier`` — the tier whose candidate set was budget-filtered.
    * ``scope`` — always ``"per_turn"`` (the only hard cap).
    * ``cap_cents`` — the configured ``max_cents_per_turn`` (string).
    * ``cheapest_candidate_cents`` — the lowest estimated per-turn cost among
      the capability-passing candidates (string), so the operator sees how far
      over the cap the cheapest option was.
    """
