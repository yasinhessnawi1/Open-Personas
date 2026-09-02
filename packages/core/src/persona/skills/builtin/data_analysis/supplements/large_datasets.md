# Large Dataset Handling

Reference depth for Step 3 of the `data_analysis` SKILL.md. Read this
mid-task when a dataset is large enough that the triage rule activates.

## The profile snippet

Run this immediately after loading. It gives the four numbers the
triage rule needs:

```python
import pandas as pd

shape = df.shape
resident_mb = df.memory_usage(deep=True).sum() / 1024**2
dtype_mix = df.dtypes.value_counts().to_dict()
na_counts = df.isna().sum().to_dict()

print(f"shape: {shape[0]:,} rows × {shape[1]} cols")
print(f"resident_mb: {resident_mb:.1f}")
print(f"dtype_mix: {dtype_mix}")
print(f"na_counts: {na_counts}")
```

**Why `deep=True`.** Without it, pandas reports object columns as a flat
pointer cost (8 bytes per cell). With `deep=True`, it recursively
measures the actual string storage, typically 10-50× more memory than
the flat estimate suggests. A "1 GB" CSV is often 5 GB resident when
loaded naively. The triage rule is calibrated against deep-memory.

## Dtype downcast (cheap wins)

Before triaging, try downcasting. Float and integer columns often hold
overspecified types:

```python
import numpy as np

# Float downcast: float64 → float32 halves memory if precision allows
float_cols = df.select_dtypes(include=["float64"]).columns
df[float_cols] = df[float_cols].astype("float32")

# Integer downcast: int64 → int32/int16 if range fits
int_cols = df.select_dtypes(include=["int64"]).columns
for col in int_cols:
    df[col] = pd.to_numeric(df[col], downcast="integer")

# Object → category for low-cardinality string columns
for col in df.select_dtypes(include=["object"]).columns:
    if df[col].nunique() / len(df) < 0.5:
        df[col] = df[col].astype("category")
```

Re-measure `resident_mb` after downcasting; the rule may now classify
the dataset into a smaller triage tier. Common wins: a 250 MB string-heavy
CSV becomes 60 MB after category conversion → full path instead of
sampling.

## The sampling helper (100-500 MB tier)

When the dataset lands in the sampling tier (between 100 and 500 MB
resident), sample deterministically:

```python
SAMPLE_N = 100_000
df = df.sample(n=SAMPLE_N, random_state=0)

original_n = shape[0]  # from the profile snippet above
banner = (
    f"Sampled {SAMPLE_N:,} of {original_n:,} rows for analysis "
    f"(~{resident_mb:.0f} MB resident); full-data computations available "
    "via column-filter or row-filter at upload time."
)
print(banner)
```

`random_state=0` makes the sample reproducible across turns; turn 2's
analysis sees the same 100,000 rows turn 1 sampled. **Include the banner
in your prose finding** so the user knows the answer is sample-based.

For time-series data where uniform random sampling distorts the trend,
use stratified or systematic sampling instead:

```python
# Every Nth row preserves trend shape
step = original_n // SAMPLE_N
df = df.iloc[::step].reset_index(drop=True)
```

## The refusal copy (≥ 500 MB tier)

When the dataset is too large, refuse explicitly rather than risk an
OOM-kill:

```python
print(
    f"This dataset is ~{resident_mb:.0f} MB resident, too large for "
    f"the analysis sandbox (512 MB ceiling). Please pre-filter (drop "
    "unused columns, restrict to a date range, or aggregate upstream) "
    "and re-upload."
)
```

Then stop: DO NOT proceed to compute or chart. The user sees the
refusal, filters upstream, re-uploads. **Refusal is the correct outcome**;
producing a chart on a near-OOM dataframe is the wrong outcome.

## Chunked reading (when the file is too large to load at all)

If `pd.read_csv("/workspace/in/big.csv")` itself OOMs before you can
profile, read in chunks:

```python
# Profile via chunks
chunk_iter = pd.read_csv("/workspace/in/big.csv", chunksize=100_000)
total_rows = 0
sampled = []
for chunk in chunk_iter:
    total_rows += len(chunk)
    sampled.append(chunk.sample(n=min(len(chunk), 5_000), random_state=0))
df = pd.concat(sampled, ignore_index=True)

print(f"original rows: {total_rows:,}; sampled rows: {len(df):,}")
```

Tell the user this is a chunked-sample summary, not a full-data
computation.

## Categorical dtype memory caveat

`category` dtype is memory-efficient for low-cardinality columns but
becomes a footgun on high-cardinality string columns (~unique-per-row):
the category index becomes as large as the column itself, plus the
codes. Rule of thumb: `category` is a win when `nunique/len < 0.5`. The
downcast block above checks this.
