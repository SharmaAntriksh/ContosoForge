-- ============================================================================
-- VERIFY TEMPORAL COVERAGE
-- Run after loading generated data into SQL Server.
-- Checks that every month has data, no gaps, dates within configured range.
--
-- Supports both sales_output modes:
--   sales        -> single Sales table
--   sales_order  -> OrderHeader + OrderDetail tables
-- ============================================================================

-- Detect which sales table exists
DECLARE @has_sales  BIT = CASE WHEN OBJECT_ID('dbo.Sales') IS NOT NULL THEN 1 ELSE 0 END;
DECLARE @has_header BIT = CASE WHEN OBJECT_ID('dbo.OrderHeader') IS NOT NULL THEN 1 ELSE 0 END;


-- ============================================================================
-- 1. Sales Coverage — Every Month Has Transactions
-- ============================================================================

-- 1a. Monthly sales count — no month should be zero
IF @has_sales = 1
BEGIN
    SELECT
        d.[Year],
        d.[Month],
        COUNT(f.OrderNumber)                                       AS SalesLines
    FROM Dates d
    LEFT JOIN Sales f ON f.TimeKey = d.DateKey
    WHERE d.Date BETWEEN (SELECT MIN(OrderDate) FROM Sales)
                     AND (SELECT MAX(OrderDate) FROM Sales)
    GROUP BY d.[Year], d.[Month]
    ORDER BY d.[Year], d.[Month];
    -- EXPECTED: every month has sales > 0; no gaps
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        d.[Year],
        d.[Month],
        COUNT(h.OrderNumber)                                       AS SalesLines
    FROM Dates d
    LEFT JOIN OrderHeader h ON h.TimeKey = d.DateKey
    WHERE d.Date BETWEEN (SELECT MIN(OrderDate) FROM OrderHeader)
                     AND (SELECT MAX(OrderDate) FROM OrderHeader)
    GROUP BY d.[Year], d.[Month]
    ORDER BY d.[Year], d.[Month];
    -- EXPECTED: every month has sales > 0; no gaps
END

-- 1b. Identify any gap months (zero sales)
IF @has_sales = 1
BEGIN
    SELECT
        d.[Year],
        d.[Month]
    FROM Dates d
    WHERE d.Date BETWEEN (SELECT MIN(OrderDate) FROM Sales)
                     AND (SELECT MAX(OrderDate) FROM Sales)
      AND d.[Day] = 1
      AND NOT EXISTS (
          SELECT 1 FROM Sales f
          WHERE YEAR(f.OrderDate) = d.[Year]
            AND MONTH(f.OrderDate) = d.[Month]
      );
    -- EXPECTED: zero rows (no month without sales)
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        d.[Year],
        d.[Month]
    FROM Dates d
    WHERE d.Date BETWEEN (SELECT MIN(OrderDate) FROM OrderHeader)
                     AND (SELECT MAX(OrderDate) FROM OrderHeader)
      AND d.[Day] = 1
      AND NOT EXISTS (
          SELECT 1 FROM OrderHeader h
          WHERE YEAR(h.OrderDate) = d.[Year]
            AND MONTH(h.OrderDate) = d.[Month]
      );
    -- EXPECTED: zero rows (no month without sales)
END


-- ============================================================================
-- 2. Date Range Boundaries
-- ============================================================================

-- 2a. Sales dates should fall within configured range
IF @has_sales = 1
BEGIN
    SELECT
        MIN(OrderDate) AS EarliestSale,
        MAX(OrderDate) AS LatestSale,
        MIN(DueDate)   AS EarliestDue,
        MAX(DueDate)   AS LatestDue
    FROM Sales;
    -- EXPECTED: EarliestSale >= config start date, LatestSale <= config end date
    --           DueDate may extend slightly past end date (fulfillment lag)
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        MIN(h.OrderDate) AS EarliestSale,
        MAX(h.OrderDate) AS LatestSale,
        MIN(d.DueDate)   AS EarliestDue,
        MAX(d.DueDate)   AS LatestDue
    FROM OrderHeader h
    JOIN OrderDetail d ON d.OrderNumber = h.OrderNumber;
    -- EXPECTED: EarliestSale >= config start date, LatestSale <= config end date
    --           DueDate may extend slightly past end date (fulfillment lag)
END

-- 2b. Customer start dates within range
SELECT
    MIN(CustomerStartDate) AS EarliestCustomer,
    MAX(CustomerStartDate) AS LatestCustomer,
    MIN(CustomerEndDate)   AS EarliestChurn,
    MAX(CustomerEndDate)   AS LatestChurn
FROM Customers;
-- EXPECTED: within configured defaults.dates range

-- 2c. Employee hire dates — should precede or fall within sales window
SELECT
    MIN(HireDate)        AS EarliestHire,
    MAX(HireDate)        AS LatestHire,
    MIN(TerminationDate) AS EarliestTermination,
    MAX(TerminationDate) AS LatestTermination
FROM Employees;
-- EXPECTED: EarliestHire may precede sales start (pre-existing staff)

-- 2d. Promotion dates within range
SELECT
    MIN(StartDate) AS EarliestPromo,
    MAX(EndDate)   AS LatestPromo
FROM Promotions
WHERE PromotionKey > 1;
-- EXPECTED: within configured date range


-- ============================================================================
-- 3. Exchange Rates Coverage
-- ============================================================================

-- 3a. FX rates should cover every day in the sales date range
SELECT
    COUNT(DISTINCT Date) AS FXDaysCovered,
    MIN(Date)            AS FXStart,
    MAX(Date)            AS FXEnd
FROM ExchangeRates;
-- EXPECTED: FXStart <= sales start, FXEnd >= sales end

-- 3b. Any sales dates without FX rates?
IF @has_sales = 1
BEGIN
    SELECT DISTINCT f.OrderDate
    FROM Sales f
    LEFT JOIN ExchangeRates er ON er.Date = f.OrderDate
    WHERE er.Date IS NULL;
    -- EXPECTED: zero rows (every sales day has FX coverage)
END
ELSE IF @has_header = 1
BEGIN
    SELECT DISTINCT h.OrderDate
    FROM OrderHeader h
    LEFT JOIN ExchangeRates er ON er.Date = h.OrderDate
    WHERE er.Date IS NULL;
    -- EXPECTED: zero rows (every sales day has FX coverage)
END

-- 3c. All configured currencies should have rates
SELECT DISTINCT ToCurrency FROM ExchangeRates ORDER BY ToCurrency;
-- EXPECTED: CAD, GBP, EUR, INR, AUD, CNY, JPY (+ any configured extras)


-- ============================================================================
-- 4. Date Dimension Coverage
-- ============================================================================

-- 4a. Date table should cover full sales range with no gaps
SELECT
    MIN(Date) AS DateStart,
    MAX(Date) AS DateEnd,
    COUNT(*)  AS TotalDays,
    DATEDIFF(DAY, MIN(Date), MAX(Date)) + 1 AS ExpectedDays
FROM Dates;
-- EXPECTED: TotalDays = ExpectedDays (no gaps)

-- 4b. Every sales OrderDate should have a matching TimeKey
IF @has_sales = 1
BEGIN
    SELECT COUNT(*) AS OrphanedDates
    FROM Sales f
    LEFT JOIN Dates d ON d.DateKey = f.TimeKey
    WHERE d.DateKey IS NULL;
    -- EXPECTED: zero
END
ELSE IF @has_header = 1
BEGIN
    SELECT COUNT(*) AS OrphanedDates
    FROM OrderHeader h
    LEFT JOIN Dates d ON d.DateKey = h.TimeKey
    WHERE d.DateKey IS NULL;
    -- EXPECTED: zero
END


-- ============================================================================
-- 5. Budget Temporal Coverage
-- ============================================================================

-- 5a. Budget years should cover the full date range
SELECT DISTINCT BudgetYear, Scenario
FROM BudgetYearly
ORDER BY BudgetYear, Scenario;
-- EXPECTED: every year in config range, each with Low/Medium/High

-- 5b. Monthly budget should cover every month in every year
SELECT
    BudgetYear,
    Scenario,
    COUNT(DISTINCT BudgetMonthStart)    AS MonthsCovered
FROM BudgetMonthly
GROUP BY BudgetYear, Scenario
ORDER BY BudgetYear, Scenario;
-- EXPECTED: 12 months per year per scenario


-- ============================================================================
-- 6. Inventory Temporal Coverage
-- ============================================================================

-- 6a. Inventory snapshots per period
SELECT
    YEAR(SnapshotDate)                                              AS SnapYear,
    MONTH(SnapshotDate)                                             AS SnapMonth,
    COUNT(*)                                                        AS SnapshotRows,
    COUNT(DISTINCT ProductKey)                                      AS UniqueProducts,
    COUNT(DISTINCT WarehouseKey)                                     AS UniqueWarehouses
FROM InventorySnapshot
GROUP BY YEAR(SnapshotDate), MONTH(SnapshotDate)
ORDER BY SnapYear, SnapMonth;
-- EXPECTED: consistent row counts per period; no missing months
--           (if grain=monthly, every month present)

-- 6b. Any months with zero inventory snapshots?
SELECT
    d.[Year],
    d.[Month]
FROM Dates d
WHERE d.[Day] = 1
  AND d.Date BETWEEN (SELECT MIN(SnapshotDate) FROM InventorySnapshot)
                 AND (SELECT MAX(SnapshotDate) FROM InventorySnapshot)
  AND NOT EXISTS (
      SELECT 1 FROM InventorySnapshot i
      WHERE YEAR(i.SnapshotDate) = d.[Year]
        AND MONTH(i.SnapshotDate) = d.[Month]
  );
-- EXPECTED: zero rows (no gaps in inventory coverage)


-- ============================================================================
-- 7. Subscription & Complaint Temporal Coverage
-- ============================================================================

-- 7a. Subscription billing periods span the full range
SELECT
    MIN(PeriodStartDate) AS EarliestPeriod,
    MAX(PeriodStartDate) AS LatestPeriod,
    MIN(PeriodEndDate)   AS EarliestEnd,
    MAX(PeriodEndDate)   AS LatestEnd
FROM CustomerSubscriptions;
-- EXPECTED: spans configured date range

-- 7b. Complaint dates span the range
SELECT
    MIN(ComplaintDate)   AS EarliestComplaint,
    MAX(ComplaintDate)   AS LatestComplaint,
    MIN(ResolutionDate)  AS EarliestResolution,
    MAX(ResolutionDate)  AS LatestResolution
FROM Complaints;
-- EXPECTED: within configured date range


-- ============================================================================
-- 8. TEMPORAL COVERAGE SCORECARD
-- ============================================================================
IF @has_sales = 1
BEGIN
    SELECT
        'Sales: no gap months' AS [Check],
        'Every month in the sales date range must have transactions; FAIL = missing month with zero sales' AS [Description],
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS Result
    FROM (
        SELECT d.[Year], d.[Month]
        FROM Dates d
        WHERE d.[Day] = 1
          AND d.Date BETWEEN (SELECT MIN(OrderDate) FROM Sales)
                         AND (SELECT MAX(OrderDate) FROM Sales)
          AND NOT EXISTS (
              SELECT 1 FROM Sales f
              WHERE YEAR(f.OrderDate) = d.[Year]
                AND MONTH(f.OrderDate) = d.[Month]
          )
    ) x

    UNION ALL

    SELECT 'Date dim: no gaps',
        'Date table must have one row per calendar day with no missing days; FAIL = holes in calendar dimension',
        CASE WHEN COUNT(*) = DATEDIFF(DAY, MIN(Date), MAX(Date)) + 1
             THEN 'PASS' ELSE 'FAIL' END
    FROM Dates

    UNION ALL

    SELECT 'FX: covers all sales dates',
        'ExchangeRates must have rates for every day with sales; FAIL = sales date has no FX rate available',
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
    FROM (
        SELECT DISTINCT f.OrderDate FROM Sales f
        LEFT JOIN ExchangeRates er ON er.Date = f.OrderDate
        WHERE er.Date IS NULL
    ) x

    UNION ALL

    SELECT 'Budget: 3 scenarios per year',
        'Each budget year must have Low/Medium/High scenarios; FAIL = missing scenario for a year',
        CASE WHEN MIN(ScenarioCount) = 3 AND MAX(ScenarioCount) = 3
             THEN 'PASS' ELSE 'FAIL' END
    FROM (
        SELECT BudgetYear, COUNT(DISTINCT Scenario) AS ScenarioCount
        FROM BudgetYearly GROUP BY BudgetYear
    ) x

    UNION ALL

    SELECT 'Inventory: no gap months',
        'Inventory snapshots must exist for every month in the range; FAIL = missing monthly snapshot',
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
    FROM (
        SELECT d.[Year], d.[Month]
        FROM Dates d
        WHERE d.[Day] = 1
          AND d.Date BETWEEN (SELECT MIN(SnapshotDate) FROM InventorySnapshot)
                         AND (SELECT MAX(SnapshotDate) FROM InventorySnapshot)
          AND NOT EXISTS (
              SELECT 1 FROM InventorySnapshot i
              WHERE YEAR(i.SnapshotDate) = d.[Year]
                AND MONTH(i.SnapshotDate) = d.[Month]
          )
    ) x;
END
ELSE IF @has_header = 1
BEGIN
    SELECT
        'Sales: no gap months' AS [Check],
        'Every month in the sales date range must have transactions; FAIL = missing month with zero sales' AS [Description],
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS Result
    FROM (
        SELECT d.[Year], d.[Month]
        FROM Dates d
        WHERE d.[Day] = 1
          AND d.Date BETWEEN (SELECT MIN(OrderDate) FROM OrderHeader)
                         AND (SELECT MAX(OrderDate) FROM OrderHeader)
          AND NOT EXISTS (
              SELECT 1 FROM OrderHeader h
              WHERE YEAR(h.OrderDate) = d.[Year]
                AND MONTH(h.OrderDate) = d.[Month]
          )
    ) x

    UNION ALL

    SELECT 'Date dim: no gaps',
        'Date table must have one row per calendar day with no missing days; FAIL = holes in calendar dimension',
        CASE WHEN COUNT(*) = DATEDIFF(DAY, MIN(Date), MAX(Date)) + 1
             THEN 'PASS' ELSE 'FAIL' END
    FROM Dates

    UNION ALL

    SELECT 'FX: covers all sales dates',
        'ExchangeRates must have rates for every day with sales; FAIL = sales date has no FX rate available',
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
    FROM (
        SELECT DISTINCT h.OrderDate FROM OrderHeader h
        LEFT JOIN ExchangeRates er ON er.Date = h.OrderDate
        WHERE er.Date IS NULL
    ) x

    UNION ALL

    SELECT 'Budget: 3 scenarios per year',
        'Each budget year must have Low/Medium/High scenarios; FAIL = missing scenario for a year',
        CASE WHEN MIN(ScenarioCount) = 3 AND MAX(ScenarioCount) = 3
             THEN 'PASS' ELSE 'FAIL' END
    FROM (
        SELECT BudgetYear, COUNT(DISTINCT Scenario) AS ScenarioCount
        FROM BudgetYearly GROUP BY BudgetYear
    ) x

    UNION ALL

    SELECT 'Inventory: no gap months',
        'Inventory snapshots must exist for every month in the range; FAIL = missing monthly snapshot',
        CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
    FROM (
        SELECT d.[Year], d.[Month]
        FROM Dates d
        WHERE d.[Day] = 1
          AND d.Date BETWEEN (SELECT MIN(SnapshotDate) FROM InventorySnapshot)
                         AND (SELECT MAX(SnapshotDate) FROM InventorySnapshot)
          AND NOT EXISTS (
              SELECT 1 FROM InventorySnapshot i
              WHERE YEAR(i.SnapshotDate) = d.[Year]
                AND MONTH(i.SnapshotDate) = d.[Month]
          )
    ) x;
END
