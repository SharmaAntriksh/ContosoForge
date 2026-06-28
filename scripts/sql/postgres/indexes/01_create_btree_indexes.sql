/*
  01_create_btree_indexes.sql – Btree indexes on FK source columns (Postgres).

  Postgres has no clustered columnstore equivalent, so joining a fact
  table to a dimension without an index on the FK column forces a
  sequential scan of the fact. This script creates one non-unique btree
  per FK column, which is what the planner needs to pick nested-loop
  and merge joins on selective filters.

  Scope
  ─────
  • Fact tables (Sales, Returns, OrderHeader, OrderDetail,
    InventorySnapshot) — every FK source column that isn't already the
    leading column of a PK / UQ.
  • Dimension tables — FK columns to other dims (cheap because dim
    tables are small, useful for joins like Customers -> Geography).

  Applied after the load phase. Building these indexes during COPY
  would force per-row updates and crush throughput; deferring to a
  post-load batch lets Postgres build them with parallel sort + merge.

  Every statement is guarded by ``to_regclass`` (skip if table absent)
  and ``information_schema.columns`` (skip if column absent) so optional
  dims and skip_order_cols configurations work without errors. The
  index itself uses ``CREATE INDEX IF NOT EXISTS`` for idempotency.
*/

-----------------------------------------------------------------------
-- DIMENSION-TO-DIMENSION FKs
-----------------------------------------------------------------------

-- Warehouses -> Geography
DO $$
BEGIN
    IF to_regclass('"public"."Warehouses"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Warehouses' AND column_name='GeographyKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_Warehouses_GeographyKey"
            ON "public"."Warehouses" ("GeographyKey");
    END IF;
END $$;

-- EmployeeStoreAssignments -> Employees / Stores
DO $$
BEGIN
    IF to_regclass('"public"."EmployeeStoreAssignments"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='EmployeeStoreAssignments' AND column_name='EmployeeKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_EmployeeStoreAssignments_EmployeeKey"
            ON "public"."EmployeeStoreAssignments" ("EmployeeKey");
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."EmployeeStoreAssignments"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='EmployeeStoreAssignments' AND column_name='StoreKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_EmployeeStoreAssignments_StoreKey"
            ON "public"."EmployeeStoreAssignments" ("StoreKey");
    END IF;
END $$;

-- Product hierarchy
DO $$
BEGIN
    IF to_regclass('"public"."Products"') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS "IX_Products_SubcategoryKey"
            ON "public"."Products" ("SubcategoryKey");
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."ProductSubcategory"') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS "IX_ProductSubcategory_CategoryKey"
            ON "public"."ProductSubcategory" ("CategoryKey");
    END IF;
END $$;

-- ProductProfile -> Suppliers (ProductKey is already the PK)
DO $$
BEGIN
    IF to_regclass('"public"."ProductProfile"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='ProductProfile' AND column_name='SupplierKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_ProductProfile_SupplierKey"
            ON "public"."ProductProfile" ("SupplierKey");
    END IF;
END $$;

-- Customers FKs
DO $$
BEGIN
    IF to_regclass('"public"."Customers"') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS "IX_Customers_GeographyKey"
            ON "public"."Customers" ("GeographyKey");
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Customers"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Customers' AND column_name='LoyaltyTierKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_Customers_LoyaltyTierKey"
            ON "public"."Customers" ("LoyaltyTierKey");
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Customers"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Customers' AND column_name='CustomerAcquisitionChannelKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_Customers_CustomerAcquisitionChannelKey"
            ON "public"."Customers" ("CustomerAcquisitionChannelKey");
    END IF;
END $$;

-- Stores FKs
DO $$
BEGIN
    IF to_regclass('"public"."Stores"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Stores' AND column_name='GeographyKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_Stores_GeographyKey"
            ON "public"."Stores" ("GeographyKey");
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Stores"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Stores' AND column_name='WarehouseKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_Stores_WarehouseKey"
            ON "public"."Stores" ("WarehouseKey");
    END IF;
END $$;

-- ExchangeRates currency FKs (Date is already in the composite PK leading position)
DO $$
BEGIN
    IF to_regclass('"public"."ExchangeRates"') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS "IX_ExchangeRates_FromCurrencyKey"
            ON "public"."ExchangeRates" ("FromCurrencyKey");
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."ExchangeRates"') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS "IX_ExchangeRates_ToCurrencyKey"
            ON "public"."ExchangeRates" ("ToCurrencyKey");
    END IF;
END $$;

-- CustomerSubscriptions FKs (SubscriptionKey + BillingCycleNumber is the composite PK)
DO $$
BEGIN
    IF to_regclass('"public"."CustomerSubscriptions"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerSubscriptions' AND column_name='CustomerKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_CustomerSubscriptions_CustomerKey"
            ON "public"."CustomerSubscriptions" ("CustomerKey");
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."CustomerSubscriptions"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerSubscriptions' AND column_name='PlanKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_CustomerSubscriptions_PlanKey"
            ON "public"."CustomerSubscriptions" ("PlanKey");
    END IF;
END $$;

-- CustomerWishlists FKs
DO $$
BEGIN
    IF to_regclass('"public"."CustomerWishlists"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerWishlists' AND column_name='CustomerKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_CustomerWishlists_CustomerKey"
            ON "public"."CustomerWishlists" ("CustomerKey");
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."CustomerWishlists"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerWishlists' AND column_name='ProductKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_CustomerWishlists_ProductKey"
            ON "public"."CustomerWishlists" ("ProductKey");
    END IF;
END $$;

-- Complaints -> Customers
DO $$
BEGIN
    IF to_regclass('"public"."Complaints"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Complaints' AND column_name='CustomerKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_Complaints_CustomerKey"
            ON "public"."Complaints" ("CustomerKey");
    END IF;
END $$;

-----------------------------------------------------------------------
-- SALES (flat) FACT FK COLUMNS
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS "IX_Sales_CustomerKey"
            ON "public"."Sales" ("CustomerKey");
        CREATE INDEX IF NOT EXISTS "IX_Sales_ProductKey"
            ON "public"."Sales" ("ProductKey");
        CREATE INDEX IF NOT EXISTS "IX_Sales_StoreKey"
            ON "public"."Sales" ("StoreKey");
        CREATE INDEX IF NOT EXISTS "IX_Sales_PromotionKey"
            ON "public"."Sales" ("PromotionKey");
        CREATE INDEX IF NOT EXISTS "IX_Sales_CurrencyKey"
            ON "public"."Sales" ("CurrencyKey");
        CREATE INDEX IF NOT EXISTS "IX_Sales_OrderDate"
            ON "public"."Sales" ("OrderDate");
        CREATE INDEX IF NOT EXISTS "IX_Sales_DueDate"
            ON "public"."Sales" ("DueDate");
        CREATE INDEX IF NOT EXISTS "IX_Sales_DeliveryDate"
            ON "public"."Sales" ("DeliveryDate");
    END IF;
END $$;

-- Conditional Sales FK columns
DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Sales' AND column_name='EmployeeKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_Sales_EmployeeKey"
            ON "public"."Sales" ("EmployeeKey");
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Sales' AND column_name='ChannelKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_Sales_ChannelKey"
            ON "public"."Sales" ("ChannelKey");
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Sales' AND column_name='TimeKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_Sales_TimeKey"
            ON "public"."Sales" ("TimeKey");
    END IF;
END $$;

-----------------------------------------------------------------------
-- SALESRETURN FACT FK COLUMNS
-- (OrderNumber + OrderLineNumber composite is already
--  indexed via IX_Returns_NaturalKey from the constraints script.)
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."Returns"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Returns' AND column_name='ReturnDate')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_Returns_ReturnDate"
            ON "public"."Returns" ("ReturnDate");
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Returns"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Returns' AND column_name='ReturnReasonKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_Returns_ReturnReasonKey"
            ON "public"."Returns" ("ReturnReasonKey");
    END IF;
END $$;

-----------------------------------------------------------------------
-- SALESORDERHEADER FK COLUMNS
-- (OrderNumber has UQ_OrderHeader_OrderNumber.)
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."OrderHeader"') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS "IX_OrderHeader_CustomerKey"
            ON "public"."OrderHeader" ("CustomerKey");
        CREATE INDEX IF NOT EXISTS "IX_OrderHeader_StoreKey"
            ON "public"."OrderHeader" ("StoreKey");
        CREATE INDEX IF NOT EXISTS "IX_OrderHeader_OrderDate"
            ON "public"."OrderHeader" ("OrderDate");
    END IF;
END $$;

-- Conditional OrderHeader FK columns
DO $$
BEGIN
    IF to_regclass('"public"."OrderHeader"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderHeader' AND column_name='EmployeeKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_OrderHeader_EmployeeKey"
            ON "public"."OrderHeader" ("EmployeeKey");
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."OrderHeader"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderHeader' AND column_name='TimeKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_OrderHeader_TimeKey"
            ON "public"."OrderHeader" ("TimeKey");
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."OrderHeader"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderHeader' AND column_name='ChannelKey')
    THEN
        CREATE INDEX IF NOT EXISTS "IX_OrderHeader_ChannelKey"
            ON "public"."OrderHeader" ("ChannelKey");
    END IF;
END $$;

-----------------------------------------------------------------------
-- SALESORDERDETAIL FK COLUMNS
-- (OrderNumber leads UQ_OrderDetail_OrderLine, so it's
--  already indexed for joins on that column alone.)
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."OrderDetail"') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS "IX_OrderDetail_ProductKey"
            ON "public"."OrderDetail" ("ProductKey");
        CREATE INDEX IF NOT EXISTS "IX_OrderDetail_PromotionKey"
            ON "public"."OrderDetail" ("PromotionKey");
        CREATE INDEX IF NOT EXISTS "IX_OrderDetail_CurrencyKey"
            ON "public"."OrderDetail" ("CurrencyKey");
        CREATE INDEX IF NOT EXISTS "IX_OrderDetail_DueDate"
            ON "public"."OrderDetail" ("DueDate");
        CREATE INDEX IF NOT EXISTS "IX_OrderDetail_DeliveryDate"
            ON "public"."OrderDetail" ("DeliveryDate");
    END IF;
END $$;

-----------------------------------------------------------------------
-- INVENTORYSNAPSHOT FK COLUMNS
-- (ProductKey leads the composite PK so joins on ProductKey alone
--  are already covered. WarehouseKey is the 2nd PK column → not
--  covered as a leading prefix, so add a standalone index.)
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."InventorySnapshot"') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS "IX_InventorySnapshot_WarehouseKey"
            ON "public"."InventorySnapshot" ("WarehouseKey");
    END IF;
END $$;
