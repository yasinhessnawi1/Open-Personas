# Layouts: non-default slide patterns

The `pptx_generation` SKILL.md body covers the title slide and the
title+content slide. This file covers the layouts you reach for when
the default two don't fit the slide's intent.

Read this from inside your code when the slide-list plan calls for one
of: two-column comparison, image-with-caption, section divider, "title
only" with a single visual, or a custom layout that isn't on the
default master.

---

## Default master layout indices (recap)

`python-pptx` ships a default 16:9 master. The layouts available via
`prs.slide_layouts[N]`:

| N | Name | Use it for |
|---|---|---|
| 0 | Title Slide | The deck's cover slide. Has a title placeholder + a subtitle. |
| 1 | Title and Content | Standard content slide, with a title + a body text-frame for bullets. |
| 2 | Section Header | Visual divider between deck sections. Title + a small subtitle. |
| 3 | Two Content | Title + two side-by-side body text-frames (left/right comparison). |
| 4 | Comparison | Title + two title-content pairs (left/right with sub-headings). |
| 5 | Title Only | Title placeholder only. Use when the body is a chart or image. |
| 6 | Blank | No placeholders. Avoid for content slides; placeholders give consistency. |
| 7 | Content with Caption | Body + caption text below. Good for image+caption. |
| 8 | Picture with Caption | Picture placeholder + caption. Use the picture placeholder, not `add_picture` floating. |

`len(prs.slide_layouts)` is 11 in the default master; layouts 9 and 10
are duplicates of 1 / 5 with different background formatting and
rarely the right choice. Stick with 0-8 for normal decks.

---

## Pattern: two-column comparison

Pre-2024 vs post-2024, before vs after: the comparison layout (4) is
designed for this. Layout 3 is a leaner alternative without
sub-headings.

```python
from pptx.util import Inches, Pt

s = prs.slides.add_slide(prs.slide_layouts[4])  # Comparison
s.shapes.title.text = "Rent-increase notice periods"

# placeholders[1] = left-column heading (e.g., "Pre-2024")
# placeholders[2] = left-column body
# placeholders[3] = right-column heading (e.g., "Post-2024")
# placeholders[4] = right-column body
s.placeholders[1].text = "Pre-2024"
s.placeholders[2].text_frame.text = "60 days for any increase"
s.placeholders[2].text_frame.add_paragraph().text = "No tenure floor"

s.placeholders[3].text = "Post-2024"
s.placeholders[4].text_frame.text = "90 days for ≥10% increase"
s.placeholders[4].text_frame.add_paragraph().text = "180 days for tenure ≥5y"
```

If you want a plain two-column body without the sub-headings, layout 3
is the same shape with placeholders `[1]` and `[2]` being the two body
text-frames directly.

---

## Pattern: image with caption

Use layout 7 (Content with Caption) when the focus is an image with a
short caption underneath.

```python
from pptx.util import Inches

s = prs.slides.add_slide(prs.slide_layouts[7])
s.shapes.title.text = "Norway, tenant complaints 2025"

# placeholders[1] = the content area (use add_picture into the slide,
# anchored where the placeholder sits, keeping within its bounds)
# placeholders[2] = caption text frame

s.shapes.add_picture(
    "/workspace/out/charts/complaints-2025.png",
    left=Inches(1.0), top=Inches(1.5),
    width=Inches(7.0), height=Inches(4.5),
)
s.placeholders[2].text_frame.text = (
    "Source: Statistics Norway, Q1-Q4 2025."
)
```

If you only have a picture (no caption), layout 5 (Title Only) +
`add_picture` is leaner.

---

## Pattern: section divider

Layout 2 is the right choice when you want a visual break between
major sections of a long deck (a "chapter break"). One title, one
subtitle, generous whitespace.

```python
s = prs.slides.add_slide(prs.slide_layouts[2])  # Section Header
s.shapes.title.text = "Part II: Post-2024 framework"
s.placeholders[1].text = "The four amendments and their effective dates"
```

Don't use section headers in a deck under 8 slides; the divider
overhead isn't earned.

---

## Pattern: title-only for a single visual

When a slide is *just* a chart or a single large image (no bullets,
no caption), layout 5 keeps it clean.

```python
from pptx.util import Inches

s = prs.slides.add_slide(prs.slide_layouts[5])
s.shapes.title.text = "Annual complaint trend"
s.shapes.add_picture(
    "/workspace/out/charts/trend-2025.png",
    left=Inches(0.75), top=Inches(1.5),
    width=Inches(12.0), height=Inches(5.5),
)
```

Centre the image horizontally if the deck width is 13.333"; see the
sizing table in `charts.md`.

---

## Picking the right layout: a decision rule

The default-template-mess failure mode (see SKILL.md §Failure modes)
comes from defaulting to layout 0 or 1 for everything. The rule:

1. Cover slide (slide 0) → layout 0 (Title Slide).
2. Section divider (only in decks ≥8 slides) → layout 2.
3. Title + bullets → layout 1.
4. Title + two parallel topics → layout 3 (plain) or 4 (with
   sub-headings).
5. Title + caption + image → layout 7.
6. Title + one large visual → layout 5.
7. Anything else → layout 1 (Title + Content) as the default.

If you find yourself reaching for layout 6 (Blank), stop and reconsider;
blank slides skip the master's typography and look amateur.

---

## Custom layouts (the master-edit path)

If none of the 9 default layouts fit, the right answer is *almost
always* layout 1 + custom text-frame placement. Editing the slide
master directly is possible (`prs.slide_master.slide_layouts[N]
.shapes`) but breaks the "one master per deck" rule because subtle
master changes propagate to every slide.

The cleanest non-default layout is: add an empty slide via layout 5
(Title Only), then add a `text_frame` shape via
`s.shapes.add_textbox(left, top, width, height)`. This stays within
the master typography (the textbox inherits the master's default font
via the theme) without editing the master itself.

```python
from pptx.util import Inches, Pt

s = prs.slides.add_slide(prs.slide_layouts[5])
s.shapes.title.text = "Custom layout"

tb = s.shapes.add_textbox(
    left=Inches(0.75), top=Inches(1.5),
    width=Inches(11.5), height=Inches(5.0),
)
tf = tb.text_frame
tf.word_wrap = True
tf.text = "Custom body content."
# To stay consistent with the master, do NOT set font name/size here;
# inherit from the theme. Only set sizes if you need non-default
# emphasis.
```

---

## Placeholder gotchas

- **`s.placeholders[N]` indices vary by layout.** Layout 1 has
  `placeholders[0]` (title) and `placeholders[1]` (body); layout 4 has
  five placeholders. Print `[ph.placeholder_format.idx for ph in
  s.placeholders]` once when adapting a layout you haven't used before.
- **`title` is always `s.shapes.title`, not `s.placeholders[0]`.** Use
  the explicit `shapes.title` accessor; it's the same object but
  the named accessor is clearer and won't break if the layout's title
  placeholder isn't at index 0.
- **A placeholder's `text_frame.text = "..."` replaces *all* paragraphs.**
  To add a second paragraph, use `add_paragraph()` after setting `text`.
  Setting `text` again replaces the lot.
