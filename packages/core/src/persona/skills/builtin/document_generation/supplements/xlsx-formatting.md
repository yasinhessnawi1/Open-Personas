# XLSX formatting: depth supplement

Read this when the lean SKILL.md body's formatting coverage isn't
enough. Covers `number_format` strings (currency / date / percent /
custom), column-width sizing strategies, conditional formatting, borders,
merged cells, row heights, and freeze-pane / split-pane combinations.

## `number_format`: the centre of the format universe

`number_format` is a string that controls how a numeric cell is
**displayed** (the underlying value is unchanged). Set it on the cell:

```python
ws["B2"].value = 12345.67
ws["B2"].number_format = "#,##0.00 [$NOK]"
# displays as: 12 345,67 NOK (depending on locale)
```

Common format strings:

| Purpose | Format string | Renders as |
|---|---|---|
| Generic number | `"0"` | `12346` |
| Thousands separator | `"#,##0"` | `12,346` |
| Two decimal places | `"#,##0.00"` | `12,345.67` |
| Currency (NOK) | `"#,##0.00 [$NOK]"` | `12 345,67 NOK` |
| Currency (USD) | `"[$$-409]#,##0.00"` | `$12,345.67` |
| Percent | `"0.00%"` | `12.35%` |
| Date | `"yyyy-mm-dd"` | `2026-06-06` |
| Date + time | `"yyyy-mm-dd hh:mm"` | `2026-06-06 14:30` |
| Negative as red | `"#,##0.00;[Red]-#,##0.00"` | `1,234.56` / `-1,234.56` (red) |

The four sub-format slots in a custom format are
`positive;negative;zero;text`. Use the negative slot to colour negative
numbers, or to wrap them in parentheses (`"(#,##0.00)"`).

### NOK / Norwegian locale specifics

The Norwegian decimal separator is `,` (comma) and the thousands
separator is non-breaking space. Excel renders the format string using
the **viewer's locale**, not the writer's, so:

```python
ws["B2"].number_format = "#,##0.00 [$NOK]"
```

renders as `12 345,67 NOK` for a Norwegian viewer and as `12,345.67 NOK`
for an English viewer. The format string uses `.` and `,` as US-style
placeholders; the viewer's locale localises them.

## Applying format to many cells

Per-cell `number_format` is verbose for large sheets. For a column
block:

```python
for row in ws.iter_rows(min_row=2, min_col=2, max_col=14):
    for cell in row:
        cell.number_format = "#,##0.00 [$NOK]"
```

Note: `ws.column_dimensions["B"].number_format = …` does **not** apply
to existing cells; it's a default for newly added cells only.

## Column widths: sizing strategies

Three approaches:

### Fixed widths

The simplest: set widths to known good values:

```python
from openpyxl.utils import get_column_letter
for col in range(1, 15):
    ws.column_dimensions[get_column_letter(col)].width = 14
ws.column_dimensions["A"].width = 22
```

### Auto-fit (best-effort)

openpyxl has no native auto-fit (Excel's auto-fit needs the rendered
font metrics). Approximation: walk the column and set width to the
longest string length + 2.

```python
def autosize(ws, columns):
    for col_idx in columns:
        letter = get_column_letter(col_idx)
        max_len = max(
            (len(str(c.value)) for c in ws[letter] if c.value is not None),
            default=10,
        )
        ws.column_dimensions[letter].width = max_len + 2

autosize(ws, range(1, 15))
```

This is good enough for most sheets but undersizes columns containing
formulas (since the formula string is shorter than the rendered value).
For currency / date columns, set fixed widths instead.

### Wide-first-column convention

The leftmost column usually holds labels (categories, dates) and is
wider than the data columns. Convention: 22 for labels, 14 for currency,
12 for dates, 10 for small numbers.

## Row heights

Default row height is 15 pt. For header rows that wrap text, raise:

```python
ws.row_dimensions[1].height = 30
ws["A1"].alignment = Alignment(wrap_text=True, vertical="center")
```

## Conditional formatting

Highlight cells based on their values. The most useful pattern is "above
average" or "above threshold":

```python
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule
from openpyxl.styles import PatternFill

red_fill = PatternFill("solid", fgColor="FFC7CE")
ws.conditional_formatting.add(
    "B2:M5",
    CellIsRule(operator="greaterThan", formula=["10000"], fill=red_fill),
)

# 3-colour scale (low → mid → high)
ws.conditional_formatting.add(
    "B2:M5",
    ColorScaleRule(
        start_type="min", start_color="63BE7B",
        mid_type="percentile", mid_value=50, mid_color="FFEB84",
        end_type="max", end_color="F8696B",
    ),
)
```

## Borders

Bordered cells stand out from styled-but-borderless ones. The pattern
is verbose; usually worth a helper:

```python
from openpyxl.styles import Border, Side

thin = Side(style="thin", color="000000")
box = Border(left=thin, right=thin, top=thin, bottom=thin)

for row in ws.iter_rows(min_row=1, max_row=5, min_col=1, max_col=14):
    for cell in row:
        cell.border = box
```

## Merged cells

Use sparingly; merged cells break sorting, filtering, and many formula
patterns. Useful for headers that span columns:

```python
ws.merge_cells("A1:N1")
ws["A1"].value = "Annual budget 2026"
ws["A1"].alignment = Alignment(horizontal="center")
ws["A1"].font = Font(bold=True, size=14)
```

The value goes in the top-left cell of the merged range; the others are
implicitly empty.

## Freeze panes vs split panes

`ws.freeze_panes = "B2"` freezes everything **above and to the left** of
B2: row 1 (header) and column A (labels) stay visible while the user
scrolls.

| Freeze target | Locks | Use when… |
|---|---|---|
| `"A2"` | row 1 | header row only |
| `"B1"` | column A | label column only |
| `"B2"` | row 1 + column A | both (most common for budgets) |

Split panes (`ws.sheet_view.view = "pageBreakPreview"` + manual split
markers) are rarely worth the complexity; use freeze.

## Header-row anti-patterns

- **Mixed alignment.** Some headers centred, others left-aligned.
  Pick one.
- **Inconsistent fill colour across sheets.** All sheets in one
  workbook should share the same header style.
- **Forgetting wrap_text on multi-word headers.** "Year total" wraps
  ugly in a 10-char column. Either widen or wrap.

## When formatting breaks the data

Two cells with the same `value` but different `number_format` look
different but compare equal in formulas. Two cells with the same
**displayed** value but different underlying values compare unequal. If
the user is going to copy-paste-special "values only", they get the raw
number, so make sure that's what you want them to read.
