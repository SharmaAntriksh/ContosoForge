SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

CREATE OR ALTER PROCEDURE [verify].[EmployeeStoreSales]
AS
BEGIN
    SET NOCOUNT ON;

    IF OBJECT_ID(N'dbo.EmployeeStoreAssignments', N'U') IS NULL
       OR OBJECT_ID(N'dbo.Employees', N'U') IS NULL
       OR OBJECT_ID(N'dbo.Stores', N'U') IS NULL
        RETURN;

    DECLARE @has_sales  BIT = CASE WHEN OBJECT_ID('dbo.Sales',            'U') IS NOT NULL THEN 1 ELSE 0 END;
    DECLARE @has_header BIT = CASE WHEN OBJECT_ID('dbo.OrderHeader', 'U') IS NOT NULL THEN 1 ELSE 0 END;

    CREATE TABLE #R (
        Category    VARCHAR(50)  NOT NULL,
        [Check]     VARCHAR(200) NOT NULL,
        Description VARCHAR(500) NOT NULL,
        Result      VARCHAR(10)  NOT NULL,
        ActualValue VARCHAR(500) NOT NULL
    );

    -- ########################################################################
    -- EMPLOYEES
    -- ########################################################################

    -- Duplicate EmployeeKeys
    DECLARE @dup_ek INT;
    SELECT @dup_ek = COUNT(*) FROM (
        SELECT EmployeeKey FROM dbo.Employees GROUP BY EmployeeKey HAVING COUNT(*) > 1
    ) x;
    INSERT INTO #R VALUES ('Employees', 'No duplicate EmployeeKeys',
        'EmployeeKey must be unique across all rows',
        CASE WHEN @dup_ek = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@dup_ek AS VARCHAR) + ' duplicate(s)');

    -- HireDate <= TerminationDate
    DECLARE @emp_date_inv INT;
    SELECT @emp_date_inv = COUNT(*) FROM dbo.Employees
    WHERE TerminationDate IS NOT NULL AND HireDate > TerminationDate;
    INSERT INTO #R VALUES ('Employees', 'Employee HireDate <= TerminationDate',
        'Employee cannot be terminated before being hired',
        CASE WHEN @emp_date_inv = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@emp_date_inv AS VARCHAR) + ' inversions');

    -- IsActive=1 has no TerminationDate
    DECLARE @active_term INT;
    SELECT @active_term = COUNT(*) FROM dbo.Employees
    WHERE IsActive = 1 AND TerminationDate IS NOT NULL;
    INSERT INTO #R VALUES ('Employees', 'IsActive=1 has no TerminationDate',
        'Active employees must not have a termination date',
        CASE WHEN @active_term = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@active_term AS VARCHAR) + ' violation(s)');

    -- IsActive=0 has TerminationDate
    DECLARE @inactive_no_term INT;
    SELECT @inactive_no_term = COUNT(*) FROM dbo.Employees
    WHERE IsActive = 0 AND TerminationDate IS NULL;
    INSERT INTO #R VALUES ('Employees', 'IsActive=0 has TerminationDate',
        'Inactive employees must have a termination date',
        CASE WHEN @inactive_no_term = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@inactive_no_term AS VARCHAR) + ' violation(s)');

    -- Employee hierarchy valid
    DECLARE @bad_parent INT;
    SELECT @bad_parent = COUNT(*) FROM dbo.Employees e
    LEFT JOIN dbo.Employees p ON p.EmployeeKey = e.ParentEmployeeKey
    WHERE e.OrgLevel > 1 AND p.EmployeeKey IS NULL;
    INSERT INTO #R VALUES ('Employees', 'Employee hierarchy valid',
        'Every non-CEO employee must reference a valid ParentEmployeeKey',
        CASE WHEN @bad_parent = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@bad_parent AS VARCHAR) + ' broken links');

    -- No managers flagged as salespeople
    DECLARE @mgr_sp INT;
    SELECT @mgr_sp = COUNT(*) FROM dbo.Employees
    WHERE Title = 'Store Manager'
      AND IsSalesperson = 1;
    INSERT INTO #R VALUES ('Employees', 'No managers flagged as salespeople',
        'Employees with Title=Store Manager must not have IsSalesperson=1',
        CASE WHEN @mgr_sp = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@mgr_sp AS VARCHAR) + ' manager(s) with IsSalesperson=1');

    -- ########################################################################
    -- ESA BRIDGE TABLE
    -- ########################################################################

    -- Duplicate AssignmentKeys
    DECLARE @dup_ak INT;
    SELECT @dup_ak = COUNT(*) FROM (
        SELECT AssignmentKey FROM dbo.EmployeeStoreAssignments GROUP BY AssignmentKey HAVING COUNT(*) > 1
    ) x;
    INSERT INTO #R VALUES ('ESA-Bridge', 'No duplicate AssignmentKeys',
        'AssignmentKey must be unique across all ESA rows',
        CASE WHEN @dup_ak = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@dup_ak AS VARCHAR) + ' duplicate(s)');

    -- StartDate <= EndDate
    DECLARE @sd_ed INT;
    SELECT @sd_ed = COUNT(*) FROM dbo.EmployeeStoreAssignments
    WHERE EndDate IS NOT NULL AND StartDate > EndDate;
    INSERT INTO #R VALUES ('ESA-Bridge', 'StartDate <= EndDate',
        'No assignment row may have StartDate after EndDate',
        CASE WHEN @sd_ed = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@sd_ed AS VARCHAR) + ' violation(s)');

    -- No overlapping same-store assignments
    DECLARE @overlaps INT;
    SELECT @overlaps = COUNT(*) FROM dbo.EmployeeStoreAssignments a
    JOIN dbo.EmployeeStoreAssignments b
      ON  b.EmployeeKey = a.EmployeeKey
     AND b.StoreKey     = a.StoreKey
     AND b.StartDate    > a.StartDate
    WHERE a.EndDate IS NOT NULL AND b.StartDate <= a.EndDate;
    INSERT INTO #R VALUES ('ESA-Bridge', 'No overlapping same-store assignments',
        'An employee should not have overlapping date windows at the same store',
        CASE WHEN @overlaps = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@overlaps AS VARCHAR) + ' overlaps');

    -- All ESA EmployeeKeys exist in Employees
    DECLARE @orphan_ek INT;
    SELECT @orphan_ek = COUNT(DISTINCT esa.EmployeeKey)
    FROM dbo.EmployeeStoreAssignments esa
    LEFT JOIN dbo.Employees e ON e.EmployeeKey = esa.EmployeeKey
    WHERE e.EmployeeKey IS NULL;
    INSERT INTO #R VALUES ('ESA-Bridge', 'All assignment keys exist in Employees',
        'EmployeeKey in bridge table references a valid Employees row',
        CASE WHEN @orphan_ek = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@orphan_ek AS VARCHAR) + ' invalid keys');

    -- All ESA StoreKeys exist in Stores
    IF OBJECT_ID(N'dbo.Stores', N'U') IS NOT NULL
    BEGIN
        DECLARE @orphan_sk INT;
        SELECT @orphan_sk = COUNT(DISTINCT esa.StoreKey)
        FROM dbo.EmployeeStoreAssignments esa
        LEFT JOIN dbo.Stores s ON s.StoreKey = esa.StoreKey
        WHERE s.StoreKey IS NULL;
        INSERT INTO #R VALUES ('ESA-Bridge', 'All ESA StoreKeys exist in Stores',
            'StoreKey in ESA must reference a valid Stores row',
            CASE WHEN @orphan_sk = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@orphan_sk AS VARCHAR) + ' orphan key(s)');
    END

    -- Assignment dates within employment
    DECLARE @bad_dates INT;
    SELECT @bad_dates = COUNT(*) FROM dbo.EmployeeStoreAssignments esa
    JOIN dbo.Employees e ON e.EmployeeKey = esa.EmployeeKey
    WHERE esa.StartDate < e.HireDate
       OR (e.TerminationDate IS NOT NULL AND esa.EndDate IS NOT NULL
           AND esa.EndDate > e.TerminationDate);
    INSERT INTO #R VALUES ('ESA-Bridge', 'Assignment dates within employment',
        'Assignment StartDate >= HireDate and EndDate <= TerminationDate',
        CASE WHEN @bad_dates = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@bad_dates AS VARCHAR) + ' violations');

    -- No Active assignments for terminated employees
    DECLARE @zombie INT;
    SELECT @zombie = COUNT(*)
    FROM dbo.EmployeeStoreAssignments esa
    JOIN dbo.Employees e ON e.EmployeeKey = esa.EmployeeKey
    WHERE e.IsActive = 0 AND esa.AssignmentStatus = 'Active';
    INSERT INTO #R VALUES ('ESA-Bridge', 'No Active assignments for terminated employees',
        'Terminated employees must not have AssignmentStatus=Active in ESA',
        CASE WHEN @zombie = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@zombie AS VARCHAR) + ' zombie assignment(s)');

    -- AssignmentSequence contiguous per employee (min=1, max=count)
    DECLARE @seq_gaps INT;
    SELECT @seq_gaps = COUNT(*) FROM (
        SELECT EmployeeKey
        FROM dbo.EmployeeStoreAssignments
        GROUP BY EmployeeKey
        HAVING MAX(AssignmentSequence) <> COUNT(*) OR MIN(AssignmentSequence) <> 1
    ) x;
    INSERT INTO #R VALUES ('ESA-Bridge', 'AssignmentSequence contiguous per employee',
        'Each employee sequence must start at 1 with no gaps',
        CASE WHEN @seq_gaps = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@seq_gaps AS VARCHAR) + ' employee(s) with non-contiguous sequence');

    -- Every store-level employee has an assignment
    DECLARE @no_assign INT;
    SELECT @no_assign = COUNT(*) FROM dbo.Employees e
    WHERE e.HomeStoreKey IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM dbo.EmployeeStoreAssignments esa WHERE esa.EmployeeKey = e.EmployeeKey
      );
    INSERT INTO #R VALUES ('ESA-Bridge', 'Store employees have assignments',
        'Every store-level employee must have at least one ESA row',
        CASE WHEN @no_assign = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@no_assign AS VARCHAR) + ' without assignments');

    -- No ESA rows start after store ClosingDate
    IF OBJECT_ID(N'dbo.Stores', N'U') IS NOT NULL
    BEGIN
        DECLARE @past_close INT;
        SELECT @past_close = COUNT(*) FROM dbo.EmployeeStoreAssignments esa
        JOIN dbo.Stores s ON s.StoreKey = esa.StoreKey
        WHERE s.ClosingDate IS NOT NULL AND esa.StartDate >= s.ClosingDate;
        INSERT INTO #R VALUES ('ESA-Bridge', 'No ESA rows start after store ClosingDate',
            'Assignment start must be before the store closing date',
            CASE WHEN @past_close = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@past_close AS VARCHAR) + ' violation(s)');
    END

    -- Renovation current-assignment rules (only if Stores has renovation columns)
    IF OBJECT_ID(N'dbo.Stores', N'U') IS NOT NULL
       AND COL_LENGTH('dbo.Stores', 'RenovationStartDate') IS NOT NULL
    BEGIN
        DECLARE @n_reno INT;
        SELECT @n_reno = COUNT(*) FROM dbo.Stores
        WHERE RenovationStartDate IS NOT NULL AND RenovationEndDate IS NOT NULL;

        IF @n_reno > 0
        BEGIN
            DECLARE @n_rr INT, @bad_rr_current INT;
            SELECT
                @n_rr = COUNT(*),
                @bad_rr_current = ISNULL(SUM(CASE
                    WHEN rr.IsCurrentAssignment <>
                         CASE WHEN rr.AssignmentSequence = latest.MaxSequence THEN 1 ELSE 0 END
                    THEN 1 ELSE 0 END), 0)
            FROM dbo.EmployeeStoreAssignments rr
            JOIN (
                SELECT EmployeeKey, MAX(AssignmentSequence) AS MaxSequence
                FROM dbo.EmployeeStoreAssignments
                GROUP BY EmployeeKey
            ) latest ON latest.EmployeeKey = rr.EmployeeKey
            WHERE rr.TransferReason = 'Renovation Reassignment';

            INSERT INTO #R VALUES ('ESA-Bridge', 'Renovation Reassignment current flags',
                'A reassignment is current only when it is the employee''s latest segment',
                CASE WHEN @bad_rr_current = 0 THEN 'PASS' ELSE 'FAIL' END,
                CAST(@n_rr AS VARCHAR) + ' segment(s), ' + CAST(@bad_rr_current AS VARCHAR) + ' stale flag(s)');

            -- Renovation segments complete
            DECLARE @miss_reassign INT, @miss_return INT, @malformed_return INT;

            ;WITH EmployeeEnd AS (
                SELECT EmployeeKey, MAX(EndDate) AS EmploymentEnd
                FROM dbo.EmployeeStoreAssignments
                GROUP BY EmployeeKey
            ), ExpectedHomeRenovation AS (
                SELECT
                    init.EmployeeKey,
                    s.StoreKey,
                    s.ClosingDate,
                    s.RenovationStartDate,
                    s.RenovationEndDate,
                    ee.EmploymentEnd,
                    CASE
                        WHEN init.StartDate > s.RenovationStartDate THEN init.StartDate
                        ELSE s.RenovationStartDate
                    END AS ExpectedReassignmentStart
                FROM dbo.EmployeeStoreAssignments init
                JOIN dbo.Stores s ON s.StoreKey = init.StoreKey
                JOIN EmployeeEnd ee ON ee.EmployeeKey = init.EmployeeKey
                WHERE init.TransferReason = 'Initial'
                  AND s.RenovationStartDate IS NOT NULL
                  AND s.RenovationEndDate IS NOT NULL
                  AND init.StartDate < s.RenovationEndDate
                  AND ee.EmploymentEnd >= s.RenovationStartDate
            )
            SELECT
                @miss_reassign = ISNULL(SUM(CASE
                    WHEN rr.EmployeeKey IS NULL THEN 1 ELSE 0 END), 0),
                @miss_return = ISNULL(SUM(CASE
                    WHEN expected.ClosingDate IS NULL
                     AND expected.EmploymentEnd >= expected.RenovationEndDate
                     AND ret.EmployeeKey IS NULL
                    THEN 1 ELSE 0 END), 0)
            FROM ExpectedHomeRenovation expected
            LEFT JOIN (
                SELECT DISTINCT EmployeeKey, StartDate
                FROM dbo.EmployeeStoreAssignments
                WHERE TransferReason = 'Renovation Reassignment'
            ) rr
              ON rr.EmployeeKey = expected.EmployeeKey
             AND rr.StartDate = expected.ExpectedReassignmentStart
            LEFT JOIN (
                SELECT DISTINCT EmployeeKey, StoreKey, StartDate
                FROM dbo.EmployeeStoreAssignments
                WHERE TransferReason = 'Renovation Return'
            ) ret
              ON ret.EmployeeKey = expected.EmployeeKey
             AND ret.StoreKey = expected.StoreKey
             AND ret.StartDate = expected.RenovationEndDate;

            SELECT @malformed_return = COUNT(*)
            FROM dbo.EmployeeStoreAssignments ret
            LEFT JOIN dbo.EmployeeStoreAssignments rr
              ON rr.EmployeeKey = ret.EmployeeKey
             AND rr.AssignmentSequence = ret.AssignmentSequence - 1
            LEFT JOIN dbo.Stores s ON s.StoreKey = ret.StoreKey
            WHERE ret.TransferReason = 'Renovation Return'
              AND (
                   rr.TransferReason IS NULL
                OR rr.TransferReason <> 'Renovation Reassignment'
                OR s.StoreKey IS NULL
                OR s.RenovationEndDate IS NULL
                OR s.ClosingDate IS NOT NULL
                OR ret.StartDate <> s.RenovationEndDate
                OR rr.EndDate <> DATEADD(DAY, -1, ret.StartDate)
              );

            INSERT INTO #R VALUES ('ESA-Bridge', 'Renovation segments complete',
                'Expected home-store hops exist and every return is a contiguous hop to a reopened store',
                CASE WHEN @miss_reassign = 0 AND @miss_return = 0 AND @malformed_return = 0 THEN 'PASS' ELSE 'FAIL' END,
                'missing reassign:' + CAST(@miss_reassign AS VARCHAR)
                + ', missing return:' + CAST(@miss_return AS VARCHAR)
                + ', malformed return:' + CAST(@malformed_return AS VARCHAR));
        END
    END

-- Salesperson coverage: union of eligible intervals on every operating day.
-- Include online stores, partial months, and adjacent handoffs. No status flags.
DECLARE @coverage_gaps INT;
DECLARE @cov_start DATE, @cov_end DATE;
SELECT @cov_start = MIN(StartDate), @cov_end = MAX(EndDate)
FROM dbo.EmployeeStoreAssignments;
;WITH OperatingWindows AS (
    SELECT s.StoreKey, s.RenovationStartDate, s.RenovationEndDate,
           CASE WHEN s.OpeningDate > @cov_start THEN s.OpeningDate ELSE @cov_start END AS FirstDay,
           CASE WHEN s.ClosingDate <= @cov_end THEN DATEADD(DAY, -1, s.ClosingDate) ELSE @cov_end END AS LastDay
    FROM dbo.Stores s
    WHERE s.OpeningDate IS NOT NULL
), WindowParts AS (
    SELECT StoreKey, FirstDay, LastDay FROM OperatingWindows
    WHERE RenovationStartDate IS NULL OR RenovationEndDate IS NULL
       OR RenovationStartDate >= RenovationEndDate
    UNION ALL
    SELECT StoreKey, FirstDay,
           CASE WHEN RenovationStartDate <= LastDay THEN DATEADD(DAY, -1, RenovationStartDate) ELSE LastDay END
    FROM OperatingWindows
    WHERE RenovationStartDate < RenovationEndDate
    UNION ALL
    SELECT StoreKey,
           CASE WHEN RenovationEndDate > FirstDay THEN RenovationEndDate ELSE FirstDay END, LastDay
    FROM OperatingWindows
    WHERE RenovationStartDate < RenovationEndDate
), RequiredWindows AS (
    SELECT StoreKey, FirstDay, LastDay FROM WindowParts WHERE FirstDay <= LastDay
), EligibleIntervals AS (
    SELECT a.StoreKey,
           CASE WHEN e.HireDate > a.StartDate THEN e.HireDate ELSE a.StartDate END AS FirstDay,
           CASE WHEN e.TerminationDate < a.EndDate THEN e.TerminationDate ELSE a.EndDate END AS LastDay
    FROM dbo.EmployeeStoreAssignments a
    JOIN dbo.Employees e ON e.EmployeeKey = a.EmployeeKey
    JOIN dbo.Stores s ON s.StoreKey = a.StoreKey
    WHERE e.IsSalesperson = 1 AND e.Title <> 'Store Manager' AND a.RoleAtStore = e.Title
      AND e.HireDate IS NOT NULL AND a.StartDate IS NOT NULL AND a.EndDate IS NOT NULL
      AND ((a.RoleAtStore = 'Online Sales Representative' AND s.StoreType = 'Online')
        OR (a.RoleAtStore <> 'Online Sales Representative' AND s.StoreType <> 'Online'))
), Coverage AS (
    SELECT DISTINCT w.StoreKey, w.FirstDay, w.LastDay,
           CASE WHEN a.FirstDay > w.FirstDay THEN a.FirstDay ELSE w.FirstDay END AS CoveredStart,
           CASE WHEN a.LastDay < w.LastDay THEN a.LastDay ELSE w.LastDay END AS CoveredEnd
    FROM RequiredWindows w
    JOIN EligibleIntervals a ON a.StoreKey = w.StoreKey
        AND a.FirstDay <= a.LastDay
        AND a.FirstDay <= w.LastDay AND a.LastDay >= w.FirstDay
), Boundaries AS (
    SELECT *, MAX(CoveredEnd) OVER (
        PARTITION BY StoreKey, FirstDay, LastDay ORDER BY CoveredStart, CoveredEnd
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ) AS PreviousEnd
    FROM Coverage
)
SELECT @coverage_gaps = COUNT(*) FROM (
    SELECT DISTINCT StoreKey, FirstDay, LastDay FROM RequiredWindows w
    WHERE NOT EXISTS (SELECT 1 FROM Coverage c WHERE c.StoreKey = w.StoreKey
                      AND c.FirstDay = w.FirstDay AND c.LastDay = w.LastDay)
       OR EXISTS (SELECT 1 FROM Boundaries b WHERE b.StoreKey = w.StoreKey
                  AND b.FirstDay = w.FirstDay AND b.LastDay = w.LastDay
                  AND ((b.PreviousEnd IS NULL AND b.CoveredStart > w.FirstDay)
                    OR b.CoveredStart > DATEADD(DAY, 1, b.PreviousEnd)))
       OR (SELECT MAX(c.CoveredEnd) FROM Coverage c WHERE c.StoreKey = w.StoreKey
           AND c.FirstDay = w.FirstDay AND c.LastDay = w.LastDay) < w.LastDay
) gaps;
SELECT @coverage_gaps = @coverage_gaps + COUNT(*) FROM dbo.Stores WHERE OpeningDate IS NULL;
IF @cov_start IS NULL OR @cov_end IS NULL
    SELECT @coverage_gaps = COUNT(*) FROM dbo.Stores;
INSERT INTO #R VALUES ('ESA-Bridge', 'Salesperson coverage (every operating day)',
    'Union of eligible intervals covers all operating days in the assignment observation window',
    CASE WHEN @coverage_gaps = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@coverage_gaps AS VARCHAR) + ' operating window(s) with gaps');

    -- INFO: TransferReason distribution
    DECLARE @tr_info VARCHAR(500);
    SELECT @tr_info = STRING_AGG(CAST(TransferReason AS VARCHAR(50)) + ':' + CAST(Cnt AS VARCHAR), ', ')
    FROM (SELECT TransferReason, COUNT(*) AS Cnt FROM dbo.EmployeeStoreAssignments GROUP BY TransferReason) x;
    INSERT INTO #R VALUES ('ESA-Bridge', 'TransferReason distribution',
        'Distribution of TransferReason values across all ESA rows',
        'INFO', ISNULL(@tr_info, '(empty)'));

    -- ########################################################################
    -- SALES
    -- ########################################################################

    IF @has_sales = 1 OR @has_header = 1
    BEGIN

    -- No managers in sales
    DECLARE @mgr_sales INT;
    IF @has_sales = 1
        SELECT @mgr_sales = COUNT(DISTINCT f.EmployeeKey) FROM dbo.Sales f
        JOIN dbo.Employees e ON e.EmployeeKey = f.EmployeeKey
        WHERE e.Title = 'Store Manager';
    ELSE
        SELECT @mgr_sales = COUNT(DISTINCT f.EmployeeKey) FROM dbo.OrderHeader f
        JOIN dbo.Employees e ON e.EmployeeKey = f.EmployeeKey
        WHERE e.Title = 'Store Manager';
    INSERT INTO #R VALUES ('Sales', 'No managers in sales',
        'Employees with Title=Store Manager should not appear as EmployeeKey in sales',
        CASE WHEN @mgr_sales = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@mgr_sales AS VARCHAR) + ' manager keys found');

    -- No unassigned salesperson keys
    DECLARE @unassigned INT, @total_sales INT;
    IF @has_sales = 1
    BEGIN
        SELECT @unassigned  = COUNT(*) FROM dbo.Sales WHERE EmployeeKey <= 0;
        SELECT @total_sales = COUNT(*) FROM dbo.Sales;
    END
    ELSE
    BEGIN
        SELECT @unassigned  = COUNT(*) FROM dbo.OrderHeader WHERE EmployeeKey <= 0;
        SELECT @total_sales = COUNT(*) FROM dbo.OrderHeader;
    END
    INSERT INTO #R VALUES ('Sales', 'No unassigned salesperson keys',
        'EmployeeKey should be > 0 (not -1 or 0)',
        CASE WHEN @unassigned = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@unassigned AS VARCHAR) + ' of ' + CAST(@total_sales AS VARCHAR) + ' unassigned');

    -- All salesperson keys exist in Employees
    DECLARE @bad_sp INT;
    IF @has_sales = 1
        SELECT @bad_sp = COUNT(DISTINCT f.EmployeeKey)
        FROM dbo.Sales f LEFT JOIN dbo.Employees e ON e.EmployeeKey = f.EmployeeKey
        WHERE f.EmployeeKey > 0 AND e.EmployeeKey IS NULL;
    ELSE
        SELECT @bad_sp = COUNT(DISTINCT f.EmployeeKey)
        FROM dbo.OrderHeader f LEFT JOIN dbo.Employees e ON e.EmployeeKey = f.EmployeeKey
        WHERE f.EmployeeKey > 0 AND e.EmployeeKey IS NULL;
    INSERT INTO #R VALUES ('Sales', 'All salesperson keys exist in Employees',
        'EmployeeKey in sales references a valid Employees row',
        CASE WHEN @bad_sp = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@bad_sp AS VARCHAR) + ' invalid keys');

    -- Orphaned sales (employee+store+date not in ESA window)
    DECLARE @orphaned INT;
    IF @has_sales = 1
        SELECT @orphaned = COUNT(DISTINCT CAST(f.EmployeeKey AS VARCHAR) + '-' + CAST(f.StoreKey AS VARCHAR))
        FROM dbo.Sales f
        LEFT JOIN dbo.EmployeeStoreAssignments esa
          ON  esa.EmployeeKey = f.EmployeeKey
         AND esa.StoreKey     = f.StoreKey
         AND f.OrderDate     >= esa.StartDate
         AND (esa.EndDate IS NULL OR f.OrderDate <= esa.EndDate)
        WHERE f.EmployeeKey > 0 AND esa.EmployeeKey IS NULL;
    ELSE
        SELECT @orphaned = COUNT(DISTINCT CAST(f.EmployeeKey AS VARCHAR) + '-' + CAST(f.StoreKey AS VARCHAR))
        FROM dbo.OrderHeader f
        LEFT JOIN dbo.EmployeeStoreAssignments esa
          ON  esa.EmployeeKey = f.EmployeeKey
         AND esa.StoreKey     = f.StoreKey
         AND f.OrderDate     >= esa.StartDate
         AND (esa.EndDate IS NULL OR f.OrderDate <= esa.EndDate)
        WHERE f.EmployeeKey > 0 AND esa.EmployeeKey IS NULL;
    INSERT INTO #R VALUES ('Sales', 'No orphaned sales (employee+store+date)',
        'Every sale with a salesperson has a matching effective-dated ESA assignment',
        CASE WHEN @orphaned = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@orphaned AS VARCHAR) + ' orphaned combos');

    -- No sales during renovation
    IF OBJECT_ID(N'dbo.Stores', N'U') IS NOT NULL
       AND COL_LENGTH('dbo.Stores', 'RenovationStartDate') IS NOT NULL
    BEGIN
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
            'No sales rows should fall within a store renovation window',
            CASE WHEN @reno_sales = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@reno_sales AS VARCHAR) + ' violation(s)');
    END

    -- No sales after store closure
    IF OBJECT_ID(N'dbo.Stores', N'U') IS NOT NULL
    BEGIN
        DECLARE @post_close_sales INT;
        IF @has_sales = 1
            SELECT @post_close_sales = COUNT(*) FROM dbo.Sales f
            JOIN dbo.Stores s ON s.StoreKey = f.StoreKey
            WHERE s.ClosingDate IS NOT NULL AND f.OrderDate >= s.ClosingDate;
        ELSE
            SELECT @post_close_sales = COUNT(*) FROM dbo.OrderHeader f
            JOIN dbo.Stores s ON s.StoreKey = f.StoreKey
            WHERE s.ClosingDate IS NOT NULL AND f.OrderDate >= s.ClosingDate;
        INSERT INTO #R VALUES ('Sales', 'No sales after store closure',
            'No sales rows should fall on or after a store ClosingDate',
            CASE WHEN @post_close_sales = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@post_close_sales AS VARCHAR) + ' violation(s)');
    END

    -- No post-transfer sales leakage
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
        'No sales should occur after the last ESA EndDate for that employee+store pair',
        CASE WHEN @leaked = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@leaked AS VARCHAR) + ' sale(s) after last ESA EndDate');

-- Check both attribution sources without copying transaction tables to tempdb.
-- Dataset-end status flags must not filter historical transactions.
DECLARE @invalid_sales BIGINT = 0, @invalid_header BIGINT = 0, @ineligible BIGINT;
IF @has_sales = 1
    SELECT @invalid_sales = COUNT_BIG(*) FROM dbo.Sales f
WHERE NOT EXISTS (
    SELECT 1
    FROM dbo.EmployeeStoreAssignments esa
    JOIN dbo.Employees e ON e.EmployeeKey = esa.EmployeeKey
    JOIN dbo.Stores s ON s.StoreKey = esa.StoreKey
    WHERE esa.EmployeeKey = f.EmployeeKey AND esa.StoreKey = f.StoreKey
      AND f.EmployeeKey > 0
      AND e.IsSalesperson = 1 AND e.Title <> 'Store Manager'
      -- IsSalesperson is generated using the configured primary role plus online reps.
      AND esa.RoleAtStore = e.Title
      AND ((esa.RoleAtStore = 'Online Sales Representative' AND s.StoreType = 'Online')
        OR (esa.RoleAtStore <> 'Online Sales Representative' AND s.StoreType <> 'Online'))
      AND f.OrderDate >= e.HireDate
      AND (e.TerminationDate IS NULL OR f.OrderDate <= e.TerminationDate)
      AND f.OrderDate >= esa.StartDate AND f.OrderDate <= esa.EndDate
      AND f.OrderDate >= s.OpeningDate
      AND (s.ClosingDate IS NULL OR f.OrderDate < s.ClosingDate)
      AND NOT (s.RenovationStartDate IS NOT NULL AND s.RenovationEndDate IS NOT NULL
               AND f.OrderDate >= s.RenovationStartDate AND f.OrderDate < s.RenovationEndDate)
);

IF @has_header = 1
    SELECT @invalid_header = COUNT_BIG(*) FROM dbo.OrderHeader f
WHERE NOT EXISTS (
    SELECT 1
    FROM dbo.EmployeeStoreAssignments esa
    JOIN dbo.Employees e ON e.EmployeeKey = esa.EmployeeKey
    JOIN dbo.Stores s ON s.StoreKey = esa.StoreKey
    WHERE esa.EmployeeKey = f.EmployeeKey AND esa.StoreKey = f.StoreKey
      AND f.EmployeeKey > 0
      AND e.IsSalesperson = 1 AND e.Title <> 'Store Manager'
      -- IsSalesperson is generated using the configured primary role plus online reps.
      AND esa.RoleAtStore = e.Title
      AND ((esa.RoleAtStore = 'Online Sales Representative' AND s.StoreType = 'Online')
        OR (esa.RoleAtStore <> 'Online Sales Representative' AND s.StoreType <> 'Online'))
      AND f.OrderDate >= e.HireDate
      AND (e.TerminationDate IS NULL OR f.OrderDate <= e.TerminationDate)
      AND f.OrderDate >= esa.StartDate AND f.OrderDate <= esa.EndDate
      AND f.OrderDate >= s.OpeningDate
      AND (s.ClosingDate IS NULL OR f.OrderDate < s.ClosingDate)
      AND NOT (s.RenovationStartDate IS NOT NULL AND s.RenovationEndDate IS NOT NULL
               AND f.OrderDate >= s.RenovationStartDate AND f.OrderDate < s.RenovationEndDate)
);

SET @ineligible = @invalid_sales + @invalid_header;
INSERT INTO #R VALUES ('Sales', 'Salesperson eligible on OrderDate',
    'Role, employment, store operation and assignment dates match in both attribution sources',
    CASE WHEN @ineligible = 0 THEN 'PASS' ELSE 'FAIL' END,
    CAST(@ineligible AS VARCHAR) + ' invalid row(s) across Sales and OrderHeader');

    END -- @has_sales = 1 OR @has_header = 1

    SELECT Category, [Check], Description, Result, ActualValue FROM #R;
    DROP TABLE #R;
END;
GO
