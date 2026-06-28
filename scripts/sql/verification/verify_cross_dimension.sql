-- ============================================================================
-- VERIFY CROSS-DIMENSION CONSISTENCY
-- Run after loading generated data into SQL Server.
-- Checks that dimensions agree with each other where they share references.
--
-- Supports both sales_output modes:
--   sales        -> single Sales table
--   sales_order  -> OrderHeader + OrderDetail tables
-- ============================================================================

-- Detect which sales table exists
DECLARE @has_sales  BIT = CASE WHEN OBJECT_ID('dbo.Sales') IS NOT NULL THEN 1 ELSE 0 END;
DECLARE @has_header BIT = CASE WHEN OBJECT_ID('dbo.OrderHeader') IS NOT NULL THEN 1 ELSE 0 END;


-- ============================================================================
-- 1. Store EmployeeCount vs Actual Employee Rows
-- ============================================================================

-- 1a. Compare stores.EmployeeCount with actual count from Employees table
SELECT
    s.StoreKey,
    s.EmployeeCount                                                 AS DeclaredCount,
    ISNULL(e.ActualCount, 0)                                        AS ActualCount,
    s.EmployeeCount - ISNULL(e.ActualCount, 0)                      AS Diff
FROM Stores s
LEFT JOIN (
    SELECT StoreKey, COUNT(*) AS ActualCount
    FROM Employees
    WHERE StoreKey IS NOT NULL AND StoreKey > 0
    GROUP BY StoreKey
) e ON e.StoreKey = s.StoreKey
WHERE s.Status = 'Open'
  AND ABS(s.EmployeeCount - ISNULL(e.ActualCount, 0)) > 0;
-- EXPECTED: zero rows (run_employees syncs EmployeeCount back to stores.parquet)

-- 1b. Closed stores should have zero active employees
SELECT
    s.StoreKey,
    s.Status,
    COUNT(e.EmployeeKey) AS ActiveEmployees
FROM Stores s
JOIN Employees e ON e.StoreKey = s.StoreKey AND e.IsActive = 1
WHERE s.Status = 'Closed'
GROUP BY s.StoreKey, s.Status
HAVING COUNT(e.EmployeeKey) > 0;
-- EXPECTED: zero rows


-- ============================================================================
-- 2. Geography Key Consistency
-- ============================================================================

-- 2a. All GeographyKeys used by Customers exist in Geography
SELECT DISTINCT c.GeographyKey
FROM Customers c
LEFT JOIN Geography g ON g.GeographyKey = c.GeographyKey
WHERE g.GeographyKey IS NULL;
-- EXPECTED: zero rows

-- 2b. All GeographyKeys used by Stores exist in Geography
SELECT DISTINCT s.GeographyKey
FROM Stores s
LEFT JOIN Geography g ON g.GeographyKey = s.GeographyKey
WHERE g.GeographyKey IS NULL;
-- EXPECTED: zero rows

-- 2c. All GeographyKeys used by Employees exist in Geography
SELECT DISTINCT e.GeographyKey
FROM Employees e
LEFT JOIN Geography g ON g.GeographyKey = e.GeographyKey
WHERE e.GeographyKey IS NOT NULL AND g.GeographyKey IS NULL;
-- EXPECTED: zero rows


-- ============================================================================
-- 3. Currency & Exchange Rate Consistency
-- ============================================================================

-- 3a. All CurrencyKeys in Sales exist in Currency dimension
IF @has_sales = 1
BEGIN
    SELECT DISTINCT f.CurrencyKey
    FROM Sales f
    LEFT JOIN Currency c ON c.CurrencyKey = f.CurrencyKey
    WHERE c.CurrencyKey IS NULL;
    -- EXPECTED: zero rows
END
ELSE IF @has_header = 1
BEGIN
    SELECT DISTINCT h.CurrencyKey
    FROM OrderHeader h
    LEFT JOIN Currency c ON c.CurrencyKey = h.CurrencyKey
    WHERE c.CurrencyKey IS NULL;
    -- EXPECTED: zero rows
END

-- 3b. All FromCurrencyKey values in ExchangeRates exist in Currency dimension
SELECT DISTINCT er.FromCurrencyKey
FROM ExchangeRates er
LEFT JOIN Currency c ON c.CurrencyKey = er.FromCurrencyKey
WHERE c.CurrencyKey IS NULL;
-- EXPECTED: zero rows

-- 3c. All ToCurrencyKey values in ExchangeRates exist in Currency dimension
SELECT DISTINCT er.ToCurrencyKey
FROM ExchangeRates er
LEFT JOIN Currency c ON c.CurrencyKey = er.ToCurrencyKey
WHERE c.CurrencyKey IS NULL;
-- EXPECTED: zero rows


-- ============================================================================
-- 4. Product Hierarchy Consistency
-- ============================================================================

-- 4a. Every product's SubcategoryKey exists in ProductSubcategory
SELECT DISTINCT p.SubcategoryKey
FROM Products p
LEFT JOIN ProductSubcategory ps ON ps.SubcategoryKey = p.SubcategoryKey
WHERE ps.SubcategoryKey IS NULL;
-- EXPECTED: zero rows

-- 4b. Every subcategory's CategoryKey exists in ProductCategory
SELECT DISTINCT ps.CategoryKey
FROM ProductSubcategory ps
LEFT JOIN ProductCategory pc ON pc.CategoryKey = ps.CategoryKey
WHERE pc.CategoryKey IS NULL;
-- EXPECTED: zero rows

-- 4c. ProductProfile has a row for every IsCurrent=1 product
SELECT p.ProductKey
FROM Products p
LEFT JOIN ProductProfile pp ON pp.ProductKey = p.ProductKey
WHERE pp.ProductKey IS NULL;
-- EXPECTED: zero rows (profile covers all SCD2 versions)

-- 4d. No orphan ProductProfile rows (profile without a product)
SELECT pp.ProductKey
FROM ProductProfile pp
LEFT JOIN Products p ON p.ProductKey = pp.ProductKey
WHERE p.ProductKey IS NULL;
-- EXPECTED: zero rows


-- ============================================================================
-- 5. Customer Profile Consistency
-- ============================================================================

-- 5a. CustomerProfile has a row for every IsCurrent=1 person (orgs have OrganizationProfile)
SELECT c.CustomerKey, c.CustomerID
FROM Customers c
LEFT JOIN CustomerProfile cp ON cp.CustomerKey = c.CustomerKey
WHERE c.IsCurrent = 1 AND c.CustomerType != 'Organization' AND cp.CustomerKey IS NULL;
-- EXPECTED: zero rows

-- 5b. OrganizationProfile only for Organization-type customers
SELECT op.CustomerKey
FROM OrganizationProfile op
JOIN Customers c ON c.CustomerKey = op.CustomerKey
WHERE c.CustomerType <> 'Organization';
-- EXPECTED: zero rows

-- 5c. Every org customer (IsCurrent=1) should have an OrganizationProfile
SELECT c.CustomerKey
FROM Customers c
LEFT JOIN OrganizationProfile op ON op.CustomerKey = c.CustomerKey
WHERE c.IsCurrent = 1
  AND c.CustomerType = 'Organization'
  AND op.CustomerKey IS NULL;
-- EXPECTED: zero rows


-- ============================================================================
-- 6. Employee Store Assignment Consistency
-- ============================================================================

-- 6a. All EmployeeKeys in assignments exist in Employees
SELECT DISTINCT a.EmployeeKey
FROM EmployeeStoreAssignments a
LEFT JOIN Employees e ON e.EmployeeKey = a.EmployeeKey
WHERE e.EmployeeKey IS NULL;
-- EXPECTED: zero rows

-- 6b. All StoreKeys in assignments exist in Stores
SELECT DISTINCT a.StoreKey
FROM EmployeeStoreAssignments a
LEFT JOIN Stores s ON s.StoreKey = a.StoreKey
WHERE s.StoreKey IS NULL;
-- EXPECTED: zero rows

-- 6c. Every active store-level employee should have at least one assignment
SELECT e.EmployeeKey, e.Title, e.StoreKey
FROM Employees e
LEFT JOIN EmployeeStoreAssignments a ON a.EmployeeKey = e.EmployeeKey
WHERE e.IsActive = 1
  AND e.StoreKey > 0
  AND a.EmployeeKey IS NULL;
-- EXPECTED: zero rows


-- ============================================================================
-- 7. Supplier Consistency
-- ============================================================================

-- 7a. All SupplierKeys in ProductProfile exist in Suppliers
SELECT DISTINCT pp.SupplierKey
FROM ProductProfile pp
LEFT JOIN Suppliers s ON s.SupplierKey = pp.SupplierKey
WHERE pp.SupplierKey IS NOT NULL AND s.SupplierKey IS NULL;
-- EXPECTED: zero rows


-- ============================================================================
-- 8. Sales Person Consistency
-- ============================================================================

-- 8a. EmployeeKey in Sales should reference valid employee
IF @has_sales = 1
BEGIN
    SELECT DISTINCT f.EmployeeKey
    FROM Sales f
    LEFT JOIN Employees e ON e.EmployeeKey = f.EmployeeKey
    WHERE f.EmployeeKey IS NOT NULL
      AND e.EmployeeKey IS NULL;
    -- EXPECTED: zero rows
END
ELSE IF @has_header = 1
BEGIN
    SELECT DISTINCT h.EmployeeKey
    FROM OrderHeader h
    LEFT JOIN Employees e ON e.EmployeeKey = h.EmployeeKey
    WHERE h.EmployeeKey IS NOT NULL
      AND e.EmployeeKey IS NULL;
    -- EXPECTED: zero rows
END

-- 8b. Sales person employees should have IsSalesperson=1
IF @has_sales = 1
BEGIN
    SELECT DISTINCT f.EmployeeKey, e.IsSalesperson
    FROM Sales f
    JOIN Employees e ON e.EmployeeKey = f.EmployeeKey
    WHERE e.IsSalesperson = 0;
    -- EXPECTED: zero rows
END
ELSE IF @has_header = 1
BEGIN
    SELECT DISTINCT h.EmployeeKey, e.IsSalesperson
    FROM OrderHeader h
    JOIN Employees e ON e.EmployeeKey = h.EmployeeKey
    WHERE e.IsSalesperson = 0;
    -- EXPECTED: zero rows
END


-- ============================================================================
-- 9. CROSS-DIMENSION CONSISTENCY SCORECARD
-- ============================================================================
SELECT
    'Store: EmployeeCount matches actual' AS [Check],
    'Stores.EmployeeCount must match count of employee rows per store; FAIL = sync step was skipped or failed' AS [Description],
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS Result
FROM Stores s
LEFT JOIN (
    SELECT StoreKey, COUNT(*) AS Cnt FROM Employees
    WHERE StoreKey IS NOT NULL AND StoreKey > 0 GROUP BY StoreKey
) e ON e.StoreKey = s.StoreKey
WHERE s.Status = 'Open'
  AND ABS(s.EmployeeCount - ISNULL(e.Cnt, 0)) > 0

UNION ALL

SELECT 'Geography: all customer geos valid',
    'Every customer GeographyKey must exist in Geography table; FAIL = orphaned geography reference',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Customers c LEFT JOIN Geography g ON g.GeographyKey = c.GeographyKey
WHERE g.GeographyKey IS NULL

UNION ALL

SELECT 'Product hierarchy: subcategory -> category valid',
    'Every subcategory CategoryKey must exist in ProductCategory; FAIL = broken product hierarchy',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM ProductSubcategory ps LEFT JOIN ProductCategory pc ON pc.CategoryKey = ps.CategoryKey
WHERE pc.CategoryKey IS NULL

UNION ALL

SELECT 'CustomerProfile: covers all current persons',
    'Every IsCurrent=1 non-org customer must have a CustomerProfile row; FAIL = missing profile for active person',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Customers c LEFT JOIN CustomerProfile cp ON cp.CustomerKey = c.CustomerKey
WHERE c.IsCurrent = 1 AND c.CustomerType != 'Organization' AND cp.CustomerKey IS NULL

UNION ALL

SELECT 'OrgProfile: only for org customers',
    'OrganizationProfile rows must only link to Organization-type customers; FAIL = individual has org profile',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM OrganizationProfile op JOIN Customers c ON c.CustomerKey = op.CustomerKey
WHERE c.CustomerType <> 'Organization'

UNION ALL

SELECT 'ProductProfile: covers all products',
    'Every product (all SCD2 versions) must have a ProductProfile row; FAIL = missing profile',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Products p LEFT JOIN ProductProfile pp ON pp.ProductKey = p.ProductKey
WHERE pp.ProductKey IS NULL;

-- Currency + SalesPerson FK checks run separately per sales mode
-- because UNION ALL cannot contain IF/ELSE branching.
IF @has_sales = 1
BEGIN
    SELECT 'Currency: all sales currencies valid' AS [Check],
        'Every CurrencyKey used in Sales must exist in Currency dim; FAIL = orphaned currency reference' AS [Description],
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS Result
    FROM (SELECT DISTINCT CurrencyKey FROM Sales) f
    LEFT JOIN Currency c ON c.CurrencyKey = f.CurrencyKey
    WHERE c.CurrencyKey IS NULL;

    SELECT 'SalesPerson: valid employee references' AS [Check],
        'Every EmployeeKey in Sales must exist in Employees; FAIL = sale assigned to non-existent employee' AS [Description],
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS Result
    FROM (SELECT DISTINCT EmployeeKey FROM Sales WHERE EmployeeKey IS NOT NULL) f
    LEFT JOIN Employees e ON e.EmployeeKey = f.EmployeeKey
    WHERE e.EmployeeKey IS NULL;
END
ELSE IF @has_header = 1
BEGIN
    SELECT 'Currency: all sales currencies valid' AS [Check],
        'Every CurrencyKey used in Sales must exist in Currency dim; FAIL = orphaned currency reference' AS [Description],
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS Result
    FROM (SELECT DISTINCT CurrencyKey FROM OrderHeader) f
    LEFT JOIN Currency c ON c.CurrencyKey = f.CurrencyKey
    WHERE c.CurrencyKey IS NULL;

    SELECT 'SalesPerson: valid employee references' AS [Check],
        'Every EmployeeKey in OrderHeader must exist in Employees; FAIL = sale assigned to non-existent employee' AS [Description],
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS Result
    FROM (SELECT DISTINCT EmployeeKey FROM OrderHeader WHERE EmployeeKey IS NOT NULL) f
    LEFT JOIN Employees e ON e.EmployeeKey = f.EmployeeKey
    WHERE e.EmployeeKey IS NULL;
END
