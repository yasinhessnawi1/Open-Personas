"""Tests for Spec F5 T02 — artifact-list endpoint (D-F5-1).

Verifies the GET /v1/personas/{persona_id}/artifacts contract: workspace
walk + sidecar read + filtering + pagination + Pydantic-Field clamping
(structured 422 on overflow, NOT silent truncation) + cross-tenant 404.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.errors import PersonaNotFoundError
from persona_api.services import persona_service
from persona_api.services.artifact_metadata import (
    WorkspaceArtifactMetadata,
    write_artifact_sidecar,
)
from persona_api.storage import LocalFileStorage


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def persona_root(workspace_root: Path) -> Path:
    """Per-tenant persona scope: workspace_root/<owner_id>/<persona_id>/."""
    root = workspace_root / "u1" / "astrid"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    workspace_root: Path,
) -> TestClient:
    app = create_app(
        # Cloud auth wall, but no lifespan engine is built here (the fixture
        # returns the client without entering its context + sets rls_engine=None).
        # Distinct app DSN satisfies the R2 cloud-config guard (R2-D-1).
        APIConfig(
            database_url="postgresql+psycopg://super@localhost/persona_shell",
            app_database_url="postgresql+psycopg://persona_app@localhost/persona_shell",
        )
    )

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter

    app.state.verify_token = _verify
    app.state.rls_engine = None
    app.state.workspace_root = workspace_root
    app.state.file_storage = LocalFileStorage(workspace_root)  # R5-D-4: list/delete read this
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=10_000, per_endpoint={}
    )

    def _fake_get_persona(*, rls_engine: Any, persona_id: str) -> dict[str, Any]:  # noqa: ANN401, ARG001
        if persona_id == "astrid":
            return {"id": persona_id, "owner_id": "u1", "yaml": ""}
        raise PersonaNotFoundError("persona not found", context={"persona_id": persona_id})

    monkeypatch.setattr(persona_service, "get_persona", _fake_get_persona)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer u1"}


def _make_meta(
    *,
    source: str = "upload",
    type_: str = "image",
    producing_spec: str = "13",
    conversation_id: str | None = None,
    original_name: str | None = None,
    created_at: datetime | None = None,
) -> WorkspaceArtifactMetadata:
    return WorkspaceArtifactMetadata(
        source=source,  # type: ignore[arg-type]
        type=type_,  # type: ignore[arg-type]
        producing_spec=producing_spec,  # type: ignore[arg-type]
        conversation_id=conversation_id,
        created_at=created_at or datetime(2026, 6, 7, 12, 0, tzinfo=UTC),
        original_name=original_name,
    )


def _seed(persona_root: Path, rel: str, content: bytes = b"X") -> Path:
    target = persona_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


# -- happy-path enumeration --------------------------------------------------


def test_empty_workspace_returns_empty_list(client: TestClient) -> None:
    resp = client.get("/v1/personas/astrid/artifacts", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"total": 0, "limit": 50, "offset": 0, "items": []}


def test_lists_files_with_metadata_when_sidecars_present(
    client: TestClient, persona_root: Path
) -> None:
    image = _seed(persona_root, "uploads/abc.png", b"PNGDATA")
    write_artifact_sidecar(image, _make_meta(original_name="photo.png"))

    resp = client.get("/v1/personas/astrid/artifacts", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    [item] = body["items"]
    assert item["ref"] == "uploads/abc.png"
    assert item["size_bytes"] == 7
    assert item["media_type"] == "image/png"
    assert item["metadata"]["source"] == "upload"
    assert item["metadata"]["original_name"] == "photo.png"


def test_skips_sidecar_files_themselves(client: TestClient, persona_root: Path) -> None:
    image = _seed(persona_root, "uploads/abc.png")
    write_artifact_sidecar(image, _make_meta())

    resp = client.get("/v1/personas/astrid/artifacts", headers=_auth())
    body = resp.json()
    # Only the bytes file; the .f5.json sidecar does NOT appear as an item.
    assert body["total"] == 1
    assert body["items"][0]["ref"] == "uploads/abc.png"


def test_skips_spec_14_meta_json_sidecars(client: TestClient, persona_root: Path) -> None:
    """Regression: Spec 14's document_service.upload writes <bytes>.meta.json
    sidecars. F5's artifact-list endpoint must skip them during enumeration
    so the sidecar itself never appears as an artifact row (Phase 5 discovery)."""
    bytes_path = _seed(persona_root, "uploads/report.pdf")
    spec14_sidecar = bytes_path.parent / f"{bytes_path.name}.meta.json"
    spec14_sidecar.write_text(
        '{"workspace_path": "uploads/report.pdf", "format": "pdf",'
        ' "strategy": "rasterise", "token_count": 1234}',
        encoding="utf-8",
    )
    resp = client.get("/v1/personas/astrid/artifacts", headers=_auth())
    body = resp.json()
    refs = {item["ref"] for item in body["items"]}
    assert refs == {"uploads/report.pdf"}  # Spec 14 sidecar absent.


def test_legacy_files_without_sidecars_surface_with_null_metadata(
    client: TestClient, persona_root: Path
) -> None:
    _seed(persona_root, "uploads/legacy.png")
    resp = client.get("/v1/personas/astrid/artifacts", headers=_auth())
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["metadata"] is None


def test_media_type_derives_from_extension(client: TestClient, persona_root: Path) -> None:
    _seed(persona_root, "uploads/doc.pdf")
    _seed(persona_root, "uploads/data.parquet")
    _seed(persona_root, "uploads/unknown.xyz")

    resp = client.get("/v1/personas/astrid/artifacts", headers=_auth())
    body = resp.json()
    by_ref = {item["ref"]: item["media_type"] for item in body["items"]}
    assert by_ref["uploads/doc.pdf"] == "application/pdf"
    assert by_ref["uploads/data.parquet"] == "application/vnd.apache.parquet"
    assert by_ref["uploads/unknown.xyz"] == "application/octet-stream"


# -- filtering ---------------------------------------------------------------


@pytest.fixture
def populated(persona_root: Path) -> None:
    """Seed a mixed workspace: 2 uploads + 2 generated + 1 legacy + 1 deep tree."""
    img_upload = _seed(persona_root, "uploads/abc.png")
    write_artifact_sidecar(
        img_upload,
        _make_meta(
            source="upload",
            type_="image",
            producing_spec="13",
            conversation_id="conv-1",
            original_name="cat.png",
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        ),
    )
    doc_upload = _seed(persona_root, "uploads/report.pdf")
    write_artifact_sidecar(
        doc_upload,
        _make_meta(
            source="upload",
            type_="doc",
            producing_spec="14",
            conversation_id="conv-1",
            original_name="report.pdf",
            created_at=datetime(2026, 6, 2, tzinfo=UTC),
        ),
    )
    chart_gen = _seed(persona_root, "charts/q3.png")
    write_artifact_sidecar(
        chart_gen,
        _make_meta(
            source="generated",
            type_="chart",
            producing_spec="17",
            conversation_id="conv-2",
            original_name=None,
            created_at=datetime(2026, 6, 3, tzinfo=UTC),
        ),
    )
    img_gen = _seed(persona_root, "uploads/generated_image.png")
    write_artifact_sidecar(
        img_gen,
        _make_meta(
            source="generated",
            type_="image",
            producing_spec="15",
            conversation_id="conv-2",
            original_name=None,
            created_at=datetime(2026, 6, 4, tzinfo=UTC),
        ),
    )
    _seed(persona_root, "uploads/legacy.png")  # no sidecar


def test_filter_by_source_upload(
    client: TestClient,
    persona_root: Path,  # noqa: ARG001 — fixture chain only
    populated: None,  # noqa: ARG001 — fixture chain only
) -> None:
    resp = client.get("/v1/personas/astrid/artifacts?source=upload", headers=_auth())
    body = resp.json()
    assert body["total"] == 2
    sources = {item["metadata"]["source"] for item in body["items"]}
    assert sources == {"upload"}


def test_filter_by_source_excludes_metadata_less_legacy_files(
    client: TestClient,
    persona_root: Path,  # noqa: ARG001 — fixture chain only
    populated: None,  # noqa: ARG001 — fixture chain only
) -> None:
    """Filters that need metadata skip rows without sidecars."""
    resp = client.get("/v1/personas/astrid/artifacts?source=upload", headers=_auth())
    body = resp.json()
    refs = {item["ref"] for item in body["items"]}
    assert "uploads/legacy.png" not in refs


def test_filter_by_type_chart(
    client: TestClient,
    persona_root: Path,  # noqa: ARG001 — fixture chain only
    populated: None,  # noqa: ARG001 — fixture chain only
) -> None:
    resp = client.get("/v1/personas/astrid/artifacts?type=chart", headers=_auth())
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["ref"] == "charts/q3.png"


def test_filter_by_conversation_id(
    client: TestClient,
    persona_root: Path,  # noqa: ARG001 — fixture chain only
    populated: None,  # noqa: ARG001 — fixture chain only
) -> None:
    resp = client.get("/v1/personas/astrid/artifacts?conversation_id=conv-2", headers=_auth())
    body = resp.json()
    assert body["total"] == 2
    convs = {item["metadata"]["conversation_id"] for item in body["items"]}
    assert convs == {"conv-2"}


def test_filter_by_q_matches_original_name(
    client: TestClient,
    persona_root: Path,  # noqa: ARG001 — fixture chain only
    populated: None,  # noqa: ARG001 — fixture chain only
) -> None:
    resp = client.get("/v1/personas/astrid/artifacts?q=cat", headers=_auth())
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["metadata"]["original_name"] == "cat.png"


def test_filter_by_q_matches_ref_path(
    client: TestClient,
    persona_root: Path,  # noqa: ARG001 — fixture chain only
    populated: None,  # noqa: ARG001 — fixture chain only
) -> None:
    """q falls back to the workspace-relative path when no original_name match."""
    resp = client.get("/v1/personas/astrid/artifacts?q=charts", headers=_auth())
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["ref"].startswith("charts/")


def test_filter_by_q_is_case_insensitive(
    client: TestClient,
    persona_root: Path,  # noqa: ARG001 — fixture chain only
    populated: None,  # noqa: ARG001 — fixture chain only
) -> None:
    resp = client.get("/v1/personas/astrid/artifacts?q=CAT", headers=_auth())
    body = resp.json()
    assert body["total"] == 1


def test_filters_combine(
    client: TestClient,
    persona_root: Path,  # noqa: ARG001 — fixture chain only
    populated: None,  # noqa: ARG001 — fixture chain only
) -> None:
    resp = client.get(
        "/v1/personas/astrid/artifacts?source=generated&type=image",
        headers=_auth(),
    )
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["metadata"]["producing_spec"] == "15"


def test_invalid_source_value_is_422(
    client: TestClient,
    persona_root: Path,  # noqa: ARG001 — fixture chain only
    populated: None,  # noqa: ARG001 — fixture chain only
) -> None:
    resp = client.get("/v1/personas/astrid/artifacts?source=invalid", headers=_auth())
    assert resp.status_code == 422


def test_invalid_type_value_is_422(
    client: TestClient,
    persona_root: Path,  # noqa: ARG001 — fixture chain only
    populated: None,  # noqa: ARG001 — fixture chain only
) -> None:
    resp = client.get("/v1/personas/astrid/artifacts?type=video", headers=_auth())
    assert resp.status_code == 422


# -- pagination + Pydantic-Field clamping ------------------------------------


def test_pagination_window(client: TestClient, persona_root: Path) -> None:
    for i in range(10):
        path = _seed(persona_root, f"uploads/f{i}.png")
        write_artifact_sidecar(
            path,
            _make_meta(
                created_at=datetime(2026, 6, i + 1, tzinfo=UTC),
            ),
        )
    resp = client.get("/v1/personas/astrid/artifacts?limit=3&offset=4", headers=_auth())
    body = resp.json()
    assert body["total"] == 10
    assert body["limit"] == 3
    assert body["offset"] == 4
    assert len(body["items"]) == 3


def test_limit_max_200_accepted(client: TestClient) -> None:
    resp = client.get("/v1/personas/astrid/artifacts?limit=200", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["limit"] == 200


def test_limit_over_200_returns_structured_422(
    client: TestClient,
) -> None:
    """D-F5-X-artifact-list-pagination: limit overflow returns structured 422,
    NOT silent truncation."""
    resp = client.get("/v1/personas/astrid/artifacts?limit=10000", headers=_auth())
    assert resp.status_code == 422
    body = resp.json()
    # FastAPI's structured-error shape carries 'detail' as a list of errors.
    assert "detail" in body


def test_limit_zero_is_422(client: TestClient) -> None:
    resp = client.get("/v1/personas/astrid/artifacts?limit=0", headers=_auth())
    assert resp.status_code == 422


def test_negative_offset_is_422(client: TestClient) -> None:
    resp = client.get("/v1/personas/astrid/artifacts?offset=-1", headers=_auth())
    assert resp.status_code == 422


# -- sort order --------------------------------------------------------------


def test_sorted_by_created_at_descending(client: TestClient, persona_root: Path) -> None:
    oldest = _seed(persona_root, "uploads/old.png")
    write_artifact_sidecar(oldest, _make_meta(created_at=datetime(2026, 1, 1, tzinfo=UTC)))
    newest = _seed(persona_root, "uploads/new.png")
    write_artifact_sidecar(newest, _make_meta(created_at=datetime(2026, 12, 31, tzinfo=UTC)))
    middle = _seed(persona_root, "uploads/mid.png")
    write_artifact_sidecar(middle, _make_meta(created_at=datetime(2026, 6, 1, tzinfo=UTC)))

    resp = client.get("/v1/personas/astrid/artifacts", headers=_auth())
    refs = [item["ref"] for item in resp.json()["items"]]
    assert refs == ["uploads/new.png", "uploads/mid.png", "uploads/old.png"]


# -- cross-tenant + RLS ------------------------------------------------------


def test_cross_tenant_persona_returns_404(client: TestClient) -> None:
    resp = client.get("/v1/personas/someone_elses_persona/artifacts", headers=_auth())
    assert resp.status_code == 404


def test_unauthenticated_returns_401(client: TestClient) -> None:
    resp = client.get("/v1/personas/astrid/artifacts")
    assert resp.status_code in (401, 403)


# -- deep tree walk ----------------------------------------------------------


def test_delete_artifact_removes_bytes_and_sidecar(client: TestClient, persona_root: Path) -> None:
    bytes_path = _seed(persona_root, "uploads/abc.png")
    write_artifact_sidecar(bytes_path, _make_meta())

    resp = client.delete("/v1/personas/astrid/artifacts/uploads/abc.png", headers=_auth())
    assert resp.status_code == 204
    assert not bytes_path.is_file()
    sidecar = bytes_path.parent / f"{bytes_path.name}.f5.json"
    assert not sidecar.is_file()


def test_delete_artifact_missing_returns_404(client: TestClient) -> None:
    resp = client.delete("/v1/personas/astrid/artifacts/uploads/nope.png", headers=_auth())
    assert resp.status_code == 404


def test_delete_artifact_cross_tenant_returns_404(client: TestClient) -> None:
    resp = client.delete(
        "/v1/personas/someone_elses_persona/artifacts/uploads/x.png",
        headers=_auth(),
    )
    assert resp.status_code == 404


def test_walks_nested_subdirectories(client: TestClient, persona_root: Path) -> None:
    """Charts may live at charts/<id>.png; intermediate/<x>.parquet etc."""
    chart = _seed(persona_root, "charts/abc.png")
    write_artifact_sidecar(
        chart,
        _make_meta(source="generated", type_="chart", producing_spec="17"),
    )
    _seed(persona_root, "intermediate/data.parquet")
    # No sidecar for intermediate (per D-F4-X-bare-ref-resolution policy).
    resp = client.get("/v1/personas/astrid/artifacts", headers=_auth())
    body = resp.json()
    refs = {item["ref"] for item in body["items"]}
    assert "charts/abc.png" in refs
    assert "intermediate/data.parquet" in refs
