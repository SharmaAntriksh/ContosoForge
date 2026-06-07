"""Tests for the shared batch-fact writer (src/facts/shared/writers.py)."""
from __future__ import annotations

import pandas as pd

from src.facts.shared.writers import write_fact_table


class TestBatchFactCsvLineTerminator:
    """TOOLS-1: batch facts must be written with LF, because the generated
    BULK INSERT hardcodes ROWTERMINATOR='0x0a'. pandas to_csv defaults to CRLF
    on Windows, which would corrupt/break the last column on import."""

    def _df(self):
        # last column numeric (the TOOLS-1 hard-failure case) + a string column
        return pd.DataFrame({
            "BudgetMethod": ["seasonal", "flat", "blend"],
            "ResponseDays": [1, 5, 9],
        })

    def test_single_file_uses_lf(self, tmp_path):
        write_fact_table(self._df(), tmp_path, "BudgetMonthly", "csv")
        raw = (tmp_path / "BudgetMonthly.csv").read_bytes()
        assert b"\r\n" not in raw
        assert b"\r" not in raw

    def test_chunked_files_use_lf(self, tmp_path):
        write_fact_table(self._df(), tmp_path, "BudgetMonthly", "csv", csv_chunk_size=2)
        chunks = sorted(tmp_path.glob("BudgetMonthly_*.csv"))
        assert len(chunks) == 2  # 3 rows, chunk size 2
        for chunk in chunks:
            raw = chunk.read_bytes()
            assert b"\r\n" not in raw
            assert b"\r" not in raw
