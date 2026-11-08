"""The live system's hostnames are on openpersonas.si, and the old domains are gone.

Owner change, 2026-11-07: the domain is openpersonas.si. The web is on Vercel at
openpersonas.si, app. and www.; the core host serves api. and connectors.; the voice host
will serve voice. and livekit. Nothing was ever deployed on the old names, so they are
dropped everywhere, with no alias. Only CHANGELOG.md keeps them, as history.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "deploy" / "server"
#: Spelled in parts so this file does not match itself.
OLD = ("openpersonas" + "ai", "openpersona" + "\\.online")
WEB_ORIGINS = "https://openpersonas.si,https://app.openpersonas.si,https://www.openpersonas.si"


def test_no_tracked_file_names_an_old_domain() -> None:
    found = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "grep",
            "-l",
            "-i",
            "-E",
            "|".join(OLD),
            "--",
            ".",
            ":!CHANGELOG.md",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    assert found == []


def _template(name: str) -> dict[str, str]:
    lines = (KIT / "env" / f"{name}.env.example").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def test_cors_allows_exactly_the_webs_origins() -> None:
    assert _template("api")["PERSONA_API_CORS_ORIGINS"] == WEB_ORIGINS
    assert _template("voice")["PERSONA_VOICE_CORS_ORIGINS"] == WEB_ORIGINS


def test_the_voice_hosts_names_are_on_the_new_domain() -> None:
    caddyfile = (KIT / "Caddyfile.voice").read_text(encoding="utf-8")
    assert re.search(r"^voice\.openpersonas\.si \{$", caddyfile, re.M)
    assert re.search(r"^livekit\.openpersonas\.si \{$", caddyfile, re.M)
    render = (KIT / "render_env.py").read_text(encoding="utf-8")
    assert '"wss://livekit.openpersonas.si"' in render


def test_the_dead_fly_smoke_and_its_gate_agree() -> None:
    """deploy.yml stays manual-only; its Fly smoke and check_deploy_wiring's allow-list
    must name the same URL, or the gate fails on the next CI run."""
    url = "https://api.openpersonas.si/healthz"
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    gate = (ROOT / "scripts" / "check_deploy_wiring.py").read_text(encoding="utf-8")
    assert url in workflow
    assert url in gate


#: The one sentence an operator must not miss (final review, MEDIUM): a NEW Clerk instance
#: issues new user ids, the api takes identity from the JWT `sub`, so every restored user
#: would sign in as a stranger with their data unreachable.
CLERK_RULE = (
    "Change the domain of the EXISTING production Clerk instance in the Clerk dashboard; "
    "never create a new instance, or the restored users' ids stop matching."
)


def test_the_runbook_says_to_move_the_existing_clerk_instance() -> None:
    runbook = (KIT / "RUNBOOK.md").read_text(encoding="utf-8")
    flat = " ".join(runbook.split())
    step4 = flat.split("## Step 4", 1)[1].split("## Step 5", 1)[0]
    table = flat.split("## New hostnames: what moves with them", 1)[1].split(" ## ", 1)[0]
    assert CLERK_RULE in step4
    assert CLERK_RULE in table
    assert "new Clerk production instance" not in flat
    assert "the instance set up for openpersonas.si" not in flat


def _profile() -> dict[str, str]:
    lines = (KIT / "hosts" / "oracle.env").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


#: A plain DNS name: labels of letters, digits and inner hyphens, at least one dot. No
#: port, list, space or wildcard, which would turn a Caddy site into a catch-all.
DNS_NAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def test_the_core_hostnames_are_plain_dns_names() -> None:
    values = _profile()
    for name in ("OP_API_HOST", "OP_CONNECTORS_HOST"):
        assert DNS_NAME.match(values[name]), f"{name}={values[name]}"


def test_the_dns_name_check_refuses_catch_alls() -> None:
    for bad in (
        "*.openpersonas.si",
        "api.openpersonas.si:443",
        "a.si,b.si",
        ":443",
        "localhost",
        "api .openpersonas.si",
        "-api.openpersonas.si",
    ):
        assert not DNS_NAME.match(bad), bad
    assert DNS_NAME.match("api.openpersonas.si")
