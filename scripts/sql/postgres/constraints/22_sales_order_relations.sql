/*
  22_sales_order_relations.sql – Inter-fact relations for Postgres:
    OrderDetail -> OrderHeader
    Returns      -> OrderDetail

  Hand-translated from scripts/sql/bootstrap/constraints/22_sales_order_relations.sql.
*/

-----------------------------------------------------------------------
-- OrderDetail -> OrderHeader
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."OrderDetail"') IS NOT NULL
       AND to_regclass('"public"."OrderHeader"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='OrderDetail' AND column_name='OrderNumber')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_OrderDetail_OrderHeader' AND contype = 'f')
    THEN
        ALTER TABLE "public"."OrderDetail"
        ADD CONSTRAINT "FK_OrderDetail_OrderHeader"
            FOREIGN KEY ("OrderNumber")
            REFERENCES "public"."OrderHeader" ("OrderNumber");
    END IF;
END $$;

-----------------------------------------------------------------------
-- Returns -> OrderDetail (natural key join)
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."Returns"') IS NOT NULL
       AND to_regclass('"public"."OrderDetail"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Returns' AND column_name='OrderNumber')
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='Returns' AND column_name='OrderLineNumber')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_Returns_OrderDetail' AND contype = 'f')
    THEN
        ALTER TABLE "public"."Returns"
        ADD CONSTRAINT "FK_Returns_OrderDetail"
            FOREIGN KEY ("OrderNumber", "OrderLineNumber")
            REFERENCES "public"."OrderDetail" ("OrderNumber", "OrderLineNumber");
    END IF;
END $$;
