-- ============================================================================
-- VERIFY SCD TYPE 2 BEHAVIOUR FOR CUSTOMERS
-- Run after loading generated data into SQL Server.
-- Assumes customers.scd2.enabled=true in config.
-- ============================================================================


-- ############################################################################
-- CUSTOMER SCD2 — Life Event Engine
-- ############################################################################

-- ============================================================================
-- 1. Basic SCD2 Structure
-- ============================================================================

-- 1a. Version distribution — how many customers have multiple versions?
SELECT
    VersionCount,
    COUNT(*)                                                    AS CustomerCount,
    CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS PctOfTotal
FROM (
    SELECT CustomerID, COUNT(*) AS VersionCount
    FROM Customers
    GROUP BY CustomerID
) x
GROUP BY VersionCount
ORDER BY VersionCount;
-- EXPECTED: ~85% at version 1 (unchanged), 15% with 2-4 versions
--           (governed by scd2.change_rate, default 0.15)

-- 1b. Only one IsCurrent=1 row per CustomerID
SELECT
    CustomerID,
    SUM(CAST(IsCurrent AS INT)) AS CurrentCount
FROM Customers
GROUP BY CustomerID
HAVING SUM(CAST(IsCurrent AS INT)) <> 1;
-- EXPECTED: zero rows (exactly one current version per customer)

-- 1c. Version numbers are sequential per CustomerID
SELECT c.CustomerID, c.VersionNumber
FROM Customers c
WHERE NOT EXISTS (
    SELECT 1 FROM Customers c2
    WHERE c2.CustomerID = c.CustomerID
      AND c2.VersionNumber = c.VersionNumber - 1
)
AND c.VersionNumber > 1;
-- EXPECTED: zero rows (no gaps in version sequence)


-- ============================================================================
-- 2. Date Chain Integrity
-- ============================================================================

-- 2a. EffectiveStartDate < EffectiveEndDate for all non-current rows
SELECT CustomerID, VersionNumber, EffectiveStartDate, EffectiveEndDate
FROM Customers
WHERE IsCurrent = 0
  AND EffectiveStartDate >= EffectiveEndDate;
-- EXPECTED: zero rows

-- 2b. Current rows should have EffectiveEndDate = '9999-12-31'
SELECT CustomerID, VersionNumber, EffectiveEndDate
FROM Customers
WHERE IsCurrent = 1
  AND EffectiveEndDate <> '9999-12-31';
-- EXPECTED: zero rows

-- 2c. No gaps or overlaps in date chain per CustomerID
--     (previous version EndDate + 1 day = next version StartDate)
SELECT
    c1.CustomerID,
    c1.VersionNumber                                    AS PrevVersion,
    c1.EffectiveEndDate                                 AS PrevEnd,
    c2.VersionNumber                                    AS NextVersion,
    c2.EffectiveStartDate                               AS NextStart,
    DATEDIFF(DAY, c1.EffectiveEndDate, c2.EffectiveStartDate) AS GapDays
FROM Customers c1
JOIN Customers c2
  ON c2.CustomerID = c1.CustomerID
 AND c2.VersionNumber = c1.VersionNumber + 1
WHERE DATEDIFF(DAY, c1.EffectiveEndDate, c2.EffectiveStartDate) <> 1;
-- EXPECTED: zero rows (each next version starts exactly 1 day after prev ends)

-- 2d. First version EffectiveStartDate matches CustomerStartDate
SELECT CustomerID, VersionNumber, EffectiveStartDate, CustomerStartDate
FROM Customers
WHERE VersionNumber = 1
  AND CAST(EffectiveStartDate AS DATE) <> CAST(CustomerStartDate AS DATE);
-- EXPECTED: zero rows


-- ============================================================================
-- 3. Life Event Coherence
-- ============================================================================

-- 3a. What attributes changed between versions? (shows life event variety)
SELECT
    ChangeType,
    COUNT(*)                                                    AS Occurrences,
    CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS PctOfChanges
FROM (
    SELECT
        c2.CustomerID,
        CASE
            WHEN c2.YearlyIncome > c1.YearlyIncome AND c2.MaritalStatus = c1.MaritalStatus
                THEN 'Career Growth'
            WHEN c1.MaritalStatus IN ('Single', 'Divorced') AND c2.MaritalStatus = 'Married'
                THEN 'Marriage'
            WHEN c1.MaritalStatus = 'Married' AND c2.MaritalStatus = 'Divorced'
                THEN 'Divorce'
            WHEN c2.NumberOfChildren > c1.NumberOfChildren
                THEN 'Family Growth'
            WHEN c1.HomeOwnership IN ('Rent', 'Other') AND c2.HomeOwnership = 'Own'
                THEN 'Home Purchase'
            WHEN c2.GeographyKey <> c1.GeographyKey
                THEN 'Relocation'
            WHEN c2.LoyaltyTierKey > c1.LoyaltyTierKey
                THEN 'Tier Upgrade'
            ELSE 'Other / Combined'
        END AS ChangeType
    FROM Customers c1
    JOIN Customers c2
      ON c2.CustomerID = c1.CustomerID
     AND c2.VersionNumber = c1.VersionNumber + 1
) x
GROUP BY ChangeType
ORDER BY Occurrences DESC;
-- EXPECTED: mix of all event types, Career Growth ~30%, Marriage ~20%, etc.

-- 3b. Organizations should NOT have SCD2 versions
SELECT CustomerID, COUNT(*) AS VersionCount
FROM Customers
WHERE CustomerType = 'Organization'
GROUP BY CustomerID
HAVING COUNT(*) > 1;
-- EXPECTED: zero rows (orgs stay at version 1)

-- 3c. Marital status transitions should be valid
--     (Single->Married OK, Married->Divorced OK, but not Single->Divorced directly)
SELECT
    c1.CustomerID,
    c1.MaritalStatus AS FromStatus,
    c2.MaritalStatus AS ToStatus
FROM Customers c1
JOIN Customers c2
  ON c2.CustomerID = c1.CustomerID
 AND c2.VersionNumber = c1.VersionNumber + 1
WHERE c1.MaritalStatus = 'Single' AND c2.MaritalStatus = 'Divorced';
-- EXPECTED: zero rows (can't divorce without being married first)

-- 3d. NumberOfChildren should never decrease
SELECT
    c1.CustomerID,
    c1.VersionNumber,
    c1.NumberOfChildren AS PrevChildren,
    c2.NumberOfChildren AS NextChildren
FROM Customers c1
JOIN Customers c2
  ON c2.CustomerID = c1.CustomerID
 AND c2.VersionNumber = c1.VersionNumber + 1
WHERE c2.NumberOfChildren < c1.NumberOfChildren;
-- EXPECTED: zero rows


-- ============================================================================
-- 4. Key Integrity After SCD2 Expansion
-- ============================================================================

-- 4a. CustomerKey is unique across all version rows
SELECT CustomerKey, COUNT(*) AS Cnt
FROM Customers
GROUP BY CustomerKey
HAVING COUNT(*) > 1;
-- EXPECTED: zero rows

-- 4b. CustomerProfile links to IsCurrent=1 row only
SELECT
    cp.CustomerKey,
    c.IsCurrent
FROM CustomerProfile cp
LEFT JOIN Customers c ON c.CustomerKey = cp.CustomerKey
WHERE c.IsCurrent IS NULL OR c.IsCurrent <> 1;
-- EXPECTED: zero rows

-- 4c. OrganizationProfile links to IsCurrent=1 row only
SELECT
    op.CustomerKey,
    c.IsCurrent
FROM OrganizationProfile op
LEFT JOIN Customers c ON c.CustomerKey = op.CustomerKey
WHERE c.IsCurrent IS NULL OR c.IsCurrent <> 1;
-- EXPECTED: zero rows

-- 4d. Sales fact should join to customers (IsCurrent=1 for point-in-time reporting)
IF OBJECT_ID('dbo.Sales') IS NOT NULL
BEGIN
    SELECT
        COUNT(*)                                                        AS TotalSales,
        SUM(CASE WHEN c.CustomerKey IS NULL THEN 1 ELSE 0 END)         AS OrphanedSales,
        SUM(CASE WHEN c.IsCurrent = 1 THEN 1 ELSE 0 END)              AS CurrentVersionSales
    FROM Sales f
    LEFT JOIN Customers c ON c.CustomerKey = f.CustomerKey;
END
ELSE IF OBJECT_ID('dbo.OrderHeader') IS NOT NULL
BEGIN
    SELECT
        COUNT(*)                                                        AS TotalSales,
        SUM(CASE WHEN c.CustomerKey IS NULL THEN 1 ELSE 0 END)         AS OrphanedSales,
        SUM(CASE WHEN c.IsCurrent = 1 THEN 1 ELSE 0 END)              AS CurrentVersionSales
    FROM OrderHeader f
    LEFT JOIN Customers c ON c.CustomerKey = f.CustomerKey;
END
-- EXPECTED: zero orphaned sales, all join to IsCurrent=1


-- ############################################################################
-- 5. CUSTOMER SCD2 HEALTH SCORECARD
-- ############################################################################
SELECT
    'Customer: unique IsCurrent per ID'   AS [Check],
    'Exactly one IsCurrent=1 row per CustomerID; FAIL = duplicate or missing current version' AS [Description],
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS Result
FROM (
    SELECT CustomerID FROM Customers
    GROUP BY CustomerID HAVING SUM(CAST(IsCurrent AS INT)) <> 1
) x

UNION ALL

SELECT
    'Customer: no date chain gaps',
    'Each version EndDate+1 day = next version StartDate; FAIL = gap or overlap in version timeline',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Customers c1
JOIN Customers c2
  ON c2.CustomerID = c1.CustomerID AND c2.VersionNumber = c1.VersionNumber + 1
WHERE DATEDIFF(DAY, c1.EffectiveEndDate, c2.EffectiveStartDate) <> 1

UNION ALL

SELECT
    'Customer: orgs have no versions',
    'Organization customers stay at version 1; FAIL = org has SCD2 life events (should be individual-only)',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM (
    SELECT CustomerID FROM Customers WHERE CustomerType = 'Organization'
    GROUP BY CustomerID HAVING COUNT(*) > 1
) x

UNION ALL

SELECT
    'Customer: children never decrease',
    'NumberOfChildren only increases across versions (family growth); FAIL = child count went down',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Customers c1
JOIN Customers c2
  ON c2.CustomerID = c1.CustomerID AND c2.VersionNumber = c1.VersionNumber + 1
WHERE c2.NumberOfChildren < c1.NumberOfChildren;
