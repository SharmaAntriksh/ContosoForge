/*
  22_sales_order_relations.sql – Inter-fact relations for Postgres:
    SalesOrderDetail -> SalesOrderHeader
    SalesReturn      -> SalesOrderDetail

  Hand-translated from scripts/sql/bootstrap/constraints/22_sales_order_relations.sql.
*/

-----------------------------------------------------------------------
-- SalesOrderDetail -> SalesOrderHeader
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."SalesOrderDetail"') IS NOT NULL
       AND to_regclass('"public"."SalesOrderHeader"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesOrderDetail' AND column_name='SalesOrderNumber')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesOrderDetail_SalesOrderHeader' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesOrderDetail"
        ADD CONSTRAINT "FK_SalesOrderDetail_SalesOrderHeader"
            FOREIGN KEY ("SalesOrderNumber")
            REFERENCES "public"."SalesOrderHeader" ("SalesOrderNumber");
    END IF;
END $$;

-----------------------------------------------------------------------
-- SalesReturn -> SalesOrderDetail (natural key join)
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."SalesReturn"') IS NOT NULL
       AND to_regclass('"public"."SalesOrderDetail"') IS NOT NULL
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesReturn' AND column_name='SalesOrderNumber')
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='SalesReturn' AND column_name='SalesOrderLineNumber')
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_SalesReturn_SalesOrderDetail' AND contype = 'f')
    THEN
        ALTER TABLE "public"."SalesReturn"
        ADD CONSTRAINT "FK_SalesReturn_SalesOrderDetail"
            FOREIGN KEY ("SalesOrderNumber", "SalesOrderLineNumber")
            REFERENCES "public"."SalesOrderDetail" ("SalesOrderNumber", "SalesOrderLineNumber");
    END IF;
END $$;
