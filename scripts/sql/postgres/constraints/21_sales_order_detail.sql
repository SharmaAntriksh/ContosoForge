/*
  21_sales_order_detail.sql – SalesOrderDetail constraints for Postgres.
  Hand-translated from scripts/sql/bootstrap/constraints/21_sales_order_detail.sql.
*/

-----------------------------------------------------------------------
-- CANDIDATE KEY (supports SalesReturn -> SalesOrderDetail FK)
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."SalesOrderDetail"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesOrderDetail' AND column_name='SalesOrderNumber')
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesOrderDetail' AND column_name='SalesOrderLineNumber')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'UQ_SalesOrderDetail_OrderLine')
    THEN
        ALTER TABLE "public"."SalesOrderDetail"
        ADD CONSTRAINT "UQ_SalesOrderDetail_OrderLine"
            UNIQUE ("SalesOrderNumber", "SalesOrderLineNumber");
    END IF;
END $$;

-----------------------------------------------------------------------
-- FOREIGN KEYS
-----------------------------------------------------------------------

-- SalesOrderDetail -> Products
DO $$
BEGIN
    IF to_regclass('"public"."SalesOrderDetail"') IS NOT NULL
       AND to_regclass('"public"."Products"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesOrderDetail' AND column_name='ProductKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesOrderDetail_Products' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesOrderDetail"
        ADD CONSTRAINT "FK_SalesOrderDetail_Products"
            FOREIGN KEY ("ProductKey")
            REFERENCES "public"."Products" ("ProductKey");
    END IF;
END $$;

-- SalesOrderDetail -> Promotions
DO $$
BEGIN
    IF to_regclass('"public"."SalesOrderDetail"') IS NOT NULL
       AND to_regclass('"public"."Promotions"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesOrderDetail' AND column_name='PromotionKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesOrderDetail_Promotions' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesOrderDetail"
        ADD CONSTRAINT "FK_SalesOrderDetail_Promotions"
            FOREIGN KEY ("PromotionKey")
            REFERENCES "public"."Promotions" ("PromotionKey");
    END IF;
END $$;

-- SalesOrderDetail -> Currency
DO $$
BEGIN
    IF to_regclass('"public"."SalesOrderDetail"') IS NOT NULL
       AND to_regclass('"public"."Currency"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesOrderDetail' AND column_name='CurrencyKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesOrderDetail_Currency' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesOrderDetail"
        ADD CONSTRAINT "FK_SalesOrderDetail_Currency"
            FOREIGN KEY ("CurrencyKey")
            REFERENCES "public"."Currency" ("CurrencyKey");
    END IF;
END $$;

-- SalesOrderDetail -> Dates (DueDate)
DO $$
BEGIN
    IF to_regclass('"public"."SalesOrderDetail"') IS NOT NULL
       AND to_regclass('"public"."Dates"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesOrderDetail' AND column_name='DueDate')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesOrderDetail_Dates_DueDate' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesOrderDetail"
        ADD CONSTRAINT "FK_SalesOrderDetail_Dates_DueDate"
            FOREIGN KEY ("DueDate")
            REFERENCES "public"."Dates" ("Date");
    END IF;
END $$;

-- SalesOrderDetail -> Dates (DeliveryDate)
DO $$
BEGIN
    IF to_regclass('"public"."SalesOrderDetail"') IS NOT NULL
       AND to_regclass('"public"."Dates"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesOrderDetail' AND column_name='DeliveryDate')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesOrderDetail_Dates_DeliveryDate' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesOrderDetail"
        ADD CONSTRAINT "FK_SalesOrderDetail_Dates_DeliveryDate"
            FOREIGN KEY ("DeliveryDate")
            REFERENCES "public"."Dates" ("Date");
    END IF;
END $$;
