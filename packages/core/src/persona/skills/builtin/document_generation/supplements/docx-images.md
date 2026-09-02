# Images in python-docx 1.1.2

Read this when you need to embed an image (a logo, a screenshot, a
chart produced by Spec 17) into the document body.

## Supported formats

`docx.document.Document.add_picture()` accepts **PNG, JPEG, GIF, BMP,
TIFF** (verified against python-docx 1.1.2's `docx/image` package).

- **SVG is NOT supported** in 1.1.2. SVG-via-`add_picture` PRs #1343 and #1386 are unmerged as of June 2026. If a chart pipeline only has SVG, rasterise to PNG first (matplotlib: `plt.savefig(path, format="png", dpi=150)`).
- **WebP / HEIC** are not supported either. Re-save as PNG.

## The basic embed

```python
from docx.shared import Inches

doc.add_picture("/workspace/in/diagram.png", width=Inches(4.0))
```

`width=` is **not optional in practice**. Without it, python-docx uses
the image's intrinsic pixel size at 96 dpi, which for a typical 1200-px
chart is ~12.5 inches, past the page margin, clipped on the right.

The quality bar (`100_000 <= doc.inline_shapes[0].width.emu <=
5_000_000`, that is roughly 0.1 to 5.5 inches in EMUs) passes when you
size between 1 and 6 inches. The sweet spot for a body-width chart is
`Inches(4)` to `Inches(5.5)`.

## Captioning an image

The `Caption` named style ships with the default template. Add a caption
paragraph immediately after the picture:

```python
caption = doc.add_paragraph("Figure 1: Notice-period rules pre- vs post-2024.")
caption.style = doc.styles["Caption"]
caption.alignment = 1    # CENTER
```

## Aligning the image

`add_picture` produces an **inline** shape (sits in the run of a new
paragraph). To centre it:

```python
para = doc.paragraphs[-1]   # the paragraph add_picture just appended
para.alignment = 1          # WD_ALIGN_PARAGRAPH.CENTER
```

Floating images (text wraps around) are a manual XML manipulation,
out of scope here; for v0.1 prefer inline centred.

## Embedding a Spec 17 chart (D-16-5 contract)

Spec 17 produces charts at `/workspace/out/charts/<id>.png`
(sandbox-internal view). The D-16-5 contract says these are PNG at the
pinned `matplotlib==3.9.2`.

**Same-session reachability only.** Within a single sandbox session,
files written to `/workspace/out/charts/<id>.png` in an earlier
`code_execution` call are reachable on later calls in the same session.
Across sessions, the file is not persisted to the persona workspace in
v0.1 (no host-side bridge yet). Embed in the same session that produced
the chart, or in the same `code_execution` call that produces both.

```python
from pathlib import Path
from docx.shared import Inches

chart_path = "/workspace/out/charts/notice-period-by-tenure.png"
assert Path(chart_path).exists(), f"chart not found: {chart_path}"

doc.add_picture(chart_path, width=Inches(5.5))
caption = doc.add_paragraph("Figure 2: Notice period (months) by tenure year.")
caption.style = doc.styles["Caption"]
```

If the chart was produced in a previous turn / call (cross-session), the
honest path in v0.1 is to **regenerate the chart in the same call**:
write the matplotlib code that produces the PNG immediately before the
python-docx code that embeds it. This is a pragmatic v0.1 constraint
called out in the spec; v0.2 will add a host-side produced-files bridge.

## Sizing rules of thumb

| Image | Width |
|---|---|
| Body figure / chart | `Inches(4)` to `Inches(5.5)` |
| Logo at top of page | `Inches(1)` to `Inches(1.5)` |
| Full-width screenshot | `Inches(6)` (just under default 6.5-inch text box) |
| Inline icon | `Inches(0.25)` to `Inches(0.5)` |

For an A4 page with 25 mm margins, the usable width is ~160 mm ≈ 6.3
inches. Stay at `Inches(5.5)` or less to leave breathing room.

## Resolution

PNG embed is **lossless and sized by Word at render time**. Embedding a
2400×1800-pixel PNG at `width=Inches(5.5)` produces a 5.5-inch print at
~436 dpi (excellent). Embedding the same PNG at `width=Inches(2)`
produces a 2-inch print at ~1200 dpi (wasteful but fine).

For matplotlib charts, `plt.savefig(path, dpi=150)` is sharp at
`width=Inches(5.5)` body size. `dpi=100` is acceptable for screen-only
review.

## Common pitfalls

- **No `width=`**: image overflows page margin (most common failure).
- **JPEG photo on white background**: embed as PNG; JPEG compresses photographs but introduces blocky artefacts around chart text and axes.
- **`add_picture(io.BytesIO(...))`**: works, but only if you have already written the bytes; safer for v0.1 to write the PNG to `/workspace/out/charts/<id>.png` first and pass the path.
- **`UnrecognizedImageError`**: the file's header is not one of PNG / JPEG / GIF / BMP / TIFF (even if the extension says so). Re-save with `PIL.Image.open(src).save(dst, "PNG")` first.
- **Embedding the same image twice**: fine; python-docx writes the bytes twice in the package. For a logo used on every page, render once via the section header (out of scope here) rather than re-embed per page.
