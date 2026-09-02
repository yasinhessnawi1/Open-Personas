# PDF Generation: Images and embedded charts (detailed reference)

Verbose reference for embedding PNG / JPEG images in reportlab PDFs,
including the Spec 17 chart-embedding contract. Read from inside your
code:

```python
from pathlib import Path
detail = Path(
    "/workspace/in/.skills/pdf_generation/supplements/images.md"
).read_text()
```

## Supported formats

`reportlab.platypus.Image` accepts the formats Python Imaging Library
(PIL / Pillow) decodes: primarily PNG, JPEG, GIF, BMP, TIFF. **SVG is
not supported** by the `Image` flowable at reportlab 4.2.5; it would
require `svglib`, which is not in the Spec 12 sandbox image manifest
(see Spec 16 D-16-5-rejection-SVG for the rationale).

**Use PNG for everything.** It is lossless, supports transparency,
embeds cleanly, and is what Spec 17 produces by default.

## The Spec 17 chart contract (D-16-5)

When Spec 17 produces a chart **in the same `code_execution` session**,
it writes it to:

```
/workspace/out/charts/<id>.png
```

`<id>` is a caller-chosen UUID or descriptive slug picked by Spec 17.
Spec 16 documents read the file via `Image(path, width=…, height=…)`:

```python
from reportlab.platypus import Image
from reportlab.lib.units import cm

chart = Image("/workspace/out/charts/complaints-by-district.png",
              width=14 * cm, height=8 * cm)
chart.hAlign = "CENTER"
story.append(chart)
```

**Same-session only.** The sandbox cleans `/workspace/out` between
`code_execution` calls (per Spec 16 state.md A2 finding). A chart Spec 17
produced on a previous turn is **not** at that path on this turn. The
SKILL.md body's failure-modes list reiterates this.

If the orchestrator enables sandbox session mode (D-12-1 scaled scope),
filesystem state persists across `docker exec` calls *within* one
session, so the chart from call N is still there at call N+1 in the same
session. Across sessions, nothing persists in v0.1.

## Sizing

**Always pass `width` and `height` in cm or inch units.** Raw
`Image(path)` without sizing interprets the PNG's pixel dimensions as
points (one point = 1/72 inch), so a 1200×800 chart becomes a 16 × 11
inch flowable, which is off-page on A4.

```python
from reportlab.lib.units import cm, inch

Image(path, width=14 * cm, height=8 * cm)   # metric
Image(path, width=5 * inch, height=3 * inch)  # imperial
```

### Maintaining aspect ratio

If you only know the target width and want to preserve the source's
aspect ratio, read the source dimensions via PIL:

```python
from PIL import Image as PILImage
from reportlab.lib.units import cm
from reportlab.platypus import Image

with PILImage.open(chart_path) as im:
    src_w, src_h = im.size  # pixels

target_w_cm = 14
target_h_cm = target_w_cm * (src_h / src_w)
chart = Image(chart_path,
              width=target_w_cm * cm,
              height=target_h_cm * cm)
```

PIL is pre-installed in the Spec 12 sandbox image (it's a transitive of
both reportlab and matplotlib).

### Fitting an image to the content frame

The A4 content frame with 2 cm margins is 17 × 25.7 cm. A chart wider
than the frame is a layout failure (the right edge clips). Compute the
target size against the frame width:

```python
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm

frame_w_cm = (A4[0] - 4 * cm) / cm   # ≈ 17 cm

with PILImage.open(chart_path) as im:
    src_w, src_h = im.size

# Use full frame width but cap height at 60% of frame height
target_w_cm = min(frame_w_cm, 14)
target_h_cm = target_w_cm * (src_h / src_w)
max_h_cm = (A4[1] - 4 * cm) / cm * 0.6  # ≈ 15 cm
if target_h_cm > max_h_cm:
    target_h_cm = max_h_cm
    target_w_cm = target_h_cm * (src_w / src_h)
```

This is the safe sizing pattern when you don't know the chart's
dimensions ahead of time.

## DPI guidance

For embedded charts (matplotlib output), prefer `dpi=150` or higher when
the chart is rendered. The default `plt.savefig(path)` is 100 DPI, which
prints fine but looks soft on high-resolution screens.

```python
# In the chart-producing code (or in your own pre-pass if you're
# generating both chart and PDF in one code_execution call):
import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(7, 4))  # inches
ax.plot(x, y)
fig.savefig("/workspace/out/charts/complaints.png",
            dpi=150, bbox_inches="tight")
plt.close(fig)
```

`bbox_inches="tight"` trims surrounding whitespace, which keeps the
embedded image visually balanced with the surrounding text.

## Alignment

`Image` is left-aligned by default. To centre or right-align:

```python
img = Image(path, width=14 * cm, height=8 * cm)
img.hAlign = "CENTER"   # or "LEFT" / "RIGHT"
```

`vAlign` exists but is rarely useful at the flowable level; vertical
position is determined by the flow.

## Image with a caption

Wrap image + caption in a `KeepTogether` so they don't split across
pages:

```python
from reportlab.platypus import KeepTogether, Image, Paragraph, Spacer

caption_style = ParagraphStyle(
    "Caption", parent=body, fontSize=9, alignment=1, textColor=colors.grey,
)

img = Image(chart_path, width=14 * cm, height=8 * cm)
img.hAlign = "CENTER"
caption = Paragraph(
    "<i>Figure 1.</i> Complaints by district, Q1 2026.", caption_style,
)
story.append(KeepTogether([img, Spacer(1, 0.2 * cm), caption]))
```

## Images inside table cells

`Image` flowables can be cells in a `Table` or `LongTable`:

```python
data = [
    ["District", "Trend chart"],
    ["Sentrum", Image(sentrum_chart, width=4 * cm, height=2 * cm)],
    ["Grünerløkka", Image(grun_chart, width=4 * cm, height=2 * cm)],
]
```

Use small target sizes; table cells get cramped quickly. Match the row
height implied by the image to the column widths you set.

## Backgrounds and watermarks

For a watermark image on every page, draw it in the page callback (not
as a flowable):

```python
def _watermark(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFillAlpha(0.05)
    canvas.drawImage("/workspace/in/.skills/pdf_generation/assets/wm.png",
                     x=A4[0] / 2 - 8 * cm, y=A4[1] / 2 - 8 * cm,
                     width=16 * cm, height=16 * cm,
                     preserveAspectRatio=True, mask="auto")
    canvas.restoreState()
```

`canvas.drawImage(...)` (not the `Image` flowable) is the low-level
call. `mask="auto"` honours transparency in the source PNG.

## Common mistakes

1. **`Image(path)` without size.** Renders pixel-as-point. Always pass
   `width=` and `height=` in cm or inch.

2. **Aspect-ratio drift.** Setting both `width` and `height` without
   reading the source dimensions distorts the image. Use PIL to read
   the source size and compute one dimension from the other.

3. **Embedding a chart from a different `code_execution` call.** The
   `/workspace/out/` directory is cleaned between executions. The chart
   does not survive, so produce the chart in the same call as the PDF, or
   use session mode if the orchestrator enables it.

4. **JPEG with transparency expected.** JPEG does not support an alpha
   channel; transparent regions render as solid white. Use PNG for any
   image with transparency.

5. **A chart that exceeds the page frame.** Width 18 cm on an A4 page
   with 2 cm margins clips the right edge. Cap at frame width (≈ 17
   cm).

6. **Drawing into the canvas after `build()` returns.** The canvas is
   closed at the end of `build()`. Any drawing must happen in a flowable
   or in the page callback during the build, not after.

7. **Forgetting `plt.close(fig)`** when producing many charts in a loop.
   Matplotlib keeps figures alive in memory until closed; long loops
   leak memory.
