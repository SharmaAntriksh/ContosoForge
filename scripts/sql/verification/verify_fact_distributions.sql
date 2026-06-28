-- ============================================================================
-- VERIFY FACT TABLE DISTRIBUTIONS & STATISTICAL REASONABLENESS
-- Run after loading generated data into SQL Server.
-- Checks that generated data isn't flat/uniform but follows expected patterns.
--
-- Supports both sales_output modes:
--   sales        -> single Sales table
--   sales_order  -> OrderHeader + OrderDetail tables
-- ============================================================================

-- Detect which sales table exists
DECLARE @has_sales  BIT = CASE WHEN OBJECT_ID('dbo.Sales') IS NOT NULL THEN 1 ELSE 0 END;
DECLARE @has_header BIT = CASE WHEN OBJECT_ID('dbo.OrderHeader') IS NOT NULL THEN 1 ELSE 0 END;


-- ============================================================================
-- 1. Sales Volume by Month (Macro Demand Curve)
-- ============================================================================

-- 1a. Monthly sales should NOT be uniform — expect seasonal variation
IF @has_sales = 1
BEGIN
    SELECT
        YEAR(OrderDate)                                                 AS SalesYear,
        MONTH(OrderDate)                                                AS SalesMonth,
        COUNT(*)                                                        AS LineItems,
        COUNT(DISTINCT OrderNumber)                                AS Orders,
        CAST(SUM(NetPrice) AS DECIMAL(18,2))                            AS Revenue,
        CAST(AVG(NetPrice) AS DECIMAL(10,2))                            AS AvgLinePrice
    FROM Sales
    GROUP BY YEAR(OrderDate), MONTH(OrderDate)
    ORDER BY SalesYear, SalesMonth;
    -- EXPECTED: variation across months; Q4 typically higher (holiday season)
    --           coefficient of variation (stdev/mean) > 0.10
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        YEAR(h.OrderDate)                                               AS SalesYear,
        MONTH(h.OrderDate)                                              AS SalesMonth,
        COUNT(*)                                                        AS LineItems,
        COUNT(DISTINCT h.OrderNumber)                              AS Orders,
        CAST(SUM(d.NetPrice) AS DECIMAL(18,2))                          AS Revenue,
        CAST(AVG(d.NetPrice) AS DECIMAL(10,2))                          AS AvgLinePrice
    FROM OrderHeader h
    JOIN OrderDetail d ON d.OrderNumber = h.OrderNumber
    GROUP BY YEAR(h.OrderDate), MONTH(h.OrderDate)
    ORDER BY SalesYear, SalesMonth;
    -- EXPECTED: variation across months; Q4 typically higher (holiday season)
END

-- 1b. Year-over-year growth should exist (not flat)
IF @has_sales = 1
BEGIN
    SELECT
        YEAR(OrderDate)                                                 AS SalesYear,
        COUNT(*)                                                        AS LineItems,
        COUNT(DISTINCT OrderNumber)                                AS Orders,
        CAST(SUM(NetPrice) AS DECIMAL(18,2))                            AS Revenue
    FROM Sales
    GROUP BY YEAR(OrderDate)
    ORDER BY SalesYear;
    -- EXPECTED: generally increasing trend (macro demand curve)
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        YEAR(h.OrderDate)                                               AS SalesYear,
        COUNT(*)                                                        AS LineItems,
        COUNT(DISTINCT h.OrderNumber)                              AS Orders,
        CAST(SUM(d.NetPrice) AS DECIMAL(18,2))                          AS Revenue
    FROM OrderHeader h
    JOIN OrderDetail d ON d.OrderNumber = h.OrderNumber
    GROUP BY YEAR(h.OrderDate)
    ORDER BY SalesYear;
    -- EXPECTED: generally increasing trend (macro demand curve)
END


-- ============================================================================
-- 2. No Single Entity Dominates
-- ============================================================================

-- 2a. Top 10 customers by revenue — no one customer > 5% of total
IF @has_sales = 1
BEGIN
    SELECT TOP 10
        c.CustomerKey,
        c.CustomerName,
        COUNT(*)                                                        AS LineItems,
        CAST(SUM(f.NetPrice) AS DECIMAL(18,2))                          AS Revenue,
        CAST(SUM(f.NetPrice) * 100.0 / (SELECT SUM(NetPrice) FROM Sales) AS DECIMAL(5,2)) AS PctOfTotal
    FROM Sales f
    JOIN Customers c ON c.CustomerKey = f.CustomerKey
    GROUP BY c.CustomerKey, c.CustomerName
    ORDER BY Revenue DESC;
    -- EXPECTED: top customer < 5% of total revenue
END
ELSE IF @has_header = 1
BEGIN
    SELECT TOP 10
        c.CustomerKey,
        c.CustomerName,
        COUNT(*)                                                        AS LineItems,
        CAST(SUM(d.NetPrice) AS DECIMAL(18,2))                          AS Revenue,
        CAST(SUM(d.NetPrice) * 100.0 / (
            SELECT SUM(d2.NetPrice) FROM OrderHeader h2
            JOIN OrderDetail d2 ON d2.OrderNumber = h2.OrderNumber
        ) AS DECIMAL(5,2)) AS PctOfTotal
    FROM OrderHeader h
    JOIN OrderDetail d ON d.OrderNumber = h.OrderNumber
    JOIN Customers c ON c.CustomerKey = h.CustomerKey
    GROUP BY c.CustomerKey, c.CustomerName
    ORDER BY Revenue DESC;
    -- EXPECTED: top customer < 5% of total revenue
END

-- 2b. Top 10 products by units sold
IF @has_sales = 1
BEGIN
    SELECT TOP 10
        p.ProductKey,
        p.ProductName,
        SUM(f.Quantity)                                                 AS UnitsSold,
        CAST(SUM(f.Quantity) * 100.0 / (SELECT SUM(Quantity) FROM Sales) AS DECIMAL(5,2)) AS PctOfUnits
    FROM Sales f
    JOIN Products p ON p.ProductKey = f.ProductKey
    GROUP BY p.ProductKey, p.ProductName
    ORDER BY UnitsSold DESC;
    -- EXPECTED: top product < 3% of total units
END
ELSE IF @has_header = 1
BEGIN
    SELECT TOP 10
        p.ProductKey,
        p.ProductName,
        SUM(d.Quantity)                                                 AS UnitsSold,
        CAST(SUM(d.Quantity) * 100.0 / (
            SELECT SUM(d2.Quantity) FROM OrderDetail d2
        ) AS DECIMAL(5,2)) AS PctOfUnits
    FROM OrderDetail d
    JOIN Products p ON p.ProductKey = d.ProductKey
    GROUP BY p.ProductKey, p.ProductName
    ORDER BY UnitsSold DESC;
    -- EXPECTED: top product < 3% of total units
END

-- 2c. Top 10 stores by revenue
IF @has_sales = 1
BEGIN
    SELECT TOP 10
        s.StoreKey,
        s.StoreName,
        s.StoreType,
        COUNT(*)                                                        AS LineItems,
        CAST(SUM(f.NetPrice) * 100.0 / (SELECT SUM(NetPrice) FROM Sales) AS DECIMAL(5,2)) AS PctOfTotal
    FROM Sales f
    JOIN Stores s ON s.StoreKey = f.StoreKey
    GROUP BY s.StoreKey, s.StoreName, s.StoreType
    ORDER BY PctOfTotal DESC;
    -- EXPECTED: no single store > 5% (with 500 stores)
END
ELSE IF @has_header = 1
BEGIN
    SELECT TOP 10
        s.StoreKey,
        s.StoreName,
        s.StoreType,
        COUNT(*)                                                        AS LineItems,
        CAST(SUM(d.NetPrice) * 100.0 / (
            SELECT SUM(d2.NetPrice) FROM OrderHeader h2
            JOIN OrderDetail d2 ON d2.OrderNumber = h2.OrderNumber
        ) AS DECIMAL(5,2)) AS PctOfTotal
    FROM OrderHeader h
    JOIN OrderDetail d ON d.OrderNumber = h.OrderNumber
    JOIN Stores s ON s.StoreKey = h.StoreKey
    GROUP BY s.StoreKey, s.StoreName, s.StoreType
    ORDER BY PctOfTotal DESC;
    -- EXPECTED: no single store > 5% (with 500 stores)
END


-- ============================================================================
-- 3. Price Distribution
-- ============================================================================

-- 3a. Price band distribution (should span configured range, not cluster)
IF @has_sales = 1
BEGIN
    SELECT
        PriceBand,
        COUNT(*)                                                        AS LineItems,
        CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS PctOfTotal
    FROM (
        SELECT CASE
            WHEN UnitPrice < 25    THEN '$0-25'
            WHEN UnitPrice < 100   THEN '$25-100'
            WHEN UnitPrice < 500   THEN '$100-500'
            WHEN UnitPrice < 1000  THEN '$500-1000'
            ELSE '$1000+'
        END AS PriceBand
        FROM Sales
    ) x
    GROUP BY PriceBand
    ORDER BY PriceBand;
    -- EXPECTED: spread across bands, not concentrated in one
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        PriceBand,
        COUNT(*)                                                        AS LineItems,
        CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS PctOfTotal
    FROM (
        SELECT CASE
            WHEN UnitPrice < 25    THEN '$0-25'
            WHEN UnitPrice < 100   THEN '$25-100'
            WHEN UnitPrice < 500   THEN '$100-500'
            WHEN UnitPrice < 1000  THEN '$500-1000'
            ELSE '$1000+'
        END AS PriceBand
        FROM OrderDetail
    ) x
    GROUP BY PriceBand
    ORDER BY PriceBand;
    -- EXPECTED: spread across bands, not concentrated in one
END

-- 3b. Discount usage — not every sale should be discounted
IF @has_sales = 1
BEGIN
    SELECT
        CASE WHEN DiscountAmount > 0 THEN 'Discounted' ELSE 'Full Price' END AS SaleType,
        COUNT(*)                                                        AS LineItems,
        CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS PctOfTotal,
        CAST(AVG(CASE WHEN DiscountAmount > 0 THEN DiscountAmount END) AS DECIMAL(10,2)) AS AvgDiscount
    FROM Sales
    GROUP BY CASE WHEN DiscountAmount > 0 THEN 'Discounted' ELSE 'Full Price' END;
    -- EXPECTED: majority full price; discounted portion driven by promotion count
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        CASE WHEN DiscountAmount > 0 THEN 'Discounted' ELSE 'Full Price' END AS SaleType,
        COUNT(*)                                                        AS LineItems,
        CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS PctOfTotal,
        CAST(AVG(CASE WHEN DiscountAmount > 0 THEN DiscountAmount END) AS DECIMAL(10,2)) AS AvgDiscount
    FROM OrderDetail
    GROUP BY CASE WHEN DiscountAmount > 0 THEN 'Discounted' ELSE 'Full Price' END;
    -- EXPECTED: majority full price; discounted portion driven by promotion count
END

-- 3c. NetPrice should never exceed UnitPrice
IF @has_sales = 1
BEGIN
    SELECT COUNT(*) AS ExceedCount
    FROM Sales
    WHERE NetPrice > UnitPrice;
    -- EXPECTED: zero (selling price cannot exceed sticker price)
END
ELSE IF @has_header = 1
BEGIN
    SELECT COUNT(*) AS ExceedCount
    FROM OrderDetail
    WHERE NetPrice > UnitPrice;
    -- EXPECTED: zero (selling price cannot exceed sticker price)
END


-- ============================================================================
-- 4. Order Structure
-- ============================================================================

-- 4a. Lines per order distribution
IF @has_sales = 1
BEGIN
    SELECT
        LinesPerOrder,
        COUNT(*)                                                        AS OrderCount,
        CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS PctOfOrders
    FROM (
        SELECT OrderNumber, COUNT(*) AS LinesPerOrder
        FROM Sales
        GROUP BY OrderNumber
    ) x
    GROUP BY LinesPerOrder
    ORDER BY LinesPerOrder;
    -- EXPECTED: 1-5 lines (governed by max_lines_per_order), majority single-line
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        LinesPerOrder,
        COUNT(*)                                                        AS OrderCount,
        CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS PctOfOrders
    FROM (
        SELECT OrderNumber, COUNT(*) AS LinesPerOrder
        FROM OrderDetail
        GROUP BY OrderNumber
    ) x
    GROUP BY LinesPerOrder
    ORDER BY LinesPerOrder;
    -- EXPECTED: 1-5 lines (governed by max_lines_per_order), majority single-line
END

-- 4b. Quantity per line distribution
IF @has_sales = 1
BEGIN
    SELECT
        CASE
            WHEN Quantity = 1 THEN '1'
            WHEN Quantity BETWEEN 2 AND 3 THEN '2-3'
            WHEN Quantity BETWEEN 4 AND 5 THEN '4-5'
            ELSE '6+'
        END AS QuantityBand,
        COUNT(*)                                                        AS LineItems,
        CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS Pct
    FROM Sales
    GROUP BY CASE
        WHEN Quantity = 1 THEN '1'
        WHEN Quantity BETWEEN 2 AND 3 THEN '2-3'
        WHEN Quantity BETWEEN 4 AND 5 THEN '4-5'
        ELSE '6+'
    END
    ORDER BY QuantityBand;
    -- EXPECTED: Poisson-distributed, majority quantity=1
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        CASE
            WHEN Quantity = 1 THEN '1'
            WHEN Quantity BETWEEN 2 AND 3 THEN '2-3'
            WHEN Quantity BETWEEN 4 AND 5 THEN '4-5'
            ELSE '6+'
        END AS QuantityBand,
        COUNT(*)                                                        AS LineItems,
        CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS Pct
    FROM OrderDetail
    GROUP BY CASE
        WHEN Quantity = 1 THEN '1'
        WHEN Quantity BETWEEN 2 AND 3 THEN '2-3'
        WHEN Quantity BETWEEN 4 AND 5 THEN '4-5'
        ELSE '6+'
    END
    ORDER BY QuantityBand;
    -- EXPECTED: Poisson-distributed, majority quantity=1
END


-- ============================================================================
-- 5. Delivery Status Distribution
-- ============================================================================

-- 5a. Delivery status breakdown
IF @has_sales = 1
BEGIN
    SELECT
        DeliveryStatus,
        COUNT(*)                                                        AS LineItems,
        CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS Pct
    FROM Sales
    GROUP BY DeliveryStatus
    ORDER BY LineItems DESC;
    -- EXPECTED: mostly "Delivered", small % "Pending"/"In Transit"/"Cancelled"
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        DeliveryStatus,
        COUNT(*)                                                        AS LineItems,
        CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS Pct
    FROM OrderDetail
    GROUP BY DeliveryStatus
    ORDER BY LineItems DESC;
    -- EXPECTED: mostly "Delivered", small % "Pending"/"In Transit"/"Cancelled"
END

-- 5b. Delayed orders should be a minority
IF @has_sales = 1
BEGIN
    SELECT
        COUNT(*)                                                        AS TotalOrders,
        SUM(CAST(IsOrderDelayed AS INT))                                AS DelayedOrders,
        CAST(SUM(CAST(IsOrderDelayed AS INT)) * 100.0
             / NULLIF(COUNT(*), 0) AS DECIMAL(5,1))                    AS PctDelayed
    FROM Sales;
    -- EXPECTED: delayed orders < 20%
END
ELSE IF @has_header = 1
BEGIN
    -- IsOrderDelayed is on OrderHeader
    SELECT
        COUNT(*)                                                        AS TotalOrders,
        SUM(CAST(IsOrderDelayed AS INT))                                AS DelayedOrders,
        CAST(SUM(CAST(IsOrderDelayed AS INT)) * 100.0
             / NULLIF(COUNT(*), 0) AS DECIMAL(5,1))                    AS PctDelayed
    FROM OrderHeader;
    -- EXPECTED: delayed orders < 20%
END


-- ============================================================================
-- 6. Returns Distribution
-- ============================================================================

-- 6a. Return rate should match config (~3%)
IF @has_sales = 1
BEGIN
    SELECT
        (SELECT COUNT(DISTINCT OrderNumber) FROM Sales)             AS TotalOrders,
        COUNT(DISTINCT OrderNumber)                                 AS ReturnedOrders,
        CAST(COUNT(DISTINCT OrderNumber) * 100.0
             / NULLIF((SELECT COUNT(DISTINCT OrderNumber) FROM Sales), 0)
             AS DECIMAL(5,2))                                           AS ReturnRatePct
    FROM [Returns];
    -- EXPECTED: ~3% (governed by returns.return_rate)
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        (SELECT COUNT(DISTINCT OrderNumber) FROM OrderHeader)  AS TotalOrders,
        COUNT(DISTINCT OrderNumber)                                 AS ReturnedOrders,
        CAST(COUNT(DISTINCT OrderNumber) * 100.0
             / NULLIF((SELECT COUNT(DISTINCT OrderNumber) FROM OrderHeader), 0)
             AS DECIMAL(5,2))                                           AS ReturnRatePct
    FROM [Returns];
    -- EXPECTED: ~3% (governed by returns.return_rate)
END

-- 6b. Return days after sale distribution
IF @has_sales = 1
BEGIN
    SELECT
        DaysBand,
        COUNT(*)                                                        AS Returns,
        CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS Pct
    FROM (
        SELECT CASE
            WHEN DATEDIFF(DAY, s.OrderDate, r.ReturnDate) BETWEEN 1 AND 7   THEN '1-7 days'
            WHEN DATEDIFF(DAY, s.OrderDate, r.ReturnDate) BETWEEN 8 AND 14  THEN '8-14 days'
            WHEN DATEDIFF(DAY, s.OrderDate, r.ReturnDate) BETWEEN 15 AND 30 THEN '15-30 days'
            WHEN DATEDIFF(DAY, s.OrderDate, r.ReturnDate) BETWEEN 31 AND 60 THEN '31-60 days'
            ELSE '60+ days'
        END AS DaysBand
        FROM [Returns] r
        JOIN Sales s ON s.OrderNumber = r.OrderNumber
                    AND s.OrderLineNumber = r.OrderLineNumber
    ) x
    GROUP BY DaysBand
    ORDER BY DaysBand;
    -- EXPECTED: spread across 1-60 days (config min_days_after_sale/max_days_after_sale)
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        DaysBand,
        COUNT(*)                                                        AS Returns,
        CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS Pct
    FROM (
        SELECT CASE
            WHEN DATEDIFF(DAY, h.OrderDate, r.ReturnDate) BETWEEN 1 AND 7   THEN '1-7 days'
            WHEN DATEDIFF(DAY, h.OrderDate, r.ReturnDate) BETWEEN 8 AND 14  THEN '8-14 days'
            WHEN DATEDIFF(DAY, h.OrderDate, r.ReturnDate) BETWEEN 15 AND 30 THEN '15-30 days'
            WHEN DATEDIFF(DAY, h.OrderDate, r.ReturnDate) BETWEEN 31 AND 60 THEN '31-60 days'
            ELSE '60+ days'
        END AS DaysBand
        FROM [Returns] r
        JOIN OrderHeader h ON h.OrderNumber = r.OrderNumber
    ) x
    GROUP BY DaysBand
    ORDER BY DaysBand;
    -- EXPECTED: spread across 1-60 days (config min_days_after_sale/max_days_after_sale)
END

-- 6c. Return reason distribution
SELECT
    rr.ReturnReason,
    COUNT(*)                                                        AS Returns,
    CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS Pct
FROM [Returns] r
JOIN ReturnReason rr ON rr.ReturnReasonKey = r.ReturnReasonKey
GROUP BY rr.ReturnReason
ORDER BY Returns DESC;
-- EXPECTED: varied distribution across reasons


-- ============================================================================
-- 7. FACT DISTRIBUTION SCORECARD
-- ============================================================================
IF @has_sales = 1
BEGIN
    SELECT
        'Sales: monthly CoV > 0.10' AS [Check],
        'Monthly sales volume should vary (seasonal demand); FAIL = data is too uniform/flat' AS [Description],
        CASE WHEN x.CoV > 0.10 THEN 'PASS' ELSE 'FAIL' END AS Result
    FROM (
        SELECT STDEV(MonthlySales) / NULLIF(AVG(MonthlySales), 0) AS CoV
        FROM (
            SELECT YEAR(OrderDate) * 100 + MONTH(OrderDate) AS YM, COUNT(*) AS MonthlySales
            FROM Sales GROUP BY YEAR(OrderDate), MONTH(OrderDate)
        ) m
    ) x

    UNION ALL

    SELECT 'Sales: no customer > 5% of revenue',
        'No single customer should dominate total revenue; FAIL = concentration risk, likely a sampling bug',
        CASE WHEN MAX(CustPct) < 5.0 THEN 'PASS' ELSE 'FAIL' END
    FROM (
        SELECT SUM(NetPrice) * 100.0 / (SELECT SUM(NetPrice) FROM Sales) AS CustPct
        FROM Sales GROUP BY CustomerKey
    ) x

    UNION ALL

    SELECT 'Sales: quantity distribution peaks at 1',
        'Poisson quantity model means most lines have Qty=1 (>30%); FAIL = quantity distribution is wrong',
        CASE WHEN x.Mode1Pct > 30 THEN 'PASS' ELSE 'FAIL' END
    FROM (
        SELECT SUM(CASE WHEN Quantity = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS Mode1Pct
        FROM Sales
    ) x

    UNION ALL

    SELECT 'Sales: NetPrice <= UnitPrice',
        'NetPrice must not exceed UnitPrice (selling price cannot exceed sticker price); FAIL = net exceeds list',
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
    FROM Sales WHERE NetPrice > UnitPrice

    UNION ALL

    SELECT 'Returns: rate within 1-5%',
        'Return rate should match config (~3%); FAIL = returns are missing or over-generated',
        CASE WHEN x.RetRate BETWEEN 1.0 AND 5.0 THEN 'PASS' ELSE 'FAIL' END
    FROM (
        SELECT COUNT(DISTINCT r.OrderNumber) * 100.0
               / NULLIF((SELECT COUNT(DISTINCT OrderNumber) FROM Sales), 0) AS RetRate
        FROM [Returns] r
    ) x;
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        'Sales: monthly CoV > 0.10' AS [Check],
        'Monthly sales volume should vary (seasonal demand); FAIL = data is too uniform/flat' AS [Description],
        CASE WHEN x.CoV > 0.10 THEN 'PASS' ELSE 'FAIL' END AS Result
    FROM (
        SELECT STDEV(MonthlySales) / NULLIF(AVG(MonthlySales), 0) AS CoV
        FROM (
            SELECT YEAR(OrderDate) * 100 + MONTH(OrderDate) AS YM, COUNT(*) AS MonthlySales
            FROM OrderHeader GROUP BY YEAR(OrderDate), MONTH(OrderDate)
        ) m
    ) x

    UNION ALL

    SELECT 'Sales: no customer > 5% of revenue',
        'No single customer should dominate total revenue; FAIL = concentration risk, likely a sampling bug',
        CASE WHEN MAX(CustPct) < 5.0 THEN 'PASS' ELSE 'FAIL' END
    FROM (
        SELECT SUM(d.NetPrice) * 100.0 / (
            SELECT SUM(d2.NetPrice) FROM OrderHeader h2
            JOIN OrderDetail d2 ON d2.OrderNumber = h2.OrderNumber
        ) AS CustPct
        FROM OrderHeader h
        JOIN OrderDetail d ON d.OrderNumber = h.OrderNumber
        GROUP BY h.CustomerKey
    ) x

    UNION ALL

    SELECT 'Sales: quantity distribution peaks at 1',
        'Poisson quantity model means most lines have Qty=1 (>30%); FAIL = quantity distribution is wrong',
        CASE WHEN x.Mode1Pct > 30 THEN 'PASS' ELSE 'FAIL' END
    FROM (
        SELECT SUM(CASE WHEN Quantity = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS Mode1Pct
        FROM OrderDetail
    ) x

    UNION ALL

    SELECT 'Sales: NetPrice <= UnitPrice',
        'NetPrice must not exceed UnitPrice (selling price cannot exceed sticker price); FAIL = net exceeds list',
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
    FROM OrderDetail WHERE NetPrice > UnitPrice

    UNION ALL

    SELECT 'Returns: rate within 1-5%',
        'Return rate should match config (~3%); FAIL = returns are missing or over-generated',
        CASE WHEN x.RetRate BETWEEN 1.0 AND 5.0 THEN 'PASS' ELSE 'FAIL' END
    FROM (
        SELECT COUNT(DISTINCT r.OrderNumber) * 100.0
               / NULLIF((SELECT COUNT(DISTINCT OrderNumber) FROM OrderHeader), 0) AS RetRate
        FROM [Returns] r
    ) x;
END
