"""One list of credential shapes, used to scrub logs and to refuse to print (R9-213).

``redact_secrets`` masks credential-looking text in provider errors, and the model-chain
boot line refuses to print a chain entry that looks like a credential. Both read the same
list in ``persona.logging``, so a shape added for one protects the other.

Masking is asserted on the exact output, never with ``not in``: a pattern that stops at a
``.`` masks the head of a key and prints its tail, and ``secret not in output`` passes on
that. Fake credentials are assembled at runtime so no literal in this file has a real
token's shape (push protection and secret scanners would rightly object to one).
"""

from __future__ import annotations

import pytest
from persona.logging import looks_like_secret, redact_secrets

_ALNUM_40 = "Ab3" * 13 + "x"
_MASKED = "provider said: <redacted> was rejected"
_JWT = (
    "ey" + "JhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    "." + "eyJzdWIiOiJ1c2VyXzEyMyIsImV4cCI6MTcwMDAwMDAwMH0"
    "." + "c2lnbmF0dXJlLXBhcnQtb2YtdGhlLXRva2Vu"
)

#: Shapes recognised by a prefix, which must also mask whatever key alphabet follows it.
PREFIXED = [
    pytest.param("sk-" + "or-v1-" + _ALNUM_40, id="openrouter"),
    pytest.param("sk-" + "proj-" + _ALNUM_40, id="openai-project"),
    pytest.param("sk-" + "ant-api03-" + _ALNUM_40, id="anthropic"),
    pytest.param("sk" + "_live_" + _ALNUM_40, id="stripe-secret"),
    pytest.param("rk" + "_test_" + _ALNUM_40, id="stripe-restricted"),
    pytest.param("whsec" + "_" + _ALNUM_40, id="stripe-webhook"),
    pytest.param("gsk" + "_" + _ALNUM_40, id="groq"),
    pytest.param("nvapi" + "-" + _ALNUM_40, id="nvidia"),
    pytest.param("gh" + "p_" + _ALNUM_40[:36], id="github-classic"),
    pytest.param("github" + "_pat_" + _ALNUM_40, id="github-fine-grained"),
    pytest.param("AKIA" + "IOSFODNN7EXAMPLE", id="aws-access-key-id"),
    pytest.param("AI" + "za" + _ALNUM_40[:35], id="google"),
    pytest.param("hf" + "_" + _ALNUM_40[:34], id="huggingface"),
    pytest.param("xox" + "b-1234567890-" + _ALNUM_40[:24], id="slack"),
    pytest.param(_JWT, id="jwt"),
]

SECRETS = [
    *PREFIXED,
    pytest.param("-----BEGIN" + " PRIVATE KEY-----", id="pem"),
    pytest.param("-----BEGIN" + " RSA PRIVATE KEY-----", id="pem-rsa"),
    pytest.param("-----BEGIN" + " OPENSSH PRIVATE KEY-----", id="pem-openssh"),
    pytest.param("-----BEGIN" + " ENCRYPTED PRIVATE KEY-----", id="pem-encrypted"),
    pytest.param("-----BEGIN" + " PGP PRIVATE KEY BLOCK-----", id="pgp-private-key-block"),
    pytest.param("Bearer " + _ALNUM_40, id="bearer"),
    pytest.param("api_key=" + _ALNUM_40[:12], id="labelled"),
]

#: Real model slugs, including long ones and ones that end in :free. None of these is a
#: secret, and the long ones are exactly what a generic "long opaque run" rule misfires on.
NOT_SECRETS = [
    "openrouter/nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/anthropic/claude-sonnet-5",
    "openrouter/z-ai/glm-5.3-flash",
    "groq/llama-3.1-8b-instant",
    "deepseek/deepseek-chat",
    "openai/gpt-4o",
    "openrouter/meta-llama/llama-3.3-70b-instruct:free",
    "openrouter/openai/gpt-oss-120b:free",
    "nvidia/flux.2-klein-4b",
]

#: Text that merely resembles a credential and must pass through ``redact_secrets`` intact.
NOT_MASKED = [
    pytest.param("flag sk_learn_compat_module_enabled is on", id="sk-underscore-identifier"),
    pytest.param("-----BEGIN PUBLIC KEY-----", id="pem-public-key"),
    pytest.param("-----BEGIN CERTIFICATE-----", id="pem-certificate"),
    pytest.param("-----BEGIN PGP PUBLIC KEY BLOCK-----", id="pgp-public-key-block"),
]


@pytest.mark.parametrize("secret", SECRETS)
def test_every_known_credential_shape_is_recognised(secret: str) -> None:
    assert looks_like_secret(secret)
    assert looks_like_secret(f"openrouter/{secret}")


@pytest.mark.parametrize("secret", SECRETS)
def test_redact_secrets_masks_every_known_credential_shape_whole(secret: str) -> None:
    assert redact_secrets(f"provider said: {secret} was rejected") == _MASKED


@pytest.mark.parametrize("secret", PREFIXED)
def test_a_prefixed_key_is_masked_through_dots_pluses_and_slashes(secret: str) -> None:
    """Key alphabets carry ``.``, ``+``, ``/`` and ``=``. A pattern that stopped at the first
    of them masked the head and printed the rest of the key."""
    tail = ".T4il+m0re/r3st="
    assert redact_secrets(f"provider said: {secret}{tail} was rejected") == _MASKED


def test_a_restricted_stripe_key_with_a_dotted_tail_is_masked_whole() -> None:
    key = "rk" + "_live_" + ("Ab3" * 7) + "." + ("Xy9Wv8" * 4)
    assert redact_secrets(f"provider said: {key} was rejected") == _MASKED


@pytest.mark.parametrize("text", NOT_MASKED)
def test_text_that_only_resembles_a_credential_is_left_alone(text: str) -> None:
    assert redact_secrets(text) == text
    assert not looks_like_secret(text)


@pytest.mark.parametrize("slug", NOT_SECRETS)
def test_a_real_model_slug_is_not_a_secret(slug: str) -> None:
    assert not looks_like_secret(slug)
