# Assumes PBIP templates define a Power Query expression named:
#   expression ContosoFolder
# used as the root path for generated datasets.

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Optional

from src.exceptions import PackagingError


PBIP_TEMPLATES_ROOT = Path("samples/powerbi/templates")

# sales.sales_output -> folder name under templates/{csv|parquet}/
_MODE_DIR = {
    "sales": "Sales PBIP",
    "sales_order": "Orders PBIP",
    "both": "Both PBIP",
}


def resolve_pbip_template_root(
    *,
    file_format: str,
    sales_output: str,
    templates_root: Path = PBIP_TEMPLATES_ROOT,
) -> Optional[Path]:
    """
    Strict resolver (no legacy fallback).

    Layout expected:
      samples/powerbi/templates/
        csv/{Sales|Orders|Sales and Orders}/...
        parquet/{Sales|Orders|Sales and Orders}/...

    Returns:
      Path to template root directory (to be copied), or None when PBIP should be skipped
      (deltaparquet).
    """
    fmt = (file_format or "").strip().lower()
    mode = (sales_output or "").strip().lower()

    # deltaparquet intentionally skips PBIP
    if fmt == "deltaparquet":
        return None

    if fmt not in {"csv", "parquet"}:
        raise ValueError(f"Unsupported file_format={file_format!r}; expected csv|parquet|deltaparquet")

    if mode not in _MODE_DIR:
        raise ValueError(f"Unsupported sales_output={sales_output!r}; expected one of {sorted(_MODE_DIR)}")

    candidate = templates_root / fmt / _MODE_DIR[mode]
    if not candidate.exists():
        raise FileNotFoundError(f"PBIP template not found: {candidate}")

    return candidate


def maybe_attach_pbip_project(
    *,
    final_folder: Path,
    file_format: str,
    sales_output: str,
    expression_name: str = "ContosoFolder",
    pbip_dst_name: Optional[str] = None,
) -> bool:
    """
    Convenience wrapper:
      - resolves PBIP template folder for (file_format, sales_output)
      - attaches it if applicable
      - returns True if PBIP attached, False if skipped (deltaparquet)
    """
    template_root = resolve_pbip_template_root(
        file_format=file_format,
        sales_output=sales_output,
    )
    if template_root is None:
        return False

    attach_pbip_project(
        final_folder=final_folder,
        pbip_template_root=template_root,
        expression_name=expression_name,
        pbip_dst_name=pbip_dst_name,
    )
    return True


def attach_pbip_project(
    final_folder: Path,
    pbip_template_root: Path,
    expression_name: str = "ContosoFolder",
    pbip_dst_name: Optional[str] = None,
):
    """
    Copies a PBIP template into the final output folder and
    rewrites the specified TMDL expression to point at final_folder.

    pbip_template_root should be the PBIP root folder (contains exactly one *.SemanticModel),
    e.g. samples/powerbi/templates/csv/Orders
    """
    # ------------------------------------------------------------
    # Copy PBIP folder into final output
    # ------------------------------------------------------------
    dst_folder_name = pbip_dst_name or pbip_template_root.name
    pbip_dst = final_folder / dst_folder_name

    if pbip_dst.exists():
        shutil.rmtree(pbip_dst)

    shutil.copytree(pbip_template_root, pbip_dst)

    # ------------------------------------------------------------
    # Locate SemanticModel folder (auto-detect)
    # ------------------------------------------------------------
    semantic_models = list(pbip_dst.glob("*.SemanticModel"))
    if len(semantic_models) != 1:
        raise PackagingError(
            f"Expected exactly one *.SemanticModel folder in PBIP, found {len(semantic_models)}"
        )

    semantic_model = semantic_models[0]
    expressions_file = semantic_model / "definition" / "expressions.tmdl"

    if not expressions_file.exists():
        raise PackagingError(
            "expressions.tmdl not found. Ensure the PBIP template defines "
            f"an expression named '{expression_name}'.\n"
            f"Expected path: {expressions_file}"
        )

    # ------------------------------------------------------------
    # Rewrite the expression
    # ------------------------------------------------------------
    _rewrite_expression(
        expressions_file=expressions_file,
        expression_name=expression_name,
        final_folder=final_folder,
    )


def _rewrite_expression(
    *,
    expressions_file: Path,
    expression_name: str,
    final_folder: Path,
):
    """
    Rewrite exactly one expression of the form:

        expression <expression_name> = "<path>"

    Replacing <path> with final_folder.
    """
    text = expressions_file.read_text(encoding="utf-8")
    new_path = str(final_folder).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')

    # Restrict match to a single line string literal to avoid spanning across expressions.
    pattern = rf'''
        expression\s+{re.escape(expression_name)}\s*=\s*
        "[^"\n]*"
    '''

    updated, count = re.subn(
        pattern,
        lambda _: f'expression {expression_name} = "{new_path}"',
        text,
        flags=re.IGNORECASE | re.VERBOSE,
    )

    if count != 1:
        raise PackagingError(
            f"Expected exactly one expression named '{expression_name}', found {count}"
        )

    expressions_file.write_text(updated, encoding="utf-8")
