/*
  00_model_views.sql – Postgres pass-through + fact views.
  Hand-translated from scripts/sql/views/00_model_views.sql.

  Schema convention
  ─────────────────
  Tables live in ``"public"``. Views land in ``"public"`` by default
  (with ``vw_`` prefix), or in a custom schema (without prefix) when
  ``cfg.defaults.view_schema`` is overridden. The composer rewrites
  ``"public"."vw_X"`` -> ``"<schema>"."X"`` for the custom-schema flow,
  so the form used in this file is always the default form.

  Translation notes
  ─────────────────
  • Every view is guarded with ``to_regclass`` so the script is resilient
    to partial deployments (e.g. a minimal config that omits LoyaltyTiers,
    Subscriptions, etc.).
  • CREATE VIEW inside ``DO $$`` is wrapped in ``EXECUTE`` to keep
    syntax consistent and to allow the runtime-conditional vw_Sales body.
  • SQL Server ``MONEY`` is replaced with ``NUMERIC(19,4)`` — Postgres
    has a MONEY type, but its locale-dependent formatting makes it a
    poor analytics target. NUMERIC matches the SQL Server fix note.
  • ``CAST(x AS INT)`` -> ``CAST(x AS INTEGER)`` (Postgres spelling).
*/

-----------------------------------------------------------------------
-- DIMENSION VIEWS (pass-through)
-----------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('"public"."Currency"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_Currency" AS SELECT * FROM "public"."Currency"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Customers"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_Customers" AS SELECT * FROM "public"."Customers"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."CustomerProfile"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_CustomerProfile" AS SELECT * FROM "public"."CustomerProfile"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."OrganizationProfile"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_OrganizationProfile" AS SELECT * FROM "public"."OrganizationProfile"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."CustomerAcquisitionChannels"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_CustomerAcquisitionChannels" AS SELECT * FROM "public"."CustomerAcquisitionChannels"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."CustomerWishlists"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_CustomerWishlists" AS SELECT * FROM "public"."CustomerWishlists"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Complaints"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_Complaints" AS SELECT * FROM "public"."Complaints"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."CustomerSubscriptions"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_CustomerSubscriptions" AS SELECT * FROM "public"."CustomerSubscriptions"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Plans"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_Plans" AS SELECT * FROM "public"."Plans"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Dates"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_Dates" AS SELECT * FROM "public"."Dates"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Time"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_Time" AS SELECT * FROM "public"."Time"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Employees"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_Employees" AS SELECT * FROM "public"."Employees"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."EmployeeStoreAssignments"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_EmployeeStoreAssignments" AS SELECT * FROM "public"."EmployeeStoreAssignments"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."ExchangeRates"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_ExchangeRates" AS SELECT * FROM "public"."ExchangeRates"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."ExchangeRatesMonthly"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_ExchangeRatesMonthly" AS SELECT * FROM "public"."ExchangeRatesMonthly"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Geography"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_Geography" AS SELECT * FROM "public"."Geography"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."LoyaltyTiers"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_LoyaltyTiers" AS SELECT * FROM "public"."LoyaltyTiers"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."ProductCategory"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_ProductCategory" AS SELECT * FROM "public"."ProductCategory"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Products"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_Products" AS SELECT * FROM "public"."Products"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."ProductProfile"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_ProductProfile" AS SELECT * FROM "public"."ProductProfile"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."ProductSubcategory"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_ProductSubcategory" AS SELECT * FROM "public"."ProductSubcategory"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Promotions"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_Promotions" AS SELECT * FROM "public"."Promotions"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."ReturnReason"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_ReturnReason" AS SELECT * FROM "public"."ReturnReason"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Channels"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_Channels" AS SELECT * FROM "public"."Channels"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Stores"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_Stores" AS SELECT * FROM "public"."Stores"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Warehouses"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_Warehouses" AS SELECT * FROM "public"."Warehouses"';
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('"public"."Suppliers"') IS NOT NULL THEN
        EXECUTE 'CREATE OR REPLACE VIEW "public"."vw_Suppliers" AS SELECT * FROM "public"."Suppliers"';
    END IF;
END $$;

-----------------------------------------------------------------------
-- FACT VIEWS (conditional, schema-dependent)
--   - sales_output: sales       -> vw_Sales from Sales
--   - sales_output: sales_order -> vw_OrderHeader/Detail + vw_Sales from join
--   - sales_output: both        -> vw_Sales from Sales + vw_OrderHeader/Detail
-----------------------------------------------------------------------

-- vw_OrderHeader
DO $$
BEGIN
    IF to_regclass('"public"."OrderHeader"') IS NOT NULL THEN
        EXECUTE $sql$
            CREATE OR REPLACE VIEW "public"."vw_OrderHeader" AS
            SELECT
                "OrderNumber",
                "CustomerKey",
                "StoreKey",
                "EmployeeKey",
                "PromotionKey",
                "CurrencyKey",
                "ChannelKey",
                "OrderDate",
                "TimeKey",
                "IsOrderDelayed"
            FROM "public"."OrderHeader"
        $sql$;
    END IF;
END $$;

-- vw_OrderDetail
DO $$
BEGIN
    IF to_regclass('"public"."OrderDetail"') IS NOT NULL THEN
        EXECUTE $sql$
            CREATE OR REPLACE VIEW "public"."vw_OrderDetail" AS
            SELECT
                "OrderNumber",
                "OrderLineNumber",
                "ProductKey",
                "DueDate",
                "DeliveryDate",
                "Quantity",
                CAST("NetPrice"       AS NUMERIC(19, 4)) AS "NetPrice",
                CAST("UnitCost"       AS NUMERIC(19, 4)) AS "UnitCost",
                CAST("UnitPrice"      AS NUMERIC(19, 4)) AS "UnitPrice",
                CAST("DiscountAmount" AS NUMERIC(19, 4)) AS "DiscountAmount",
                "DeliveryStatus"
            FROM "public"."OrderDetail"
        $sql$;
    END IF;
END $$;

-- vw_Returns
DO $$
BEGIN
    IF to_regclass('"public"."Returns"') IS NOT NULL THEN
        EXECUTE $sql$
            CREATE OR REPLACE VIEW "public"."vw_Returns" AS
            SELECT
                "ReturnEventKey",
                "OrderNumber",
                "OrderLineNumber",
                "ReturnSequence",
                "ReturnDate",
                "ReturnReasonKey",
                "ReturnQuantity",
                CAST("ReturnNetPrice" AS NUMERIC(19, 4)) AS "ReturnNetPrice"
            FROM "public"."Returns"
        $sql$;
    END IF;
END $$;

-- vw_InventorySnapshot
DO $$
BEGIN
    IF to_regclass('"public"."InventorySnapshot"') IS NOT NULL THEN
        EXECUTE $sql$
            CREATE OR REPLACE VIEW "public"."vw_InventorySnapshot" AS
            SELECT * FROM "public"."InventorySnapshot"
        $sql$;
    END IF;
END $$;

-- vw_Sales: Sales table if present; otherwise join Header+Detail.
-- Order-cols (OrderNumber/LineNumber) are included only when the
-- Sales table has them (skip_order_cols=true strips them).
DO $$
DECLARE
    has_order_cols boolean;
    select_prefix text := '';
BEGIN
    IF to_regclass('"public"."Sales"') IS NOT NULL THEN
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'Sales' AND column_name = 'OrderNumber'
        ) AND EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'Sales' AND column_name = 'OrderLineNumber'
        )
        INTO has_order_cols;

        IF has_order_cols THEN
            select_prefix := '"OrderNumber", "OrderLineNumber", ';
        END IF;

        EXECUTE format($sql$
            CREATE OR REPLACE VIEW "public"."vw_Sales" AS
            SELECT
                %s
                "CustomerKey",
                "ProductKey",
                "StoreKey",
                "EmployeeKey",
                "PromotionKey",
                "CurrencyKey",
                CAST("ChannelKey" AS INTEGER) AS "ChannelKey",
                CAST("TimeKey"         AS INTEGER) AS "TimeKey",
                "OrderDate",
                "DueDate",
                "DeliveryDate",
                "Quantity",
                CAST("NetPrice"       AS NUMERIC(19, 4)) AS "NetPrice",
                CAST("UnitCost"       AS NUMERIC(19, 4)) AS "UnitCost",
                CAST("UnitPrice"      AS NUMERIC(19, 4)) AS "UnitPrice",
                CAST("DiscountAmount" AS NUMERIC(19, 4)) AS "DiscountAmount",
                "DeliveryStatus",
                "IsOrderDelayed"
            FROM "public"."Sales"
        $sql$, select_prefix);

    ELSIF to_regclass('"public"."OrderHeader"') IS NOT NULL
          AND to_regclass('"public"."OrderDetail"') IS NOT NULL THEN
        EXECUTE $sql$
            CREATE OR REPLACE VIEW "public"."vw_Sales" AS
            SELECT
                d."OrderNumber",
                d."OrderLineNumber",
                h."CustomerKey",
                d."ProductKey",
                h."StoreKey",
                h."EmployeeKey",
                h."PromotionKey",
                h."CurrencyKey",
                CAST(h."ChannelKey" AS INTEGER) AS "ChannelKey",
                CAST(h."TimeKey"         AS INTEGER) AS "TimeKey",
                h."OrderDate",
                d."DueDate",
                d."DeliveryDate",
                d."Quantity",
                CAST(d."NetPrice"       AS NUMERIC(19, 4)) AS "NetPrice",
                CAST(d."UnitCost"       AS NUMERIC(19, 4)) AS "UnitCost",
                CAST(d."UnitPrice"      AS NUMERIC(19, 4)) AS "UnitPrice",
                CAST(d."DiscountAmount" AS NUMERIC(19, 4)) AS "DiscountAmount",
                d."DeliveryStatus",
                h."IsOrderDelayed"
            FROM "public"."OrderDetail" d
            JOIN "public"."OrderHeader" h
              ON h."OrderNumber" = d."OrderNumber"
        $sql$;
    ELSE
        RAISE EXCEPTION
            'No fact tables found for vw_Sales. Expected Sales OR (OrderHeader + OrderDetail).';
    END IF;
END $$;
