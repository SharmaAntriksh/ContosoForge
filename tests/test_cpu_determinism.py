"""Generated dimensions and sales must not depend on NumPy CPU dispatch."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import numpy as np
import pytest


def test_generation_identical_with_and_without_optional_cpu_features(tmp_path):
    # NumPy exposes its compiled dispatch targets here. Query rather than hard
    # coding AVX names: recent wheels group them under X86_V3/X86_V4 instead.
    runtime = np._core._multiarray_umath
    optional = [
        feature for feature in runtime.__cpu_dispatch__
        if runtime.__cpu_features__.get(feature)
    ]
    if not optional:
        pytest.skip("No optional NumPy CPU features available to compare")

    repo_root = Path(__file__).resolve().parents[1]
    probe = tmp_path / "probe.py"
    probe.write_text(textwrap.dedent('''
        import json
        from pathlib import Path
        import sys
        import pandas as pd
        from tests import sales_gen
        from tests.test_sales_snapshot import _fingerprint, _load_sales_fact
        import src.versioning.version_store as versions

        if __name__ == "__main__":
            work = Path(sys.argv[1])
            work.mkdir()
            versions.VERSION_DIR = work / "versions"
            versions.VERSION_DIR.mkdir()
            cfg = sales_gen.small_config(
                dims_dir=work / "dims", scratch_dir=work / "scratch",
                final_dir=work / "final", workers=1, chunk_size=12_000,
            )
            sales_gen.run_pipeline_stage(work, cfg, only="dimensions")
            sales_gen.run_pipeline_stage(work, cfg, only="sales")
            result = {"sales": _fingerprint(_load_sales_fact(
                work / "final", work / "scratch", "parquet",
            ))}
            for table in ("customers", "customer_profile", "organization_profile",
                          "products", "product_profile"):
                result[table] = _fingerprint(pd.read_parquet(work / "dims" / (table + ".parquet")))
            (work / "fingerprints.json").write_text(json.dumps(result), encoding="utf-8")
    '''), encoding="utf-8")

    results = []
    for label, disabled in (("native", ""), ("baseline", ",".join(optional))):
        work = tmp_path / label
        result = subprocess.run(
            [sys.executable, str(probe), str(work)],
            cwd=repo_root,
            env={**os.environ, "PYTHONPATH": str(repo_root), "PYTHONUTF8": "1",
                 "NPY_DISABLE_CPU_FEATURES": disabled},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
        results.append(json.loads((work / "fingerprints.json").read_text(encoding="utf-8")))

    assert results[0] == results[1], f"NumPy CPU dispatch changed output: {results}"
