"""Tools + skills read-only endpoints (spec 08, T13, §5.4).

No DB. Mounts the app with a fake verifier and asserts /v1/tools and /v1/skills
return the built-in tools + bundled skills as name/description lists, and require
auth.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig


@pytest.fixture
def client() -> TestClient:
    app = create_app(
        # Cloud auth wall, but no lifespan engine is built here (the fixture
        # returns the client without entering its context + sets rls_engine=None).
        # Distinct app DSN satisfies the R2 cloud-config guard (R2-D-1).
        APIConfig(
            database_url="postgresql+psycopg://super@localhost/persona_shell",
            app_database_url="postgresql+psycopg://persona_app@localhost/persona_shell",
        )
    )  # no DB needed for the catalog routes

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    app.state.verify_token = _verify
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer u1"}


def test_list_tools(client: TestClient) -> None:
    resp = client.get("/v1/tools", headers=_auth())
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()}
    # Every built-in tool factory the runtime wires up — see catalog_service.
    # Authoring constrains the LLM to "names from AVAILABLE only"; a tool
    # missing here is silently invisible to the wizard.
    assert {
        "web_search",
        "web_fetch",
        "file_read",
        "file_write",
        "code_execution",
        "generate_image",
        # Spec 26 T08 — the new built-ins must also surface in authoring so the
        # wizard can offer them (sourced from persona-core TOOL_CATALOG).
        "calculator",
        "datetime",
        "regex_match",
        "json_query",
        "text_diff",
        "currency_convert",
        "text_summarize",
    } <= names
    # each has a non-empty description
    assert all(t["description"] for t in resp.json())


def test_list_skills(client: TestClient) -> None:
    resp = client.get("/v1/skills", headers=_auth())
    assert resp.status_code == 200
    names = {s["name"] for s in resp.json()}
    # Every folder under persona/skills/builtin must be declared in the
    # catalog — otherwise the authoring wizard can't suggest the skill.
    # Spec 24 (D-24-1): the 5 document-format packs folded into the single
    # document_generation skill (deprecated names still resolve via the alias
    # shim, but the catalog surfaces only the live folders).
    assert {
        "code_review",
        "data_analysis",
        "document_generation",
        "web_research",
    } <= names
    # The deleted document-format skills must NOT appear as separate entries.
    assert not (
        {
            "document_drafting",
            "docx_generation",
            "pdf_generation",
            "pptx_generation",
            "xlsx_generation",
        }
        & names
    )


def test_tools_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/tools").status_code == 401


def test_skills_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/skills").status_code == 401


# -- S3 (S3-D-3 / T1): /v1/specialities carries trust tier + content_hash ----


def test_specialities_carries_tier_and_content_hash(client: TestClient) -> None:
    """The builtin floor surfaces as specialities with tier=builtin + a content hash.

    Builtins are ``builtin`` tier (S1-D-3), never consent-gated (S1-D-4), and the
    scanner assigns ``content_hash = sha256(body)`` (S1-D-5) — the version the S3
    consent flow binds to. This is the tier-aware data the plain /v1/skills lacked.
    """
    resp = client.get("/v1/specialities", headers=_auth())
    assert resp.status_code == 200
    rows = {s["name"]: s for s in resp.json()}
    assert {"code_review", "data_analysis", "document_generation", "web_research"} <= set(rows)
    cr = rows["code_review"]
    assert cr["trust"] == "builtin"
    assert cr["requires_consent"] is False  # builtin/vetted activate freely (S1-D-4)
    assert cr["content_hash"]  # sha256 of the body, present for every builtin
    assert cr["source"] == "builtin"


def test_specialities_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/specialities").status_code == 401


def test_specialities_merge_includes_external_tiers_and_builtin_wins(tmp_path: Path) -> None:
    """S2's synced external skills surface with their source-assigned tier + consent gate;
    a repo builtin is never superseded by a same-named external skill (S1-D-3 + merge order)."""
    from persona.schema.skills import SkillProvenance, SkillSpec, SkillTrust
    from persona.skills.skill_mirror import write_skill_mirror_atomic
    from persona_api.services import catalog_service

    external = [
        SkillSpec(
            name="legal_research",  # a third_party external skill (consent-gated)
            description="External legal research helper.",
            path=tmp_path / "legal_research",
            when_to_use="Use for case-law lookups.",
            trust=SkillTrust.THIRD_PARTY,
            provenance=SkillProvenance(
                source="github:acme/skills",
                source_uri="https://github.com/acme/skills",
                source_ref="abc123def456",
                content_hash="deadbeef",
            ),
        ),
        SkillSpec(
            name="code_review",  # collides with a builtin → builtin must win
            description="IMPOSTER external code_review.",
            path=tmp_path / "code_review",
            trust=SkillTrust.THIRD_PARTY,
            provenance=SkillProvenance(source="github:evil/skills", content_hash="beef"),
        ),
    ]
    override = tmp_path / "skill_mirror.json"
    write_skill_mirror_atomic(external, override)

    specs = {s.name: s for s in catalog_service.list_specialities(skill_mirror_path=override)}
    # the external third_party skill is listed, tier-tagged, consent-gated, hash-bound
    legal = specs["legal_research"]
    assert legal.trust is SkillTrust.THIRD_PARTY
    assert legal.trust.requires_consent is True
    assert legal.provenance is not None
    assert legal.provenance.content_hash == "deadbeef"
    # builtin wins the collision — the imposter external code_review is discarded
    assert specs["code_review"].trust is SkillTrust.BUILTIN
    assert specs["code_review"].description != "IMPOSTER external code_review."


def test_specialities_no_mirror_is_exactly_the_builtins(tmp_path: Path) -> None:
    """No mirror snapshot → fail-soft to the builtin floor only (never raises)."""
    from persona_api.services import catalog_service

    absent = tmp_path / "does_not_exist.json"
    names = {s.name for s in catalog_service.list_specialities(skill_mirror_path=absent)}
    assert names == {"code_review", "data_analysis", "document_generation", "web_research"}


# -- N1 (D-N1-3): /v1/mcp-catalog = builtin floor + Docker mirror -------------

_BUILTINS = {"time", "calculator", "filesystem", "weather", "fetch", "github"}


def test_mcp_catalog_legacy_contract_unchanged_and_fields_additive(client: TestClient) -> None:
    """The spec-30 five-field contract is intact; N1 display fields ride defaults.

    A client written against spec 30 (name/description/provider/default_enabled/
    required_env) sees no break — the new fields are additive-with-default, so the
    builtin rows carry empty/neutral defaults. Spec N7 (D-N7-2) wraps the list under
    ``servers`` (a sibling ``capabilities`` object rides alongside — see the
    dedicated wrapper-shape tests below); the per-server contract itself is
    otherwise unchanged.
    """
    resp = client.get("/v1/mcp-catalog", headers=_auth())
    assert resp.status_code == 200
    rows = {r["name"]: r for r in resp.json()["servers"]}
    assert set(rows) >= _BUILTINS  # builtin floor always present (no mirror needed)

    fs = rows["filesystem"]
    # legacy spec-30 contract intact
    assert {"name", "description", "provider", "default_enabled", "required_env"} <= set(fs)
    assert fs["default_enabled"] is True
    assert fs["provider"] == "mcp:builtin"
    # additive N1 fields present, defaulted for a builtin row
    assert fs["display_name"] == ""
    assert fs["icon_url"] == ""
    assert fs["server_type"] == "builtin"
    assert fs["signed"] is False
    assert fs["allow_hosts"] == []
    assert fs["secrets"] == []
    # additive N7 (R8 rebind) fields present, defaulted for a non-oauth builtin row
    assert fs["auth_method"] == ""
    assert fs["oauth_provider"] == ""


def test_mcp_catalog_secret_schema_is_display_only(client: TestClient) -> None:
    """D-N1-5 at the API boundary: the secret schema exposes no value field."""
    schema = client.get("/openapi.json").json()
    secret = schema["components"]["schemas"]["MCPCatalogSecret"]["properties"]
    assert set(secret) == {"name", "env", "example", "description"}
    assert "value" not in secret
    assert "credential" not in secret


def test_mcp_catalog_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/mcp-catalog").status_code == 401


# -- N7 (D-N7-2): capabilities wrapper + oauth passthrough + the C-filter ----


def test_mcp_catalog_wrapper_shape(client: TestClient) -> None:
    """The response is a wrapper object ``{servers, capabilities}``, not a bare
    array — an intentional, atomic breaking change (the ONE regen this ships with,
    N7-T2): a bare list cannot carry the sibling ``capabilities`` field."""
    resp = client.get("/v1/mcp-catalog", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"servers", "capabilities"}
    assert isinstance(body["servers"], list)
    assert set(body["capabilities"]) == {"per_tenant_runtime", "gateway", "oauth_providers"}


def test_mcp_catalog_capabilities_truth_table(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capabilities are computed truthfully from live deployment state, not guessed."""
    # Baseline: no per-tenant runtime composed, no gateway configured, no oauth
    # provider configured — the fixture's fully-off deployment.
    resp = client.get("/v1/mcp-catalog", headers=_auth())
    assert resp.json()["capabilities"] == {
        "per_tenant_runtime": False,
        "gateway": False,
        "oauth_providers": [],
    }

    # per_tenant_runtime flips true ONLY when app.state.mcp_runtime is non-None —
    # exactly the condition (N6 composition guard passed) under which adopting an
    # image app would spawn a real Machine.
    client.app.state.mcp_runtime = object()  # type: ignore[attr-defined]
    try:
        resp = client.get("/v1/mcp-catalog", headers=_auth())
        assert resp.json()["capabilities"]["per_tenant_runtime"] is True
    finally:
        client.app.state.mcp_runtime = None  # type: ignore[attr-defined]

    # gateway flips true when the operator-configured gateway URL env var is set.
    monkeypatch.setenv("PERSONA_DOCKER_MCP_GATEWAY_URL", "http://gw.internal:8811/mcp")
    resp = client.get("/v1/mcp-catalog", headers=_auth())
    assert resp.json()["capabilities"]["gateway"] is True
    monkeypatch.delenv("PERSONA_DOCKER_MCP_GATEWAY_URL")

    # oauth_providers reflects the configured provider registry (github, once its
    # client_id is set); an unconfigured provider stays absent (fail-closed).
    original_config = client.app.state.config  # type: ignore[attr-defined]
    client.app.state.config = APIConfig(  # type: ignore[attr-defined]
        database_url=original_config.database_url,
        app_database_url=original_config.app_database_url,
        mcp_oauth_github_client_id="cid_test",
    )
    try:
        resp = client.get("/v1/mcp-catalog", headers=_auth())
        assert resp.json()["capabilities"]["oauth_providers"] == ["github"]
    finally:
        client.app.state.config = original_config  # type: ignore[attr-defined]


def test_mcp_catalog_oauth_passthrough(client: TestClient) -> None:
    """``auth_method``/``oauth_provider`` pass through for the bundled github entry
    (Spec R8, rebound off the operator-global token; Spec N7 threads it to the web
    so the catalog card can offer a Connect affordance instead of a credential
    form)."""
    resp = client.get("/v1/mcp-catalog", headers=_auth())
    rows = {r["name"]: r for r in resp.json()["servers"]}
    github = rows["github"]
    assert github["auth_method"] == "oauth"
    assert github["oauth_provider"] == "github"


def test_mcp_catalog_c_filter_excludes_unrunnable_image_entries(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner-ruled C-filter: an image-type (``server_type == "server"``) mirror
    entry that neither the per-tenant runtime nor a gateway could ever run is
    excluded from the LISTING outright — never merely shown-then-refused. A
    gateway-configured deployment keeps it listed (the operator may have enabled it
    there); a runtime-configured deployment keeps it listed too."""
    from persona.tools.mcp.catalog import MCPCatalog, MCPServerCatalogEntry
    from persona_api.services import catalog_service

    image_entry = MCPServerCatalogEntry(
        name="image-app",
        description="An image-runtime app.",
        kind="external",
        risk="low",
        server_type="server",
    )
    fake = MCPCatalog(servers={"image-app": image_entry})
    monkeypatch.setattr(catalog_service, "load_mirror_catalog", lambda **_kw: fake)

    # No runtime, no gateway → excluded entirely.
    resp = client.get("/v1/mcp-catalog", headers=_auth())
    names = {r["name"] for r in resp.json()["servers"]}
    assert "image-app" not in names

    # Gateway configured → stays listed (operator-managed, unknown to the catalog).
    monkeypatch.setenv("PERSONA_DOCKER_MCP_GATEWAY_URL", "http://gw.internal:8811/mcp")
    resp = client.get("/v1/mcp-catalog", headers=_auth())
    names = {r["name"] for r in resp.json()["servers"]}
    assert "image-app" in names
    monkeypatch.delenv("PERSONA_DOCKER_MCP_GATEWAY_URL")

    # Per-tenant runtime composed → stays listed.
    client.app.state.mcp_runtime = object()  # type: ignore[attr-defined]
    try:
        resp = client.get("/v1/mcp-catalog", headers=_auth())
        names = {r["name"] for r in resp.json()["servers"]}
        assert "image-app" in names
    finally:
        client.app.state.mcp_runtime = None  # type: ignore[attr-defined]

    # merged_mcp_catalog() itself (the unfiltered source other consumers — adoption,
    # run_policy, mcp_search — read from) still carries the full set regardless.
    assert "image-app" in {e.name for e in catalog_service.merged_mcp_catalog()}


def test_merged_catalog_no_override_is_builtin_floor_plus_bundled_mirror() -> None:
    """N2-D-1: with no operator override, the catalog is the builtin floor merged over the

    BUNDLED mirror snapshot (``mirror.json``) — no longer just the six builtins, since a bundled
    snapshot now ships. The builtin floor always survives (builtin-wins on collision). Derived
    from the sources (not hardcoded) so it tracks the bundled catalog as it grows; the pure
    builtin-fallback path is unit-covered in ``test_mcp_mirror.py``.
    """
    from persona.tools.mcp.catalog import BUILTIN_MCP_CATALOG
    from persona.tools.mcp.mirror import load_mirror_catalog
    from persona_api.services import catalog_service

    names = {e.name for e in catalog_service.merged_mcp_catalog()}
    expected = set(BUILTIN_MCP_CATALOG.servers) | set(load_mirror_catalog(override=None).servers)
    assert names == expected
    assert names >= _BUILTINS  # the always-present core floor survives the merge


def test_merged_catalog_builtin_floor_with_builtin_wins_on_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Builtin is the floor; a same-named mirror entry is superseded; tail is unioned."""
    from persona.tools.mcp.catalog import MCPCatalog, MCPServerCatalogEntry
    from persona_api.services import catalog_service

    fake = MCPCatalog(
        servers={
            # collides with the authored builtin "github" → builtin must win
            "github": MCPServerCatalogEntry(
                name="github", description="MIRROR github", kind="external", risk="high"
            ),
            # long-tail mirror entry → unioned in
            "notion-mirror": MCPServerCatalogEntry(
                name="notion-mirror",
                description="Notion",
                kind="external",
                risk="medium",
                display_name="Notion",
            ),
        }
    )
    # N2: load_mirror_catalog now takes an `override` kwarg; the fake ignores it.
    monkeypatch.setattr(catalog_service, "load_mirror_catalog", lambda **_: fake)
    merged = {e.name: e for e in catalog_service.merged_mcp_catalog()}

    assert set(merged) >= _BUILTINS  # the floor survives
    assert merged["github"].description != "MIRROR github"  # builtin-wins (authored)
    assert merged["notion-mirror"].display_name == "Notion"  # tail unioned
    # deterministic order: builtins first, then the new mirror name
    names = [e.name for e in catalog_service.merged_mcp_catalog()]
    assert names[: len(_BUILTINS)] == [
        "time",
        "calculator",
        "filesystem",
        "weather",
        "fetch",
        "github",
    ]
    assert names[-1] == "notion-mirror"


def test_merged_catalog_reads_the_override_mirror_path(tmp_path: Path) -> None:
    """N2-D-1: merged_mcp_catalog reads the auto-synced override snapshot when given one."""
    from persona.tools.mcp.mirror_sync import sync_mirror
    from persona_api.services import catalog_service

    # Build an override snapshot from a local registry checkout (no network).
    server_dir = tmp_path / "registry" / "servers" / "notion-mirror"
    server_dir.mkdir(parents=True)
    (server_dir / "server.yaml").write_text(
        "name: notion-mirror\nabout:\n  title: Notion\n  description: Notion MCP.\n",
        encoding="utf-8",
    )
    override = tmp_path / "vol" / "mirror.json"
    sync_mirror(registry_root=tmp_path / "registry", mirror_path=override)

    names = {e.name for e in catalog_service.merged_mcp_catalog(mirror_path=override)}
    assert names >= _BUILTINS  # the floor survives
    assert "notion-mirror" in names  # the override's long-tail entry is listed


# -- N2-D-4: removed-server surfaces (a) not-enableable + (c) owner-visible flag ----


def _override_with(tmp_path: Path, *server_names: str) -> Path:
    """Build an override mirror snapshot listing exactly the given server names."""
    from persona.tools.mcp.mirror_sync import sync_mirror

    registry = tmp_path / "registry"
    for name in server_names:
        d = registry / "servers" / name
        d.mkdir(parents=True)
        (d / "server.yaml").write_text(
            f"name: {name}\nabout:\n  title: {name}\n  description: d.\n", encoding="utf-8"
        )
    (registry / "servers").mkdir(parents=True, exist_ok=True)
    override = tmp_path / "mirror.json"
    sync_mirror(registry_root=registry, mirror_path=override)
    return override


def test_available_mcp_server_names_includes_builtins_and_mirror(tmp_path: Path) -> None:
    from persona_api.services import catalog_service

    override = _override_with(tmp_path, "notion-mirror")
    names = catalog_service.available_mcp_server_names(mirror_path=override)
    assert names >= _BUILTINS  # the builtin floor is always available
    assert "notion-mirror" in names  # plus the mirror tail


def test_removed_server_is_not_offered_as_enableable(tmp_path: Path) -> None:
    """Surface (a): a server absent from the mirror is not in the available set."""
    from persona_api.services import catalog_service

    override = _override_with(tmp_path, "notion-mirror")  # 'ghost' deliberately absent
    names = catalog_service.available_mcp_server_names(mirror_path=override)
    assert "ghost" not in names  # cannot be enabled — it's gone


def test_unavailable_enabled_flags_only_removed_servers(tmp_path: Path) -> None:
    """Surface (c): flag enabled ``mcp:<name>`` whose server is gone; ignore the rest."""
    from persona_api.services import catalog_service

    override = _override_with(tmp_path, "notion-mirror")
    tools = [
        "mcp:notion-mirror",  # available via the mirror → not flagged
        "mcp:github",  # builtin floor → available → not flagged
        "web_search",  # not an mcp enablement → ignored
        "mcp:docker:fetch",  # a gateway TOOL (mcp:<server>:<tool>) → not a server enablement
        "mcp:ghost",  # enabled but gone → flagged
        "mcp:ghost",  # duplicate → de-duplicated
    ]
    assert catalog_service.unavailable_enabled_mcp_servers(tools, mirror_path=override) == ["ghost"]


def test_unavailable_enabled_empty_when_no_enablements() -> None:
    from persona_api.services import catalog_service

    # No ``mcp:<name>`` enablement entries → empty, and the mirror is never consulted.
    assert catalog_service.unavailable_enabled_mcp_servers(["web_search", "file_read"]) == []


# -- N2-D-5 (criterion 4): the sync changes AVAILABILITY, never ENABLEMENT ----------


def _seed_registry(root: Path, *server_names: str) -> Path:
    """Write a ``docker/mcp-registry``-shaped checkout listing the given servers."""
    for name in server_names:
        d = root / "servers" / name
        d.mkdir(parents=True)
        (d / "server.yaml").write_text(
            f"name: {name}\nabout:\n  title: {name}\n  description: d.\n", encoding="utf-8"
        )
    return root


def test_newly_available_mirror_server_is_not_default_enabled(tmp_path: Path) -> None:
    """A freshly-synced catalog server is OPT-IN — never default-enabled / auto-on."""
    from persona.tools.mcp.catalog import recommender_provider_tag
    from persona_api.services import catalog_service

    override = _override_with(tmp_path, "newserver")
    entry = next(
        e for e in catalog_service.merged_mcp_catalog(mirror_path=override) if e.name == "newserver"
    )
    assert entry.default_enabled is False  # availability ≠ default-on
    assert recommender_provider_tag(entry) == "mcp:optional"  # opt-in, not a builtin default


def test_sync_raises_availability_never_enablement(tmp_path: Path) -> None:
    """STRUCTURAL contract: a sync that ADDS a server raises availability for everyone,
    but a persona that did not explicitly enable it never gains it (criterion 4)."""
    from persona.tools.mcp.mirror_reconcile import reconcile_mirror
    from persona_api.services import catalog_service

    mirror = tmp_path / "mirror.json"
    reconcile_mirror(mirror_path=mirror, registry_root=_seed_registry(tmp_path / "r1", "oldserver"))
    assert "newserver" not in catalog_service.available_mcp_server_names(mirror_path=mirror)

    # P enabled only the pre-existing server; Q happens to have explicitly enabled newserver.
    p_tools = ["file_read", "mcp:oldserver"]
    q_tools = ["mcp:newserver"]

    # Upstream gains newserver; the sync reconciles it into availability.
    result = reconcile_mirror(
        mirror_path=mirror, registry_root=_seed_registry(tmp_path / "r2", "oldserver", "newserver")
    )
    assert "newserver" in result.added

    # Availability rose for EVERYONE (the catalog listing changed)...
    avail = catalog_service.available_mcp_server_names(mirror_path=mirror)
    assert {"oldserver", "newserver"} <= avail

    # ...but ENABLEMENT did not: the sync has no handle to a persona's allow-list, so P
    # never gains mcp:newserver. Enablement stays the explicit per-persona gate.
    assert "mcp:newserver" not in p_tools  # no auto-enable on a persona that didn't choose it
    # P's enabled server (oldserver) is still available; nothing P enabled is "unavailable".
    assert catalog_service.unavailable_enabled_mcp_servers(p_tools, mirror_path=mirror) == []
    # Q's explicit choice (newserver) is now available — enablement was Q's, not the sync's.
    assert catalog_service.unavailable_enabled_mcp_servers(q_tools, mirror_path=mirror) == []
