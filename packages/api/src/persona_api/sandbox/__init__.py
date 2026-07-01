"""``persona_api.sandbox`` — hosted sandbox surface (spec 12 T08+).

The ``HostedSandbox`` adapter wraps the E2B Code Interpreter SDK into the
:class:`persona.sandbox.protocol.CodeSandbox` Protocol so the runtime +
agentic loop work unchanged across local (``LocalDockerSandbox``) and
hosted backends — the load-bearing reversibility property D-12-12 buys
via the Protocol design.
"""

from __future__ import annotations

from persona_api.sandbox.config import SandboxPoolConfig, SandboxTemplateConfig
from persona_api.sandbox.context import (
    SandboxRequestContext,
    get_sandbox_request_context,
    reset_sandbox_request_context,
    set_sandbox_request_context,
)
from persona_api.sandbox.hosted import HostedSandbox
from persona_api.sandbox.pool import SandboxHandle, SandboxPool
from persona_api.sandbox.runtime_tool import make_pool_code_execution_tool

__all__ = [
    "HostedSandbox",
    "SandboxHandle",
    "SandboxPool",
    "SandboxPoolConfig",
    "SandboxTemplateConfig",
    "SandboxRequestContext",
    "get_sandbox_request_context",
    "make_pool_code_execution_tool",
    "reset_sandbox_request_context",
    "set_sandbox_request_context",
]
