"""Shared helpers for small end-to-end sales-fact generation in tests.

Several determinism / invariance guardrails (Phase 0 of the sales-fact
improvement plan, and later phase-acceptance tests) need to run the real
pipeline on a tiny, fast, deterministic dataset and inspect the resulting sales
fact. This module centralises that harness so the individual test files stay
focused on *what* they assert rather than *how* to generate data.

Not collected by pytest (module name does not match ``test_*``).
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import yaml
import pandas as pd
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[1]

# Default small scale. Chosen so the pipeline runs in a few seconds while still
# being multi-chunk-capable and keeping customer discovery active/binding.
DEFAULT_SEED = 1234
DEFAULT_TOTAL_ROWS = 12_000
DEFAULT_CUSTOMERS = 4_000

# Columns unique to the sales fact — used to identify it among packaged parquet.
_SALES_MARKER_COLS = {"UnitPrice", "CustomerKey", "ProductKey"}


def models_config() -> dict:
    """The repo's real models.yaml as a dict."""
    return yaml.safe_load((REPO_ROOT / "models.yaml").read_text(encoding="utf-8"))


def small_config(
    *,
    dims_dir: Path,
    scratch_dir: Path,
    final_dir: Path,
    workers: int,
    chunk_size: int,
    total_rows: int = DEFAULT_TOTAL_ROWS,
    customers: int = DEFAULT_CUSTOMERS,
    seed: int = DEFAULT_SEED,
) -> dict:
    """The repo's real config.yaml, patched down to a fast deterministic run.

    Basing on the shipped config (rather than a hand-built minimal one) keeps all
    the required nested defaults present. Secondary/optional facts are disabled to
    keep the run fast and focused on the sales fact.
    """
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["scale"]["sales_rows"] = total_rows
    cfg["scale"]["customers"] = customers
    cfg["scale"]["stores"] = 4
    cfg["scale"]["products"] = {"catalog": "contoso", "rows": 120}
    cfg["defaults"]["seed"] = seed
    cfg["defaults"]["dates"] = {"start": "2022-01-01", "end": "2023-12-31"}
    cfg["defaults"]["final_output"] = str(final_dir)

    sales = cfg["sales"]
    sales["file_format"] = "parquet"
    sales["sales_output"] = "sales"
    sales["skip_order_cols"] = False   # keep OrderNumber/OrderLineNumber as a row key
    sales["quality_report"] = False
    sales["parquet_folder"] = str(dims_dir)
    sales["out_folder"] = str(scratch_dir)
    sales.setdefault("advanced", {})
    sales["advanced"]["chunk_size"] = chunk_size
    sales["advanced"]["workers"] = workers

    for section in ("returns", "budget", "inventory", "subscriptions",
                    "wishlists", "complaints"):
        cfg.setdefault(section, {})["enabled"] = False
    return cfg


def write_configs(work_dir: Path, cfg: dict) -> tuple[str, str]:
    """Write config.yaml + models.yaml into *work_dir*; return their paths."""
    cfg_path = work_dir / "config.yaml"
    models_path = work_dir / "models.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    models_path.write_text(yaml.safe_dump(models_config(), sort_keys=False), encoding="utf-8")
    return str(cfg_path), str(models_path)


def run_pipeline_stage(work_dir: Path, cfg: dict, only: str) -> None:
    """Write configs and run one pipeline stage (``"dimensions"`` | ``"sales"``)."""
    from src.engine.runners.pipeline_runner import run_pipeline

    cfg_path, models_path = write_configs(work_dir, cfg)
    run_pipeline(config_path=cfg_path, models_config_path=models_path, only=only)


def load_sales(*roots: Path) -> pd.DataFrame:
    """Concatenate every sales-fact parquet found under *roots*.

    Identified by carrying all of UnitPrice/CustomerKey/ProductKey, so packaged
    dimensions are never mistaken for the fact. Rows are returned unsorted;
    callers sort as needed.
    """
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.parquet"):
            try:
                cols = set(pq.read_schema(path).names)
            except Exception:
                continue
            if _SALES_MARKER_COLS <= cols:
                files.append(path)
    assert files, f"no sales parquet found under {[str(r) for r in roots]}"
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def sales_digest(df: pd.DataFrame) -> str:
    """Canonical SHA-256 of the sales fact.

    Rows are sorted by (OrderNumber, OrderLineNumber) — a unique per-line key
    independent of customer identity — so any change in *which* customer/price/etc.
    an order carries surfaces as a digest difference.
    """
    sort_cols = [c for c in ("OrderNumber", "OrderLineNumber") if c in df.columns]
    ordered = df.sort_values(sort_cols or list(df.columns)).reset_index(drop=True)
    row_hashes = pd.util.hash_pandas_object(ordered, index=False).values
    return hashlib.sha256(row_hashes.tobytes()).hexdigest()


def per_month_distinct_customers(df: pd.DataFrame) -> pd.Series:
    """Distinct CustomerKey count per calendar month of OrderDate.

    Indexed by ``YYYY-MM`` string and sorted, so two runs' curves compare directly.
    """
    months = pd.to_datetime(df["OrderDate"].astype(str), errors="coerce").dt.to_period("M").astype(str)
    return df.groupby(months)["CustomerKey"].nunique().sort_index()
