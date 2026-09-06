"""Host the connector transports inside the api process (Spec I1 T2, D-I1-17).

The embedded half of the connectors fold. The standalone connector service ran a second
Fly machine whose ~400 MB was almost entirely a DUPLICATE model stack (torch, the
bge-small embedder, the tier registry, the model backends) that it shared with nothing.
Measured, the connector CODE costs under 5 MB on top of an api process (D-I1-1), so
hosting the same runners here and reusing the api's own engines and
:class:`~persona_api.services.runtime_factory.RuntimeFactory` removes the duplication
rather than moving it.

What this module does NOT do, deliberately:

- **It starts no second server.** :func:`~persona_connectors.service.build_connectors`
  hands back a merged ASGI app, and :func:`mount_connector_routes` folds those routes into
  the api's own app on the api's own port. Serving them on :8080 in-process would be
  Option B of the decision record, which was not chosen: the api keeps one public ingress,
  and the cutover is one DNS record (``docs/ops/connectors_fold_decision.md``).
- **It supervises with the connectors' own** :func:`~persona_connectors.service._supervised`
  (D-I1-10). That function already carries R9-071 containment, R9-073c restart-with-backoff,
  the healthy-uptime reset and the give-up ceiling, covered to its exact backoff schedule.
  A second supervisor here would be a third thing to keep honest.
- **It never fails the api's startup.** A flag-ON boot with no platform configured logs
  loudly and continues (D-I1-6): the api serves the web app, and its availability must not
  depend on a connector token being present.

The RLS posture is the one thing worth being explicit about (R9-123, D-I1-18). Connector
work is owner-scoped, so it MUST run on a role row-level security actually binds. Here the
api's ``rls_engine`` (the non-superuser ``persona_app`` role, already proven non-superuser
by the R2-D-1 startup probe) is the owner-scoped engine, and the api's ``admin_engine`` is
the cross-tenant dispatch engine its pre-auth resolve/redeem reads need. Passing those two
the wrong way round would reintroduce a silent cross-tenant leak INSIDE the api process,
which is why they are separate parameters rather than one URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

import httpx
from persona.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import FastAPI, Request
    from persona_connectors.service import ConnectorsBundle
    from sqlalchemy.engine import Engine
    from starlette.responses import Response

    from persona_api.billing import StripeGateway
    from persona_api.config import APIConfig
    from persona_api.editions.credits_policy import CreditsPolicy
    from persona_api.jobs import JobQueue
    from persona_api.services.runtime_factory import RuntimeFactory

__all__ = [
    "EmbeddedConnectors",
    "find_mounted_link_endpoint",
    "mount_connector_routes",
    "start_embedded_connectors",
]

_log = get_logger("api.connectors_host")

# The shared outbound client's budget, identical to the standalone service's, so a folded
# deployment's platform calls time out exactly as they did on their own machine.
_HTTP_TIMEOUT_SECONDS = 60.0


class EmbeddedConnectors:
    """Owns the supervised connector runner tasks (start on boot, drain on shutdown).

    Mirrors :class:`~persona_api.background.worker_root.InProcessWorker`, the R9-093
    hosting precedent: one task per supervised loop, launched from the lifespan and
    drained by it. Holds the shared ``httpx`` client because this host CREATED it, and
    nothing should close a client it did not create.

    Args:
        bundle: The composed connectors.
        http: The shared outbound client this host owns and closes.
        sleep: Injected into :func:`~persona_connectors.service._supervised` so a test can
            drive real crash-and-restart without real backoff waits (D-I1-14).
        monotonic: Injected likewise, for the healthy-uptime reset.
    """

    def __init__(
        self,
        bundle: ConnectorsBundle,
        *,
        http: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bundle = bundle
        self._http = http
        self._sleep = sleep
        self._monotonic = monotonic
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def http_app(self) -> FastAPI | None:
        """The merged ASGI app whose routes the api mounts (T3); ``None`` if unconfigured."""
        return self._bundle.http_app

    @property
    def platforms(self) -> tuple[str, ...]:
        """The configured platform keys, for logging and health reporting."""
        return tuple(self._bundle.deliverers)

    def start(self) -> None:
        """Launch one supervised task per runner, plus the idle sweep. Idempotent.

        Every transport runs under the connectors' own ``_supervised``, so a crashed
        platform is contained and restarted with backoff while the others keep serving,
        and ``CancelledError`` still propagates for a clean shutdown.
        """
        if self._tasks:
            return
        from persona_connectors.service import _supervised

        for name, factory in self._bundle.runners.items():
            self._tasks.append(
                asyncio.create_task(
                    _supervised(name, factory, sleep=self._sleep, monotonic=self._monotonic),
                    name=f"connector:{name}",
                )
            )
        if self._bundle.idle_sweep is not None:
            self._tasks.append(
                asyncio.create_task(self._bundle.idle_sweep(), name="connector:idle_sweep")
            )
        _log.info(
            "embedded connectors started for: {platforms}",
            platforms=", ".join(self.platforms) or "none",
        )

    async def aclose(self) -> None:
        """Cancel every runner, await its exit, then close the shared client.

        The runners are infinite loops (long-poll, gateway socket, socket mode, the idle
        sweep), so cancellation IS the drain: there is no graceful-exit signal to request,
        and ``_supervised`` re-raises ``CancelledError`` rather than treating it as a crash
        to restart. Called from the lifespan BEFORE ``runtime_factory.aclose()`` and before
        the engines are disposed (D-I1-8), so nothing in flight can touch a dead pool.
        """
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        await self._http.aclose()
        _log.info("embedded connectors stopped")


async def start_embedded_connectors(
    *,
    config: APIConfig,
    rls_engine: Engine,
    dispatch_engine: Engine,
    runtime_factory: RuntimeFactory,
    credits_policy: CreditsPolicy,
    job_queue: JobQueue,
    stripe_gateway: StripeGateway | None,
) -> EmbeddedConnectors | None:
    """Compose + start the embedded connectors, or return ``None`` when there are none.

    Args:
        config: The api config, shared with the injected runtime.
        rls_engine: The api's OWNER-SCOPED engine (the non-superuser ``persona_app`` role).
            Row-level security must bind this role, or every owner scope is honoured by
            nothing (R9-123, D-I1-18).
        dispatch_engine: The api's cross-tenant engine, for the pre-auth resolve and redeem
            reads that precede any owner scope.
        runtime_factory: The api's own runtime. Reusing it is the entire saving.
        credits_policy: The api's edition credits policy, so a connector turn meters through
            the same object a web turn does.
        job_queue: The api's durable enqueue surface.
        stripe_gateway: The api's Stripe gateway, or ``None`` outside cloud.

    Returns:
        The started handle, or ``None`` when no connector platform is configured. ``None``
        is a normal outcome, not a failure: the caller logs it and keeps serving (D-I1-6).
    """
    from persona_connectors.config import ConnectorConfig
    from persona_connectors.service import build_connectors

    http = httpx.AsyncClient(timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS))
    try:
        bundle = await build_connectors(
            connector_config=ConnectorConfig(),
            api_config=config,
            rls_engine=rls_engine,
            dispatch_engine=dispatch_engine,
            runtime_factory=runtime_factory,
            credits_policy=credits_policy,
            job_queue=job_queue,
            stripe_gateway=stripe_gateway,
            http=http,
        )
    except Exception:
        # Composition reaches the live platforms (Telegram getMe, Discord /users/@me,
        # Slack auth.test), so a revoked token or a platform outage can raise here. That
        # must not take down the api, which is also serving the web app.
        await http.aclose()
        raise
    if not bundle.deliverers:
        await http.aclose()
        return None
    handle = EmbeddedConnectors(bundle, http=http)
    handle.start()
    return handle


def mount_connector_routes(app: FastAPI, routes_app: FastAPI) -> int:
    """Fold the connectors' HTTP surface into the api's own app (Spec I1 T3, D-I1-11).

    The five provider registrations (Discord and Slack OAuth redirects, the Slack events
    URL, the Postmark inbound webhook, the Twilio webhooks) pin
    ``https://connectors.openpersonasai.com/...`` paths. Mounting the routes UNCHANGED is
    what lets the cutover be a DNS record instead of five re-registrations, so the paths
    are appended verbatim rather than re-prefixed.

    Two things are done to each route and both are required:

    - ``include_in_schema = False``, because these are provider webhooks and OAuth
      callbacks, not part of the api's public contract. Without it the generated web
      client would grow six webhook endpoints it must never call.
    - the app's cached ``openapi_schema`` is dropped, because a schema generated earlier
      in the boot would otherwise be served stale.

    Appending after construction is sound: Starlette matches against ``router.routes`` at
    request time, and lifespan startup completes before the first request is served. It is
    also the idiom the connectors' own composition already uses to merge six transports
    onto one parent app.

    Args:
        app: The api application.
        routes_app: The merged connectors app from the bundle.

    Returns:
        How many routes were mounted, for the startup log.
    """
    mounted = 0
    for route in routes_app.router.routes:
        # Only APIRoute carries include_in_schema; guard rather than assume the shape.
        if hasattr(route, "include_in_schema"):
            route.include_in_schema = False
        app.router.routes.append(route)
        mounted += 1
    # Drop the cached schema so a schema built earlier in the boot is not served stale.
    app.openapi_schema = None
    return mounted


def find_mounted_link_endpoint(
    app: FastAPI, platform: str
) -> Callable[[Request], Awaitable[Response]] | None:
    """The in-process handler for ``POST /v1/connectors/{platform}/link``, if mounted.

    Spec I1 D-I1-16. When the connectors are embedded, the api's authenticated front door
    must NOT forward the link request to itself over HTTP: a process calling its own public
    hostname burns a round trip, turns a local failure into an opaque 503, and depends on
    DNS already being correct in order to serve the request that the cutover exists to make
    correct. Resolving the mounted handler and calling it directly removes that hop.

    The lookup walks the router rather than caching a table at mount time, so it cannot go
    stale against the routes actually being served. Link initiation is a rare, human-paced
    request, so the walk is not on any hot path.

    Args:
        app: The api application.
        platform: The platform key, already constrained to the closed enum by the route.

    Returns:
        The endpoint callable, or ``None`` when the connectors are not mounted (in which
        case the caller keeps today's forwarder).
    """
    target = f"/v1/connectors/{platform}/link"
    for route in app.router.routes:
        if getattr(route, "path", None) != target:
            continue
        if "POST" not in (getattr(route, "methods", None) or ()):
            continue
        endpoint: Callable[[Request], Awaitable[Response]] | None = getattr(route, "endpoint", None)
        if endpoint is not None:
            return endpoint
    return None
