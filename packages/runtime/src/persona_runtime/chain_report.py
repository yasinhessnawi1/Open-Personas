"""Which model chains this process was handed, said once at boot (R9-213).

Each hosted process reads its OWN copy of every model list: the api composes a tier
registry, and voice composes its own (D-V5-6) rather than asking the api for a turn. On
2026-09-20 the owner updated the api's chains, chat moved, calls did not, and nothing
anywhere said the update was partial; the only way to see it was to compare secret
digests app by app. This is the line that makes a mismatch visible in two log reads.

It reports the settings in :data:`persona_runtime.tier.CHAIN_ENV_NAMES`, each with its
parsed model list and a short fingerprint (the first 8 hex of sha256 over the
``provider/model`` entries joined by ``,``), and says ``unset`` or ``empty`` explicitly.
Model slugs are not secrets, but a chain VALUE is operator-typed, and a bad copy has
already reached a production app once (one value in all three voice free chains). A
quoting slip, a pasted ``.env`` line, a URL with credentials and a key typed where a model
belongs all parse as a model list. So an entry is printed only when it is plainly a model
slug (:func:`_printable`). A chain with any other entry is reported, whole and with NO
fingerprint (an unsalted 32-bit hash of a pasted password is a guessing oracle), as one of:

* ``withheld``: it parses, so the registry builder serves it. It is configured, not
  broken, and counts in "N of 6 set"; only its display is held back.
* ``malformed``: it does not parse as a model list, so no builder serves it as written.

Nothing else from the environment is read.

The residual, stated plainly (owner ruling, R9-213 option a: print slug-shaped names rather
than only catalogue ids). A bare, unprefixed secret typed directly after a valid
``provider/`` (a short password such as ``openrouter/hunter2-summer24``, with no
recognisable key prefix, no ``=``, ``@``, whitespace or ``://``, and no unbroken run of 32
letters and digits) looks like a model slug, so it is printed, together with its
fingerprint. The fingerprint is what the cross-app check relies on: two processes handed
the same list print the same one. ``test_the_documented_residual_...`` pins this, so
narrowing it is a ruling, not a refactor.

The report can never stop a process from booting: any failure inside it becomes one
WARNING naming only the exception type.

This is the CONFIGURED list. The retired-model and OpenRouter free-mode filters run later,
inside the registry builders, and log their own warnings when they drop a slot.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from persona.backends.credentials import parse_models_list
from persona.backends.errors import LocalProviderInModelsListError, MalformedTierModelsError
from persona.logging import get_logger, looks_like_secret

from persona_runtime.tier import CHAIN_ENV_NAMES

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "ChainReport",
    "chain_fingerprint",
    "format_model_chains",
    "log_model_chains_at_boot",
    "model_chains",
]

_log = get_logger("runtime.chains")

#: Hex characters of the sha256 kept as the fingerprint: short enough to compare by eye
#: across two log lines, long enough that two different chains colliding is not a concern.
_FINGERPRINT_HEX_CHARS = 8

#: A printable entry: ``provider/model`` made of letters, digits and ``._:/-`` only. The
#: class leaves out ``=``, whitespace, control characters and ``@`` by construction, which
#: are what a quoting slip, a pasted ``.env`` line and a URL with credentials bring. The one
#: ``@`` allowed is the Cloudflare Workers AI namespace that opens a model id (``@cf/``,
#: ``@hf/``), so a real Workers AI chain is not withheld.
_PRINTABLE_ENTRY = re.compile(r"[A-Za-z0-9._-]+/(?:@(?:cf|hf)/)?[A-Za-z0-9._:/-]+")
#: ``:`` and ``/`` are each fine in a slug (``:free``, ``vendor/model``); together they are
#: a URL, and a URL is not a model.
_URL_SCHEME = "://"
#: A long unbroken run of letters and digits is how a hex or base62 key looks; model slugs
#: separate their words with ``-`` or ``.``.
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9]{32,}")

ChainState = Literal["set", "withheld", "empty", "unset", "malformed"]

#: The states of a chain the registry builder serves: counted in "N of 6 set".
_CONFIGURED: frozenset[ChainState] = frozenset({"set", "withheld"})


@dataclass(frozen=True)
class ChainReport:
    """One chain setting as this process sees it.

    Attributes:
        name: The environment name, e.g. ``PERSONA_MID_MODELS``.
        state: ``set`` (parsed, every entry printable), ``withheld`` (parsed and served,
            but an entry is not plainly a model slug), ``empty`` (present, blank),
            ``unset`` (absent) or ``malformed`` (present, does not parse as a model list).
        models: The parsed ``provider/model`` entries in chain order; empty unless ``set``.
        fingerprint: :func:`chain_fingerprint` of the entries; ``None`` unless ``set``.
    """

    name: str
    state: ChainState
    models: tuple[str, ...]
    fingerprint: str | None


def chain_fingerprint(models: Sequence[str]) -> str:
    """First 8 hex of sha256 over the entries joined by ``,``.

    Order is part of a chain (the fallback walks it in order, D-20-4), so it is part of the
    fingerprint: the same models in a different order are a different chain.
    """
    canonical = ",".join(models)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_FINGERPRINT_HEX_CHARS]


def _tier_label(name: str) -> str:
    """``PERSONA_FREE_MID_MODELS`` -> ``free_mid``: the parser uses it only in its messages."""
    return name.removeprefix("PERSONA_").removesuffix("_MODELS").lower()


def _printable(entry: str) -> bool:
    """Whether ``entry`` is plainly a model slug and nothing that could be a credential."""
    return (
        _PRINTABLE_ENTRY.fullmatch(entry) is not None
        and _URL_SCHEME not in entry
        and _OPAQUE_RUN.search(entry) is None
        and not looks_like_secret(entry)
    )


def _report_one(name: str, raw: str | None) -> ChainReport:
    if raw is None:
        return ChainReport(name=name, state="unset", models=(), fingerprint=None)
    if not raw.strip():
        return ChainReport(name=name, state="empty", models=(), fingerprint=None)
    try:
        parsed = parse_models_list(_tier_label(name), raw)
    except (MalformedTierModelsError, LocalProviderInModelsListError):
        return ChainReport(name=name, state="malformed", models=(), fingerprint=None)
    models = tuple(f"{provider}/{model}" for provider, model in parsed)
    if not all(_printable(entry) for entry in models):
        return ChainReport(name=name, state="withheld", models=(), fingerprint=None)
    return ChainReport(name=name, state="set", models=models, fingerprint=chain_fingerprint(models))


def model_chains(env: Mapping[str, str] | None = None) -> tuple[ChainReport, ...]:
    """One :class:`ChainReport` per name in :data:`CHAIN_ENV_NAMES`, in that order.

    Args:
        env: The environment to read; defaults to a snapshot of ``os.environ``.
    """
    snapshot = dict(os.environ if env is None else env)
    return tuple(_report_one(name, snapshot.get(name)) for name in CHAIN_ENV_NAMES)


def _segment(report: ChainReport) -> str:
    if report.state == "set":
        return f"{report.name}=[{','.join(report.models)}] fp={report.fingerprint}"
    return f"{report.name}={report.state}"


def format_model_chains(reports: Iterable[ChainReport]) -> str:
    """Every chain on one line, ``|``-separated, in report order."""
    return " | ".join(_segment(report) for report in reports)


def log_model_chains_at_boot(env: Mapping[str, str] | None = None) -> None:
    """One INFO line at boot: how many chains are set (withheld ones included), then each.

    One line, so the api's and voice's can be compared as two units. Read it with
    ``grep "model chains at boot"``. Never raises: a report that fails logs one WARNING
    naming the exception type only (its message could carry the value) and returns.
    """
    try:
        reports = model_chains(env)
        chains = format_model_chains(reports)
    except Exception as exc:  # noqa: BLE001 - a boot report must never stop a boot
        _log.warning("model chains at boot: report unavailable ({error})", error=type(exc).__name__)
        return
    configured = sum(1 for report in reports if report.state in _CONFIGURED)
    _log.info(
        "model chains at boot: {configured} of {total} set | {chains}",
        configured=configured,
        total=len(reports),
        chains=chains,
    )
