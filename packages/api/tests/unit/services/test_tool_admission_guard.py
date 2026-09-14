"""Structural guard: every tool composed into ``extra_tools`` can actually reach a model.

THE DEFECT CLASS. A tool reaches a model only if it is in
:data:`persona.tools.catalog.TOOL_CATALOG` (so a persona's YAML can name it) or in
:data:`persona.tools._factory.SELF_KNOWLEDGE_TOOLS` (auto-allowed: presence in ``extra_tools`` IS
the authorization). Compose a tool into ``extra_tools`` and put it in NEITHER, and
``build_default_toolbox`` registers it, counts it in ``extra_tool_count``, and then filters
it straight back out for every persona, because ``ensure_default_capabilities`` guarantees a
non-empty allow-list. The tool exists, is composed on every turn, is logged, and no model
ever sees it. It has shipped three times: ``schedule_introspect`` (R9-075),
``task_introspect`` (R9-150) and ``record_user_fact`` (R9-160). The docstring above
``SELF_KNOWLEDGE_TOOLS`` already described the failure in detail before the third
recurrence, which is the point: prose did not stop it, so this file does.

WHAT THE GUARD CATCHES.

1. A tool composed into the ``extra_tools`` list that is in neither admission set
   (:func:`test_every_composed_tool_can_reach_a_model`). This is the defect itself.
2. A NEW composition site this harness does not exercise
   (:func:`test_every_composition_site_is_exercised`). The static count of append sites is
   read out of the composition function's own AST and compared against the number of tools
   the live composition actually produced. Add a tenth site behind a dependency the harness
   does not stub and the counts disagree, so the guard fails rather than quietly checking
   nine of ten tools.
3. A change to the SHAPE of the composition that would invalidate the count
   (:func:`test_the_composed_list_is_only_ever_appended_to`): an ``extend``, an ``insert``,
   a ``+=``, or a rebind of the list.
4. A SECOND composition root that starts passing ``extra_tools``
   (:func:`test_one_composition_root_passes_extra_tools`). The dynamic half only drives the
   runtime factory; a new caller elsewhere in ``packages/*/src`` would be outside its reach,
   so the guard fails until someone teaches it about the new site.
5. Its own discovery going blind (:func:`test_the_guard_sees_a_non_empty_composed_set`). A
   guard that passes when it sees nothing is worse than no guard.

WHAT THE GUARD CANNOT CATCH, stated plainly.

* A tool that is admitted but broken. This is an ADMISSION guard, not a behaviour test.
* Composition that is not a literal ``<list>.append(...)`` inside the composition function:
  a helper that returns tools, or a loop appending a computed list. Test 3 is the tripwire,
  because the shape check fails on anything but a plain append, but a helper called as
  ``extra.append(_pick())`` still counts as one site, which is correct, while
  ``extra.append(x) for x in ...`` inside a comprehension would count one site and produce
  many tools, and test 2 would fail rather than mislead.
* Names computed at runtime. The dynamic half reads ``tool.name`` off the real composed
  objects, so a name assembled at build time IS seen; a name that varies per request is not,
  because only one composition is driven here.
* A call that passes ``extra_tools`` through a ``**kwargs`` splat instead of a written
  keyword. Test 4 reads keywords out of the AST, so a splat is invisible to it and the
  composition behind it would be unguarded.
* MCP tools, bring-your-own tools and the gateway. Those ride their own admission paths
  (``byo_allow`` / ``grant_allow``) inside ``build_default_toolbox`` and are out of scope.
* Any tool reaching a model through a path other than a persona toolbox.

THE INSTANCE. ``record_user_fact`` is fixed here too, and proved end to end rather than by
membership: a persona built through the real composition path must ADVERTISE it. A
membership assertion on the frozenset would pass even if composition stopped injecting it.

No DB: the stores are lazy and every owner-scoped reader resolves per dispatch from the RLS
contextvar, so nothing here touches Postgres.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills import BUILTIN_ROOT, SkillScanner
from persona.tools._factory import SELF_KNOWLEDGE_TOOLS
from persona.tools.catalog import known_tool_names
from persona_api.services import runtime_factory as runtime_factory_module
from persona_api.services.runtime_factory import RuntimeFactory

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persona.tools.protocol import AsyncTool
    from persona.tools.toolbox import Toolbox

_INSTANCE = "record_user_fact"

#: The composition function the guard reads, statically and dynamically. Held as the
#: unbound function so a rename moves the guard with it instead of silently unhooking it.
_COMPOSITION = RuntimeFactory._build_toolbox  # noqa: SLF001 — the composition IS the subject


# --------------------------------------------------------------------------------------
# Static half: discover the composition sites from the composition code's own AST.
# --------------------------------------------------------------------------------------


def _repo_root() -> Path:
    """The workspace root, found by walking up from this file.

    Deliberately not derived from an installed package location: the guard scans source
    trees, so it needs the checkout it is running against, worktree or canonical alike.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "packages").is_dir():
            return parent
    raise AssertionError("could not locate the workspace root from the guard's own path")


def _composition_ast() -> ast.AsyncFunctionDef | ast.FunctionDef:
    """The composition function's syntax tree, parsed from its live source."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(_COMPOSITION)))
    node = tree.body[0]
    assert isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef), type(node)
    return node


def _composed_list_name(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> str:
    """The local variable the composition passes as ``extra_tools``.

    Read from the call rather than hard-coded, so renaming the local does not quietly
    point the guard at a variable nobody composes into any more.
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "extra_tools":
                continue
            names = [n.id for n in ast.walk(keyword.value) if isinstance(n, ast.Name)]
            assert names, f"extra_tools= is not passed a named local: {ast.dump(keyword.value)}"
            return names[0]
    raise AssertionError(f"{_COMPOSITION.__qualname__} no longer passes extra_tools=")


def _method_calls_on(fn: ast.AsyncFunctionDef | ast.FunctionDef, var: str) -> list[str]:
    """Every method called on ``var`` inside ``fn``, in source order."""
    return [
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == var
    ]


def _bindings_of(fn: ast.AsyncFunctionDef | ast.FunctionDef, var: str) -> int:
    """How many times ``var`` is assigned (including augmented assignment) inside ``fn``."""
    count = 0
    for node in ast.walk(fn):
        if isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets: list[ast.expr] = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            continue
        count += sum(1 for t in targets if isinstance(t, ast.Name) and t.id == var)
    return count


def _source_files() -> Iterator[Path]:
    """Every product source file in the workspace (tests excluded by construction)."""
    yield from sorted(_repo_root().glob("packages/*/src/**/*.py"))


def _files_passing_extra_tools() -> set[Path]:
    """Every product source file with a call that passes an ``extra_tools`` keyword."""
    found: set[Path] = set()
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(k.arg == "extra_tools" for k in node.keywords):
                found.add(path)
    return found


# --------------------------------------------------------------------------------------
# Dynamic half: drive the real composition with every optional dependency present.
# --------------------------------------------------------------------------------------


class _StubBackend:
    """Stands in for a chat / image backend; the tools only hold the reference."""


class _StubTierRegistry:
    """A tier registry that resolves every tier, so the summarize site composes."""

    def get(self, tier: str) -> _StubBackend:  # noqa: ARG002 — every tier resolves
        return _StubBackend()


class _StubSandbox:
    """Stands in for the sandbox substrate the code-execution tool dispatches into."""


class _StubPool:
    """Stands in for the hosted sandbox pool; acquired per dispatch, never at build."""

    sandbox = _StubSandbox()


class _StubFileStorage:
    """Stands in for the artifact storage the workspace persister writes through."""


class _StubGraphStore:
    """Stands in for the user-scoped graph store ``record_user_fact`` writes into."""


def _persona(*, tools: list[str]) -> Persona:
    """A persona with a realistic non-empty allow-list that names none of the extras."""
    return Persona(
        persona_id="persona_tool_admission_guard",
        identity=PersonaIdentity(
            name="Astrid",
            role="assistant",
            background="A helper for tool-admission guard tests.",
        ),
        tools=tools,
        skills=["web_research"],
    )


def _fully_composed_factory(tmp_path: Path) -> RuntimeFactory:
    """A factory with EVERY optional dependency present, so every site composes.

    Each stub stands in for a dependency whose absence makes the corresponding tool absent
    (the documented graceful-absence shape). The point of the guard is to see the whole
    composed surface at once, which a production factory has and a minimal one does not.
    """
    factory = RuntimeFactory(
        rls_engine=None,  # type: ignore[arg-type]
        embedder=None,  # type: ignore[arg-type]
        tier_registry=_StubTierRegistry(),  # type: ignore[arg-type]
        turn_log_writer=None,  # type: ignore[arg-type]
        audit_root=tmp_path / "audit",
        file_storage=_StubFileStorage(),  # type: ignore[arg-type]
        sandbox_pool=_StubPool(),  # type: ignore[arg-type]
        workspace_root=tmp_path / "workspaces",
        image_backend=_StubBackend(),  # type: ignore[arg-type]
    )
    # ``enable_graph_writes`` needs a live Postgres engine to build the real store, and the
    # guard is a unit test; the composition branch it feeds is what matters here.
    factory._graph_store = _StubGraphStore()  # type: ignore[assignment]  # noqa: SLF001
    return factory


def _scanned_skills() -> list[object]:
    """Real scanned skills, so the ``use_skill`` composition site fires."""
    scanned = SkillScanner(skill_paths=[BUILTIN_ROOT]).scan(
        declared_skills=["web_research"], tool_allow_list=None
    )
    assert scanned, "the bundled skills did not scan; the use_skill site cannot be exercised"
    return list(scanned)


async def _compose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, tools: list[str]
) -> tuple[list[AsyncTool], Toolbox]:
    """Run the real composition, capturing what it hands to ``build_default_toolbox``.

    The spy delegates to the real function, so the returned Toolbox is the one production
    would get: the guard reads the composed list AND the toolbox it produced from one run.
    """
    captured: list[list[AsyncTool]] = []
    real = runtime_factory_module.build_default_toolbox

    async def _spy(*args: object, **kwargs: object) -> object:
        captured.append(list(cast("list[AsyncTool] | None", kwargs.get("extra_tools")) or []))
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime_factory_module, "build_default_toolbox", _spy)
    toolbox = await _fully_composed_factory(tmp_path)._build_toolbox(  # noqa: SLF001
        _persona(tools=tools), _scanned_skills()
    )
    assert len(captured) == 1, f"expected one composition, saw {len(captured)}"
    return captured[0], toolbox


# --------------------------------------------------------------------------------------
# The guard.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_guard_sees_a_non_empty_composed_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Discovery is alive. A guard that passes on an empty set proves nothing at all.

    This is the mutation that matters most: break the capture and every other assertion in
    this file goes vacuously green while the defect class walks straight back in.
    """
    composed, _ = await _compose(monkeypatch, tmp_path, tools=["file_read"])

    assert composed, "the guard discovered NO composed tools, so it is checking nothing"
    assert all(t.name for t in composed), "a composed tool has no name to admit"


@pytest.mark.asyncio
async def test_every_composed_tool_can_reach_a_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """THE GUARD. Every composed tool is in the catalog or auto-allowed.

    A tool in neither is registered, counted, and filtered straight back out for every
    persona, so it is dead on arrival no matter how correct its own code is.
    """
    composed, _ = await _compose(monkeypatch, tmp_path, tools=["file_read"])
    admitted = known_tool_names() | SELF_KNOWLEDGE_TOOLS

    unreachable = sorted({t.name for t in composed} - admitted)
    assert not unreachable, (
        f"composed but unreachable: {unreachable}. Each of these is registered by "
        "build_default_toolbox and then filtered out for every persona, because it is in "
        "neither TOOL_CATALOG (nameable in a persona's YAML) nor SELF_KNOWLEDGE_TOOLS "
        "(auto-allowed). Add it to one: the catalog when a user should see and grant it, "
        "the auto-allow set when its presence IS the authorization."
    )


@pytest.mark.asyncio
async def test_every_composition_site_is_exercised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The harness reaches every site the composition code actually has.

    The count comes from the composition function's own AST, never a hand-maintained list,
    so a site added behind a dependency this harness does not stub fails here instead of
    slipping past a guard that silently checks a subset.
    """
    fn = _composition_ast()
    sites = _method_calls_on(fn, _composed_list_name(fn)).count("append")
    composed, _ = await _compose(monkeypatch, tmp_path, tools=["file_read"])

    assert len(composed) == sites, (
        f"{sites} composition sites in {_COMPOSITION.__qualname__} but {len(composed)} tools "
        f"composed here ({sorted(t.name for t in composed)}). A site this harness cannot "
        "reach is a tool the admission guard never checks. Stub whatever dependency gates "
        "the new site in _fully_composed_factory so the guard sees it."
    )


def test_the_composed_list_is_only_ever_appended_to() -> None:
    """The shape the site count depends on: one binding, appends and nothing else.

    An ``extend`` or a comprehension would make one site contribute an unknown number of
    tools, and the count above would stop meaning what it says.
    """
    fn = _composition_ast()
    var = _composed_list_name(fn)
    calls = sorted(set(_method_calls_on(fn, var)))

    assert calls == ["append"], (
        f"{var} is mutated by {calls} in {_COMPOSITION.__qualname__}, not append alone. The "
        "site count in test_every_composition_site_is_exercised assumes one tool per site; "
        "teach it the new shape before using one."
    )
    assert _bindings_of(fn, var) == 1, (
        f"{var} is bound {_bindings_of(fn, var)} times; a rebind can drop composed tools "
        "before they are ever handed to build_default_toolbox."
    )


def test_one_composition_root_passes_extra_tools() -> None:
    """The runtime factory is the only product caller that composes extra tools.

    The dynamic half drives that one composition root. A second caller anywhere under
    ``packages/*/src`` would compose tools this guard never sees, so it fails here until
    someone extends the harness to drive it too.
    """
    known = Path(inspect.getsourcefile(RuntimeFactory) or "").resolve()
    found = {p.resolve() for p in _files_passing_extra_tools()}

    assert found == {known}, (
        f"extra_tools is passed from {sorted(str(p) for p in found)}, not only {known}. "
        "The admission guard only drives the runtime factory, so any other composition "
        "root is unguarded: drive it here as well."
    )


# --------------------------------------------------------------------------------------
# The instance: record_user_fact, proved end to end rather than by membership.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_user_fact_is_advertised_to_the_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R9-160 / R9-159: a persona asked to remember something has the tool to do it.

    Asserted through the REAL composition path, not against the frozenset: membership alone
    would stay green if composition stopped injecting the tool, which is the other half of
    the same failure. The allow-list here is a realistic one, and like every real one it
    never names ``record_user_fact`` (it is absent from the catalog, so no YAML can).
    """
    tools = ["file_read", "file_write", "web_search"]
    composed, toolbox = await _compose(monkeypatch, tmp_path, tools=tools)

    assert _INSTANCE in {t.name for t in composed}, "not composed; the fixture is not wired"
    assert _INSTANCE not in tools  # no persona's YAML names it
    assert toolbox.is_allowed(_INSTANCE), f"{_INSTANCE} is registered but gated out"
    assert _INSTANCE in toolbox.names(), (
        f"{_INSTANCE} never reaches the model, so 'save that into my memory' is answered "
        "by reaching for a filesystem write instead (R9-159)"
    )
    assert "file_read" in toolbox.names()  # sanity: wires not crossed


def test_record_user_fact_is_auto_allowed_not_a_catalog_entry() -> None:
    """The owner's ruling, pinned: auto-allowed, joining the existing four.

    Not a catalog entry a user grants per persona. The tradeoff accepted is that the user
    does not separately withhold it; the write still lands in that user's own memory and
    carries an audit reason.
    """
    assert _INSTANCE in SELF_KNOWLEDGE_TOOLS
    assert _INSTANCE not in known_tool_names()
