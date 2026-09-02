# Tables in python-docx 1.1.2

Read this before writing a table that is anything more than 3 columns
of plain text.

## Anatomy

A `python-docx` table is a `docx.table.Table`. The model you write to:

- `table = doc.add_table(rows=R, cols=C)`: creates a `R × C` grid of empty cells.
- `table.style = "Light Grid Accent 1"` (or `"Table Grid"` for the unstyled black-border default).
- `cell = table.cell(row_idx, col_idx)`: 0-indexed access.
- `cell.text = "value"`: sets the cell to a single paragraph.
- For multi-paragraph or styled cell content, manipulate `cell.paragraphs[0]` and add runs.
- Append rows after creation with `table.add_row()` (returns the new `Row`).

## The header-row pattern

Every data table ships with a styled header row:

```python
from docx.shared import Pt, RGBColor
from docx.enum.table import WD_TABLE_ALIGNMENT

table = doc.add_table(rows=1, cols=3)
table.style = "Light Grid Accent 1"
table.alignment = WD_TABLE_ALIGNMENT.CENTER

header = table.rows[0]
for cell, text in zip(header.cells, ["Period", "Old rule", "New rule"]):
    cell.text = text
    run = cell.paragraphs[0].runs[0]
    run.bold = True
    run.font.size = Pt(11)

# Data rows.
data = [
    ("Pre-2024", "3 months notice", "n/a"),
    ("Post-2024", "n/a", "6 months notice"),
]
for period, old, new in data:
    row = table.add_row()
    row.cells[0].text = period
    row.cells[1].text = old
    row.cells[2].text = new
```

The criterion-#7 quality bar (`len(doc.tables[0].rows) >= 3`) is met
when there is one header row + at least two data rows.

## Column widths

`python-docx` does not automatically size columns from content. Word
honours `cell.width` *per cell* (the OOXML model has no shared
"column" width; each cell carries the width).

```python
from docx.shared import Inches

widths = [Inches(1.2), Inches(2.0), Inches(2.0)]
for row in table.rows:
    for cell, width in zip(row.cells, widths):
        cell.width = width
```

Set widths **after** adding all rows. New rows added afterwards inherit
the table-level default and need their own pass.

## Merged cells

Horizontal merge across a row, e.g. a "Summary" header spanning all
three columns:

```python
table = doc.add_table(rows=1, cols=3)
top = table.rows[0]
merged = top.cells[0].merge(top.cells[2])     # left ∪ right
merged.text = "Summary"
merged.paragraphs[0].runs[0].bold = True
```

Vertical merge for grouped rows:

```python
col0 = table.columns[0]
col0.cells[1].merge(col0.cells[3])    # rows 1..3 in column 0 share one cell
```

The merge returns the surviving cell; assign `.text` to it once.

## Repeating the header row on page break

Long tables span pages. Word's "Repeat as header row at the top of each
page" is set on the row's `trPr` element:

```python
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

def repeat_as_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)

repeat_as_header(table.rows[0])
```

Apply to the header row exactly once. If the table is added before the
first page break this has no visible effect; it kicks in only when the
table grows past one page.

## Cell shading (alternating row colour)

`cell.fill` does not exist in python-docx; apply shading via raw XML:

```python
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

def shade(cell, hex_rgb):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), hex_rgb)
    tc_pr.append(shd)

for i, row in enumerate(table.rows[1:], start=1):
    if i % 2 == 0:
        for cell in row.cells:
            shade(cell, "F2F2F2")    # light grey
```

## Common pitfalls

- `cell.text = "x"` replaces all paragraphs and runs in the cell. If you
  set `.text` after styling runs, the styling is gone. Style **after**
  setting text.
- `table.add_row()` mutates the table; the returned row is the new one.
  Iterating `for row in table.rows` while adding is undefined behaviour.
- Cell widths are set per-cell. Set them on every row, or copy widths
  from the first row's cells in a post-pass.
- `table.style` must be a style name that exists in the document's
  template. The default template ships `Table Grid`, `Light Grid Accent
  1..6`, `Medium Shading 1..2 Accent 1..6`. Custom names raise `KeyError`.
- Merged cells survive in the underlying XML, so querying
  `len(table.rows[0].cells)` may return the original column count even
  after a merge, but iterating returns the merge target multiple times
  (one per original cell). Iterate by index when widths matter.
