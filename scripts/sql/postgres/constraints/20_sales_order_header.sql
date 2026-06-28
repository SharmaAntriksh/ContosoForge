/*
  20_sales_order_header.sql – OrderHeader constraints for Postgres.
  Hand-translated from scripts/sql/bootstrap/constraints/20_sales_order_header.sql.

  Columns (expected): OrderNumber, CustomerKey, StoreKey, EmployeeKey,
  OrderDate, TimeKey, ChannelKey, IsOrderDelayed.

  IsOrderDelayed maps to BOOLEAN in Postgres so the IN(0,1) CHECK is dropped.
*/

-----------------------------------------------------------------------
-- CANDIDATE KEY (required for OrderDetail -> OrderHeader FK)
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."OrderHeader"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderHeader' AND column_name='OrderNumber')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'UQ_OrderHeader_OrderNumber')
    THEN
        ALTER TABLE "public"."OrderHeader"
        ADD CONSTRAINT "UQ_OrderHeader_OrderNumber"
            UNIQUE ("OrderNumber");
    END IF;
END $$;

-----------------------------------------------------------------------
-- FOREIGN KEYS
-----------------------------------------------------------------------

-- OrderHeader -> Customers
DO $$
BEGIN
    IF to_regclass('"public"."OrderHeader"') IS NOT NULL
       AND to_regclass('"public"."Customers"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderHeader' AND column_name='CustomerKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_OrderHeader_Customers' AND contype = 'f')
    THEN
        ALTER TABLE "public"."OrderHeader"
        ADD CONSTRAINT "FK_OrderHeader_Customers"
            FOREIGN KEY ("CustomerKey")
            REFERENCES "public"."Customers" ("CustomerKey");
    END IF;
END $$;

-- OrderHeader -> Stores
DO $$
BEGIN
    IF to_regclass('"public"."OrderHeader"') IS NOT NULL
       AND to_regclass('"public"."Stores"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderHeader' AND column_name='StoreKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_OrderHeader_Stores' AND contype = 'f')
    THEN
        ALTER TABLE "public"."OrderHeader"
        ADD CONSTRAINT "FK_OrderHeader_Stores"
            FOREIGN KEY ("StoreKey")
            REFERENCES "public"."Stores" ("StoreKey");
    END IF;
END $$;

-- OrderHeader -> Employees
DO $$
BEGIN
    IF to_regclass('"public"."OrderHeader"') IS NOT NULL
       AND to_regclass('"public"."Employees"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderHeader' AND column_name='EmployeeKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_OrderHeader_Employees_EmployeeKey' AND contype = 'f')
    THEN
        ALTER TABLE "public"."OrderHeader"
        ADD CONSTRAINT "FK_OrderHeader_Employees_EmployeeKey"
            FOREIGN KEY ("EmployeeKey")
            REFERENCES "public"."Employees" ("EmployeeKey");
    END IF;
END $$;

-- OrderHeader -> Dates (OrderDate)
DO $$
BEGIN
    IF to_regclass('"public"."OrderHeader"') IS NOT NULL
       AND to_regclass('"public"."Dates"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderHeader' AND column_name='OrderDate')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_OrderHeader_Dates_OrderDate' AND contype = 'f')
    THEN
        ALTER TABLE "public"."OrderHeader"
        ADD CONSTRAINT "FK_OrderHeader_Dates_OrderDate"
            FOREIGN KEY ("OrderDate")
            REFERENCES "public"."Dates" ("Date");
    END IF;
END $$;

-- OrderHeader -> Time
DO $$
BEGIN
    IF to_regclass('"public"."OrderHeader"') IS NOT NULL
       AND to_regclass('"public"."Time"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderHeader' AND column_name='TimeKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_OrderHeader_Time' AND contype = 'f')
    THEN
        ALTER TABLE "public"."OrderHeader"
        ADD CONSTRAINT "FK_OrderHeader_Time"
            FOREIGN KEY ("TimeKey")
            REFERENCES "public"."Time" ("TimeKey");
    END IF;
END $$;

-- OrderHeader -> Channels
DO $$
BEGIN
    IF to_regclass('"public"."OrderHeader"') IS NOT NULL
       AND to_regclass('"public"."Channels"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderHeader' AND column_name='ChannelKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_OrderHeader_Channels' AND contype = 'f')
    THEN
        ALTER TABLE "public"."OrderHeader"
        ADD CONSTRAINT "FK_OrderHeader_Channels"
            FOREIGN KEY ("ChannelKey")
            REFERENCES "public"."Channels" ("ChannelKey");
    END IF;
END $$;
