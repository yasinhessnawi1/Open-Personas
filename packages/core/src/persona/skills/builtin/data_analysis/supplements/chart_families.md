# Chart Family Reference

Reference depth for Step 5 of the `data_analysis` SKILL.md. Read this
when the question's chart family is ambiguous or when you need the
matplotlib pattern for a specific family.

## Question → family decision

The question's shape determines the chart family. Apply this order:

1. **"How did X change over time?"** → time-series line.
2. **"What's the distribution of X?"** → histogram.
3. **"Is there a relationship between X and Y?"** → scatter.
4. **"Compare X across categories"** → horizontal bar.
5. **"Compare X across categories AND across a second dimension"** →
   grouped bar OR small multiples.
6. **"Composition / share of a total"** → stacked bar (NOT pie).

Anything else (sankey, heatmap, violin, box) is a v0.2 consideration.

---

## Time-series line

**When:** the x-axis is time (date / month / year / timestamp).

```python
import matplotlib.pyplot as plt
import pandas as pd
from pathlib import Path

Path("charts").mkdir(parents=True, exist_ok=True)
monthly = df.groupby(pd.Grouper(key="date", freq="M"))["sales"].sum()

fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
ax.plot(monthly.index, monthly.values, color="#1f77b4", linewidth=1.8)
ax.set_title("Monthly sales 2020-2025")
ax.set_xlabel("Month")
ax.set_ylabel("Sales (NOK)")
ax.grid(True, alpha=0.3)
fig.autofmt_xdate(rotation=30)
fig.tight_layout()
fig.savefig("charts/sales-trend.png", dpi=150, bbox_inches="tight")
plt.close(fig)
```

For multi-series trends (e.g. sales by region over time), one `ax.plot`
per series with a label, then `ax.legend()`. Keep series count ≤ 6.

**Failure mode:** using a bar chart for a time-series. Bars suggest
discrete categories; lines suggest continuous time.

---

## Histogram (distribution)

**When:** the question is about the spread / shape of one numeric
variable.

```python
fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
ax.hist(df["age"].dropna(), bins=30, color="#4c72b0", edgecolor="white")
ax.set_title("Customer age distribution")
ax.set_xlabel("Age (years)")
ax.set_ylabel("Number of customers")
ax.grid(True, alpha=0.3, axis="y")
fig.tight_layout()
fig.savefig("charts/age-distribution.png", dpi=150, bbox_inches="tight")
plt.close(fig)
```

`bins=30` is the default rule-of-thumb; raise to 50 for high-resolution
distributions, drop to 10-15 for noisy small samples. `edgecolor="white"`
gives a clean look that reads better than no edge.

**Failure mode:** showing a density plot when you should show a histogram.
For non-technical readers, histogram bars are easier to read.

---

## Scatter (relationship)

**When:** the question is "do X and Y move together?".

```python
fig, ax = plt.subplots(figsize=(8, 6), dpi=150)
ax.scatter(df["height_cm"], df["weight_kg"], alpha=0.5, s=15, color="#4c72b0")
ax.set_title("Height vs weight (n=500)")
ax.set_xlabel("Height (cm)")
ax.set_ylabel("Weight (kg)")
ax.grid(True, alpha=0.3)
fig.tight_layout()
fig.savefig("charts/height-weight.png", dpi=150, bbox_inches="tight")
plt.close(fig)

corr = df[["height_cm", "weight_kg"]].corr().iloc[0, 1]
print(f"Pearson correlation: {corr:.2f}")
```

`alpha=0.5` mitigates overplotting when there are >300 points; `s=15`
keeps the markers small enough to read. Drop a regression line via
`np.polyfit` + `ax.plot` if it adds insight.

**Failure mode:** using a line plot to show a relationship. Lines
connect adjacent points in some order; scatter has no order, and the
relationship shape is what matters.

---

## Horizontal bar (categorical comparison)

**When:** comparing one metric across a small number of categories.

```python
agg = df.groupby("region")["sales"].sum().sort_values()
fig, ax = plt.subplots(figsize=(8, max(4, 0.4 * len(agg))), dpi=150)
ax.barh(agg.index, agg.values, color="#4c72b0")
ax.set_title("Total sales by region 2025")
ax.set_xlabel("Sales (NOK)")
ax.grid(True, alpha=0.3, axis="x")
fig.tight_layout()
fig.savefig("charts/region-comparison.png", dpi=150, bbox_inches="tight")
plt.close(fig)
```

**Horizontal, not vertical**: category labels read more naturally
along the y-axis. `sort_values()` orders bars by magnitude (the eye
catches the pattern faster than alphabetical order). `figsize` height
scales with the number of categories.

**Failure mode:** vertical bars with rotated labels. Rotation kills
readability; horizontal bars solve it for free.

---

## Grouped bar (multi-series categorical)

**When:** comparing one metric across categories AND across a second
dimension (e.g. quarter × region).

```python
import numpy as np

pivot = df.pivot_table(index="region", columns="quarter", values="sales", aggfunc="sum")
x = np.arange(len(pivot.index))
width = 0.8 / len(pivot.columns)

fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
for i, col in enumerate(pivot.columns):
    ax.bar(x + i * width, pivot[col], width, label=col)
ax.set_xticks(x + width * (len(pivot.columns) - 1) / 2)
ax.set_xticklabels(pivot.index)
ax.set_title("Sales by region and quarter")
ax.set_xlabel("Region")
ax.set_ylabel("Sales (NOK)")
ax.legend(title="Quarter", frameon=False)
ax.grid(True, alpha=0.3, axis="y")
fig.tight_layout()
fig.savefig("charts/region-quarter.png", dpi=150, bbox_inches="tight")
plt.close(fig)
```

For >3 series, switch to small multiples (one chart per series,
arranged in a grid); grouped bars become hard to read above 3 series.

---

## Composition (stacked bar, NEVER pie)

**When:** showing how a total breaks down into parts across one
categorical dimension.

```python
pivot = df.pivot_table(index="region", columns="category", values="sales", aggfunc="sum")
fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
pivot.plot.bar(stacked=True, ax=ax, edgecolor="white")
ax.set_title("Sales composition by region")
ax.set_ylabel("Sales (NOK)")
ax.legend(title="Category", frameon=False, bbox_to_anchor=(1.02, 1), borderaxespad=0)
fig.tight_layout()
fig.savefig("charts/composition.png", dpi=150, bbox_inches="tight")
plt.close(fig)
```

**Never a pie.** Bar (stacked or grouped) always reads more accurately
than pie; humans estimate length better than area. The only place pie
arguably works is "two slices of dramatically unequal size", and even
there a single number ("82%") is clearer.
