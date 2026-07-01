"""Coordinator-side setup for the sales fact.

Modules that run once in the main process (``generate_sales_fact``) to prepare
inputs for the worker pool — dimension loading, correlation/lookup precompute,
the SCD2 price grid, worker-config assembly + schema, the memory model, the
coverage pre-flight, and leaf helpers. Kept together (and apart from the hot
worker path in ``sales_logic``/``sales_worker``) purely for navigability; there
is no behavior change from the flat layout. Import submodules explicitly
(``from .prep.dimension_loaders import ...``); this package has no re-exports so
it stays side-effect-free and cycle-safe.
"""
