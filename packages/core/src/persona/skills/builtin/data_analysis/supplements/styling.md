# Chart Styling Craft

Reference depth for Step 6 of the `data_analysis` SKILL.md. Read this
mid-task when a chart needs more than the body's minimum floor.

## Figure size by chart family

The matplotlib default 6.4 × 4.8 inches is too small for inline display.
Pick by family:

- **Time-series line**: `figsize=(10, 5)` (wide; date axis benefits from
  width)
- **Histogram**: `figsize=(8, 5)`
- **Scatter**: `figsize=(8, 6)` (closer to square so the relationship
  reads symmetrically)
- **Horizontal bar**: `figsize=(8, max(4, 0.4*n))` (height scales with
  the number of categories `n`; do not crush 30 labels into 4 inches)
- **Grouped bar**: `figsize=(10, 5)`

Always set `dpi=150` on both `subplots(...)` and `savefig(...)`. The
display rendering is the saved file; consistency matters.

## Title + axis labels (mandatory)

```python
ax.set_title("Monthly sales 2020-2025")
ax.set_xlabel("Month")
ax.set_ylabel("Sales (NOK)")
```

Title is a sentence-like description, not a generic noun. The axis
labels carry units in parentheses when applicable: "Sales (NOK)", "Age
(years)", "Latency (ms)". No units when the unit is implicit ("Number
of customers"). Title font is 12pt by default; bump to 14pt with
`ax.set_title("...", fontsize=14)` for inline display.

## Tick formatting

Raw floats on axes are illegible. Use `FuncFormatter` for thousands,
percent, and currency:

```python
from matplotlib.ticker import FuncFormatter

# Thousands separator
ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:,.0f}"))

# Percent (0-1 range)
ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:.0%}"))

# Currency
ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"NOK {x:,.0f}"))
```

For date axes, matplotlib's autolocator usually picks reasonable ticks.
If they collide, rotate: `fig.autofmt_xdate(rotation=30)`.

## Grid + spines

A subtle grid helps readability without dominating:

```python
ax.grid(True, alpha=0.3, linestyle="--", linewidth=0.5)
```

Remove the top + right spines for a cleaner look (optional but common):

```python
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
```

Keep the bottom + left spines; they're the axis lines the eye follows.

## Legend discipline

Only show a legend when there's more than one series. Position it inside
the axes when there's whitespace; outside when the data fills the frame:

```python
ax.legend(loc="upper left", frameon=False)
# Or, outside:
ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1.0), borderaxespad=0)
```

`frameon=False` removes the legend box border, which is cleaner against the
white background. Series labels go on the `ax.plot(..., label="...")`
call; without labels, `ax.legend()` is a no-op.

## Colour discipline

Default matplotlib colours are fine for v0.1. Two rules:

- **Single series → one consistent colour**, not the default rotation
  (which can produce orange for what should obviously be blue):
  ```python
  ax.plot(x, y, color="#1f77b4")  # matplotlib's default blue
  ```
- **Multi-series → at most 6 series** in one chart. Beyond 6, the legend
  becomes a key the reader cross-references constantly; split into
  small multiples or filter to the top-N series instead.

For categorical comparison where a single colour is enough, use a muted
hue:

```python
ax.barh(labels, values, color="#4c72b0")
```

## Saving

The final two lines, every time:

```python
fig.tight_layout()
fig.savefig("charts/<descriptive-name>.png", dpi=150, bbox_inches="tight")
plt.close(fig)
```

`tight_layout()` prevents label clipping. `bbox_inches="tight"` trims
whitespace around the figure boundary. `plt.close(fig)` releases the
figure from matplotlib's pyplot cache, which is important when running many
charts in one session (memory accumulates otherwise).

Filename is descriptive, lowercase, with hyphens: `sales-trend.png`,
`age-distribution.png`, `region-comparison.png`. NOT `chart.png`,
`plot1.png`, `figure_2.png`.

## Common eyesores to avoid

- **Default 6.4 × 4.8 figsize.** Too small for inline display; charts
  feel cramped.
- **No title.** The chart can't stand alone; readers ask "what is this?"
- **Both axes unlabelled.** Numbers without units are noise.
- **Default tick labels on time axes.** `2020-01-01 00:00:00` is ugly;
  use date-aware formatters or rotate.
- **Legend on top of the data.** Move it out, frame it off, or kill it
  if there's only one series.
- **10+ colours in a categorical chart.** Cognitive overload; split or
  filter.
- **Pie charts.** Bar always reads more accurately. Resist.
