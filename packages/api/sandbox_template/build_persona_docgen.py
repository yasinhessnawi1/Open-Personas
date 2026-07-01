#!/usr/bin/env python3
"""Build + publish the Open Persona custom E2B sandbox template (Spec P5).

**Owner-run, one-time (and on lib security patches).** This script is *not* imported
by the API and is not part of its runtime dependencies — it uses the **`e2b` v2 SDK**
(the Build System 2.0 ``Template`` builder), whereas the API runtime uses
``e2b-code-interpreter>=1.0,<2`` unchanged. See ``README.md`` in this directory for
the full runbook (prerequisites, how to run, how to wire the alias into deploy).

What it does (P5-D-1): extend E2B's base ``code-interpreter-v1`` template — inheriting
its Jupyter start command + config via ``from_template`` (a Dockerfile would force
re-declaring that start cmd, a boot-breaking footgun) — and **pip-install the two
document-generation libs the base lacks** (P5-D-2): ``reportlab`` + ``python-pptx``.
The base already ships pillow + scipy + pandas/matplotlib/numpy/openpyxl/python-docx,
so nothing else is added.

**Egress invariant (D-12-4) — unchanged.** The pip install here runs at *build* time
(PyPI reachable while building the image); it bakes the libs into the image's
site-packages. At **runtime** the sandbox still runs with ``allow_internet_access=False``
(enforced in ``persona_api.sandbox.hosted``), and the libs import with **zero network** —
that is the whole point. This script does not touch egress; there is no runtime pip.

Result: a published template with the alias below. Set it as ``PERSONA_SANDBOX_TEMPLATE``
in the deploy (a Fly secret); rebuilding under the same alias (e.g. for a reportlab
security release) updates it in place, so the secret never changes (P5-D-3).
"""

from __future__ import annotations

import os

from e2b import Template, default_build_logger

# P5-D-2: the exact libs absent from the base ``code-interpreter-v1`` requirements.txt
# (verified from source: the base already ships pillow==12.2.0, scipy==1.17.1, pandas,
# matplotlib, numpy, openpyxl==3.1.5, python-docx==1.1.2). Pinned EXACTLY — a baked
# template must be a reproducible build.
#   - reportlab 4.5.1: the most-mature-still-patched 4-line tip (May-2026). 5.0.0 is a
#     major bump released 2026-06-18 (deferred; a template-baked lib needs an owner
#     rebuild to hotfix, so stability beats newest). Bump to the latest patched 5.x
#     ONLY if the 4.x line goes EOL / unpatched (security > maturity).
#   - python-pptx 1.0.2: current stable.
BAKED_LIBS: list[str] = ["reportlab==4.5.1", "python-pptx==1.0.2"]

# The base template this extends (inherits its start cmd + config).
BASE_TEMPLATE: str = "code-interpreter-v1"

# The published alias. Overridable for a staging build; the deploy's
# PERSONA_SANDBOX_TEMPLATE must match whatever this publishes.
TEMPLATE_ALIAS: str = os.environ.get("PERSONA_SANDBOX_TEMPLATE_ALIAS", "persona-docgen-interpreter")

# = the base code-interpreter-v1 floors (P5-D-5: resource floors deferred — this build
# is floor-neutral; raising CPU/mem is a separable knob for a later spec).
CPU_COUNT: int = 2
MEMORY_MB: int = 2048


def main() -> None:
    """Build + publish the custom template under ``TEMPLATE_ALIAS``.

    Requires ``E2B_API_KEY`` in the environment (or ``e2b auth login``). Prints
    build logs; on success the alias is usable as ``Sandbox(template=alias)``.
    """
    template = Template().from_template(BASE_TEMPLATE).pip_install(BAKED_LIBS)
    Template.build(
        template,
        alias=TEMPLATE_ALIAS,
        cpu_count=CPU_COUNT,
        memory_mb=MEMORY_MB,
        on_build_logs=default_build_logger(),
    )
    print(  # noqa: T201 — owner-facing CLI script
        f"\n✓ Built + published E2B template alias '{TEMPLATE_ALIAS}' "
        f"(base={BASE_TEMPLATE}, +{BAKED_LIBS}).\n"
        f"  Set it in the deploy:  fly secrets set "
        f"PERSONA_SANDBOX_TEMPLATE={TEMPLATE_ALIAS} -a <app>\n"
        f"  Egress stays OFF at runtime (D-12-4); no runtime pip. "
        f"Run the R4 six-format offline pass to verify."
    )


if __name__ == "__main__":
    main()
