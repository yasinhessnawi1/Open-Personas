# PDF Generation: Pagination (detailed reference)

Verbose reference for page breaks, page numbers, headers, footers, and
multi-page table layout in `reportlab`. Read from inside your code:

```python
from pathlib import Path
detail = Path(
    "/workspace/in/.skills/pdf_generation/supplements/pagination.md"
).read_text()
```

## How reportlab paginates

`SimpleDocTemplate.build(story)` flows your flowables through *frames*.
A frame is a rectangular region on a page; `SimpleDocTemplate` provides
one frame per page, sized by the margins you set. When a flowable
doesn't fit in the current frame, the doc emits a page break, draws the
next page (calling the page-level callback), and resumes flowing.

Two callbacks let you draw onto each page outside the main frame:

- `onFirstPage(canvas, doc)`: called once, before any flowable on page 1.
- `onLaterPages(canvas, doc)`: called for every page **after** the first.

`canvas` is a low-level drawing surface (`reportlab.pdfgen.canvas.Canvas`);
`doc` is the `SimpleDocTemplate` instance and exposes `doc.page` (the
current 1-indexed page number) and `doc.pageTemplate.frames[0]` (the
content frame, if you need its bounds).

## Page numbers in the footer

The standard pattern:

```python
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm

def _footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 9)
    canvas.setFillGray(0.4)
    canvas.drawRightString(
        A4[0] - 2 * cm,   # right edge minus right margin
        1.2 * cm,         # vertical offset from bottom edge
        f"Page {doc.page}",
    )
    canvas.restoreState()

doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
```

Always wrap `canvas` mutations in `saveState()` / `restoreState()`; the
canvas is shared with the flowable rendering; leaving state mutated
(font, colour, transform) bleeds into the next page's content.

### Skipping the page number on the cover

Use a different callback for `onFirstPage`:

```python
def _cover_page(canvas, doc) -> None:
    pass  # no footer on cover

def _body_page(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 9)
    # doc.page is the actual page number; subtract 1 if you want
    # body-relative numbering (i.e., body page 1 is overall page 2).
    canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"Page {doc.page - 1}")
    canvas.restoreState()

doc.build(story, onFirstPage=_cover_page, onLaterPages=_body_page)
```

### "Page X of Y": the two-pass trick

`doc.page` knows the current page; it does **not** know the total. To
print "Page X of Y," you need two passes: build once to learn the total,
then build again with the total embedded.

```python
class _PageCountCanvas:
    def __init__(self) -> None:
        self.total = 0

count = _PageCountCanvas()

def _count_pages(canvas, doc) -> None:
    count.total = max(count.total, doc.page)

# Pass 1: count
doc.build(list(story), onFirstPage=_count_pages, onLaterPages=_count_pages)

# Pass 2: render with total
def _footer_xof(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 9)
    canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm,
                           f"Page {doc.page} of {count.total}")
    canvas.restoreState()

doc = SimpleDocTemplate(...)  # rebuild, the previous doc is consumed
doc.build(story, onFirstPage=_footer_xof, onLaterPages=_footer_xof)
```

Reportlab also ships a built-in `BaseDocTemplate` + `PageTemplate`
pattern that handles this in one pass; for most v0.1 reports the
two-pass trick is simpler and the cost (a brief in-memory rebuild) is
negligible.

## Headers: running document title

Same pattern, top of page:

```python
def _header(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica-Oblique", 9)
    canvas.setFillGray(0.3)
    canvas.drawString(2 * cm, A4[1] - 1.2 * cm,
                      "Tenant protection: quarterly summary")
    # Thin rule under the header
    canvas.setStrokeGray(0.7)
    canvas.setLineWidth(0.4)
    canvas.line(2 * cm, A4[1] - 1.35 * cm,
                A4[0] - 2 * cm, A4[1] - 1.35 * cm)
    canvas.restoreState()
```

If header text varies per section, you need either a `BaseDocTemplate`
with multiple `PageTemplate`s, or you stash the current section title in
a module-level variable the callback reads. The first is cleaner; the
second is shorter.

## `PageBreak` and `CondPageBreak`

- `PageBreak()`: unconditional. Next flowable starts on a new page.
- `CondPageBreak(height)`: conditional. Inserts a break **only** if
  less than `height` remains on the current page.

```python
from reportlab.platypus import PageBreak, CondPageBreak
from reportlab.lib.units import cm

story.append(PageBreak())            # cover → body
story.append(CondPageBreak(6 * cm))  # avoid stranding a header at page bottom
story.append(Paragraph("4. Conclusions", h2))
```

`CondPageBreak` before a section header prevents the "header at the
bottom of the page, body on the next page" failure mode.

## Multi-page tables

`LongTable` (covered in `flowables.md`) handles the splitting. Two
related pagination concerns:

### `repeatRows=N`

Repeats the first `N` rows at the top of every continuation page.
Almost always you want `repeatRows=1` (the header). For two-row stacked
headers, use `repeatRows=2`.

### `splitByRow=True` (default)

`LongTable` splits between rows, not within a row. If a single row's
content is taller than the remaining frame, the row is moved entire to
the next page. If a single row is taller than a full page (e.g., a cell
contains a 50-line `Paragraph`), reportlab raises `LayoutError`;
either shrink the cell content or wrap the cell `Paragraph` in
`KeepInFrame`.

### Avoiding orphan rows

A natural-looking page-spanning table leaves at least 2-3 body rows on
the last page. There is no built-in "minimum rows after split" option;
the workaround is to pre-compute approximate row counts and insert a
`CondPageBreak` before the table if the remaining height is small.

## Margins, frames, and page templates

`SimpleDocTemplate(...)` accepts `leftMargin`, `rightMargin`, `topMargin`,
`bottomMargin` in points (use `* cm` or `* inch`). The content frame is
the page rectangle minus those margins.

For different margins per section, you need `BaseDocTemplate` with
multiple `PageTemplate`s. A common case is a landscape-orientation
chapter inside a portrait report:

```python
from reportlab.platypus import BaseDocTemplate, PageTemplate, Frame, NextPageTemplate
from reportlab.lib.pagesizes import A4, landscape

doc = BaseDocTemplate(path, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
                      topMargin=2 * cm, bottomMargin=2 * cm)

portrait_frame = Frame(2 * cm, 2 * cm, A4[0] - 4 * cm, A4[1] - 4 * cm,
                       id="portrait")
landscape_frame = Frame(2 * cm, 2 * cm,
                        landscape(A4)[0] - 4 * cm, landscape(A4)[1] - 4 * cm,
                        id="landscape")

doc.addPageTemplates([
    PageTemplate(id="portrait", frames=[portrait_frame], pagesize=A4,
                 onPage=_footer),
    PageTemplate(id="landscape", frames=[landscape_frame],
                 pagesize=landscape(A4), onPage=_footer),
])

# Switch templates mid-story:
story.append(NextPageTemplate("landscape"))
story.append(PageBreak())
story.append(wide_table)
story.append(NextPageTemplate("portrait"))
story.append(PageBreak())
```

## Two-column layouts

Two-column body text needs `BaseDocTemplate` with two frames on one
`PageTemplate`:

```python
from reportlab.platypus import BaseDocTemplate, PageTemplate, Frame
gap = 0.5 * cm
col_w = (A4[0] - 4 * cm - gap) / 2

left = Frame(2 * cm, 2 * cm, col_w, A4[1] - 4 * cm, id="left")
right = Frame(2 * cm + col_w + gap, 2 * cm, col_w, A4[1] - 4 * cm,
              id="right")

doc.addPageTemplates([PageTemplate(id="2col", frames=[left, right])])
```

Flowables flow left-to-right, top-to-bottom within each frame, then
right-frame top, then next page.

## Bookmarks and outline (PDF navigation pane)

```python
canvas.bookmarkPage("section-3")
canvas.addOutlineEntry("3. Recommendations", "section-3", level=0)
```

Call these from a page callback when a section heading is rendered, or
override `Paragraph` to call them as a side effect. Most v0.1 reports
skip this; add only if the user explicitly asks for navigation.

## Common mistakes

1. **Forgetting `saveState`/`restoreState`** in the page callback, and the
   font / colour you set bleeds into the flowable rendering on the
   following page.

2. **Mutating `story` between `build()` calls** in the two-pass trick:
   pass a `list(story)` copy to pass 1 so flowable state doesn't
   contaminate pass 2.

3. **Drawing into the content frame** from a page callback: the callback
   runs **before** flowable rendering on that page; whatever you draw is
   drawn under the flowables. Use it for headers / footers in the margin
   region, not for in-frame content.

4. **`CondPageBreak` with a `height` larger than the frame**: never
   triggers; reportlab silently treats it as a no-op.

5. **A flowable taller than the page**: `LayoutError`. The fix is one of:
   shrink the flowable (smaller font, smaller image), split it
   (multiple paragraphs instead of one), or wrap in `KeepInFrame`.
