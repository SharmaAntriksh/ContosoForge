# Parquet Optimization

Re-compress and re-row-group existing Parquet output **without regenerating data**. Useful for tuning file size, compression codec, or row-group layout after a run completes.

The script reads each `.parquet` file in the dataset folder, rewrites it with the new settings, and either writes alongside the original or overwrites in place.

---

## When to use

- You generated with the default codec and want smaller files on disk → re-compress with `zstd -l 9`
- You'll be loading into Power BI or Spark and want larger row groups for scan efficiency → bump `--row-group-size`
- You want to test the trade-off of codecs before committing to a large run → use `--dry-run`
- You generated Delta Lake output and want to recompress the data files → use [delta-repartitioning.md](./delta-repartitioning.md) instead, which handles Delta transaction logs correctly

---

## Quick recipes

### Re-compress an existing dataset to zstd level 9, in place
```powershell
python scripts/optimize_parquet.py `
  ".\generated_datasets\2026-03-28 01_03_23 PM Customers 89K Sales 21M PARQUET" `
  -c zstd `
  -l 9 `
  --in-place
```

### Switch from zstd to snappy (faster reads, larger files)
```powershell
python scripts/optimize_parquet.py `
  ".\generated_datasets\<your-run-folder>" `
  -c snappy
```

### Increase row-group size for analytical scans
```powershell
python scripts/optimize_parquet.py `
  ".\generated_datasets\<your-run-folder>" `
  -r 2_000_000
```

### Preview without writing
```powershell
python scripts/optimize_parquet.py ".\generated_datasets\<your-run-folder>" -c zstd --dry-run
```

### Maximum compression for cold storage
```powershell
python scripts/optimize_parquet.py `
  ".\generated_datasets\<your-run-folder>" `
  -c zstd `
  -l 22 `
  --in-place
```
Level 22 is the slowest zstd setting but gives the smallest files. Only use for archival.

### Decompress entirely (for benchmarking baseline)
```powershell
python scripts/optimize_parquet.py `
  ".\generated_datasets\<your-run-folder>" `
  -c none
```
Useful for comparing scan performance against compressed variants.

### Gzip for legacy interop
```powershell
python scripts/optimize_parquet.py `
  ".\generated_datasets\<your-run-folder>" `
  -c gzip `
  -l 6
```
For tools that don't support zstd (older Hadoop, some Hive deployments).

### Compare codecs before committing
```powershell
# Side-by-side: run with each codec into separate folders, then compare folder sizes
python scripts/optimize_parquet.py ".\generated_datasets\<run>" -c snappy
python scripts/optimize_parquet.py ".\generated_datasets\<run>" -c zstd -l 3
python scripts/optimize_parquet.py ".\generated_datasets\<run>" -c zstd -l 9
python scripts/optimize_parquet.py ".\generated_datasets\<run>" -c zstd -l 22

# Each writes to a sibling folder named with the codec/level
```

### Tune row groups for Power BI VertiPaq import
```powershell
python scripts/optimize_parquet.py `
  ".\generated_datasets\<your-run-folder>" `
  -c zstd `
  -l 9 `
  -r 5000000 `
  --in-place
```
Larger row groups reduce metadata overhead during VertiPaq's columnar encoding.

### Tune row groups for streaming / low-memory reads
```powershell
python scripts/optimize_parquet.py `
  ".\generated_datasets\<your-run-folder>" `
  -c snappy `
  -r 200000
```
Smaller row groups let memory-constrained readers materialize less at once.

---

## Full flag reference

| Flag | Description | Default |
|---|---|---|
| `-c` / `--compression` | Codec: `snappy`, `zstd`, `gzip`, `brotli`, `lz4`, `none` | `zstd` |
| `-l` / `--level` | Compression level (codec-dependent: zstd 1-22, gzip 1-9, brotli 1-11) | codec default |
| `-r` / `--row-group-size` | Target rows per row group | `1,000,000` |
| `--in-place` | Overwrite originals instead of writing to a new folder | off |
| `--dry-run` | Show what would be done without writing | off |

When `--in-place` is omitted, output goes to a sibling folder with the codec and level appended to the name.

---

## Choosing a codec

| Codec | Speed | Ratio | When to use |
|---|---|---|---|
| `snappy` | Fastest | Modest | Default for analytical engines (Spark, DuckDB). Optimized for read throughput. |
| `zstd` | Fast (level 3), slow at high levels | Best | Default. Excellent compression with reasonable speed. Level 9 is the sweet spot for cold storage. |
| `gzip` | Slow | Good | Legacy compatibility. Not recommended for new data. |
| `brotli` | Slow | Very good | Web/CDN scenarios. Rarely used for Parquet. |
| `lz4` | Fastest | Worst | Hot data with strict latency requirements. |
| `none` | N/A | None | Benchmarking only. |

---

## Choosing row-group size

| Row group size | Best for | Trade-off |
|---|---|---|
| 100K – 500K | Wide tables, low-memory readers | More metadata overhead, less compression |
| 1M (default) | General-purpose | Balanced |
| 2M – 5M | Power BI VertiPaq, columnar scans | Higher peak read memory |
| 10M+ | Cold-storage with full-table scans | Poor selective-read performance |

The reader has to materialize an entire row group into memory at minimum. Sizing for downstream readers matters more than write-side considerations.

---

## Troubleshooting

**"MemoryError" during compression**
Lower `--row-group-size`. Compression buffers each row group fully before writing.

**Files grew after re-compression**
Check the original codec — if it was already zstd at a higher level, switching to default zstd level can produce larger files. Use `-l` to match or exceed the original level.

**`-l` flag rejected for my codec**
Snappy and lz4 don't accept compression levels — they're single-level codecs. Drop `-l` when using them. Levels apply to zstd (1-22), gzip (1-9), brotli (1-11).

**Dataset folder name suggests a different codec than I'm using**
The original folder name reflects the generation-time codec. Re-running this script changes the file contents but doesn't rename the folder unless you also pass `--in-place`. The sibling-folder name will reflect the new codec.
