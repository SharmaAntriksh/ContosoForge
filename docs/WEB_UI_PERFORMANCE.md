# Web UI performance and retention

This page describes the intentional limits used by the local web interface.
They keep long generation runs and large dataset previews predictable without
hiding data-loss or pagination tradeoffs.

## Generation logs

- The server retains the latest 5,000 lines by default.
- Set `WEB_LOG_RETENTION_LINES` before starting the server to change the limit.
  Values below 100 are raised to 100.
- Every retained line has a monotonically increasing sequence number.
- `/api/generate/stream?after=<sequence>` returns only newer retained lines.
- A reconnecting client whose cursor is older than the retained window receives
  a visible `truncated` event before the remaining lines.
- `/api/generate/status` reports retained count, dropped count, and the first and
  last available sequence numbers.
- The browser retains the latest 2,000 displayed lines. A visible notice remains
  at the start of the browser window after older lines are trimmed.

These are preview logs, not an archival logging system. Use the pipeline's file
logs when a complete long-run record is required.

## Dataset preview

Preview requests return 100 rows by default and allow at most 500 rows.

| Format | Pagination behavior |
|---|---|
| Parquet | Row-group metadata skips complete groups. Selected groups are decoded in bounded Arrow batches and sliced before Python conversion. A high offset inside one very large row group must still decode preceding batches, but it does not create a million-row pandas frame. |
| Delta | File fragments are counted and skipped before scanning. A high offset inside the selected fragment still scans earlier batches in that fragment. Physical Delta row order is a preview order, not a guaranteed business sort. |
| CSV | File row counts are cached by modification time, and complete chunk files are skipped. A high offset inside one CSV file still requires parsing preceding records because CSV has no native row index. |

Changing page, table, or dataset aborts the superseded browser request so stale
responses cannot replace the latest selection. The synchronous server read may
finish in its worker thread after a browser abort.

Preview values are formatted for display only; generated files are never
modified. Currency-like fields use two decimal places, other floats use compact
precision without binary artifacts, dates use ISO text, nulls remain explicit,
booleans use `true`/`false`, and integers are transmitted as strings so values
beyond JavaScript's safe integer range remain exact.

## Representative local benchmark

The Phase 6 regression fixture measured 100-row first/last previews on July 29,
2026:

| Format | Fixture | First page | Last page |
|---|---:|---:|---:|
| Parquet | 1,000,000 rows in one row group | 0.0115 s | 0.0788 s |
| CSV | 20,000 rows | 0.0197 s | 0.0093 s |
| Delta | 2,000 rows across two fragments | 0.0286 s | 0.0048 s |

These numbers are regression evidence, not performance guarantees. Storage,
compression, schema width, antivirus scanning, and hardware can change them
substantially.
