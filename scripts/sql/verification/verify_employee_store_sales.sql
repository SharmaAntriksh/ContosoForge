-- ============================================================================
-- VERIFY EMPLOYEE -> EMPLOYEE_STORE_ASSIGNMENTS -> SALES RELATIONSHIP
-- 41 checks matching scripts/verify_employee_store_sales.py
--
-- Run after loading generated data into SQL Server.
-- Supports both sales_output modes:
--   sales        -> single Sales table
--   sales_order  -> OrderHeader + OrderDetail tables
-- ============================================================================
SET NOCOUNT ON;

DECLARE @has_sales  BIT = CASE WHEN OBJECT_ID('dbo.Sales', 'U') IS NOT NULL THEN 1 ELSE 0 END;
DECLARE @has_header BIT = CASE WHEN OBJECT_ID('dbo.OrderHeader', 'U') IS NOT NULL THEN 1 ELSE 0 END;
DECLARE @sales_tbl  SYSNAME = CASE WHEN OBJECT_ID('dbo.Sales', 'U') IS NOT NULL THEN 'Sales' ELSE 'OrderHeader' END;

IF @has_sales = 0 AND @has_header = 0
BEGIN
    PRINT 'No Sales or OrderHeader table found — skipping.';
    RETURN;
END

CREATE TABLE #R (
    Seq         INT IDENTITY(1,1),
    Category    VARCHAR(30)  NOT NULL,
    [Check]     VARCHAR(120) NOT NULL,
    Result      VARCHAR(10)  NOT NULL,
    ActualValue VARCHAR(500) NOT NULL
);


-- ############################################################################
-- STORES
-- ############################################################################

-- 1. No duplicate StoreKeys
DECLARE @dup_sk INT;
SELECT @dup_sk = COUNT(*) FROM (
    SELECT StoreKey FROM dbo.Stores GROUP BY StoreKey HAVING COUNT(*) > 1
) x;
INSERT INTO #R VALUES ('Stores', 'No duplicate StoreKeys',
    CASE WHEN @dup_sk = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@dup_sk AS VARCHAR) + ' duplicate(s)');

-- 2. Store counts (INFO)
DECLARE @n_stores INT, @n_phys INT, @n_online INT;
SELECT @n_stores = COUNT(*),
       @n_phys   = SUM(CASE WHEN StoreKey < 10000 THEN 1 ELSE 0 END),
       @n_online = SUM(CASE WHEN StoreKey >= 10000 THEN 1 ELSE 0 END)
FROM dbo.Stores;
INSERT INTO #R VALUES ('Stores', 'Store counts', 'INFO',
    CAST(@n_stores AS VARCHAR) + ' stores (' + CAST(@n_phys AS VARCHAR) + ' physical, ' + CAST(@n_online AS VARCHAR) + ' online)');

-- 3. Online StoreKeys have StoreType=Online
DECLARE @bad_online INT;
SELECT @bad_online = COUNT(*) FROM dbo.Stores
WHERE StoreKey >= 10000 AND StoreType <> 'Online';
INSERT INTO #R VALUES ('Stores', 'Online StoreKeys have StoreType=Online',
    CASE WHEN @bad_online = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@bad_online AS VARCHAR) + ' violation(s)');

-- 4. Renovation count (INFO)
DECLARE @n_reno INT;
SELECT @n_reno = COUNT(*) FROM dbo.Stores
WHERE RenovationStartDate IS NOT NULL AND RenovationEndDate IS NOT NULL;
INSERT INTO #R VALUES ('Stores', 'Renovation count', 'INFO',
    CAST(@n_reno AS VARCHAR) + ' store(s) with renovation dates');

-- 5. RenovationEnd >= RenovationStart
DECLARE @reno_inv INT;
SELECT @reno_inv = COUNT(*) FROM dbo.Stores
WHERE RenovationStartDate IS NOT NULL AND RenovationEndDate IS NOT NULL
  AND RenovationEndDate < RenovationStartDate;
INSERT INTO #R VALUES ('Stores', 'RenovationEnd >= RenovationStart',
    CASE WHEN @reno_inv = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@reno_inv AS VARCHAR) + ' violation(s)');

-- 6. RenovationStart >= OpeningDate
DECLARE @reno_before_open INT;
SELECT @reno_before_open = COUNT(*) FROM dbo.Stores
WHERE RenovationStartDate IS NOT NULL AND RenovationStartDate < OpeningDate;
INSERT INTO #R VALUES ('Stores', 'RenovationStart >= OpeningDate',
    CASE WHEN @reno_before_open = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@reno_before_open AS VARCHAR) + ' violation(s)');

-- 7. ClosingDate implies Status=Closed
DECLARE @close_status INT;
SELECT @close_status = COUNT(*) FROM dbo.Stores
WHERE ClosingDate IS NOT NULL AND Status <> 'Closed';
INSERT INTO #R VALUES ('Stores', 'ClosingDate implies Status=Closed',
    CASE WHEN @close_status = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@close_status AS VARCHAR) + ' store(s) with ClosingDate but Status <> Closed');

-- 8. ClosingDate > OpeningDate
DECLARE @close_order INT;
SELECT @close_order = COUNT(*) FROM dbo.Stores
WHERE ClosingDate IS NOT NULL AND ClosingDate <= OpeningDate;
INSERT INTO #R VALUES ('Stores', 'ClosingDate > OpeningDate',
    CASE WHEN @close_order = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@close_order AS VARCHAR) + ' violation(s)');


-- ############################################################################
-- EMPLOYEES
-- ############################################################################

-- 9. No duplicate EmployeeKeys
DECLARE @dup_ek INT;
SELECT @dup_ek = COUNT(*) FROM (
    SELECT EmployeeKey FROM dbo.Employees GROUP BY EmployeeKey HAVING COUNT(*) > 1
) x;
INSERT INTO #R VALUES ('Employees', 'No duplicate EmployeeKeys',
    CASE WHEN @dup_ek = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@dup_ek AS VARCHAR) + ' duplicate(s)');

-- 10. HireDate <= TerminationDate
DECLARE @hire_term INT;
SELECT @hire_term = COUNT(*) FROM dbo.Employees
WHERE TerminationDate IS NOT NULL AND HireDate > TerminationDate;
INSERT INTO #R VALUES ('Employees', 'HireDate <= TerminationDate',
    CASE WHEN @hire_term = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@hire_term AS VARCHAR) + ' violation(s)');

-- 11. IsActive=1 has no TerminationDate
DECLARE @active_term INT;
SELECT @active_term = COUNT(*) FROM dbo.Employees
WHERE IsActive = 1 AND TerminationDate IS NOT NULL;
INSERT INTO #R VALUES ('Employees', 'IsActive=1 has no TerminationDate',
    CASE WHEN @active_term = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@active_term AS VARCHAR) + ' violation(s)');

-- 12. IsActive=0 has TerminationDate
DECLARE @inactive_no_term INT;
SELECT @inactive_no_term = COUNT(*) FROM dbo.Employees
WHERE IsActive = 0 AND TerminationDate IS NULL;
INSERT INTO #R VALUES ('Employees', 'IsActive=0 has TerminationDate',
    CASE WHEN @inactive_no_term = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@inactive_no_term AS VARCHAR) + ' violation(s)');

-- 13. Employee hierarchy valid (non-root ParentEmployeeKey exists)
DECLARE @orphan_parent INT;
SELECT @orphan_parent = COUNT(*) FROM dbo.Employees e
LEFT JOIN dbo.Employees p ON p.EmployeeKey = e.ParentEmployeeKey
WHERE e.OrgLevel > 1 AND p.EmployeeKey IS NULL;
INSERT INTO #R VALUES ('Employees', 'Employee hierarchy valid',
    CASE WHEN @orphan_parent = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@orphan_parent AS VARCHAR) + ' broken parent link(s)');

-- 14. No managers flagged as salespeople
DECLARE @mgr_sp INT;
SELECT @mgr_sp = COUNT(*) FROM dbo.Employees
WHERE EmployeeKey >= 30000000 AND EmployeeKey < 40000000
  AND IsSalesperson = 1;
INSERT INTO #R VALUES ('Employees', 'No managers flagged as salespeople',
    CASE WHEN @mgr_sp = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@mgr_sp AS VARCHAR) + ' manager(s) with IsSalesperson=1');


-- ############################################################################
-- ESA BRIDGE TABLE
-- ############################################################################

-- 15. No duplicate AssignmentKeys
DECLARE @dup_ak INT;
SELECT @dup_ak = COUNT(*) FROM (
    SELECT AssignmentKey FROM dbo.EmployeeStoreAssignments GROUP BY AssignmentKey HAVING COUNT(*) > 1
) x;
INSERT INTO #R VALUES ('ESA-Bridge', 'No duplicate AssignmentKeys',
    CASE WHEN @dup_ak = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@dup_ak AS VARCHAR) + ' duplicate(s)');

-- 16. StartDate <= EndDate
DECLARE @sd_ed INT;
SELECT @sd_ed = COUNT(*) FROM dbo.EmployeeStoreAssignments
WHERE EndDate IS NOT NULL AND StartDate > EndDate;
INSERT INTO #R VALUES ('ESA-Bridge', 'StartDate <= EndDate',
    CASE WHEN @sd_ed = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@sd_ed AS VARCHAR) + ' violation(s)');

-- 17-19. Distribution checks (INFO)
DECLARE @tr_info VARCHAR(200);
SELECT @tr_info = STRING_AGG(CAST(TransferReason AS VARCHAR(50)) + ':' + CAST(Cnt AS VARCHAR), ', ')
FROM (SELECT TransferReason, COUNT(*) AS Cnt FROM dbo.EmployeeStoreAssignments GROUP BY TransferReason) x;
INSERT INTO #R VALUES ('ESA-Bridge', 'TransferReason distribution', 'INFO', ISNULL(@tr_info, '(empty)'));

DECLARE @ip_true INT, @ip_false INT;
SELECT @ip_true  = SUM(CASE WHEN IsPrimary = 1 THEN 1 ELSE 0 END),
       @ip_false = SUM(CASE WHEN IsPrimary = 0 THEN 1 ELSE 0 END)
FROM dbo.EmployeeStoreAssignments;
INSERT INTO #R VALUES ('ESA-Bridge', 'IsPrimary distribution', 'INFO',
    'True:' + CAST(@ip_true AS VARCHAR) + ', False:' + CAST(@ip_false AS VARCHAR));

DECLARE @st_info VARCHAR(200);
SELECT @st_info = STRING_AGG(CAST(Status AS VARCHAR(30)) + ':' + CAST(Cnt AS VARCHAR), ', ')
FROM (SELECT Status, COUNT(*) AS Cnt FROM dbo.EmployeeStoreAssignments GROUP BY Status) x;
INSERT INTO #R VALUES ('ESA-Bridge', 'Status distribution', 'INFO', ISNULL(@st_info, '(empty)'));

-- 20. Renovation Reassignment IsPrimary rules
--     Temp (store reopened) must be False; Permanent (store closed) must be True
DECLARE @bad_temp_ip INT = 0, @bad_perm_ip INT = 0;
DECLARE @n_temp INT = 0, @n_perm INT = 0;
IF @n_reno > 0
BEGIN
    SELECT
        @n_temp      = ISNULL(SUM(CASE WHEN s.ClosingDate IS NULL THEN 1 ELSE 0 END), 0),
        @bad_temp_ip = ISNULL(SUM(CASE WHEN s.ClosingDate IS NULL  AND rr.IsPrimary = 1 THEN 1 ELSE 0 END), 0),
        @n_perm      = ISNULL(SUM(CASE WHEN s.ClosingDate IS NOT NULL THEN 1 ELSE 0 END), 0),
        @bad_perm_ip = ISNULL(SUM(CASE WHEN s.ClosingDate IS NOT NULL AND rr.IsPrimary = 0 THEN 1 ELSE 0 END), 0)
    FROM dbo.EmployeeStoreAssignments rr
    JOIN dbo.EmployeeStoreAssignments init
      ON init.EmployeeKey = rr.EmployeeKey AND init.TransferReason = 'Initial'
    JOIN dbo.Stores s ON s.StoreKey = init.StoreKey
    WHERE rr.TransferReason = 'Renovation Reassignment'
      AND s.RenovationStartDate IS NOT NULL;

    INSERT INTO #R VALUES ('ESA-Bridge', 'Renovation Reassignment IsPrimary rules',
        CASE WHEN @bad_temp_ip = 0 AND @bad_perm_ip = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@n_temp AS VARCHAR) + ' temp (bad:' + CAST(@bad_temp_ip AS VARCHAR) + '), '
        + CAST(@n_perm AS VARCHAR) + ' perm (bad:' + CAST(@bad_perm_ip AS VARCHAR) + ')');
END

-- 21. Renovation segments complete
--     Reopened stores: reassign + return. Closed stores: reassign only.
IF @n_reno > 0
BEGIN
    DECLARE @miss_reassign INT, @miss_return INT, @unexpected_return INT;

    SELECT
        @miss_reassign     = COUNT(DISTINCT CASE WHEN s.ClosingDate IS NULL     AND ea.EmployeeKey IS NULL     THEN init.EmployeeKey END),
        @miss_return       = COUNT(DISTINCT CASE WHEN s.ClosingDate IS NULL     AND er.EmployeeKey IS NULL     THEN init.EmployeeKey END),
        @unexpected_return = COUNT(DISTINCT CASE WHEN s.ClosingDate IS NOT NULL AND er.EmployeeKey IS NOT NULL THEN init.EmployeeKey END)
    FROM dbo.EmployeeStoreAssignments init
    JOIN dbo.Stores s ON s.StoreKey = init.StoreKey
    LEFT JOIN (SELECT DISTINCT EmployeeKey FROM dbo.EmployeeStoreAssignments WHERE TransferReason = 'Renovation Reassignment') ea
      ON ea.EmployeeKey = init.EmployeeKey
    LEFT JOIN (SELECT DISTINCT EmployeeKey FROM dbo.EmployeeStoreAssignments WHERE TransferReason = 'Renovation Return') er
      ON er.EmployeeKey = init.EmployeeKey
    WHERE init.TransferReason = 'Initial'
      AND s.RenovationStartDate IS NOT NULL;

    INSERT INTO #R VALUES ('ESA-Bridge', 'Renovation segments complete',
        CASE WHEN @miss_reassign = 0 AND @miss_return = 0 AND @unexpected_return = 0 THEN 'PASS' ELSE 'FAIL' END,
        'missing reassign:' + CAST(@miss_reassign AS VARCHAR)
        + ', missing return:' + CAST(@miss_return AS VARCHAR)
        + ', unexpected return:' + CAST(@unexpected_return AS VARCHAR));
END

-- 22. No overlapping same-store assignments
DECLARE @overlaps INT;
SELECT @overlaps = COUNT(*) FROM dbo.EmployeeStoreAssignments a
JOIN dbo.EmployeeStoreAssignments b
  ON  b.EmployeeKey = a.EmployeeKey
 AND b.StoreKey     = a.StoreKey
 AND b.StartDate    > a.StartDate
WHERE a.EndDate IS NOT NULL AND b.StartDate <= a.EndDate;
INSERT INTO #R VALUES ('ESA-Bridge', 'No overlapping same-store assignments',
    CASE WHEN @overlaps = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@overlaps AS VARCHAR) + ' overlap(s)');

-- 23. All ESA EmployeeKeys exist in Employees
DECLARE @orphan_ek INT;
SELECT @orphan_ek = COUNT(DISTINCT esa.EmployeeKey)
FROM dbo.EmployeeStoreAssignments esa
LEFT JOIN dbo.Employees e ON e.EmployeeKey = esa.EmployeeKey
WHERE e.EmployeeKey IS NULL;
INSERT INTO #R VALUES ('ESA-Bridge', 'All ESA EmployeeKeys exist in Employees',
    CASE WHEN @orphan_ek = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@orphan_ek AS VARCHAR) + ' orphan key(s)');

-- 24. All ESA StoreKeys exist in Stores
DECLARE @orphan_sk INT;
SELECT @orphan_sk = COUNT(DISTINCT esa.StoreKey)
FROM dbo.EmployeeStoreAssignments esa
LEFT JOIN dbo.Stores s ON s.StoreKey = esa.StoreKey
WHERE s.StoreKey IS NULL;
INSERT INTO #R VALUES ('ESA-Bridge', 'All ESA StoreKeys exist in Stores',
    CASE WHEN @orphan_sk = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@orphan_sk AS VARCHAR) + ' orphan key(s)');

-- 25. Every store-level employee has an assignment
DECLARE @no_assign INT;
SELECT @no_assign = COUNT(*) FROM dbo.Employees e
WHERE e.EmployeeKey >= 30000000
  AND NOT EXISTS (
    SELECT 1 FROM dbo.EmployeeStoreAssignments esa WHERE esa.EmployeeKey = e.EmployeeKey
  );
INSERT INTO #R VALUES ('ESA-Bridge', 'Every store-level employee has an assignment',
    CASE WHEN @no_assign = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@no_assign AS VARCHAR) + ' employee(s) with no ESA row');

-- 26. AssignmentSequence contiguous per employee (max = count, min = 1)
DECLARE @seq_gaps INT;
SELECT @seq_gaps = COUNT(*) FROM (
    SELECT EmployeeKey,
           MAX(AssignmentSequence) AS MaxSeq,
           COUNT(*)               AS Cnt,
           MIN(AssignmentSequence) AS MinSeq
    FROM dbo.EmployeeStoreAssignments
    GROUP BY EmployeeKey
    HAVING MAX(AssignmentSequence) <> COUNT(*) OR MIN(AssignmentSequence) <> 1
) x;
INSERT INTO #R VALUES ('ESA-Bridge', 'AssignmentSequence contiguous per employee',
    CASE WHEN @seq_gaps = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@seq_gaps AS VARCHAR) + ' employee(s) with non-contiguous sequence');

-- 27. No Active assignments for terminated employees
DECLARE @zombie INT;
SELECT @zombie = COUNT(*)
FROM dbo.EmployeeStoreAssignments esa
JOIN dbo.Employees e ON e.EmployeeKey = esa.EmployeeKey
WHERE e.IsActive = 0 AND esa.Status = 'Active';
INSERT INTO #R VALUES ('ESA-Bridge', 'No Active assignments for terminated employees',
    CASE WHEN @zombie = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@zombie AS VARCHAR) + ' zombie assignment(s)');

-- 28. Assignment StartDate >= HireDate
DECLARE @before_hire INT;
SELECT @before_hire = COUNT(*) FROM dbo.EmployeeStoreAssignments esa
JOIN dbo.Employees e ON e.EmployeeKey = esa.EmployeeKey
WHERE esa.StartDate < e.HireDate;
INSERT INTO #R VALUES ('ESA-Bridge', 'Assignment StartDate >= HireDate',
    CASE WHEN @before_hire = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@before_hire AS VARCHAR) + ' violation(s)');

-- 29. No ESA rows start after store ClosingDate
DECLARE @past_close INT;
SELECT @past_close = COUNT(*) FROM dbo.EmployeeStoreAssignments esa
JOIN dbo.Stores s ON s.StoreKey = esa.StoreKey
WHERE s.ClosingDate IS NOT NULL AND esa.StartDate >= s.ClosingDate;
INSERT INTO #R VALUES ('ESA-Bridge', 'No ESA rows start after store ClosingDate',
    CASE WHEN @past_close = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@past_close AS VARCHAR) + ' violation(s)');

-- 30. Salesperson coverage (every open non-renovating store-month)
--     Uses a months table crossed with physical stores, then checks for
--     at least 1 Sales Associate assignment covering the 1st of each month.
DECLARE @coverage_gaps INT;
DECLARE @cov_start DATE, @cov_end DATE;
SELECT @cov_start = CAST(DATEADD(MONTH, DATEDIFF(MONTH, 0, MIN(StartDate)), 0) AS DATE),
       @cov_end   = CAST(MAX(EndDate) AS DATE)
FROM dbo.EmployeeStoreAssignments;
;WITH Months AS (
    SELECT @cov_start AS dt
    UNION ALL
    SELECT DATEADD(MONTH, 1, dt) FROM Months
    WHERE DATEADD(MONTH, 1, dt) <= @cov_end
),
PhysStores AS (
    SELECT StoreKey, OpeningDate, ClosingDate, RenovationStartDate, RenovationEndDate
    FROM dbo.Stores WHERE StoreKey < 10000
),
StoreMonths AS (
    SELECT s.StoreKey, m.dt
    FROM PhysStores s CROSS JOIN Months m
    WHERE (s.OpeningDate IS NULL OR m.dt >= s.OpeningDate)
      AND (s.ClosingDate IS NULL OR m.dt < s.ClosingDate)
      AND NOT (s.RenovationStartDate IS NOT NULL
               AND s.RenovationEndDate IS NOT NULL
               AND m.dt >= s.RenovationStartDate
               AND m.dt < s.RenovationEndDate)
)
SELECT @coverage_gaps = COUNT(*) FROM StoreMonths sm
WHERE NOT EXISTS (
    SELECT 1 FROM dbo.EmployeeStoreAssignments esa
    WHERE esa.StoreKey = sm.StoreKey
      AND esa.RoleAtStore = 'Sales Associate'
      AND esa.StartDate <= sm.dt
      AND (esa.EndDate IS NULL OR esa.EndDate >= sm.dt)
)
OPTION (MAXRECURSION 0);
INSERT INTO #R VALUES ('ESA-Bridge', 'Salesperson coverage (every open store-month)',
    CASE WHEN @coverage_gaps = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@coverage_gaps AS VARCHAR) + ' store-month gap(s)');


-- ############################################################################
-- SALES ALIGNMENT (uses dynamic table name via IF/ELSE)
-- ############################################################################

-- 31. Sales row count (INFO)
DECLARE @n_sales INT, @min_date DATE, @max_date DATE, @n_s_stores INT, @n_s_emps INT;
IF @has_sales = 1
    SELECT @n_sales = COUNT(*), @min_date = MIN(OrderDate), @max_date = MAX(OrderDate),
           @n_s_stores = COUNT(DISTINCT StoreKey), @n_s_emps = COUNT(DISTINCT EmployeeKey)
    FROM dbo.Sales;
ELSE
    SELECT @n_sales = COUNT(*), @min_date = MIN(OrderDate), @max_date = MAX(OrderDate),
           @n_s_stores = COUNT(DISTINCT StoreKey), @n_s_emps = COUNT(DISTINCT EmployeeKey)
    FROM dbo.OrderHeader;
INSERT INTO #R VALUES ('Sales', 'Sales row count', 'INFO',
    CAST(@n_sales AS VARCHAR) + ' rows, ' + CAST(@min_date AS VARCHAR) + ' to ' + CAST(@max_date AS VARCHAR)
    + ', ' + CAST(@n_s_stores AS VARCHAR) + ' stores, ' + CAST(@n_s_emps AS VARCHAR) + ' employees');

-- 32. No null/zero EmployeeKey in sales
DECLARE @null_ek INT;
IF @has_sales = 1
    SELECT @null_ek = COUNT(*) FROM dbo.Sales WHERE EmployeeKey <= 0;
ELSE
    SELECT @null_ek = COUNT(*) FROM dbo.OrderHeader WHERE EmployeeKey <= 0;
INSERT INTO #R VALUES ('Sales', 'No null/zero EmployeeKey in sales',
    CASE WHEN @null_ek = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@null_ek AS VARCHAR) + ' sale(s) with EmployeeKey <= 0');

-- 33. Sales StoreKey exists in Stores
DECLARE @orphan_s_sk INT;
IF @has_sales = 1
    SELECT @orphan_s_sk = COUNT(*) FROM dbo.Sales f
    LEFT JOIN dbo.Stores s ON s.StoreKey = f.StoreKey
    WHERE s.StoreKey IS NULL;
ELSE
    SELECT @orphan_s_sk = COUNT(*) FROM dbo.OrderHeader f
    LEFT JOIN dbo.Stores s ON s.StoreKey = f.StoreKey
    WHERE s.StoreKey IS NULL;
INSERT INTO #R VALUES ('Sales', 'Sales StoreKey exists in Stores',
    CASE WHEN @orphan_s_sk = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@orphan_s_sk AS VARCHAR) + ' sale(s) referencing unknown StoreKey');

-- 34. Sales EmployeeKey exists in ESA bridge
DECLARE @orphan_s_ek INT;
IF @has_sales = 1
    SELECT @orphan_s_ek = COUNT(*) FROM (
        SELECT DISTINCT f.EmployeeKey FROM dbo.Sales f
        WHERE f.EmployeeKey > 0
          AND NOT EXISTS (SELECT 1 FROM dbo.EmployeeStoreAssignments esa WHERE esa.EmployeeKey = f.EmployeeKey)
    ) x;
ELSE
    SELECT @orphan_s_ek = COUNT(*) FROM (
        SELECT DISTINCT f.EmployeeKey FROM dbo.OrderHeader f
        WHERE f.EmployeeKey > 0
          AND NOT EXISTS (SELECT 1 FROM dbo.EmployeeStoreAssignments esa WHERE esa.EmployeeKey = f.EmployeeKey)
    ) x;
INSERT INTO #R VALUES ('Sales', 'Sales EmployeeKey exists in ESA bridge',
    CASE WHEN @orphan_s_ek = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@orphan_s_ek AS VARCHAR) + ' unknown EmployeeKey(s)');

-- 35. No manager keys in sales (30M-40M range)
DECLARE @mgr_sales INT;
IF @has_sales = 1
    SELECT @mgr_sales = COUNT(*) FROM dbo.Sales
    WHERE EmployeeKey >= 30000000 AND EmployeeKey < 40000000;
ELSE
    SELECT @mgr_sales = COUNT(*) FROM dbo.OrderHeader
    WHERE EmployeeKey >= 30000000 AND EmployeeKey < 40000000;
INSERT INTO #R VALUES ('Sales', 'No manager keys in sales',
    CASE WHEN @mgr_sales = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@mgr_sales AS VARCHAR) + ' sale(s) with manager EmployeeKey');

-- 36. Online/physical channel alignment
DECLARE @phys_at_online INT = 0, @online_at_phys INT = 0;
IF @has_sales = 1
BEGIN
    SELECT @phys_at_online = COUNT(*) FROM dbo.Sales
    WHERE StoreKey >= 10000 AND EmployeeKey < 50000000 AND EmployeeKey > 0;
    SELECT @online_at_phys = COUNT(*) FROM dbo.Sales
    WHERE StoreKey < 10000 AND EmployeeKey >= 50000000;
END
ELSE
BEGIN
    SELECT @phys_at_online = COUNT(*) FROM dbo.OrderHeader
    WHERE StoreKey >= 10000 AND EmployeeKey < 50000000 AND EmployeeKey > 0;
    SELECT @online_at_phys = COUNT(*) FROM dbo.OrderHeader
    WHERE StoreKey < 10000 AND EmployeeKey >= 50000000;
END
INSERT INTO #R VALUES ('Sales', 'Online/physical channel alignment',
    CASE WHEN @phys_at_online = 0 AND @online_at_phys = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@phys_at_online AS VARCHAR) + ' phys emp at online, '
    + CAST(@online_at_phys AS VARCHAR) + ' online emp at phys');

-- 37. Quantity > 0 and UnitPrice > 0
DECLARE @bad_qty INT = 0, @bad_price INT = 0;
IF @has_sales = 1
BEGIN
    SELECT @bad_qty = COUNT(*) FROM dbo.Sales WHERE Quantity <= 0;
    SELECT @bad_price = COUNT(*) FROM dbo.Sales WHERE UnitPrice <= 0;
END
ELSE IF @has_header = 1 AND OBJECT_ID('dbo.OrderDetail', 'U') IS NOT NULL
BEGIN
    SELECT @bad_qty = COUNT(*) FROM dbo.OrderDetail WHERE Quantity <= 0;
    SELECT @bad_price = COUNT(*) FROM dbo.OrderDetail WHERE UnitPrice <= 0;
END
INSERT INTO #R VALUES ('Sales', 'Quantity > 0 and UnitPrice > 0',
    CASE WHEN @bad_qty = 0 AND @bad_price = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@bad_qty AS VARCHAR) + ' zero/neg qty, ' + CAST(@bad_price AS VARCHAR) + ' zero/neg price');

-- 38. Sales-to-ESA 100% match (employee+store+date within ESA window)
DECLARE @unmatched INT;
IF @has_sales = 1
    SELECT @unmatched = COUNT(*) FROM dbo.Sales f
    WHERE f.EmployeeKey > 0
      AND NOT EXISTS (
        SELECT 1 FROM dbo.EmployeeStoreAssignments esa
        WHERE esa.EmployeeKey = f.EmployeeKey
          AND esa.StoreKey    = f.StoreKey
          AND f.OrderDate    >= esa.StartDate
          AND (esa.EndDate IS NULL OR f.OrderDate <= esa.EndDate)
      );
ELSE
    SELECT @unmatched = COUNT(*) FROM dbo.OrderHeader f
    WHERE f.EmployeeKey > 0
      AND NOT EXISTS (
        SELECT 1 FROM dbo.EmployeeStoreAssignments esa
        WHERE esa.EmployeeKey = f.EmployeeKey
          AND esa.StoreKey    = f.StoreKey
          AND f.OrderDate    >= esa.StartDate
          AND (esa.EndDate IS NULL OR f.OrderDate <= esa.EndDate)
      );
DECLARE @match_pct DECIMAL(7,2) = CASE WHEN @n_sales > 0 THEN (@n_sales - @unmatched) * 100.0 / @n_sales ELSE 100.0 END;
INSERT INTO #R VALUES ('Sales', 'Sales-to-ESA 100% match',
    CASE WHEN @unmatched = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@match_pct AS VARCHAR) + '% (' + CAST(@n_sales - @unmatched AS VARCHAR) + '/' + CAST(@n_sales AS VARCHAR) + '). '
    + CAST(@unmatched AS VARCHAR) + ' unmatched');

-- 39. No sales during renovation
DECLARE @reno_sales INT;
IF @has_sales = 1
    SELECT @reno_sales = COUNT(*) FROM dbo.Sales f
    JOIN dbo.Stores s ON s.StoreKey = f.StoreKey
    WHERE s.RenovationStartDate IS NOT NULL AND s.RenovationEndDate IS NOT NULL
      AND f.OrderDate >= s.RenovationStartDate AND f.OrderDate < s.RenovationEndDate;
ELSE
    SELECT @reno_sales = COUNT(*) FROM dbo.OrderHeader f
    JOIN dbo.Stores s ON s.StoreKey = f.StoreKey
    WHERE s.RenovationStartDate IS NOT NULL AND s.RenovationEndDate IS NOT NULL
      AND f.OrderDate >= s.RenovationStartDate AND f.OrderDate < s.RenovationEndDate;
INSERT INTO #R VALUES ('Sales', 'No sales during renovation',
    CASE WHEN @reno_sales = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@reno_sales AS VARCHAR) + ' violation(s)');

-- 40. No sales after store closure
DECLARE @post_close INT;
IF @has_sales = 1
    SELECT @post_close = COUNT(*) FROM dbo.Sales f
    JOIN dbo.Stores s ON s.StoreKey = f.StoreKey
    WHERE s.ClosingDate IS NOT NULL AND f.OrderDate >= s.ClosingDate;
ELSE
    SELECT @post_close = COUNT(*) FROM dbo.OrderHeader f
    JOIN dbo.Stores s ON s.StoreKey = f.StoreKey
    WHERE s.ClosingDate IS NOT NULL AND f.OrderDate >= s.ClosingDate;
INSERT INTO #R VALUES ('Sales', 'No sales after store closure',
    CASE WHEN @post_close = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@post_close AS VARCHAR) + ' violation(s)');

-- 41. No post-transfer sales leakage
DECLARE @leaked INT;
IF @has_sales = 1
    SELECT @leaked = COUNT(*) FROM dbo.Sales f
    JOIN (
        SELECT EmployeeKey, StoreKey, MAX(EndDate) AS LastEnd
        FROM dbo.EmployeeStoreAssignments
        GROUP BY EmployeeKey, StoreKey
    ) le ON le.EmployeeKey = f.EmployeeKey AND le.StoreKey = f.StoreKey
    WHERE f.OrderDate > le.LastEnd;
ELSE
    SELECT @leaked = COUNT(*) FROM dbo.OrderHeader f
    JOIN (
        SELECT EmployeeKey, StoreKey, MAX(EndDate) AS LastEnd
        FROM dbo.EmployeeStoreAssignments
        GROUP BY EmployeeKey, StoreKey
    ) le ON le.EmployeeKey = f.EmployeeKey AND le.StoreKey = f.StoreKey
    WHERE f.OrderDate > le.LastEnd;
INSERT INTO #R VALUES ('Sales', 'No post-transfer sales leakage',
    CASE WHEN @leaked = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@leaked AS VARCHAR) + ' sale(s) after last ESA EndDate');


-- ############################################################################
-- RESULTS
-- ############################################################################

DECLARE @total INT, @passed INT, @failed INT;
SELECT @total  = COUNT(*),
       @passed = SUM(CASE WHEN Result = 'PASS' THEN 1 ELSE 0 END),
       @failed = SUM(CASE WHEN Result = 'FAIL' THEN 1 ELSE 0 END)
FROM #R;

SELECT Category, [Check], Result, ActualValue FROM #R ORDER BY Seq;

PRINT '';
PRINT 'SUMMARY: ' + CAST(@passed AS VARCHAR) + '/' + CAST(@total AS VARCHAR) + ' passed, '
    + CAST(@failed AS VARCHAR) + ' failed';

DROP TABLE #R;
