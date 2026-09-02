# Theme: typography, colour, master consistency

The `pptx_generation` SKILL.md body sets the floor: title ≥28pt, body
≥18pt, one font family per deck. This file covers the cases where you
need finer control (weight, colour, accent palette, master-level
overrides) and how to map a persona's `identity.visual_style` onto
the deck's theme.

Read this from inside your code when the persona has a declared
visual_style hint, or when the slide intent calls for a non-default
accent colour, a heavier title weight, or a custom title-bar fill.

---

## The python-pptx theme model (in one paragraph)

A `Presentation()` carries one `slide_master` with a `theme`. The
theme defines six colours (background-1/2, text-1/2, accent-1
through 6) and a font scheme (major font for titles, minor font for
body). Every layout inherits the theme; every slide inherits its
layout; every shape can override locally via `run.font` or
`shape.fill`. The cleanest approach is: leave the theme alone; set
per-run overrides only when the slide intent demands it.

---

## Setting fonts on a run

```python
from pptx.util import Pt
from pptx.dml.color import RGBColor

# Title run
title_frame = s.shapes.title.text_frame
for p in title_frame.paragraphs:
    for r in p.runs:
        r.font.name = "Calibri"  # or the persona's preferred sans
        r.font.size = Pt(32)
        r.font.bold = True
        r.font.color.rgb = RGBColor(0x1F, 0x2D, 0x3D)  # dark navy

# Body run
body_frame = s.placeholders[1].text_frame
for p in body_frame.paragraphs:
    for r in p.runs:
        r.font.name = "Calibri"
        r.font.size = Pt(20)
        r.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
```

**Gotcha:** if you set `text_frame.text = "..."` *after* tweaking runs,
the runs are recreated and your formatting is lost. Set the text
first, then iterate runs.

---

## Accent colour for emphasis

Use one accent colour for emphasis (a chart's highlighted bar, a
callout box). One accent, not two. Two accents means the audience
doesn't know which is the focus.

```python
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE

ACCENT = RGBColor(0xC8, 0x1D, 0x25)  # deep red

box = s.shapes.add_shape(
    MSO_SHAPE.ROUNDED_RECTANGLE,
    left=Inches(0.75), top=Inches(5.5),
    width=Inches(11.5), height=Inches(1.0),
)
box.fill.solid()
box.fill.fore_color.rgb = ACCENT
box.line.color.rgb = ACCENT  # match line to fill
tf = box.text_frame
tf.text = "Key takeaway"
for p in tf.paragraphs:
    for r in p.runs:
        r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        r.font.size = Pt(20)
        r.font.bold = True
```

---

## Mapping persona.identity.visual_style

When the persona has `identity.visual_style` set (Spec 15), the field
is a free-form string like:

- `"warm earth tones, serif headings, conversational register"`
- `"minimalist monochrome, Helvetica, executive register"`
- `"Norwegian state aesthetic, deep navy + gold accent, formal"`

The skill body says *"prefer those aesthetic hints over generic
defaults."* The mapping is interpretive; there is no schema. Read the
hint, pick reasonable concrete values:

| Hint phrase | Title font | Body font | Accent | Notes |
|---|---|---|---|---|
| "minimalist", "executive", "clean" | Helvetica / Arial | Helvetica / Arial | one cool accent (navy or teal) | Lighter weights; generous whitespace |
| "warm", "conversational" | Georgia or a humanist sans | Calibri / Open Sans | warm accent (amber, terracotta) | Avoid pure black; use `RGBColor(0x33, 0x29, 0x22)` |
| "formal", "state", "official" | Calibri or Cambria | Calibri | deep navy + gold | Conservative; high contrast |
| "academic", "report" | Cambria / Times | Cambria | one muted accent (slate) | Serif headings; small caps for section dividers |
| (no hint) | Calibri | Calibri | `RGBColor(0x1F, 0x4E, 0x79)` (Office blue) | Office default; safe everywhere |

If the hint mentions a *specific* colour (e.g. "deep navy + gold"), use
those colours literally as the accent. If it's abstract ("warm earth
tones"), pick one concrete colour from that family.

**Voice register propagates to speaker notes.** A "conversational"
persona writes speaker notes in first person ("I'd open by asking
the room..."); a "formal" persona writes them as third-person stage
directions ("Open with a brief overview of...").

---

## Per-run weight and alignment

```python
from pptx.enum.text import PP_ALIGN

# Right-align a footer-style line
p = body_frame.paragraphs[-1]
p.alignment = PP_ALIGN.RIGHT
for r in p.runs:
    r.font.italic = True
    r.font.size = Pt(14)
```

Don't centre-align body text; centre alignment is for titles and
section headers only. Left-align body is the default and the right
choice in 95% of cases.

---

## Master-level edits (last resort)

If you genuinely need the same colour on every slide's title bar
(e.g. corporate branding across a 12-slide deck), edit the master
once rather than every slide:

```python
from pptx.dml.color import RGBColor

master = prs.slide_master
title_ph = master.placeholders[0]  # title placeholder on the master
for p in title_ph.text_frame.paragraphs:
    for r in p.runs:
        r.font.color.rgb = RGBColor(0x1F, 0x2D, 0x3D)
```

The change propagates to every layout that inherits the master's title
placeholder. **Confirm visually after master edits**: some layouts
override the master and your change won't appear there.

---

## What NOT to do

- **Do not import an external `.pptx` template** for v0.1. The
  `Presentation(template_path)` constructor works, but the template
  paths aren't part of the sandbox image and the deck becomes
  template-coupled. Stay on the default master + per-run overrides.
- **Do not mix more than two font families.** One major (titles), one
  minor (body) is the cap. Three families is the amateur tell.
- **Do not set every body run to a custom size.** The master's default
  body size (typically Pt(18) on the default layout) is right for most
  bullets. Override only when the slide intent demands it (e.g. a
  quotation slide where the quote is `Pt(28)`).
- **Do not use `RGBColor(0xFF, 0xFF, 0x00)` as an accent.** Pure
  primaries clash with photos and look like a 1996 deck. Use muted
  tones (`0xC8, 0x1D, 0x25` for red; `0x1F, 0x4E, 0x79` for navy).

---

## Colour palette quick reference

Six tested palettes for the `visual_style` mapping above:

```python
from pptx.dml.color import RGBColor

OFFICE_BLUE   = RGBColor(0x1F, 0x4E, 0x79)
NAVY_DARK     = RGBColor(0x1F, 0x2D, 0x3D)
DEEP_RED      = RGBColor(0xC8, 0x1D, 0x25)
WARM_AMBER    = RGBColor(0xE2, 0x8B, 0x18)
SLATE         = RGBColor(0x46, 0x4C, 0x57)
GOLD_ACCENT   = RGBColor(0xC9, 0xA2, 0x27)
TERRACOTTA    = RGBColor(0xB7, 0x52, 0x36)
TEAL          = RGBColor(0x2C, 0x69, 0x6E)
```

Pick one for the deck's accent; pair with near-black body text
(`RGBColor(0x33, 0x33, 0x33)`) and white background. Don't use more
than one accent per deck unless the persona's `visual_style`
explicitly names a pair.
