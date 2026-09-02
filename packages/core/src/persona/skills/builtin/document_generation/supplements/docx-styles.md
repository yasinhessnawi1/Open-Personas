# Styles, lists, footers in python-docx 1.1.2

Read this when you need named-style chains, list styles, page numbers in
the footer, or want to set paragraph spacing consistently across the
document. The lean SKILL.md body covers the "set Normal" minimum; this
file covers the rest.

## The style model

Word documents carry **named styles** in `doc.styles`. The default
template (`Document()` with no argument) provides:

- Paragraph styles: `Normal`, `Title`, `Subtitle`, `Heading 1` … `Heading 9`, `List Bullet`, `List Number`, `Quote`, `Intense Quote`, `Caption`, `Footer`, `Header`, `TOC Heading`, `TOC 1` … `TOC 9`.
- Character styles: `Emphasis`, `Strong`, `Subtle Emphasis`, `Subtle Reference`, `Intense Reference`, `Book Title`.
- Table styles: see `supplements/tables.md`.

Anything else is `KeyError`. Use these names verbatim.

## Setting body defaults

Run this **once at the top** of the script, before adding any content.
Word reads `Normal` for any paragraph that does not declare its own
style; setting `Normal` cascades:

```python
from docx.shared import Pt, RGBColor

normal = doc.styles["Normal"]
normal.font.name = "Calibri"        # or persona's visual_style preference
normal.font.size = Pt(11)
normal.font.color.rgb = RGBColor(0x1F, 0x1F, 0x1F)    # near-black, not pure
normal.paragraph_format.space_after = Pt(6)
normal.paragraph_format.line_spacing = 1.15
```

The criterion-#4 quality bar (`doc.styles["Normal"].font.name is not
None and .size is not None`) passes once these two lines run.

## Heading styles in a chain

Adjust the heading styles to a consistent palette **once**, not per
paragraph:

```python
for level, size in [(1, 18), (2, 14), (3, 12)]:
    style = doc.styles[f"Heading {level}"]
    style.font.name = "Calibri"
    style.font.size = Pt(size)
    style.font.bold = True
    style.paragraph_format.space_before = Pt(12)
    style.paragraph_format.space_after = Pt(4)
```

Then apply by name:

```python
doc.add_heading("Section title", level=1)         # uses Heading 1
doc.add_paragraph("Subsection lead", style="Heading 2")
```

## List styles

```python
doc.add_paragraph("First bullet", style="List Bullet")
doc.add_paragraph("Second bullet", style="List Bullet")
doc.add_paragraph("Numbered item", style="List Number")
```

`List Bullet 2` / `List Number 2` give the indented second-level
variants. python-docx does not produce a "nested list" automatically;
each paragraph carries its own list style; visual nesting comes from the
list style itself, not from a parent / child relationship.

## Page numbers in the footer (field code)

There is **no `add_page_number()` helper** in python-docx 1.1.2. You add
a `PAGE` field by appending raw OOXML to the footer paragraph:

```python
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

def add_page_number(paragraph):
    run = paragraph.add_run()
    fld_char1 = OxmlElement("w:fldChar")
    fld_char1.set(qn("w:fldCharType"), "begin")
    instr_text = OxmlElement("w:instrText")
    instr_text.set(qn("xml:space"), "preserve")
    instr_text.text = "PAGE"
    fld_char2 = OxmlElement("w:fldChar")
    fld_char2.set(qn("w:fldCharType"), "end")
    run._r.append(fld_char1)
    run._r.append(instr_text)
    run._r.append(fld_char2)

footer = doc.sections[0].footer
para = footer.paragraphs[0]
para.text = "Page "
add_page_number(para)
para.alignment = 1    # WD_ALIGN_PARAGRAPH.CENTER = 1
```

The quality bar (`any("PAGE" in p._p.xml for p in
doc.sections[0].footer.paragraphs)`) passes once the field is in the XML.
Word renders "Page 1", "Page 2", … on open with no user action needed
(unlike TOC, see `supplements/toc.md`).

## Page setup

`doc.sections[0]` carries the page geometry. Defaults are US Letter +
1-inch margins; for an A4 European persona:

```python
from docx.shared import Mm

section = doc.sections[0]
section.page_height = Mm(297)
section.page_width = Mm(210)
section.top_margin = Mm(25)
section.bottom_margin = Mm(25)
section.left_margin = Mm(25)
section.right_margin = Mm(25)
```

Set this **before** adding content. Changing page size mid-document
requires section breaks (rare; usually not needed for v0.1).

## Paragraph spacing: the consistent-spacing trap

The quality-bar row "Paragraph spacing consistent" fails when two
blank `add_paragraph("")` calls leave back-to-back empty paragraphs.
Use `space_after` on the paragraph format instead:

```python
p = doc.add_paragraph("Some prose.")
p.paragraph_format.space_after = Pt(12)
```

Never write `doc.add_paragraph("\n\n")`; `\n` is a line break inside
the run, not a paragraph break, and the result is ragged. New paragraphs
come from new `add_paragraph` calls.

## Hyperlinks

python-docx 1.1.2 has no `add_hyperlink` helper. Use the field-code
idiom (similar to PAGE) with `HYPERLINK "<url>"` as the field instr;
the simpler path is to write the URL as plain text and let Word
auto-link on open (acceptable for v0.1 if hyperlinks are not the focus).
