"""``persona repair <path>``: check a persona's memory for broken version chains.

Spec K13, T3 (D-K13-7). The defect this exists for is an interrupted save: a versioned update
used to be written as two operations, and a process killed between them left one version
pointing forward at a version that was never written. That made the memory's history
unreadable and, in the worst shape, made the memory itself invisible to every read while its
rows sat on disk. The write is one operation now, so no NEW chain can break this way, and this
command is for the ones that might already have.

It is preventive, not a cleanup crew. On a healthy store the honest output is one calm line
saying everything is intact, because that is the answer almost every run will give and a
maintenance command that cries wolf gets ignored the one time it matters.

Two things it deliberately will not do. It never repairs anything the version order cannot
justify (see :func:`persona.stores.versioning.diagnose_chain`), and it never loads an embedding
model: repairs move version pointers only, through the transport's ``relink``, so no text is
re-read, no vector is recomputed, and starting this command costs nothing but a database
connection.
"""
# ruff: noqa: B008 - typer.Argument/Option in defaults is the framework idiom

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - typer needs runtime access
from typing import TYPE_CHECKING, Any

import typer

from persona.audit import JSONLAuditLogger
from persona.config import PersonaCoreConfig
from persona.schema.persona import Persona
from persona.stores.core_memory import CORE_MEMORY_KIND, CoreMemoryStore
from persona.stores.episodic import EpisodicStore
from persona.stores.self_facts import SelfFactsStore
from persona.stores.worldview import WorldviewStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.audit import AuditLogger
    from persona.stores.backend import Backend
    from persona.stores.base import StoreDiagnosis, TypedStore

__all__ = ["repair"]

#: The kinds that carry version chains. Identity is immutable at runtime and gist rows are
#: always written as version 1 under a fresh id, so neither can hold a broken chain.
_VERSIONED_KINDS: tuple[str, ...] = ("self_facts", "worldview", "episodic", CORE_MEMORY_KIND)


class _NoEmbedder:
    """An embedder for a command that must never embed.

    Both transports take an embedder at construction because writing a chunk needs one. A
    repair does not write chunks: it moves version pointers through ``relink``, which never
    reads text and never touches a vector. Passing a real embedder here would load torch and
    sentence-transformers (about 100 seconds on a cold cache) to change one column, and on a
    host configured with a different model it could quietly rewrite the stored vectors.

    So this one refuses. If anything on the repair path ever asks it to work, that is a bug in
    the repair path and it should be loud rather than slow.
    """

    model_name: str = "repair-never-embeds"

    @property
    def dimension(self) -> int:
        self._refuse()
        return 0  # pragma: no cover - _refuse always raises

    def encode(self, texts: Sequence[str]) -> list[list[float]]:  # noqa: ARG002
        self._refuse()
        return []  # pragma: no cover - _refuse always raises

    def _refuse(self) -> None:
        msg = (
            "persona repair does not embed anything: it only moves version pointers. "
            "Something on the repair path asked for an embedding, which is a bug."
        )
        raise RuntimeError(msg)


def _build_backend(config: PersonaCoreConfig, database_url: str | None) -> tuple[Backend, str]:
    """Return the transport to repair and a description of it safe to print."""
    if database_url:
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url

        from persona.stores.postgres import PostgresBackend

        url = make_url(database_url)
        engine = create_engine(database_url)
        # Never echo the DSN: it carries a password.
        return (
            PostgresBackend(engine=engine, embedder=_NoEmbedder()),
            f"the database {url.database!r} on {url.host}",
        )

    from persona.stores.chroma import ChromaBackend

    return (
        ChromaBackend(persist_path=config.chroma_path, embedder=_NoEmbedder()),
        f"the local store at {config.chroma_path}",
    )


def _stores(backend: Backend, audit_logger: AuditLogger, kinds: Sequence[str]) -> list[TypedStore]:
    built: dict[str, Any] = {
        "self_facts": SelfFactsStore,
        "worldview": WorldviewStore,
        "episodic": EpisodicStore,
        CORE_MEMORY_KIND: CoreMemoryStore,
    }
    return [built[k](backend=backend, audit_logger=audit_logger) for k in kinds]


def _report(diagnosis: StoreDiagnosis) -> None:
    """Print one store's findings. Called only when there is something to say."""
    for finding in diagnosis.findings:
        typer.echo("")
        typer.echo(f"  {diagnosis.store_kind}: {finding.logical_id}")
        typer.echo(f"    {finding.detail}")
        typer.echo(
            f"    {finding.version_count} saved versions; "
            f"{'this one can be repaired' if finding.repairable else 'left alone, see above'}"
        )


def repair(
    persona_path: Path = typer.Argument(
        ..., help="Path to the persona YAML whose memory should be checked."
    ),
    store: str | None = typer.Option(
        None, "--store", help=f"Check only one store: {', '.join(_VERSIONED_KINDS)}."
    ),
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        help="Check a Postgres-backed store instead of the local one.",
        envvar="PERSONA_REPAIR_DATABASE_URL",
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Repair what can be repaired. Without this, nothing is changed."
    ),
) -> None:
    """Check a persona's memory for broken version chains, and repair them with ``--apply``."""
    persona = Persona.from_yaml(persona_path)
    persona_id = persona.persona_id or persona_path.stem

    if store is not None and store not in _VERSIONED_KINDS:
        raise typer.BadParameter(
            f"invalid --store {store!r}; expected one of {', '.join(_VERSIONED_KINDS)}"
        )
    kinds = (store,) if store is not None else _VERSIONED_KINDS

    config = PersonaCoreConfig()
    backend, where = _build_backend(config, database_url)
    audit_root = config.audit_path or (config.chroma_path / "audit")
    audit_logger = JSONLAuditLogger(audit_root)

    typer.echo(f"Checking {persona_id} in {where}.")

    stores = _stores(backend, audit_logger, kinds)
    diagnoses = [s.diagnose(persona_id) for s in stores]
    checked = sum(d.chains_checked for d in diagnoses)
    findings = [f for d in diagnoses for f in d.findings]

    if not findings:
        typer.echo(f"All {checked} version chains are intact. Nothing to repair.")
        return

    typer.echo(f"{len(findings)} of {checked} version chains need attention.")
    for diagnosis in diagnoses:
        _report(diagnosis)
    typer.echo("")

    repairable = [f for f in findings if f.repairable]
    refused = [f for f in findings if not f.repairable]

    if not apply:
        if repairable:
            typer.echo(
                f"Nothing has been changed. Run the same command with --apply to repair "
                f"{len(repairable)} of these."
            )
        if refused:
            typer.echo(
                f"{len(refused)} cannot be repaired automatically without guessing, so they "
                "are reported and left exactly as they are."
            )
        raise typer.Exit(code=1)

    repaired = sum(s.repair(persona_id, written_by="cli.repair") for s in stores)
    typer.echo(f"Repaired {repaired} version chains.")

    # Re-check rather than trust the count above. If a repair reported success and the chain
    # is still broken, the operator has to hear that from us and not from the next failure.
    remaining = [f for s in stores for f in s.diagnose(persona_id).findings]
    if not remaining:
        typer.echo("Every version chain is now intact.")
        return

    still_repairable = [f for f in remaining if f.repairable]
    if still_repairable:
        typer.echo(
            f"{len(still_repairable)} chains are still broken after the repair ran, which "
            "should not happen. Nothing has been lost: every version is still on disk. "
            "Please report this with the lines above."
        )
    left_alone = len(remaining) - len(still_repairable)
    if left_alone:
        typer.echo(
            f"{left_alone} were left alone because repairing them would mean guessing. "
            "They are listed above, and each one says what is known about it."
        )
    raise typer.Exit(code=1)
