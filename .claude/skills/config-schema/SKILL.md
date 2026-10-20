---
name: config-schema
description: >-
  Update or debug ContosoForge configuration schemas, normalization, and config consumers. Use for new YAML fields, validation errors, or cfg/models_cfg access. Distinguishes typed pipeline models from raw web editor dicts and the sales worker wire schema.
---

# Config Schema: the Pydantic Spine

ContosoForge has two config files (`config.yaml` for shape/scale, `models.yaml` for
behavior), and **both are parsed into Pydantic models**, not dicts. Getting this wrong
is the single most common mistake in this codebase, so internalize the rules below
before touching pipeline config-reading code.

## Pipeline models

- `cfg` is an **`AppConfig`** instance.
- `models_cfg` (aka `State.models_cfg`) is a **`ModelsInnerConfig`** instance.

Both are Pydantic models from `src/engine/config/config_schema.py`. **Access fields by
attribute**, not dict subscription:

```python
fmt = cfg.sales.file_format               # yes
infl = models_cfg.pricing.inflation       # yes
fmt = cfg["sales"]["file_format"]         # avoid (works on read via a shim, but wrong idiom)
```

Compatibility shims exist so legacy code keeps working, but write new code in the
attribute idiom:

- **Write:** `cfg.sales.file_format = "csv"` (attribute assignment). Bracket write
  (`cfg["key"] = val`) works via `_MutationMixin.__setitem__`.
- **`.get()` shim:** `_MutationMixin.get(key, default)` delegates to `getattr()`, so
  existing `.get()` chains keep working on models. Intentional, for code that handles
  both dicts and models.
- **Copy typed pipeline models:** prefer `cfg.model_copy(deep=True)`.
  `dict(cfg)` is not a deep conversion; use `model_dump()` for serialization.
  Web snapshots intentionally use `copy.deepcopy()` for mixed dict/model state.
- **Serialization/hashing:** use `model_dump()` at the boundary where a plain
  dict is needed; reuse a local conversion helper when one already exists.

## Boundaries: raw YAML, web state, and worker config

The typed rule applies after pipeline validation. Raw `yaml.safe_load()` values
and request payloads are dicts. In `web/shared_state.py`, `_cfg` is initialized as
`AppConfig`, while `_models_cfg` holds the **raw root models dict**, including its
`models` key. `models_routes.py` validates via `ModelsConfig.from_raw_dict()` but
stores the raw dict for editing and serialization. Do not replace those web
accesses with `ModelsInnerConfig` attribute access. Web snapshots use
`copy.deepcopy()` under `_cfg_lock`; see the web-layer skill.

The sales wire config is a dict, but its schema is the `SalesWorkerCfg`
`TypedDict` in `src/facts/sales/prep/worker_cfg_schema.py`. Coordinator holders such
as `SalesRunConfig` and `MonthPlan` are frozen dataclasses in `prep/run_config.py`,
not Pydantic models. Check the boundary before choosing an access/copy idiom.

## Adding a new config field

Update the relevant schema and normalizer, shipped YAML defaults/comments, and
`docs/CONFIG_GUIDE.md` or `docs/MODELS_GUIDE.md` when the field is user-facing.
If the web UI exposes it, trace form/preset/Quick Start mapping too. Fields that
change dimension output must participate in the dimension version key; check
`dimensions_runner.py` and the dimension runner before assuming a config edit
will invalidate cached parquet.

The sub-models reject unknown keys, so you can't just start reading a new YAML key —
you must add it to the schema:

- **Sub-models use `extra="forbid"`** (via `_Base`). Add your new field to the relevant
  schema class in `config_schema.py`, or validation will reject the key.
- **`AppConfig` and `ModelsConfig` use `extra="allow"`** for runtime-injected keys only
  — don't rely on that for real config fields; type them properly on the sub-model.
- **`AppConfig.from_raw_dict()` strips `_`-prefixed keys** using
  `_strip_internal_keys()` before validation. Do not assume `ModelsConfig` uses
  the same preprocessing; its constructor calls `model_validate()` directly.
- Canonical constructors: `AppConfig.from_raw_dict(d)` and
  `ModelsConfig.from_raw_dict(d)`.

## Normalizer registry

`src/engine/config/config.py` holds the strict normalizer registry that canonicalizes
raw YAML before Pydantic validation (coercing legacy key shapes, applying defaults,
cross-section rules). `apply_cross_section_rules()` is where, e.g., returns/complaints
get auto-disabled when they'd have no OrderNumber to link to (gotcha #4). If a config
value needs massaging before it's valid, that belongs in the normalizer, not scattered
through consumers.

## Coercion helpers

Import `bool_or`, `int_or`, `float_or`, `str_or` from `src.utils.config_helpers` — do
**not** define local variants (gotcha #10). They're still needed for:

- plain dict accesses (worker configs, nested profile dicts),
- function params that might be `None`.

For **Pydantic model attributes with typed defaults** they're redundant (the type is
already guaranteed) but harmless. Worker configs (`worker_cfg`, `acfg`, sales worker
internals) remain **plain dicts** for multiprocessing pickling — those correctly use
dict access and the coercion helpers.

## The trend-preset caveat

After `resolve_trend_preset()` runs, `models_cfg.macro_demand` and `models_cfg.customers`
are **replaced** with freshly validated Pydantic models (`MacroDemandConfig`,
`CustomersDemandConfig`) — not plain dicts. So downstream code can rely on attribute
access there too. (The old `resolve_customer_profile` is deprecated.)

## Tests

```bash
.venv/Scripts/python.exe -m pytest tests/test_config_loader.py tests/test_schema.py tests/test_state.py
```
