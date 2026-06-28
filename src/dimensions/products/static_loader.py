from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional, Sequence, Tuple

import pandas as pd

from src.utils import info, skip
from src.utils.output_utils import write_parquet_with_date32
from src.versioning import should_regenerate, save_version


def _file_sha256(path: Path, _chunk: int = 1 << 20) -> str:
    """Content fingerprint of a file, stable across git clone/checkout.

    Used instead of mtime so a checkout that rewrites the source with
    byte-identical content does not trigger a needless regeneration.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_chunk), b""):
            h.update(block)
    return h.hexdigest()


def load_static_dimension(
    name: str,
    src_path: Path,
    output_path: Path,
    *,
    # Date logic (Power Query friendly)
    date_cols: Optional[Sequence[str]] = None,
    cast_all_datetime: bool = False,
    # Parquet options
    compression: str = "snappy",
    compression_level: Optional[int] = None,
    force_date32: bool = True,
    log_skip: bool = True,
) -> Tuple[pd.DataFrame, bool]:
    """
    Load a static reference dimension from parquet and write it to output_path.

    Returns: (DataFrame, regenerated: bool)

    Date logic:
      - Writes selected datetime columns as Arrow date32 when pyarrow is available,
        so Power Query imports them as Date (not DateTime).
      - If pyarrow isn't available and force_date32=True, falls back to python date
        objects for selected columns during write.
    """
    src_path = Path(src_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not src_path.exists():
        raise FileNotFoundError(f"Static dimension source not found: {src_path}")

    stat = src_path.stat()

    # Version key: path + content fingerprint. Uses a content hash (not mtime)
    # so a git clone/checkout that rewrites the file with identical bytes does
    # not trigger a needless regeneration.
    version_key = {
        "source": str(src_path),
        "source_size": int(stat.st_size),
        "source_sha256": _file_sha256(src_path),
        "date_cols": list(date_cols) if date_cols is not None else None,
        "cast_all_datetime": bool(cast_all_datetime),
        "compression": str(compression),
        "compression_level": (int(compression_level) if compression_level is not None else None),
        "force_date32": bool(force_date32),
    }

    display_name = name.replace("_", " ").title()

    if not should_regenerate(name, version_key, output_path):
        if log_skip:
            skip(f"{display_name} up-to-date")
        return pd.read_parquet(output_path), False

    info(f"Loading {display_name}")

    df = pd.read_parquet(src_path)

    # Write using shared utility for consistent Power Query behavior
    write_parquet_with_date32(
        df,
        output_path,
        date_cols=date_cols,
        cast_all_datetime=cast_all_datetime,
        compression=compression,
        compression_level=compression_level,
        force_date32=force_date32,
    )

    save_version(name, version_key, output_path)
    return df, True
