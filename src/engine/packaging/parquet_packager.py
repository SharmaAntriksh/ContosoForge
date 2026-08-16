from pathlib import Path
from typing import Optional

from src.exceptions import PackagingError
from src.utils.logging_utils import info, done
from src.facts.sales.output_paths import TABLE_SALES

from .paths import table_dir_name
from .artifact_transfer import transfer_file


def resolve_merged_parquet(fact_out: Path, sales_cfg: dict, table: str) -> Optional[Path]:
    """
    Locate merged parquet output in scratch fact_out.

    Layouts supported (scratch has evolved):
      - <fact_out>/parquet/<snake_table>/<snake_table>.parquet   (current order tables)
      - <fact_out>/<snake_table>/<snake_table>.parquet          (older)
      - Sales merged file may sit at <fact_out>/parquet/sales.parquet or <fact_out>/sales.parquet
    """
    parquet_root = fact_out / "parquet"
    tdir = table_dir_name(table)

    if table == TABLE_SALES:
        merged_name = str(sales_cfg.get("merged_file") or "sales.parquet")
        candidates = [
            parquet_root / merged_name,
            parquet_root / tdir / merged_name,
            fact_out / merged_name,
            fact_out / tdir / merged_name,
        ]
    else:
        merged_name = f"{table_dir_name(table)}.parquet"  # snake_case file
        candidates = [
            parquet_root / tdir / merged_name,
            parquet_root / table / merged_name,  # fallback if CamelCase dir
            parquet_root / merged_name,          # fallback if written to root
            fact_out / tdir / merged_name,
            fact_out / table / merged_name,
            fact_out / merged_name,
            # legacy fallback: older runs may still have CamelCase filename
            parquet_root / tdir / f"{table}.parquet",
            fact_out / tdir / f"{table}.parquet",
        ]

    for p in candidates:
        if p.exists() and p.is_file():
            return p
    return None


def _find_chunk_parquets(
    fact_out: Path, table: str, manifest_out_dir: Optional[Path] = None
) -> list[Path]:
    """Find unmerged chunk parquet files for a table when merge is disabled.

    Prefers the manifest-declared scratch dir (authoritative — copies from exactly
    where the writer put the chunks, no candidate probing); falls back to the
    legacy candidate roots only when no manifest dir was threaded in.
    """
    snake = table_dir_name(table)
    prefix = f"{snake}_chunk"

    if manifest_out_dir is not None:
        return sorted(manifest_out_dir.glob(f"{prefix}*.parquet"))

    if table == TABLE_SALES:
        # Uniform layout puts Sales chunks in fact_out/parquet/sales; older layouts
        # rooted them at fact_out or fact_out/parquet.
        roots = [fact_out / "parquet" / snake, fact_out, fact_out / "parquet"]
    else:
        # Other tables: chunks live in fact_out/<subdir>/ or fact_out/parquet/<subdir>/
        roots = [fact_out / snake, fact_out / "parquet" / snake, fact_out, fact_out / "parquet"]
    for root in roots:
        chunks = sorted(root.glob(f"{prefix}*.parquet"))
        if chunks:
            return chunks
    return []


def _manifest_out_dir(manifest, table: str) -> Optional[Path]:
    """The scratch dir the run manifest declared for ``table``'s parquet chunks.

    Authoritative: returns the declared dir only if it exists, so a merge-disabled
    run copies chunks from exactly where the writer put them (never a stale sibling
    from a prior run at a different layout).
    """
    if manifest is None:
        return None
    entry = getattr(manifest, "tables", {}).get(table)
    out_dir = getattr(entry, "out_dir", None) if entry is not None else None
    if out_dir:
        p = Path(out_dir)
        if p.is_dir():
            return p
    return None


def _manifest_merged_parquet(manifest, table: str) -> Optional[Path]:
    """The merged-parquet path the run manifest declared for ``table``.

    Authoritative: returns the declared path only if a file actually sits there.
    A merge-disabled run declares the would-be path but writes no file, so this
    returns None and the caller falls back to the chunk copy. Unlike
    ``resolve_merged_parquet`` this never probes sibling candidates, so a stale
    merged file from a previous run at a different layout can't be shipped.
    """
    if manifest is None:
        return None
    entry = getattr(manifest, "tables", {}).get(table)
    mp = getattr(entry, "merged_path", None) if entry is not None else None
    if mp:
        p = Path(mp)
        if p.is_file():
            return p
    return None


def copy_parquet_facts(*, fact_out: Path, facts_out: Path, sales_cfg: dict,
                       tables: list[str], manifest=None, promote: bool = False) -> None:
    copied = 0
    promoted = 0
    missing: list[str] = []

    for t in tables:
        # Prefer the run manifest's declared merged path (no candidate probing);
        # fall back to the legacy probe only when no manifest was threaded in.
        if manifest is not None:
            src = _manifest_merged_parquet(manifest, t)
        else:
            src = resolve_merged_parquet(fact_out, sales_cfg, t)
        if src is not None:
            # Merged file: copy directly into facts_out (no subdirectory)
            dst = facts_out / src.name
            if dst.exists():
                dst.unlink()
            promoted += int(transfer_file(src, dst, promote=promote))
            copied += 1
            continue

        # No merged file — copy unmerged chunks into a subdirectory. Prefer the
        # manifest's declared scratch dir; probe legacy roots only without one.
        chunks = _find_chunk_parquets(fact_out, t, _manifest_out_dir(manifest, t))
        if chunks:
            dst_dir = facts_out / table_dir_name(t)
            dst_dir.mkdir(parents=True, exist_ok=True)
            for chunk in chunks:
                promoted += int(
                    transfer_file(chunk, dst_dir / chunk.name, promote=promote)
                )
            copied += 1
            info(f"Copied {len(chunks)} unmerged chunk(s) for {t} -> {table_dir_name(t)}/")
            continue

        missing.append(t)
        info(f"No parquet found for {t}. Checked: {fact_out / 'parquet'} and {fact_out}.")

    if missing:
        raise PackagingError(
            "No merged parquet found for table(s): "
            + ", ".join(missing)
            + f". Scratch roots checked: {fact_out} and {fact_out / 'parquet'}."
        )

    action = f"; promoted {promoted} artifact(s)" if promote else ""
    done(f"Packaged {copied} parquet fact table(s){action}.")
