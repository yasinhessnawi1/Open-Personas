"""The server kit's host profiles: what runs where, and how big it may get.

Owner ruling 2026-11-07: production runs on two hosts. The CORE host (an Oracle Always
Free arm64 server, 2 OCPU and 12 GB, shared with the owner's other work) runs postgres,
the migrate step, the api and Caddy. The VOICE host (Azure, Phase B) runs voice and
LiveKit. So the core compose file holds no voice service, and every size the core host
depends on is a setting of its host profile (``hosts/<profile>.env``), adjustable without
editing the compose file.

Pinned here:

* the core file runs exactly postgres, migrate, api and caddy, and voice lives in its own
  file;
* every ``${OP_...}`` the compose files interpolate has a value in every profile, and a
  profile holds plain non-secret OP_* settings only;
* on each profile the memory limits fit the host with the profile's reserve left over,
  postgres never swaps, and its cache settings fit its limit;
* every script reaches compose through ONE helper that applies the profile, so a restore
  and a deploy start postgres with the same limits;
* the core host's deploy and preflight no longer expect voice or LiveKit.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "deploy" / "server"
HOSTS = KIT / "hosts"
PROFILES = sorted(p.stem for p in HOSTS.glob("*.env"))
CORE_SERVICES = {"postgres", "migrate", "api", "caddy"}
#: Interpolated by the scripts themselves, not by a profile.
SCRIPT_VARS = {"OP_ENV_DIR", "OP_DATA_DIR", "OP_IMAGE_TAG"}
_INTERPOLATION = re.compile(r"\$\{(OP_[A-Z0-9_]+)(?::-([^}]*))?\}")
#: The same rule bin/lib/profile.sh enforces before it exports a setting into a root shell.
_PROFILE_LINE = re.compile(r"^(OP_[A-Z0-9_]+)=([A-Za-z0-9._,/:-]*)$")
_SIZE = re.compile(r"^(\d+(?:\.\d+)?)([kmg]?)b?$", re.I)


def _text(name: str) -> str:
    return (KIT / name).read_text(encoding="utf-8")


def _yaml(name: str) -> dict[str, Any]:
    return yaml.safe_load(_text(name))


def profile(name: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in (HOSTS / f"{name}.env").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PROFILE_LINE.match(line)
        assert match, f"hosts/{name}.env: not a plain OP_* setting: {line.split('=', 1)[0]}"
        values[match.group(1)] = match.group(2)
    return values


def resolve(value: object, values: dict[str, str]) -> str:
    """Compose's ``${VAR:-default}`` interpolation, with the profile's values."""

    def one(match: re.Match[str]) -> str:
        return values.get(match.group(1), match.group(2) or "")

    return _INTERPOLATION.sub(one, str(value))


def gib(size: str) -> float:
    match = _SIZE.match(size.strip())
    assert match, f"not a size: {size!r}"
    number, unit = float(match.group(1)), match.group(2).lower()
    return number * {"": 1 / 1024**3, "k": 1 / 1024**2, "m": 1 / 1024, "g": 1}[unit]


def _pg_setting(command: list[str], name: str, values: dict[str, str]) -> str:
    for item in command:
        resolved = resolve(item, values)
        if resolved.startswith(f"{name}="):
            return resolved.split("=", 1)[1]
    raise AssertionError(f"postgres sets no {name}")


# --- what runs where -------------------------------------------------------------------------


def test_the_core_host_runs_no_voice() -> None:
    assert set(_yaml("docker-compose.yml")["services"]) == CORE_SERVICES


def test_voice_and_livekit_live_in_the_voice_hosts_file() -> None:
    assert {"voice", "livekit"} <= set(_yaml("docker-compose.voice.yml")["services"])
    assert not CORE_SERVICES - {"caddy"} & set(_yaml("docker-compose.voice.yml")["services"])


def test_caddy_is_pinned_by_digest() -> None:
    image = _yaml("docker-compose.yml")["services"]["caddy"]["image"]
    assert re.fullmatch(r"caddy:2\.10\.2@sha256:[0-9a-f]{64}", image), image


# --- profiles --------------------------------------------------------------------------------


def test_there_is_an_oracle_profile() -> None:
    assert "oracle" in PROFILES


@pytest.mark.parametrize("name", PROFILES)
def test_every_interpolated_setting_has_a_value_in_the_profile(name: str) -> None:
    # The voice file is deployed by no profile yet (task T13 adds the voice host's).
    values = profile(name)
    for var, _default in _INTERPOLATION.findall(_text("docker-compose.yml")):
        if var not in SCRIPT_VARS:
            assert var in values, f"hosts/{name}.env does not set {var}"


@pytest.mark.parametrize("name", PROFILES)
def test_the_memory_limits_fit_the_host_with_its_reserve(name: str) -> None:
    values = profile(name)
    services = _yaml("docker-compose.yml")["services"]
    total = sum(gib(resolve(s["mem_limit"], values)) for s in services.values() if "mem_limit" in s)
    room = float(values["OP_HOST_RAM_GB"]) - float(values["OP_HOST_RESERVED_GB"])
    assert total <= room, f"{name}: limits {total:.2f} GiB, room {room:.2f} GiB"
    # The long-running services all carry a limit; only the one-shot migrate may not.
    assert {n for n, s in services.items() if "mem_limit" not in s} <= {"migrate"}


@pytest.mark.parametrize("name", PROFILES)
def test_postgres_never_swaps_and_its_cache_fits_its_limit(name: str) -> None:
    values = profile(name)
    postgres = _yaml("docker-compose.yml")["services"]["postgres"]
    limit = resolve(postgres["mem_limit"], values)
    assert resolve(postgres["memswap_limit"], values) == limit
    shared = gib(_pg_setting(postgres["command"], "shared_buffers", values))
    assert shared <= 0.4 * gib(limit), "shared_buffers above 40% of the container's limit"
    cache = gib(_pg_setting(postgres["command"], "effective_cache_size", values))
    assert cache <= float(values["OP_HOST_RAM_GB"])
    assert int(_pg_setting(postgres["command"], "max_connections", values)) <= 100


def test_the_oracle_profile_is_sized_for_twelve_gigabytes() -> None:
    values = profile("oracle")
    assert values["OP_HOST_RAM_GB"] == "12"
    assert float(values["OP_HOST_RESERVED_GB"]) >= 3


def test_the_size_checks_are_not_vacuous() -> None:
    """The old 16 GB box's sizes fail these checks on the Oracle profile."""
    oracle = profile("oracle")
    old_core = gib("5g") + gib("4g") + gib("256m")  # postgres, api, caddy on the 16 GB box
    assert old_core > float(oracle["OP_HOST_RAM_GB"]) - float(oracle["OP_HOST_RESERVED_GB"])
    assert gib("2GB") > 0.4 * gib("3g")  # its shared_buffers in a 3g container
    assert gib("512m") == 0.5
    assert resolve("${OP_X:-2g}", {}) == "2g"
    assert resolve("${OP_X:-2g}", {"OP_X": "1g"}) == "1g"


# --- one way to reach compose ------------------------------------------------------------------

SCRIPTS = sorted((KIT / "bin").glob("*.sh"))
LIB = KIT / "bin" / "lib" / "compose.sh"


def test_scripts_reach_compose_only_through_the_helper() -> None:
    for script in SCRIPTS:
        text = script.read_text(encoding="utf-8")
        assert "docker compose -f" not in text, f"{script.name} calls compose directly"
        if re.search(r"\bcompose (up|exec|run|ps|build|down)\b", text):
            assert 'lib/compose.sh"' in text, f"{script.name} uses compose without the helper"


def test_the_helper_applies_the_host_profile() -> None:
    lib = LIB.read_text(encoding="utf-8") + (LIB.parent / "profile.sh").read_text(encoding="utf-8")
    assert '--env-file "${KIT_DIR}/hosts/${OP_HOST_PROFILE}.env"' in lib
    assert "/etc/open-persona/host-profile" in lib
    # A profile name is a file name under hosts/: nothing that could climb out of it.
    assert "^[a-z0-9-]+$" in lib


# --- the core host expects no voice ----------------------------------------------------------


def test_deploy_never_waits_for_voice_or_starts_livekit() -> None:
    deploy = _text("bin/deploy.sh")
    for needle in ("8001", "selfhosted-livekit", "voice.env", "COMPOSE_PROFILES"):
        assert needle not in deploy, needle


def test_preflight_requires_no_voice_file_and_allows_no_media_port() -> None:
    preflight = _text("bin/preflight.sh")
    assert re.search(r'^required="common api postgres migrate caddy alert"$', preflight, re.M)
    assert re.search(r'^ALLOWED_UFW="22/tcp 80/tcp 443/tcp"$', preflight, re.M)
    assert re.search(r'^ALLOWED_TCP="22 80 443"$', preflight, re.M)
    for needle in ("8001", "7880", "7881", "7882", "PERSONA_VOICE_DATABASE_URL"):
        assert needle not in preflight, needle


@pytest.mark.parametrize("lib", sorted(LIB.parent.glob("*.sh")), ids=lambda p: p.name)
def test_the_helpers_are_lf_strict_and_parse(lib: Path) -> None:
    raw = lib.read_bytes()
    assert b"\r\n" not in raw, "CRLF line endings break bash on the host"
    assert raw.decode("utf-8").startswith("# shellcheck shell=bash\n")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")
    result = subprocess.run([bash, "-n", str(lib)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_the_profiles_are_tracked_although_env_files_under_the_kit_are_not() -> None:
    """.gitignore drops every *.env under the kit (a rendered secret); profiles are not secrets."""
    for name in PROFILES:
        ignored = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", f"deploy/server/hosts/{name}.env"],
            check=False,
        )
        assert ignored.returncode == 1, f"hosts/{name}.env is gitignored"
    rendered = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "-q", "deploy/server/env/api.env"], check=False
    )
    assert rendered.returncode == 0, "a rendered env file under the kit must stay ignored"
