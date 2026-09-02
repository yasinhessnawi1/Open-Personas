# PDF Generation: Flowables (detailed reference)

Verbose reference for `reportlab.platypus` flowables. Read this from
inside your code when the SKILL.md body's overview isn't enough.

```python
from pathlib import Path
detail = Path(
    "/workspace/in/.skills/pdf_generation/supplements/flowables.md"
).read_text()
```

## What a flowable is

A "flowable" is any object reportlab can lay out in a frame: it knows its
own dimensions and how to render itself onto a canvas. You build a list
(`story`) of flowables, and `SimpleDocTemplate.build(story)` flows them
through frames page by page. When a flowable doesn't fit on the current
page, the doc starts a new page.

The flowables you will use most:

| Flowable | Purpose | Where it lives |
|---|---|---|
| `Paragraph` | Wrapped text in a paragraph style | `reportlab.platypus.Paragraph` |
| `Spacer` | Vertical whitespace | `reportlab.platypus.Spacer` |
| `Table` | Fixed-size table (single page only) | `reportlab.platypus.Table` |
| `LongTable` | Multi-page table (splits across pages) | `reportlab.platypus.LongTable` |
| `Image` | Embedded raster image (PNG / JPEG) | `reportlab.platypus.Image` |
| `PageBreak` | Force a new page | `reportlab.platypus.PageBreak` |
| `KeepTogether` | Refuse to split a group across pages | `reportlab.platypus.KeepTogether` |
| `KeepInFrame` | Shrink content to fit a frame | `reportlab.platypus.KeepInFrame` |
| `HRFlowable` | Horizontal rule | `reportlab.platypus.HRFlowable` |

## `Paragraph`: the workhorse

`Paragraph(text, style)` accepts a subset of HTML-like inline markup:
`<b>`, `<i>`, `<u>`, `<font name=… size=… color=…>`, `<br/>`,
`<a href="…">`, `<sup>`, `<sub>`. Anything outside that set is treated
as literal text. To use literal `<` / `>` / `&`, escape them as `&lt;`
`&gt;` `&amp;`.

```python
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle

body = ParagraphStyle(
    "Body", fontName="Helvetica", fontSize=10.5, leading=14,
    spaceAfter=6, alignment=0,  # 0=LEFT, 1=CENTER, 2=RIGHT, 4=JUSTIFY
)

para = Paragraph(
    "Complaints rose <b>12%</b> quarter-on-quarter, driven by the "
    "<i>Sentrum</i> district (n=314). See <a href='#sec-3'>§3</a>.",
    body,
)
```

### Paragraph style fields you'll set most

- `fontName`: `"Helvetica"`, `"Times-Roman"`, `"Courier"` ship by default.
- `fontSize`: points. Body ≥ 10. Headings 12-18 typically.
- `leading`: line height in points. Rule of thumb: `1.3 * fontSize`.
- `spaceBefore` / `spaceAfter`: vertical spacing around the paragraph.
- `leftIndent` / `rightIndent` / `firstLineIndent`: indentation in pts.
- `alignment`: 0 / 1 / 2 / 4 (LEFT/CENTER/RIGHT/JUSTIFY).
- `textColor`: `colors.black`, `colors.HexColor("#…")`, etc.

## `Spacer`: vertical whitespace

```python
from reportlab.platypus import Spacer
from reportlab.lib.units import cm
story.append(Spacer(1, 0.5 * cm))   # width arg is ignored; height matters
```

Prefer `Spacer` over `<br/>` chains inside a Paragraph for layout
spacing. Use `spaceBefore`/`spaceAfter` on the style for *consistent*
spacing around every paragraph.

## `Table` and `LongTable`

`Table` is single-page only: if it doesn't fit on the current page,
reportlab tries to push it to the next page; if it still doesn't fit, it
raises `LayoutError`. Use `LongTable` whenever the row count might
exceed one page's worth.

### Building data

`data` is a list of rows; each row is a list of cells. Cells may be:

- strings (plain or with inline markup),
- `Paragraph` objects (for wrapped multi-line cells),
- numbers,
- `Image` objects (for embedded thumbnails in cells).

```python
from reportlab.platypus import Table, LongTable, TableStyle
from reportlab.lib import colors
from reportlab.lib.units import cm

# 25+ rows
data = [["District", "Complaints", "Resolved", "Pending"]]
for d, c, r in district_rows:
    data.append([d, c, r, c - r])

# colWidths in points or units; sum ≤ frame width (A4 - margins ≈ 17 cm).
table = LongTable(data, repeatRows=1,
                  colWidths=[5 * cm, 3 * cm, 3 * cm, 3 * cm])
```

### `repeatRows`

`repeatRows=1` repeats the first row (the header) on every continuation
page. `repeatRows=2` repeats the first two. Without this, only the first
page shows the header, and the rest of the table looks unlabelled.

### `TableStyle`: the styling DSL

`TableStyle` takes a list of styling tuples. Each tuple is
`(command, start_cell, end_cell, *args)`. Cells are `(col, row)`,
0-indexed; `-1` means last column / row.

```python
table.setStyle(TableStyle([
    # header row background + text colour
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2E4053")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("ALIGN", (1, 0), (-1, -1), "RIGHT"),  # numeric columns right-align
    # body
    ("FONTSIZE", (0, 0), (-1, -1), 10),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
    # alternating row colour
    ("ROWBACKGROUNDS", (0, 1), (-1, -1),
     [colors.white, colors.HexColor("#F4F6F7")]),
    # grid lines
    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
    # span: merge cells (col0, row3) through (col2, row3)
    # ("SPAN", (0, 3), (2, 3)),
]))
```

The available commands cover: `BACKGROUND`, `TEXTCOLOR`, `FONTNAME`,
`FONTSIZE`, `LEADING`, `ALIGN` (LEFT/CENTER/RIGHT/DECIMAL), `VALIGN`
(TOP/MIDDLE/BOTTOM), `BOTTOMPADDING` / `TOPPADDING` / `LEFTPADDING` /
`RIGHTPADDING`, `LINEABOVE` / `LINEBELOW` / `LINEBEFORE` / `LINEAFTER`,
`BOX` / `OUTLINE` / `INNERGRID` / `GRID`, `SPAN`, `ROWBACKGROUNDS`.

### Column widths

`colWidths` is mandatory for `LongTable` (auto-sizing produces
unpredictable splits) and strongly recommended for `Table`. Values are
points by default; multiply by `cm` or `inch` from `reportlab.lib.units`
for readable code.

`rowHeights` is rarely needed; let the table size rows from content.

## `Image`

```python
from reportlab.platypus import Image
from reportlab.lib.units import cm

img = Image("/workspace/out/charts/complaints.png",
            width=14 * cm, height=8 * cm)
img.hAlign = "CENTER"
story.append(img)
```

**Always pass `width` and `height` in cm or inch units.** Raw `Image(path)`
without sizing renders pixels-as-points, so a 1200×800 chart becomes
16 × 11 inches, off-page. Maintain the original aspect ratio: divide
target width by source pixel-width, multiply source pixel-height by the
same factor.

If the image's intrinsic dimensions are unknown:

```python
from PIL import Image as PILImage
with PILImage.open(path) as im:
    w, h = im.size
target_w = 14 * cm
target_h = target_w * (h / w)
img = Image(path, width=target_w, height=target_h)
```

See `images.md` for the Spec 17 chart contract and DPI guidance.

## `PageBreak`

```python
from reportlab.platypus import PageBreak
story.append(PageBreak())
```

Forces the next flowable onto a new page. Use between cover and body,
and between major sections of a long report.

`CondPageBreak(height)` is conditional: it forces a break only if less
than `height` remains on the current page. Useful before a section
header you don't want stranded at the bottom of a page.

## `KeepTogether` and `KeepInFrame`

`KeepTogether(flowables)` refuses to split the group across pages. If it
doesn't fit on the current page, it forces a `PageBreak` first.

```python
from reportlab.platypus import KeepTogether
section_header = Paragraph("3. Recommendations", h2)
section_first_para = Paragraph("We recommend that…", body)
story.append(KeepTogether([section_header, section_first_para]))
```

Use it to glue a header to its first paragraph.

`KeepInFrame(maxWidth, maxHeight, flowables, mode="shrink")` shrinks (or
overflows / truncates) content to fit a fixed frame. Use it for boxed
side notes or fixed-height cover pages. Modes: `"shrink"`, `"overflow"`,
`"truncate"`, `"error"`.

## `HRFlowable`

```python
from reportlab.platypus import HRFlowable
story.append(HRFlowable(width="100%", thickness=0.5,
                        color=colors.grey, spaceBefore=6, spaceAfter=6))
```

Horizontal rule between sections. Subtler than a heavy `Spacer`.

## Common mistakes

1. **Mutating a `ParagraphStyle` after use.** Build styles once at the
   top, reuse. If you need a variant, derive: `var = ParagraphStyle(
   "Var", parent=body, fontSize=12)`.

2. **Reusing a `Paragraph` instance.** A flowable carries layout state
   after being drawn, so appending the same instance twice produces
   undefined output. Build a fresh `Paragraph` for each occurrence.

3. **Empty `data` rows in `Table`.** A row that is `[]` (rather than
   `["", "", "", ""]`) raises `IndexError` during layout. Pad to the
   declared column count.

4. **`Paragraph` with raw `<` / `>`.** Treated as malformed markup.
   Escape as `&lt;` / `&gt;` / `&amp;`.

5. **`Image` with PIL not installed.** `reportlab.platypus.Image` calls
   PIL internally for non-trivial formats. PIL ships pre-installed in
   the Spec 12 sandbox image; in stripped environments, install it.
