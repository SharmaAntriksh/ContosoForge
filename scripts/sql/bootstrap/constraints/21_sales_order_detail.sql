-----------------------------------------------------------------------
-- FACT: OrderDetail (CANDIDATE KEY + FOREIGN KEYS WITH CHECK)
-- Columns (expected):
--   OrderNumber, OrderLineNumber, ProductKey,
--   PromotionKey, CurrencyKey,
--   DueDate, DeliveryDate, Quantity, NetPrice, UnitCost, UnitPrice,
--   DiscountAmount, DeliveryStatus
-----------------------------------------------------------------------

SET NOCOUNT ON;
SET XACT_ABORT ON;

-----------------------------------------------------------------------
-- CANDIDATE KEY (supports Returns -> OrderDetail FK)
-----------------------------------------------------------------------
IF OBJECT_ID(N'dbo.OrderDetail', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderDetail', N'OrderNumber') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderDetail', N'OrderLineNumber') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.key_constraints
    WHERE name = N'UQ_OrderDetail_OrderLine'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderDetail')
)
BEGIN
    ALTER TABLE dbo.OrderDetail
    ADD CONSTRAINT UQ_OrderDetail_OrderLine
        UNIQUE NONCLUSTERED ([OrderNumber], [OrderLineNumber]);
END;

-----------------------------------------------------------------------
-- FOREIGN KEYS (WITH CHECK)
-----------------------------------------------------------------------

-- OrderDetail -> Products
IF OBJECT_ID(N'dbo.OrderDetail', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Products', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderDetail', N'ProductKey') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_OrderDetail_Products'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderDetail')
)
BEGIN
    ALTER TABLE dbo.OrderDetail WITH CHECK
    ADD CONSTRAINT FK_OrderDetail_Products
        FOREIGN KEY ([ProductKey])
        REFERENCES dbo.Products ([ProductKey]);

    ALTER TABLE dbo.OrderDetail CHECK CONSTRAINT FK_OrderDetail_Products;
END;

-- OrderDetail -> Promotions
IF OBJECT_ID(N'dbo.OrderDetail', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Promotions', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderDetail', N'PromotionKey') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_OrderDetail_Promotions'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderDetail')
)
BEGIN
    ALTER TABLE dbo.OrderDetail WITH CHECK
    ADD CONSTRAINT FK_OrderDetail_Promotions
        FOREIGN KEY ([PromotionKey])
        REFERENCES dbo.Promotions ([PromotionKey]);

    ALTER TABLE dbo.OrderDetail CHECK CONSTRAINT FK_OrderDetail_Promotions;
END;

-- OrderDetail -> Currency
IF OBJECT_ID(N'dbo.OrderDetail', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Currency', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderDetail', N'CurrencyKey') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_OrderDetail_Currency'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderDetail')
)
BEGIN
    ALTER TABLE dbo.OrderDetail WITH CHECK
    ADD CONSTRAINT FK_OrderDetail_Currency
        FOREIGN KEY ([CurrencyKey])
        REFERENCES dbo.Currency ([CurrencyKey]);

    ALTER TABLE dbo.OrderDetail CHECK CONSTRAINT FK_OrderDetail_Currency;
END;

-- OrderDetail -> Dates (DueDate)
IF OBJECT_ID(N'dbo.OrderDetail', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Dates', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderDetail', N'DueDate') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_OrderDetail_Dates_DueDate'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderDetail')
)
BEGIN
    ALTER TABLE dbo.OrderDetail WITH CHECK
    ADD CONSTRAINT FK_OrderDetail_Dates_DueDate
        FOREIGN KEY ([DueDate])
        REFERENCES dbo.Dates ([Date]);

    ALTER TABLE dbo.OrderDetail CHECK CONSTRAINT FK_OrderDetail_Dates_DueDate;
END;

-- OrderDetail -> Dates (DeliveryDate)
IF OBJECT_ID(N'dbo.OrderDetail', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.Dates', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderDetail', N'DeliveryDate') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_OrderDetail_Dates_DeliveryDate'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderDetail')
)
BEGIN
    ALTER TABLE dbo.OrderDetail WITH CHECK
    ADD CONSTRAINT FK_OrderDetail_Dates_DeliveryDate
        FOREIGN KEY ([DeliveryDate])
        REFERENCES dbo.Dates ([Date]);

    ALTER TABLE dbo.OrderDetail CHECK CONSTRAINT FK_OrderDetail_Dates_DeliveryDate;
END;
