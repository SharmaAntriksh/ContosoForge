/*
  10_sales.sql – Fact table constraints: Sales + Returns
  Aligned to src/utils/static_schemas.py (_SALES_SCHEMA, FACT_SCHEMAS["Returns"])

  Included when sales_output mode is 'sales' or 'both' (see sql_scripts.py).
  For the normalised order-split model, see 20/21/22_sales_order_*.sql.

  Design notes
  ────────────
  • All primary keys are NONCLUSTERED to coexist with the clustered
    columnstore index applied by create_drop_cci.sql.

  • COL_LENGTH guards are applied to fact columns that reference
    config-optional dimensions (Channels, Time, Employees) so the FK
    is silently skipped when the column is absent.

  • The Sales PK and Returns natural-key index are also guarded by
    COL_LENGTH on OrderNumber / OrderLineNumber so they are
    silently skipped when skip_order_cols is true.

  • The Employees FK uses a full type-compatibility check (system_type_id,
    user_type_id, max_length, precision, scale) because older configs
    may still have EmployeeKey as BIGINT while
    Employees.EmployeeKey is INT.  This matches the pattern in
    20_sales_order_header.sql.

  • Returns is linked back to Sales via the composite natural key
    (OrderNumber, OrderLineNumber).  In 'sales_order' mode
    this relationship is handled by 22_sales_order_relations.sql instead.

  • Each logical section is separated by GO so a failure in one batch
    does not roll back unrelated constraints.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;

-----------------------------------------------------------------------
-- 1. SALES: PRIMARY KEY (order-line grain)
-----------------------------------------------------------------------

IF OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.Sales', N'OrderNumber') IS NOT NULL
AND COL_LENGTH(N'dbo.Sales', N'OrderLineNumber') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.key_constraints
    WHERE name = N'PK_Sales'
      AND parent_object_id = OBJECT_ID(N'dbo.Sales')
)
BEGIN
    ALTER TABLE dbo.Sales
    ADD CONSTRAINT PK_Sales
        PRIMARY KEY NONCLUSTERED ([OrderNumber], [OrderLineNumber]);
END;
GO

-----------------------------------------------------------------------
-- 2. SALES: FOREIGN KEYS (dimension links)
-----------------------------------------------------------------------
SET NOCOUNT ON;
SET XACT_ABORT ON;

-- Sales -> Customers
IF OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Customers', N'U') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_Sales_Customers'
      AND parent_object_id = OBJECT_ID(N'dbo.Sales')
)
BEGIN
    ALTER TABLE dbo.Sales WITH CHECK
    ADD CONSTRAINT FK_Sales_Customers
        FOREIGN KEY ([CustomerKey])
        REFERENCES dbo.Customers ([CustomerKey]);

    ALTER TABLE dbo.Sales CHECK CONSTRAINT FK_Sales_Customers;
END;

-- Sales -> Products
IF OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Products', N'U') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_Sales_Products'
      AND parent_object_id = OBJECT_ID(N'dbo.Sales')
)
BEGIN
    ALTER TABLE dbo.Sales WITH CHECK
    ADD CONSTRAINT FK_Sales_Products
        FOREIGN KEY ([ProductKey])
        REFERENCES dbo.Products ([ProductKey]);

    ALTER TABLE dbo.Sales CHECK CONSTRAINT FK_Sales_Products;
END;

-- Sales -> Stores
IF OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Stores', N'U') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_Sales_Stores'
      AND parent_object_id = OBJECT_ID(N'dbo.Sales')
)
BEGIN
    ALTER TABLE dbo.Sales WITH CHECK
    ADD CONSTRAINT FK_Sales_Stores
        FOREIGN KEY ([StoreKey])
        REFERENCES dbo.Stores ([StoreKey]);

    ALTER TABLE dbo.Sales CHECK CONSTRAINT FK_Sales_Stores;
END;

-- Sales -> Employees (full type-compatibility guard)
IF OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Employees', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.Sales', N'EmployeeKey') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_Sales_Employees'
      AND parent_object_id = OBJECT_ID(N'dbo.Sales')
)
AND EXISTS (
    SELECT 1
    FROM sys.columns pc
    JOIN sys.columns rc
      ON rc.object_id = OBJECT_ID(N'dbo.Employees')
     AND rc.name = N'EmployeeKey'
    WHERE pc.object_id = OBJECT_ID(N'dbo.Sales')
      AND pc.name = N'EmployeeKey'
      AND pc.system_type_id = rc.system_type_id
      AND pc.user_type_id  = rc.user_type_id
      AND pc.max_length    = rc.max_length
      AND pc.precision     = rc.precision
      AND pc.scale         = rc.scale
)
BEGIN
    ALTER TABLE dbo.Sales WITH CHECK
    ADD CONSTRAINT FK_Sales_Employees
        FOREIGN KEY ([EmployeeKey])
        REFERENCES dbo.Employees ([EmployeeKey]);

    ALTER TABLE dbo.Sales CHECK CONSTRAINT FK_Sales_Employees;
END;

-- Sales -> Promotions
IF OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Promotions', N'U') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_Sales_Promotions'
      AND parent_object_id = OBJECT_ID(N'dbo.Sales')
)
BEGIN
    ALTER TABLE dbo.Sales WITH CHECK
    ADD CONSTRAINT FK_Sales_Promotions
        FOREIGN KEY ([PromotionKey])
        REFERENCES dbo.Promotions ([PromotionKey]);

    ALTER TABLE dbo.Sales CHECK CONSTRAINT FK_Sales_Promotions;
END;

-- Sales -> Currency
IF OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Currency', N'U') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_Sales_Currency'
      AND parent_object_id = OBJECT_ID(N'dbo.Sales')
)
BEGIN
    ALTER TABLE dbo.Sales WITH CHECK
    ADD CONSTRAINT FK_Sales_Currency
        FOREIGN KEY ([CurrencyKey])
        REFERENCES dbo.Currency ([CurrencyKey]);

    ALTER TABLE dbo.Sales CHECK CONSTRAINT FK_Sales_Currency;
END;

-- Sales -> Channels (config-optional dimension)
IF OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Channels', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.Sales', N'ChannelKey') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_Sales_Channels'
      AND parent_object_id = OBJECT_ID(N'dbo.Sales')
)
BEGIN
    ALTER TABLE dbo.Sales WITH CHECK
    ADD CONSTRAINT FK_Sales_Channels
        FOREIGN KEY ([ChannelKey])
        REFERENCES dbo.Channels ([ChannelKey]);

    ALTER TABLE dbo.Sales CHECK CONSTRAINT FK_Sales_Channels;
END;

-- Sales -> Time (config-optional dimension)
IF OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Time', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.Sales', N'TimeKey') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_Sales_Time'
      AND parent_object_id = OBJECT_ID(N'dbo.Sales')
)
BEGIN
    ALTER TABLE dbo.Sales WITH CHECK
    ADD CONSTRAINT FK_Sales_Time
        FOREIGN KEY ([TimeKey])
        REFERENCES dbo.Time ([TimeKey]);

    ALTER TABLE dbo.Sales CHECK CONSTRAINT FK_Sales_Time;
END;

-- Sales -> Dates (OrderDate)
IF OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Dates', N'U') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_Sales_Dates_OrderDate'
      AND parent_object_id = OBJECT_ID(N'dbo.Sales')
)
BEGIN
    ALTER TABLE dbo.Sales WITH CHECK
    ADD CONSTRAINT FK_Sales_Dates_OrderDate
        FOREIGN KEY ([OrderDate])
        REFERENCES dbo.Dates ([Date]);

    ALTER TABLE dbo.Sales CHECK CONSTRAINT FK_Sales_Dates_OrderDate;
END;

-- Sales -> Dates (DueDate)
IF OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Dates', N'U') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_Sales_Dates_DueDate'
      AND parent_object_id = OBJECT_ID(N'dbo.Sales')
)
BEGIN
    ALTER TABLE dbo.Sales WITH CHECK
    ADD CONSTRAINT FK_Sales_Dates_DueDate
        FOREIGN KEY ([DueDate])
        REFERENCES dbo.Dates ([Date]);

    ALTER TABLE dbo.Sales CHECK CONSTRAINT FK_Sales_Dates_DueDate;
END;

-- Sales -> Dates (DeliveryDate)
IF OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Dates', N'U') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_Sales_Dates_DeliveryDate'
      AND parent_object_id = OBJECT_ID(N'dbo.Sales')
)
BEGIN
    ALTER TABLE dbo.Sales WITH CHECK
    ADD CONSTRAINT FK_Sales_Dates_DeliveryDate
        FOREIGN KEY ([DeliveryDate])
        REFERENCES dbo.Dates ([Date]);

    ALTER TABLE dbo.Sales CHECK CONSTRAINT FK_Sales_Dates_DeliveryDate;
END;
GO

-----------------------------------------------------------------------
-- 3. SALES: CHECK CONSTRAINTS
-----------------------------------------------------------------------
SET NOCOUNT ON;
SET XACT_ABORT ON;

-- IsOrderDelayed is a bit/flag represented as INT in the fact schema (0/1).
-- Mirrors CK_OrderHeader_IsOrderDelayed_01 in 20_sales_order_header.sql.
IF OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.Sales', N'IsOrderDelayed') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.check_constraints
    WHERE name = N'CK_Sales_IsOrderDelayed_01'
      AND parent_object_id = OBJECT_ID(N'dbo.Sales')
)
BEGIN
    ALTER TABLE dbo.Sales WITH CHECK
    ADD CONSTRAINT CK_Sales_IsOrderDelayed_01
        CHECK ([IsOrderDelayed] IN (0, 1));
END;
GO

-----------------------------------------------------------------------
-- 4. SALESRETURN: PRIMARY KEY + INDEXES
-----------------------------------------------------------------------
SET NOCOUNT ON;
SET XACT_ABORT ON;

-- PK: ReturnEventKey (surrogate; only when column exists)
IF OBJECT_ID(N'dbo.Returns', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.Returns', N'ReturnEventKey') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.key_constraints
    WHERE name = N'PK_Returns'
      AND parent_object_id = OBJECT_ID(N'dbo.Returns')
)
BEGIN
    ALTER TABLE dbo.Returns
    ADD CONSTRAINT PK_Returns PRIMARY KEY NONCLUSTERED ([ReturnEventKey]);
END;

-- Natural-key access path (non-unique; supports joins back to Sales)
IF OBJECT_ID(N'dbo.Returns', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.Returns', N'OrderNumber') IS NOT NULL
AND COL_LENGTH(N'dbo.Returns', N'OrderLineNumber') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.indexes
    WHERE name = N'IX_Returns_NaturalKey'
      AND object_id = OBJECT_ID(N'dbo.Returns')
)
BEGIN
    CREATE INDEX IX_Returns_NaturalKey
    ON dbo.Returns ([OrderNumber], [OrderLineNumber], [ReturnSequence]);
END;
GO

-----------------------------------------------------------------------
-- 5. SALESRETURN: FOREIGN KEYS
-----------------------------------------------------------------------
SET NOCOUNT ON;
SET XACT_ABORT ON;

-- Returns -> Sales (natural key; flat-model equivalent of
-- 22_sales_order_relations.sql FK_Returns_OrderDetail)
IF OBJECT_ID(N'dbo.Returns', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.Returns', N'OrderNumber') IS NOT NULL
AND COL_LENGTH(N'dbo.Returns', N'OrderLineNumber') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_Returns_Sales'
      AND parent_object_id = OBJECT_ID(N'dbo.Returns')
)
BEGIN
    ALTER TABLE dbo.Returns WITH CHECK
    ADD CONSTRAINT FK_Returns_Sales
        FOREIGN KEY ([OrderNumber], [OrderLineNumber])
        REFERENCES dbo.Sales ([OrderNumber], [OrderLineNumber]);

    ALTER TABLE dbo.Returns CHECK CONSTRAINT FK_Returns_Sales;
END;

-- Returns -> Dates (ReturnDate)
IF OBJECT_ID(N'dbo.Returns', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Dates', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.Returns', N'ReturnDate') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_Returns_Dates_ReturnDate'
      AND parent_object_id = OBJECT_ID(N'dbo.Returns')
)
BEGIN
    ALTER TABLE dbo.Returns WITH CHECK
    ADD CONSTRAINT FK_Returns_Dates_ReturnDate
        FOREIGN KEY ([ReturnDate])
        REFERENCES dbo.Dates ([Date]);

    ALTER TABLE dbo.Returns CHECK CONSTRAINT FK_Returns_Dates_ReturnDate;
END;

-- Returns -> ReturnReason (type-compatibility guard for INT/BIGINT mismatch)
IF OBJECT_ID(N'dbo.Returns', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.ReturnReason', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.Returns', N'ReturnReasonKey') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_Returns_ReturnReason'
      AND parent_object_id = OBJECT_ID(N'dbo.Returns')
)
AND EXISTS (
    SELECT 1
    FROM sys.columns pc
    JOIN sys.columns rc
      ON rc.object_id = OBJECT_ID(N'dbo.ReturnReason')
     AND rc.name = N'ReturnReasonKey'
    WHERE pc.object_id = OBJECT_ID(N'dbo.Returns')
      AND pc.name = N'ReturnReasonKey'
      AND pc.system_type_id = rc.system_type_id
      AND pc.user_type_id  = rc.user_type_id
      AND pc.max_length    = rc.max_length
      AND pc.precision     = rc.precision
      AND pc.scale         = rc.scale
)
BEGIN
    ALTER TABLE dbo.Returns WITH CHECK
    ADD CONSTRAINT FK_Returns_ReturnReason
        FOREIGN KEY ([ReturnReasonKey])
        REFERENCES dbo.ReturnReason ([ReturnReasonKey]);

    ALTER TABLE dbo.Returns CHECK CONSTRAINT FK_Returns_ReturnReason;
END;
GO
