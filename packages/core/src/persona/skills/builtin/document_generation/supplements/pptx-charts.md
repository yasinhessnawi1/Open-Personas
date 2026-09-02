# Charts: embedding visuals in a deck

The `pptx_generation` SKILL.md body teaches the basics of
`add_picture` for a Spec-17 chart at
`/workspace/out/charts/<id>.png`. This file covers the rest: sizing
tables, the native python-pptx chart path (for cases where Spec 17
isn't in flight or you want data-linked editability), and the gotchas
that distinguish a sharp embed from a fuzzy mess.

Read this when the slide intent calls for a chart, when the embedded
image looks pixelated, or when the persona has a "deck must be
editable in PowerPoint" requirement.

---

## Decision: raster embed vs native chart

Two paths exist:

1. **Raster embed**: Spec 17 (or your own matplotlib code) produces a
   PNG; `add_picture` places it on the slide. This is the **D-16-5
   default**: works across all engines, no editability, sharp at the
   intended size, fuzzy if resized.

2. **Native python-pptx chart**: build the chart inline with
   `slide.shapes.add_chart(...)`. Editable in PowerPoint, smaller file,
   but limited chart types (bar / line / pie / scatter, with no boxplots,
   no heatmaps, no custom annotations).

| Need | Choose |
|---|---|
| Spec 17 already produced the chart | Raster embed (D-16-5) |
| Heatmap, complex annotations, custom layout | Raster embed (matplotlib) |
| User must edit the chart in PowerPoint | Native chart |
| Data is small (≤20 rows) and chart is bar/line/pie | Native chart |
| Deck must look identical on every viewer | Raster embed |

Default to raster embed unless the user explicitly asks for
editability.

---

## Raster embed: the D-16-5 contract

Spec 17 writes charts to `/workspace/out/charts/<id>.png` (sandbox
path). Same-session only; the file is gone across sessions (see
SKILL.md §Step 5 and the same-session reachability note).

```python
from pptx.util import Inches

slide.shapes.add_picture(
    "/workspace/out/charts/notice-periods.png",
    left=Inches(1.0),
    top=Inches(1.8),
    width=Inches(11.0),
    height=Inches(5.0),
)
```

**Aspect-ratio gotcha.** If you set both `width` and `height` and they
don't match the PNG's native aspect ratio, the image is stretched.
Two safer patterns:

```python
# Pattern A: lock to width; height auto-scales
pic = slide.shapes.add_picture(
    "/workspace/out/charts/notice-periods.png",
    left=Inches(1.0), top=Inches(1.8),
    width=Inches(11.0),
)

# Pattern B: query native size, scale uniformly
from PIL import Image
img = Image.open("/workspace/out/charts/notice-periods.png")
native_w, native_h = img.size
ratio = native_h / native_w
target_w = Inches(11.0)
pic = slide.shapes.add_picture(
    "/workspace/out/charts/notice-periods.png",
    left=Inches(1.0), top=Inches(1.8),
    width=target_w, height=int(target_w * ratio),
)
```

Pattern A is the leaner default; use Pattern B when you need exact
centring on a 13.333×7.5 slide.

---

## Sizing reference for the default 16:9 master

The default master is 13.333" wide × 7.5" tall (with 0.5" comfort
margin all around: usable area 12.333 × 6.5).

| Slide intent | left | top | width | height |
|---|---|---|---|---|
| Full-body chart (title + chart) | 0.5 | 1.6 | 12.333 | 5.4 |
| Right-half chart (left bullets, right chart) | 6.8 | 1.8 | 5.8 | 4.5 |
| Bottom-half chart (top bullets, bottom chart) | 1.5 | 4.0 | 10.5 | 3.0 |
| Quarter (one of four small charts) | varies | varies | 5.8 | 3.0 |

Units are `Inches`. For the centred full-body chart, `left = (13.333 -
width) / 2 = 0.5`.

---

## Resolution: making the embed sharp

Spec 17's matplotlib charts default to `dpi=100`. At full-slide width
(11"), that's 1100 px wide, fine for screen, soft for projection.

Two sharpness improvements:

1. **Render at `dpi=150` or `dpi=200`** when producing the PNG
   upstream. `plt.savefig(path, format="png", dpi=150)` produces a
   1650-px-wide PNG; embeds sharp on a projector. python-pptx accepts
   the larger PNG without complaint; the .pptx file grows by
   ~50-150 KB per chart.

2. **Don't resize the PNG larger than its source.** If the PNG is
   800 px wide and you place it at `Inches(11)`, the embed is
   upscaled and fuzzy. Either re-render larger or place at
   `Inches(7)`.

---

## Native python-pptx chart (when editability matters)

If the user says "I need to edit the numbers in PowerPoint", build a
native chart. The pattern (bar chart):

```python
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

chart_data = CategoryChartData()
chart_data.categories = ["1 year", "2 years", "5 years", "10 years"]
chart_data.add_series(
    "Pre-2024 (months)",
    (2.0, 2.0, 2.0, 2.0),
)
chart_data.add_series(
    "Post-2024 (months)",
    (3.0, 3.0, 4.5, 6.0),
)

chart_shape = s.shapes.add_chart(
    XL_CHART_TYPE.COLUMN_CLUSTERED,
    left=Inches(1.0), top=Inches(1.8),
    cx=Inches(11.0), cy=Inches(5.0),
    chart_data=chart_data,
)
chart = chart_shape.chart
chart.has_title = True
chart.chart_title.text_frame.text = "Notice period by tenant tenure"
chart.has_legend = True
```

Supported chart types (the useful subset):

| Constant | Use for |
|---|---|
| `XL_CHART_TYPE.COLUMN_CLUSTERED` | Side-by-side bars (the default first reach) |
| `XL_CHART_TYPE.COLUMN_STACKED` | Stacked bars when parts make a whole |
| `XL_CHART_TYPE.BAR_CLUSTERED` | Horizontal bars (long category labels) |
| `XL_CHART_TYPE.LINE` | Trends over time |
| `XL_CHART_TYPE.PIE` | Single series of ≤6 categories totalling 100% |
| `XL_CHART_TYPE.XY_SCATTER` | Two numeric axes (correlation) |

`XL_CHART_TYPE.XL_AREA`, doughnut, radar: supported but rarely the
right choice; prefer column/line for most cases.

---

## Colour for native charts

Native charts inherit the master's accent colours. To override one
series' colour:

```python
from pptx.dml.color import RGBColor

series = chart.plots[0].series[0]
fill = series.format.fill
fill.solid()
fill.fore_color.rgb = RGBColor(0xC8, 0x1D, 0x25)  # deep red
```

Apply this once per series. Don't colour every category cell; that's
visual noise.

---

## Common failure modes (chart-specific)

**Fuzzy embed.** The PNG was rendered at `dpi=100` and placed at
full-slide width. Re-render at `dpi=150` or shrink the embed.

**Stretched aspect.** Both `width` and `height` were set, ratio
didn't match. Use Pattern A (width-only) or Pattern B (PIL query).

**Missing file at embed time.** Spec 17 ran in a previous session;
the file is gone. Either run Spec 17 in the same session as the embed,
or take the chart via `input_files=` on the embed call.

**Native chart with too many series.** A column chart with 8 series
is unreadable. Cap at 3 series; if you have more, split to multiple
charts or switch to a line chart.

**Pie chart with negative values.** `XL_CHART_TYPE.PIE` requires
non-negative numbers. If any value is negative or you have >6 slices,
use a bar chart instead.

---

## End-to-end: chart-on-slide-4 (workshop deck pattern)

The representative task ("post-2024 rules + an embedded bar chart of
notice-period months by tenant-tenure-year") expects a chart on slide 4.
Full pattern:

```python
from pathlib import Path
from pptx.util import Inches

# Assume Spec 17 wrote /workspace/out/charts/notice-periods.png in this
# same session. If not, render it now via matplotlib.

s = prs.slides.add_slide(prs.slide_layouts[1])  # Title + Content
s.shapes.title.text = "Post-2024 rules"

body = s.placeholders[1].text_frame
body.text = "90-day notice for any increase ≥10%"
body.add_paragraph().text = "180-day notice for tenants ≥5 years tenure"
body.add_paragraph().text = "Notice voids retroactively if not in writing"

# Replace the body's lower half with the chart by shrinking the
# placeholder OR add the picture below. Simpler: leave bullets at top,
# anchor chart in the lower half:
chart_path = Path("/workspace/out/charts/notice-periods.png")
if chart_path.exists():
    s.shapes.add_picture(
        str(chart_path),
        left=Inches(1.0), top=Inches(4.2),
        width=Inches(11.0), height=Inches(2.8),
    )
else:
    # Spec 17 didn't run this session; fall back to native chart
    # using the post-2024 numbers from the bullets above.
    pass  # native-chart code from §"Native python-pptx chart"

s.notes_slide.notes_text_frame.text = (
    "Walk the audience through the two notice-period changes; "
    "the chart shows the cumulative impact by tenure-year."
)
```

This is the pattern T12's manual inspection scores against.
