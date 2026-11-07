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

from dataclasses import dataclass
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
    from persona.stores.versioning import ChainDiagnosis

__all__ = ["VERSIONED_KINDS", "NoEmbedder", "repair", "run_forgotten_sweep", "run_repair"]

#: The kinds that carry version chains. Identity is immutable at runtime and gist rows are
#: always written as version 1 under a fresh id, so neither can hold a broken chain.
VERSIONED_KINDS: tuple[str, ...] = ("self_facts", "worldview", "episodic", CORE_MEMORY_KIND)

_RACED_HINT = (
    "{count} were left by two saves racing. Add --resolve-raced to keep every version, in the "
    "order they were saved."
)


class NoEmbedder:
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


@dataclass(frozen=True)
class _Target:
    """The transport to repair, what to audit into, and a description safe to print."""

    backend: Backend
    audit_logger: AuditLogger
    where: str


def _build_target(config: PersonaCoreConfig, database_url: str | None) -> _Target:
    if database_url:
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url

        from persona.audit_postgres import PostgresAuditLogger
        from persona.stores.postgres import PostgresBackend

        url = make_url(database_url)
        engine = create_engine(database_url)
        # Never echo the DSN: it carries a password.
        return _Target(
            backend=PostgresBackend(engine=engine, embedder=NoEmbedder()),
            # Into the database, not a file: this runs in a one-shot container on a hosted
            # deploy, and a JSONL file there would vanish with the record of what was changed.
            audit_logger=PostgresAuditLogger(engine),
            where=f"the database {url.database!r} on {url.host}",
        )

    from persona.stores.chroma import ChromaBackend

    return _Target(
        backend=ChromaBackend(persist_path=config.chroma_path, embedder=NoEmbedder()),
        audit_logger=JSONLAuditLogger(config.audit_path or (config.chroma_path / "audit")),
        where=f"the local store at {config.chroma_path}",
    )


def _stores(backend: Backend, audit_logger: AuditLogger, kinds: Sequence[str]) -> list[TypedStore]:
    built: dict[str, Any] = {
        "self_facts": SelfFactsStore,
        "worldview": WorldviewStore,
        "episodic": EpisodicStore,
        CORE_MEMORY_KIND: CoreMemoryStore,
    }
    return [built[k](backend=backend, audit_logger=audit_logger) for k in kinds]


def _report(persona_id: str, diagnosis: StoreDiagnosis, *, name_persona: bool) -> None:
    """Print one store's findings. Called only when there is something to say."""
    for finding in diagnosis.findings:
        typer.echo("")
        prefix = f"{persona_id} " if name_persona else ""
        typer.echo(f"  {prefix}{diagnosis.store_kind}: {finding.logical_id}")
        typer.echo(f"    {finding.detail}")
        if finding.repairable:
            verdict = "this one can be repaired"
        elif finding.resolvable:
            verdict = "this one can be resolved with --resolve-raced"
        else:
            verdict = "left alone, see above"
        typer.echo(f"    {finding.version_count} saved versions; {verdict}")


def run_repair(
    *,
    backend: Backend,
    audit_logger: AuditLogger,
    persona_ids: Sequence[str],
    kinds: Sequence[str] = VERSIONED_KINDS,
    apply: bool = False,
    resolve_raced: bool = False,
    written_by: str = "cli.repair",
) -> int:
    """Check these personas' chains, repair them if asked, and return the exit code.

    The body of ``persona repair``, separate so a deployment whose database is not reachable
    from a DSN on the command line can drive the same check with its own transport. 0 means
    every chain is intact, 1 means something still needs attention.
    """
    stores = _stores(backend, audit_logger, kinds)
    swept = [(pid, s.diagnose(pid)) for pid in persona_ids for s in stores]
    checked = sum(d.chains_checked for _, d in swept)
    findings = [f for _, d in swept for f in d.findings]

    if not findings:
        typer.echo(f"All {checked} version chains are intact. Nothing to repair.")
        return 0

    typer.echo(f"{len(findings)} of {checked} version chains need attention.")
    for persona_id, diagnosis in swept:
        _report(persona_id, diagnosis, name_persona=len(persona_ids) > 1)
    typer.echo("")

    if not apply:
        _say_what_a_run_would_do(findings, resolve_raced=resolve_raced)
        return 1

    repaired = sum(
        s.repair(pid, written_by=written_by, resolve_raced=resolve_raced)
        for pid in persona_ids
        for s in stores
    )
    typer.echo(f"Repaired {repaired} version chains.")

    # Re-check rather than trust the count above. If a repair reported success and the chain
    # is still broken, the operator has to hear that from us and not from the next failure.
    remaining = [f for pid in persona_ids for s in stores for f in s.diagnose(pid).findings]
    if not remaining:
        typer.echo("Every version chain is now intact.")
        return 0
    _say_what_is_left(remaining, resolve_raced=resolve_raced)
    return 1


def _say_what_a_run_would_do(findings: Sequence[ChainDiagnosis], *, resolve_raced: bool) -> None:
    repairable = [f for f in findings if f.repairable]
    raced = [f for f in findings if f.resolvable]
    refused = [f for f in findings if not f.repairable and not f.resolvable]
    if repairable or (raced and resolve_raced):
        count = len(repairable) + (len(raced) if resolve_raced else 0)
        typer.echo(
            f"Nothing has been changed. Run the same command with --apply to repair {count} of "
            "these."
        )
    if raced and not resolve_raced:
        typer.echo(_RACED_HINT.format(count=len(raced)))
    if refused:
        typer.echo(
            f"{len(refused)} cannot be repaired automatically without guessing, so they "
            "are reported and left exactly as they are."
        )


def _say_what_is_left(remaining: Sequence[ChainDiagnosis], *, resolve_raced: bool) -> None:
    still_repairable = [f for f in remaining if f.repairable or (f.resolvable and resolve_raced)]
    raced = [f for f in remaining if f.resolvable and not resolve_raced]
    if still_repairable:
        typer.echo(
            f"{len(still_repairable)} chains are still broken after the repair ran, which "
            "should not happen. Nothing has been lost: every version is still on disk. "
            "Please report this with the lines above."
        )
    if raced:
        typer.echo(_RACED_HINT.format(count=len(raced)))
    left_alone = len(remaining) - len(still_repairable) - len(raced)
    if left_alone:
        typer.echo(
            f"{left_alone} were left alone because repairing them would mean guessing. "
            "They are listed above, and each one says what is known about it."
        )


def run_forgotten_sweep(
    *,
    backend: Backend,
    audit_logger: AuditLogger,
    persona_ids: Sequence[str],
    apply: bool = False,
    written_by: str = "cli.repair",
) -> int:
    """Find memory summaries citing evidence that is gone, and delete them with ``apply``.

    For restored data (R9-209): a forget now reaches the summaries built from what it deleted,
    but rows written before that rule still cite it. This reads citations, deletes versions and
    closes the chain, so it needs no model of any kind. The next ordinary pass rebuilds from
    what remains. Returns the exit code: 0 clean, 1 something still needs attention.
    """
    store = CoreMemoryStore(backend=backend, audit_logger=audit_logger)
    found = {pid: ids for pid in persona_ids if (ids := store.orphaned_citations(pid))}
    if not found:
        typer.echo(f"No memory summary in {len(persona_ids)} personas cites anything forgotten.")
        return 0

    typer.echo(
        f"{len(found)} of {len(persona_ids)} personas have a memory summary citing something "
        "that has been forgotten."
    )
    for persona_id, ids in found.items():
        typer.echo(f"  {persona_id}: {len(ids)} forgotten sources are cited")
    if not apply:
        typer.echo(
            "Nothing has been changed. Run the same command with --apply to delete the versions "
            "citing them. The summary is rebuilt from what remains on the next pass."
        )
        return 1

    removed = sum(store.forget_cited(pid, ids, written_by=written_by) for pid, ids in found.items())
    typer.echo(f"Deleted {removed} versions.")
    if any(store.orphaned_citations(pid) for pid in found):
        typer.echo("Some are still there after the sweep ran, which should not happen.")
        return 1
    typer.echo("No memory summary cites anything forgotten now.")
    return 0


def repair(
    persona_path: Path | None = typer.Argument(
        None, help="Path to the persona YAML whose memory should be checked."
    ),
    all_personas: bool = typer.Option(
        False,
        "--all-personas",
        help="Check every persona in the database instead. Needs --database-url.",
    ),
    store: str | None = typer.Option(
        None, "--store", help=f"Check only one store: {', '.join(VERSIONED_KINDS)}."
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
    resolve_raced: bool = typer.Option(
        False,
        "--resolve-raced",
        help=(
            "Also settle chains two saves raced: keep every version, in the order they were "
            "saved, and renumber them. Still needs --apply to change anything."
        ),
    ),
    forgotten_evidence: bool = typer.Option(
        False,
        "--forgotten-evidence",
        help=(
            "Instead of checking version chains, find memory summaries citing something that "
            "has been forgotten, as restored data can. --apply deletes those versions. Uses no "
            "model."
        ),
    ),
) -> None:
    """Check a persona's memory for broken version chains, and repair them with ``--apply``."""
    if all_personas == (persona_path is not None):
        raise typer.BadParameter("give a persona YAML or --all-personas, one or the other")
    if all_personas and not database_url:
        raise typer.BadParameter("--all-personas needs --database-url: only a database lists them")
    if store is not None and store not in VERSIONED_KINDS:
        raise typer.BadParameter(
            f"invalid --store {store!r}; expected one of {', '.join(VERSIONED_KINDS)}"
        )
    if forgotten_evidence and (resolve_raced or store is not None):
        raise typer.BadParameter("--forgotten-evidence runs on its own, not with chain options")
    kinds = (store,) if store is not None else VERSIONED_KINDS
    if forgotten_evidence:
        kinds = (CORE_MEMORY_KIND,)

    target = _build_target(PersonaCoreConfig(), database_url)
    if persona_path is not None:
        persona = Persona.from_yaml(persona_path)
        persona_ids = [persona.persona_id or persona_path.stem]
        typer.echo(f"Checking {persona_ids[0]} in {target.where}.")
    else:
        persona_ids = _every_persona(target, kinds)
        typer.echo(f"Checking {len(persona_ids)} personas in {target.where}.")

    if forgotten_evidence:
        code = run_forgotten_sweep(
            backend=target.backend,
            audit_logger=target.audit_logger,
            persona_ids=persona_ids,
            apply=apply,
        )
    else:
        code = run_repair(
            backend=target.backend,
            audit_logger=target.audit_logger,
            persona_ids=persona_ids,
            kinds=kinds,
            apply=apply,
            resolve_raced=resolve_raced,
        )
    if code != 0:
        raise typer.Exit(code=code)


def _every_persona(target: _Target, kinds: Sequence[str]) -> list[str]:
    """Every persona id the sweep should visit, refusing a role that would see too few."""
    from persona.stores.postgres import PostgresBackend

    backend = target.backend
    assert isinstance(backend, PostgresBackend)  # --all-personas requires --database-url
    if not backend.sees_every_tenant():
        typer.echo(
            "This role is subject to row level security, so it cannot see every persona, and a "
            "sweep would report a healthy database whatever the truth was. Use the superuser "
            "connection."
        )
        raise typer.Exit(code=2)
    return backend.persona_ids(tuple(kinds))
