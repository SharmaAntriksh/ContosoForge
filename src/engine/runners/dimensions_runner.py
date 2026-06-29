# ---------------------------------------------------------
#  DIMENSIONS ORCHESTRATOR (DECLARATIVE + DEPENDENCY-AWARE)
# ---------------------------------------------------------
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from src.engine.config.config import skip_order_blocks_feature
from src.exceptions import DimensionError
from src.utils.config_precedence import _is_random_mode
from src.utils.logging_utils import info, skip, stage
from src.versioning import delete_version

from src.dimensions.geography import run_geography
from src.dimensions.customers import run_customers
from src.dimensions.customers.subscriptions import run_subscriptions
from src.dimensions.stores import run_stores
from src.dimensions.promotions import run_promotions
from src.dimensions.dates import run_dates
from src.dimensions.exchange_rates import run_currency, run_exchange_rates
from src.dimensions.products import run_suppliers, generate_product_dimension as run_products
from src.dimensions.employees import run_employees, run_employee_store_assignments
from src.dimensions.warehouses import run_warehouses
from src.dimensions.dates import run_time_table
from src.dimensions.return_reasons import run_return_reasons

from src.dimensions.lookups import (
    run_sales_channels,
    run_loyalty_tiers,
    run_customer_acquisition_channels,
)


# =========================================================
# Helpers (keep backward-compatible config behavior)
# =========================================================

def _get_defaults_dates(cfg):
    """Return defaults.dates from cfg."""
    defaults_section = getattr(cfg, "defaults", None)
    if not defaults_section:
        return None
    return getattr(defaults_section, "dates", None)


def _cfg_for_dimension(cfg, dim_key: str):
    """Return a deep-copied cfg (mutations won't affect the original)."""
    return cfg.model_copy(deep=True) if hasattr(cfg, "model_copy") else dict(cfg)


def _inject_global_dates(cfg_copy, dim_key: str, global_dates) -> None:
    """Inject ``global_dates`` into the dim section of an already-copied cfg, in place.

    Operates on the per-dimension copy from :func:`_cfg_for_dimension` so we
    take a single deep copy per dimension rather than two.
    """
    if global_dates is None:
        return
    dim_section = getattr(cfg_copy, dim_key, None)
    if dim_section is not None:
        object.__setattr__(dim_section, "global_dates", global_dates)


def _returns_enabled(cfg) -> bool:
    returns_cfg = cfg.returns if hasattr(cfg, "returns") else None
    if not returns_cfg or not isinstance(returns_cfg, Mapping):
        return False
    if not bool(getattr(returns_cfg, "enabled", False)):
        return False
    return not skip_order_blocks_feature(cfg)


def _subscriptions_enabled(cfg) -> bool:
    sub_cfg = getattr(cfg, "subscriptions", None)
    if not sub_cfg or not isinstance(sub_cfg, Mapping):
        return False
    return bool(getattr(sub_cfg, "enabled", False))




# =========================================================
# Spec registry
# =========================================================

EnabledFn = Callable[[Dict[str, Any]], bool]
RunFn = Callable[[Dict[str, Any], Path], Any]


@dataclass(frozen=True)
class DimensionSpec:
    """
    Declarative dimension metadata.

    name:
      - used in force_regenerate set and in summary keys
    cfg_key:
      - config section name passed to _cfg_for_dimension/_cfg_with_global_dates
    deps:
      - upstream dependencies; used for ordering and force cascading (downstream)
    date_dependent:
      - if any date-dependent dim is forced, we expand force to all date-dependent dims
    inject_global_dates:
      - whether to pass defaults.dates into cfg[<cfg_key>]["global_dates"]
    enabled:
      - whether dim is active; forcing overrides enabled=False
    outputs_any:
      - if any of these files exist/changes, we treat the dim as producing outputs
    outputs_all:
      - if provided, we monitor these files (all are monitored; changes indicate regen)
    force_also:
      - extra forced dims to include when this dim is forced (special-cases like products -> suppliers)
    """
    name: str
    cfg_key: str
    run_fn: RunFn
    deps: Tuple[str, ...] = ()
    date_dependent: bool = False
    inject_global_dates: bool = False
    enabled: EnabledFn = lambda cfg: True
    outputs_any: Tuple[str, ...] = ()
    outputs_all: Tuple[str, ...] = ()
    force_also: Tuple[str, ...] = ()
    regenerated_from_return_key: Optional[str] = None  # e.g. "_regenerated" for products


DIM_SPECS: List[DimensionSpec] = [
    # 1) Geography (upstream for stores)
    DimensionSpec(
        name="geography",
        cfg_key="geography",
        run_fn=run_geography,
        outputs_all=("geography.parquet",),
    ),

    # 1.5) Lookups (trimmed)
    DimensionSpec(name="sales_channels", cfg_key="sales_channels", run_fn=run_sales_channels, outputs_all=("channels.parquet",)),
    DimensionSpec(name="loyalty_tiers", cfg_key="loyalty_tiers", run_fn=run_loyalty_tiers, outputs_all=("loyalty_tiers.parquet",)),
    DimensionSpec(name="customer_acquisition_channels", cfg_key="customer_acquisition_channels", run_fn=run_customer_acquisition_channels, outputs_all=("customer_acquisition_channels.parquet",)),

    # 2) Customers (depends on geography for GeographyKey assignment)
    DimensionSpec(
        name="customers",
        cfg_key="customers",
        run_fn=run_customers,
        deps=("geography",),
        date_dependent=True,
        inject_global_dates=True,
        outputs_all=("customers.parquet", "customer_profile.parquet", "organization_profile.parquet"),
    ),

    # 2.5) Subscriptions (depends on customers)
    DimensionSpec(
        name="subscriptions",
        cfg_key="subscriptions",
        run_fn=run_subscriptions,
        deps=("customers",),
        date_dependent=True,
        inject_global_dates=True,
        enabled=_subscriptions_enabled,
        outputs_any=("plans.parquet", "customer_subscriptions.parquet"),
    ),

    # 3) Stores (depends on geography)
    DimensionSpec(
        name="stores",
        cfg_key="stores",
        run_fn=run_stores,
        deps=("geography",),
        date_dependent=True,
        inject_global_dates=True,
        outputs_all=("stores.parquet",),
    ),

    # 3.25) Warehouses (depends on stores + geography; modifies stores.parquet)
    DimensionSpec(
        name="warehouses",
        cfg_key="warehouses",
        run_fn=run_warehouses,
        deps=("stores", "geography"),
        outputs_all=("warehouses.parquet",),
    ),

    # 3.5) Employees (depends on stores + warehouses)
    DimensionSpec(
        name="employees",
        cfg_key="employees",
        run_fn=run_employees,
        deps=("stores", "warehouses"),
        date_dependent=True,
        inject_global_dates=True,
        outputs_all=("employees.parquet",),
    ),

    # 3.6) EmployeeStoreAssignments (depends on employees + stores)
    DimensionSpec(
        name="employee_store_assignments",
        cfg_key="employee_store_assignments",
        run_fn=run_employee_store_assignments,
        deps=("stores", "employees"),
        date_dependent=True,
        inject_global_dates=True,
        outputs_all=("employee_store_assignments.parquet",),
    ),

    # 4) Promotions
    DimensionSpec(
        name="promotions",
        cfg_key="promotions",
        run_fn=run_promotions,
        date_dependent=True,
        inject_global_dates=True,
        outputs_all=("promotions.parquet",),
    ),

    # 4.5) Return Reasons (only if returns enabled; force overrides)
    DimensionSpec(
        name="return_reason",
        cfg_key="return_reason",
        run_fn=run_return_reasons,
        enabled=_returns_enabled,
        # tolerate naming differences
        outputs_any=("return_reasons.parquet", "return_reason.parquet"),
    ),

    # 4.8) Suppliers
    DimensionSpec(
        name="suppliers",
        cfg_key="suppliers",
        run_fn=run_suppliers,
        outputs_all=("suppliers.parquet",),
    ),

    # 5) Products (depends on suppliers; also force suppliers when products forced)
    DimensionSpec(
        name="products",
        cfg_key="products",
        run_fn=run_products,
        deps=("suppliers",),
        force_also=("suppliers",),
        regenerated_from_return_key="_regenerated",
        outputs_all=("products.parquet", "product_profile.parquet"),
    ),

    # 6) Dates
    DimensionSpec(
        name="dates",
        cfg_key="dates",
        run_fn=run_dates,
        date_dependent=True,
        inject_global_dates=True,
        outputs_all=("dates.parquet",),
    ),

    # 7) Currency
    DimensionSpec(
        name="currency",
        cfg_key="currency",
        run_fn=run_currency,
        deps=("dates",),
        date_dependent=True,
        outputs_all=("currency.parquet",),
    ),

    # 8) Exchange Rates
    DimensionSpec(
        name="exchange_rates",
        cfg_key="exchange_rates",
        run_fn=run_exchange_rates,
        deps=("dates", "currency"),
        date_dependent=True,
        inject_global_dates=True,
        outputs_all=("exchange_rates.parquet", "exchange_rates_monthly.parquet"),
    ),

    # 9) Time
    DimensionSpec(
        name="time",
        cfg_key="time",
        run_fn=run_time_table,
        outputs_all=("time.parquet",),
    ),
]


# =========================================================
# Graph utilities
# =========================================================

def _stable_toposort(specs: Sequence[DimensionSpec]) -> List[DimensionSpec]:
    """
    Stable Kahn topological sort: preserves original order among nodes
    that have equal dependency readiness.
    """
    by_name = {s.name: s for s in specs}
    indeg: Dict[str, int] = {s.name: 0 for s in specs}
    out: Dict[str, List[str]] = {s.name: [] for s in specs}

    # Build graph
    for s in specs:
        for d in s.deps:
            if d not in by_name:
                raise DimensionError(f"Dimension '{s.name}' depends on unknown dimension '{d}'")
            out[d].append(s.name)
            indeg[s.name] += 1

    # Queue in original spec order (deque for O(1) popleft)
    queue: deque[str] = deque(s.name for s in specs if indeg[s.name] == 0)
    result: List[str] = []

    while queue:
        n = queue.popleft()
        result.append(n)
        for m in out[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)

    if len(result) != len(specs):
        remaining = [k for k, v in indeg.items() if v > 0]
        raise RuntimeError(f"Cycle detected in dimension dependency graph. Remaining: {remaining}")

    return [by_name[n] for n in result]


def _dependents_map(specs: Sequence[DimensionSpec]) -> Dict[str, Set[str]]:
    deps: Dict[str, Set[str]] = {s.name: set() for s in specs}
    for s in specs:
        for d in s.deps:
            deps[d].add(s.name)
    return deps


# =========================================================
# Output-change detection
# =========================================================

def _stat_sig(p: Path) -> Optional[Tuple[int, int]]:
    """
    Signature for change detection: (mtime_ns, size). None if missing.
    """
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None


def _resolve_watch_paths(folder: Path, spec: DimensionSpec) -> List[Path]:
    paths: List[Path] = []
    if spec.outputs_all:
        paths.extend([folder / x for x in spec.outputs_all])
    if spec.outputs_any:
        paths.extend([folder / x for x in spec.outputs_any])
    # de-dupe
    seen = set()
    uniq: List[Path] = []
    for p in paths:
        if p not in seen:
            uniq.append(p)
            seen.add(p)
    return uniq


def _snapshot(folder: Path, spec: DimensionSpec) -> Dict[Path, Optional[Tuple[int, int]]]:
    return {p: _stat_sig(p) for p in _resolve_watch_paths(folder, spec)}


def _detect_regen_from_io(before: Dict[Path, Optional[Tuple[int, int]]], after: Dict[Path, Optional[Tuple[int, int]]]) -> Optional[bool]:
    if not before and not after:
        return None
    for p, b in before.items():
        a = after.get(p)
        if a != b:
            return True
    return False


# =========================================================
# Main Orchestrator
# =========================================================

def generate_dimensions(
    cfg: Dict[str, Any],
    parquet_dims_folder: Path,
    force_regenerate: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    Orchestrates dimension generation in dependency order.

    Key behaviors:
    - Force cascades downstream to dependents (via deps graph)
    - Any forced date-dependent dim expands to all date-dependent dims
    - Forced dims run even if disabled by config
    - regenerated[] is inferred via output file changes where possible
    """
    parquet_dims_folder = Path(parquet_dims_folder).resolve()
    parquet_dims_folder.mkdir(parents=True, exist_ok=True)

    global_dates = _get_defaults_dates(cfg)
    if global_dates is not None:
        info(f"Using global dates: start={getattr(global_dates, 'start', None)} end={getattr(global_dates, 'end', None)}")

    specs_ordered = _stable_toposort(DIM_SPECS)
    by_name = {s.name: s for s in DIM_SPECS}
    known = set(by_name.keys())

    requested = set(force_regenerate or set())
    if "all" in requested:
        force_set = set(known)
    else:
        unknown = {x for x in requested if x not in known}
        if unknown:
            info(f"Ignoring unknown force_regenerate keys: {sorted(unknown)}")
        force_set = {x for x in requested if x in known}

    # random mode: every run must regenerate all dimensions
    if _is_random_mode(cfg):
        force_set = set(known)

    # Special per-dimension force expansions (e.g., products -> suppliers).
    # Iterate to a fixed point so chained force_also entries are all included.
    _pending = list(force_set)
    while _pending:
        n = _pending.pop()
        for extra in by_name[n].force_also:
            if extra not in force_set:
                force_set.add(extra)
                _pending.append(extra)

    # Date-dependent expansion:
    date_dependent_names = {s.name for s in DIM_SPECS if s.date_dependent}
    if force_set.intersection(date_dependent_names):
        force_set.update(date_dependent_names)

    # Downstream cascade: if A is forced, force all dependents of A.
    # This also handles the case where warehouses must regenerate when
    # stores regenerates (warehouses modifies stores.parquet to add
    # WarehouseKey — a fresh stores.parquet without it breaks inventory).
    dependents = _dependents_map(DIM_SPECS)
    queue = list(force_set)
    while queue:
        n = queue.pop()
        for dep in dependents.get(n, ()):
            if dep not in force_set:
                force_set.add(dep)
                queue.append(dep)

    # Delete version files for forced dims — each dimension's
    # should_regenerate() will return True when the file is missing.
    if force_set:
        # When all dims are forced, clean the output folder to remove
        # orphaned files from prior runs (e.g. moved or renamed outputs).
        all_dim_names = {s.name for s in DIM_SPECS}
        if force_set >= all_dim_names:
            for f in parquet_dims_folder.glob("*.parquet"):
                f.unlink()

        for n in sorted(force_set):
            delete_version(n)

    regenerated: Dict[str, bool] = {}

    with stage("Generating Dimensions"):
        for spec in specs_ordered:
            forced = spec.name in force_set
            enabled = spec.enabled(cfg) or forced
            if not enabled:
                regenerated[spec.name] = False
                continue

            cfg_run = _cfg_for_dimension(cfg, spec.cfg_key)
            if spec.inject_global_dates:
                _inject_global_dates(cfg_run, spec.cfg_key, global_dates)

            before = _snapshot(parquet_dims_folder, spec)
            out = spec.run_fn(cfg_run, parquet_dims_folder)
            after = _snapshot(parquet_dims_folder, spec)

            if spec.regenerated_from_return_key and isinstance(out, Mapping):
                regen = bool(out.get(spec.regenerated_from_return_key))
            else:
                io_regen = _detect_regen_from_io(before, after)
                regen = bool(io_regen) if io_regen is not None else False

            regenerated[spec.name] = bool(regen)

        # Propagate to downstream dims that consume a regenerated upstream but
        # were skipped (their own version hash didn't change). The canonical
        # case: warehouses MODIFIES stores.parquet to add WarehouseKey, so a
        # stores regen (hash change, not force) leaves a fresh stores.parquet
        # without WarehouseKey unless warehouses re-runs — which then breaks
        # inventory. Other consumers (employees, ESA) likewise need fresh
        # upstream data. Walk in topological order so each re-run cascades to
        # its own dependents (stores -> warehouses -> employees -> ESA).
        for spec in specs_ordered:
            if regenerated.get(spec.name):
                continue
            if not (spec.enabled(cfg) or spec.name in force_set):
                continue
            if not any(regenerated.get(dep) for dep in spec.deps):
                continue
            info(f"{spec.name} re-running: an upstream dependency regenerated")
            delete_version(spec.name)
            cfg_run = _cfg_for_dimension(cfg, spec.cfg_key)
            if spec.inject_global_dates:
                _inject_global_dates(cfg_run, spec.cfg_key, global_dates)
            spec.run_fn(cfg_run, parquet_dims_folder)
            regenerated[spec.name] = True

    return {
        "global_dates": global_dates,
        "folder": str(parquet_dims_folder),
        "regenerated": regenerated,
    }
