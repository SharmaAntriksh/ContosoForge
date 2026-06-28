-- ============================================================================
-- VERIFY DIMENSION DATA QUALITY
-- Run after loading generated data into SQL Server.
-- Checks uniqueness, domain values, date sanity, and NULL hygiene.
-- ============================================================================


-- ============================================================================
-- 1. Business Key Uniqueness
-- ============================================================================

-- 1a. CustomerID should be unique (CustomerKey is unique by PK, but
--     CustomerID is the durable business key — multiple rows per ID with SCD2)
--     Verify: no duplicate CustomerID where IsCurrent=1
SELECT CustomerID, COUNT(*) AS Cnt
FROM Customers
WHERE IsCurrent = 1
GROUP BY CustomerID
HAVING COUNT(*) > 1;
-- EXPECTED: zero rows

-- 1b. ProductID uniqueness among current versions
SELECT ProductID, COUNT(*) AS Cnt
FROM Products
WHERE IsCurrent = 1
GROUP BY ProductID
HAVING COUNT(*) > 1;
-- EXPECTED: zero rows

-- 1c. StoreNumber should be unique
SELECT StoreNumber, COUNT(*) AS Cnt
FROM Stores
GROUP BY StoreNumber
HAVING COUNT(*) > 1;
-- EXPECTED: zero rows

-- 1d. EmployeeKey uniqueness (already PK, but sanity check on data)
SELECT EmployeeKey, COUNT(*) AS Cnt
FROM Employees
GROUP BY EmployeeKey
HAVING COUNT(*) > 1;
-- EXPECTED: zero rows


-- ============================================================================
-- 2. Gender Domain Values
-- ============================================================================

-- 2a. Customer GenderCode must be M, F, or O (M=Male, F=Female, O=Organization).
-- Gender holds the readable label (Male/Female/Org); the single-char code is GenderCode.
SELECT GenderCode, COUNT(*) AS Cnt
FROM Customers
WHERE GenderCode NOT IN ('M', 'F', 'O')
GROUP BY GenderCode;
-- EXPECTED: zero rows

-- 2b. Employee Gender must be Male or Female (readable labels)
SELECT Gender, COUNT(*) AS Cnt
FROM Employees
WHERE Gender NOT IN ('Male', 'Female')
GROUP BY Gender;
-- EXPECTED: zero rows


-- ============================================================================
-- 3. Store Domain Values
-- ============================================================================

-- 3a. StoreType must be from known set
SELECT StoreType, COUNT(*) AS Cnt
FROM Stores
WHERE StoreType NOT IN ('Supermarket', 'Convenience', 'Online', 'Hypermarket', 'Fulfillment')
GROUP BY StoreType;
-- EXPECTED: zero rows

-- 3b. RevenueClass must be A, B, or C
SELECT RevenueClass, COUNT(*) AS Cnt
FROM Stores
WHERE RevenueClass NOT IN ('A', 'B', 'C')
GROUP BY RevenueClass;
-- EXPECTED: zero rows

-- 3c. Status distribution
SELECT
    Status,
    COUNT(*)                                                        AS Cnt,
    CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS Pct
FROM Stores
GROUP BY Status
ORDER BY Cnt DESC;
-- EXPECTED: Open ~85%, Closed ~10%, Renovating ~5%

-- 3d. EmployeeCount should be > 0 for open stores
SELECT StoreKey, Status, EmployeeCount
FROM Stores
WHERE Status = 'Open' AND EmployeeCount <= 0;
-- EXPECTED: zero rows


-- ============================================================================
-- 4. Customer Domain Values
-- ============================================================================

-- 4a. CustomerType must be Individual or Organization
SELECT CustomerType, COUNT(*) AS Cnt
FROM Customers
WHERE CustomerType NOT IN ('Individual', 'Organization')
GROUP BY CustomerType;
-- EXPECTED: zero rows

-- 4b. MaritalStatus domain (individuals only)
SELECT MaritalStatus, COUNT(*) AS Cnt
FROM Customers
WHERE CustomerType = 'Individual'
  AND MaritalStatus NOT IN ('Single', 'Married', 'Divorced', 'Widowed')
GROUP BY MaritalStatus;
-- EXPECTED: zero rows

-- 4c. HomeOwnership domain (individuals only)
SELECT HomeOwnership, COUNT(*) AS Cnt
FROM Customers
WHERE CustomerType = 'Individual'
  AND HomeOwnership NOT IN ('Own', 'Rent', 'Mortgage', 'Other')
GROUP BY HomeOwnership;
-- EXPECTED: zero rows

-- 4d. IncomeGroup domain
SELECT IncomeGroup, COUNT(*) AS Cnt
FROM Customers
WHERE CustomerType = 'Individual'
  AND IncomeGroup NOT IN ('Low', 'Mid', 'High', 'Premium')
GROUP BY IncomeGroup;
-- EXPECTED: zero rows

-- 4e. Orgs should have NULL demographics
SELECT COUNT(*) AS OrgsWithDemographics
FROM Customers
WHERE CustomerType = 'Organization'
  AND (MaritalStatus IS NOT NULL
       OR YearlyIncome IS NOT NULL
       OR NumberOfChildren IS NOT NULL);
-- EXPECTED: zero


-- ============================================================================
-- 5. Date Sanity
-- ============================================================================

-- 5a. Store: OpeningDate <= ClosingDate (when both exist)
SELECT StoreKey, OpeningDate, ClosingDate
FROM Stores
WHERE ClosingDate IS NOT NULL
  AND OpeningDate > ClosingDate;
-- EXPECTED: zero rows

-- 5b. Employee: HireDate <= TerminationDate
SELECT EmployeeKey, HireDate, TerminationDate
FROM Employees
WHERE TerminationDate IS NOT NULL
  AND HireDate > TerminationDate;
-- EXPECTED: zero rows

-- 5c. Customer: StartDate <= EndDate
SELECT CustomerKey, CustomerStartDate, CustomerEndDate
FROM Customers
WHERE CustomerEndDate IS NOT NULL
  AND CustomerStartDate > CustomerEndDate;
-- EXPECTED: zero rows

-- 5d. Promotion: StartDate <= EndDate
SELECT PromotionKey, StartDate, EndDate
FROM Promotions
WHERE StartDate > EndDate;
-- EXPECTED: zero rows

-- 5e. Employee store assignments: StartDate <= EndDate
SELECT EmployeeKey, StoreKey, StartDate, EndDate
FROM EmployeeStoreAssignments
WHERE EndDate IS NOT NULL
  AND StartDate > EndDate;
-- EXPECTED: zero rows


-- ============================================================================
-- 6. Product Domain Values
-- ============================================================================

-- 6a. ListPrice >= UnitCost (margin must be non-negative)
SELECT ProductKey, ListPrice, UnitCost
FROM Products
WHERE UnitCost > ListPrice;
-- EXPECTED: zero rows

-- 6b. Prices must be positive
SELECT ProductKey, ListPrice, UnitCost
FROM Products
WHERE ListPrice <= 0 OR UnitCost < 0;
-- EXPECTED: zero rows

-- 6c. ABCClassification domain in ProductProfile
SELECT ABCClassification, COUNT(*) AS Cnt
FROM ProductProfile
WHERE ABCClassification NOT IN ('A', 'B', 'C')
GROUP BY ABCClassification;
-- EXPECTED: zero rows

-- 6d. Every product should link to a valid subcategory
SELECT p.ProductKey, p.SubcategoryKey
FROM Products p
LEFT JOIN ProductSubcategory ps ON ps.SubcategoryKey = p.SubcategoryKey
WHERE ps.SubcategoryKey IS NULL;
-- EXPECTED: zero rows


-- ============================================================================
-- 7. Promotion Domain Values
-- ============================================================================

-- 7a. PromotionKey=1 should be "No Discount" sentinel
SELECT PromotionKey, PromotionName, DiscountPct
FROM Promotions
WHERE PromotionKey = 1
  AND (DiscountPct <> 0 OR PromotionName NOT LIKE '%No Discount%');
-- EXPECTED: zero rows

-- 7b. DiscountPct should be in [0, 1]
SELECT PromotionKey, DiscountPct
FROM Promotions
WHERE DiscountPct < 0 OR DiscountPct > 1;
-- EXPECTED: zero rows

-- 7c. Promotion types distribution
SELECT
    PromotionType,
    COUNT(*)                                                        AS Cnt,
    CAST(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS DECIMAL(5,1)) AS Pct
FROM Promotions
WHERE PromotionKey > 1
GROUP BY PromotionType
ORDER BY Cnt DESC;
-- EXPECTED: mix of Seasonal, Clearance, Limited, Flash, Volume, Loyalty, Bundle, NewCustomer


-- ============================================================================
-- 8. Employee Hierarchy Integrity
-- ============================================================================

-- 8a. CEO should have no parent
SELECT EmployeeKey, ParentEmployeeKey, Title, OrgLevel
FROM Employees
WHERE OrgLevel = 1 AND ParentEmployeeKey IS NOT NULL;
-- EXPECTED: zero rows

-- 8b. Non-CEO employees should have a valid parent
SELECT e.EmployeeKey, e.ParentEmployeeKey, e.OrgLevel
FROM Employees e
LEFT JOIN Employees p ON p.EmployeeKey = e.ParentEmployeeKey
WHERE e.OrgLevel > 1 AND p.EmployeeKey IS NULL;
-- EXPECTED: zero rows

-- 8c. Active employees should have IsActive=1 and no TerminationDate
SELECT EmployeeKey, IsActive, TerminationDate
FROM Employees
WHERE IsActive = 1 AND TerminationDate IS NOT NULL;
-- EXPECTED: zero rows (active employees shouldn't be terminated)

-- 8d. Terminated employees should have IsActive=0
SELECT EmployeeKey, IsActive, TerminationDate
FROM Employees
WHERE TerminationDate IS NOT NULL AND IsActive = 1;
-- EXPECTED: zero rows


-- ============================================================================
-- 9. Loyalty Tier & Acquisition Channel Integrity
-- ============================================================================

-- 9a. All customer LoyaltyTierKeys exist in lookup
SELECT DISTINCT c.LoyaltyTierKey
FROM Customers c
LEFT JOIN LoyaltyTiers lt ON lt.LoyaltyTierKey = c.LoyaltyTierKey
WHERE lt.LoyaltyTierKey IS NULL AND c.LoyaltyTierKey IS NOT NULL;
-- EXPECTED: zero rows

-- 9b. All CustomerAcquisitionChannelKeys exist in lookup
SELECT DISTINCT c.CustomerAcquisitionChannelKey
FROM Customers c
LEFT JOIN CustomerAcquisitionChannels cac
  ON cac.CustomerAcquisitionChannelKey = c.CustomerAcquisitionChannelKey
WHERE cac.CustomerAcquisitionChannelKey IS NULL
  AND c.CustomerAcquisitionChannelKey IS NOT NULL;
-- EXPECTED: zero rows


-- ============================================================================
-- 10. DIMENSION QUALITY SCORECARD
-- ============================================================================
SELECT
    'Customer: IsCurrent unique per ID' AS [Check],
    'No duplicate IsCurrent=1 rows for same CustomerID; FAIL = broken SCD2 current pointer' AS [Description],
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS Result
FROM (
    SELECT CustomerID FROM Customers WHERE IsCurrent = 1
    GROUP BY CustomerID HAVING COUNT(*) > 1
) x

UNION ALL

SELECT 'Customer: valid CustomerType',
    'Must be Individual or Organization; FAIL = unknown customer type in data',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Customers WHERE CustomerType NOT IN ('Individual', 'Organization')

UNION ALL

SELECT 'Customer: StartDate <= EndDate',
    'Churned customers must have StartDate before EndDate; FAIL = timeline inversion',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Customers WHERE CustomerEndDate IS NOT NULL AND CustomerStartDate > CustomerEndDate

UNION ALL

SELECT 'Product: UnitCost <= ListPrice',
    'Cost must not exceed selling price; FAIL = negative margin products',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Products WHERE UnitCost > ListPrice

UNION ALL

SELECT 'Product: valid ABC classification',
    'Must be A, B, or C; FAIL = unknown classification value',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM ProductProfile WHERE ABCClassification NOT IN ('A', 'B', 'C')

UNION ALL

SELECT 'Store: open stores have employees',
    'Open stores must have EmployeeCount > 0; FAIL = open store with no staff',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Stores WHERE Status = 'Open' AND EmployeeCount <= 0

UNION ALL

SELECT 'Store: OpeningDate <= ClosingDate',
    'Store cannot close before it opens; FAIL = date inversion',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Stores WHERE ClosingDate IS NOT NULL AND OpeningDate > ClosingDate

UNION ALL

SELECT 'Employee: HireDate <= TerminationDate',
    'Employee cannot be terminated before being hired; FAIL = date inversion',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Employees WHERE TerminationDate IS NOT NULL AND HireDate > TerminationDate

UNION ALL

SELECT 'Employee: hierarchy valid (non-CEO has parent)',
    'Every non-CEO employee must reference a valid ParentEmployeeKey; FAIL = broken org tree',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END
FROM Employees e
LEFT JOIN Employees p ON p.EmployeeKey = e.ParentEmployeeKey
WHERE e.OrgLevel > 1 AND p.EmployeeKey IS NULL

UNION ALL

SELECT 'Promotion: No Discount sentinel at key 1',
    'PromotionKey=1 must be the No Discount placeholder with 0% discount; FAIL = missing or misconfigured sentinel',
    CASE WHEN COUNT(*) = 1 THEN 'PASS' ELSE 'FAIL' END
FROM Promotions WHERE PromotionKey = 1 AND DiscountPct = 0;
