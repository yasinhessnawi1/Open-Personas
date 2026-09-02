# Table of contents in python-docx 1.1.2

Read this **every time** you put a TOC in a document. The TOC is the
docx feature with the largest gap between "the code looks right" and
"the user opens the file and sees what they expect." Without
understanding the Word "Update Field" gotcha, every TOC ships looking
empty.

## What python-docx writes vs what Word renders

Word's TOC is a **field**, not a static block of text. A field is a
deferred instruction: the OOXML contains a `<w:fldSimple>` or a
`<w:fldChar>` + `<w:instrText>` sequence that tells Word *"on open,
walk the document, find paragraphs with the Heading 1 / 2 / 3 styles,
build the TOC text here."*

python-docx 1.1.2 writes the **field shell** (the instruction). It does
NOT execute the instruction. Word executes it on open, but **only
after** the user hits **F9** or right-clicks → **Update Field**. (Word
2016+ usually prompts to update fields on open; recent Word for Mac
does not always prompt.)

This is the single most common docx-skill failure: the persona writes
correct code, the file is correct, the user opens it, and the TOC
section is empty. The persona has to **warn the user** in the same turn
that the file was produced.

## The minimal correct field shell

```python
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

def add_toc(doc, levels="1-3"):
    p = doc.add_paragraph()
    p.style = doc.styles["TOC Heading"]
    p.add_run("Table of Contents")

    p = doc.add_paragraph()
    run = p.add_run()
    fld_char_begin = OxmlElement("w:fldChar")
    fld_char_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = f' TOC \\o "{levels}" \\h \\z \\u '
    fld_char_separate = OxmlElement("w:fldChar")
    fld_char_separate.set(qn("w:fldCharType"), "separate")
    fld_char_end = OxmlElement("w:fldChar")
    fld_char_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_char_begin)
    run._r.append(instr)
    run._r.append(fld_char_separate)
    run._r.append(fld_char_end)
```

Place the call **at the top** of the body, immediately after the title:

```python
doc.add_heading("Norwegian tenancy law: protections for tenants", level=0)
add_toc(doc, levels="1-3")
doc.add_page_break()
doc.add_heading("Background", level=1)
# … rest of the document …
```

The criterion-#1 quality bar (`assert "fldSimple" in xml or "fldChar"
in xml`) passes because `fldChar` is in the XML.

## The `\o "1-3"` switch and friends

The instr field is a TOC field with switches. The useful ones:

- `\o "1-3"`: include Heading 1 through Heading 3 (most common). `"1-9"` for all heading levels.
- `\h`: render entries as hyperlinks (so the user can ctrl-click to navigate).
- `\z`: hide tab leaders in Web Layout view.
- `\u`: use outline-level paragraphs (not just named-style headings).

Default to `\o "1-3" \h \z \u`. Wider ranges produce noisier TOCs.

## The "Update Field" gotcha: what to tell the user

When the document opens for the first time after generation, Word
shows a placeholder like *"Right-click here to update field"* OR
nothing visible at all. Tell the user **explicitly**, in your reply:

> "I've put a Table of Contents at the top. **Word will show it as
> empty until you press F9** (or right-click → Update Field). This is
> normal for any docx with a generated TOC; Word fills it on first
> update, not at write time."

LibreOffice Writer fills the TOC on first open without user action.
Microsoft Word does not (as of Word 2021 / 365). Don't assume the
user is on LibreOffice.

## Pre-populating the cached TOC text (optional, advanced)

If the document is meant to be viewed without user interaction (e.g.
print-on-receive), you can embed the **cached TOC text** between the
`separate` and `end` field chars so Word displays it immediately while
still treating it as a live field on F9.

This requires walking the document headings yourself, generating the
"Heading text … page number" lines, and inserting them as paragraphs
with the `TOC 1` / `TOC 2` / `TOC 3` styles between the `separate` and
`end` chars. Page numbers are not knowable until Word lays the document
out, so the cached page numbers are best-effort (often `1`, `1`, `1`
until F9 fixes them).

For v0.1 the recommended path is the field-shell-only approach above
plus the user-facing warning. Cached pre-population is a v0.2
enhancement; document it in the close-out if you implement it.

## Common pitfalls

- **Calling `add_toc` before any headings exist**: the field shell is fine. Word builds the TOC from the headings present at open time, not at write time. Order does not matter as long as headings have named styles.
- **Headings with ad-hoc bold + size instead of `Heading 1` style**: they are invisible to the TOC field walker. The named style is what Word reads. Re-emphasised here: named styles, always.
- **No `TOC Heading` paragraph above the field**: the TOC entries render, but there is no "Table of Contents" label. Add the label paragraph above.
- **Using `\\o "1-3"` (escaped backslashes in a regular string)**: that produces `\\o "1-3"` literally in the field instr, which Word does not parse. Use a raw string `r' TOC \o "1-3" \h '` or escape once `' TOC \\o "1-3" \\h '`. The example above uses a regular f-string where the single backslashes survive (`\o` → `\o` since `\o` is not a Python escape).
- **No `\h` switch**: the entries are still clickable in Word (Ctrl+Click) but only because Word's default behaviour adds hyperlinks; explicit `\h` is portable across Word versions and LibreOffice.
- **Generating TOCs in a one-page document**: Word still renders the field, but the document looks silly. Skip TOCs for ≤3-section / ≤2-page documents.
