"""The Hetzner deploy kit (deploy/hetzner): its env templates, compose file and scripts.

Production moved off Fly by restoring a backup onto one box. The ways that goes wrong
quietly are the ones pinned here:

* an env name the code never reads (the PERSONA_AUDIT_ROOT typo class, which once left
  the api writing audit records to a root-owned path), so every app setting in a
  template must be a real setting;
* a dev value riding into production from the owner's .env, or a Fly-only setting
  surviving the move;
* the initiative or the per-tenant MCP runtime coming back on by accident;
* the database or an app port published past the firewall, or the api scaled out;
* the compose file drifting from the fly.toml [env] values it replaces;
* render_env.py printing a value.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args

import pytest
import yaml
from persona_runtime.chain_report import UNIFIED_ENV_NAMES

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "deploy" / "hetzner"
ENV_DIR = KIT / "env"
APP_TEMPLATES = ("common", "api", "voice")

# Every module that defines a pydantic-settings class the api or voice reads.
SETTINGS_MODULES = (
    "persona_api.config",
    "persona_api.mcp.runtime_config",
    "persona_api.sandbox.config",
    "persona_api.services.memory_service",
    "persona_connectors.config",
    "persona.backends.config",
    "persona.billing.formula",
    "persona.config",
    "persona.events.config",
    "persona.graph.config",
    "persona.imagegen.config",
    "persona.initiative.config",
    "persona.recall.config",
    "persona.stores.lifecycle",
    "persona_runtime.safety_intercept",
    "persona_voice.config",
    "persona_voice.stt.config",
    "persona_voice.tts.config",
)

# Read through os.environ rather than a settings class. Each must appear literally in the
# package sources, which the test checks, so this list cannot rot into fiction.
DIRECT_ENV_READS = ("E2B_API_KEY", "PERSONA_IMAGEGEN_MODELS", "PERSONA_CLOUDFLARE_ACCOUNT_ID")

# Names that must never reach production from the owner's .env, or must not survive Fly.
FORBIDDEN = {
    "PERSONA_DEV_CHEAP_TIERS",
    "PERSONA_CHROMA_PATH",
    "POSTGRES_HOST_PORT",
    "SOAK_DB_PASSWORD",
    "PG_SUPERUSER_PASSWORD",
    "GITHUB_TOKEN",
    "PERSONA_PYKEY",
    "FLY_API_TOKEN",
    "PERSONA_MCP_RUNTIME_FLY_APP",
    "PERSONA_MCP_RUNTIME_FLY_PORT",
    *(
        f"PERSONA_{tier}_{suffix}"
        for tier in ("FRONTIER", "MID", "SMALL")
        for suffix in ("API_KEY", "MODEL", "PROVIDER")
    ),
}

TOKEN = re.compile(
    r"^__(FROM_ENV(:[A-Z][A-Z0-9_]*)?|GITHUB_PRODUCTION|OWNER|LIVEKIT_URL|LIVEKIT_API_KEY"
    r"|LIVEKIT_API_SECRET)__$"
)
GEN = re.compile(r"__GEN_[A-Z0-9_]+__")
DSN = re.compile(
    r"^postgresql\+psycopg://(persona|persona_app|persona_platform):__GEN_[A-Z_]+__"
    r"@postgres:5432/persona$"
)
FIXED_VALUES = {
    "",  # alert.env OP_ALERT_CMD: empty means the on-box marker only
    "0",
    "false",
    "persona",
    "https://acme-staging-v02.api.letsencrypt.org/directory",
}


def _template(name: str) -> list[tuple[str, str]]:
    pairs = []
    for raw in (ENV_DIR / f"{name}.env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            pairs.append((key, value))
    return pairs


def _all_templates() -> dict[str, list[tuple[str, str]]]:
    return {
        p.name.removesuffix(".env.example"): _template(p.name.removesuffix(".env.example"))
        for p in sorted(ENV_DIR.glob("*.env.example"))
    }


def _settings_env_names() -> set[str]:
    from pydantic import AliasChoices
    from pydantic_settings import BaseSettings

    names: set[str] = set()
    for module_name in SETTINGS_MODULES:
        module = importlib.import_module(module_name)
        for obj in vars(module).values():
            if not (isinstance(obj, type) and issubclass(obj, BaseSettings)) or obj is BaseSettings:
                continue
            prefix = (obj.model_config.get("env_prefix") or "").upper()
            for field_name, field in obj.model_fields.items():
                alias = field.validation_alias or field.alias
                if isinstance(alias, AliasChoices):
                    names.update(str(c).upper() for c in alias.choices if isinstance(c, str))
                elif isinstance(alias, str):
                    names.add(alias.upper())
                else:
                    names.add(f"{prefix}{field_name}".upper())
    return names


def _provider_env_names() -> set[str]:
    from persona.backends.config import Provider

    return {
        f"PERSONA_{p.upper()}_{suffix}"
        for p in get_args(Provider)
        for suffix in ("API_KEY", "BASE_URL")
    }


# --- env templates ---------------------------------------------------------------------------


def test_every_app_setting_in_the_templates_is_read_by_the_code() -> None:
    known = _settings_env_names() | _provider_env_names() | set(UNIFIED_ENV_NAMES)
    sources = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in (ROOT / "packages").glob("*/src/**/*.py")
    )
    for name in DIRECT_ENV_READS:
        assert f'"{name}"' in sources, f"{name} is no longer read directly; drop it from the list"
    known |= set(DIRECT_ENV_READS)

    unknown = [
        f"{template}.env: {name}"
        for template in APP_TEMPLATES
        for name, _ in _template(template)
        if name not in known
    ]
    assert unknown == [], f"settings no code reads (typo or retired): {unknown}"


def test_no_dev_only_or_fly_only_name_is_rendered() -> None:
    present = {name for pairs in _all_templates().values() for name, _ in pairs}
    assert present & FORBIDDEN == set()


def test_no_name_appears_twice_in_one_template() -> None:
    for template, pairs in _all_templates().items():
        names = [n for n, _ in pairs]
        assert len(names) == len(set(names)), template


def test_every_template_value_is_a_token_or_a_reviewed_fixed_value() -> None:
    """A template must never carry a real secret: only tokens, DSNs of tokens, fixed values."""
    for template, pairs in _all_templates().items():
        for name, value in pairs:
            ok = (
                TOKEN.match(value)
                or DSN.match(value)
                or value in FIXED_VALUES
                or re.fullmatch(r"(__GEN_[A-Z0-9_]+__)(: __GEN_[A-Z0-9_]+__)?", value)
            )
            assert ok, f"{template}.env {name}: not a token or a reviewed fixed value"


def test_the_seven_shared_settings_come_from_github_production() -> None:
    common = dict(_template("common"))
    for name in UNIFIED_ENV_NAMES:
        assert common.get(name) == "__GITHUB_PRODUCTION__", name


def test_initiative_and_per_tenant_mcp_are_pinned_off() -> None:
    api = dict(_template("api"))
    assert api["PERSONA_INITIATIVE_ENABLED"] == "false"
    assert api["PERSONA_ALLOW_PER_TENANT_MCP"] == "0"


def test_only_the_migration_holds_the_superuser_dsn() -> None:
    """H1: the long-running api never holds superuser credentials."""
    api = dict(_template("api"))
    voice = dict(_template("voice"))
    migrate = dict(_template("migrate"))
    assert "//persona_platform:" in api["DATABASE_URL"]
    assert "//persona_app:" in api["APP_DATABASE_URL"]
    assert "//persona_app:" in voice["PERSONA_VOICE_DATABASE_URL"]
    assert "//persona:" in migrate["DATABASE_URL"]
    holders = [
        name
        for name, pairs in _all_templates().items()
        if any("//persona:" in value for _, value in pairs)
    ]
    assert holders == ["migrate"]


def test_migrate_reads_only_its_own_env_file_and_has_no_route_out() -> None:
    migrate = _compose()["services"]["migrate"]
    assert migrate["env_file"] == ["${OP_ENV_DIR:-/opt/open-persona/env}/migrate.env"]
    assert migrate["networks"] == ["db"]


def test_every_generated_database_password_has_a_generator() -> None:
    fill = (KIT / "bin" / "fill_generated.sh").read_text(encoding="utf-8")
    tokens = {
        t for pairs in _all_templates().values() for _, value in pairs for t in GEN.findall(value)
    }
    for token in tokens:
        name = token.removeprefix("__GEN_").removesuffix("__")
        assert re.search(rf"\b{name}\b", fill), f"fill_generated.sh cannot make {name}"


def test_livekit_key_pair_is_the_same_for_the_sfu_and_voice() -> None:
    livekit = dict(_template("livekit"))
    assert livekit["LIVEKIT_KEYS"] == "__GEN_LIVEKIT_API_KEY__: __GEN_LIVEKIT_API_SECRET__"
    render = _load_render()
    assert render.SELFHOSTED_LIVEKIT["__LIVEKIT_API_KEY__"] == "__GEN_LIVEKIT_API_KEY__"
    assert render.SELFHOSTED_LIVEKIT["__LIVEKIT_API_SECRET__"] == "__GEN_LIVEKIT_API_SECRET__"


# --- compose ---------------------------------------------------------------------------------


def _compose() -> dict[str, Any]:
    return yaml.safe_load((KIT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_postgres_is_never_published() -> None:
    assert "ports" not in _compose()["services"]["postgres"]


def test_postgres_sits_only_on_an_internal_network() -> None:
    compose = _compose()
    assert compose["services"]["postgres"]["networks"] == ["db"]
    assert compose["networks"]["db"]["internal"] is True
    assert "@sha256:" in compose["services"]["postgres"]["image"]


def test_app_containers_drop_every_capability_and_cap_pids() -> None:
    services = _compose()["services"]
    for name in ("api", "voice", "migrate", "livekit", "caddy"):
        assert services[name]["cap_drop"] == ["ALL"], name
        assert services[name]["pids_limit"] <= 1024, name
    assert services["caddy"]["cap_add"] == ["NET_BIND_SERVICE"]
    for name in ("api", "voice", "migrate", "livekit"):
        assert "cap_add" not in services[name], name
    assert services["livekit"]["user"] == "65534:65534"


def test_the_daemon_forbids_new_privileges_and_keeps_containers_across_upgrades() -> None:
    provision = (KIT / "bin" / "provision.sh").read_text(encoding="utf-8")
    assert '"no-new-privileges": true' in provision
    assert '"live-restore": true' in provision
    assert "origin=Docker,label=Docker CE" in provision
    assert "9DC858229FC7DD38854AE2D88D81803C0EBFCD88" in provision


def test_app_ports_are_published_on_loopback_only() -> None:
    for name, service in _compose()["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} past ufw"


def test_the_api_cannot_be_scaled_out() -> None:
    api = _compose()["services"]["api"]
    assert api["container_name"] == "open-persona-api"
    assert api["deploy"]["replicas"] == 1


def test_host_network_only_for_caddy_and_livekit() -> None:
    host = {n for n, s in _compose()["services"].items() if s.get("network_mode") == "host"}
    assert host == {"caddy", "livekit"}


def test_livekit_and_migrate_only_run_when_asked() -> None:
    services = _compose()["services"]
    assert services["livekit"]["profiles"] == ["selfhosted-livekit"]
    assert services["migrate"]["profiles"] == ["tools"]
    assert services["migrate"]["command"] == ["python", "packages/api/release.py"]


def test_env_files_live_outside_the_checkout() -> None:
    for name, service in _compose()["services"].items():
        for env_file in service.get("env_file", []):
            assert env_file.startswith("${OP_ENV_DIR:-/opt/open-persona/env}/"), name


def _fly_env(package: str) -> dict[str, str]:
    data = tomllib.loads((ROOT / "packages" / package / "fly.toml").read_text(encoding="utf-8"))
    return {k: str(v) for k, v in data.get("env", {}).items()}


@pytest.mark.parametrize(
    ("package", "service", "changed"),
    [
        # On Fly voice was reached over its public https URL; on one box it is internal.
        ("api", "api", {"PERSONA_VOICE_SERVICE_URL"}),
        ("voice", "voice", set()),
    ],
)
def test_compose_carries_the_fly_env_it_replaces(
    package: str, service: str, changed: set[str]
) -> None:
    environment = {k: str(v) for k, v in _compose()["services"][service]["environment"].items()}
    for key, value in _fly_env(package).items():
        assert key in environment, f"{service} lost {key} from {package}/fly.toml"
        if key not in changed:
            assert environment[key] == value, f"{service} {key} differs from fly.toml"
    assert environment.get("PERSONA_VOICE_SERVICE_URL", "http://voice:8001") == "http://voice:8001"


def test_voice_audit_goes_to_postgres_in_cloud() -> None:
    environment = _compose()["services"]["voice"]["environment"]
    assert environment["PERSONA_EDITION"] == "cloud"
    assert environment["PERSONA_VOICE_AUDIT_BACKEND"] == "postgres"


# --- Caddy and preflight agree on the hostnames ----------------------------------------------

HOSTS = {
    "api.openpersonasai.com",
    "api.openpersona.online",
    "connectors.openpersonasai.com",
    "voice.openpersonasai.com",
    "livekit.openpersonasai.com",
}


def test_caddy_admin_api_is_off_and_bodies_are_capped() -> None:
    caddyfile = (KIT / "Caddyfile").read_text(encoding="utf-8")
    assert re.search(r"^\tadmin off$", caddyfile, re.M)
    sites = caddyfile.count("reverse_proxy 127.0.0.1")
    assert caddyfile.count("max_size") == sites
    assert "request>uri delete" in caddyfile


def test_the_connector_host_serves_exactly_the_provider_paths() -> None:
    """connectors.* may reach only the webhooks and OAuth callbacks the code mounts."""
    src = ROOT / "packages" / "connectors" / "src" / "persona_connectors"
    constants = {
        "discord/app.py": "_CALLBACK_PATH",
        "slack/app.py": "_CALLBACK_PATH",
        "slack/events.py": "_EVENTS_PATH",
        "telegram/app.py": "_WEBHOOK_PATH",
    }
    expected = set()
    for file, constant in constants.items():
        match = re.search(
            rf'^{constant} = "([^"]+)"', (src / file).read_text(encoding="utf-8"), re.M
        )
        assert match, f"{file} {constant}"
        expected.add(match.group(1))
    email = (src / "email" / "app.py").read_text(encoding="utf-8")
    expected.update(re.findall(r'@app\.post\("(/email/[^"]+)"\)', email))
    twilio = (src / "_twilio" / "app.py").read_text(encoding="utf-8")
    assert 'webhook_path = f"/{platform}/webhook"' in twilio
    assert 'status_path = f"/{platform}/status"' in twilio
    service = (src / "service.py").read_text(encoding="utf-8")
    for platform in sorted(set(re.findall(r'http_apps\["(sms|whatsapp)"\]', service))):
        expected.update({f"/{platform}/webhook", f"/{platform}/status"})

    caddyfile = (KIT / "Caddyfile").read_text(encoding="utf-8")
    matcher = re.search(r"^\t@provider path (.+)$", caddyfile, re.M)
    assert matcher
    assert set(matcher.group(1).split()) == expected


def test_caddy_serves_every_production_hostname() -> None:
    caddyfile = (KIT / "Caddyfile").read_text(encoding="utf-8")
    sites = set(re.findall(r"^([a-z0-9.,\s-]+\.(?:com|online))\s*\{", caddyfile, re.M))
    served = {h.strip() for site in sites for h in site.split(",")}
    assert served == HOSTS


def test_preflight_checks_dns_for_every_hostname() -> None:
    preflight = (KIT / "bin" / "preflight.sh").read_text(encoding="utf-8")
    for host in HOSTS:
        assert host in preflight, host


# --- scripts ---------------------------------------------------------------------------------

SCRIPTS = sorted((KIT / "bin").glob("*.sh"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_are_lf_strict_and_parse(script: Path) -> None:
    raw = script.read_bytes()
    assert b"\r\n" not in raw, "CRLF line endings break bash on the box"
    text = raw.decode("utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert re.search(r"^set -[a-z]*u[a-z]* ?(-o pipefail)?", text, re.M), "no `set -u`"
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")
    result = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_no_em_or_en_dashes_in_the_kit() -> None:
    offenders = [
        str(p.relative_to(ROOT))
        for p in KIT.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and re.search("[\u2013\u2014]", p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# --- render_env.py ---------------------------------------------------------------------------


def _load_render() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hetzner_render_env", KIT / "render_env.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fake_secret(tag: str) -> str:
    # Assembled at runtime so no secret-shaped literal sits in the repository.
    return "fake" + "-" + tag + "-" + "0a1b2c3d" * 4


def _fake_dotenv(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    pem = "-----BEGIN PUBLIC KEY-----\nMIIBfake\n-----END PUBLIC KEY-----"
    values = {
        "PERSONA_API_JWT_PUBLIC_KEY": pem,
        "PERSONA_OPENROUTER_API_KEY": _fake_secret("or"),
        "PERSONA_STRIPE_SECRET_KEY": _fake_secret("it's$quoted"),
        "PERSONA_FRONTIER_MODELS": "openrouter/x/y",
        "PERSONA_LIVEKIT_URL": "wss://example.livekit.cloud",
        "PERSONA_LIVEKIT_API_KEY": _fake_secret("lk"),
        "PERSONA_LIVEKIT_API_SECRET": _fake_secret("lks"),
        "PERSONA_INITIATIVE_ENABLED": "true",
        "PERSONA_DEV_CHEAP_TIERS": "1",
    }
    lines = []
    for key, value in values.items():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        lines.append(f'{key}="{escaped}"')
    path = tmp_path / "dotenv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, values


_RENDERED_LINE = re.compile(r"""^([A-Z][A-Z0-9_]*)=(?:'([^']*)'|"((?:[^"\\]|\\.)*)")$""", re.M)


def _compose_unescape(value: str) -> str:
    """What Docker Compose reads from a double-quoted env_file value (verified with
    `docker compose config` on 2026-10-23: `\\n` is a newline, `$$` a literal `$`)."""
    out, i = [], 0
    while i < len(value):
        pair = value[i : i + 2]
        if pair in ("\\n", '\\"', "\\\\"):
            out.append({"\\n": "\n", '\\"': '"', "\\\\": "\\"}[pair])
            i += 2
        elif pair == "$$":
            out.append("$")
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def _rendered(out: Path, name: str) -> dict[str, str]:
    """A rendered env file read with Docker Compose's semantics, which is what reads it."""
    text = (out / f"{name}.env").read_text(encoding="utf-8")
    return {
        m.group(1): m.group(2) if m.group(2) is not None else _compose_unescape(m.group(3))
        for m in _RENDERED_LINE.finditer(text)
    }


def test_render_copies_resolves_and_never_prints_a_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    render = _load_render()
    dotenv, values = _fake_dotenv(tmp_path)
    out = tmp_path / "out"

    assert render.main(["--dotenv", str(dotenv), "--out", str(out), "--livekit", "cloud"]) == 0
    printed = capsys.readouterr()
    # Short flag values ("1", "true") occur in any text; every secret-shaped value is long.
    for value in (v for v in values.values() if len(v) > 8):
        assert value not in printed.out
        assert value not in printed.err

    api = _rendered(out, "api")
    common = _rendered(out, "common")
    voice = _rendered(out, "voice")
    assert api["PERSONA_API_JWT_PUBLIC_KEY"] == values["PERSONA_API_JWT_PUBLIC_KEY"]
    assert voice["PERSONA_VOICE_JWT_PUBLIC_KEY"] == values["PERSONA_API_JWT_PUBLIC_KEY"]
    assert api["PERSONA_STRIPE_SECRET_KEY"] == values["PERSONA_STRIPE_SECRET_KEY"]
    assert common["PERSONA_OPENROUTER_API_KEY"] == values["PERSONA_OPENROUTER_API_KEY"]
    # .env's initiative state is ignored on purpose; the dev-only name is never rendered.
    assert api["PERSONA_INITIATIVE_ENABLED"] == "false"
    assert all("PERSONA_DEV_CHEAP_TIERS" not in _rendered(out, n) for n in APP_TEMPLATES)
    # Chains wait for the owner unless the owner confirmed .env holds production.
    assert common["PERSONA_FRONTIER_MODELS"] == "__GITHUB_PRODUCTION__"
    assert api["PERSONA_API_CORS_ORIGINS"] == "__OWNER__"
    assert api["E2B_API_KEY"] == "__MISSING_FROM_DOTENV__"
    assert "__GEN_APP_DB_PASSWORD__" in api["APP_DATABASE_URL"]
    assert voice["PERSONA_VOICE_LIVEKIT_URL"] == values["PERSONA_LIVEKIT_URL"]


def test_render_selfhosted_livekit_and_confirmed_chains(tmp_path: Path) -> None:
    render = _load_render()
    dotenv, values = _fake_dotenv(tmp_path)
    out = tmp_path / "out"
    argv = [
        "--dotenv",
        str(dotenv),
        "--out",
        str(out),
        "--livekit",
        "selfhosted",
        "--chains-from-dotenv",
    ]
    assert render.main(argv) == 0
    voice = _rendered(out, "voice")
    assert voice["PERSONA_VOICE_LIVEKIT_URL"] == "wss://livekit.openpersonasai.com"
    assert voice["PERSONA_VOICE_LIVEKIT_API_KEY"] == "__GEN_LIVEKIT_API_KEY__"
    assert _rendered(out, "common")["PERSONA_FRONTIER_MODELS"] == values["PERSONA_FRONTIER_MODELS"]


def test_render_refuses_to_write_inside_the_checkout(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    render = _load_render()
    dotenv, _ = _fake_dotenv(tmp_path)
    inside = KIT / "env" / "rendered-by-a-test"
    assert render.main(["--dotenv", str(dotenv), "--out", str(inside), "--livekit", "cloud"]) == 1
    assert not inside.exists()


# --- first boot and the backup timer ---------------------------------------------------------


def test_cloud_init_locks_the_box_down_from_first_boot() -> None:
    text = (KIT / "cloud-init.yaml").read_text(encoding="utf-8")
    assert text.startswith("#cloud-config\n")
    config = yaml.safe_load(text)
    assert config["ssh_pwauth"] is False
    (user,) = config["users"]
    assert user["name"] == "deploy"
    # NOT 1000: uid 1000 is the images' `persona` user and owns the app's data.
    assert user["uid"] == 2000
    assert user["lock_passwd"] is True
    assert user["ssh_authorized_keys"] == ["__OWNER_SSH_PUBLIC_KEY__"]
    assert "ufw limit 22/tcp" in config["runcmd"]
    assert "ufw --force enable" in config["runcmd"]
    # Root login is closed by the sshd drop-in, which the lockout guard removes when the
    # deploy key never arrived, so root's console key stays a way in.
    (drop_in,) = [f for f in config["write_files"] if f["path"].endswith("10-open-persona.conf")]
    assert "PermitRootLogin no" in drop_in["content"]
    assert "AllowUsers deploy" in drop_in["content"]
    assert config["disable_root"] is False
    guard = next(c for c in config["runcmd"] if "authorized_keys" in str(c))
    assert "rm -f /etc/ssh/sshd_config.d/10-open-persona.conf" in guard
    assert config["runcmd"].index(guard) < config["runcmd"].index("systemctl restart ssh")


def test_the_key_placeholder_never_passes_for_a_key() -> None:
    """The guard and provision.sh recognise a key by its type prefix; the placeholder has none."""
    key_line = re.compile(r"^(ssh-(ed25519|rsa)|ecdsa-sha2-|sk-)")
    assert not key_line.match("__OWNER_SSH_PUBLIC_KEY__")
    assert key_line.match("ssh-ed25519 AAAAC3Nza fake@example")
    provision = (KIT / "bin" / "provision.sh").read_text(encoding="utf-8")
    assert "^(ssh-(ed25519|rsa)|ecdsa-sha2-|sk-)" in provision


def test_provision_opens_exactly_the_public_ports() -> None:
    provision = (KIT / "bin" / "provision.sh").read_text(encoding="utf-8")
    opened = set(re.findall(r"^ufw (?:allow|limit) (\S+)", provision, re.M))
    assert opened == {"22/tcp", "80/tcp", "443/tcp", "7881/tcp", "7882/udp"}


def test_the_backup_service_runs_the_kit_from_the_provisioned_checkout() -> None:
    service = (KIT / "systemd" / "open-persona-backup.service").read_text(encoding="utf-8")
    assert "ExecStart=/opt/open-persona/repo/deploy/hetzner/bin/backup.sh" in service
    # Root: the data is owned by uid 1000 and the operator (uid 2000) does not own it.
    assert "User=" not in service


@pytest.mark.parametrize("unit", ["open-persona-backup.service", "open-persona-diskcheck.service"])
def test_a_failing_timer_job_raises_an_alert(unit: str) -> None:
    text = (KIT / "systemd" / unit).read_text(encoding="utf-8")
    assert "OnFailure=open-persona-alert@%n.service" in text


def test_provision_installs_every_systemd_file() -> None:
    provision = (KIT / "bin" / "provision.sh").read_text(encoding="utf-8")
    for unit in (KIT / "systemd").iterdir():
        assert unit.name in provision, f"provision.sh never installs {unit.name}"


def test_backup_refuses_below_the_free_space_floor() -> None:
    backup = (KIT / "bin" / "backup.sh").read_text(encoding="utf-8")
    assert "OP_BACKUP_MIN_FREE_GB" in backup
    assert "REFUSED" in backup


# --- the inventory and the MCP seam ----------------------------------------------------------


def test_inventory_lists_presence_and_destination_and_never_a_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    render = _load_render()
    dotenv, values = _fake_dotenv(tmp_path)
    dotenv.write_text(dotenv.read_text(encoding="utf-8") + 'E2B_API_KEY=""\n', encoding="utf-8")

    assert render.main(["--dotenv", str(dotenv), "--inventory"]) == 0
    printed = capsys.readouterr().out
    for value in (v for v in values.values() if len(v) > 8):
        assert value not in printed
    lines = {line.split()[2]: line for line in printed.splitlines() if line.startswith("[ ] ")}
    assert ".env: set" in lines["PERSONA_OPENROUTER_API_KEY"]
    assert ".env: empty" in lines["E2B_API_KEY"]
    assert ".env: absent" in lines["MCP_CREDENTIAL_KEY"]
    assert "-> api.env" in lines["MCP_CREDENTIAL_KEY"]
    # A copy under another name says where it lands; voice gets the api's Clerk PEM.
    voice_pem = [
        line for line in printed.splitlines() if "voice.env as PERSONA_VOICE_JWT_PUBLIC_KEY" in line
    ]
    assert len(voice_pem) == 1
    assert "PERSONA_API_JWT_PUBLIC_KEY" in voice_pem[0]
    # The chains and the LiveKit Cloud sources are listed: a render could copy them.
    assert "PERSONA_FRONTIER_MODELS" in lines
    assert "PERSONA_LIVEKIT_URL" in printed
    # Fixed and generated values are not .env's business and are not listed.
    assert "PERSONA_INITIATIVE_ENABLED" not in lines
    assert "DATABASE_URL" not in lines


def test_inventory_writes_nothing(tmp_path: Path) -> None:
    render = _load_render()
    dotenv, _ = _fake_dotenv(tmp_path)
    before = sorted(tmp_path.rglob("*"))
    assert render.main(["--dotenv", str(dotenv), "--inventory"]) == 0
    assert sorted(tmp_path.rglob("*")) == before


def test_rendering_still_requires_out_and_livekit(tmp_path: Path) -> None:
    render = _load_render()
    dotenv, _ = _fake_dotenv(tmp_path)
    assert render.main(["--dotenv", str(dotenv), "--out", str(tmp_path / "o")]) == 2


def test_the_mcp_seam_is_named_and_the_app_never_gets_the_docker_socket() -> None:
    text = (KIT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "SEAM: per-tenant MCP runtime" in text
    assert "SEAM: per-tenant MCP runtime" in (KIT / "RUNBOOK.md").read_text(encoding="utf-8")
    services = _compose()["services"]
    for name, service in services.items():
        for mount in (str(v) for v in service.get("volumes", [])):
            source = mount.split(":", 1)[0]
            assert "docker.sock" not in mount, f"{name} mounts the Docker socket"
            assert not source.startswith(("/run", "/var/run")), f"{name} mounts {source}"
        assert service.get("privileged") is not True, f"{name} is privileged"
        environment = service.get("environment", {}) or {}
        assert "DOCKER_HOST" not in environment, f"{name} is pointed at a Docker daemon"
