/*
  40_inventory.sql – InventorySnapshot constraints for Postgres.
  Hand-translated from scripts/sql/bootstrap/constraints/40_inventory.sql.

  Composite PK on (ProductKey, WarehouseKey, SnapshotDate) + FKs to
  Products and Warehouses.
*/

-----------------------------------------------------------------------
-- 1. INVENTORYSNAPSHOT: PRIMARY KEY
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."InventorySnapshot"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_InventorySnapshot')
    THEN
        ALTER TABLE "public"."InventorySnapshot"
        ADD CONSTRAINT "PK_InventorySnapshot"
            PRIMARY KEY ("ProductKey", "WarehouseKey", "SnapshotDate");
    END IF;
END $$;

-----------------------------------------------------------------------
-- 2. INVENTORYSNAPSHOT: FOREIGN KEYS
-----------------------------------------------------------------------

-- InventorySnapshot -> Products
DO $$
BEGIN
    IF to_regclass('"public"."InventorySnapshot"') IS NOT NULL
       AND to_regclass('"public"."Products"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_InventorySnapshot_Products' AND contype = 'f')
    THEN
        ALTER TABLE "public"."InventorySnapshot"
        ADD CONSTRAINT "FK_InventorySnapshot_Products"
            FOREIGN KEY ("ProductKey")
            REFERENCES "public"."Products" ("ProductKey");
    END IF;
END $$;

-- InventorySnapshot -> Warehouses
DO $$
BEGIN
    IF to_regclass('"public"."InventorySnapshot"') IS NOT NULL
       AND to_regclass('"public"."Warehouses"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'FK_InventorySnapshot_Warehouses' AND contype = 'f')
    THEN
        ALTER TABLE "public"."InventorySnapshot"
        ADD CONSTRAINT "FK_InventorySnapshot_Warehouses"
            FOREIGN KEY ("WarehouseKey")
            REFERENCES "public"."Warehouses" ("WarehouseKey");
    END IF;
END $$;
