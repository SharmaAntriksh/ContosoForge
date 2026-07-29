---
name: config-schema
description: >-
  How ContosoForge's configuration is typed and validated — the Pydantic spine in
  src/engine/config/config_schema.py, the normalizer registry in config.py, and the
  coercion helpers in src/utils/config_helpers.py. Load this when adding or changing a
  config field, editing config validation/normalization, writing code that reads cfg
  or models_cfg, or debugging a config validation error. Its single most important
  point: BOTH cfg and models_cfg are Pydantic models, not dicts — access fields by
  attribute. Use it whenever you see AttributeError/ValidationError on config, need to
  add a new config.yaml or models.yaml key, or aren't sure whether to use dict or
  attribute access on a config object.
---

# Config Schema: the Pydantic Spine

ContosoForge has two config files (`config.yaml` for shape/scale, `models.yaml` for
behavior), and **both are parsed into Pydantic models**, not dicts. Getting this wrong
is the single most common mistake in this codebase, so internalize the rules below
before touching config-reading code.

## The one rule to remember

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
- **Copy:** use `cfg.model_copy(deep=True)` — **not** `dict(cfg)` or
  `copy.deepcopy(cfg)`.
- **Hashing:** use the `_to_dict(obj)` helper (calls `model_dump()` on Pydantic,
  pass-through for dicts) before `json.dumps()`.

## Adding a new config field

The sub-models reject unknown keys, so you can't just start reading a new YAML key —
you must add it to the schema:

- **Sub-models use `extra="forbid"`** (via `_Base`). Add your new field to the relevant
  schema class in `config_schema.py`, or validation will reject the key.
- **`AppConfig` and `ModelsConfig` use `extra="allow"`** for runtime-injected keys only
  — don't rely on that for real config fields; type them properly on the sub-model.
- **`_`-prefixed keys are stripped** before validation (`_strip_internal_keys()`), since
  Pydantic v2 treats them as private. Normalizer metadata uses this.
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
pytest tests/test_config_loader.py tests/test_schema.py tests/test_state.py
```
