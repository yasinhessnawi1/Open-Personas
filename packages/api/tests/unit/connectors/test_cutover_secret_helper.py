"""The cutover secret-copy helper must never put the two DSNs on the api (Spec I1 T5).

``scripts/migration/set-connector-secrets-on-api.sh`` copies the captured
``PERSONA_CONNECTORS_*`` secrets onto the api app for the fold. Two of those names are
DSNs that must NOT travel: embedded, the connectors run on the engines the api already
built, so the api's ``APP_DATABASE_URL`` is the owner-scoped ``persona_app`` engine and
its ``DATABASE_URL`` is the cross-tenant dispatch engine (D-I1-18). Copying the
connectors' own copies over would put a second, independently-drifting definition of the
same two roles on the app.

That matters because of what going wrong looks like. R9-123 was an owner-scoped engine
running on a role Postgres exempts from row-level security: every owner scope was set
correctly and honoured by nothing, and one linked Telegram user was offered fifteen
personas across five accounts. Nothing logged, nothing refused. The excluded variables are
inert on the api today, which is precisely why a wrong value would sit there unnoticed
until something started reading them.

The script is driven for real, in its dry-run mode, against a fixture that CONTAINS both
forbidden names. Reading the script's source for a grep pattern would pass just as happily
against a filter that never runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _repo_script() -> Path:
    """Walk up to the repo root rather than counting parents, which silently mis-resolves."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "scripts" / "migration" / "set-connector-secrets-on-api.sh"
        if candidate.is_file():
            return candidate
    msg = "set-connector-secrets-on-api.sh not found above this test"
    raise AssertionError(msg)


_SCRIPT = _repo_script()

_FORBIDDEN = ("PERSONA_CONNECTORS_DATABASE_URL", "PERSONA_CONNECTORS_APP_DATABASE_URL")

_FIXTURE = """\
PERSONA_CONNECTORS_TELEGRAM_BOT_TOKEN=123:abcdef
PERSONA_CONNECTORS_DATABASE_URL=postgresql+psycopg://persona:secret@db.internal:5432/persona
PERSONA_CONNECTORS_SLACK_BOT_TOKEN=xoxb-test
PERSONA_CONNECTORS_APP_DATABASE_URL=postgresql+psycopg://persona_app:secret@db.internal:5432/persona
PERSONA_CONNECTORS_TWILIO_WEBHOOK_AUTH_TOKEN=twilio-token
DATABASE_URL=postgresql+psycopg://persona:secret@db.internal:5432/persona
"""


@pytest.fixture
def captured_secrets(tmp_path: Path) -> Path:
    path = tmp_path / "open-persona-connectors.env"
    path.write_text(_FIXTURE, encoding="utf-8")
    return path


def _dry_run(source: Path) -> str:
    """Run the helper with no ``--apply``. It must write nothing and touch no network."""
    result = subprocess.run(  # noqa: S603 — fixed argv, repo-local script
        ["/bin/bash", str(_SCRIPT), str(source)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout


def test_the_two_role_dsns_are_never_in_the_planned_copy(captured_secrets: Path) -> None:
    """The required exclusion, asserted on the plan the script actually produces."""
    out = _dry_run(captured_secrets)

    planned = {line.split()[1] for line in out.splitlines() if line.strip().startswith("COPY ")}
    assert planned, f"the script planned to copy nothing; output was:\n{out}"

    for name in _FORBIDDEN:
        assert name not in planned, (
            f"{name} is in the planned copy. Embedded, the api supplies that role itself "
            f"(D-I1-18); a second definition on the app is how R9-123 comes back."
        )


def test_the_excluded_names_are_reported_as_skipped_not_silently_dropped(
    captured_secrets: Path,
) -> None:
    """Silence would read as 'they were not in the capture', which is a different fact.

    An operator comparing the count against ``flyctl secrets list`` needs to see WHY the
    numbers differ, or the next person re-adds them by hand.
    """
    out = _dry_run(captured_secrets)

    skipped = {line.split()[1] for line in out.splitlines() if line.strip().startswith("SKIP ")}
    assert skipped == set(_FORBIDDEN), f"expected both DSNs reported as skipped; saw {skipped}"
    assert "copy:   3" in out, f"expected 3 copyable secrets from the fixture; output:\n{out}"


def test_the_dry_run_copies_the_real_connector_credentials(captured_secrets: Path) -> None:
    """The exclusion must not become a filter that drops everything.

    Without this, a broken pattern that matched nothing would satisfy the two tests above
    while making the script useless.
    """
    out = _dry_run(captured_secrets)
    planned = {line.split()[1] for line in out.splitlines() if line.strip().startswith("COPY ")}

    assert planned == {
        "PERSONA_CONNECTORS_TELEGRAM_BOT_TOKEN",
        "PERSONA_CONNECTORS_SLACK_BOT_TOKEN",
        "PERSONA_CONNECTORS_TWILIO_WEBHOOK_AUTH_TOKEN",
    }
    # Names outside the connector prefix are the api's own and never travel.
    assert "DATABASE_URL" not in planned


def test_the_dry_run_writes_nothing_and_never_prints_a_secret_value(
    captured_secrets: Path,
) -> None:
    """Default-dry-run is load-bearing: ``flyctl secrets set`` RESTARTS the app, and during
    cutover the number and timing of restarts is what keeps two consumers off one Slack
    socket. The audit line carries the name and a length, never the value."""
    out = _dry_run(captured_secrets)

    assert "DRY RUN. Nothing was written." in out
    for secret in ("123:abcdef", "xoxb-test", "twilio-token", "secret@db.internal"):
        assert secret not in out, f"the dry run printed a secret value: {secret!r}"
