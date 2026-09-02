# Custom E2B sandbox template: build and publish runbook

This is the **owner-run** half of the custom sandbox template. The agent-run half
(the `PERSONA_SANDBOX_TEMPLATE` config, the `HostedSandbox` wiring, the
`document_generation` fidelity, and the tests) is already in the codebase and
covered by CI. This step needs **your E2B account**, so it is a runbook, not an
automated build.

**What it produces:** a published E2B template, the base `code-interpreter-v1`
plus **`reportlab` and `python-pptx`** baked in, so `code_execution` produces every
document format (pdf via reportlab, pptx via python-pptx, plus docx, xlsx, md,
txt) **with internet egress still disabled**.

> **Egress invariant, never weakened.** The `pip install` below runs at **build**
> time, when PyPI is reachable, and bakes the libs into the image. At **runtime**
> the sandbox still runs `allow_internet_access=False`, and the libs import with
> **zero network**. That is the whole point of baking them in. Nothing here
> enables runtime egress or runtime `pip install`.

---

## Why only two libs

The base **`code-interpreter-v1`** template already ships `pillow`, `scipy`,
`pandas`, `matplotlib`, `numpy`, `openpyxl`, and `python-docx` (verified from its
`requirements.txt`). Only **`reportlab`** (rich PDF) and **`python-pptx`** (PPTX)
are missing, so those are the only two we add. Both are pinned exactly for a
reproducible build:

| Lib | Pin | Note |
|---|---|---|
| `reportlab` | `4.5.1` | The most mature still patched 4-line tip. `5.0.0` is deferred (a major, released 2026-06-18). Bump to the latest patched 5.x only if 4.x goes EOL. |
| `python-pptx` | `1.0.2` | Current stable. |

Both bundle their offline assets (reportlab's standard-14 fonts, python-pptx's
default `.pptx` template), so generation is fully offline. Change the pins in
[`build_persona_docgen.py`](./build_persona_docgen.py) (`BAKED_LIBS`).

## Prerequisites

1. An **E2B account** with template build access, and either `E2B_API_KEY`
   exported or `e2b auth login` completed.
2. The **`e2b` v2 SDK**, the Build System 2.0 `Template` builder. This is *build
   tooling*, separate from the API's runtime `e2b-code-interpreter`. Install it in
   an isolated env:
   ```bash
   pip install --upgrade e2b            # or: uv tool install e2b
   ```

## Build and publish

From the repo root:

```bash
export E2B_API_KEY=<your-key>
python packages/api/sandbox_template/build_persona_docgen.py
# or, without polluting your env:
#   uv run --with e2b python packages/api/sandbox_template/build_persona_docgen.py
```

This publishes the template under the alias **`persona-docgen-interpreter`**.
Override it with `PERSONA_SANDBOX_TEMPLATE_ALIAS=<alias>` for a staging build.

## Wire it into the deploy

Point the API at the published template (a sandbox scope env var):

```bash
fly secrets set PERSONA_SANDBOX_TEMPLATE=persona-docgen-interpreter -a <your-app>
```

- **The value is the alias, not an immutable build id.** To ship a lib security
  patch, bump the pin in `build_persona_docgen.py` and re-run the build against
  the same alias. It updates the alias in place, so **the secret never changes**.
- **Unset means the SDK default `code-interpreter-v1`.** Community and OSS deploys
  that never set this keep working through the offline degrade fallback
  (matplotlib PDF; slides offered as `.docx` or `.md`). Setting the secret is a
  **cloud only** step.

## Verify: the six format offline pass (owner-run)

CI proves the config wiring, the offline degrade fallback, and both supplement
variants, but it **cannot** prove real template generation, because there is no
E2B account in CI. After publishing and wiring, run the acceptance leg: with the
custom template active and **egress off**, generate all six formats and confirm
each file opens.

`docx · xlsx · md · txt · pdf (reportlab) · pptx (python-pptx)`

The simplest way is to ask a persona (with `code_execution` and the
`document_generation` skill) for one document per format, then confirm: (a) the
`.pdf` is reportlab rendered, meaning rich text and tables, not a matplotlib
page; (b) the `.pptx` is produced, not degraded to docx or md; (c) no network call
is attempted, so egress stays off. Record the result in `spec_R4`.
