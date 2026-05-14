# Configuration Reference (`config.yaml`)

This guide documents every section and knob in `config.yaml`. Sections are listed in the same order as the config file.

---

## Scale

Top-level entity counts. Start here to size your dataset.

| Key | Description | Example |
|-----|-------------|---------|
| `scale.sales_rows` | Total sales transaction rows to generate. This is the primary driver of output size and run time. | `1003295` |
| `scale.products` | Number of products in the product dimension. The base Contoso catalog has 5167 products; values above that expand via stratified duplication, below that trim. | `5167` |
| `scale.customers` | Number of unique customers. Determines the customer dimension size and how many buyers are in the sales pool. | `82851` |
| `scale.stores` | Total store count (physical + online combined). | `200` |
| `scale.promotions` | Promotion counts by type. Each type creates that many promo rows per year window. Holiday promos are always auto-generated on top of these. | `{ seasonal: 11, clearance: 4, limited: 5, flash: 6, volume: 4, loyalty: 3, bundle: 3, new_customer: 3 }` |

---

## Defaults

Global settings that apply across all generators.

| Key | Description | Example |
|-----|-------------|---------|
| `defaults.seed` | Master random seed. All generators derive sub-seeds from this for reproducibility. Same seed + same config = identical output. | `42` |
| `defaults.random` | When `true`, ignores all seeds and uses OS entropy. Produces non-deterministic output every run. | `false` |
| `defaults.view_schema` | SQL schema name for generated views. `"dbo"` creates views with `vw_` prefix in dbo schema. Any other value (e.g. `"report"`, `"bi"`) creates a dedicated schema. | `"bi"` |
| `defaults.final_output` | Root folder where timestamped output packages are written. Each run creates a subfolder here. | `"./generated_datasets"` |
| `defaults.dates.start` | Global start date for all time-dependent generators (sales, exchange rates, budget, inventory). Individual sections cannot override this. | `"2021-01-01"` |
| `defaults.dates.end` | Global end date. Same coupling rules as start. | `"2025-12-31"` |

---

## Sales

Controls the sales fact table: output format, structure, and performance tuning.

### Output format

| Key | Description | Example |
|-----|-------------|---------|
| `sales.file_format` | Output format for all fact tables. `"csv"` = chunked CSVs + auto-generated SQL scripts. `"parquet"` = merged single parquet file. `"deltaparquet"` = Delta Lake with ACID transactions. | `"csv"` |
| `sales.sales_output` | Table structure for sales output. `"sales"` = flat denormalized table. `"sales_order"` = normalized header + detail tables. `"both"` = generates all three tables. | `"sales"` |
| `sales.skip_order_cols` | When `true`, omits `SalesOrderNumber` and `SalesOrderLineNumber` columns. Reduces file size but disables returns generation (returns need order IDs to link back). | `false` |
| `sales.max_lines_per_order` | Maximum line items per sales order. Orders are randomly sized 1..N. Higher values create more multi-item baskets. | `8` |

### Merge & partitioning

| Key | Description | Example |
|-----|-------------|---------|
| `sales.merge.enabled` | Merge parquet chunk files into a single output file after generation. Only applies to parquet/deltaparquet. | `true` |
| `sales.merge.file` | Filename for the merged parquet output. | `"sales.parquet"` |
| `sales.merge.delete_chunks` | Delete individual chunk files after successful merge. | `true` |
| `sales.partition_by` | Partition columns for deltaparquet output. Set to `null` or `[]` to disable partitioning. | `["Year", "Month"]` |

### Post-processing

| Key | Description | Example |
|-----|-------------|---------|
| `sales.sort_merged_parquet` | Sort merged parquet files by key columns for better read performance (predicate pushdown). Only applies to parquet format. Adds time to the packaging step. | `false` |
| `sales.sort_delta_parts` | Sort each delta partition part by key columns for better read performance. Only applies to deltaparquet format. | `false` |
| `sales.quality_report` | Generate an HTML data quality report after packaging. Includes row counts, null checks, and distribution summaries. | `false` |

### Advanced / performance

| Key | Description | Example |
|-----|-------------|---------|
| `sales.advanced.chunk_size` | Rows per parallel worker chunk. Smaller chunks mean more parallelism but more overhead. Default 1M is a good balance for most machines. | `1_000_000` |
| `sales.advanced.workers` | Number of worker processes for parallel sales generation. Set to `null` for auto-detect (CPU count). Don't exceed your CPU count. | `8` |
| `sales.advanced.row_group_size` | Parquet row group size. Controls memory usage during reads and predicate pushdown granularity. | `1_000_000` |
| `sales.advanced.compression` | Parquet compression codec. Options: `snappy` (fast), `zstd` (smaller), `gzip`, `lz4`, `none`. | `"snappy"` |

---

## Returns

Sales return fact table. Generates return events linked to original sales orders.

| Key | Description | Example |
|-----|-------------|---------|
| `returns.enabled` | Master toggle. When `false`, no returns are generated. Also auto-disabled if `skip_order_cols=true` (returns need `SalesOrderNumber` to link back). | `true` |
| `returns.return_rate` | Fraction of sales rows that generate a return event. 0.08 = 8% of sales are returned. | `0.08` |
| `returns.min_days_after_sale` | Minimum days between sale date and return date. | `1` |
| `returns.max_days_after_sale` | Maximum days between sale date and return date. | `60` |

---

## Products

Product dimension configuration: pricing, lifecycle, and SCD2 versioning.

### Pricing

| Key | Description | Example |
|-----|-------------|---------|
| `products.active_ratio` | Fraction of products marked as active/sellable. Inactive products exist in the dimension but won't appear in new sales. | `0.96` |
| `products.value_scale` | Global multiplier applied to all base product prices. Use >1 to inflate prices, <1 to deflate. Affects both ListPrice and UnitCost proportionally. | `1` |
| `products.price_range` | `[min, max]` bounds for ListPrice after scaling. Products outside this range are clamped. | `[20, 4000]` |
| `products.margin_range` | `[min, max]` cost margin as a fraction. 0.15 = 15% margin between UnitCost and ListPrice. Controls profitability distribution. | `[0.15, 0.45]` |

### Brand normalization

| Key | Description | Example |
|-----|-------------|---------|
| `products.brand_normalize` | When `true`, pulls brand-level average prices toward the global median. Reduces price spread between premium and budget brands. | `false` |
| `products.brand_normalize_alpha` | How much brand identity to retain during normalization. 0.35 = brands keep 65% of their original price identity, 35% pulled to median. | `0.35` |

### SCD Type 2 (price history)

| Key | Description | Example |
|-----|-------------|---------|
| `products.scd2.enabled` | Enable SCD Type 2 price history tracking. Creates multiple version rows per product with different prices over time. | `false` |
| `products.scd2.revision_frequency` | Months between price revisions. 12 = prices change roughly once per year. | `12` |
| `products.scd2.price_drift` | Magnitude of price change per revision as a fraction. 0.20 = prices shift up to ~20% per revision. | `0.20` |
| `products.scd2.max_versions` | Maximum version rows per product. Caps how many historical price points are tracked. | `4` |

---

## Customers

Customer dimension: demographics, regional distribution, and SCD2 versioning.

### Demographics

| Key | Description | Example |
|-----|-------------|---------|
| `customers.active_ratio` | Fraction of customers who are active buyers. Inactive customers exist in the dimension but won't generate sales. | `0.95` |
| `customers.household_pct` | Fraction of individual customers placed into multi-person households (spouse/dependent matching). 0.35 = 35% of customers share a HouseholdKey. | `0.35` |
| `customers.region_mix` | Regional distribution of customers by weight. Values are relative (auto-normalized to 100%). Accepted keys: `Americas` (aliases: `US`, `USA`), `Europe` (alias: `EU`), `India`, `APAC` (aliases: `Asia`, `Asia-Pacific`). | `{ Americas: 41, Europe: 39, India: 2, APAC: 18 }` |
| `customers.org_pct` | Percentage of customers that are organizations (B2B accounts) rather than individuals. Organizations get an OrganizationProfile record. | `1` |

### SCD Type 2 (life events)

| Key | Description | Example |
|-----|-------------|---------|
| `customers.scd2.enabled` | Enable SCD Type 2 history tracking. Simulates life events (career changes, marriage, relocation) that create new version rows. | `false` |
| `customers.scd2.change_rate` | Fraction of customers who experience at least one life event change per year. | `0.15` |
| `customers.scd2.max_versions` | Maximum version rows per customer. | `4` |

---

## Wishlists

Customer wishlist fact table. Generates product wishlists linked to customers.

| Key | Description | Example |
|-----|-------------|---------|
| `wishlists.enabled` | Master toggle for wishlist generation. | `true` |
| `wishlists.participation_rate` | Fraction of customers who have wishlists. | `0.35` |
| `wishlists.avg_items` | Mean wishlist items per participating customer (Poisson lambda). Actual counts vary randomly. | `3.5` |
| `wishlists.max_items` | Hard cap on wishlist items per customer. | `20` |
| `wishlists.pre_browse_days` | How many days before a customer's start date they can begin wishlisting products. Simulates browsing before first purchase. | `90` |
| `wishlists.affinity_strength` | Probability that the next wishlist item comes from the same subcategory as a previous item. Higher values create more focused wishlists. | `0.6` |
| `wishlists.conversion_rate` | Fraction of wishlist items drawn from the customer's actual purchase history. Higher values mean wishlists overlap more with what the customer actually bought. | `0.50` |
| `wishlists.seed` | Random seed for wishlist generation. | `500` |

---

## Complaints

Customer complaint fact table. Generates complaint/resolution records.

| Key | Description | Example |
|-----|-------------|---------|
| `complaints.enabled` | Master toggle for complaint generation. | `true` |
| `complaints.complaint_rate` | Fraction of customers who file at least one complaint. | `0.03` |
| `complaints.repeat_complaint_rate` | Fraction of complainers who file more than one complaint. | `0.15` |
| `complaints.max_complaints` | Maximum complaints per customer. | `5` |
| `complaints.resolution_rate` | Fraction of complaints that reach a resolution. Remaining complaints stay open or get escalated. | `0.85` |
| `complaints.escalation_rate` | Fraction of unresolved complaints that get escalated to management. The rest remain in "Open" status. | `0.10` |
| `complaints.avg_response_days` | Mean days to resolve a complaint (exponential distribution). | `5` |
| `complaints.max_response_days` | Hard cap on resolution time in days. | `30` |
| `complaints.seed` | Random seed for complaint generation. | `600` |

---

## Subscriptions

Customer subscription bridge table for DAX many-to-many patterns.

| Key | Description | Example |
|-----|-------------|---------|
| `subscriptions.enabled` | Master toggle for subscription generation. | `true` |
| `subscriptions.generate_bridge` | Generate the `CustomerSubscriptionBridge` table (DAX bridge pattern for many-to-many). | `true` |
| `subscriptions.participation_rate` | Fraction of customers who have at least one subscription. | `0.65` |
| `subscriptions.avg_subscriptions_per_customer` | Mean subscriptions per subscriber (Poisson lambda). | `1.5` |
| `subscriptions.max_subscriptions` | Maximum subscriptions per customer. | `5` |
| `subscriptions.churn_rate` | Fraction of subscriptions that are cancelled before their natural expiry date. | `0.25` |
| `subscriptions.trial_rate` | Fraction of new subscriptions that start with a free trial period. | `0.30` |
| `subscriptions.trial_conversion_rate` | Fraction of trial users who convert to a paid subscription after the trial ends. | `0.85` |
| `subscriptions.seed` | Random seed for subscription generation. | `700` |

---

## Geography

Geography dimension. Derived from a curated master file. Generally left empty — the generator uses built-in curated city/country data.

```yaml
geography: {}
```

---

## Promotions

Promotion dimension configuration.

| Key | Description | Example |
|-----|-------------|---------|
| `promotions.new_customer_window_months` | Number of months after a customer's start date during which the "New Customer" promo type applies. 0 = same month only. | `3` |

> Promotion counts are set in the [Scale](#scale) section (`scale.promotions`).

---

## Stores

Store dimension: physical and online store configuration.

| Key | Description | Example |
|-----|-------------|---------|
| `stores.online_stores` | Number of online-only stores carved from the total store count (`scale.stores`). These get StoreType "Online" instead of physical store types. | `5` |
| `stores.ensure_iso_coverage` | When `true`, ensures at least one store exists per ISO currency code used in exchange rates. Prevents missing-store gaps in cross-currency analytics. | `true` |
| `stores.staffing_ranges` | `[min, max]` employee count per physical store type. Online stores always have 1 employee. | `{ Supermarket: [8, 20], Hypermarket: [15, 40], Convenience: [2, 6] }` |
| `stores.closing.enabled` | Master toggle for store closures. When `false`, all stores remain open for the entire date range. | `true` |
| `stores.closing.close_share` | Fraction of physical stores that close. Closing dates are distributed across the simulation period. | `0.20` |

---

## Warehouses

Warehouse dimension: distribution centers derived from store geography.

| Key | Description | Example |
|-----|-------------|---------|
| `warehouses.seed` | Random seed for warehouse generation. | `42` |

---

## Employees

Employee dimension: HR configuration and inter-store transfers.

| Key | Description | Example |
|-----|-------------|---------|
| `employees.hr.email_domain` | Domain suffix for generated employee email addresses. | `"contoso.com"` |
| `employees.transfers.enabled` | Enable inter-store staff transfers. Creates EmployeeStoreAssignment records showing transfer history. | `true` |
| `employees.transfers.annual_rate` | Fraction of eligible staff that transfer per year. | `0.40` |
| `employees.transfers.min_tenure_months` | Minimum months at current store before an employee is eligible for transfer. | `6` |
| `employees.transfers.same_region_pref` | Probability that a transfer stays within the same geographic region. | `0.70` |

---

## Dates

Date dimension: calendar systems and fiscal year configuration.

### Core settings

| Key | Description | Example |
|-----|-------------|---------|
| `dates.fiscal_start_month` | Month number (1-12) when the fiscal year begins. 5 = fiscal year starts in May. Affects all fiscal calendar columns (FiscalYear, FiscalQuarter, etc.). | `5` |
| `dates.as_of_date` | Reference date for `IsToday`, `IsCurrentMonth`, etc. columns. Set to `null` to use the actual current date at generation time. Useful for reproducible demos. | `null` |

### Include toggles

| Key | Description | Example |
|-----|-------------|---------|
| `dates.include.calendar` | Include standard Gregorian calendar columns (Year, Quarter, Month, Day, etc.). Always `true`. | `true` |
| `dates.include.iso` | Include ISO 8601 week-based calendar columns (ISOYear, ISOWeekNumber, etc.). | `false` |
| `dates.include.fiscal` | Include month-based fiscal calendar columns (FiscalYear, FiscalQuarter, FiscalMonth, etc.). | `true` |

### Weekly fiscal calendar

| Key | Description | Example |
|-----|-------------|---------|
| `dates.include.weekly_fiscal.enabled` | Include weekly fiscal calendar columns (4-4-5, 4-5-4, or 5-4-4 patterns). Common in retail and CPG industries. | `true` |
| `dates.include.weekly_fiscal.first_day_of_week` | Day the fiscal week starts. 0 = Monday, 6 = Sunday. | `0` |
| `dates.include.weekly_fiscal.weekly_type` | How the fiscal year end is determined. `"Last"` = last occurrence of the week start day. `"Nearest"` = nearest occurrence. | `"Last"` |
| `dates.include.weekly_fiscal.quarter_week_type` | Week distribution pattern across fiscal quarters. `"445"` = 4-4-5 weeks per quarter. Also supports `"454"` and `"544"`. | `"445"` |
| `dates.include.weekly_fiscal.type_start_fiscal_year` | Which calendar year the fiscal year label uses. 1 = year the fiscal year starts in. | `1` |

---

## Exchange Rates

Exchange rate dimension: currency pairs and rate simulation.

| Key | Description | Example |
|-----|-------------|---------|
| `exchange_rates.from_currencies` | List of source currency codes. Combined with `to_currencies` to determine which pairs to generate. | `["USD"]` |
| `exchange_rates.to_currencies` | List of target currency codes. Rates are generated as each `from` → each `to` pair via triangulation through USD. | `["CAD", "GBP", "EUR", "INR", "AUD", "CNY", "JPY"]` |
| `exchange_rates.base_currency` | The reference currency for the master FX file. Must be `"USD"` (master file is USD-based). | `"USD"` |
| `exchange_rates.future_annual_drift` | Annual compounding drift rate applied to project rates beyond today's date. Simulates gradual currency movement for future dates. | `0.02` |
| `exchange_rates.include_monthly` | Generate `exchange_rates_monthly.parquet` with monthly aggregated rates alongside the daily rates table. | `true` |

> Exchange rate dates are always coupled to `defaults.dates.start/end`. You cannot set FX dates independently.

---

## Budget

Budget fact tables: yearly and monthly budget forecasts by country and category.

| Key | Description | Example |
|-----|-------------|---------|
| `budget.enabled` | Master toggle for budget generation. | `true` |
| `budget.report_currency` | Currency code for budget amounts. All budget values are expressed in this currency. | `"USD"` |
| `budget.scenarios` | Named scenarios with growth adjustments applied on top of the base forecast. Each scenario produces a separate set of budget rows. | `{ Low: -0.03, Medium: 0.00, High: 0.05 }` |
| `budget.growth_caps` | Upper and lower bounds on computed growth rates. Prevents unrealistic budget swings. | `{ high: 0.30, low: -0.20 }` |
| `budget.weights` | Blending weights for the three growth signal sources. `local` = country-level historical growth. `category` = product category growth. `global` = overall dataset growth. Must sum to 1.0. | `{ local: 0.60, category: 0.30, global: 0.10 }` |
| `budget.default_backcast_growth` | Default growth rate used when insufficient historical data exists for backcasting. | `0.05` |
| `budget.return_rate_cap` | Maximum return rate applied when adjusting budget for returns. Prevents returns from exceeding this fraction of gross sales in the budget. | `0.30` |

---

## Inventory Snapshot

Inventory snapshot fact table: periodic stock levels by product and store.

### Core settings

| Key | Description | Example |
|-----|-------------|---------|
| `inventory.enabled` | Master toggle for inventory generation. | `true` |
| `inventory.seed` | Random seed for inventory simulation. | `42` |
| `inventory.grain` | Snapshot frequency. `"monthly"` = one row per product/store/month. `"quarterly"` = one per quarter. Monthly produces ~12x more rows. | `"monthly"` |
| `inventory.partition_by` | Partition columns for deltaparquet output. Set to `null` or `[]` to disable. | `["Year"]` |
| `inventory.abc_filter` | Filter products by ABC classification. `null` = include all classes. `["A", "B"]` = exclude C-class products to reduce output size. | `["A", "B"]` |
| `inventory.write_chunk_rows` | Rows per CSV chunk. Inventory compresses well, so larger chunks are fine. | `3_000_000` |

### Supply chain simulation

| Key | Description | Example |
|-----|-------------|---------|
| `inventory.min_demand_months` | Minimum months of sales history required before a product/store combination gets inventory snapshots. Avoids generating inventory for products with no demand signal. | `6` |
| `inventory.initial_stock_multiplier` | Starting inventory as a multiple of average monthly demand. 1.5 = start with 1.5 months of average demand on hand. | `1.5` |
| `inventory.reorder_compliance` | Probability that a reorder happens on time when stock drops to the reorder point. 0.65 = 35% of reorders are late or missed (realistic for most supply chains). | `0.65` |
| `inventory.lead_time_variance` | Variance in supplier delivery times as a fraction of base lead time. 0.40 = actual delivery can be +/-40% of the expected lead time. | `0.40` |
| `inventory.overstock_bias` | Multiplier on reorder quantities. 1.0 = order exactly what's needed. >1.0 = systematically over-order (builds buffer stock). <1.0 = under-order. | `1.0` |

### ABC stock multipliers

| Key | Description | Example |
|-----|-------------|---------|
| `inventory.abc_stock_multiplier` | Stock level multipliers by ABC classification. A-class (fast movers) get more safety stock, C-class (slow movers) get less attention. | `{ A: 1.20, B: 1.00, C: 0.60 }` |

### Shrinkage

| Key | Description | Example |
|-----|-------------|---------|
| `inventory.shrinkage.enabled` | Enable inventory shrinkage (theft, damage, spoilage). Reduces on-hand quantities by a small percentage each period. | `true` |
| `inventory.shrinkage.rate` | Monthly shrinkage rate as a fraction of on-hand stock. 0.02 = 2% of inventory is lost per month. | `0.02` |

---

## Scaling tips

Chart quality and data realism depend on the balance between customers, sales rows, and date range.

### Rules of thumb

- **More customers relative to rows** → spikier, more realistic charts (each customer averages fewer orders, so monthly variation is visible)
- **Fewer customers relative to rows** → smoother, flatter charts (law of large numbers averages out variance)
- **Longer date ranges** → need fewer customers for the same row count (rows spread over more months)
- **Target ~1.5 orders per customer per month**: `customers ≈ sales_rows / months / 1.5`

### Recommended customer counts

For visually interesting charts at ~1.5 orders/customer/month:

| Sales Rows | 3 years (36 mo) | 6 years (72 mo) | 10 years (120 mo) | 20 years (240 mo) |
|---|---|---|---|---|
| 2M | 37K | 19K | 11K | 6K |
| 10M | 185K | 93K | 56K | 28K |
| 20M | 370K | 185K | 111K | 56K |
| 100M | 1.85M | 925K | 555K | 278K |

### Effects on output

| Symptom | Likely cause | Fix |
|---|---|---|
| Charts look "too smooth" / flat | Too few customers relative to rows | Increase `customers` toward the table above |
| Sparse purchase history per customer | Too many customers relative to rows | Decrease `customers`, or increase `sales_rows` |
| Spiky charts with unrealistic gaps | Date range too long for row count | Increase `sales_rows` or narrow the date range |

