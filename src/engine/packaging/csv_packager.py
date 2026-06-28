import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from src.utils.logging_utils import info, done
from src.facts.sales.output_paths import TABLE_SALES

from .paths import table_dir_name


def resolve_csv_table_dir(fact_out: Path, table: str) -> Optional[Path]:
    """
    Locate CSV chunk folder for a table.
    Accepts:
      - <fact_out>/csv/<snake_table>/
      - <fact_out>/csv/<TableName>/
      - <fact_out>/csv/ (Sales may be rooted)
    """
    csv_root = fact_out / "csv"
    if not csv_root.exists():
        return None

    tdir = table_dir_name(table)

    if table == TABLE_SALES:
        candidates = [csv_root / tdir, csv_root / table, csv_root]
    else:
        candidates = [csv_root / tdir, csv_root / table, csv_root / table.lower()]

    for p in candidates:
        if p.exists() and p.is_dir():
            return p
    return None


def copy_csv_facts(*, fact_out: Path, facts_out: Path, tables: list[str]) -> None:
    """
    Copy CSV fact chunk files into the packaged facts folder.

    Layout rules:
      - Multi-table (len(tables) > 1):
          facts/<table_dir_name(table)>/*.csv  for every table (including Sales)
      - Sales-only (tables == ["sales"]):
          facts/sales/*.csv   (NOT facts/*.csv)

    Logging:
      - 1 INFO line with totals + per-table counts
      - 1 DONE line with total copied
    """
    copied_files = 0
    missing_dirs: list[str] = []

    multi_table = len(tables) > 1
    force_sales_subdir = (len(tables) == 1 and tables[0] == TABLE_SALES)

    # Build plan first so we can log once
    plan: list[tuple[str, Path, list[Path], Path]] = []  # (table, src_dir, files, dst_dir)

    for t in tables:
        src_dir = resolve_csv_table_dir(fact_out, t)
        if src_dir is None:
            missing_dirs.append(t)
            continue

        if t == TABLE_SALES:
            # Key change: when sales-only, keep under facts/sales instead of facts/
            dst_dir = (facts_out / table_dir_name(t)) if (multi_table or force_sales_subdir) else facts_out
        else:
            dst_dir = facts_out / table_dir_name(t)

        csv_files = sorted(src_dir.glob("*.csv"))
        plan.append((t, src_dir, csv_files, dst_dir))

    if missing_dirs:
        raise RuntimeError(
            "No CSV fact outputs found for table(s): "
            + ", ".join(missing_dirs)
            + f". Expected under: {fact_out / 'csv'}"
        )

    # Concise summary log
    short = {
        "sales": "sales",
        "order_detail": "detail",
        "order_header": "header",
        "returns": "return",
    }
    counts = [(short.get(table_dir_name(t), table_dir_name(t)), len(files)) for (t, _src, files, _dst) in plan]
    total = sum(n for _name, n in counts)

    if counts and len({n for _name, n in counts}) == 1:
        n = counts[0][1]
        info(f"Copy CSV facts: {total} files ({n} each) -> " + ", ".join(name for name, _ in counts))
    else:
        info("Copy CSV facts: " + ", ".join(f"{name}={n}" for name, n in counts) + f" (total={total})")

    # Pre-validate: check for duplicates before copying
    copy_pairs: list[tuple[Path, Path]] = []
    seen_targets: set[str] = set()
    for _t, _src_dir, csv_files, dst_dir in plan:
        dst_dir.mkdir(parents=True, exist_ok=True)
        for f in csv_files:
            target = dst_dir / f.name
            key = str(target)
            if key in seen_targets or target.exists():
                raise RuntimeError(f"Duplicate CSV filename during packaging: {target}")
            seen_targets.add(key)
            copy_pairs.append((f, target))

    # Parallel copy (IO-bound, benefits from threading)
    def _copy(pair: tuple[Path, Path]) -> None:
        shutil.copy2(pair[0], pair[1])

    if len(copy_pairs) > 4:
        with ThreadPoolExecutor(max_workers=min(8, len(copy_pairs))) as pool:
            list(pool.map(_copy, copy_pairs))
    else:
        for pair in copy_pairs:
            _copy(pair)

    done(f"Copied {len(copy_pairs)} CSV fact file(s).")