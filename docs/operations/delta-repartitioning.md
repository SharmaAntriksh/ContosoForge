# Delta Lake Repartitioning

Change the partition layout of an **already-generated** Delta Lake dataset without regenerating from scratch. Useful for switching between `Year`, `Year+Month`, or unpartitioned layouts after a run, or for recompressing while repartitioning in one pass.

The script streams each partitioned table partition-by-partition, so memory stays bounded even for large fact tables. Unpartitioned tables (dimensions, small facts) are skipped automatically.

---

## When to use

- You generated with the wrong partition granularity and don't want to wait for a full regen
- You want to recompress all Parquet files inside a Delta table without re-running the pipeline
- You're consolidating multiple small partitions into a single unpartitioned table for downstream tools that don't benefit from pruning

**Do not run this if:**
- You only want to consolidate small files within the *existing* partition layout → use [delta-optimization.md](./delta-optimization.md) instead

---

## Quick recipes

### Demote Year+Month → Year (fewer, larger files)
```powershell
python scripts/repartition_delta.py `
  "generated_datasets\2026-03-29 07_11_29 PM Customers 43K Sales 1M DELTAPARQUET" `
  --partition-by year
```

### Promote Year → Year+Month (finer pruning)
```powershell
python scripts/repartition_delta.py `
  "generated_datasets\<your-run-folder>" `
  --partition-by year-month
```

### Remove partitions entirely (single consolidated table)
```powershell
python scripts/repartition_delta.py `
  "generated_datasets\<your-run-folder>" `
  --partition-by none
```

### Recompress while repartitioning
```powershell
python scripts/repartition_delta.py `
  "generated_datasets\<your-run-folder>" `
  --partition-by year `
  --compression ZSTD
```

### Switch to uncompressed (for benchmark baselines)
```powershell
python scripts/repartition_delta.py `
  "generated_datasets\<your-run-folder>" `
  --partition-by year `
  --compression UNCOMPRESSED
```

### Use Snappy instead of ZSTD (faster reads)
```powershell
python scripts/repartition_delta.py `
  "generated_datasets\<your-run-folder>" `
  --partition-by year `
  --compression SNAPPY
```

### Keep current partition, only recompress
Not directly supported — `--partition-by` is required. If the table is already at the target layout it's skipped, so this works as a recompress-only pass:
```powershell
# Already partitioned by year? This recompresses without touching layout.
python scripts/repartition_delta.py `
  "generated_datasets\<your-run-folder>" `
  --partition-by year `
  --compression ZSTD
```

### Flatten then optimize (single consolidated table per fact)
```powershell
python scripts/repartition_delta.py "generated_datasets\<run>" --partition-by none
python scripts/optimize_delta.py "generated_datasets\<run>" --target-size 512 --min-files 2
```

### Full Delta tuning pipeline (re-layout + recompress + compact)
```powershell
# 1. Switch to year+month and recompress to zstd
python scripts/repartition_delta.py `
  "generated_datasets\<run>" `
  --partition-by year-month `
  --compression ZSTD

# 2. Compact files inside each new partition
python scripts/optimize_delta.py `
  "generated_datasets\<run>" `
  --target-size 256
```

---

## Full flag reference

| Flag | Description | Required |
|---|---|---|
| `--partition-by` | Target layout: `none`, `year`, or `year-month` | yes |
| `--compression` | Optional recompression codec: `UNCOMPRESSED`, `SNAPPY`, `GZIP`, `BROTLI`, `LZ4`, `ZSTD`, `LZ4_RAW` | no (keep existing) |

Tables already at the requested layout are skipped without modification.

---

## Picking the right layout

| Layout | File count | Best for |
|---|---|---|
| `none` | Smallest count, largest files | Small datasets, or downstream tools without partition awareness |
| `year` | Moderate | Default for multi-year sales. Each year is a partition. |
| `year-month` | Largest count | Heavy month-filter analytical queries on multi-year data |

Rule of thumb: aim for each partition to hold **at least 1 GB** of data after compaction. If `year-month` produces 200 MB partitions, demote to `year`.

---

## What this script does internally

1. Discovers all Delta tables in the dataset folder
2. For each table:
   - Reads the current partition schema from the Delta transaction log
   - If it matches `--partition-by`, skips the table
   - Otherwise: streams rows partition-by-partition, writes a new Delta table with the target schema, then atomically swaps the table directory
3. Optionally recompresses Parquet files during the write

The streaming behavior means peak memory is bounded by a single source partition, not the full table size.

---

## After running this

If you want to also compact small files within the new partition layout, run [delta-optimization.md](./delta-optimization.md) afterwards. The two scripts are complementary:

- **Repartition** changes how rows are split across folders
- **Optimize** consolidates files within whatever folder layout exists

---

## Troubleshooting

**Script reports "no Delta tables found"**
Check the path. The dataset folder must contain a `facts/` subfolder with `_delta_log/` directories inside. If you generated with `--format parquet`, this script doesn't apply.

**Out-of-memory on a single partition**
A single source partition is too large to fit in memory. The current implementation streams between partitions, not within. Workarounds: temporarily lower the row group size in source files (re-run `optimize_parquet.py` first), or regenerate at a smaller scale.

**Table came out empty after switching to `none`**
Check the log for write errors. The atomic swap only happens after a successful write — an empty result usually means a failed write rolled back. The original table should still be intact.
