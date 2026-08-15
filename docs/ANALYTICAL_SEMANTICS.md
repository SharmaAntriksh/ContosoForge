# Analytical measure and event semantics

This document defines the generator contracts that matter when aggregating the
model or validating a generated dataset.

## Monetary fields

- `Sales.UnitPrice`, `Sales.UnitCost`, and `Sales.NetPrice` are **per-unit**
  values. Sales line revenue is `NetPrice * Quantity`; line cost is
  `UnitCost * Quantity`.
- `Sales.DiscountAmount` is a per-unit discount and satisfies
  `UnitPrice - DiscountAmount = NetPrice` after currency rounding.
- `Returns.ReturnNetPrice` retains its compatibility name but is a **summable
  return-event amount**: `Sales.NetPrice * ReturnQuantity`. Sum the field
  directly; do not multiply it by `ReturnQuantity` again.

## Dates and customer lifecycle

- Sales eligibility is exact to the day and inclusive:
  `CustomerStartDate <= OrderDate <= CustomerEndDate` when an end date exists.
- Sales and subscriptions stop at `defaults.dates.end`.
- `dates.post_sale_tail_days` extends the Date dimension only, allowing genuine
  post-sale returns, complaints, and resolutions to remain date-conformed. It
  does not create extra sales demand months.
- The quality report verifies that return, complaint, and resolution dates all
  fall inside the generated Date dimension.

## Promotions

- `Promotions.DiscountPct` is the **nominal target depth** associated with the
  selected promotion identity.
- Realized depth is `Sales.DiscountAmount / Sales.UnitPrice`. It can be lower
  than nominal when minimum-net-price or margin constraints cap the discount.
- Date, product/category, channel, loyalty, and exact new-customer-window rules
  are eligibility constraints; promotion salience affects selection among
  eligible promotions only.

## Wishlists

- The reported conversion definition is a purchase of the same
  customer/product pair on or after `AddedDate`.
- Converted wishlist rows are anchored to a stable purchase date; the configured
  `conversion_rate` targets this dated definition, not lifetime pair overlap.
- `pre_browse_days` permits browsing before acquisition within the documented
  lookback window.

## Inventory

- `StockoutFlag = true` means demand exceeded available stock during the
  snapshot period.
- `DaysOutOfStock` estimates the resulting whole days without stock, rounded up
  and capped at 30 for monthly grain (90 after quarterly aggregation).
- A stockout always has at least one out-of-stock day; a non-stockout has zero.

## Currency and geography

- Geography `ISOCode` contains the local three-letter currency code used to join
  Geography, Currency, Stores, Sales, and Exchange Rates.
- Missing currency joins are fatal. The generator never silently substitutes
  USD or the first currency in the dimension.
- Norway, Poland, Sweden, and Switzerland use NOK, PLN, SEK, and CHF respectively.
