/*
  10_sales.sql – Fact table constraints for Postgres: Sales + SalesReturn.
  Hand-translated from scripts/sql/bootstrap/constraints/10_sales.sql.

  Included when sales_output mode is 'sales' or 'both' (see sql_scripts.py).
  For the normalised order-split model, see 20/21/22_sales_order_*.sql.

  Postgres-specific notes
  ───────────────────────
  • Sales PK is composite (SalesOrderNumber, SalesOrderLineNumber).
    NONCLUSTERED is dropped — Postgres has no equivalent.

  • Column-existence guards on SalesOrderNumber / SalesOrderLineNumber are
    kept so the PK is silently skipped when skip_order_cols is true.

  • Column-existence guards remain on FKs that reference config-optional
    columns (SalesChannels, Time, Employees, ReturnReason).

  • The SQL Server type-compatibility EXISTS blocks for Sales -> Employees
    and SalesReturn -> ReturnReason are dropped. The Postgres importer
    always runs against a fresh DB built by the same generator, so
    reference types match by construction.
*/

-----------------------------------------------------------------------
-- 1. SALES: PRIMARY KEY (order-line grain)
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Sales' AND column_name='SalesOrderNumber')
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Sales' AND column_name='SalesOrderLineNumber')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_Sales')
    THEN
        ALTER TABLE "public"."Sales"
        ADD CONSTRAINT "PK_Sales"
            PRIMARY KEY ("SalesOrderNumber", "SalesOrderLineNumber");
    END IF;
END $$;

-----------------------------------------------------------------------
-- 2. SALES: FOREIGN KEYS (dimension links)
-----------------------------------------------------------------------

-- Sales -> Customers
DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL
       AND to_regclass('"public"."Customers"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Sales_Customers' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Sales"
        ADD CONSTRAINT "FK_Sales_Customers"
            FOREIGN KEY ("CustomerKey")
            REFERENCES "public"."Customers" ("CustomerKey");
    END IF;
END $$;

-- Sales -> Products
DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL
       AND to_regclass('"public"."Products"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Sales_Products' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Sales"
        ADD CONSTRAINT "FK_Sales_Products"
            FOREIGN KEY ("ProductKey")
            REFERENCES "public"."Products" ("ProductKey");
    END IF;
END $$;

-- Sales -> Stores
DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL
       AND to_regclass('"public"."Stores"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Sales_Stores' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Sales"
        ADD CONSTRAINT "FK_Sales_Stores"
            FOREIGN KEY ("StoreKey")
            REFERENCES "public"."Stores" ("StoreKey");
    END IF;
END $$;

-- Sales -> Employees
DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL
       AND to_regclass('"public"."Employees"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Sales' AND column_name='EmployeeKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Sales_Employees' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Sales"
        ADD CONSTRAINT "FK_Sales_Employees"
            FOREIGN KEY ("EmployeeKey")
            REFERENCES "public"."Employees" ("EmployeeKey");
    END IF;
END $$;

-- Sales -> Promotions
DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL
       AND to_regclass('"public"."Promotions"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Sales_Promotions' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Sales"
        ADD CONSTRAINT "FK_Sales_Promotions"
            FOREIGN KEY ("PromotionKey")
            REFERENCES "public"."Promotions" ("PromotionKey");
    END IF;
END $$;

-- Sales -> Currency
DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL
       AND to_regclass('"public"."Currency"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Sales_Currency' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Sales"
        ADD CONSTRAINT "FK_Sales_Currency"
            FOREIGN KEY ("CurrencyKey")
            REFERENCES "public"."Currency" ("CurrencyKey");
    END IF;
END $$;

-- Sales -> SalesChannels (config-optional dimension)
DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL
       AND to_regclass('"public"."SalesChannels"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Sales' AND column_name='SalesChannelKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Sales_SalesChannels' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Sales"
        ADD CONSTRAINT "FK_Sales_SalesChannels"
            FOREIGN KEY ("SalesChannelKey")
            REFERENCES "public"."SalesChannels" ("SalesChannelKey");
    END IF;
END $$;

-- Sales -> Time (config-optional dimension)
DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL
       AND to_regclass('"public"."Time"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Sales' AND column_name='TimeKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Sales_Time' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Sales"
        ADD CONSTRAINT "FK_Sales_Time"
            FOREIGN KEY ("TimeKey")
            REFERENCES "public"."Time" ("TimeKey");
    END IF;
END $$;

-- Sales -> Dates (OrderDate)
DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL
       AND to_regclass('"public"."Dates"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Sales_Dates_OrderDate' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Sales"
        ADD CONSTRAINT "FK_Sales_Dates_OrderDate"
            FOREIGN KEY ("OrderDate")
            REFERENCES "public"."Dates" ("Date");
    END IF;
END $$;

-- Sales -> Dates (DueDate)
DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL
       AND to_regclass('"public"."Dates"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Sales_Dates_DueDate' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Sales"
        ADD CONSTRAINT "FK_Sales_Dates_DueDate"
            FOREIGN KEY ("DueDate")
            REFERENCES "public"."Dates" ("Date");
    END IF;
END $$;

-- Sales -> Dates (DeliveryDate)
DO $$
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL
       AND to_regclass('"public"."Dates"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Sales_Dates_DeliveryDate' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Sales"
        ADD CONSTRAINT "FK_Sales_Dates_DeliveryDate"
            FOREIGN KEY ("DeliveryDate")
            REFERENCES "public"."Dates" ("Date");
    END IF;
END $$;

-----------------------------------------------------------------------
-- 3. SALES: CHECK CONSTRAINTS
--
-- IsOrderDelayed maps to BOOLEAN in Postgres, so the IN(0,1) CHECK from
-- SQL Server is omitted.  No remaining CHECK constraints on Sales.
-----------------------------------------------------------------------

-----------------------------------------------------------------------
-- 4. SALESRETURN: PRIMARY KEY + INDEXES
-----------------------------------------------------------------------

-- PK: ReturnEventKey (surrogate; only when column exists)
DO $$
BEGIN
    IF to_regclass('"public"."SalesReturn"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesReturn' AND column_name='ReturnEventKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_SalesReturn')
    THEN
        ALTER TABLE "public"."SalesReturn"
        ADD CONSTRAINT "PK_SalesReturn" PRIMARY KEY ("ReturnEventKey");
    END IF;
END $$;

-- Natural-key access path (non-unique; supports joins back to Sales)
DO $$
BEGIN
    IF to_regclass('"public"."SalesReturn"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesReturn' AND column_name='SalesOrderNumber')
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesReturn' AND column_name='SalesOrderLineNumber')
       AND NOT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname='public' AND tablename='SalesReturn' AND indexname='IX_SalesReturn_NaturalKey')
    THEN
        CREATE INDEX "IX_SalesReturn_NaturalKey"
        ON "public"."SalesReturn" ("SalesOrderNumber", "SalesOrderLineNumber", "ReturnSequence");
    END IF;
END $$;

-----------------------------------------------------------------------
-- 5. SALESRETURN: FOREIGN KEYS
-----------------------------------------------------------------------

-- SalesReturn -> Sales (composite natural key)
DO $$
BEGIN
    IF to_regclass('"public"."SalesReturn"') IS NOT NULL
       AND to_regclass('"public"."Sales"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesReturn' AND column_name='SalesOrderNumber')
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesReturn' AND column_name='SalesOrderLineNumber')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesReturn_Sales' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesReturn"
        ADD CONSTRAINT "FK_SalesReturn_Sales"
            FOREIGN KEY ("SalesOrderNumber", "SalesOrderLineNumber")
            REFERENCES "public"."Sales" ("SalesOrderNumber", "SalesOrderLineNumber");
    END IF;
END $$;

-- SalesReturn -> Dates (ReturnDate)
DO $$
BEGIN
    IF to_regclass('"public"."SalesReturn"') IS NOT NULL
       AND to_regclass('"public"."Dates"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesReturn' AND column_name='ReturnDate')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesReturn_Dates_ReturnDate' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesReturn"
        ADD CONSTRAINT "FK_SalesReturn_Dates_ReturnDate"
            FOREIGN KEY ("ReturnDate")
            REFERENCES "public"."Dates" ("Date");
    END IF;
END $$;

-- SalesReturn -> ReturnReason
DO $$
BEGIN
    IF to_regclass('"public"."SalesReturn"') IS NOT NULL
       AND to_regclass('"public"."ReturnReason"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesReturn' AND column_name='ReturnReasonKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesReturn_ReturnReason' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesReturn"
        ADD CONSTRAINT "FK_SalesReturn_ReturnReason"
            FOREIGN KEY ("ReturnReasonKey")
            REFERENCES "public"."ReturnReason" ("ReturnReasonKey");
    END IF;
END $$;
