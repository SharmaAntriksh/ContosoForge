# Release Notes

## Unreleased

### Temporal mix dynamics

Sales generation now produces gradual, deterministic changes in the relative mix
of customer country, customer gender, product category, brand within category, and
color within category. This reduces unrealistically static year-over-year ranks
while retaining structural differences such as large markets and strong product
groups.

The movement is derived automatically from the existing trend preset, run seed, and
relative month in the configured date range. There are no new user-facing controls,
no calendar-year-specific factors, and no output-schema changes. Monthly row,
order, and distinct-customer plans remain unchanged.

The implementation remains deterministic across repeated runs, worker counts, and
chunk sizes. Because customer and product assignments intentionally change, the
sales golden fingerprints have been reviewed and updated for all existing snapshot
fixtures.

The old `brand_popularity` models block is deprecated. Existing configurations
continue to parse and emit a warning, but the legacy rotating-brand effect is not
stacked with the unified temporal mix model. The block has been removed from the
shipped default configuration.
