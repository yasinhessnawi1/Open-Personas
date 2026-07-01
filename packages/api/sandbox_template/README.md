# Custom E2B sandbox template — build & publish runbook (Spec P5)

This is the **owner-run** half of P5. The agent-run half (the `PERSONA_SANDBOX_TEMPLATE`
config + `HostedSandbox` wiring + the `document_generation` fidelity + tests) is in the
codebase and covered by CI. **This step needs your E2B account**, so it is a runbook, not
an automated build.

**What it produces:** a published E2B template — the base `code-interpreter-v1` plus
**`reportlab` + `python-pptx`** baked in — so `code_execution` produces every document
format (pdf via reportlab, pptx via python-pptx, plus docx/xlsx/md/txt) **with internet
egress still disabled** (D-12-4).

> **Egress invariant (never weakened).** The `pip install` below runs at **build** time
> (PyPI reachable while building the image) and bakes the libs into the image. At
> **runtime** the sandbox still runs `allow_internet_access=False` and the libs import
> with **zero network** — that is the whole point of baking them in. Nothing here enables
> runtime egress or runtime `pip install`.

---

## Why only two libs

The base **`code-interpreter-v1`** template already ships `pillow`, `scipy`, `pandas`,
`matplotlib`, `numpy`, `openpyxl`, and `python-docx` (verified from its
`requirements.txt`). Only **`reportlab`** (rich PDF) and **`python-pptx`** (PPTX) are
missing — so those are the only two we add. Pinned exactly for a reproducible build:

| Lib | Pin | Note |
|---|---|---|
| `reportlab` | `4.5.1` | Most-mature-still-patched 4-line tip. `5.0.0` deferred (major, released 2026-06-18) — bump to latest patched 5.x only if 4.x goes EOL. |
| `python-pptx` | `1.0.2` | Current stable. |

Both bundle their offline assets (reportlab's standard-14 fonts, python-pptx's default
`.pptx` template), so generation is fully offline. Change the pins in
[`build_persona_docgen.py`](./build_persona_docgen.py) (`BAKED_LIBS`).

## Prerequisites

1. An **E2B account** with template-build access, and either `E2B_API_KEY` exported or
   `e2b auth login` completed.
2. The **`e2b` v2 SDK** (the Build System 2.0 `Template` builder). This is *build tooling*,
   separate from the API's runtime `e2b-code-interpreter`. Install it in an isolated env:
   ```bash
   pip install --upgrade e2b            # or: uv tool install e2b
   ```

## Build & publish

From the repo root:

```bash
export E2B_API_KEY=<your-key>
python packages/api/sandbox_template/build_persona_docgen.py
# or, without polluting your env:
#   uv run --with e2b python packages/api/sandbox_template/build_persona_docgen.py
```

This publishes the template under the alias **`persona-docgen-interpreter`** (override
with `PERSONA_SANDBOX_TEMPLATE_ALIAS=<alias>` for a staging build).

## Wire it into the deploy

Point the API at the published template (sandbox-scope env var, P5-D-3):

```bash
fly secrets set PERSONA_SANDBOX_TEMPLATE=persona-docgen-interpreter -a <your-app>
```

- **The value is the alias, not an immutable build id.** To ship a lib security patch,
  bump the pin in `build_persona_docgen.py`, re-run the build (same alias) — it updates
  the alias in place, so **the secret never changes**.
- **Unset ⇒ the SDK default `code-interpreter-v1`.** Community / OSS deploys that never
  set this keep working via the offline-degrade fallback (matplotlib PDF; slides offered
  as `.docx`/`.md`). Setting the secret is a **cloud-only** step.

## Verify — the six-format offline pass (R4, owner-run)

CI proves the config wiring + the offline-degrade fallback + both supplement variants, but
**cannot** prove real-template generation (no E2B account in CI). After publishing + wiring,
run the R4 acceptance leg: with the custom template active and **egress off**, generate all
six formats and confirm each file opens —

`docx · xlsx · md · txt · pdf (reportlab) · pptx (python-pptx)`

— e.g. by asking a persona (with `code_execution` + the `document_generation` skill) for one
document per format, and confirming: (a) the `.pdf` is reportlab-rendered (rich text/tables,
not a matplotlib page), (b) the `.pptx` is produced (not degraded to docx/md), (c) no network
call is attempted (egress stays off). Record the result in `spec_R4`.
