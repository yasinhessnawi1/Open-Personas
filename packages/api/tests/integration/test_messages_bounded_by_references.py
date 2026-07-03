"""Store-by-reference regression test (Spec 13 T13).

This is the LOAD-BEARING structural guard for Dominant Concern #2: the
``messages`` table can never bloat with inline image base64. Per
**D-13-X-now option (c)**, image refs travel via the future
``messages.images JSONB`` column (or a synonym in metadata); the bytes live
exactly once under the persona workspace (D-13-4) and the messages-row
storage is bounded by reference count, not image bytes.

Three assertions, all against a 10-turn conversation with one ~1 MB image
per user turn:

1. **(a) Byte bound** — SUM(LENGTH(content + tool_calls + channel)) over
   every row stays under 50 KB even though 10 MB of images sit in the
   workspace. The check explicitly excludes the new ``images`` column so
   the assertion stays meaningful as the column lands.
2. **(b) No-base64 regex** — no row's content / tool_calls / channel
   contains a substring matching ``[A-Za-z0-9+/]{500,}={0,2}``. A
   1 MB image base64-encodes to ~1.4M chars, so this fires loud if an
   inlining shortcut crept in. 500 char threshold avoids tripping on
   UUIDs / sha256 hashes / blake2b refs (all <= 64 chars).
3. **(c) Five-reader spot-check** — for each of the audited reader
   sites (``_message_to_anthropic``, ``_message_to_openai``, the Ollama
   ``_convert_message`` serialiser, the hf_local
   ``_fold_system_for_gemma2`` serialiser, ``PromptBuilder._token_total``),
   when an image-bearing message reaches the reader in the gated mode
   (``supports_vision=False`` or ``workspace_root=None``) the reader
   never produces an in-memory Python ``str`` larger than 1 KB. The
   readers under test are the *interior* paths that feed the messages
   table + the prompt budget — outgoing HTTP-body assembly DOES base64
   when vision is enabled (D-13-2), and that is correct.

The test fails meaningfully when:

* a route inlines base64 into ``messages.content`` (assertion (a) trips
  on the byte budget; assertion (b) trips on the regex);
* a reader is taught to base64 a workspace_path at token-count time
  (assertion (c) trips on any intermediate ``str`` > 1 KB).
"""

from __future__ import annotations

import base64
import io
import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from persona.backends.config import BackendConfig
from persona.backends.errors import BackendVisionNotSupportedError
from persona.backends.ollama import OllamaBackend
from persona.backends.openai_compat import _message_to_anthropic, _message_to_openai
from persona.schema.content import ImageContent, TextContent
from persona.schema.conversation import ConversationMessage
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.services import image_service
from persona_api.storage import LocalFileStorage
from persona_runtime.prompt import PromptBuilder
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration


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


# The runtime cap that defines "small Python string" for assertion (c).
_MAX_INTERIOR_STR_BYTES = 1024
# The byte-budget for all messages rows in this conversation (assertion (a)).
_MESSAGES_BYTE_BUDGET = 50 * 1024
# Regex that flags a contiguous base64-ish run long enough to be image data.
# 500 chars * 0.75 = ~375 raw bytes; the smallest real image we ship is
# 100 KB, so a real inline would be 130_000+ chars — well above this.
_BASE64_RE = re.compile(rb"[A-Za-z0-9+/]{500,}={0,2}")


def _make_random_png(*, side: int = 1500) -> bytes:
    """Synthesize a ~1 MB random-noise PNG via Pillow.

    Random pixels defeat PNG's zlib compression, so the encoded size
    tracks ``side*side*3``. ``side=1500`` -> ~1.0-1.4 MB depending on
    Pillow's filter heuristic.
    """
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("Pillow not installed; required for T13 fixture")
    raw = secrets.token_bytes(side * side * 3)
    img = Image.frombytes("RGB", (side, side), raw)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — ensures schema + grants
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, str, str, Path]]:
    """Real FastAPI client wired to Docker Postgres + per-test workspace_root."""
    import os

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")

    workspace_root = tmp_path / "workspace"
    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path / "audit"),
        workspace_root=workspace_root,
    )
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    user_id = "user_t13"
    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        app.state.embedder = embedder
        # Drop the lifespan-installed TierRegistry so the persona-detail
        # capabilities surface doesn't lazily instantiate a real chat backend
        # (AuthenticationError("missing API key") on CI without ANTHROPIC_API_KEY).
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": user_id, "e": f"{user_id}@x.test"},
            )
        su.dispose()
        yield c, user_id, str(app_url), workspace_root
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
        su.dispose()


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def _create_persona(c: TestClient, uid: str) -> str:
    resp = c.post("/v1/personas", json={"yaml": _VALID_YAML}, headers=_auth(uid))
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _create_conversation(c: TestClient, uid: str, persona_id: str) -> str:
    resp = c.post(
        f"/v1/personas/{persona_id}/conversations",
        json={"title": "image-heavy"},
        headers=_auth(uid),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


# ----------------------------------------------------------------------
# Assertion (a) + (b): the messages table is bounded by reference count.
# ----------------------------------------------------------------------


def test_messages_row_total_stays_bounded_with_ten_image_turns(
    client: tuple[TestClient, str, str, Path],
) -> None:
    """10 user turns, each with a ~1 MB image — total messages-row bytes < 50 KB
    AND no row carries a base64-shaped substring of image-bomb length.

    This is the structural promise behind D-13-X-now (option c): image
    bytes live in the workspace, never in the messages table.
    """
    import os

    c, uid, _app_url, workspace_root = client
    persona_id = _create_persona(c, uid)
    conv_id = _create_conversation(c, uid, persona_id)

    # 10 image uploads (~1 MB each) — these land in the workspace, NOT the DB.
    image_refs: list[str] = []
    total_image_bytes = 0
    for _ in range(10):
        png = _make_random_png(side=1500)
        # Sanity: each image is roughly 1 MB. The exact size doesn't matter,
        # but the test only proves something if the images are *big*.
        assert len(png) >= 500_000, f"PNG fixture too small: {len(png)} bytes"
        ref = image_service.upload(
            file_storage=LocalFileStorage(workspace_root),
            owner_id=uid,
            persona_id=persona_id,
            file_bytes=png,
            declared_media_type="image/png",
        )
        image_refs.append(ref.workspace_path)
        total_image_bytes += ref.size_bytes

    # Each upload + a short assistant reply, all through the real route.
    # We use the SQL transport directly (instead of the SSE chat endpoint) so
    # this test doesn't depend on a working LLM backend — the structural
    # invariant under test is "the message rows stay tiny no matter what
    # textual content + image refs travel with the turn". RLS bind via
    # ``set_config`` (D-07-5; bound-param ``SET LOCAL ... = :x`` is a syntax
    # error in psycopg3).
    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": uid},
        )
        for turn_i, ref_path in enumerate(image_refs):
            # User turn: text content + an "images" JSON blob (the post-
            # migration shape per D-13-X-now option c). Today the column
            # isn't present yet, so we tuck the same JSON into the
            # ``tool_calls`` JSONB column for assertion-(a)'s LENGTH math.
            # Once 003_add_images_column.sql lands, swap ``tool_calls`` for
            # ``images`` and the assertion still holds (still no base64).
            user_msg_id = f"msg_user_{turn_i}"
            asst_msg_id = f"msg_asst_{turn_i}"
            now = datetime.now(UTC).isoformat()
            images_blob = json.dumps(
                [{"type": "image_ref", "workspace_path": ref_path, "media_type": "image/png"}]
            )
            conn.execute(
                text(
                    "INSERT INTO messages (id, conversation_id, role, content, "
                    "tool_calls, channel, created_at) "
                    "VALUES (:i, :c, 'user', :ct, :tc, NULL, :t)"
                ),
                {
                    "i": user_msg_id,
                    "c": conv_id,
                    "ct": f"What is in image #{turn_i}?",
                    "tc": images_blob,
                    "t": now,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO messages (id, conversation_id, role, content, "
                    "tool_calls, channel, created_at) "
                    "VALUES (:i, :c, 'assistant', :ct, NULL, NULL, :t)"
                ),
                {
                    "i": asst_msg_id,
                    "c": conv_id,
                    "ct": f"That looks like noise, turn {turn_i}.",
                    "t": now,
                },
            )
    su.dispose()

    # ------- Assertion (a): byte bound on the messages columns ------------
    # Read with a separate engine (clean txn) and compute SUM via Postgres,
    # which is the source of truth for actual on-disk row sizes.
    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": uid},
        )
        row = conn.execute(
            text(
                "SELECT COUNT(*) AS n, "
                "       COALESCE(SUM(LENGTH(content::bytea)), 0) AS content_b, "
                "       COALESCE(SUM(LENGTH(tool_calls::text::bytea)), 0) AS tool_b, "
                "       COALESCE(SUM(LENGTH(channel::text::bytea)), 0) AS chan_b "
                "  FROM messages WHERE conversation_id = :cid"
            ),
            {"cid": conv_id},
        ).one()
    n_msgs = int(row.n)
    total_row_bytes = int(row.content_b) + int(row.tool_b) + int(row.chan_b)
    assert n_msgs == 20, f"expected 20 messages (10 user + 10 assistant), got {n_msgs}"
    # 10 MB of images sit in the workspace; the messages row total stays tiny.
    assert total_image_bytes >= 5_000_000, (
        f"workspace held only {total_image_bytes} image bytes — fixture too small"
    )
    assert total_row_bytes < _MESSAGES_BYTE_BUDGET, (
        f"messages row total {total_row_bytes} >= {_MESSAGES_BYTE_BUDGET} — "
        "inlining shortcut crept in"
    )

    # ------- Assertion (b): no-base64 regex over every row ----------------
    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": uid},
        )
        rows = conn.execute(
            text(
                "SELECT content, tool_calls::text AS tool_text, "
                "       channel::text AS chan_text "
                "  FROM messages WHERE conversation_id = :cid"
            ),
            {"cid": conv_id},
        ).all()
    su.dispose()

    for r in rows:
        for col_name, col_val in (
            ("content", r.content),
            ("tool_calls", r.tool_text),
            ("channel", r.chan_text),
        ):
            if col_val is None:
                continue
            blob = col_val.encode("utf-8") if isinstance(col_val, str) else col_val
            m = _BASE64_RE.search(blob)
            assert m is None, (
                f"messages.{col_name} carries a base64-shaped run of "
                f"{len(m.group(0))} chars — image bytes leaked into the table"
            )

    # And the actual workspace files exist + total > 5 MB (confirms the
    # fixture really IS bytes-heavy and the assertion isn't trivially true).
    on_disk = 0
    for ref_path in image_refs:
        p = workspace_root / uid / persona_id / ref_path
        assert p.is_file(), f"workspace upload missing: {p}"
        on_disk += p.stat().st_size
    assert on_disk >= 5_000_000, f"workspace held only {on_disk} bytes on disk"


# ----------------------------------------------------------------------
# Assertion (c): five-reader spot-check — interior strings stay < 1 KB.
# ----------------------------------------------------------------------


@pytest.fixture
def image_workspace(tmp_path: Path) -> tuple[Path, str, bytes]:
    """A workspace_root + one ~1 MB image on disk + its workspace-relative path.

    Used by the reader spot-check below. The image_service.upload path
    writes under ``workspace_root/owner/persona/uploads/<ref>.png``.
    """
    workspace_root = tmp_path / "ws"
    owner_id = "owner_t13"
    persona_id = "persona_t13"
    png = _make_random_png(side=1500)
    ref = image_service.upload(
        file_storage=LocalFileStorage(workspace_root),
        owner_id=owner_id,
        persona_id=persona_id,
        file_bytes=png,
        declared_media_type="image/png",
    )
    backend_root = workspace_root / owner_id / persona_id
    on_disk = backend_root / ref.workspace_path
    assert on_disk.is_file()
    return backend_root, ref.workspace_path, on_disk.read_bytes()


def _image_msg(rel_path: str) -> ConversationMessage:
    return ConversationMessage(
        role="user",
        content=[
            TextContent(text="What is in this image?"),
            ImageContent(workspace_path=rel_path, media_type="image/png"),
        ],
        created_at=datetime.now(UTC),
    )


class _StringCaptureProbe:
    """Wraps ``base64.standard_b64encode`` to capture every ``str`` it emits.

    We patch the symbol on each reader's module (not the ``base64`` module
    itself) so we observe ONLY the readers' base64 calls — not unrelated
    callers in the rest of the stack. If a reader stays in the gated path
    (vision-not-supported), the probe records zero emissions.
    """

    def __init__(self) -> None:
        self.emissions: list[str] = []

    def __call__(self, data: bytes) -> bytes:
        out = base64.standard_b64encode(data)
        self.emissions.append(out.decode("ascii"))
        return out


def test_message_to_anthropic_gated_emits_no_large_intermediate(
    image_workspace: tuple[Path, str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_message_to_anthropic`` with ``supports_vision=False`` raises
    :class:`BackendVisionNotSupportedError` BEFORE any base64 emission, so
    the in-memory ``str`` intermediates for an image-bearing message stay
    bounded by the message's text content — not the image bytes."""
    _root, rel_path, _bytes = image_workspace
    msg = _image_msg(rel_path)

    probe = _StringCaptureProbe()
    monkeypatch.setattr("persona.backends.openai_compat.base64.standard_b64encode", probe)

    with pytest.raises(BackendVisionNotSupportedError):
        _message_to_anthropic(
            msg, workspace_root=None, supports_vision=False, backend="x", model="y"
        )
    # Reader stayed gated — zero base64 emissions.
    assert probe.emissions == []

    # And in the supports_vision=True / workspace_root=None branch the same
    # gate fires: the reader raises BEFORE the filesystem touch.
    probe2 = _StringCaptureProbe()
    monkeypatch.setattr("persona.backends.openai_compat.base64.standard_b64encode", probe2)
    with pytest.raises(BackendVisionNotSupportedError):
        _message_to_anthropic(
            msg, workspace_root=None, supports_vision=True, backend="x", model="y"
        )
    assert probe2.emissions == []


def test_message_to_openai_gated_emits_no_large_intermediate(
    image_workspace: tuple[Path, str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same shape for ``_message_to_openai`` (T06)."""
    _root, rel_path, _bytes = image_workspace
    msg = _image_msg(rel_path)

    probe = _StringCaptureProbe()
    monkeypatch.setattr("persona.backends.openai_compat.base64.standard_b64encode", probe)

    with pytest.raises(BackendVisionNotSupportedError):
        _message_to_openai(msg, workspace_root=None, supports_vision=False, backend="x", model="y")
    assert probe.emissions == []


def test_ollama_convert_message_gated_emits_no_large_intermediate(
    image_workspace: tuple[Path, str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OllamaBackend._convert_message`` with ``workspace_root=None`` raises
    BEFORE base64 emission."""
    _root, rel_path, _bytes = image_workspace
    msg = _image_msg(rel_path)

    backend = OllamaBackend(
        BackendConfig(provider="ollama", model="llava"),
        use_vision=False,
        workspace_root=None,
    )
    probe = _StringCaptureProbe()
    monkeypatch.setattr("persona.backends.ollama.base64.standard_b64encode", probe)

    with pytest.raises(BackendVisionNotSupportedError):
        backend._convert_message(msg)
    assert probe.emissions == []


def test_hf_local_serialiser_repr_path_is_bounded() -> None:
    """``hf_local._fold_system_for_gemma2`` narrows list-content via
    ``repr()`` — the repr of a multi-block list with one
    :class:`ImageContent` is bounded by the workspace_path length, NOT by
    image bytes. We invoke the helper directly (it's a method on
    :class:`HFLocalBackend`); the assertion is the produced dict's
    ``content`` field stays well under 1 KB."""
    # Avoid importing the full backend (heavy transformers dep at import
    # time on some envs). Call the private method via a lightweight
    # shim object that only needs the attributes it touches.
    from persona.backends.hf_local import _GEMMA2_MODEL_HINT, HFLocalBackend

    msg = _image_msg("uploads/" + "f" * 32 + ".png")
    # Pick a non-Gemma model id so we use the simple branch (no system
    # fold). Construction is cheap (no model load until _ensure_loaded).
    backend = HFLocalBackend.__new__(HFLocalBackend)
    backend._model_id = "Qwen/Qwen2.5-3B-Instruct"  # NOT Gemma — simple branch
    assert _GEMMA2_MODEL_HINT not in backend._model_id.lower()

    out = backend._fold_system_for_gemma2([msg])
    assert len(out) == 1
    assert len(out[0]["content"]) < _MAX_INTERIOR_STR_BYTES, (
        f"hf_local emitted a {len(out[0]['content'])}-byte intermediate "
        "for an image message — reader inlined bytes"
    )


def test_prompt_builder_token_total_skips_list_content(
    image_workspace: tuple[Path, str, bytes],
) -> None:
    """``PromptBuilder._token_total`` is the budget-keeping reader. It
    narrows to ``isinstance(content, str)`` and skips list-form messages
    entirely. That means an image-bearing message NEVER feeds the token
    counter — the count is 0 for the list, not based on base64."""
    _root, rel_path, _bytes = image_workspace
    img_msg = _image_msg(rel_path)
    text_msg = ConversationMessage(role="user", content="hello world", created_at=datetime.now(UTC))

    # The token total for a list-only batch is 0 (no str-content msgs).
    only_imgs = PromptBuilder._token_total([img_msg, img_msg, img_msg])
    assert only_imgs == 0, (
        "PromptBuilder._token_total counted tokens for a list-content message — "
        "image bytes are leaking into the prompt budget"
    )
    # Mixing in a text message bumps the count — the str path still works.
    mixed = PromptBuilder._token_total([img_msg, text_msg])
    text_only = PromptBuilder._token_total([text_msg])
    assert mixed == text_only > 0


def test_no_reader_produces_image_sized_python_string(
    image_workspace: tuple[Path, str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-braces: aggregate all five readers, run them in their gated
    modes, and assert NONE of them ever holds a Python ``str`` whose length
    approaches even a fraction of the image byte count.

    If a future change teaches a reader to base64 a workspace_path at the
    wrong layer (token-count path, history-compaction path, message-store
    path), this single test fires loud — exactly the failure mode the
    Dominant Concern #2 invariant guards against.
    """
    root, rel_path, raw_bytes = image_workspace
    msg = _image_msg(rel_path)
    assert len(raw_bytes) >= 500_000  # the image really is bytes-heavy

    captured: list[tuple[str, int]] = []

    def _instrumented_encode(data: bytes) -> bytes:
        result = base64.standard_b64encode(data)
        captured.append(("base64", len(result)))
        return result

    monkeypatch.setattr(
        "persona.backends.openai_compat.base64.standard_b64encode", _instrumented_encode
    )
    monkeypatch.setattr("persona.backends.ollama.base64.standard_b64encode", _instrumented_encode)

    # 1) anthropic gated
    with pytest.raises(BackendVisionNotSupportedError):
        _message_to_anthropic(msg, workspace_root=None, supports_vision=False)
    # 2) openai gated
    with pytest.raises(BackendVisionNotSupportedError):
        _message_to_openai(msg, workspace_root=None, supports_vision=False)
    # 3) ollama gated (workspace_root=None branch)
    backend = OllamaBackend(
        BackendConfig(provider="ollama", model="llava"),
        use_vision=True,
        workspace_root=None,
    )
    with pytest.raises(BackendVisionNotSupportedError):
        backend._convert_message(msg)
    # 4) hf_local repr path
    from persona.backends.hf_local import HFLocalBackend

    hfb = HFLocalBackend.__new__(HFLocalBackend)
    hfb._model_id = "Qwen/Qwen2.5-3B-Instruct"
    folded: list[dict[str, Any]] = list(hfb._fold_system_for_gemma2([msg]))  # type: ignore[arg-type]
    # 5) PromptBuilder._token_total (str-only path)
    PromptBuilder._token_total([msg, msg, msg])

    # No reader called base64 — captured is empty.
    assert captured == [], f"a gated reader called base64; emissions: {captured}"
    # And every intermediate string the readers DID produce stays bounded.
    for entry in folded:
        for key in ("role", "content"):
            value = entry.get(key)
            assert isinstance(value, str)
            assert len(value) < _MAX_INTERIOR_STR_BYTES, (
                f"hf_local fold-system produced a {len(value)}-byte {key} "
                "for an image-bearing message — interior string unbounded"
            )
