SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

CREATE OR ALTER PROCEDURE [verify].[Stores]
AS
BEGIN
    SET NOCOUNT ON;

    IF OBJECT_ID(N'dbo.Stores', N'U') IS NULL
        RETURN;

    CREATE TABLE #R (
        Category    VARCHAR(50)  NOT NULL,
        [Check]     VARCHAR(200) NOT NULL,
        Description VARCHAR(500) NOT NULL,
        Result      VARCHAR(10)  NOT NULL,
        ActualValue VARCHAR(100) NOT NULL
    );

    -- StoreType domain
    DECLARE @bad_type INT;
    SELECT @bad_type = COUNT(*) FROM dbo.Stores
    WHERE StoreType NOT IN ('Supermarket', 'Convenience', 'Online', 'Hypermarket', 'Fulfillment');
    INSERT INTO #R VALUES ('Domain', 'Valid StoreType',
        'Must be Supermarket/Convenience/Online/Hypermarket/Fulfillment',
        CASE WHEN @bad_type = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@bad_type AS VARCHAR) + ' invalid');

    -- Online StoreKeys have StoreType=Online
    DECLARE @bad_online INT;
    SELECT @bad_online = COUNT(*) FROM dbo.Stores
    WHERE StoreKey >= 10000 AND StoreType <> 'Online';
    INSERT INTO #R VALUES ('Domain', 'Online StoreKeys have StoreType=Online',
        'Stores with StoreKey >= 10000 must have StoreType = Online',
        CASE WHEN @bad_online = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@bad_online AS VARCHAR) + ' invalid');

    -- RevenueClass domain
    DECLARE @bad_rc INT;
    SELECT @bad_rc = COUNT(*) FROM dbo.Stores WHERE RevenueClass NOT IN ('A', 'B', 'C');
    INSERT INTO #R VALUES ('Domain', 'Valid RevenueClass',
        'Must be A, B, or C',
        CASE WHEN @bad_rc = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@bad_rc AS VARCHAR) + ' invalid');

    -- Open stores have employees
    DECLARE @no_staff INT;
    SELECT @no_staff = COUNT(*) FROM dbo.Stores WHERE Status = 'Open' AND EmployeeCount <= 0;
    INSERT INTO #R VALUES ('Domain', 'Open stores have employees',
        'Open stores must have EmployeeCount > 0',
        CASE WHEN @no_staff = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@no_staff AS VARCHAR) + ' open stores with 0 staff');

    -- ClosingDate implies Status=Closed
    DECLARE @close_status INT;
    SELECT @close_status = COUNT(*) FROM dbo.Stores
    WHERE ClosingDate IS NOT NULL AND Status <> 'Closed';
    INSERT INTO #R VALUES ('Domain', 'ClosingDate implies Status=Closed',
        'Any store with a ClosingDate must have Status = Closed',
        CASE WHEN @close_status = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@close_status AS VARCHAR) + ' store(s) with ClosingDate but Status <> Closed');

    -- OpeningDate <= ClosingDate
    DECLARE @date_inv INT;
    SELECT @date_inv = COUNT(*) FROM dbo.Stores
    WHERE ClosingDate IS NOT NULL AND OpeningDate > ClosingDate;
    INSERT INTO #R VALUES ('DateSanity', 'OpeningDate <= ClosingDate',
        'Store cannot close before it opens',
        CASE WHEN @date_inv = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@date_inv AS VARCHAR) + ' inversions');

    -- Renovation date sanity (only if column exists)
    IF COL_LENGTH('dbo.Stores', 'RenovationStartDate') IS NOT NULL
    BEGIN
        DECLARE @n_reno INT;
        SELECT @n_reno = COUNT(*) FROM dbo.Stores
        WHERE RenovationStartDate IS NOT NULL AND RenovationEndDate IS NOT NULL;
        INSERT INTO #R VALUES ('Info', 'Renovation count',
            'Stores with both RenovationStartDate and RenovationEndDate populated',
            'INFO', CAST(@n_reno AS VARCHAR) + ' store(s) with renovation dates');

        DECLARE @reno_inv INT;
        SELECT @reno_inv = COUNT(*) FROM dbo.Stores
        WHERE RenovationStartDate IS NOT NULL AND RenovationEndDate IS NOT NULL
          AND RenovationEndDate < RenovationStartDate;
        INSERT INTO #R VALUES ('DateSanity', 'RenovationEnd >= RenovationStart',
            'Renovation cannot end before it starts',
            CASE WHEN @reno_inv = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@reno_inv AS VARCHAR) + ' violation(s)');

        DECLARE @reno_before_open INT;
        SELECT @reno_before_open = COUNT(*) FROM dbo.Stores
        WHERE RenovationStartDate IS NOT NULL AND RenovationStartDate < OpeningDate;
        INSERT INTO #R VALUES ('DateSanity', 'RenovationStart >= OpeningDate',
            'Renovation cannot start before the store opened',
            CASE WHEN @reno_before_open = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@reno_before_open AS VARCHAR) + ' violation(s)');
    END

    -- EmployeeCount matches actual
    IF OBJECT_ID(N'dbo.Employees', N'U') IS NOT NULL
    BEGIN
        DECLARE @count_mismatch INT;
        SELECT @count_mismatch = COUNT(*) FROM dbo.Stores s
        LEFT JOIN (
            SELECT HomeStoreKey, COUNT(*) AS Cnt FROM dbo.Employees
            WHERE HomeStoreKey IS NOT NULL AND HomeStoreKey > 0 AND IsActive = 1
            GROUP BY HomeStoreKey
        ) e ON e.HomeStoreKey = s.StoreKey
        WHERE s.Status = 'Open'
          AND ABS(s.EmployeeCount - ISNULL(e.Cnt, 0)) > 0;
        INSERT INTO #R VALUES ('Referential', 'EmployeeCount matches actual',
            'Stores.EmployeeCount must match count of employee rows',
            CASE WHEN @count_mismatch = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@count_mismatch AS VARCHAR) + ' mismatched');

        -- Closed stores have no active current assignments. HomeStoreKey is an
        -- immutable origin attribute, so current location comes from the bridge.
        IF OBJECT_ID(N'dbo.EmployeeStoreAssignments', N'U') IS NOT NULL
        BEGIN
            DECLARE @closed_active INT;
            SELECT @closed_active = COUNT(DISTINCT s.StoreKey)
            FROM dbo.Stores s
            JOIN dbo.EmployeeStoreAssignments esa
              ON esa.StoreKey = s.StoreKey
             AND esa.IsCurrentAssignment = 1
             AND esa.AssignmentStatus = 'Active'
            JOIN dbo.Employees e
              ON e.EmployeeKey = esa.EmployeeKey
             AND e.IsActive = 1
            WHERE s.Status = 'Closed';
            INSERT INTO #R VALUES ('Referential', 'Closed stores have no active staff',
                'No active current assignment should target a closed store',
                CASE WHEN @closed_active = 0 THEN 'PASS' ELSE 'FAIL' END,
                CAST(@closed_active AS VARCHAR) + ' closed stores with active staff');
        END
    END

    -- Geography FK
    DECLARE @bad_geo INT;
    SELECT @bad_geo = COUNT(DISTINCT s.GeographyKey)
    FROM dbo.Stores s LEFT JOIN dbo.Geography g ON g.GeographyKey = s.GeographyKey
    WHERE g.GeographyKey IS NULL;
    INSERT INTO #R VALUES ('Referential', 'Valid GeographyKey',
        'Every store GeographyKey must exist in Geography',
        CASE WHEN @bad_geo = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@bad_geo AS VARCHAR) + ' orphaned keys');

    -- StoreNumber uniqueness
    DECLARE @dup_sn INT;
    SELECT @dup_sn = COUNT(*) FROM (
        SELECT StoreNumber FROM dbo.Stores GROUP BY StoreNumber HAVING COUNT(*) > 1
    ) x;
    INSERT INTO #R VALUES ('Uniqueness', 'Unique StoreNumber',
        'StoreNumber should be unique across all stores',
        CASE WHEN @dup_sn = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@dup_sn AS VARCHAR) + ' duplicates');

    -- INFO: status distribution
    DECLARE @open INT, @closed INT, @other INT;
    SELECT @open = SUM(CASE WHEN Status = 'Open' THEN 1 ELSE 0 END),
           @closed = SUM(CASE WHEN Status = 'Closed' THEN 1 ELSE 0 END),
           @other = SUM(CASE WHEN Status NOT IN ('Open', 'Closed') THEN 1 ELSE 0 END)
    FROM dbo.Stores;
    INSERT INTO #R VALUES ('Info', 'Store status distribution',
        'Open / Closed / Other',
        'INFO', CAST(@open AS VARCHAR) + ' open / ' + CAST(@closed AS VARCHAR) + ' closed / ' + CAST(@other AS VARCHAR) + ' other');

    SELECT Category, [Check], Description, Result, ActualValue FROM #R;
    DROP TABLE #R;
END;
GO
