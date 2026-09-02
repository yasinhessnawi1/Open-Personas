---
name: data_analysis
description: Analyse an uploaded dataset (CSV/XLSX) and produce a clear finding paired with the right chart, run via the code_execution sandbox.
when_to_use: >
  Use this skill when the user uploads a dataset (CSV, XLSX, TSV) and asks
  an analysis question: what's the trend, what correlates, what does this
  show. Skip for quick factual questions or for analysis you can answer
  from already-loaded context without running code.
tools_required:
  - code_execution
---

# Data Analysis

A procedure for analysing an uploaded dataset and producing a finding
paired with the right chart. The dataset is staged at
`/workspace/in/<filename>` by the runtime. The value is **insight**,
so finish with prose that explains what the chart shows.

## When to use

Activate for: "what's the trend?", "is there a relationship?", "plot
the distribution", "which category performed best?".

Skip for: questions answerable without computation, tiny ad-hoc
calculations, modelling / prediction (out of scope for v0.1).

## The procedure

### Step 1: Load and re-load

On the **first analysis turn**, read the dataset and cache it as parquet
for fast re-load on later turns:

```python
import pandas as pd
from pathlib import Path

Path("intermediate").mkdir(parents=True, exist_ok=True)
df = pd.read_csv("/workspace/in/<dataset>.csv")
df.to_parquet("intermediate/df.parquet")
```

On **every subsequent turn**, re-load from the parquet cache, and DO NOT
re-read the source CSV:

```python
import pandas as pd
df = pd.read_parquet("intermediate/df.parquet")
```

**Why:** the sandbox preserves filesystem state across turns but NOT
Python variables (each turn runs a fresh interpreter). Re-loading the
parquet is millisecond-fast (~15× faster than re-parsing the CSV);
re-reading the source CSV is wasteful and signals you didn't understand
the session model. Use the parameterised path `intermediate/<doc_ref>.parquet`
when more than one dataset is in play.

### Step 2: Profile the data

Before computing anything, understand the data:

```python
print(df.shape, df.dtypes.to_dict(), df.isna().sum().to_dict())
print(f"resident_mb: {df.memory_usage(deep=True).sum() / 1024**2:.1f}")
print(df.head())
```

Note the shape, column types, missing-value counts, and basic ranges.
Use the **resident MB** number in the next step.

### Step 3: Triage by size

The sandbox has a 512 MB memory ceiling. Apply this rule based on the
resident MB from Step 2:

- **< 100 MB**: full analysis on the whole dataset.
- **100 MB ≤ size < 500 MB**: sample to 100,000 rows:
  ```python
  df = df.sample(n=100_000, random_state=0)
  ```
  Tell the user in your finding: "Sampled 100,000 of N rows for analysis
  (~M MB resident); full-data computations available via column-filter
  or row-filter at upload time."
- **≥ 500 MB**: refuse, tell the user the dataset is too large
  (~M MB resident vs the 512 MB ceiling), and ask them to pre-filter
  (drop unused columns, restrict to a date range, aggregate upstream)
  and re-upload.

See `supplements/large_datasets.md` for the dtype-aware profile snippet,
sampling helpers, and the full banner copy.

### Step 4: Compute

Pick the right statistic for the question:

- "trend over time" → `df.groupby(pd.Grouper(key="date", freq="M"))[col].mean()`
- "relationship" → `df[[a,b]].corr()` for numeric pairs; cross-tab for
  categorical
- "distribution" → `df[col].describe()` + the histogram in Step 5
- "by category" → `df.groupby("category")[metric].agg(["mean","median","count"])`

Print the numeric finding before charting. The number IS part of the
answer, so don't bury it in the chart.

### Step 5: Choose the right chart

Match the chart family to the question. Wrong chart misleads.

| Question shape | Chart family | matplotlib call |
|---|---|---|
| Trend over time | Line | `ax.plot(x, y)` |
| Distribution of one variable | Histogram | `ax.hist(values, bins=30)` |
| Relationship between two numeric variables | Scatter | `ax.scatter(x, y, alpha=0.5)` |
| Comparison across categories | Horizontal bar | `ax.barh(labels, values)` |
| Multi-series categorical comparison | Grouped bar | `ax.bar(x+offset, y, width)` per series |

**Never a pie chart.** Bar always reads more accurately. Never bar for a
time-series; line carries the continuous-time semantics.

For the full family-by-family judgement with worked snippets, read
`supplements/chart_families.md`.

### Step 6: Render with clarity

A default matplotlib chart is functional but ugly. Apply this floor:

```python
import matplotlib.pyplot as plt
from pathlib import Path

Path("charts").mkdir(parents=True, exist_ok=True)
fig, ax = plt.subplots(figsize=(9, 5), dpi=150)
ax.plot(x, y)
ax.set_title("Monthly sales 2020-2025")
ax.set_xlabel("Month")
ax.set_ylabel("Sales (NOK)")
ax.grid(True, alpha=0.3)
fig.tight_layout()
fig.savefig("charts/sales-trend.png", dpi=150, bbox_inches="tight")
plt.close(fig)
```

Required every time: title, both axes labelled with units, `figsize`
explicit (default is too small), `dpi=150`, `tight_layout()`, `bbox_inches="tight"`.
For axis formatting (thousands separators, percent, currency), legend
discipline, and font sizes, read `supplements/styling.md`.

### Step 7: Explain in prose

Finish with a one-paragraph finding naming the chart's shape and what it
means ("Sales grew 18% YoY 2020-2024 then plateaued; inflection is the
August 2024 launch."). Bare image = incomplete. Bare prose when a chart
was asked = incomplete.

## Workspace layout

Three top-level directories, three meanings:

- **`charts/<name>.png`**: produced charts shown **inline** to the user.
- **`uploads/<name>.<ext>`**: files offered as **download** chips.
- **`intermediate/<name>.parquet`**: cross-turn **cache**, not shown.

`Path("<dir>").mkdir(parents=True, exist_ok=True)` before saving; the
sandbox does not auto-create subdirectories.

## Quality checks before done

- [ ] Profile printed before computing.
- [ ] Triage applied if > 100 MB resident.
- [ ] Chart family matches question shape (Step 5 table).
- [ ] Title + axes labelled with units; `dpi=150`; `tight_layout`.
- [ ] Prose finding accompanies the chart.
- [ ] Chart in `charts/`, not `uploads/`.

## Failure modes

**Wrong chart type.** Bar for time-series, pie for categories, line for
unordered groups. Step 5 is the guard.

**Default matplotlib eyesore.** Tiny fonts, no titles, no labels, ugly
defaults. Step 6's floor is non-negotiable.

**Re-reading the source CSV every turn.** Signals you missed the session
model. Re-load from parquet.

**Computing on a 600 MB dataframe.** OOM risk. Triage first; refuse if
too large.

**Bare image without prose.** Chart is the vehicle; prose is the finding.
Both, not either.
