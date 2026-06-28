import os
import pyarrow.parquet as pq

from src.utils.logging_utils import info
from src.versioning.version_store import load_version, _compute_hash


def load_dimension(name, parquet_dims_path, expected_config):
    """
    Loads a dimension parquet file and returns:
        (pandas_dataframe, changed_flag)

    Rules:
    - If parquet is missing → changed_flag = True
    - If version file is missing → changed_flag = True
    - If expected_config is None (static dimension) → changed_flag = False
    - Otherwise → changed_flag = version mismatch
    """

    path = os.path.join(str(parquet_dims_path), f"{name}.parquet")

    # Missing parquet -> must regenerate AND nothing to return
    if not os.path.exists(path):
        info(f"{name.title()} missing - will regenerate.")
        return None, True

    # Load the dimension as Pandas DataFrame (NOT PyArrow Table)
    df = pq.read_table(path).to_pandas()

    # Version check
    prev_version = load_version(name)

    # If no version file exists → changed
    if prev_version is None:
        changed_flag = True
    elif expected_config is None:
        # static dimension: version existence is enough
        changed_flag = False
    else:
        # prev_version is the stored metadata envelope
        # ({"config_hash": ..., "parquet_mtime": ...}). Compare its stored
        # hash against the expected config's hash — NOT the envelope itself,
        # which never equals a raw config section.
        prev_hash = prev_version.get("config_hash") if isinstance(prev_version, dict) else None
        changed_flag = prev_hash != _compute_hash(expected_config)

    return df, changed_flag
