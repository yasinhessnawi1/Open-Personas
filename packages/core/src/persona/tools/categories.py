"""The A3 action-category taxonomy — what *kind of effect* a tool has (A3-D-1).

Every tool/capability declares one or more **action categories**. The categories are the
risk/permission axis the A3 approval spine gates on; they are **orthogonal** to
:class:`persona.tools.catalog.ToolCategory` (a *need* grouping for the recommender — web /
files / compute / …). A tool's risk is *what it can do to the world*, not *what need it
serves*, so the mapping is **explicit per tool, never derived** from the catalog category.

The eight categories (each explainable to a user in one sentence):

- ``observe`` — it reads, searches, fetches, or retrieves; it changes nothing.
- ``compute`` — it runs sandboxed code or analysis that stays inside the workspace.
- ``draft`` — it creates an artifact only you will see or send (a file, an image, a draft).
- ``notify_user`` — it sends *you* a message (always allowed; governed by cadence, never a gate).
- ``communicate_as_user`` — it sends something to *other people* in your name.
- ``spend`` — it commits money.
- ``external_mutate`` — it changes state in the outside world (submits, posts, books, deletes).
- ``credentialed_access`` — it uses your stored credentials or servers.

The first four are **free** (a default task may run them unattended); the last four are
**gated by default** (a default task may not, without an explicit contract grant — A4).

**The back-door closure (A3-D-X-completeness):** :func:`resolve_action_categories` is the
single authoritative mapping (the ``resolve_tool_kind`` DRY pattern). Any unmapped tool —
and *every* MCP ``mcp:<server>:<tool>`` until it declares otherwise — resolves to the
most-restrictive gated category (``external_mutate``): *unmapped = gated, never free*. A
future built-in tool added to the catalog without an explicit mapping is caught by
:func:`unmapped_catalog_tools` (the registration-enforcement unit test).

**The sandbox-egress nuance (A3-D-1, verified against Spec 12):** the sandbox has **no
network egress by default**, and when egress is enabled it is bounded to a persona-authored
``allowed_hosts`` allow-list with metadata/RFC-1918 ranges always blocked (the model can
never change the policy — D-12-4). So ``code_execution`` is ``compute`` in the default
config — but once network egress is enabled it can *reach external hosts*, which is real
external reach an unattended leg must not have for free; :func:`resolve_action_categories`
escalates it to include ``external_mutate`` when ``network_enabled=True``.
"""

from __future__ import annotations

from enum import StrEnum

from persona.tools.catalog import known_tool_names

__all__ = [
    "FREE_CATEGORIES",
    "GATED_BY_DEFAULT",
    "ActionCategory",
    "resolve_action_categories",
    "unmapped_catalog_tools",
]


class ActionCategory(StrEnum):
    """The risk/permission category of a tool's effect (A3-D-1). See the module docstring."""

    OBSERVE = "observe"
    COMPUTE = "compute"
    DRAFT = "draft"
    NOTIFY_USER = "notify_user"
    COMMUNICATE_AS_USER = "communicate_as_user"
    SPEND = "spend"
    EXTERNAL_MUTATE = "external_mutate"
    CREDENTIALED_ACCESS = "credentialed_access"


#: Categories a default (unconfigured) task may run unattended without an approval.
FREE_CATEGORIES: frozenset[ActionCategory] = frozenset(
    {
        ActionCategory.OBSERVE,
        ActionCategory.COMPUTE,
        ActionCategory.DRAFT,
        ActionCategory.NOTIFY_USER,
    }
)

#: Categories gated by default — a contract clause (A4) is required to run them unattended.
GATED_BY_DEFAULT: frozenset[ActionCategory] = frozenset(
    {
        ActionCategory.COMMUNICATE_AS_USER,
        ActionCategory.SPEND,
        ActionCategory.EXTERNAL_MUTATE,
        ActionCategory.CREDENTIALED_ACCESS,
    }
)

#: The most-restrictive gated category an unmapped / undeclared tool falls back to. Anything
#: not explicitly mapped is gated unattended (the back-door closure, A3-D-X-completeness).
_UNMAPPED_DEFAULT: frozenset[ActionCategory] = frozenset({ActionCategory.EXTERNAL_MUTATE})

#: MCP tools are dynamic and undeclared in v1 → gated by default (back-door closure).
_MCP_PREFIX = "mcp:"

#: The sandboxed-code tool, whose risk depends on whether sandbox network egress is enabled.
_CODE_EXECUTION = "code_execution"

#: The single authoritative tool → action-category mapping (the completeness review). Seeded
#: from the whole ``TOOL_CATALOG`` surface + the skill-activation tool. Risk grouping, NOT the
#: catalog's need grouping — assigned explicitly per tool.
_TOOL_CATEGORIES: dict[str, frozenset[ActionCategory]] = {
    # observe — read / search / fetch / retrieve, no outside effect.
    "web_search": frozenset({ActionCategory.OBSERVE}),
    "web_fetch": frozenset({ActionCategory.OBSERVE}),
    "file_read": frozenset({ActionCategory.OBSERVE}),
    "datetime": frozenset({ActionCategory.OBSERVE}),
    "currency_convert": frozenset({ActionCategory.OBSERVE}),
    # mcp_search (Spec N4) — reads the catalog's display metadata to find candidate
    # apps; enables nothing, touches no secret, has no outside effect → free. (The
    # adopted mcp:* tools it surfaces are gated separately, N4-D-8.)
    "mcp_search": frozenset({ActionCategory.OBSERVE}),
    # compute — analysis / transformation that stays inside the workspace.
    "code_execution": frozenset({ActionCategory.COMPUTE}),
    "calculator": frozenset({ActionCategory.COMPUTE}),
    "regex_match": frozenset({ActionCategory.COMPUTE}),
    "text_diff": frozenset({ActionCategory.COMPUTE}),
    "text_summarize": frozenset({ActionCategory.COMPUTE}),
    "json_query": frozenset({ActionCategory.COMPUTE}),
    # draft — an artifact only the user sees / sends, written to the workspace.
    "file_write": frozenset({ActionCategory.DRAFT}),
    "generate_image": frozenset({ActionCategory.DRAFT}),
    "render_diagram": frozenset({ActionCategory.DRAFT}),
    # skill activation — loads instructions into context; the skill's constituent tools are
    # independently gated at their own dispatch, so the activation itself is free (observe).
    "use_skill": frozenset({ActionCategory.OBSERVE}),
    # Self-knowledge and self-work (Spec W1, D-W1-45). These four reach only the OWNER'S OWN
    # durable state through owner-scoped ports: nothing outside the tenant is read, changed,
    # spent or credentialed. Unmapped they fell to the gated default, so a leg that looked at
    # its own tasks parked the task on an approval — the whole point of T9 (a persona that can
    # see its work) turned into a question the user had to answer before it could look.
    "task_introspect": frozenset({ActionCategory.OBSERVE}),
    "schedule_introspect": frozenset({ActionCategory.OBSERVE}),
    # A fact about the user, written to the persona's own typed memory. Nobody but the owner
    # ever sees it, which is what DRAFT means here (an artifact only the user sees).
    "record_user_fact": frozenset({ActionCategory.DRAFT}),
    # ``task_pickup`` MUTATES (it resumes a task), so OBSERVE is a stretch of the label and is
    # recorded as one. The mapping is a RISK grouping, not a verb grouping, and this verb's
    # risk is an observation's: it acts only on work the owner already agreed to, through a
    # port bound to one persona (D-W1-10), under that task's own bounds and the owner's pause
    # dial. Gating it would leave a persona able to SEE that its work stalled and unable to
    # carry it on without asking, which is the half-feature T9 existed to end.
    "task_pickup": frozenset({ActionCategory.OBSERVE}),
}


def resolve_action_categories(
    tool_name: str, *, network_enabled: bool = False
) -> frozenset[ActionCategory]:
    """Resolve a tool name to its action categories (the single authoritative mapping).

    Pure + total: an unmapped name — and every MCP ``mcp:<server>:<tool>`` — resolves to the
    most-restrictive gated category (``external_mutate``), so an undeclared capability is
    never reachable unattended for free (the back-door closure, A3-D-X-completeness).

    Args:
        tool_name: The dispatched tool name (``"web_search"``, ``"code_execution"``,
            ``"mcp:stripe:create_charge"``, …).
        network_enabled: Whether the persona's sandbox has network egress enabled. Escalates
            ``code_execution`` to also carry ``external_mutate`` (bounded egress is still
            external reach an unattended leg must gate on); irrelevant to every other tool.

    Returns:
        The frozenset of categories the tool's effect spans. Never empty.
    """
    if tool_name.startswith(_MCP_PREFIX):
        return _UNMAPPED_DEFAULT
    base = _TOOL_CATEGORIES.get(tool_name)
    if base is None:
        return _UNMAPPED_DEFAULT
    if tool_name == _CODE_EXECUTION and network_enabled:
        return base | {ActionCategory.EXTERNAL_MUTATE}
    return base


def unmapped_catalog_tools() -> frozenset[str]:
    """Catalog tools lacking an explicit category mapping (the registration enforcement).

    The completeness review as an executable invariant: a future tool added to
    :data:`persona.tools.catalog.TOOL_CATALOG` without a :data:`_TOOL_CATEGORIES` entry
    appears here, failing the registration test — it must declare a category rather than be
    silently carried by the gated default. Returns the empty set when the mapping is
    complete.
    """
    return frozenset(name for name in known_tool_names() if name not in _TOOL_CATEGORIES)
