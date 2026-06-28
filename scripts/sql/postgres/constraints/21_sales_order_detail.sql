/*
  21_sales_order_detail.sql – OrderDetail constraints for Postgres.
  Hand-translated from scripts/sql/bootstrap/constraints/21_sales_order_detail.sql.
*/

-----------------------------------------------------------------------
-- CANDIDATE KEY (supports Returns -> OrderDetail FK)
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."OrderDetail"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderDetail' AND column_name='OrderNumber')
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderDetail' AND column_name='OrderLineNumber')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'UQ_OrderDetail_OrderLine')
    THEN
        ALTER TABLE "public"."OrderDetail"
        ADD CONSTRAINT "UQ_OrderDetail_OrderLine"
            UNIQUE ("OrderNumber", "OrderLineNumber");
    END IF;
END $$;

-----------------------------------------------------------------------
-- FOREIGN KEYS
-----------------------------------------------------------------------

-- OrderDetail -> Products
DO $$
BEGIN
    IF to_regclass('"public"."OrderDetail"') IS NOT NULL
       AND to_regclass('"public"."Products"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderDetail' AND column_name='ProductKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_OrderDetail_Products' AND contype = 'f')
    THEN
        ALTER TABLE "public"."OrderDetail"
        ADD CONSTRAINT "FK_OrderDetail_Products"
            FOREIGN KEY ("ProductKey")
            REFERENCES "public"."Products" ("ProductKey");
    END IF;
END $$;

-- OrderDetail -> Promotions
DO $$
BEGIN
    IF to_regclass('"public"."OrderDetail"') IS NOT NULL
       AND to_regclass('"public"."Promotions"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderDetail' AND column_name='PromotionKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_OrderDetail_Promotions' AND contype = 'f')
    THEN
        ALTER TABLE "public"."OrderDetail"
        ADD CONSTRAINT "FK_OrderDetail_Promotions"
            FOREIGN KEY ("PromotionKey")
            REFERENCES "public"."Promotions" ("PromotionKey");
    END IF;
END $$;

-- OrderDetail -> Currency
DO $$
BEGIN
    IF to_regclass('"public"."OrderDetail"') IS NOT NULL
       AND to_regclass('"public"."Currency"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderDetail' AND column_name='CurrencyKey')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_OrderDetail_Currency' AND contype = 'f')
    THEN
        ALTER TABLE "public"."OrderDetail"
        ADD CONSTRAINT "FK_OrderDetail_Currency"
            FOREIGN KEY ("CurrencyKey")
            REFERENCES "public"."Currency" ("CurrencyKey");
    END IF;
END $$;

-- OrderDetail -> Dates (DueDate)
DO $$
BEGIN
    IF to_regclass('"public"."OrderDetail"') IS NOT NULL
       AND to_regclass('"public"."Dates"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderDetail' AND column_name='DueDate')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_OrderDetail_Dates_DueDate' AND contype = 'f')
    THEN
        ALTER TABLE "public"."OrderDetail"
        ADD CONSTRAINT "FK_OrderDetail_Dates_DueDate"
            FOREIGN KEY ("DueDate")
            REFERENCES "public"."Dates" ("Date");
    END IF;
END $$;

-- OrderDetail -> Dates (DeliveryDate)
DO $$
BEGIN
    IF to_regclass('"public"."OrderDetail"') IS NOT NULL
       AND to_regclass('"public"."Dates"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderDetail' AND column_name='DeliveryDate')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_OrderDetail_Dates_DeliveryDate' AND contype = 'f')
    THEN
        ALTER TABLE "public"."OrderDetail"
        ADD CONSTRAINT "FK_OrderDetail_Dates_DeliveryDate"
            FOREIGN KEY ("DeliveryDate")
            REFERENCES "public"."Dates" ("Date");
    END IF;
END $$;
