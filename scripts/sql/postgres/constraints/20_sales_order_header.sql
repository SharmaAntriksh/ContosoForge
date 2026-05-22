/*
  20_sales_order_header.sql – SalesOrderHeader constraints for Postgres.
  Hand-translated from scripts/sql/bootstrap/constraints/20_sales_order_header.sql.

  Columns (expected): SalesOrderNumber, CustomerKey, StoreKey, EmployeeKey,
  OrderDate, TimeKey, SalesChannelKey, IsOrderDelayed.

  IsOrderDelayed maps to BOOLEAN in Postgres so the IN(0,1) CHECK is dropped.
*/

-----------------------------------------------------------------------
-- CANDIDATE KEY (required for SalesOrderDetail -> SalesOrderHeader FK)
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."SalesOrderHeader"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesOrderHeader' AND column_name='SalesOrderNumber')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'UQ_SalesOrderHeader_SalesOrderNumber')
    THEN
        ALTER TABLE "public"."SalesOrderHeader"
        ADD CONSTRAINT "UQ_SalesOrderHeader_SalesOrderNumber"
            UNIQUE ("SalesOrderNumber");
    END IF;
END $$;

-----------------------------------------------------------------------
-- FOREIGN KEYS
-----------------------------------------------------------------------

-- SalesOrderHeader -> Customers
DO $$
BEGIN
    IF to_regclass('"public"."SalesOrderHeader"') IS NOT NULL
       AND to_regclass('"public"."Customers"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesOrderHeader' AND column_name='CustomerKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesOrderHeader_Customers' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesOrderHeader"
        ADD CONSTRAINT "FK_SalesOrderHeader_Customers"
            FOREIGN KEY ("CustomerKey")
            REFERENCES "public"."Customers" ("CustomerKey");
    END IF;
END $$;

-- SalesOrderHeader -> Stores
DO $$
BEGIN
    IF to_regclass('"public"."SalesOrderHeader"') IS NOT NULL
       AND to_regclass('"public"."Stores"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesOrderHeader' AND column_name='StoreKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesOrderHeader_Stores' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesOrderHeader"
        ADD CONSTRAINT "FK_SalesOrderHeader_Stores"
            FOREIGN KEY ("StoreKey")
            REFERENCES "public"."Stores" ("StoreKey");
    END IF;
END $$;

-- SalesOrderHeader -> Employees
DO $$
BEGIN
    IF to_regclass('"public"."SalesOrderHeader"') IS NOT NULL
       AND to_regclass('"public"."Employees"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesOrderHeader' AND column_name='EmployeeKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesOrderHeader_Employees_EmployeeKey' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesOrderHeader"
        ADD CONSTRAINT "FK_SalesOrderHeader_Employees_EmployeeKey"
            FOREIGN KEY ("EmployeeKey")
            REFERENCES "public"."Employees" ("EmployeeKey");
    END IF;
END $$;

-- SalesOrderHeader -> Dates (OrderDate)
DO $$
BEGIN
    IF to_regclass('"public"."SalesOrderHeader"') IS NOT NULL
       AND to_regclass('"public"."Dates"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesOrderHeader' AND column_name='OrderDate')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesOrderHeader_Dates_OrderDate' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesOrderHeader"
        ADD CONSTRAINT "FK_SalesOrderHeader_Dates_OrderDate"
            FOREIGN KEY ("OrderDate")
            REFERENCES "public"."Dates" ("Date");
    END IF;
END $$;

-- SalesOrderHeader -> Time
DO $$
BEGIN
    IF to_regclass('"public"."SalesOrderHeader"') IS NOT NULL
       AND to_regclass('"public"."Time"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesOrderHeader' AND column_name='TimeKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesOrderHeader_Time' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesOrderHeader"
        ADD CONSTRAINT "FK_SalesOrderHeader_Time"
            FOREIGN KEY ("TimeKey")
            REFERENCES "public"."Time" ("TimeKey");
    END IF;
END $$;

-- SalesOrderHeader -> SalesChannels
DO $$
BEGIN
    IF to_regclass('"public"."SalesOrderHeader"') IS NOT NULL
       AND to_regclass('"public"."SalesChannels"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesOrderHeader' AND column_name='SalesChannelKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesOrderHeader_SalesChannels' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesOrderHeader"
        ADD CONSTRAINT "FK_SalesOrderHeader_SalesChannels"
            FOREIGN KEY ("SalesChannelKey")
            REFERENCES "public"."SalesChannels" ("SalesChannelKey");
    END IF;
END $$;
