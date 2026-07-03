---
name: document_generation
description: Produce a downloadable document (docx, pdf, pptx, xlsx, md, txt) by writing code in the sandbox, routed by a format parameter.
when_to_use: >
  Use when the user wants a document FILE they can download — a Word doc, PDF,
  PowerPoint, Excel workbook, Markdown file, or plain-text file (not prose in
  the chat). Pass format=docx|pdf|pptx|xlsx|md|txt. For prose-then-format, draft
  the prose in your own context first, then activate this skill and embed the
  prose as a Python string. This skill also COVERS condensing/summarising source
  material into a brief — say so via content_spec (a plain string of the text is
  fine; an object with sections is optional). Skip for inline replies.
tools_required:
  - code_execution
metadata:
  parameters:
    type: object
    additionalProperties: false
    required: [format]
    properties:
      format:
        type: string
        enum: [docx, pdf, pptx, xlsx, md, txt]
        description: Output file format. Routes to the matching handler.
      template:
        type: string
        enum: [memo, report, business_letter, research_paper]
        description: Optional starting structure; its Markdown is staged for you to follow.
      domain:
        type: string
        description: Optional domain hint (e.g. legal, business, academic) for tone.
      content_spec:
        description: >
          The content to render. Pass EITHER a plain string of the document text
          (the simplest call — it is wrapped to {content: "<text>"} for you), OR
          an object with structured fields (title, sections, summary, ...). Both
          forms work; reach for the object only when you need named sections.
        oneOf:
          - type: string
          - type: object
  not_for:
    - Inline chat replies or single-paragraph answers — just write the text.
    - Reading or parsing an existing document — that is document ingestion, not generation.
    - A format outside the registered six — add a handler module, do not improvise.
  composes_with:
    - web_research
    - code_review
  output_format: A file written to /workspace/out/<name>.<ext>, surfaced to the conversation as an artifact.
  # D-24-5 per-skill loosening (2026-07-03): the P5 full-fidelity variant
  # resolves to ~2114 tokens; at 2000 the injector would TRUNCATE it the day
  # a custom sandbox template activates. 2200 covers it with headroom; the
  # degrade variant (~1953) was always under. The class default stays 2000.
  token_budget: 2200
---

# Document Generation

One skill, six formats. You pick the format via the `format` parameter; the
runtime routes to the right handler and stages that format's supplements into
the sandbox. You author the document by writing Python that runs through the
`code_execution` tool — the file lands in the workspace and is surfaced to the
user. New formats are added by the platform (a handler module), never by you
improvising an unsupported one.

## Shared conventions (every format)

- **The sandbox has NO internet — use ONLY pre-installed libraries.** Egress is
  disabled by design, so any runtime package install ALWAYS fails (it hangs
  until the setup timeout). NEVER shell out to a package manager, probe-then-
  install a module, or make any network call from your code. Always pre-installed
  and ready to `import`: `python-docx`, `openpyxl`, `matplotlib` (with
  `PIL`/Pillow), plus `pandas`/`numpy`. Two more — `reportlab` (rich PDF) and
  `python-pptx` (slides) — are present **only on the custom doc-gen sandbox
  template**: the `pdf` section below try-imports `reportlab` and falls back to
  matplotlib, so it works either way; the `pptx` section states whether slides
  are available. Everything else (`pdfkit`, `fpdf`, `weasyprint`) is unobtainable
  offline. Route each format to a pre-installed library; when none fits, **degrade
  honestly** (produce the content in a format that works and say so) — never
  attempt a doomed install.
- **Output path.** Write to `/workspace/out/<descriptive-name><ext>` from
  inside the sandbox — lowercase, hyphenated filename. The runtime pre-creates
  `/workspace/out` and surfaces the produced file. Same-session persistence
  only; do not promise cross-session re-open.
- **Visual style.** If `persona.identity.visual_style` is set, prefer those
  aesthetic hints (palette, font, register) over generic defaults.
- **Compose, don't round-trip.** For prose-then-format, the bridge is your own
  context — embed drafted prose as a Python **string**. Do NOT write a `.md`
  file and read it back.
- **Summarise in place.** When the user wants a condensed brief, do the
  condensing as you build `content_spec` (lead with the finding, cut to the
  essentials) — there is no separate summarise skill; this is the folded
  capability (D-24-7). `content_spec` accepts a plain **string** of the text
  (wrapped to `{content: "..."}` for you) or a structured object — pass the
  string unless you need named sections.
- **Depth on demand.** The section below is the must-do path. Read a supplement
  **from inside your generated code** only when the task needs the depth:
  `Path("/workspace/in/.skills/document_generation/supplements/<format>-<topic>.md").read_text()`.
- **Templates.** If a `template` is given, its Markdown is staged at
  `/workspace/in/.skills/document_generation/templates/<template>.md` — read it
  and follow its structure, filling `{{placeholders}}` from `content_spec`.

## Formats

### `docx` — Word (`python-docx`, pre-installed)

Named styles, not ad-hoc bold. Set `Normal` font + size before content; apply
`Heading 1/2/3` as named styles; page numbers in the footer; a TOC field shell
for ≥4 headings (tell the user to press F9). Supplements: `docx-tables`,
`docx-styles`, `docx-images`, `docx-toc`.

```python
from docx import Document
from docx.shared import Pt
doc = Document()
doc.styles["Normal"].font.name = "Calibri"; doc.styles["Normal"].font.size = Pt(11)
doc.add_heading("Title", level=0); doc.add_heading("Section", level=1)
doc.add_paragraph("Lead sentence.")
doc.save("/workspace/out/example.docx")
```

### `xlsx` — workbook (`openpyxl`, pre-installed)

Header row + typed cells; column widths; formulas as strings (`"=SUM(B2:B9)"`);
number formats for currency/percent. Supplements: `xlsx-formulas`,
`xlsx-formatting`, `xlsx-charts`.

```python
from openpyxl import Workbook
wb = Workbook(); ws = wb.active; ws.append(["Item", "Qty"]); ws.append(["A", 3])
ws["B4"] = "=SUM(B2:B3)"
wb.save("/workspace/out/sheet.xlsx")
```

### `pdf` — report (`reportlab` when present, else matplotlib `PdfPages`)

**Prefer `reportlab`** for real rich-text/tables (`SimpleDocTemplate` + platypus
flowables — the supplements below are reportlab). It is present on the custom
doc-gen template but NOT on the default one, so **detect it with a try-import and
fall back to matplotlib** — never install it. Both branches write a real `.pdf`;
reportlab is higher fidelity, matplotlib is the always-available floor. This is
the ONLY place doc-gen branches on a library, and it does so at import time (no
network, no config). Supplements: `pdf-flowables`, `pdf-pagination`, `pdf-images`.

```python
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate("/workspace/out/report.pdf", pagesize=A4)
    doc.build([
        Paragraph("Title", styles["Title"]),
        Spacer(1, 12),
        Paragraph("Lead paragraph of the report body.", styles["BodyText"]),
    ])  # rich text, wrapped tables, multi-page — see the pdf-flowables supplement
except ModuleNotFoundError:
    # Default template: no reportlab. matplotlib PdfPages is the offline floor —
    # one figure per page, fig.text for headings/body, real Axes for charts.
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from textwrap import wrap
    with PdfPages("/workspace/out/report.pdf") as pdf:
        fig = plt.figure(figsize=(8.27, 11.69))  # A4 portrait
        fig.text(0.08, 0.95, "Title", fontsize=18, weight="bold", va="top")
        y = 0.88
        for line in wrap("Lead paragraph of the report body.", 90):
            fig.text(0.08, y, line, fontsize=11, va="top"); y -= 0.025
        plt.axis("off"); pdf.savefig(fig); plt.close(fig)
```

### `pptx` — slides
<!--fidelity:full-->
`python-pptx` IS available on this deployment (the custom doc-gen template).
**Produce real `.pptx` slides** — a `Presentation()`, one slide per section using
the layout placeholders (title + body), charts via `python-pptx`'s chart API.
Supplements: `pptx-layouts`, `pptx-charts`, `pptx-theme`. Keep the try-import
guard below: if the import unexpectedly fails (a misconfigured template), fall
back to a slide-structured `docx` and say so — never attempt an install.

```python
try:
    from pptx import Presentation
    from pptx.util import Inches, Pt
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])  # title + content
    slide.shapes.title.text = "Title"
    slide.placeholders[1].text = "First bullet"
    prs.save("/workspace/out/deck.pptx")
except ModuleNotFoundError:
    # Misconfigured template (python-pptx absent): degrade to a slide-structured
    # docx (one heading per slide) and tell the user. Do NOT install anything.
    from docx import Document
    doc = Document()
    doc.add_heading("Title", level=1)
    doc.add_paragraph("First bullet")
    doc.save("/workspace/out/deck.docx")
```
<!--fidelity:else-->
`python-pptx` is NOT available on this deployment (the default template) and
cannot be installed offline. Do NOT attempt any install (it will hang and fail).
Instead tell the user up-front that native `.pptx` needs the custom doc-gen
sandbox template, and offer a working alternative now: a slide-structured `docx`
(one heading per slide) or a `md` outline. Pick whichever the user prefers and
produce that via the format above. Supplements: `pptx-layouts`, `pptx-charts`,
`pptx-theme` (read for slide structure you can mirror into the docx/md fallback).
<!--fidelity:end-->

### `md` — Markdown (stdlib)

Plain text with Markdown structure. No library — write the string to disk.

```python
from pathlib import Path
Path("/workspace/out/notes.md").write_text("# Title\n\nLead paragraph.\n")
```

### `txt` — plain text (stdlib)

Same, without Markdown syntax. Wrap to a sane width; no markup.

```python
from pathlib import Path
Path("/workspace/out/notes.txt").write_text("Title\n\nLead paragraph.\n")
```

## After the run

When `code_execution` returns successfully, tell the user: the filename you
wrote, anything to refresh on open (e.g. a Word TOC needs F9), and any
limitation you hit (e.g. PDF rendered via matplotlib; PPTX degraded to docx). A
partial PASS is honest; a silent PASS on a broken file is not.

## If `code_execution` raises

A Python traceback comes back as a tool error. Read it, fix the code, run again
— the loop's tool-error recovery handles the round-trip. Do not catch the error
inside your generated code. A `ModuleNotFoundError` for a missing library means
that library is NOT in the sandbox — switch to the pre-installed route above (or
degrade), never try to install it. Typical fixes live in the format's
supplements.
