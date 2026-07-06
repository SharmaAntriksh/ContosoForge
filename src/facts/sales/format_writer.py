"""Single source of truth for the sales output format.

The output format ("parquet" | "csv" | "deltaparquet") drives three separate
decisions that were each their own ``if file_format ==`` switch: the per-chunk
write target (worker), which manifest fields a table carries, and how the final
artifacts are assembled (coordinator). :class:`SalesOutputFormat` parses the
format string once into a value object those call sites branch on, and
:func:`manifest_fields_for` centralizes the manifest-field policy that used to be
duplicated between the live and empty-manifest builders.

Leaf module: depends only on the stdlib, so both the worker and coordinator
layers can import it without a cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from src.exceptions import SalesError


class SalesOutputFormat(str, Enum):
    """The sales fact output format, parsed once from the config string."""

    PARQUET = "parquet"
    CSV = "csv"
    DELTA = "deltaparquet"

    @classmethod
    def parse(cls, file_format: Any) -> "SalesOutputFormat":
        ff = str(file_format or "").strip().lower()
        try:
            return cls(ff)
        except ValueError as exc:
            raise SalesError(f"Unsupported file_format: {file_format!r}") from exc

    @property
    def is_parquet(self) -> bool:
        return self is SalesOutputFormat.PARQUET

    @property
    def is_csv(self) -> bool:
        return self is SalesOutputFormat.CSV

    @property
    def is_delta(self) -> bool:
        return self is SalesOutputFormat.DELTA


@dataclass(frozen=True)
class ManifestFields:
    """The format-dependent output locators a table records in the run manifest."""

    merged_path: Optional[str] = None       # parquet only
    delta_table_dir: Optional[str] = None   # delta only
    delta_parts_dir: Optional[str] = None   # delta only
    out_dir: Optional[str] = None           # scratch dir holding this table's chunks


def manifest_fields_for(
    fmt: SalesOutputFormat, output_paths: Any, table: str
) -> ManifestFields:
    """Resolve the manifest locators for one table under the given format.

    Parquet records its merged file; Delta records its table + parts dirs. Every
    format also records ``out_dir`` — the authoritative scratch folder the writer
    put this table's chunks in — so packaging can copy from the declared location
    (CSV chunks, or the parquet chunk fallback when merge is disabled) instead of
    re-probing a list of candidate paths.
    """
    out_dir = output_paths.table_out_dir(table)
    if fmt.is_parquet:
        return ManifestFields(
            merged_path=output_paths.merged_path(table), out_dir=out_dir
        )
    if fmt.is_delta:
        return ManifestFields(
            delta_table_dir=output_paths.delta_table_dir(table),
            delta_parts_dir=output_paths.delta_parts_dir(table),
            out_dir=out_dir,
        )
    return ManifestFields(out_dir=out_dir)
