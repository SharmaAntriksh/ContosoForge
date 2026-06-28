-- ============================================================================
-- VERIFY BUSINESS CORRELATIONS IN GENERATED SALES DATA
-- Run after loading generated data into SQL Server.
-- Each query returns evidence of correlation (or lack thereof).
--
-- Supports both sales_output modes:
--   sales        -> single Sales table
--   sales_order  -> OrderHeader + OrderDetail tables
-- ============================================================================

-- Detect which sales table exists
DECLARE @has_sales  BIT = CASE WHEN OBJECT_ID('dbo.Sales') IS NOT NULL THEN 1 ELSE 0 END;
DECLARE @has_header BIT = CASE WHEN OBJECT_ID('dbo.OrderHeader') IS NOT NULL THEN 1 ELSE 0 END;


-- ============================================================================
-- 1. StoreKey <-> ChannelKey
--    Physical stores should skew toward physical channels (1=Store, 10=Kiosk)
--    Online/Fulfillment stores should skew toward digital (2,3,6,7,8)
-- ============================================================================

-- 1a. Channel distribution by StoreType (should NOT be uniform)
IF @has_sales = 1
BEGIN
    SELECT
        s.StoreType,
        sc.Channel,
        COUNT(*)                                         AS SalesCount,
        CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (PARTITION BY s.StoreType) AS DECIMAL(5,1)) AS PctOfStoreType
    FROM Sales f
    JOIN Stores s         ON s.StoreKey = f.StoreKey
    JOIN Channels sc ON sc.ChannelKey = f.ChannelKey
    GROUP BY s.StoreType, sc.Channel
    ORDER BY s.StoreType, SalesCount DESC;
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        s.StoreType,
        sc.Channel,
        COUNT(*)                                         AS SalesCount,
        CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (PARTITION BY s.StoreType) AS DECIMAL(5,1)) AS PctOfStoreType
    FROM OrderHeader h
    JOIN Stores s         ON s.StoreKey = h.StoreKey
    JOIN Channels sc ON sc.ChannelKey = h.ChannelKey
    GROUP BY s.StoreType, sc.Channel
    ORDER BY s.StoreType, SalesCount DESC;
END

-- 1b. Quick check: do Convenience stores ever sell via Marketplace?
--     (Should be rare or zero with correlation; common without)
IF @has_sales = 1
BEGIN
    SELECT
        s.StoreType,
        sc.Channel,
        COUNT(*) AS Cnt
    FROM Sales f
    JOIN Stores s         ON s.StoreKey = f.StoreKey
    JOIN Channels sc ON sc.ChannelKey = f.ChannelKey
    WHERE s.StoreType = 'Convenience'
      AND sc.Channel IN ('Marketplace', 'SocialCommerce', 'Web', 'MobileApp')
    GROUP BY s.StoreType, sc.Channel;
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        s.StoreType,
        sc.Channel,
        COUNT(*) AS Cnt
    FROM OrderHeader h
    JOIN Stores s         ON s.StoreKey = h.StoreKey
    JOIN Channels sc ON sc.ChannelKey = h.ChannelKey
    WHERE s.StoreType = 'Convenience'
      AND sc.Channel IN ('Marketplace', 'SocialCommerce', 'Web', 'MobileApp')
    GROUP BY s.StoreType, sc.Channel;
END

-- 1c. Chi-square independence check (simplified):
--     If correlated, the ratio of Store-channel sales for physical vs online
--     store types should differ significantly.
IF @has_sales = 1
BEGIN
    SELECT
        StoreGroup,
        SUM(CASE WHEN ChannelGroup = 'Physical' THEN 1 ELSE 0 END) AS PhysicalChannelSales,
        SUM(CASE WHEN ChannelGroup = 'Digital'  THEN 1 ELSE 0 END) AS DigitalChannelSales,
        CAST(SUM(CASE WHEN ChannelGroup = 'Physical' THEN 1 ELSE 0 END) * 100.0
           / NULLIF(COUNT(*), 0) AS DECIMAL(5,1))                   AS PctPhysical
    FROM (
        SELECT
            CASE WHEN s.StoreType IN ('Online', 'Fulfillment') THEN 'Online/Fulfillment'
                 ELSE 'Physical Store' END                          AS StoreGroup,
            CASE WHEN sc.IsPhysical = 1 THEN 'Physical'
                 WHEN sc.IsDigital = 1  THEN 'Digital'
                 ELSE 'Other' END                                   AS ChannelGroup
        FROM Sales f
        JOIN Stores s         ON s.StoreKey = f.StoreKey
        JOIN Channels sc ON sc.ChannelKey = f.ChannelKey
    ) x
    GROUP BY StoreGroup;
    -- EXPECTED: Physical Stores should show >50% PhysicalChannelSales
    --           Online/Fulfillment should show >70% DigitalChannelSales
    -- NO CORRELATION: both groups ~equal split
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        StoreGroup,
        SUM(CASE WHEN ChannelGroup = 'Physical' THEN 1 ELSE 0 END) AS PhysicalChannelSales,
        SUM(CASE WHEN ChannelGroup = 'Digital'  THEN 1 ELSE 0 END) AS DigitalChannelSales,
        CAST(SUM(CASE WHEN ChannelGroup = 'Physical' THEN 1 ELSE 0 END) * 100.0
           / NULLIF(COUNT(*), 0) AS DECIMAL(5,1))                   AS PctPhysical
    FROM (
        SELECT
            CASE WHEN s.StoreType IN ('Online', 'Fulfillment') THEN 'Online/Fulfillment'
                 ELSE 'Physical Store' END                          AS StoreGroup,
            CASE WHEN sc.IsPhysical = 1 THEN 'Physical'
                 WHEN sc.IsDigital = 1  THEN 'Digital'
                 ELSE 'Other' END                                   AS ChannelGroup
        FROM OrderHeader h
        JOIN Stores s         ON s.StoreKey = h.StoreKey
        JOIN Channels sc ON sc.ChannelKey = h.ChannelKey
    ) x
    GROUP BY StoreGroup;
    -- EXPECTED: Physical Stores should show >50% PhysicalChannelSales
    --           Online/Fulfillment should show >70% DigitalChannelSales
    -- NO CORRELATION: both groups ~equal split
END


-- ============================================================================
-- 2. CustomerKey <-> StoreKey (Geographic Affinity)
--    Customers should mostly buy from stores in their own country.
-- ============================================================================

-- 2a. % of sales where customer and store are in the same country
IF @has_sales = 1
BEGIN
    SELECT
        COUNT(*)                                                        AS TotalSales,
        SUM(CASE WHEN cg.Country = sg.Country THEN 1 ELSE 0 END)       AS SameCountrySales,
        CAST(SUM(CASE WHEN cg.Country = sg.Country THEN 1 ELSE 0 END)
             * 100.0 / NULLIF(COUNT(*), 0) AS DECIMAL(5,1))            AS PctSameCountry
    FROM Sales f
    JOIN Customers c  ON c.CustomerKey = f.CustomerKey AND c.IsCurrent = 1
    JOIN Stores s     ON s.StoreKey = f.StoreKey
    JOIN Geography cg ON cg.GeographyKey = c.GeographyKey
    JOIN Geography sg ON sg.GeographyKey = s.GeographyKey;
    -- EXPECTED WITH CORRELATION: ~60-75% same country
    -- NO CORRELATION: proportional to store count per country (likely <30% for multi-country data)
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        COUNT(*)                                                        AS TotalSales,
        SUM(CASE WHEN cg.Country = sg.Country THEN 1 ELSE 0 END)       AS SameCountrySales,
        CAST(SUM(CASE WHEN cg.Country = sg.Country THEN 1 ELSE 0 END)
             * 100.0 / NULLIF(COUNT(*), 0) AS DECIMAL(5,1))            AS PctSameCountry
    FROM OrderHeader h
    JOIN Customers c  ON c.CustomerKey = h.CustomerKey AND c.IsCurrent = 1
    JOIN Stores s     ON s.StoreKey = h.StoreKey
    JOIN Geography cg ON cg.GeographyKey = c.GeographyKey
    JOIN Geography sg ON sg.GeographyKey = s.GeographyKey;
    -- EXPECTED WITH CORRELATION: ~60-75% same country
END

-- 2b. Top customer-store country pairs
IF @has_sales = 1
BEGIN
    SELECT TOP 20
        cg.Country AS CustomerCountry,
        sg.Country AS StoreCountry,
        COUNT(*)   AS SalesCount,
        CASE WHEN cg.Country = sg.Country THEN 'MATCH' ELSE 'CROSS' END AS GeoMatch
    FROM Sales f
    JOIN Customers c  ON c.CustomerKey = f.CustomerKey AND c.IsCurrent = 1
    JOIN Stores s     ON s.StoreKey = f.StoreKey
    JOIN Geography cg ON cg.GeographyKey = c.GeographyKey
    JOIN Geography sg ON sg.GeographyKey = s.GeographyKey
    GROUP BY cg.Country, sg.Country
    ORDER BY SalesCount DESC;
END
ELSE IF @has_header = 1
BEGIN
    SELECT TOP 20
        cg.Country AS CustomerCountry,
        sg.Country AS StoreCountry,
        COUNT(*)   AS SalesCount,
        CASE WHEN cg.Country = sg.Country THEN 'MATCH' ELSE 'CROSS' END AS GeoMatch
    FROM OrderHeader h
    JOIN Customers c  ON c.CustomerKey = h.CustomerKey AND c.IsCurrent = 1
    JOIN Stores s     ON s.StoreKey = h.StoreKey
    JOIN Geography cg ON cg.GeographyKey = c.GeographyKey
    JOIN Geography sg ON sg.GeographyKey = s.GeographyKey
    GROUP BY cg.Country, sg.Country
    ORDER BY SalesCount DESC;
END

-- 2c. Per-country local purchase rate
IF @has_sales = 1
BEGIN
    SELECT
        cg.Country,
        COUNT(*)                                                        AS TotalSales,
        SUM(CASE WHEN cg.Country = sg.Country THEN 1 ELSE 0 END)       AS LocalSales,
        CAST(SUM(CASE WHEN cg.Country = sg.Country THEN 1 ELSE 0 END)
             * 100.0 / NULLIF(COUNT(*), 0) AS DECIMAL(5,1))            AS PctLocal
    FROM Sales f
    JOIN Customers c  ON c.CustomerKey = f.CustomerKey AND c.IsCurrent = 1
    JOIN Stores s     ON s.StoreKey = f.StoreKey
    JOIN Geography cg ON cg.GeographyKey = c.GeographyKey
    JOIN Geography sg ON sg.GeographyKey = s.GeographyKey
    GROUP BY cg.Country
    ORDER BY TotalSales DESC;
    -- EXPECTED: each country should show 60-75% local
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        cg.Country,
        COUNT(*)                                                        AS TotalSales,
        SUM(CASE WHEN cg.Country = sg.Country THEN 1 ELSE 0 END)       AS LocalSales,
        CAST(SUM(CASE WHEN cg.Country = sg.Country THEN 1 ELSE 0 END)
             * 100.0 / NULLIF(COUNT(*), 0) AS DECIMAL(5,1))            AS PctLocal
    FROM OrderHeader h
    JOIN Customers c  ON c.CustomerKey = h.CustomerKey AND c.IsCurrent = 1
    JOIN Stores s     ON s.StoreKey = h.StoreKey
    JOIN Geography cg ON cg.GeographyKey = c.GeographyKey
    JOIN Geography sg ON sg.GeographyKey = s.GeographyKey
    GROUP BY cg.Country
    ORDER BY TotalSales DESC;
    -- EXPECTED: each country should show 60-75% local
END


-- ============================================================================
-- 3. ChannelKey <-> DeliveryDate
--    Store/Kiosk channels should have 0-day DueDate offset.
--    Online channels should have 2-5 day offset.
--    B2B should have 5-10 day offset.
-- ============================================================================

-- 3a. Average due-date offset by channel
IF @has_sales = 1
BEGIN
    SELECT
        sc.Channel,
        sc.TypicalFulfillmentDays,
        COUNT(*)                                                               AS SalesCount,
        AVG(CAST(DATEDIFF(DAY, f.OrderDate, f.DueDate) AS FLOAT))             AS AvgDueDays,
        MIN(DATEDIFF(DAY, f.OrderDate, f.DueDate))                            AS MinDueDays,
        MAX(DATEDIFF(DAY, f.OrderDate, f.DueDate))                            AS MaxDueDays
    FROM Sales f
    JOIN Channels sc ON sc.ChannelKey = f.ChannelKey
    GROUP BY sc.Channel, sc.TypicalFulfillmentDays
    ORDER BY AvgDueDays;
    -- EXPECTED: Store/Kiosk AvgDueDays ~0-1, Online ~2-4, B2B ~6-8
    -- NO CORRELATION: all channels show AvgDueDays ~5 (uniform 3-7)
END
ELSE IF @has_header = 1
BEGIN
    -- In sales_order mode, DueDate is on OrderDetail
    SELECT
        sc.Channel,
        sc.TypicalFulfillmentDays,
        COUNT(*)                                                               AS SalesCount,
        AVG(CAST(DATEDIFF(DAY, h.OrderDate, d.DueDate) AS FLOAT))             AS AvgDueDays,
        MIN(DATEDIFF(DAY, h.OrderDate, d.DueDate))                            AS MinDueDays,
        MAX(DATEDIFF(DAY, h.OrderDate, d.DueDate))                            AS MaxDueDays
    FROM OrderHeader h
    JOIN OrderDetail d ON d.OrderNumber = h.OrderNumber
    JOIN Channels sc   ON sc.ChannelKey = h.ChannelKey
    GROUP BY sc.Channel, sc.TypicalFulfillmentDays
    ORDER BY AvgDueDays;
    -- EXPECTED: Store/Kiosk AvgDueDays ~0-1, Online ~2-4, B2B ~6-8
END

-- 3b. Distribution of due-date offsets for physical vs digital channels
IF @has_sales = 1
BEGIN
    SELECT
        ChannelType,
        DueDays,
        COUNT(*) AS Cnt
    FROM (
        SELECT
            CASE WHEN sc.IsPhysical = 1 THEN 'Physical'
                 WHEN sc.IsDigital  = 1 THEN 'Digital'
                 WHEN sc.IsB2B      = 1 THEN 'B2B'
                 ELSE 'Other' END                               AS ChannelType,
            DATEDIFF(DAY, f.OrderDate, f.DueDate)               AS DueDays
        FROM Sales f
        JOIN Channels sc ON sc.ChannelKey = f.ChannelKey
    ) x
    GROUP BY ChannelType, DueDays
    ORDER BY ChannelType, DueDays;
    -- EXPECTED: Physical peaks at 0, Digital peaks at 2-4, B2B peaks at 5-8
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        ChannelType,
        DueDays,
        COUNT(*) AS Cnt
    FROM (
        SELECT
            CASE WHEN sc.IsPhysical = 1 THEN 'Physical'
                 WHEN sc.IsDigital  = 1 THEN 'Digital'
                 WHEN sc.IsB2B      = 1 THEN 'B2B'
                 ELSE 'Other' END                               AS ChannelType,
            DATEDIFF(DAY, h.OrderDate, d.DueDate)               AS DueDays
        FROM OrderHeader h
        JOIN OrderDetail d ON d.OrderNumber = h.OrderNumber
        JOIN Channels sc   ON sc.ChannelKey = h.ChannelKey
    ) x
    GROUP BY ChannelType, DueDays
    ORDER BY ChannelType, DueDays;
    -- EXPECTED: Physical peaks at 0, Digital peaks at 2-4, B2B peaks at 5-8
END

-- 3c. Same-day delivery should only come from physical channels
IF @has_sales = 1
BEGIN
    SELECT
        sc.Channel,
        sc.IsPhysical,
        COUNT(*) AS SameDayDeliveries
    FROM Sales f
    JOIN Channels sc ON sc.ChannelKey = f.ChannelKey
    WHERE f.DueDate = f.OrderDate
    GROUP BY sc.Channel, sc.IsPhysical
    ORDER BY SameDayDeliveries DESC;
    -- EXPECTED: overwhelmingly Store + Kiosk
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        sc.Channel,
        sc.IsPhysical,
        COUNT(*) AS SameDayDeliveries
    FROM OrderHeader h
    JOIN OrderDetail d ON d.OrderNumber = h.OrderNumber
    JOIN Channels sc   ON sc.ChannelKey = h.ChannelKey
    WHERE d.DueDate = h.OrderDate
    GROUP BY sc.Channel, sc.IsPhysical
    ORDER BY SameDayDeliveries DESC;
    -- EXPECTED: overwhelmingly Store + Kiosk
END


-- ============================================================================
-- 4. ChannelKey <-> ProductKey (Channel Eligibility)
--    Products marked EligibleOnline=0 should rarely appear in Online sales.
--    Products marked EligibleStore=0 should rarely appear in Store sales.
-- ============================================================================

-- 4a. Ineligible product sales by channel (should be very low %)
IF @has_sales = 1
BEGIN
    SELECT
        ChannelGroup,
        COUNT(*)                                                                AS TotalSales,
        SUM(CASE WHEN IsEligible = 0 THEN 1 ELSE 0 END)                        AS IneligibleSales,
        CAST(SUM(CASE WHEN IsEligible = 0 THEN 1 ELSE 0 END) * 100.0
             / NULLIF(COUNT(*), 0) AS DECIMAL(5,2))                             AS PctIneligible
    FROM (
        SELECT
            CASE
                WHEN f.ChannelKey IN (1, 10)      THEN 'Store'
                WHEN f.ChannelKey IN (2, 6, 7)    THEN 'Online'
                WHEN f.ChannelKey IN (3, 8)       THEN 'Marketplace'
                WHEN f.ChannelKey IN (4, 9)       THEN 'B2B'
                ELSE 'Other'
            END AS ChannelGroup,
            CASE
                WHEN f.ChannelKey IN (1, 10)   AND pp.EligibleStore       = 1 THEN 1
                WHEN f.ChannelKey IN (2, 6, 7) AND pp.EligibleOnline      = 1 THEN 1
                WHEN f.ChannelKey IN (3, 8)    AND pp.EligibleMarketplace  = 1 THEN 1
                WHEN f.ChannelKey IN (4, 9)    AND pp.EligibleB2B         = 1 THEN 1
                ELSE 0
            END AS IsEligible
        FROM Sales f
        JOIN Products p        ON p.ProductKey = f.ProductKey
        JOIN ProductProfile pp ON pp.ProductKey = p.ProductKey
    ) x
    GROUP BY ChannelGroup
    ORDER BY PctIneligible DESC;
    -- EXPECTED WITH CORRELATION: PctIneligible < 5% (soft penalty, not hard block)
    -- NO CORRELATION: PctIneligible matches overall ineligibility rate (~15-25%)
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        ChannelGroup,
        COUNT(*)                                                                AS TotalSales,
        SUM(CASE WHEN IsEligible = 0 THEN 1 ELSE 0 END)                        AS IneligibleSales,
        CAST(SUM(CASE WHEN IsEligible = 0 THEN 1 ELSE 0 END) * 100.0
             / NULLIF(COUNT(*), 0) AS DECIMAL(5,2))                             AS PctIneligible
    FROM (
        SELECT
            CASE
                WHEN h.ChannelKey IN (1, 10)      THEN 'Store'
                WHEN h.ChannelKey IN (2, 6, 7)    THEN 'Online'
                WHEN h.ChannelKey IN (3, 8)       THEN 'Marketplace'
                WHEN h.ChannelKey IN (4, 9)       THEN 'B2B'
                ELSE 'Other'
            END AS ChannelGroup,
            CASE
                WHEN h.ChannelKey IN (1, 10)   AND pp.EligibleStore       = 1 THEN 1
                WHEN h.ChannelKey IN (2, 6, 7) AND pp.EligibleOnline      = 1 THEN 1
                WHEN h.ChannelKey IN (3, 8)    AND pp.EligibleMarketplace  = 1 THEN 1
                WHEN h.ChannelKey IN (4, 9)    AND pp.EligibleB2B         = 1 THEN 1
                ELSE 0
            END AS IsEligible
        FROM OrderHeader h
        JOIN OrderDetail d ON d.OrderNumber = h.OrderNumber
        JOIN Products p         ON p.ProductKey = d.ProductKey
        JOIN ProductProfile pp  ON pp.ProductKey = p.ProductKey
    ) x
    GROUP BY ChannelGroup
    ORDER BY PctIneligible DESC;
    -- EXPECTED WITH CORRELATION: PctIneligible < 5% (soft penalty, not hard block)
END

-- 4b. Store-only products appearing in digital channels
IF @has_sales = 1
BEGIN
    SELECT
        sc.Channel,
        p.ProductName,
        pp.EligibleStore,
        pp.EligibleOnline,
        COUNT(*) AS SalesCount
    FROM Sales f
    JOIN Products p        ON p.ProductKey = f.ProductKey AND p.IsCurrent = 1
    JOIN ProductProfile pp ON pp.ProductKey = p.ProductKey
    JOIN Channels sc  ON sc.ChannelKey = f.ChannelKey
    WHERE pp.EligibleOnline = 0
      AND sc.IsDigital = 1
    GROUP BY sc.Channel, p.ProductName, pp.EligibleStore, pp.EligibleOnline
    ORDER BY SalesCount DESC;
    -- EXPECTED: very few rows (soft penalty allows ~5%)
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        sc.Channel,
        p.ProductName,
        pp.EligibleStore,
        pp.EligibleOnline,
        COUNT(*) AS SalesCount
    FROM OrderHeader h
    JOIN OrderDetail d ON d.OrderNumber = h.OrderNumber
    JOIN Products p         ON p.ProductKey = d.ProductKey AND p.IsCurrent = 1
    JOIN ProductProfile pp  ON pp.ProductKey = p.ProductKey
    JOIN Channels sc   ON sc.ChannelKey = h.ChannelKey
    WHERE pp.EligibleOnline = 0
      AND sc.IsDigital = 1
    GROUP BY sc.Channel, p.ProductName, pp.EligibleStore, pp.EligibleOnline
    ORDER BY SalesCount DESC;
    -- EXPECTED: very few rows (soft penalty allows ~5%)
END

-- 4c. Eligibility compliance rate by channel
IF @has_sales = 1
BEGIN
    SELECT
        sc.Channel,
        COUNT(*) AS TotalSales,
        SUM(CASE
            WHEN sc.ChannelKey IN (1, 10)   THEN pp.EligibleStore
            WHEN sc.ChannelKey IN (2, 6, 7) THEN pp.EligibleOnline
            WHEN sc.ChannelKey IN (3, 8)    THEN pp.EligibleMarketplace
            WHEN sc.ChannelKey IN (4, 9)    THEN pp.EligibleB2B
            ELSE 1
        END) AS EligibleSales,
        CAST(SUM(CASE
            WHEN sc.ChannelKey IN (1, 10)   THEN pp.EligibleStore
            WHEN sc.ChannelKey IN (2, 6, 7) THEN pp.EligibleOnline
            WHEN sc.ChannelKey IN (3, 8)    THEN pp.EligibleMarketplace
            WHEN sc.ChannelKey IN (4, 9)    THEN pp.EligibleB2B
            ELSE 1
        END) * 100.0 / NULLIF(COUNT(*), 0) AS DECIMAL(5,1)) AS CompliancePct
    FROM Sales f
    JOIN Products p        ON p.ProductKey = f.ProductKey
    JOIN ProductProfile pp ON pp.ProductKey = p.ProductKey
    JOIN Channels sc  ON sc.ChannelKey = f.ChannelKey
    GROUP BY sc.Channel
    ORDER BY CompliancePct;
    -- EXPECTED: >95% compliance across all channels
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        sc.Channel,
        COUNT(*) AS TotalSales,
        SUM(CASE
            WHEN sc.ChannelKey IN (1, 10)   THEN pp.EligibleStore
            WHEN sc.ChannelKey IN (2, 6, 7) THEN pp.EligibleOnline
            WHEN sc.ChannelKey IN (3, 8)    THEN pp.EligibleMarketplace
            WHEN sc.ChannelKey IN (4, 9)    THEN pp.EligibleB2B
            ELSE 1
        END) AS EligibleSales,
        CAST(SUM(CASE
            WHEN sc.ChannelKey IN (1, 10)   THEN pp.EligibleStore
            WHEN sc.ChannelKey IN (2, 6, 7) THEN pp.EligibleOnline
            WHEN sc.ChannelKey IN (3, 8)    THEN pp.EligibleMarketplace
            WHEN sc.ChannelKey IN (4, 9)    THEN pp.EligibleB2B
            ELSE 1
        END) * 100.0 / NULLIF(COUNT(*), 0) AS DECIMAL(5,1)) AS CompliancePct
    FROM OrderHeader h
    JOIN OrderDetail d ON d.OrderNumber = h.OrderNumber
    JOIN Products p         ON p.ProductKey = d.ProductKey
    JOIN ProductProfile pp  ON pp.ProductKey = p.ProductKey
    JOIN Channels sc   ON sc.ChannelKey = h.ChannelKey
    GROUP BY sc.Channel
    ORDER BY CompliancePct;
    -- EXPECTED: >95% compliance across all channels
END


-- ============================================================================
-- 5. ChannelKey <-> PromotionKey (Channel-Targeted Promos)
--    Promotions with PromotionCategory='Store' should only appear
--    with physical channels. 'Online' promos only with digital channels.
-- ============================================================================

-- 5a. Promo category vs channel type alignment
IF @has_sales = 1
BEGIN
    SELECT
        pr.PromotionCategory,
        CASE WHEN sc.IsPhysical = 1 THEN 'Physical'
             WHEN sc.IsDigital  = 1 THEN 'Digital'
             WHEN sc.IsB2B      = 1 THEN 'B2B'
             ELSE 'Other' END                                   AS ChannelType,
        COUNT(*)                                                 AS SalesCount
    FROM Sales f
    JOIN Promotions pr     ON pr.PromotionKey = f.PromotionKey
    JOIN Channels sc  ON sc.ChannelKey = f.ChannelKey
    WHERE pr.PromotionKey > 1  -- exclude "No Discount"
    GROUP BY pr.PromotionCategory,
             CASE WHEN sc.IsPhysical = 1 THEN 'Physical'
                  WHEN sc.IsDigital  = 1 THEN 'Digital'
                  WHEN sc.IsB2B      = 1 THEN 'B2B'
                  ELSE 'Other' END
    ORDER BY pr.PromotionCategory, SalesCount DESC;
    -- EXPECTED: 'Store' category -> mostly Physical channel
    --           'Online' category -> mostly Digital channel
    -- NO CORRELATION: uniform distribution across channel types
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        pr.PromotionCategory,
        CASE WHEN sc.IsPhysical = 1 THEN 'Physical'
             WHEN sc.IsDigital  = 1 THEN 'Digital'
             WHEN sc.IsB2B      = 1 THEN 'B2B'
             ELSE 'Other' END                                   AS ChannelType,
        COUNT(*)                                                 AS SalesCount
    FROM OrderHeader h
    JOIN Promotions pr     ON pr.PromotionKey = h.PromotionKey
    JOIN Channels sc  ON sc.ChannelKey = h.ChannelKey
    WHERE pr.PromotionKey > 1  -- exclude "No Discount"
    GROUP BY pr.PromotionCategory,
             CASE WHEN sc.IsPhysical = 1 THEN 'Physical'
                  WHEN sc.IsDigital  = 1 THEN 'Digital'
                  WHEN sc.IsB2B      = 1 THEN 'B2B'
                  ELSE 'Other' END
    ORDER BY pr.PromotionCategory, SalesCount DESC;
    -- EXPECTED: 'Store' category -> mostly Physical channel
    --           'Online' category -> mostly Digital channel
END

-- 5b. Misaligned promos (Store promo on Digital channel or vice versa)
IF @has_sales = 1
BEGIN
    SELECT
        'Store promo on Digital channel' AS MismatchType,
        COUNT(*) AS MismatchCount,
        CAST(COUNT(*) * 100.0 / NULLIF(
            (SELECT COUNT(*) FROM Sales f2
             JOIN Promotions pr2 ON pr2.PromotionKey = f2.PromotionKey
             WHERE pr2.PromotionCategory = 'Store' AND pr2.PromotionKey > 1), 0
        ) AS DECIMAL(5,1)) AS PctOfCategoryTotal
    FROM Sales f
    JOIN Promotions pr    ON pr.PromotionKey = f.PromotionKey
    JOIN Channels sc ON sc.ChannelKey = f.ChannelKey
    WHERE pr.PromotionCategory = 'Store'
      AND pr.PromotionKey > 1
      AND sc.IsDigital = 1

    UNION ALL

    SELECT
        'Online promo on Physical channel' AS MismatchType,
        COUNT(*) AS MismatchCount,
        CAST(COUNT(*) * 100.0 / NULLIF(
            (SELECT COUNT(*) FROM Sales f2
             JOIN Promotions pr2 ON pr2.PromotionKey = f2.PromotionKey
             WHERE pr2.PromotionCategory = 'Online' AND pr2.PromotionKey > 1), 0
        ) AS DECIMAL(5,1)) AS PctOfCategoryTotal
    FROM Sales f
    JOIN Promotions pr    ON pr.PromotionKey = f.PromotionKey
    JOIN Channels sc ON sc.ChannelKey = f.ChannelKey
    WHERE pr.PromotionCategory = 'Online'
      AND pr.PromotionKey > 1
      AND sc.IsPhysical = 1;
    -- EXPECTED WITH CORRELATION: 0% mismatch
    -- NO CORRELATION: ~50% mismatch (random assignment)
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        'Store promo on Digital channel' AS MismatchType,
        COUNT(*) AS MismatchCount,
        CAST(COUNT(*) * 100.0 / NULLIF(
            (SELECT COUNT(*) FROM OrderHeader h2
             JOIN Promotions pr2 ON pr2.PromotionKey = h2.PromotionKey
             WHERE pr2.PromotionCategory = 'Store' AND pr2.PromotionKey > 1), 0
        ) AS DECIMAL(5,1)) AS PctOfCategoryTotal
    FROM OrderHeader h
    JOIN Promotions pr    ON pr.PromotionKey = h.PromotionKey
    JOIN Channels sc ON sc.ChannelKey = h.ChannelKey
    WHERE pr.PromotionCategory = 'Store'
      AND pr.PromotionKey > 1
      AND sc.IsDigital = 1

    UNION ALL

    SELECT
        'Online promo on Physical channel' AS MismatchType,
        COUNT(*) AS MismatchCount,
        CAST(COUNT(*) * 100.0 / NULLIF(
            (SELECT COUNT(*) FROM OrderHeader h2
             JOIN Promotions pr2 ON pr2.PromotionKey = h2.PromotionKey
             WHERE pr2.PromotionCategory = 'Online' AND pr2.PromotionKey > 1), 0
        ) AS DECIMAL(5,1)) AS PctOfCategoryTotal
    FROM OrderHeader h
    JOIN Promotions pr    ON pr.PromotionKey = h.PromotionKey
    JOIN Channels sc ON sc.ChannelKey = h.ChannelKey
    WHERE pr.PromotionCategory = 'Online'
      AND pr.PromotionKey > 1
      AND sc.IsPhysical = 1;
    -- EXPECTED WITH CORRELATION: 0% mismatch
END


-- ============================================================================
-- BONUS: Combined correlation health scorecard
-- ============================================================================
IF @has_sales = 1
BEGIN
    SELECT
        'Store-Channel'      AS Correlation,
        'Physical stores use physical channels >50%' AS Expectation,
        CAST((
            SELECT SUM(CASE WHEN sc.IsPhysical = 1 THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0)
            FROM Sales f
            JOIN Stores s         ON s.StoreKey = f.StoreKey
            JOIN Channels sc ON sc.ChannelKey = f.ChannelKey
            WHERE s.StoreType NOT IN ('Online', 'Fulfillment')
        ) AS DECIMAL(5,1)) AS ActualPct

    UNION ALL

    SELECT
        'Customer-Store Geo',
        'Same-country sales >60%',
        CAST((
            SELECT SUM(CASE WHEN cg.Country = sg.Country THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0)
            FROM Sales f
            JOIN Customers c  ON c.CustomerKey = f.CustomerKey AND c.IsCurrent = 1
            JOIN Stores s     ON s.StoreKey = f.StoreKey
            JOIN Geography cg ON cg.GeographyKey = c.GeographyKey
            JOIN Geography sg ON sg.GeographyKey = s.GeographyKey
        ) AS DECIMAL(5,1))

    UNION ALL

    SELECT
        'Channel-Delivery',
        'Store/Kiosk avg due days <2',
        CAST((
            SELECT AVG(CAST(DATEDIFF(DAY, f.OrderDate, f.DueDate) AS FLOAT))
            FROM Sales f
            WHERE f.ChannelKey IN (1, 10)
        ) AS DECIMAL(5,1))

    UNION ALL

    SELECT
        'Channel-Product',
        'Channel eligibility compliance >95%',
        CAST((
            SELECT SUM(CASE
                WHEN f.ChannelKey IN (1, 10)   THEN pp.EligibleStore
                WHEN f.ChannelKey IN (2, 6, 7) THEN pp.EligibleOnline
                WHEN f.ChannelKey IN (3, 8)    THEN pp.EligibleMarketplace
                WHEN f.ChannelKey IN (4, 9)    THEN pp.EligibleB2B
                ELSE 1 END) * 100.0 / NULLIF(COUNT(*), 0)
            FROM Sales f
            JOIN Products p        ON p.ProductKey = f.ProductKey
            JOIN ProductProfile pp ON pp.ProductKey = p.ProductKey
        ) AS DECIMAL(5,1))

    UNION ALL

    SELECT
        'Channel-Promo',
        'Store promo on digital channel = 0%',
        CAST(ISNULL((
            SELECT SUM(CASE WHEN sc.IsDigital = 1 THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0)
            FROM Sales f
            JOIN Promotions pr    ON pr.PromotionKey = f.PromotionKey
            JOIN Channels sc ON sc.ChannelKey = f.ChannelKey
            WHERE pr.PromotionCategory = 'Store' AND pr.PromotionKey > 1
        ), 0) AS DECIMAL(5,1));
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        'Store-Channel'      AS Correlation,
        'Physical stores use physical channels >50%' AS Expectation,
        CAST((
            SELECT SUM(CASE WHEN sc.IsPhysical = 1 THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0)
            FROM OrderHeader h
            JOIN Stores s         ON s.StoreKey = h.StoreKey
            JOIN Channels sc ON sc.ChannelKey = h.ChannelKey
            WHERE s.StoreType NOT IN ('Online', 'Fulfillment')
        ) AS DECIMAL(5,1)) AS ActualPct

    UNION ALL

    SELECT
        'Customer-Store Geo',
        'Same-country sales >60%',
        CAST((
            SELECT SUM(CASE WHEN cg.Country = sg.Country THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0)
            FROM OrderHeader h
            JOIN Customers c  ON c.CustomerKey = h.CustomerKey AND c.IsCurrent = 1
            JOIN Stores s     ON s.StoreKey = h.StoreKey
            JOIN Geography cg ON cg.GeographyKey = c.GeographyKey
            JOIN Geography sg ON sg.GeographyKey = s.GeographyKey
        ) AS DECIMAL(5,1))

    UNION ALL

    SELECT
        'Channel-Delivery',
        'Store/Kiosk avg due days <2',
        CAST((
            SELECT AVG(CAST(DATEDIFF(DAY, h.OrderDate, d.DueDate) AS FLOAT))
            FROM OrderHeader h
            JOIN OrderDetail d ON d.OrderNumber = h.OrderNumber
            WHERE h.ChannelKey IN (1, 10)
        ) AS DECIMAL(5,1))

    UNION ALL

    SELECT
        'Channel-Product',
        'Channel eligibility compliance >95%',
        CAST((
            SELECT SUM(CASE
                WHEN h.ChannelKey IN (1, 10)   THEN pp.EligibleStore
                WHEN h.ChannelKey IN (2, 6, 7) THEN pp.EligibleOnline
                WHEN h.ChannelKey IN (3, 8)    THEN pp.EligibleMarketplace
                WHEN h.ChannelKey IN (4, 9)    THEN pp.EligibleB2B
                ELSE 1 END) * 100.0 / NULLIF(COUNT(*), 0)
            FROM OrderHeader h
            JOIN OrderDetail d ON d.OrderNumber = h.OrderNumber
            JOIN Products p         ON p.ProductKey = d.ProductKey
            JOIN ProductProfile pp  ON pp.ProductKey = p.ProductKey
        ) AS DECIMAL(5,1))

    UNION ALL

    SELECT
        'Channel-Promo',
        'Store promo on digital channel = 0%',
        CAST(ISNULL((
            SELECT SUM(CASE WHEN sc.IsDigital = 1 THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0)
            FROM OrderHeader h
            JOIN Promotions pr    ON pr.PromotionKey = h.PromotionKey
            JOIN Channels sc ON sc.ChannelKey = h.ChannelKey
            WHERE pr.PromotionCategory = 'Store' AND pr.PromotionKey > 1
        ), 0) AS DECIMAL(5,1));
END
