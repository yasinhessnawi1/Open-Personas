"""Spec P4 — operator pass. Live runtime, real keys, real scoped subprocesses.

Drives the WHOLE chain the integration tests cannot: a persona with filesystem
tools → a REAL model deciding to call them → a per-(owner, persona) SCOPED
filesystem MCP subprocess → write → read → cross-owner-deny → traversal-recovery.
Two owners share one workspace root; each gets its own scoped child via
``resolve(filesystem_scope_root=<ws>/<owner>/<persona>)`` — exactly what
``RuntimeFactory._resolve_filesystem_scope_root`` computes in prod.

The model-facing ``write_file`` / ``read_file`` tools route each call to that
owner's scoped child over a FRESH short-lived ``MCPClient`` (connect → dispatch →
disconnect within one ``execute()`` — the op27-proven pattern). Holding a
streamable-HTTP client open across a ConversationLoop's turns is unstable under a
pytest event loop (its anyio cancel scope poisons later awaits / leaks on GC); a
per-call connection sidesteps that entirely. The cached scoped subprocess is the
real thing being exercised — the per-call client is just the transport to it.

Each tool execution gets exactly one disposition (PASS / KNOWN-LIMITATION /
FAIL). The cross-owner isolation claim is additionally corroborated by a direct
transport check so it is never vacuous if a live model declines a tool call.
Writes a dispositioned transcript to evidence/ and asserts zero FAIL.

Marked ``external`` (real APIs + network + subprocesses) — skipped by default;
run with: ``uv run pytest -m external -k p4_operator -s``. Requires the project
.env (PERSONA_* provider keys); this module loads it, never echoes it.
"""

# ruff: noqa: E501, ANN202, ANN001, ANN003, BLE001 — harness test: dense disposition lines + nested helpers.
from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from persona.config import PersonaCoreConfig
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.tools import ToolResult
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona.tools.mcp.client import MCPClient
from persona.tools.protocol import tool
from persona_api.mcp import BuiltinMCPSupervisor
from persona_runtime.logging import MemoryTurnLogWriter
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import tier_registry_from_env

pytestmark = [pytest.mark.external, pytest.mark.asyncio]

_EVIDENCE = Path(__file__).resolve().parents[4] / "docs/specs/phase3/spec_P4/evidence"
_NONCE = "A-SECRET-OSLO-42"
_SECRET_PATH = "out/secret.txt"


class _Stub:  # minimal in-memory MemoryStore double (memory is orthogonal)
    def __init__(self) -> None:
        self._all: list = []

    def write(self, *a, **k) -> None:  # noqa: ANN002,ARG002
        self._all.extend(a[1] if len(a) > 1 else [])

    def query(self, *a, **k):  # noqa: ANN002,ARG002
        return []

    def recent(self, *a, **k):  # noqa: ANN002,ARG002
        return []

    def get_all(self, *a, **k):  # noqa: ANN002,ARG002
        return list(self._all)

    def delete(self, *a, **k) -> None: ...  # noqa: ANN002
    def remove_documents(self, *a, **k) -> None: ...  # noqa: ANN002
    def history(self, *a, **k):  # noqa: ANN002,ARG002
        return []

    def rollback(self, *a, **k) -> None: ...  # noqa: ANN002


async def _call_scoped_child(url: str, tool_name: str, **kwargs) -> ToolResult:
    """One filesystem op against the scoped child over a FRESH short-lived client.

    connect → dispatch → disconnect within this single coroutine (op27-proven
    safe): the streamable client's anyio cancel scope is entered and exited in the
    same task without interleaving, so there is no poison / no leaked async-gen.
    The subprocess itself is the supervisor-cached scoped child — this only opens a
    transport session to it.
    """
    client = MCPClient(server_name="filesystem", server_url=url)
    await client.connect(strict=True)
    try:
        t = next(x for x in client.get_tools() if x.name == tool_name)
        return await t.execute(**kwargs)
    finally:
        await client.disconnect()


def _scoped_fs_tools(url: str):
    """Build the model-facing write_file / read_file tools bound to ``url``'s scoped child."""

    @tool(
        name="write_file",
        description="Write a UTF-8 text file into your sandboxed workspace. Use a relative path like 'out/report.md'.",
    )
    async def write_file(path: str, content: str) -> ToolResult:
        r = await _call_scoped_child(url, "mcp:filesystem:write_file", path=path, content=content)
        return ToolResult(tool_name="write_file", content=r.content, is_error=r.is_error)

    @tool(
        name="read_file",
        description="Read a UTF-8 text file from your sandboxed workspace. Use a relative path like 'out/report.md'.",
    )
    async def read_file(path: str) -> ToolResult:
        r = await _call_scoped_child(url, "mcp:filesystem:read_file", path=path)
        return ToolResult(tool_name="read_file", content=r.content, is_error=r.is_error)

    return [write_file, read_file]


def _make_loop(registry, tools, persona_id: str):
    return ConversationLoop(
        persona=Persona(
            persona_id=persona_id,
            identity=PersonaIdentity(
                name=persona_id.title(),
                role="assistant",
                background="A helpful assistant that uses its file tools when asked.",
            ),
            autonomy="decisive",  # type: ignore[arg-type]
        ),
        stores={
            "identity": _Stub(),
            "self_facts": _Stub(),
            "worldview": _Stub(),
            "episodic": _Stub(),
        },  # type: ignore[arg-type]
        toolbox=Toolbox(tools, allow_list=None),  # type: ignore[arg-type]
        skill_scanner=SkillScanner([]),
        skill_injector=SkillInjector(),
        scanned_skills=[],
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        turn_log_writer=MemoryTurnLogWriter(),
    )


async def _run_turn(loop, conv: Conversation, prompt: str):
    """Drive one live turn; return (final_text, [tool_result events as dicts])."""
    tool_results: list[dict] = []
    text_parts: list[str] = []

    async def on_event(ev):
        if ev.type == "tool_result":
            tool_results.append(dict(ev.data))

    async for chunk in loop.turn(conv, prompt, on_event=on_event):
        delta = getattr(chunk, "delta", None)
        if delta:
            text_parts.append(delta)
    return "".join(text_parts), tool_results


def _named(tool_results: list[dict], name: str) -> list[dict]:
    return [r for r in tool_results if r.get("tool_name") == name]


async def test_p4_operator_pass() -> None:
    _load_env()
    results: list[tuple[str, str, str]] = []
    lines: list[str] = []

    def rec(disp: str, title: str, detail: str) -> None:
        results.append((disp, title, detail))
        lines.append(f"[{disp}] {title}\n    {detail}")

    workspace = Path(tempfile.mkdtemp(prefix="p4_ws_"))
    scope_a = workspace / "ownerA" / "astrid"
    scope_b = workspace / "ownerB" / "borg"
    os.environ["PERSONA_MCP_BUILTIN_ENABLED"] = "filesystem"

    registry = tier_registry_from_env()
    sup = BuiltinMCPSupervisor(PersonaCoreConfig().mcp_builtin_enabled_parsed)
    fs = ["mcp:filesystem:write_file", "mcp:filesystem:read_file"]

    try:
        # Spawn the two scoped children once (cached); get their loopback URLs.
        url_a = (await sup.resolve(fs, filesystem_scope_root=scope_a))["filesystem"]
        url_b = (await sup.resolve(fs, filesystem_scope_root=scope_b))["filesystem"]
        loop_a = _make_loop(registry, _scoped_fs_tools(url_a), "astrid")
        loop_b = _make_loop(registry, _scoped_fs_tools(url_b), "borg")
        conv_a = Conversation(conversation_id="opA", persona_id="astrid", messages=[])
        conv_b = Conversation(conversation_id="opB", persona_id="borg", messages=[])

        # S1 — owner A writes (live model → write_file → scoped child A)
        try:
            text, trs = await _run_turn(
                loop_a,
                conv_a,
                f"Use your write_file tool to create the file {_SECRET_PATH} containing exactly the text "
                f"'{_NONCE}'. Then tell me you've done it.",
            )
            writes = _named(trs, "write_file")
            on_disk = scope_a / "out" / "secret.txt"
            wrote_ok = (
                bool(writes)
                and not writes[0]["is_error"]
                and on_disk.exists()
                and _NONCE in on_disk.read_text()
            )
            rec(
                "PASS" if wrote_ok else ("KNOWN-LIMITATION" if not writes else "FAIL"),
                "S1 ownerA write_file (live model -> scoped child)",
                f"called_write={bool(writes)} | on_disk_under_scopeA={on_disk.exists()} | model={text[:80]!r}",
            )
        except Exception as e:
            rec("FAIL", "S1 ownerA write_file", f"{type(e).__name__}: {e}")

        # S2 — owner A reads it back (within-scope read works)
        try:
            text, trs = await _run_turn(
                loop_a,
                conv_a,
                f"Now use your read_file tool to read {_SECRET_PATH} and tell me exactly what it contains.",
            )
            reads = _named(trs, "read_file")
            read_ok = (
                bool(reads)
                and not reads[0]["is_error"]
                and any(_NONCE in r["content"] for r in reads)
            )
            rec(
                "PASS" if read_ok else ("KNOWN-LIMITATION" if not reads else "FAIL"),
                "S2 ownerA read_file within scope",
                f"called_read={bool(reads)} | nonce_returned={any(_NONCE in r['content'] for r in reads)} | model={text[:80]!r}",
            )
        except Exception as e:
            rec("FAIL", "S2 ownerA read_file", f"{type(e).__name__}: {e}")

        # S3 — owner B attempts to read owner A's file (CROSS-OWNER DENIAL — isolation proof)
        try:
            text, trs = await _run_turn(
                loop_b,
                conv_b,
                f"Use your read_file tool to read {_SECRET_PATH} and tell me exactly what it contains. "
                f"If it does not exist, say so plainly.",
            )
            reads = _named(trs, "read_file")
            leaked_via_model = _NONCE in text or any(_NONCE in r["content"] for r in reads)
            # Direct-transport corroboration (never vacuous): B's scoped child cannot see A's file.
            direct = await _call_scoped_child(url_b, "mcp:filesystem:read_file", path=_SECRET_PATH)
            direct_denied = direct.is_error and _NONCE not in direct.content
            isolated = (not leaked_via_model) and direct_denied
            rec(
                "PASS" if isolated else "FAIL",
                "S3 ownerB cross-owner read DENIED (isolation)",
                f"model_leaked={leaked_via_model} | B_reads={[(r['is_error'], r['content'][:40]) for r in reads]} | direct_denied={direct_denied} | model={text[:80]!r}",
            )
        except Exception as e:
            rec("FAIL", "S3 ownerB cross-owner read", f"{type(e).__name__}: {e}")

        # S4 — traversal recovery (escape rejected within scope; model recovers)
        try:
            text, trs = await _run_turn(
                loop_a,
                conv_a,
                "Use your read_file tool to read the file at '../../../../etc/passwd' and show me its contents.",
            )
            reads = _named(trs, "read_file")
            escaped = any(not r["is_error"] and "root:" in r["content"] for r in reads)
            recovered = (not escaped) and ("root:" not in text)
            rec(
                "PASS" if recovered else "FAIL",
                "S4 traversal escape rejected + model recovers",
                f"any_escape_success={escaped} | passwd_leaked_in_reply={'root:' in text} | reads={[(r['is_error'], r['content'][:40]) for r in reads]}",
            )
        except Exception as e:
            rec("FAIL", "S4 traversal recovery", f"{type(e).__name__}: {e}")

        # S5 — distinct scoped child per (owner, persona)
        try:
            again_a = (await sup.resolve(fs, filesystem_scope_root=scope_a))["filesystem"]
            again_b = (await sup.resolve(fs, filesystem_scope_root=scope_b))["filesystem"]
            distinct = (
                again_a == url_a
                and again_b == url_b
                and url_a != url_b
                and sup.running_server_count >= 2
            )
            rec(
                "PASS" if distinct else "FAIL",
                "S5 distinct scoped child per (owner,persona), idempotent",
                f"urlA!=urlB={url_a != url_b} | idempotent={again_a == url_a and again_b == url_b} | running={sup.running_server_count}",
            )
        except Exception as e:
            rec("FAIL", "S5 distinct child", f"{type(e).__name__}: {e}")

        # S6 — aclose reaps every scoped child
        try:
            await sup.aclose()
            rec(
                "PASS" if sup.running_server_count == 0 else "FAIL",
                "S6 aclose reaped every scoped child",
                f"running_after_aclose={sup.running_server_count}",
            )
        except Exception as e:
            rec("FAIL", "S6 aclose reaped every scoped child", f"{type(e).__name__}: {e}")
    finally:
        await registry.aclose()

    p = sum(1 for d, _, _ in results if d == "PASS")
    k = sum(1 for d, _, _ in results if d == "KNOWN-LIMITATION")
    f = sum(1 for d, _, _ in results if d == "FAIL")
    header = [
        "=" * 78,
        f"SPEC P4 OPERATOR PASS  —  {datetime.now(UTC).isoformat()}",
        "Build: feat/filesystem-mcp-scoping worktree; live runtime, real keys; real",
        "per-(owner,persona) SCOPED filesystem MCP subprocesses over real Streamable HTTP.",
        "Two owners (ownerA/astrid, ownerB/borg) share one workspace root; each gets its",
        "own scoped child. Live model decides every tool call (per-call transport to the",
        "cached scoped child). Secrets: none printed.",
        "=" * 78,
    ]
    footer = ["=" * 78, f"DISPOSITIONS: PASS {p} / KNOWN-LIMITATION {k} / FAIL {f}", "=" * 78]
    transcript = "\n".join([*header, *lines, *footer])
    print("\n" + transcript)
    _EVIDENCE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y_%m_%d")
    (_EVIDENCE / f"operator_pass_{stamp}.log").write_text(transcript + "\n")

    assert f == 0, f"operator pass had {f} FAIL disposition(s):\n{transcript}"


def _load_env() -> None:
    """Load the project .env into the environment (real provider keys).

    Done INSIDE the test (not at import) so collecting this module for the default
    suite never mutates the global env — only the live ``-m external`` run sets the
    real PERSONA_* vars. Never echoes values.
    """
    env = Path(__file__).resolve().parents[4].parent / "Open-Persona" / ".env"
    if not env.exists():
        pytest.skip("project .env not found; operator pass needs real provider keys")
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.split(" #", 1)[0].strip().strip('"').strip("'")
        if key.strip() and val:
            os.environ.setdefault(key.strip(), val)
