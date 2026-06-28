SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

CREATE OR ALTER PROCEDURE [verify].[FactDistributions]
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @has_sales BIT = CASE WHEN OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL THEN 1 ELSE 0 END;
    DECLARE @has_soh   BIT = CASE WHEN OBJECT_ID(N'dbo.OrderHeader', N'U') IS NOT NULL THEN 1 ELSE 0 END;

    IF @has_sales = 0 AND @has_soh = 0 RETURN;

    CREATE TABLE #R (
        Category    VARCHAR(50)  NOT NULL,
        [Check]     VARCHAR(200) NOT NULL,
        Description VARCHAR(500) NOT NULL,
        Result      VARCHAR(10)  NOT NULL,
        ActualValue VARCHAR(100) NOT NULL
    );

    -- Monthly CoV
    DECLARE @cov DECIMAL(5,2);
    IF @has_sales = 1
        SELECT @cov = ISNULL(STDEV(MonthlySales) / NULLIF(AVG(MonthlySales), 0), 0)
        FROM (
            SELECT YEAR(OrderDate) * 100 + MONTH(OrderDate) AS YM, CAST(COUNT(*) AS FLOAT) AS MonthlySales
            FROM dbo.Sales GROUP BY YEAR(OrderDate), MONTH(OrderDate)
        ) m;
    ELSE
        SELECT @cov = ISNULL(STDEV(MonthlySales) / NULLIF(AVG(MonthlySales), 0), 0)
        FROM (
            SELECT YEAR(h.OrderDate) * 100 + MONTH(h.OrderDate) AS YM, CAST(COUNT(*) AS FLOAT) AS MonthlySales
            FROM dbo.OrderHeader h
            JOIN dbo.OrderDetail d ON d.OrderNumber = h.OrderNumber
            GROUP BY YEAR(h.OrderDate), MONTH(h.OrderDate)
        ) m;
    INSERT INTO #R VALUES ('Distribution', 'Monthly CoV > 0.10',
        'Monthly sales volume should vary (seasonal demand); flat data suggests broken demand curve',
        CASE WHEN @cov > 0.10 THEN 'PASS' ELSE 'FAIL' END,
        'CoV = ' + CAST(@cov AS VARCHAR));

    -- Customer concentration
    DECLARE @max_cust_pct DECIMAL(5,2);
    IF @has_sales = 1
        SELECT @max_cust_pct = ISNULL(MAX(CustPct), 0)
        FROM (
            SELECT SUM(NetPrice) * 100.0 / NULLIF((SELECT SUM(NetPrice) FROM dbo.Sales), 0) AS CustPct
            FROM dbo.Sales GROUP BY CustomerKey
        ) x;
    ELSE
        SELECT @max_cust_pct = ISNULL(MAX(CustPct), 0)
        FROM (
            SELECT SUM(d.NetPrice) * 100.0 / NULLIF((SELECT SUM(NetPrice) FROM dbo.OrderDetail), 0) AS CustPct
            FROM dbo.OrderHeader h
            JOIN dbo.OrderDetail d ON d.OrderNumber = h.OrderNumber
            GROUP BY h.CustomerKey
        ) x;
    INSERT INTO #R VALUES ('Distribution', 'No customer > 5% of revenue',
        'No single customer should dominate total revenue',
        CASE WHEN @max_cust_pct < 5.0 THEN 'PASS' ELSE 'FAIL' END,
        'max = ' + CAST(@max_cust_pct AS VARCHAR) + '%');

    -- Quantity mode
    DECLARE @qty1_pct DECIMAL(5,1);
    IF @has_sales = 1
        SELECT @qty1_pct = ISNULL(
            SUM(CASE WHEN Quantity = 1 THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 0)
        FROM dbo.Sales;
    ELSE
        SELECT @qty1_pct = ISNULL(
            SUM(CASE WHEN Quantity = 1 THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 0)
        FROM dbo.OrderDetail;
    INSERT INTO #R VALUES ('Distribution', 'Quantity distribution peaks at 1',
        'Qty=1 percentage should be non-trivial (>5%); exact value depends on Poisson lambda',
        CASE WHEN @qty1_pct > 5 THEN 'PASS' ELSE 'FAIL' END,
        'Qty=1 is ' + CAST(@qty1_pct AS VARCHAR) + '%');

    -- NetPrice <= UnitPrice
    DECLARE @net_exceed INT;
    IF @has_sales = 1
        SELECT @net_exceed = COUNT(*) FROM dbo.Sales WHERE NetPrice > UnitPrice;
    ELSE
        SELECT @net_exceed = COUNT(*) FROM dbo.OrderDetail WHERE NetPrice > UnitPrice;
    INSERT INTO #R VALUES ('Domain', 'NetPrice <= UnitPrice',
        'Selling price should not exceed sticker price',
        CASE WHEN @net_exceed = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@net_exceed AS VARCHAR) + ' violations');

    -- Delivery delay rate (IsOrderDelayed is on header in sales_order mode)
    DECLARE @delay_pct DECIMAL(5,1);
    IF @has_sales = 1
        SELECT @delay_pct = ISNULL(
            SUM(CAST(IsOrderDelayed AS INT)) * 100.0 / NULLIF(COUNT(*), 0), 0)
        FROM dbo.Sales;
    ELSE
        SELECT @delay_pct = ISNULL(
            SUM(CAST(IsOrderDelayed AS INT)) * 100.0 / NULLIF(COUNT(*), 0), 0)
        FROM dbo.OrderHeader;
    INSERT INTO #R VALUES ('Distribution', 'Delayed orders < 35%',
        'Order delay rate should be a minority',
        CASE WHEN @delay_pct < 35 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@delay_pct AS VARCHAR) + '% delayed');

    -- INFO: average order value
    DECLARE @aov DECIMAL(10,2);
    IF @has_sales = 1
        SELECT @aov = ISNULL(AVG(NetPrice * Quantity), 0) FROM dbo.Sales;
    ELSE
        SELECT @aov = ISNULL(AVG(NetPrice * Quantity), 0) FROM dbo.OrderDetail;
    INSERT INTO #R VALUES ('Info', 'Average order line value',
        'Average NetPrice * Quantity per line item',
        'INFO', '$' + CAST(@aov AS VARCHAR));

    -- INFO: discount utilization
    DECLARE @disc_pct DECIMAL(5,1);
    IF @has_sales = 1
        SELECT @disc_pct = ISNULL(
            SUM(CASE WHEN DiscountAmount > 0 THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 0)
        FROM dbo.Sales;
    ELSE
        SELECT @disc_pct = ISNULL(
            SUM(CASE WHEN DiscountAmount > 0 THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 0)
        FROM dbo.OrderDetail;
    INSERT INTO #R VALUES ('Info', 'Discount utilization',
        'Percentage of line items with DiscountAmount > 0',
        'INFO', CAST(@disc_pct AS VARCHAR) + '% discounted');

    SELECT Category, [Check], Description, Result, ActualValue FROM #R;
    DROP TABLE #R;
END;
GO
