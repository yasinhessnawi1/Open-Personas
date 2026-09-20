"""Typer ``app`` wiring all subcommands.

The CLI entry-point declared in ``packages/core/pyproject.toml`` is
``persona = "persona.cli.main:app"``.
"""

from __future__ import annotations

import typer

from persona.cli.audit_cmd import audit
from persona.cli.chat_cmd import chat
from persona.cli.init_cmd import init
from persona.cli.repair_cmd import repair
from persona.cli.run_cmd import run
from persona.cli.validate_cmd import validate

__all__ = ["app"]

app = typer.Typer(
    name="persona",
    help="Create and chat with AI personas backed by typed memory.",
    no_args_is_help=True,
    add_completion=False,
)
app.command()(init)
app.command()(validate)
app.command()(chat)
app.command()(audit)
app.command()(run)
app.command()(repair)


if __name__ == "__main__":
    app()
