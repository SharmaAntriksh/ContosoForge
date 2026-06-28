-- ============================================================================
-- VERIFY BUDGET, INVENTORY, WISHLIST, COMPLAINT & SUBSCRIPTION QUALITY
-- Run after loading generated data into SQL Server.
-- ============================================================================


-- ############################################################################
-- BUDGET
-- ############################################################################

-- ============================================================================
-- 1. Budget Scenarios
-- ============================================================================

-- 1a. Exactly 3 scenarios should exist
SELECT DISTINCT Scenario FROM BudgetYearly ORDER BY Scenario;
-- EXPECTED: High, Low, Medium

-- 1b. Growth rates should reflect scenario direction
SELECT
    Scenario,
    AVG(BudgetGrowthPct)                    AS AvgGrowthPct,
    MIN(BudgetGrowthPct)                    AS MinGrowthPct,
    MAX(BudgetGrowthPct)                    AS MaxGrowthPct
FROM BudgetYearly
GROUP BY Scenario
ORDER BY AvgGrowthPct;
-- EXPECTED: Low avg < 0, Medium avg ~0, High avg > 0

-- 1c. Budget amounts should be positive
SELECT Scenario, COUNT(*) AS NegativeBudgets
FROM BudgetYearly
WHERE BudgetAmount <= 0
GROUP BY Scenario;
-- EXPECTED: zero rows

-- 1d. Monthly budget should sum close to yearly
SELECT
    y.Country,
    y.Category,
    y.BudgetYear,
    y.Scenario,
    y.BudgetAmount                                             AS YearlyAmount,
    m.MonthlyTotal,
    ABS(y.BudgetAmount - m.MonthlyTotal)                       AS Diff,
    CAST(ABS(y.BudgetAmount - m.MonthlyTotal) * 100.0
         / NULLIF(y.BudgetAmount, 0) AS DECIMAL(5,2))         AS DiffPct
FROM BudgetYearly y
JOIN (
    SELECT Country, Category, BudgetYear, Scenario,
           SUM(BudgetAmount) AS MonthlyTotal
    FROM BudgetMonthly
    GROUP BY Country, Category, BudgetYear, Scenario
) m ON m.Country = y.Country
   AND m.Category = y.Category
   AND m.BudgetYear = y.BudgetYear
   AND m.Scenario = y.Scenario
WHERE ABS(y.BudgetAmount - m.MonthlyTotal) / NULLIF(y.BudgetAmount, 0) > 0.02;
-- EXPECTED: zero or very few rows (monthly should sum to yearly within 2%)


-- ############################################################################
-- INVENTORY
-- ############################################################################

-- ============================================================================
-- 2. Inventory Snapshot Quality
-- ============================================================================

-- 2a. ABC classification distribution in inventory
SELECT
    pp.ABCClassification,
    COUNT(DISTINCT i.ProductKey)                                    AS Products,
    COUNT(*)                                                        AS SnapshotRows,
    CAST(COUNT(DISTINCT i.ProductKey) * 100.0
         / NULLIF((SELECT COUNT(DISTINCT ProductKey) FROM InventorySnapshot), 0)
         AS DECIMAL(5,1))                                           AS PctOfProducts
FROM InventorySnapshot i
JOIN Products p ON p.ProductKey = i.ProductKey
JOIN ProductProfile pp ON pp.ProductKey = p.ProductKey
GROUP BY pp.ABCClassification
ORDER BY pp.ABCClassification;
-- EXPECTED: A/B/C all represented (unless filtered by abc_filter config)

-- 2b. QuantityOnHand should be non-negative
SELECT COUNT(*) AS NegativeQOH
FROM InventorySnapshot
WHERE QuantityOnHand < 0;
-- EXPECTED: zero

-- 2c. Stockout flag vs DaysOutOfStock consistency
--     StockoutFlag means "experienced a stockout during this period" —
--     QOH may be >0 at snapshot time if restocked, so don't compare to QOH.
--     But DaysOutOfStock > 0 must imply StockoutFlag = 1.
SELECT
    COUNT(*)                                                        AS TotalRows,
    SUM(CASE WHEN DaysOutOfStock > 0 AND StockoutFlag = 0 THEN 1 ELSE 0 END) AS DaysWithoutFlag,
    SUM(CASE WHEN DaysOutOfStock = 0 AND StockoutFlag = 1 THEN 1 ELSE 0 END) AS FlagWithoutDays
FROM InventorySnapshot;
-- EXPECTED: DaysWithoutFlag = 0 (days out of stock implies flag was set)
--           FlagWithoutDays may be > 0 (brief stockout rounded to 0 days)

-- 2d. ReorderFlag should correlate with low stock
SELECT
    ReorderFlag,
    AVG(CAST(QuantityOnHand AS FLOAT))                              AS AvgQOH,
    COUNT(*)                                                        AS Cnt
FROM InventorySnapshot
GROUP BY ReorderFlag;
-- EXPECTED: ReorderFlag=1 rows should have lower avg QOH than ReorderFlag=0

-- 2e. DaysOutOfStock should be > 0 only when StockoutFlag = 1
SELECT
    COUNT(*)                                                        AS TotalRows,
    SUM(CASE WHEN StockoutFlag = 0 AND DaysOutOfStock > 0 THEN 1 ELSE 0 END) AS DaysWithoutStockout,
    SUM(CASE WHEN StockoutFlag = 1 AND DaysOutOfStock = 0 THEN 1 ELSE 0 END) AS StockoutWithZeroDays
FROM InventorySnapshot;
-- EXPECTED: both counts = 0 (DaysOutOfStock consistent with StockoutFlag)


-- ############################################################################
-- WISHLISTS
-- ############################################################################

-- ============================================================================
-- 3. Wishlist Quality
-- ============================================================================

-- 3a. Participation rate check
SELECT
    (SELECT COUNT(DISTINCT CustomerKey) FROM Customers WHERE IsCurrent = 1) AS TotalCustomers,
    COUNT(DISTINCT CustomerKey)                                     AS WishlistCustomers,
    CAST(COUNT(DISTINCT CustomerKey) * 100.0
         / NULLIF((SELECT COUNT(DISTINCT CustomerKey) FROM Customers WHERE IsCurrent = 1), 0)
         AS DECIMAL(5,1))                                           AS ParticipationPct
FROM CustomerWishlists;
-- EXPECTED: ~35% (governed by wishlists.participation_rate)

-- 3b. Average items per customer
SELECT
    AVG(ItemCount * 1.0)                                            AS AvgItems,
    MIN(ItemCount)                                                  AS MinItems,
    MAX(ItemCount)                                                  AS MaxItems
FROM (
    SELECT CustomerKey, COUNT(*) AS ItemCount
    FROM CustomerWishlists
    GROUP BY CustomerKey
) x;
-- EXPECTED: AvgItems ~3.5 (governed by wishlists.avg_items)

-- 3c. All wishlist products should be valid
SELECT w.ProductKey
FROM CustomerWishlists w
LEFT JOIN Products p ON p.ProductKey = w.ProductKey
WHERE p.ProductKey IS NULL;
-- EXPECTED: zero rows

-- 3d. Priority distribution
SELECT
    Priority,
    COUNT(*)                                                        AS Cnt,
    CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS Pct
FROM CustomerWishlists
GROUP BY Priority
ORDER BY Priority;
-- EXPECTED: varied distribution (1=highest priority)


-- ############################################################################
-- COMPLAINTS
-- ############################################################################

-- ============================================================================
-- 4. Complaint Quality
-- ============================================================================

-- 4a. Complaint rate check
SELECT
    (SELECT COUNT(DISTINCT CustomerKey) FROM Customers WHERE IsCurrent = 1) AS TotalCustomers,
    COUNT(DISTINCT CustomerKey)                                     AS ComplainantCustomers,
    CAST(COUNT(DISTINCT CustomerKey) * 100.0
         / NULLIF((SELECT COUNT(DISTINCT CustomerKey) FROM Customers WHERE IsCurrent = 1), 0)
         AS DECIMAL(5,1))                                           AS ComplaintRatePct
FROM Complaints;
-- EXPECTED: ~3% (governed by complaints.complaint_rate)

-- 4b. Severity distribution
SELECT
    Severity,
    COUNT(*)                                                        AS Cnt,
    CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS Pct
FROM Complaints
GROUP BY Severity
ORDER BY Cnt DESC;
-- EXPECTED: mix of Low/Medium/High/Critical

-- 4c. Resolution rate
SELECT
    COUNT(*)                                                        AS TotalComplaints,
    SUM(CASE WHEN Status = 'Resolved' THEN 1 ELSE 0 END)           AS Resolved,
    CAST(SUM(CASE WHEN Status = 'Resolved' THEN 1 ELSE 0 END) * 100.0
         / NULLIF(COUNT(*), 0) AS DECIMAL(5,1))                    AS ResolutionPct
FROM Complaints;
-- EXPECTED: ~85% resolved (governed by complaints.resolution_rate)

-- 4d. ResolutionDate should be >= ComplaintDate (when resolved)
SELECT ComplaintKey, ComplaintDate, ResolutionDate
FROM Complaints
WHERE ResolutionDate IS NOT NULL
  AND ResolutionDate < ComplaintDate;
-- EXPECTED: zero rows

-- 4e. Complaint type distribution
SELECT
    ComplaintType,
    COUNT(*)                                                        AS Cnt,
    CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS Pct
FROM Complaints
GROUP BY ComplaintType
ORDER BY Cnt DESC;
-- EXPECTED: varied across types

-- 4f. Repeat complainers (should be ~15% per config)
SELECT
    COUNT(*)                                                        AS TotalComplainants,
    SUM(CASE WHEN ComplaintCount > 1 THEN 1 ELSE 0 END)            AS RepeatComplainants,
    CAST(SUM(CASE WHEN ComplaintCount > 1 THEN 1 ELSE 0 END) * 100.0
         / NULLIF(COUNT(*), 0) AS DECIMAL(5,1))                    AS RepeatPct
FROM (
    SELECT CustomerKey, COUNT(*) AS ComplaintCount
    FROM Complaints
    GROUP BY CustomerKey
) x;
-- EXPECTED: ~15% (governed by complaints.repeat_complaint_rate)


-- ############################################################################
-- SUBSCRIPTIONS (billing-period fact)
-- ############################################################################

-- ============================================================================
-- 5. Subscription Quality
-- ============================================================================

-- 5a. Participation rate
SELECT
    (SELECT COUNT(DISTINCT CustomerKey) FROM Customers WHERE IsCurrent = 1) AS TotalCustomers,
    COUNT(DISTINCT CustomerKey)                                     AS SubscribedCustomers,
    CAST(COUNT(DISTINCT CustomerKey) * 100.0
         / NULLIF((SELECT COUNT(DISTINCT CustomerKey) FROM Customers WHERE IsCurrent = 1), 0)
         AS DECIMAL(5,1))                                           AS ParticipationPct
FROM CustomerSubscriptions;
-- EXPECTED: ~65% (governed by subscriptions.participation_rate)

-- 5b. Average subscriptions per customer
SELECT
    AVG(SubCount * 1.0)                                             AS AvgSubs,
    MIN(SubCount)                                                   AS MinSubs,
    MAX(SubCount)                                                   AS MaxSubs
FROM (
    SELECT CustomerKey, COUNT(DISTINCT SubscriptionKey) AS SubCount
    FROM CustomerSubscriptions
    GROUP BY CustomerKey
) x;
-- EXPECTED: ~1.5 (governed by subscriptions.avg_subscriptions_per_customer)

-- 5c. All PlanKeys should be valid
SELECT s.PlanKey
FROM CustomerSubscriptions s
LEFT JOIN Plans p ON p.PlanKey = s.PlanKey
WHERE p.PlanKey IS NULL;
-- EXPECTED: zero rows

-- 5d. Every subscription should have exactly one IsFirstPeriod = 1
SELECT SubscriptionKey, SUM(CAST(IsFirstPeriod AS INT)) AS FirstPeriodCount
FROM CustomerSubscriptions
GROUP BY SubscriptionKey
HAVING SUM(CAST(IsFirstPeriod AS INT)) <> 1;
-- EXPECTED: zero rows

-- 5e. Trial periods are prorated: 0 <= PeriodPrice <= plan CyclePrice
--     (charged only for the cycle portion after the free trial_days window)
SELECT COUNT(*) AS TrialPriceOutOfRange
FROM CustomerSubscriptions s
JOIN Plans p ON p.PlanKey = s.PlanKey
WHERE s.IsTrialPeriod = 1
  AND (s.PeriodPrice < 0 OR s.PeriodPrice > p.CyclePrice + 0.01);
-- EXPECTED: zero

-- 5f. Churn rate (subscriptions with a churn period / total subscriptions)
SELECT
    COUNT(DISTINCT SubscriptionKey)                                 AS TotalSubs,
    COUNT(DISTINCT CASE WHEN IsChurnPeriod = 1 THEN SubscriptionKey END) AS ChurnedSubs,
    CAST(COUNT(DISTINCT CASE WHEN IsChurnPeriod = 1 THEN SubscriptionKey END) * 100.0
         / NULLIF(COUNT(DISTINCT SubscriptionKey), 0) AS DECIMAL(5,1)) AS ChurnPct
FROM CustomerSubscriptions;
-- EXPECTED: ~25% (governed by subscriptions.churn_rate)

-- 5g. PeriodEndDate >= PeriodStartDate
SELECT SubscriptionKey, PeriodStartDate, PeriodEndDate
FROM CustomerSubscriptions
WHERE PeriodEndDate < PeriodStartDate;
-- EXPECTED: zero rows

-- 5h. BillingCycleNumber contiguity (max should equal count per subscription)
SELECT SubscriptionKey, COUNT(*) AS Periods, MAX(BillingCycleNumber) AS MaxCycle
FROM CustomerSubscriptions
GROUP BY SubscriptionKey
HAVING MAX(BillingCycleNumber) <> COUNT(*);
-- EXPECTED: zero rows

-- 5i. PeriodPrice should match plan CyclePrice for non-trial periods
SELECT COUNT(*) AS PriceMismatches
FROM CustomerSubscriptions s
JOIN Plans p ON p.PlanKey = s.PlanKey
WHERE s.IsTrialPeriod = 0
  AND ABS(s.PeriodPrice - p.CyclePrice) > 0.01;
-- EXPECTED: zero


-- ############################################################################
-- 6. SECONDARY FACTS SCORECARD
-- ############################################################################
SELECT
    'Budget: 3 scenarios exist' AS [Check],
    'Must have exactly Low/Medium/High scenarios; FAIL = missing budget scenario' AS [Description],
    CASE WHEN COUNT(DISTINCT Scenario) = 3 THEN 'PASS' ELSE 'FAIL' END AS Result
FROM BudgetYearly

UNION ALL

SELECT 'Budget: no negative amounts',
    'BudgetAmount must be positive; FAIL = budget engine produced invalid negative target',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM BudgetYearly WHERE BudgetAmount <= 0

UNION ALL

SELECT 'Inventory: no negative QOH',
    'QuantityOnHand must be >= 0; FAIL = inventory engine underflowed stock count',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM InventorySnapshot WHERE QuantityOnHand < 0

UNION ALL

SELECT 'Inventory: DaysOutOfStock implies StockoutFlag',
    'If DaysOutOfStock > 0 then StockoutFlag must be 1; FAIL = days recorded without flag set',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM InventorySnapshot
WHERE DaysOutOfStock > 0 AND StockoutFlag = 0

UNION ALL

SELECT 'Wishlists: valid product refs',
    'Every wishlist ProductKey must exist in Products; FAIL = wishlist references non-existent product',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM CustomerWishlists w LEFT JOIN Products p ON p.ProductKey = w.ProductKey
WHERE p.ProductKey IS NULL

UNION ALL

SELECT 'Complaints: ResolutionDate >= ComplaintDate',
    'Complaint cannot be resolved before it was filed; FAIL = date inversion',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Complaints WHERE ResolutionDate IS NOT NULL AND ResolutionDate < ComplaintDate

UNION ALL

SELECT 'Subscriptions: valid PlanKey refs',
    'Every subscription PlanKey must exist in Plans; FAIL = subscription references non-existent plan',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM CustomerSubscriptions s LEFT JOIN Plans p ON p.PlanKey = s.PlanKey
WHERE p.PlanKey IS NULL

UNION ALL

SELECT 'Subscriptions: PeriodEndDate >= PeriodStartDate',
    'Billing period end cannot precede period start; FAIL = date inversion',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM CustomerSubscriptions WHERE PeriodEndDate < PeriodStartDate

UNION ALL

SELECT 'Subscriptions: trial periods are discounted',
    'IsTrialPeriod=1 rows must have 0 <= PeriodPrice <= plan CyclePrice; FAIL = out of range',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM CustomerSubscriptions s
JOIN Plans p ON p.PlanKey = s.PlanKey
WHERE s.IsTrialPeriod = 1 AND (s.PeriodPrice < 0 OR s.PeriodPrice > p.CyclePrice + 0.01);
