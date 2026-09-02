# XLSX charts: depth supplement

Read this when the lean SKILL.md body's chart guidance isn't enough.
Covers `openpyxl`-native charts (the strong default: data-linked,
in-cell, no raster handoff), chart types and when to use each, axes /
titles / legends / styling, multi-series charts, and a tail note on the
Spec 17 raster-embed path for the rare case you want a pre-rendered PNG.

## Why openpyxl-native is the default

openpyxl writes **real Excel charts**, backed by the cell range you
supply, updating when the underlying cells change, zoomable without
pixelation, and editable by the user in Excel/LibreOffice. This is
almost always what you want.

A PNG embedded as an image is **frozen** at the resolution you saved it
at, doesn't update when data changes, and can't be edited. Use a raster
embed only when the chart was produced by a different tool (e.g., Spec
17's matplotlib-based chart skill) and you want to preserve its exact
appearance; see the tail note.

## The three chart types you'll use 90% of the time

### Bar / Column chart

Compare values across discrete categories. Use **column** (vertical
bars) for category-vs-value; use **bar** (horizontal bars) when category
names are long and need horizontal space.

```python
from openpyxl.chart import BarChart, Reference

chart = BarChart()
chart.type = "col"            # "col" = vertical; "bar" = horizontal
chart.style = 10              # 1..48; built-in style palette
chart.title = "Annual budget by category"
chart.y_axis.title = "NOK"
chart.x_axis.title = "Category"

# Data: the year-total column (N) for rows 2..5
data = Reference(ws, min_col=14, min_row=1, max_col=14, max_row=5)
# Categories: the category column (A) for rows 2..5
categories = Reference(ws, min_col=1, min_row=2, max_col=1, max_row=5)

chart.add_data(data, titles_from_data=True)
chart.set_categories(categories)

ws.add_chart(chart, "P2")     # anchor top-left at cell P2
```

`Reference` is `(worksheet, min_col, min_row, max_col, max_row)`.
`add_data(..., titles_from_data=True)` treats the first row of the
data range as the series title.

### Line chart

Show a trend over an ordered category axis (usually time).

```python
from openpyxl.chart import LineChart, Reference

chart = LineChart()
chart.title = "Monthly spend, 2026"
chart.y_axis.title = "NOK"
chart.x_axis.title = "Month"

# Data: 12 month columns (B..M) for one or more categories.
data = Reference(ws, min_col=2, min_row=2, max_col=13, max_row=5)
months = Reference(ws, min_col=2, min_row=1, max_col=13, max_row=1)

chart.add_data(data, titles_from_data=False, from_rows=True)
chart.set_categories(months)

ws.add_chart(chart, "P2")
```

`from_rows=True` says "each row of the data range is one series", which is what
you want when each category (Rent, Salaries, …) is a row and the months
are columns.

### Pie chart

Show parts of a whole. Use sparingly; pie charts are hard to read for
more than 5 slices. A bar chart is almost always better.

```python
from openpyxl.chart import PieChart, Reference

chart = PieChart()
chart.title = "Year-total share by category"

data = Reference(ws, min_col=14, min_row=2, max_col=14, max_row=5)
categories = Reference(ws, min_col=1, min_row=2, max_col=1, max_row=5)

chart.add_data(data)
chart.set_categories(categories)

ws.add_chart(chart, "P2")
```

## Multi-series charts

For comparing multiple categories side-by-side over the same axis, add
each series separately or use a single `Reference` covering all
categories' columns. The single-`Reference` form (with
`titles_from_data=True`) is simpler:

```python
chart = BarChart()
chart.grouping = "clustered"     # side-by-side bars per category
# or "stacked" for stacked bars, "percentStacked" for 100% stacked

# Three series in columns B, C, D; one row per group in rows 2..5.
data = Reference(ws, min_col=2, min_row=1, max_col=4, max_row=5)
groups = Reference(ws, min_col=1, min_row=2, max_col=1, max_row=5)

chart.add_data(data, titles_from_data=True)
chart.set_categories(groups)
```

## Anchoring + sizing

`ws.add_chart(chart, "P2")` anchors the chart with its top-left at cell
P2. To control size:

```python
chart.width = 18    # in cm
chart.height = 10
```

For a workbook the user will scroll through, leave at least 2 empty
columns between the data and the chart anchor so the chart doesn't
overlap the data.

## Styling: palettes and themes

`chart.style = N` picks one of 48 built-in palette+style combinations.
Styles 1-12 are the saturated colour set; 13-24 are pastel; 25-36 are
darker variants; 37-48 are monochrome. For a persona with a declared
`visual_style` palette, you'll usually want to override individual
series colours instead:

```python
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.drawing.fill import ColorChoice

for idx, series in enumerate(chart.series):
    series.graphicalProperties = GraphicalProperties(
        solidFill=ColorChoice(srgbClr=["305496", "70AD47", "FFC000"][idx])
    )
```

## Axis tuning

```python
chart.y_axis.scaling.min = 0          # start at zero (almost always)
chart.y_axis.scaling.max = 100000     # cap (optional)
chart.y_axis.majorUnit = 20000        # gridline spacing
chart.y_axis.number_format = "#,##0"  # axis label format
```

Starting the y-axis above zero is a classic chart lie; it exaggerates
small differences. Almost always start at zero for value comparisons.

## Legend + data labels

```python
from openpyxl.chart.label import DataLabelList

chart.dataLabels = DataLabelList(showVal=True)  # show each bar's value
chart.legend.position = "b"   # bottom; "t" top, "l" left, "r" right, "tr" top-right
chart.legend = None           # remove the legend entirely
```

Data labels turn a chart into a chart-plus-table, useful for handouts,
distracting on dense charts. Decide per use case.

## When the chart is wrong

- **Wrong axis on the wrong series.** Almost always means `add_data`
  was called with `from_rows` flipped. The mental model: `from_rows=True`
  ⇔ each row is a series, columns are the category axis.
- **Empty chart.** The `Reference` ranges are wrong (off-by-one on
  `min_row` / `max_row`), or pointing at empty cells. Print the `Reference`
  values and the cells they cover.
- **No category labels.** `set_categories` not called, or the category
  range overlaps the data range.

## Tail note: D-16-5 raster embed (Spec 17 PNG)

If a Spec 17 chart was already produced **in the same session** and you
want to embed it as a raster image rather than recompute the chart
natively in xlsx, the path convention is
`/workspace/out/charts/<id>.png` per D-16-5 (PNG raster confirmed across
all three document engines; see Spec 16 `research.md` §5).

```python
from openpyxl.drawing.image import Image
img = Image("/workspace/out/charts/<id>.png")
img.width = 600   # display pixels
img.height = 360
ws.add_image(img, "P2")
```

**Caveat (Spec 16 state.md A2):** cross-session persona-workspace
persistence is not in scope for v0.1; the chart file from Spec 17 is
reachable only within the **same sandbox session**. Across sessions, the
file is gone and the embed will fail with `FileNotFoundError`.

The openpyxl-native chart above is the strong default for xlsx. Reach
for the PNG embed only when Spec 17 has already produced a chart this
turn and you want pixel-perfect parity with that chart's appearance.
