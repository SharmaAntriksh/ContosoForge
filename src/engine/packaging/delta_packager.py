import shutil
from pathlib import Path
from typing import Optional

from src.utils.logging_utils import info, done
from src.facts.sales.output_paths import TABLE_SALES

from .paths import table_dir_name, to_snake


def looks_like_delta_table_dir(p: Path) -> bool:
    """Delta table root signature: has _delta_log/ (do not require parquet at root)."""
    try:
        return (p / "_delta_log").is_dir()
    except (FileNotFoundError, PermissionError, OSError):
        return False


def find_delta_table_dir(fact_out: Path, sales_cfg: dict, table: str) -> Optional[Path]:
    """
    Find the Delta table directory for `table` across known layouts.

    Known layout (your current runs):
      data/fact_out/sales/
        _delta_log                      (Sales)
        order_detail/_delta_log   (OrderDetail)
        order_header/_delta_log   (OrderHeader)

    Also probes legacy:
      data/fact_out/delta/<table_subdir>/_delta_log
      data/fact_out/deltaparquet/<table_subdir>/_delta_log
    """
    snake = to_snake(table)

    roots: list[Path] = []
    cfg_root = getattr(sales_cfg, "delta_output_folder", None)
    if cfg_root:
        roots.append(Path(cfg_root))

    roots += [
        fact_out / "delta",
        fact_out / "deltaparquet",
        fact_out / "sales",
    ]

    # De-dup roots
    seen = set()
    uniq_roots: list[Path] = []
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            uniq_roots.append(r)

    candidates: list[Path] = []
    for root in uniq_roots:
        # root itself only if it *looks like* it was intended for this table
        if root.name in {snake, table, table.lower()}:
            candidates.append(root)

        candidates += [
            root / snake,          # order_detail
            root / table,          # OrderDetail
            root / table.lower(),  # orderdetail
        ]

    # De-dup candidates
    seen_c = set()
    uniq_candidates: list[Path] = []
    for c in candidates:
        key = str(c)
        if key not in seen_c:
            seen_c.add(key)
            uniq_candidates.append(c)

    for cand in uniq_candidates:
        if looks_like_delta_table_dir(cand):
            return cand

    return None


def copy_delta_table_dir(src: Path, dst: Path, skip_dirnames: set[str]) -> None:
    """Copy a delta table directory snapshot, skipping internal scratch dirs."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    for item in src.iterdir():
        if item.name in skip_dirnames:
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def copy_delta_facts(*, fact_out: Path, facts_out: Path, sales_cfg: dict, tables: list[str]) -> None:
    copied = 0
    missing: list[str] = []

    for t in tables:
        src_dir = find_delta_table_dir(fact_out, sales_cfg, t)
        if src_dir is None:
            missing.append(t)
            info(
                f"No delta output found for {t}. "
                f"Checked delta_output_folder plus: {fact_out / 'delta'}, {fact_out / 'deltaparquet'}, {fact_out / 'sales'}."
            )
            continue

        dst_dir = facts_out / table_dir_name(t)
        skip_dirs = {"_tmp_parts"}

        # If packaging multiple tables, don't let Sales delta root drag nested tables with it.
        if t == TABLE_SALES and len(tables) > 1:
            skip_dirs |= {table_dir_name(x) for x in tables if x != TABLE_SALES}

        copy_delta_table_dir(src_dir, dst_dir, skip_dirnames=skip_dirs)
        copied += 1

    if missing:
        raise RuntimeError(
            "No delta fact outputs found for table(s): "
            + ", ".join(missing)
            + ". Checked delta_output_folder plus these scratch roots: "
            + ", ".join(
                [
                    str(p)
                    for p in [
                        fact_out / "delta",
                        fact_out / "deltaparquet",
                        fact_out / "sales",
                    ]
                ]
            )
        )

    done(f"Copied {copied} delta table snapshot(s).")
