/*
  00_dimensions.sql – Dimension table constraints for Postgres (PKs, UKs, FKs, CHECKs).
  Hand-translated from scripts/sql/bootstrap/constraints/00_dimensions.sql.

  Idempotent: every statement is guarded; safe to re-run.

  Postgres-specific notes
  ───────────────────────
  • NONCLUSTERED is dropped (Postgres has no equivalent — every PK is
    a btree index by default).

  • Table-existence checks use ``to_regclass('"public"."X"') IS NOT NULL``
    (returns NULL if the relation is absent, no exception raised).

  • Column-existence checks use ``information_schema.columns`` instead of
    SQL Server's ``COL_LENGTH``.

  • Constraint-existence checks use ``pg_constraint`` filtered by ``conname``
    and ``contype`` ('p'=PK, 'u'=UNIQUE, 'f'=FK, 'c'=CHECK).

  • CHECK constraints of the form ``[col] IN (0, 1)`` for BIT columns are
    dropped — those columns become BOOLEAN in Postgres (already restricted
    to true/false). Non-flag CHECKs (range, enum, length) are kept.

  • The Sales/Returns/OrderHeader type-compatibility EXISTS blocks
    in the SQL Server file are dropped — the Postgres importer always runs
    against a fresh DB built by the same generator, so reference types
    always match.

  • Every statement is wrapped in a ``DO $$ … END $$;`` block because
    Postgres does not have an ``IF … BEGIN … END`` batch construct outside
    of PL/pgSQL.
*/

-----------------------------------------------------------------------
-- 1. PRIMARY KEYS
-----------------------------------------------------------------------

-- Customers PK
DO $$
BEGIN
    IF to_regclass('"public"."Customers"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_Customers')
    THEN
        ALTER TABLE "public"."Customers"
        ADD CONSTRAINT "PK_Customers" PRIMARY KEY ("CustomerKey");
    END IF;
END $$;

-- Customers — SCD2 VersionNumber CHECK (IsCurrent CHECK dropped: BOOLEAN already restricts)
DO $$
BEGIN
    IF to_regclass('"public"."Customers"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Customers' AND column_name='VersionNumber')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'CK_Customers_VersionNumber' AND contype = 'c')
    THEN
        ALTER TABLE "public"."Customers"
        ADD CONSTRAINT "CK_Customers_VersionNumber" CHECK ("VersionNumber" >= 1);
    END IF;
END $$;

-- CustomerProfile PK (1:1 with Customers)
DO $$
BEGIN
    IF to_regclass('"public"."CustomerProfile"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_CustomerProfile')
    THEN
        ALTER TABLE "public"."CustomerProfile"
        ADD CONSTRAINT "PK_CustomerProfile" PRIMARY KEY ("CustomerKey");
    END IF;
END $$;

-- OrganizationProfile PK (1:1 with org-type Customers)
DO $$
BEGIN
    IF to_regclass('"public"."OrganizationProfile"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_OrganizationProfile')
    THEN
        ALTER TABLE "public"."OrganizationProfile"
        ADD CONSTRAINT "PK_OrganizationProfile" PRIMARY KEY ("CustomerKey");
    END IF;
END $$;

-- Products PK
DO $$
BEGIN
    IF to_regclass('"public"."Products"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_Products')
    THEN
        ALTER TABLE "public"."Products"
        ADD CONSTRAINT "PK_Products" PRIMARY KEY ("ProductKey");
    END IF;
END $$;

-- Products — SCD2 VersionNumber CHECK
DO $$
BEGIN
    IF to_regclass('"public"."Products"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Products' AND column_name='VersionNumber')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'CK_Products_VersionNumber' AND contype = 'c')
    THEN
        ALTER TABLE "public"."Products"
        ADD CONSTRAINT "CK_Products_VersionNumber" CHECK ("VersionNumber" >= 1);
    END IF;
END $$;

-- ProductProfile PK (one row per product, IsCurrent=true version only)
DO $$
BEGIN
    IF to_regclass('"public"."ProductProfile"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_ProductProfile')
    THEN
        ALTER TABLE "public"."ProductProfile"
        ADD CONSTRAINT "PK_ProductProfile" PRIMARY KEY ("ProductKey");
    END IF;
END $$;

-- ProductSubcategory PK
DO $$
BEGIN
    IF to_regclass('"public"."ProductSubcategory"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_ProductSubcategory')
    THEN
        ALTER TABLE "public"."ProductSubcategory"
        ADD CONSTRAINT "PK_ProductSubcategory" PRIMARY KEY ("SubcategoryKey");
    END IF;
END $$;

-- ProductCategory PK
DO $$
BEGIN
    IF to_regclass('"public"."ProductCategory"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_ProductCategory')
    THEN
        ALTER TABLE "public"."ProductCategory"
        ADD CONSTRAINT "PK_ProductCategory" PRIMARY KEY ("CategoryKey");
    END IF;
END $$;

-- Geography PK
DO $$
BEGIN
    IF to_regclass('"public"."Geography"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_Geography')
    THEN
        ALTER TABLE "public"."Geography"
        ADD CONSTRAINT "PK_Geography" PRIMARY KEY ("GeographyKey");
    END IF;
END $$;

-- Currency PK
DO $$
BEGIN
    IF to_regclass('"public"."Currency"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_Currency')
    THEN
        ALTER TABLE "public"."Currency"
        ADD CONSTRAINT "PK_Currency" PRIMARY KEY ("CurrencyKey");
    END IF;
END $$;

-- Dates PK
DO $$
BEGIN
    IF to_regclass('"public"."Dates"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_Dates')
    THEN
        ALTER TABLE "public"."Dates"
        ADD CONSTRAINT "PK_Dates" PRIMARY KEY ("Date");
    END IF;
END $$;

-- ExchangeRates PK (composite)
DO $$
BEGIN
    IF to_regclass('"public"."ExchangeRates"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_ExchangeRates')
    THEN
        ALTER TABLE "public"."ExchangeRates"
        ADD CONSTRAINT "PK_ExchangeRates" PRIMARY KEY ("Date", "FromCurrency", "ToCurrency");
    END IF;
END $$;

-- Stores PK (column may be absent)
DO $$
BEGIN
    IF to_regclass('"public"."Stores"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Stores' AND column_name='StoreKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_Stores')
    THEN
        ALTER TABLE "public"."Stores"
        ADD CONSTRAINT "PK_Stores" PRIMARY KEY ("StoreKey");
    END IF;
END $$;

-- Warehouses PK
DO $$
BEGIN
    IF to_regclass('"public"."Warehouses"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_Warehouses')
    THEN
        ALTER TABLE "public"."Warehouses"
        ADD CONSTRAINT "PK_Warehouses" PRIMARY KEY ("WarehouseKey");
    END IF;
END $$;

-- Warehouses -> Geography
DO $$
BEGIN
    IF to_regclass('"public"."Warehouses"') IS NOT NULL
       AND to_regclass('"public"."Geography"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Warehouses' AND column_name='GeographyKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Warehouses_Geography' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Warehouses"
        ADD CONSTRAINT "FK_Warehouses_Geography"
            FOREIGN KEY ("GeographyKey")
            REFERENCES "public"."Geography" ("GeographyKey");
    END IF;
END $$;

-- Promotions PK
DO $$
BEGIN
    IF to_regclass('"public"."Promotions"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Promotions' AND column_name='PromotionKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_Promotions')
    THEN
        ALTER TABLE "public"."Promotions"
        ADD CONSTRAINT "PK_Promotions" PRIMARY KEY ("PromotionKey");
    END IF;
END $$;

-- Employees PK
DO $$
BEGIN
    IF to_regclass('"public"."Employees"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Employees' AND column_name='EmployeeKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_Employees')
    THEN
        ALTER TABLE "public"."Employees"
        ADD CONSTRAINT "PK_Employees" PRIMARY KEY ("EmployeeKey");
    END IF;
END $$;

-- EmployeeStoreAssignments PK
DO $$
BEGIN
    IF to_regclass('"public"."EmployeeStoreAssignments"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='EmployeeStoreAssignments' AND column_name='AssignmentKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_EmployeeStoreAssignments')
    THEN
        ALTER TABLE "public"."EmployeeStoreAssignments"
        ADD CONSTRAINT "PK_EmployeeStoreAssignments" PRIMARY KEY ("AssignmentKey");
    END IF;
END $$;

-- EmployeeStoreAssignments -> Employees
DO $$
BEGIN
    IF to_regclass('"public"."EmployeeStoreAssignments"') IS NOT NULL
       AND to_regclass('"public"."Employees"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='EmployeeStoreAssignments' AND column_name='EmployeeKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_EmployeeStoreAssignments_Employees' AND contype = 'f')
    THEN
        ALTER TABLE "public"."EmployeeStoreAssignments"
        ADD CONSTRAINT "FK_EmployeeStoreAssignments_Employees"
            FOREIGN KEY ("EmployeeKey")
            REFERENCES "public"."Employees" ("EmployeeKey");
    END IF;
END $$;

-- EmployeeStoreAssignments -> Stores
DO $$
BEGIN
    IF to_regclass('"public"."EmployeeStoreAssignments"') IS NOT NULL
       AND to_regclass('"public"."Stores"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='EmployeeStoreAssignments' AND column_name='StoreKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_EmployeeStoreAssignments_Stores' AND contype = 'f')
    THEN
        ALTER TABLE "public"."EmployeeStoreAssignments"
        ADD CONSTRAINT "FK_EmployeeStoreAssignments_Stores"
            FOREIGN KEY ("StoreKey")
            REFERENCES "public"."Stores" ("StoreKey");
    END IF;
END $$;

-- Suppliers PK
DO $$
BEGIN
    IF to_regclass('"public"."Suppliers"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Suppliers' AND column_name='SupplierKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_Suppliers')
    THEN
        ALTER TABLE "public"."Suppliers"
        ADD CONSTRAINT "PK_Suppliers" PRIMARY KEY ("SupplierKey");
    END IF;
END $$;

-- Channels PK
DO $$
BEGIN
    IF to_regclass('"public"."Channels"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Channels' AND column_name='ChannelKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_Channels')
    THEN
        ALTER TABLE "public"."Channels"
        ADD CONSTRAINT "PK_Channels" PRIMARY KEY ("ChannelKey");
    END IF;
END $$;

-- Time PK
DO $$
BEGIN
    IF to_regclass('"public"."Time"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Time' AND column_name='TimeKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_Time')
    THEN
        ALTER TABLE "public"."Time"
        ADD CONSTRAINT "PK_Time" PRIMARY KEY ("TimeKey");
    END IF;
END $$;

-- ReturnReason PK
DO $$
BEGIN
    IF to_regclass('"public"."ReturnReason"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='ReturnReason' AND column_name='ReturnReasonKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_ReturnReason')
    THEN
        ALTER TABLE "public"."ReturnReason"
        ADD CONSTRAINT "PK_ReturnReason" PRIMARY KEY ("ReturnReasonKey");
    END IF;
END $$;

-----------------------------------------------------------------------
-- 2. CANDIDATE KEYS FOR FK TARGETS (UNIQUE INDEXES)
--
-- These ensure a unique index exists on columns referenced by foreign
-- keys in downstream scripts.  Most are redundant in Postgres because
-- the PK already provides a unique index on the same column, but they
-- mirror the SQL Server file for parity.
-----------------------------------------------------------------------

-- Currency(CurrencyCode) – unique business key
DO $$
BEGIN
    IF to_regclass('"public"."Currency"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Currency' AND column_name='CurrencyCode')
       AND NOT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname='public' AND tablename='Currency' AND indexname='UX_Currency_CurrencyCode')
    THEN
        CREATE UNIQUE INDEX "UX_Currency_CurrencyCode"
            ON "public"."Currency" ("CurrencyCode");
    END IF;
END $$;

-----------------------------------------------------------------------
-- 3. FOREIGN KEYS
-----------------------------------------------------------------------

-- Product hierarchy: Products -> ProductSubcategory
DO $$
BEGIN
    IF to_regclass('"public"."Products"') IS NOT NULL
       AND to_regclass('"public"."ProductSubcategory"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Products_ProductSubcategory' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Products"
        ADD CONSTRAINT "FK_Products_ProductSubcategory"
            FOREIGN KEY ("SubcategoryKey")
            REFERENCES "public"."ProductSubcategory" ("SubcategoryKey");
    END IF;
END $$;

-- Product hierarchy: ProductSubcategory -> ProductCategory
DO $$
BEGIN
    IF to_regclass('"public"."ProductSubcategory"') IS NOT NULL
       AND to_regclass('"public"."ProductCategory"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_ProductSubcategory_ProductCategory' AND contype = 'f')
    THEN
        ALTER TABLE "public"."ProductSubcategory"
        ADD CONSTRAINT "FK_ProductSubcategory_ProductCategory"
            FOREIGN KEY ("CategoryKey")
            REFERENCES "public"."ProductCategory" ("CategoryKey");
    END IF;
END $$;

-- ProductProfile -> Products (1:1 on ProductKey, IsCurrent=true version only)
DO $$
BEGIN
    IF to_regclass('"public"."ProductProfile"') IS NOT NULL
       AND to_regclass('"public"."Products"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_ProductProfile_Products' AND contype = 'f')
    THEN
        ALTER TABLE "public"."ProductProfile"
        ADD CONSTRAINT "FK_ProductProfile_Products"
            FOREIGN KEY ("ProductKey")
            REFERENCES "public"."Products" ("ProductKey");
    END IF;
END $$;

-- ProductProfile -> Suppliers
DO $$
BEGIN
    IF to_regclass('"public"."ProductProfile"') IS NOT NULL
       AND to_regclass('"public"."Suppliers"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='ProductProfile' AND column_name='SupplierKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_ProductProfile_Suppliers' AND contype = 'f')
    THEN
        ALTER TABLE "public"."ProductProfile"
        ADD CONSTRAINT "FK_ProductProfile_Suppliers"
            FOREIGN KEY ("SupplierKey")
            REFERENCES "public"."Suppliers" ("SupplierKey");
    END IF;
END $$;

-- Customers -> Geography
DO $$
BEGIN
    IF to_regclass('"public"."Customers"') IS NOT NULL
       AND to_regclass('"public"."Geography"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Customers_Geography' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Customers"
        ADD CONSTRAINT "FK_Customers_Geography"
            FOREIGN KEY ("GeographyKey")
            REFERENCES "public"."Geography" ("GeographyKey");
    END IF;
END $$;

-- CustomerProfile -> Customers (1:1)
DO $$
BEGIN
    IF to_regclass('"public"."CustomerProfile"') IS NOT NULL
       AND to_regclass('"public"."Customers"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_CustomerProfile_Customers' AND contype = 'f')
    THEN
        ALTER TABLE "public"."CustomerProfile"
        ADD CONSTRAINT "FK_CustomerProfile_Customers"
            FOREIGN KEY ("CustomerKey")
            REFERENCES "public"."Customers" ("CustomerKey");
    END IF;
END $$;

-- OrganizationProfile -> Customers (1:1, org-type rows only)
DO $$
BEGIN
    IF to_regclass('"public"."OrganizationProfile"') IS NOT NULL
       AND to_regclass('"public"."Customers"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_OrganizationProfile_Customers' AND contype = 'f')
    THEN
        ALTER TABLE "public"."OrganizationProfile"
        ADD CONSTRAINT "FK_OrganizationProfile_Customers"
            FOREIGN KEY ("CustomerKey")
            REFERENCES "public"."Customers" ("CustomerKey");
    END IF;
END $$;

-- Stores -> Geography
DO $$
BEGIN
    IF to_regclass('"public"."Stores"') IS NOT NULL
       AND to_regclass('"public"."Geography"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Stores' AND column_name='GeographyKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Stores_Geography' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Stores"
        ADD CONSTRAINT "FK_Stores_Geography"
            FOREIGN KEY ("GeographyKey")
            REFERENCES "public"."Geography" ("GeographyKey");
    END IF;
END $$;

-- Stores -> Warehouses
DO $$
BEGIN
    IF to_regclass('"public"."Stores"') IS NOT NULL
       AND to_regclass('"public"."Warehouses"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Stores' AND column_name='WarehouseKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Stores_Warehouses' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Stores"
        ADD CONSTRAINT "FK_Stores_Warehouses"
            FOREIGN KEY ("WarehouseKey")
            REFERENCES "public"."Warehouses" ("WarehouseKey");
    END IF;
END $$;

-- ExchangeRates -> Currency (FromCurrencyKey)
DO $$
BEGIN
    IF to_regclass('"public"."ExchangeRates"') IS NOT NULL
       AND to_regclass('"public"."Currency"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_ExchangeRates_FromCurrency' AND contype = 'f')
    THEN
        ALTER TABLE "public"."ExchangeRates"
        ADD CONSTRAINT "FK_ExchangeRates_FromCurrency"
            FOREIGN KEY ("FromCurrencyKey")
            REFERENCES "public"."Currency" ("CurrencyKey");
    END IF;
END $$;

-- ExchangeRates -> Currency (ToCurrencyKey)
DO $$
BEGIN
    IF to_regclass('"public"."ExchangeRates"') IS NOT NULL
       AND to_regclass('"public"."Currency"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_ExchangeRates_ToCurrency' AND contype = 'f')
    THEN
        ALTER TABLE "public"."ExchangeRates"
        ADD CONSTRAINT "FK_ExchangeRates_ToCurrency"
            FOREIGN KEY ("ToCurrencyKey")
            REFERENCES "public"."Currency" ("CurrencyKey");
    END IF;
END $$;

-- ExchangeRates -> Dates (Date)
DO $$
BEGIN
    IF to_regclass('"public"."ExchangeRates"') IS NOT NULL
       AND to_regclass('"public"."Dates"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_ExchangeRates_Dates' AND contype = 'f')
    THEN
        ALTER TABLE "public"."ExchangeRates"
        ADD CONSTRAINT "FK_ExchangeRates_Dates"
            FOREIGN KEY ("Date")
            REFERENCES "public"."Dates" ("Date");
    END IF;
END $$;

-----------------------------------------------------------------------
-- 3b. EXCHANGE RATES MONTHLY (conditional – table may not exist)
-----------------------------------------------------------------------

-- ExchangeRatesMonthly PK (composite)
DO $$
BEGIN
    IF to_regclass('"public"."ExchangeRatesMonthly"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_ExchangeRatesMonthly')
    THEN
        ALTER TABLE "public"."ExchangeRatesMonthly"
        ADD CONSTRAINT "PK_ExchangeRatesMonthly"
            PRIMARY KEY ("Date", "FromCurrencyKey", "ToCurrencyKey");
    END IF;
END $$;

-- ExchangeRatesMonthly -> Currency (FromCurrencyKey)
DO $$
BEGIN
    IF to_regclass('"public"."ExchangeRatesMonthly"') IS NOT NULL
       AND to_regclass('"public"."Currency"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_ExchangeRatesMonthly_FromCurrency' AND contype = 'f')
    THEN
        ALTER TABLE "public"."ExchangeRatesMonthly"
        ADD CONSTRAINT "FK_ExchangeRatesMonthly_FromCurrency"
            FOREIGN KEY ("FromCurrencyKey")
            REFERENCES "public"."Currency" ("CurrencyKey");
    END IF;
END $$;

-- ExchangeRatesMonthly -> Currency (ToCurrencyKey)
DO $$
BEGIN
    IF to_regclass('"public"."ExchangeRatesMonthly"') IS NOT NULL
       AND to_regclass('"public"."Currency"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_ExchangeRatesMonthly_ToCurrency' AND contype = 'f')
    THEN
        ALTER TABLE "public"."ExchangeRatesMonthly"
        ADD CONSTRAINT "FK_ExchangeRatesMonthly_ToCurrency"
            FOREIGN KEY ("ToCurrencyKey")
            REFERENCES "public"."Currency" ("CurrencyKey");
    END IF;
END $$;

-----------------------------------------------------------------------
-- 4. LOYALTY TIERS
-----------------------------------------------------------------------

-- LoyaltyTiers PK
DO $$
BEGIN
    IF to_regclass('"public"."LoyaltyTiers"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='LoyaltyTiers' AND column_name='LoyaltyTierKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_LoyaltyTiers')
    THEN
        ALTER TABLE "public"."LoyaltyTiers"
        ADD CONSTRAINT "PK_LoyaltyTiers" PRIMARY KEY ("LoyaltyTierKey");
    END IF;
END $$;

-- Customers -> LoyaltyTiers FK
DO $$
BEGIN
    IF to_regclass('"public"."Customers"') IS NOT NULL
       AND to_regclass('"public"."LoyaltyTiers"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Customers' AND column_name='LoyaltyTierKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Customers_LoyaltyTiers' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Customers"
        ADD CONSTRAINT "FK_Customers_LoyaltyTiers"
            FOREIGN KEY ("LoyaltyTierKey")
            REFERENCES "public"."LoyaltyTiers" ("LoyaltyTierKey");
    END IF;
END $$;

-----------------------------------------------------------------------
-- 5. CUSTOMER ACQUISITION CHANNELS
-----------------------------------------------------------------------

-- CustomerAcquisitionChannels PK
DO $$
BEGIN
    IF to_regclass('"public"."CustomerAcquisitionChannels"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerAcquisitionChannels' AND column_name='CustomerAcquisitionChannelKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_CustomerAcquisitionChannels')
    THEN
        ALTER TABLE "public"."CustomerAcquisitionChannels"
        ADD CONSTRAINT "PK_CustomerAcquisitionChannels"
            PRIMARY KEY ("CustomerAcquisitionChannelKey");
    END IF;
END $$;

-- Customers -> CustomerAcquisitionChannels FK
DO $$
BEGIN
    IF to_regclass('"public"."Customers"') IS NOT NULL
       AND to_regclass('"public"."CustomerAcquisitionChannels"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Customers' AND column_name='CustomerAcquisitionChannelKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Customers_AcquisitionChannels' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Customers"
        ADD CONSTRAINT "FK_Customers_AcquisitionChannels"
            FOREIGN KEY ("CustomerAcquisitionChannelKey")
            REFERENCES "public"."CustomerAcquisitionChannels" ("CustomerAcquisitionChannelKey");
    END IF;
END $$;

-----------------------------------------------------------------------
-- 6. PLANS + CUSTOMER SUBSCRIPTIONS BRIDGE
-----------------------------------------------------------------------

-- Plans PK
DO $$
BEGIN
    IF to_regclass('"public"."Plans"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Plans' AND column_name='PlanKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_Plans')
    THEN
        ALTER TABLE "public"."Plans"
        ADD CONSTRAINT "PK_Plans" PRIMARY KEY ("PlanKey");
    END IF;
END $$;

-- CustomerSubscriptions PK (composite on SubscriptionKey, BillingCycleNumber)
DO $$
BEGIN
    IF to_regclass('"public"."CustomerSubscriptions"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerSubscriptions' AND column_name='SubscriptionKey')
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerSubscriptions' AND column_name='BillingCycleNumber')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_CustomerSubscriptions')
    THEN
        ALTER TABLE "public"."CustomerSubscriptions"
        ADD CONSTRAINT "PK_CustomerSubscriptions"
            PRIMARY KEY ("SubscriptionKey", "BillingCycleNumber");
    END IF;
END $$;

-- CustomerSubscriptions -> Customers FK
DO $$
BEGIN
    IF to_regclass('"public"."CustomerSubscriptions"') IS NOT NULL
       AND to_regclass('"public"."Customers"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerSubscriptions' AND column_name='CustomerKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_CustomerSubscriptions_Customers' AND contype = 'f')
    THEN
        ALTER TABLE "public"."CustomerSubscriptions"
        ADD CONSTRAINT "FK_CustomerSubscriptions_Customers"
            FOREIGN KEY ("CustomerKey")
            REFERENCES "public"."Customers" ("CustomerKey");
    END IF;
END $$;

-- CustomerSubscriptions -> Plans FK
DO $$
BEGIN
    IF to_regclass('"public"."CustomerSubscriptions"') IS NOT NULL
       AND to_regclass('"public"."Plans"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerSubscriptions' AND column_name='PlanKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_CustomerSubscriptions_Plans' AND contype = 'f')
    THEN
        ALTER TABLE "public"."CustomerSubscriptions"
        ADD CONSTRAINT "FK_CustomerSubscriptions_Plans"
            FOREIGN KEY ("PlanKey")
            REFERENCES "public"."Plans" ("PlanKey");
    END IF;
END $$;

-- Plans CHECK: BillingCycle enum
DO $$
BEGIN
    IF to_regclass('"public"."Plans"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Plans' AND column_name='BillingCycle')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'CK_Plans_BillingCycle' AND contype = 'c')
    THEN
        ALTER TABLE "public"."Plans"
        ADD CONSTRAINT "CK_Plans_BillingCycle"
            CHECK ("BillingCycle" IN ('Monthly', 'Quarterly', 'Half-Yearly', 'Annual'));
    END IF;
END $$;

-- Plans CHECK: Discount range
DO $$
BEGIN
    IF to_regclass('"public"."Plans"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Plans' AND column_name='Discount')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'CK_Plans_Discount' AND contype = 'c')
    THEN
        ALTER TABLE "public"."Plans"
        ADD CONSTRAINT "CK_Plans_Discount"
            CHECK ("Discount" >= 0 AND "Discount" < 1);
    END IF;
END $$;

-- Plans CHECK: BaseMonthlyPrice >= 0
DO $$
BEGIN
    IF to_regclass('"public"."Plans"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Plans' AND column_name='BaseMonthlyPrice')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'CK_Plans_BaseMonthlyPrice' AND contype = 'c')
    THEN
        ALTER TABLE "public"."Plans"
        ADD CONSTRAINT "CK_Plans_BaseMonthlyPrice"
            CHECK ("BaseMonthlyPrice" >= 0);
    END IF;
END $$;

-- Plans CHECK: CycleMonths enum
DO $$
BEGIN
    IF to_regclass('"public"."Plans"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Plans' AND column_name='CycleMonths')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'CK_Plans_CycleMonths' AND contype = 'c')
    THEN
        ALTER TABLE "public"."Plans"
        ADD CONSTRAINT "CK_Plans_CycleMonths"
            CHECK ("CycleMonths" IN (1, 3, 6, 12));
    END IF;
END $$;

-- CustomerSubscriptions CHECK: PeriodPrice >= 0
DO $$
BEGIN
    IF to_regclass('"public"."CustomerSubscriptions"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerSubscriptions' AND column_name='PeriodPrice')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'CK_CustomerSubscriptions_PeriodPrice' AND contype = 'c')
    THEN
        ALTER TABLE "public"."CustomerSubscriptions"
        ADD CONSTRAINT "CK_CustomerSubscriptions_PeriodPrice"
            CHECK ("PeriodPrice" >= 0);
    END IF;
END $$;

-- CustomerSubscriptions CHECK: BillingCycleNumber >= 1
DO $$
BEGIN
    IF to_regclass('"public"."CustomerSubscriptions"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerSubscriptions' AND column_name='BillingCycleNumber')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'CK_CustomerSubscriptions_BillingCycleNumber' AND contype = 'c')
    THEN
        ALTER TABLE "public"."CustomerSubscriptions"
        ADD CONSTRAINT "CK_CustomerSubscriptions_BillingCycleNumber"
            CHECK ("BillingCycleNumber" >= 1);
    END IF;
END $$;

-- (IsFirstPeriod / IsChurnPeriod / IsTrialPeriod CHECKs dropped: BIT->BOOLEAN
--  already restricts these columns to true/false.)

-----------------------------------------------------------------------
-- 7. CUSTOMER WISHLISTS BRIDGE
-----------------------------------------------------------------------

-- CustomerWishlists PK
DO $$
BEGIN
    IF to_regclass('"public"."CustomerWishlists"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerWishlists' AND column_name='WishlistKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_CustomerWishlists')
    THEN
        ALTER TABLE "public"."CustomerWishlists"
        ADD CONSTRAINT "PK_CustomerWishlists" PRIMARY KEY ("WishlistKey");
    END IF;
END $$;

-- CustomerWishlists -> Customers FK
DO $$
BEGIN
    IF to_regclass('"public"."CustomerWishlists"') IS NOT NULL
       AND to_regclass('"public"."Customers"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerWishlists' AND column_name='CustomerKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_CustomerWishlists_Customers' AND contype = 'f')
    THEN
        ALTER TABLE "public"."CustomerWishlists"
        ADD CONSTRAINT "FK_CustomerWishlists_Customers"
            FOREIGN KEY ("CustomerKey")
            REFERENCES "public"."Customers" ("CustomerKey");
    END IF;
END $$;

-- CustomerWishlists -> Products FK
DO $$
BEGIN
    IF to_regclass('"public"."CustomerWishlists"') IS NOT NULL
       AND to_regclass('"public"."Products"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerWishlists' AND column_name='ProductKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_CustomerWishlists_Products' AND contype = 'f')
    THEN
        ALTER TABLE "public"."CustomerWishlists"
        ADD CONSTRAINT "FK_CustomerWishlists_Products"
            FOREIGN KEY ("ProductKey")
            REFERENCES "public"."Products" ("ProductKey");
    END IF;
END $$;

-- CustomerWishlists CHECK: Quantity >= 1
DO $$
BEGIN
    IF to_regclass('"public"."CustomerWishlists"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerWishlists' AND column_name='Quantity')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'CK_CustomerWishlists_Quantity' AND contype = 'c')
    THEN
        ALTER TABLE "public"."CustomerWishlists"
        ADD CONSTRAINT "CK_CustomerWishlists_Quantity"
            CHECK ("Quantity" >= 1);
    END IF;
END $$;

-- CustomerWishlists CHECK: Priority enum
DO $$
BEGIN
    IF to_regclass('"public"."CustomerWishlists"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerWishlists' AND column_name='Priority')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'CK_CustomerWishlists_Priority' AND contype = 'c')
    THEN
        ALTER TABLE "public"."CustomerWishlists"
        ADD CONSTRAINT "CK_CustomerWishlists_Priority"
            CHECK ("Priority" IN ('High', 'Medium', 'Low'));
    END IF;
END $$;

-- CustomerWishlists CHECK: NetPrice >= 0
DO $$
BEGIN
    IF to_regclass('"public"."CustomerWishlists"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='CustomerWishlists' AND column_name='NetPrice')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'CK_CustomerWishlists_NetPrice' AND contype = 'c')
    THEN
        ALTER TABLE "public"."CustomerWishlists"
        ADD CONSTRAINT "CK_CustomerWishlists_NetPrice"
            CHECK ("NetPrice" >= 0);
    END IF;
END $$;

------------------------------------------------------------------------
-- 8. Complaints (optional)
------------------------------------------------------------------------

-- Complaints PK
DO $$
BEGIN
    IF to_regclass('"public"."Complaints"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Complaints' AND column_name='ComplaintKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_Complaints')
    THEN
        ALTER TABLE "public"."Complaints"
        ADD CONSTRAINT "PK_Complaints" PRIMARY KEY ("ComplaintKey");
    END IF;
END $$;

-- Complaints -> Customers FK
DO $$
BEGIN
    IF to_regclass('"public"."Complaints"') IS NOT NULL
       AND to_regclass('"public"."Customers"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Complaints' AND column_name='CustomerKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Complaints_Customers' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Complaints"
        ADD CONSTRAINT "FK_Complaints_Customers"
            FOREIGN KEY ("CustomerKey")
            REFERENCES "public"."Customers" ("CustomerKey");
    END IF;
END $$;

-- Complaints CHECK: Severity enum
DO $$
BEGIN
    IF to_regclass('"public"."Complaints"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Complaints' AND column_name='Severity')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'CK_Complaints_Severity' AND contype = 'c')
    THEN
        ALTER TABLE "public"."Complaints"
        ADD CONSTRAINT "CK_Complaints_Severity"
            CHECK ("Severity" IN ('Low', 'Medium', 'High', 'Critical'));
    END IF;
END $$;

-- Complaints CHECK: Status enum
DO $$
BEGIN
    IF to_regclass('"public"."Complaints"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Complaints' AND column_name='Status')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'CK_Complaints_Status' AND contype = 'c')
    THEN
        ALTER TABLE "public"."Complaints"
        ADD CONSTRAINT "CK_Complaints_Status"
            CHECK ("Status" IN ('Open', 'Resolved', 'Escalated', 'Closed'));
    END IF;
END $$;

-- Complaints CHECK: Channel enum
DO $$
BEGIN
    IF to_regclass('"public"."Complaints"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Complaints' AND column_name='Channel')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'CK_Complaints_Channel' AND contype = 'c')
    THEN
        ALTER TABLE "public"."Complaints"
        ADD CONSTRAINT "CK_Complaints_Channel"
            CHECK ("Channel" IN ('Email', 'Phone', 'In-Store', 'Website', 'Chat'));
    END IF;
END $$;

-- Complaints CHECK: ResponseDays >= 0
DO $$
BEGIN
    IF to_regclass('"public"."Complaints"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Complaints' AND column_name='ResponseDays')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'CK_Complaints_ResponseDays' AND contype = 'c')
    THEN
        ALTER TABLE "public"."Complaints"
        ADD CONSTRAINT "CK_Complaints_ResponseDays"
            CHECK ("ResponseDays" >= 0);
    END IF;
END $$;
