"""Imagegen's auto-top-up fires from the REAL imagegen path (Spec M5, B5).

§1c.11: the M4 trigger reached chat and background runs only. Voice and imagegen, the two
priciest surfaces, were never covered, so a Pro user who opted into auto-top-up could burn
through their balance generating images and the feature they paid for would never fire.

Voice needs a durable job because it is a peer process that must never hold Stripe
credentials (D-M5-15). Imagegen does not: it already runs inside the api, which holds the
gateway, so it calls the same decision function directly. Same code decides in both cases,
so the two surfaces cannot diverge on WHEN a top-up fires.

These tests drive ``POST /v1/personas/:id/imagegen`` over the real route, with a real
deduct against the real ledger, and observe the charge as a consequence. A unit test
around ``maybe_auto_topup`` would prove the function works and prove nothing about whether
the imagegen path reaches it, which is the gap that let §1c.11 exist in the first place.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.imagegen.result import GeneratedImage, GenerationResult, ImageGenOptions
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig, Edition
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_USER = "u_m5_img_topup"


# Inlined rather than imported from ``test_api_imagegen``: this suite has no cross-test
# import convention, and pytest's rootdir does not put ``tests`` on the path, so an
# import there fails collection. Small enough to duplicate honestly.
_VALID_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy law assistant
  background: |
    Helps tenants understand husleieloven.
  language_default: en
  constraints: []
"""

#: A minimum-valid 1x1 PNG (the same bytes the imagegen suite uses).
_TINY_PNG: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae"
    "426082"
)


class _FakeGateway:
    """Records off-session charges. No Stripe network; test mode only."""

    publishable_key = "pk_test"

    def __init__(self) -> None:
        self.topups: list[dict[str, object]] = []

    def create_off_session_topup(
        self, *, customer_id: str, credit_amount: int, user_id: str, idempotency_key: str
    ) -> tuple[str, str]:
        self.topups.append(
            {
                "customer_id": customer_id,
                "credit_amount": credit_amount,
                "user_id": user_id,
                "idempotency_key": idempotency_key,
            }
        )
        return (f"pi_{idempotency_key}", "succeeded")


class _HappyBackend:
    """A structural ``ImageBackend`` returning fixed bytes (no provider network)."""

    def __init__(self, *, image_bytes_list: list[bytes]) -> None:
        self._images = image_bytes_list
        self.prompts: list[str] = []

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model-1"

    async def generate(
        self, prompt: str, *, options: ImageGenOptions | None = None
    ) -> GenerationResult:
        self.prompts.append(prompt)
        n = options.count if options is not None else 1
        return GenerationResult(
            images=[
                GeneratedImage(
                    image_bytes=b,
                    workspace_path=None,
                    media_type="image/png",
                    width=1,
                    height=1,
                    revised_prompt=None,
                )
                for b in self._images[:n]
            ],
            provider=self.provider_name,
            model=self.model_name,
            latency_ms=12.5,
        )

    async def edit(
        self,
        input_image: GeneratedImage,
        instructions: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        raise NotImplementedError("edit not supported in this fake")


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — ensures schema + grants
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, _FakeGateway, Engine]]:
    """The real app + a fake image backend + a fake Stripe gateway."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")

    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path / "audit"),
        workspace_root=tmp_path / "workspace",
        edition=Edition.cloud,
    )
    app = create_app(cfg)
    gateway = _FakeGateway()

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    with TestClient(app) as c:
        app.state.verify_token = _verify
        app.state.embedder = embedder
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        app.state.image_backend = _HappyBackend(image_bytes_list=[_TINY_PNG])
        app.state.stripe_gateway = gateway

        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": _USER, "e": f"{_USER}@x.test"},
            )
        yield c, gateway, su
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": _USER})
        su.dispose()


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_USER}"}


def _arm(su: Engine, *, plan: str = "pro", customer: str | None = "cus_img") -> None:
    with su.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO subscription "
                "(user_id, plan_code, status, stripe_customer_id, auto_topup_enabled) "
                "VALUES (:u, :p, 'active', :c, true) ON CONFLICT (user_id) DO UPDATE "
                "SET plan_code = :p, stripe_customer_id = :c, auto_topup_enabled = true"
            ),
            {"u": _USER, "p": plan, "c": customer},
        )


def _set_balance(su: Engine, amount: int) -> None:
    with su.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO credits (user_id, balance) VALUES (:u, :b) "
                "ON CONFLICT (user_id) DO UPDATE SET balance = :b"
            ),
            {"u": _USER, "b": amount},
        )


def _create_persona(c: TestClient) -> str:
    """Create the persona FIRST.

    Persona-create auto-generates an avatar (Spec 29), and that generation bills its own
    credit. So the balance has to be set AFTER this, or the crossing would happen during
    setup and the test would pass (or fail) for the wrong reason.
    """
    resp = c.post("/v1/personas", json={"yaml": _VALID_YAML}, headers=_auth())
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _generate(c: TestClient, pid: str) -> int:
    gen = c.post(
        f"/v1/personas/{pid}/imagegen",
        json={"prompt": "a small square", "size": "1024x1024", "count": 1},
        headers=_auth(),
    )
    return gen.status_code


def test_an_image_that_crosses_the_line_triggers_a_top_up(
    client: tuple[TestClient, _FakeGateway, Engine],
) -> None:
    """The §1c.11 gap, closed: a real generation on a real route fires the real trigger."""
    c, gateway, su = client
    _arm(su)
    pid = _create_persona(c)
    # Exactly ON the $2 line, so the generation's charge takes the balance BELOW it and
    # the crossing guard fires. The fake backend reports no usage, so the true-up refunds
    # the 50-credit ceiling down to the 1-credit floor: the real charge is 1 credit, and
    # 200 -> 199 is the crossing. Set AFTER create, whose auto-avatar also bills 1.
    _set_balance(su, 200)

    assert _generate(c, pid) == 201
    assert len(gateway.topups) == 1
    assert gateway.topups[0]["user_id"] == _USER


def test_an_image_well_inside_the_balance_triggers_nothing(
    client: tuple[TestClient, _FakeGateway, Engine],
) -> None:
    """Most generations are not crossings, and must not touch the card."""
    c, gateway, su = client
    _arm(su)
    pid = _create_persona(c)
    _set_balance(su, 50_000)

    assert _generate(c, pid) == 201
    assert gateway.topups == []


def test_a_free_user_generating_images_never_reaches_stripe(
    client: tuple[TestClient, _FakeGateway, Engine],
) -> None:
    """Eligibility is decided by the same engine code, so Free is refused here too."""
    c, gateway, su = client
    _arm(su, plan="free")
    pid = _create_persona(c)
    _set_balance(su, 200)  # on the line: a crossing WOULD fire if the plan allowed it

    assert _generate(c, pid) == 201
    assert gateway.topups == []


def test_an_armed_user_with_no_card_generates_but_is_not_charged(
    client: tuple[TestClient, _FakeGateway, Engine],
) -> None:
    """No saved card cannot be charged off-session. T4b tells the user; the image still ships."""
    c, gateway, su = client
    _arm(su, customer=None)
    pid = _create_persona(c)
    _set_balance(su, 200)  # a real crossing, but there is no card to charge

    assert _generate(c, pid) == 201
    assert gateway.topups == []


def test_a_failing_top_up_never_breaks_the_generation(
    client: tuple[TestClient, _FakeGateway, Engine],
) -> None:
    """Billing is enrichment; the image is the work.

    The user has already been charged for this image, so a Stripe hiccup in the top-up
    side effect must not turn a finished generation into a 500.
    """
    c, gateway, su = client
    _arm(su)
    pid = _create_persona(c)
    _set_balance(su, 200)

    def _explode(**_kwargs: object) -> tuple[str, str]:
        raise RuntimeError("stripe is down")

    gateway.create_off_session_topup = _explode  # type: ignore[method-assign]

    assert _generate(c, pid) == 201  # the generation still succeeds
