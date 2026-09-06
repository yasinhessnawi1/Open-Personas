"""The api's Fly env carries every connector env setting the standalone app carries (R9-147).

The connectors fold (Spec I1) copies the connector SECRETS onto the api with a helper, but
the standalone app also configured two non-secret settings in its ``fly.toml`` ``[env]``
block: the JWT algorithm list and the audience the link routes verify Clerk tokens against.
The first cutover attempt missed them, ``ConnectorConfig`` fell back to its ``HS256``
default, and the embedded start failed with "JWKS verifier only supports asymmetric
algorithms". This test makes that drift a red build instead of a production discovery:
every ``PERSONA_CONNECTORS_*`` key in the connectors ``[env]`` must appear with the same
value in the api ``[env]``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parents[5]
_API_FLY = _REPO / "packages" / "api" / "fly.toml"
_CONNECTORS_FLY = _REPO / "packages" / "connectors" / "fly.toml"


def _env(path: Path) -> dict[str, str]:
    with path.open("rb") as fh:
        return {k: str(v) for k, v in tomllib.load(fh).get("env", {}).items()}


def test_api_env_carries_every_connector_env_setting_with_the_same_value() -> None:
    connectors = {
        k: v for k, v in _env(_CONNECTORS_FLY).items() if k.startswith("PERSONA_CONNECTORS_")
    }
    api = _env(_API_FLY)

    assert connectors, "the connectors fly.toml no longer sets any PERSONA_CONNECTORS_* env"
    missing = sorted(k for k in connectors if k not in api)
    assert not missing, f"api fly.toml [env] is missing connector settings: {missing}"
    differing = sorted(k for k in connectors if api[k] != connectors[k])
    assert not differing, (
        f"api fly.toml [env] disagrees with the connectors fly.toml on: {differing}"
    )


def test_the_connector_jwt_settings_match_the_api_own_jwt_settings() -> None:
    """Both prefixes verify the same Clerk tokens, so they must agree on algorithm and audience."""
    api = _env(_API_FLY)
    assert api["PERSONA_CONNECTORS_JWT_ALGORITHMS"] == api["PERSONA_API_JWT_ALGORITHMS"]
    assert api["PERSONA_CONNECTORS_JWT_AUDIENCE"] == api["PERSONA_API_JWT_AUDIENCE"]
