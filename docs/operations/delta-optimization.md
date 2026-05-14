# Delta Lake Optimization

Compact small Delta Lake files into fewer, larger ones, and clean up unreferenced data files. Useful after `deltaparquet` runs where partitioned fact tables (Sales, InventorySnapshot) produce many small files from parallel worker writes.

The script runs two operations per qualifying Delta table:
- **`OPTIMIZE`** — coalesces small files into target-sized ones (compaction)
- **`VACUUM`** — removes files no longer referenced by the Delta transaction log

Tables with `--min-files` or fewer files are skipped automatically — that excludes most dimensions and small fact tables, where compaction wouldn't help.

---

## When to use

- After any `deltaparquet` run with high worker count, where Sales/Inventory partitions ended up with hundreds of tiny files
- Before publishing the dataset to a downstream system that scans by partition
- After deleting/overwriting data through Spark, to reclaim disk

**Do not run this if:**
- You just want to change the partition layout → use [delta-repartitioning.md](./delta-repartitioning.md) instead
- You only want to recompress without consolidating → use Spark `OPTIMIZE` with file-size hints directly

---

## Quick recipes

### Default compaction (256 MB target, skip tables ≤ 5 files)
```powershell
python scripts/optimize_delta.py `
  "generated_datasets\2026-03-29 07_11_29 PM Customers 43K Sales 1M DELTAPARQUET"
```

### Compact more aggressively (smaller skip threshold, smaller target)
```powershell
python scripts/optimize_delta.py `
  "generated_datasets\<your-run-folder>" `
  --target-size 128 `
  --min-files 2
```

### Limit concurrent tasks on a low-memory box
```powershell
python scripts/optimize_delta.py `
  "generated_datasets\<your-run-folder>" `
  --max-tasks 2
```

### Compact everything regardless of file count
```powershell
python scripts/optimize_delta.py `
  "generated_datasets\<your-run-folder>" `
  --min-files 1
```
Forces compaction even for small tables. Rarely useful — mostly for benchmarking.

### Large fact tables (100M+ rows): bigger target files
```powershell
python scripts/optimize_delta.py `
  "generated_datasets\<your-run-folder>" `
  --target-size 1024 `
  --min-files 10
```
1 GB target files reduce total file count more aggressively for very large datasets.

### Repeated iteration runs (overwrote same folder)
```powershell
python scripts/optimize_delta.py `
  "generated_datasets\<your-run-folder>" `
  --target-size 256
```
Disk usage from old superseded files will drop after `VACUUM`.

### Combine with repartitioning (full Delta cleanup chain)
```powershell
# 1. Change layout
python scripts/repartition_delta.py "generated_datasets\<run>" --partition-by year

# 2. Compact within the new layout
python scripts/optimize_delta.py "generated_datasets\<run>" --target-size 256
```

### Conservative compaction on a shared machine
```powershell
python scripts/optimize_delta.py `
  "generated_datasets\<your-run-folder>" `
  --max-tasks 1 `
  --target-size 128
```
Single-threaded, smaller targets — minimizes peak resource use.

---

## Full flag reference

| Flag | Description | Default |
|---|---|---|
| `--min-files N` | Skip tables with N or fewer files | `5` |
| `--target-size MB` | Target file size after compaction in MB | `256` |
| `--max-tasks N` | Max concurrent compaction tasks | CPU count |

---

## What `OPTIMIZE` and `VACUUM` actually do

**`OPTIMIZE`** rewrites the data files in a Delta table so that small files are merged into files closer to `--target-size`. The transaction log gets a new commit entry; old files remain on disk but are no longer referenced.

**`VACUUM`** then removes those unreferenced files. The script uses a zero-day retention for vacuum because no readers should be holding references to the just-replaced files (generation is single-writer).

If you're running other workloads against the same Delta table, vacuum's zero-day retention is unsafe. This script is intended for post-generation cleanup only.

---

## Partition tuning at generation time

Inventory partitioning is controlled by `inventory.partition_by` in `config.yaml`:

| Setting | Result | When to use |
|---|---|---|
| `["Year"]` (default) | One partition per year | Most analytical queries |
| `["Year", "Month"]` | Finer pruning | Heavy month-filter queries; accepts more files |
| `null` | No partitioning | Small tables where partition overhead exceeds the benefit |

Sales is always partitioned by `Year` and `Month` in Delta mode — this is hardcoded because Sales is too large to scan unpartitioned.

To change the layout of an **already-generated** dataset without regenerating, see [delta-repartitioning.md](./delta-repartitioning.md).

---

## Recipes by scenario

### Just ran 100M sales with 16 workers, lots of small files
```powershell
python scripts/optimize_delta.py "generated_datasets\<run>" --target-size 512
```
Higher target reduces total file count more aggressively.

### Want to publish dataset to ADLS / S3
Run optimize first locally — fewer files means cheaper and faster upload, and better downstream query performance.

### Disk space low after several iterations
The `VACUUM` step alone can reclaim significant space if you've been re-running with the same output folder.

---

## Troubleshooting

**Some tables show "skipped (only N files)" in the log**
That's expected. Tables under `--min-files` aren't worth compacting. Lower the threshold if you really want everything compacted.

**`OPTIMIZE` fails with a deltalake version error**
The script requires `deltalake >= 0.15`. Check `pip show deltalake`.

**Disk usage didn't drop after running**
`VACUUM` only removes files older than the retention threshold. If the run is very recent, files may be retained briefly. Re-run after a minute.
