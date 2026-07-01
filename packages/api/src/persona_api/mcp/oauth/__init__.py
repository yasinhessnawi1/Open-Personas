"""MCP OAuth 2.1 authorization-code + PKCE client (Spec R8).

R8 **extends** N4/Spec-30: storage + injection stay N4's (Fernet-encrypted
per-user credential, transient-decrypt at connect, bearer-header inject on the
outbound ``MCPClient``); R8 adds only the **obtaining** (the OAuth dance) and the
**refresh** lifecycle. Open Persona is the OAuth *client* here — never a resource
server / AS. Fail-closed throughout: any incomplete/failed OAuth leaves the server
**not connected**, never half-authenticated.
"""

from __future__ import annotations
