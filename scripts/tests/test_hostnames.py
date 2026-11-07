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
