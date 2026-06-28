-----------------------------------------------------------------------
-- FACT RELATIONS (WITH CHECK)
--  - OrderDetail -> OrderHeader
--  - Returns      -> OrderDetail
-----------------------------------------------------------------------

SET NOCOUNT ON;
SET XACT_ABORT ON;

-----------------------------------------------------------------------
-- OrderDetail -> OrderHeader
-----------------------------------------------------------------------
IF OBJECT_ID(N'dbo.OrderDetail', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.OrderHeader', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.OrderDetail', N'OrderNumber') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_OrderDetail_OrderHeader'
      AND parent_object_id = OBJECT_ID(N'dbo.OrderDetail')
)
BEGIN
    ALTER TABLE dbo.OrderDetail WITH CHECK
    ADD CONSTRAINT FK_OrderDetail_OrderHeader
        FOREIGN KEY ([OrderNumber])
        REFERENCES dbo.OrderHeader ([OrderNumber]);

    ALTER TABLE dbo.OrderDetail CHECK CONSTRAINT FK_OrderDetail_OrderHeader;
END;

-----------------------------------------------------------------------
-- Returns -> OrderDetail  (natural key join)
-----------------------------------------------------------------------
IF OBJECT_ID(N'dbo.Returns', N'U') IS NOT NULL
AND OBJECT_ID(N'dbo.OrderDetail', N'U') IS NOT NULL
AND COL_LENGTH(N'dbo.Returns', N'OrderNumber') IS NOT NULL
AND COL_LENGTH(N'dbo.Returns', N'OrderLineNumber') IS NOT NULL
AND NOT EXISTS (
    SELECT 1
    FROM sys.foreign_keys
    WHERE name = N'FK_Returns_OrderDetail'
      AND parent_object_id = OBJECT_ID(N'dbo.Returns')
)
BEGIN
    ALTER TABLE dbo.Returns WITH CHECK
    ADD CONSTRAINT FK_Returns_OrderDetail
        FOREIGN KEY ([OrderNumber], [OrderLineNumber])
        REFERENCES dbo.OrderDetail ([OrderNumber], [OrderLineNumber]);

    ALTER TABLE dbo.Returns CHECK CONSTRAINT FK_Returns_OrderDetail;
END;
