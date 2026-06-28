-- ============================================================================
-- VERIFY SCD TYPE 2 BEHAVIOUR FOR PRODUCTS
-- Run after loading generated data into SQL Server.
-- Assumes products.scd2.enabled=true in config.
-- ============================================================================


-- ############################################################################
-- PRODUCT SCD2 — Price Revision Engine
-- ############################################################################

-- ============================================================================
-- 1. Basic SCD2 Structure
-- ============================================================================

-- 1a. Version distribution for products
SELECT
    VersionCount,
    COUNT(*)                                                    AS ProductCount,
    CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS PctOfTotal
FROM (
    SELECT ProductID, COUNT(*) AS VersionCount
    FROM Products
    GROUP BY ProductID
) x
GROUP BY VersionCount
ORDER BY VersionCount;
-- EXPECTED: mix of 1-4 versions (governed by scd2.max_versions)

-- 1b. Only one IsCurrent=1 row per ProductID
SELECT
    ProductID,
    SUM(CAST(IsCurrent AS INT)) AS CurrentCount
FROM Products
GROUP BY ProductID
HAVING SUM(CAST(IsCurrent AS INT)) <> 1;
-- EXPECTED: zero rows

-- 1c. Version numbers are sequential per ProductID
SELECT p.ProductID, p.VersionNumber
FROM Products p
WHERE NOT EXISTS (
    SELECT 1 FROM Products p2
    WHERE p2.ProductID = p.ProductID
      AND p2.VersionNumber = p.VersionNumber - 1
)
AND p.VersionNumber > 1;
-- EXPECTED: zero rows


-- ============================================================================
-- 2. Date Chain Integrity
-- ============================================================================

-- 2a. EffectiveStartDate < EffectiveEndDate for non-current rows
SELECT ProductID, VersionNumber, EffectiveStartDate, EffectiveEndDate
FROM Products
WHERE IsCurrent = 0
  AND EffectiveStartDate >= EffectiveEndDate;
-- EXPECTED: zero rows

-- 2b. Current rows have EffectiveEndDate = '9999-12-31'
SELECT ProductID, VersionNumber, EffectiveEndDate
FROM Products
WHERE IsCurrent = 1
  AND EffectiveEndDate <> '9999-12-31';
-- EXPECTED: zero rows

-- 2c. No gaps or overlaps in date chain per ProductID
SELECT
    p1.ProductID,
    p1.VersionNumber                                    AS PrevVersion,
    p1.EffectiveEndDate                                 AS PrevEnd,
    p2.VersionNumber                                    AS NextVersion,
    p2.EffectiveStartDate                               AS NextStart,
    DATEDIFF(DAY, p1.EffectiveEndDate, p2.EffectiveStartDate) AS GapDays
FROM Products p1
JOIN Products p2
  ON p2.ProductID = p1.ProductID
 AND p2.VersionNumber = p1.VersionNumber + 1
WHERE DATEDIFF(DAY, p1.EffectiveEndDate, p2.EffectiveStartDate) <> 1;
-- EXPECTED: zero rows


-- ============================================================================
-- 3. Price Revision Coherence
-- ============================================================================

-- 3a. Prices should change between versions (that's the point of product SCD2)
SELECT
    COUNT(*) AS TotalTransitions,
    SUM(CASE WHEN p2.ListPrice <> p1.ListPrice THEN 1 ELSE 0 END) AS PriceChanged,
    CAST(SUM(CASE WHEN p2.ListPrice <> p1.ListPrice THEN 1 ELSE 0 END)
         * 100.0 / NULLIF(COUNT(*), 0) AS DECIMAL(5,1))           AS PctPriceChanged
FROM Products p1
JOIN Products p2
  ON p2.ProductID = p1.ProductID
 AND p2.VersionNumber = p1.VersionNumber + 1;
-- EXPECTED: ~100% of transitions have a price change

-- 3b. Price drift magnitude (should be within configured drift range, default ±5%)
SELECT
    AVG(ABS(PriceDriftPct))                    AS AvgDriftPct,
    MIN(PriceDriftPct)                         AS MinDriftPct,
    MAX(PriceDriftPct)                         AS MaxDriftPct,
    STDEV(PriceDriftPct)                       AS StdevDriftPct
FROM (
    SELECT
        p1.ProductID,
        p1.VersionNumber,
        (p2.ListPrice - p1.ListPrice) * 100.0 / NULLIF(p1.ListPrice, 0) AS PriceDriftPct
    FROM Products p1
    JOIN Products p2
      ON p2.ProductID = p1.ProductID
     AND p2.VersionNumber = p1.VersionNumber + 1
    WHERE p1.ListPrice > 0
) x;
-- EXPECTED: AvgDriftPct ~2-3%, MinDriftPct ~-5%, MaxDriftPct ~+10%
--           (drift range is [-price_drift, +price_drift*2], default [-0.05, +0.10])

-- 3c. UnitCost should never exceed ListPrice in any version
SELECT ProductID, VersionNumber, ListPrice, UnitCost
FROM Products
WHERE UnitCost > ListPrice;
-- EXPECTED: zero rows

-- 3d. Non-price attributes should NOT change between versions
SELECT COUNT(*) AS AttributeChanges
FROM Products p1
JOIN Products p2
  ON p2.ProductID = p1.ProductID
 AND p2.VersionNumber = p1.VersionNumber + 1
WHERE p1.ProductName <> p2.ProductName
   OR p1.SubcategoryKey <> p2.SubcategoryKey
   OR p1.Brand <> p2.Brand;
-- EXPECTED: zero (only prices change between product versions)


-- ============================================================================
-- 4. Key Integrity After SCD2 Expansion
-- ============================================================================

-- 4a. ProductKey is unique across all version rows
SELECT ProductKey, COUNT(*) AS Cnt
FROM Products
GROUP BY ProductKey
HAVING COUNT(*) > 1;
-- EXPECTED: zero rows

-- 4b. ProductProfile links to current version only
SELECT
    pp.ProductKey,
    p.IsCurrent
FROM ProductProfile pp
LEFT JOIN Products p ON p.ProductKey = pp.ProductKey
WHERE p.IsCurrent IS NULL OR p.IsCurrent <> 1;
-- EXPECTED: zero rows

-- 4c. Sales fact should join to products
IF OBJECT_ID('dbo.Sales') IS NOT NULL
BEGIN
    SELECT
        COUNT(*)                                                        AS TotalSales,
        SUM(CASE WHEN p.ProductKey IS NULL THEN 1 ELSE 0 END)          AS OrphanedSales
    FROM Sales f
    LEFT JOIN Products p ON p.ProductKey = f.ProductKey;
END
ELSE IF OBJECT_ID('dbo.OrderDetail') IS NOT NULL
BEGIN
    SELECT
        COUNT(*)                                                        AS TotalSales,
        SUM(CASE WHEN p.ProductKey IS NULL THEN 1 ELSE 0 END)          AS OrphanedSales
    FROM OrderDetail f
    LEFT JOIN Products p ON p.ProductKey = f.ProductKey;
END
-- EXPECTED: zero orphaned sales


-- ############################################################################
-- 5. PRODUCT SCD2 HEALTH SCORECARD
-- ############################################################################
SELECT
    'Product: unique IsCurrent per ID'   AS [Check],
    'Exactly one IsCurrent=1 row per ProductID; FAIL = duplicate or missing current version' AS [Description],
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS Result
FROM (
    SELECT ProductID FROM Products
    GROUP BY ProductID HAVING SUM(CAST(IsCurrent AS INT)) <> 1
) x

UNION ALL

SELECT
    'Product: no date chain gaps',
    'Each version EndDate+1 day = next version StartDate; FAIL = gap or overlap in price revision timeline',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Products p1
JOIN Products p2
  ON p2.ProductID = p1.ProductID AND p2.VersionNumber = p1.VersionNumber + 1
WHERE DATEDIFF(DAY, p1.EffectiveEndDate, p2.EffectiveStartDate) <> 1

UNION ALL

SELECT
    'Product: UnitCost <= ListPrice',
    'Margin must be non-negative in every version; FAIL = cost exceeds selling price',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Products WHERE UnitCost > ListPrice

UNION ALL

SELECT
    'Product: non-price attrs stable',
    'Only ListPrice/UnitCost change between versions; FAIL = name, brand, or subcategory changed',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Products p1
JOIN Products p2
  ON p2.ProductID = p1.ProductID AND p2.VersionNumber = p1.VersionNumber + 1
WHERE p1.ProductName <> p2.ProductName
   OR p1.SubcategoryKey <> p2.SubcategoryKey;
