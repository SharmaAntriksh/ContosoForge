-----------------------------------------------------------------------
-- FACT: OrderHeader (CANDIDATE KEY + FOREIGN KEYS + CHECKS)
-- Columns (expected):
--   OrderNumber, CustomerKey, StoreKey, EmployeeKey,
--   OrderDate, TimeKey, ChannelKey, IsOrderDelayed
-----------------------------------------------------------------------

SET NOCOUNT ON;
SET XACT_ABORT ON;

-----------------------------------------------------------------------
-- CLEANUP: remove legacy/incorrect constraint if it exists
-- (DeliveryDate is NOT a header column in the current schema)
-----------------------------------------------------------------------
IF OBJECT_ID(N'dbo.OrderHeader', N'U') IS NOT NULL
AND EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_OrderHeader_Dates_DeliveryDate'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderHeader')
)
BEGIN
    ALTER TABLE dbo.OrderHeader DROP CONSTRAINT FK_OrderHeader_Dates_DeliveryDate;
END;

-----------------------------------------------------------------------
-- CANDIDATE KEY (required for OrderDetail -> OrderHeader FK)
-----------------------------------------------------------------------
IF OBJECT_ID(N'dbo.OrderHeader', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderHeader', N'OrderNumber') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.key_constraints
    WHERE name = N'UQ_OrderHeader_OrderNumber'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderHeader')
)
BEGIN
    ALTER TABLE dbo.OrderHeader
    ADD CONSTRAINT UQ_OrderHeader_OrderNumber
        UNIQUE NONCLUSTERED ([OrderNumber]);
END;

-----------------------------------------------------------------------
-- FOREIGN KEYS (WITH CHECK)
-----------------------------------------------------------------------

-- OrderHeader -> Customers
IF OBJECT_ID(N'dbo.OrderHeader', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Customers', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderHeader', N'CustomerKey') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_OrderHeader_Customers'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderHeader')
)
BEGIN
    ALTER TABLE dbo.OrderHeader WITH CHECK
    ADD CONSTRAINT FK_OrderHeader_Customers
        FOREIGN KEY ([CustomerKey])
        REFERENCES dbo.Customers ([CustomerKey]);

    ALTER TABLE dbo.OrderHeader CHECK CONSTRAINT FK_OrderHeader_Customers;
END;

-- OrderHeader -> Stores
IF OBJECT_ID(N'dbo.OrderHeader', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Stores', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderHeader', N'StoreKey') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_OrderHeader_Stores'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderHeader')
)
BEGIN
    ALTER TABLE dbo.OrderHeader WITH CHECK
    ADD CONSTRAINT FK_OrderHeader_Stores
        FOREIGN KEY ([StoreKey])
        REFERENCES dbo.Stores ([StoreKey]);

    ALTER TABLE dbo.OrderHeader CHECK CONSTRAINT FK_OrderHeader_Stores;
END;

-- OrderHeader -> Employees (EmployeeKey) [guarded for type compatibility]
IF OBJECT_ID(N'dbo.OrderHeader', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Employees', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderHeader', N'EmployeeKey') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_OrderHeader_Employees_EmployeeKey'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderHeader')
)
AND EXISTS (
    SELECT 1
    FROM sys.columns pc
    JOIN sys.columns rc
      ON rc.object_id = OBJECT_ID(N'dbo.Employees')
     AND rc.name = N'EmployeeKey'
    WHERE pc.object_id = OBJECT_ID(N'dbo.OrderHeader')
      AND pc.name = N'EmployeeKey'
      AND pc.system_type_id = rc.system_type_id
      AND pc.user_type_id = rc.user_type_id
      AND pc.max_length = rc.max_length
      AND pc.precision = rc.precision
      AND pc.scale = rc.scale
)
BEGIN
    ALTER TABLE dbo.OrderHeader WITH CHECK
    ADD CONSTRAINT FK_OrderHeader_Employees_EmployeeKey
        FOREIGN KEY ([EmployeeKey])
        REFERENCES dbo.Employees ([EmployeeKey]);

    ALTER TABLE dbo.OrderHeader CHECK CONSTRAINT FK_OrderHeader_Employees_EmployeeKey;
END;

-- OrderHeader -> Dates (OrderDate)
IF OBJECT_ID(N'dbo.OrderHeader', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Dates', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderHeader', N'OrderDate') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_OrderHeader_Dates_OrderDate'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderHeader')
)
BEGIN
    ALTER TABLE dbo.OrderHeader WITH CHECK
    ADD CONSTRAINT FK_OrderHeader_Dates_OrderDate
        FOREIGN KEY ([OrderDate])
        REFERENCES dbo.Dates ([Date]);

    ALTER TABLE dbo.OrderHeader CHECK CONSTRAINT FK_OrderHeader_Dates_OrderDate;
END;

-- OrderHeader -> Time
IF OBJECT_ID(N'dbo.OrderHeader', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Time', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderHeader', N'TimeKey') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_OrderHeader_Time'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderHeader')
)
BEGIN
    ALTER TABLE dbo.OrderHeader WITH CHECK
    ADD CONSTRAINT FK_OrderHeader_Time
        FOREIGN KEY ([TimeKey])
        REFERENCES dbo.Time ([TimeKey]);

    ALTER TABLE dbo.OrderHeader CHECK CONSTRAINT FK_OrderHeader_Time;
END;

-- OrderHeader -> Channels
IF OBJECT_ID(N'dbo.OrderHeader', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Channels', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderHeader', N'ChannelKey') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_OrderHeader_Channels'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderHeader')
)
BEGIN
    ALTER TABLE dbo.OrderHeader WITH CHECK
    ADD CONSTRAINT FK_OrderHeader_Channels
        FOREIGN KEY ([ChannelKey])
        REFERENCES dbo.Channels ([ChannelKey]);

    ALTER TABLE dbo.OrderHeader CHECK CONSTRAINT FK_OrderHeader_Channels;
END;

-----------------------------------------------------------------------
-- CHECK CONSTRAINTS
-----------------------------------------------------------------------

-- IsOrderDelayed is a bit/flag represented as INT in the fact schema (0/1).
IF OBJECT_ID(N'dbo.OrderHeader', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderHeader', N'IsOrderDelayed') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.check_constraints
    WHERE name = N'CK_OrderHeader_IsOrderDelayed_01'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderHeader')
)
BEGIN
    ALTER TABLE dbo.OrderHeader WITH CHECK
    ADD CONSTRAINT CK_OrderHeader_IsOrderDelayed_01
        CHECK ([IsOrderDelayed] IN (0, 1));
END;
