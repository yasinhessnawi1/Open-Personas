# XLSX formulas: depth supplement

Read this when the lean SKILL.md body's formula coverage isn't enough.
Covers composition, relative vs absolute references, cross-sheet
references, named ranges, and the `SUMIF` / `SUMIFS` / `VLOOKUP` /
`XLOOKUP` patterns that show up in real workbooks.

## How openpyxl writes formulas

`openpyxl` writes formulas as **strings starting with `=`** into cell
values. The formula is **not evaluated** at write time; Excel /
LibreOffice evaluates on open. If you need the computed value baked into
the workbook (e.g., for a `data_only=True` re-read in a test), open the
saved file in Excel / LibreOffice and re-save it, OR pre-compute the
value in Python and write both the formula and the computed value (the
latter via `cell.value` after `cell.formula = …`, not supported by
openpyxl directly; the simplest path is the Excel re-save).

```python
ws.cell(row=2, column=14, value="=SUM(B2:M2)")     # formula
ws.cell(row=2, column=14, value=sum(row_values))   # constant
```

## Relative vs absolute references: the cell of cells

Excel references come in four shapes:

| Reference | Column | Row | Use when… |
|---|---|---|---|
| `A1` | relative | relative | both column + row should shift on copy |
| `$A1` | **absolute** | relative | column locked; row shifts (e.g., lookup column) |
| `A$1` | relative | **absolute** | row locked; column shifts (e.g., header row) |
| `$A$1` | **absolute** | **absolute** | neither shifts; pinned cell |

The classic mistake is writing `=SUMIF(Months!A:A, A2, Months!N:N)`
instead of `=SUMIF(Months!$A:$A, A2, Months!$N:$N)`. The non-absolute
form **shifts the lookup column** when copied down rows, breaking every
row past the first. Always absolute the column-range references in
`SUMIF` / `VLOOKUP` / `INDEX-MATCH`.

## Cross-sheet references

Two valid forms:

```python
# Bare sheet name: only safe when the name contains no spaces or
# special characters.
"=SUM(Months!B2:M2)"

# Quoted sheet name: required if the name has spaces, hyphens, or
# starts with a digit.
"=SUM('Year 2026'!B2:M2)"
```

**Always reference the source sheet by its descriptive name**
(`Months!`), never by `Sheet1!`. If you later rename `Sheet1` to `Months`,
every formula referencing `Sheet1!` breaks with `#REF!`. The cheapest
hedge is the **named range** (see below).

## Named ranges: formulas that survive sheet rename

Named ranges decouple formulas from sheet names. After defining a named
range, formulas reference the name, not the address.

```python
from openpyxl.workbook.defined_name import DefinedName

# Define "Categories" as Months!$A$2:$A$5 at the workbook level.
wb.defined_names["Categories"] = DefinedName(
    name="Categories",
    attr_text="Months!$A$2:$A$5",
)
wb.defined_names["MonthsTotal"] = DefinedName(
    name="MonthsTotal",
    attr_text="Months!$N$2:$N$5",
)

# Now Summary formulas use the names, robust to Months sheet rename.
summary.cell(row=2, column=2,
             value="=SUMIF(Categories, A2, MonthsTotal)")
```

Named ranges are workbook-scoped by default; pass `localSheetId=<index>`
to scope to a single sheet. For most multi-sheet workbooks the workbook
scope is what you want.

## SUMIF: the workhorse cross-sheet aggregation

`SUMIF(range, criteria, sum_range)`:

- `range`: where to look for the match.
- `criteria`: what to match. Can be a literal (`"Rent"`), a cell
  reference (`A2`), or an expression (`">100"`, `"<="&B1`).
- `sum_range`: what to sum when the match is found. Same shape as
  `range`.

Common pitfalls:

- **Mismatched range shapes.** `SUMIF(A:A, "Rent", B:C)`: the sum_range
  has two columns but range has one; Excel will sum only column `B`.
  Keep the shapes parallel.
- **Non-absolute lookup columns.** See the relative-vs-absolute section.
- **Hidden whitespace.** `"Rent "` (trailing space) doesn't match
  `"Rent"`. If your categories come from user input, normalise on write.

## SUMIFS: multi-condition aggregation

`SUMIFS(sum_range, criteria_range1, criteria1, [criteria_range2, criteria2], …)`:

```python
# Sum all "Rent" rows where the month column is between Jan and Jun.
'=SUMIFS(Months!$N:$N, Months!$A:$A, "Rent", Months!$B:$B, ">="&DATE(2026,1,1), Months!$B:$B, "<="&DATE(2026,6,30))'
```

Argument order differs from `SUMIF`: **`sum_range` comes first**, then
pairs of (criteria_range, criteria). This trips everyone the first time.

## VLOOKUP / XLOOKUP: row lookups

`VLOOKUP(lookup_value, table_array, col_index, [range_lookup])`:

```python
# Find the year-total for the category in A2, looking it up in the
# Months sheet's first 14 columns.
'=VLOOKUP(A2, Months!$A:$N, 14, FALSE)'
```

`FALSE` (exact match) is almost always what you want; `TRUE` (approximate
match) requires the lookup column to be sorted ascending and is a common
source of bugs.

`XLOOKUP` (Excel 365 / LibreOffice 7.4+) is the modern replacement:

```python
'=XLOOKUP(A2, Months!$A:$A, Months!$N:$N)'
```

`XLOOKUP` handles columns in any order (no `col_index` arithmetic),
returns `#N/A` when the value isn't found, and supports left-of-key
lookups. Prefer it when the target environment supports it.

## INDEX / MATCH: when VLOOKUP isn't flexible enough

For lookups where the return column is **left** of the key column, or
where you want exact-match control:

```python
'=INDEX(Months!$N:$N, MATCH(A2, Months!$A:$A, 0))'
```

`MATCH(value, range, 0)` returns the row index of `value` in `range`
(exact match); `INDEX(range, row_index)` returns the value at that row.

## Formula debugging when you can't open Excel

In a sandbox with no Excel, you can load the workbook with
`openpyxl.load_workbook(path, data_only=False)` and inspect formula
strings as-written:

```python
from openpyxl import load_workbook

wb = load_workbook("/workspace/out/budget-2026.xlsx", data_only=False)
print(wb["Summary"]["B2"].value)
# expected: "=SUMIF(Months!$A:$A, A2, Months!$N:$N)"
```

If the value is `None` or a different formula, you wrote it wrong; fix
in code and re-save. To see evaluated values, you need the file
round-tripped through Excel / LibreOffice once; openpyxl alone cannot
evaluate formulas.
