SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

-- ============================================================================
-- verify.Customers
--
-- General customer quality + SCD2 checks (when active).
-- For detailed investigation, see scripts/sql/verification/verify_scd2_customers.sql
--                                        scripts/sql/verification/verify_dimension_quality.sql
-- ============================================================================

CREATE OR ALTER PROCEDURE [verify].[Customers]
AS
BEGIN
    SET NOCOUNT ON;

    IF OBJECT_ID(N'dbo.Customers', N'U') IS NULL
        RETURN;

    DECLARE @has_scd2 BIT = 0;
    IF COL_LENGTH('dbo.Customers', 'VersionNumber') IS NOT NULL
    BEGIN
        IF EXISTS (SELECT 1 FROM dbo.Customers WHERE VersionNumber > 1)
            SET @has_scd2 = 1;
    END

    CREATE TABLE #R (
        Category    VARCHAR(50)  NOT NULL,
        [Check]     VARCHAR(200) NOT NULL,
        Description VARCHAR(500) NOT NULL,
        Result      VARCHAR(10)  NOT NULL,
        ActualValue VARCHAR(100) NOT NULL
    );

    -- ================================================================
    -- GENERAL CHECKS
    -- ================================================================

    -- Business key uniqueness (IsCurrent=1)
    DECLARE @dup_current INT;
    SELECT @dup_current = COUNT(*) FROM (
        SELECT CustomerID FROM dbo.Customers WHERE IsCurrent = 1
        GROUP BY CustomerID HAVING COUNT(*) > 1
    ) x;
    INSERT INTO #R VALUES ('Uniqueness', 'IsCurrent unique per CustomerID',
        'No duplicate IsCurrent=1 rows for same CustomerID',
        CASE WHEN @dup_current = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@dup_current AS VARCHAR) + ' duplicates');

    -- CustomerType domain
    DECLARE @bad_type INT;
    SELECT @bad_type = COUNT(*) FROM dbo.Customers
    WHERE CustomerType NOT IN ('Individual', 'Organization');
    INSERT INTO #R VALUES ('Domain', 'Valid CustomerType',
        'Must be Individual or Organization',
        CASE WHEN @bad_type = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@bad_type AS VARCHAR) + ' invalid');

    -- Date sanity: StartDate <= EndDate
    DECLARE @date_inv INT;
    SELECT @date_inv = COUNT(*) FROM dbo.Customers
    WHERE CustomerEndDate IS NOT NULL AND CustomerStartDate > CustomerEndDate;
    INSERT INTO #R VALUES ('DateSanity', 'StartDate <= EndDate',
        'Churned customers must have StartDate before EndDate',
        CASE WHEN @date_inv = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@date_inv AS VARCHAR) + ' inversions');

    -- GenderCode domain (single-char codes: M = Male, F = Female, O = Organization).
    -- Gender itself is the readable label (Male/Female/Org); codes live in GenderCode.
    DECLARE @bad_gender INT;
    SELECT @bad_gender = COUNT(*) FROM dbo.Customers
    WHERE GenderCode NOT IN ('M', 'F', 'O');
    INSERT INTO #R VALUES ('Domain', 'Valid GenderCode',
        'Must be M, F, or O',
        CASE WHEN @bad_gender = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@bad_gender AS VARCHAR) + ' invalid');

    -- MaritalStatus domain (individuals)
    DECLARE @bad_marital INT;
    SELECT @bad_marital = COUNT(*) FROM dbo.Customers
    WHERE CustomerType = 'Individual'
      AND MaritalStatus NOT IN ('Single', 'Married', 'Divorced', 'Widowed');
    INSERT INTO #R VALUES ('Domain', 'Valid MaritalStatus',
        'Individuals must be Single/Married/Divorced/Widowed',
        CASE WHEN @bad_marital = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@bad_marital AS VARCHAR) + ' invalid');

    -- Orgs should have NULL demographics
    DECLARE @org_demo INT;
    SELECT @org_demo = COUNT(*) FROM dbo.Customers
    WHERE CustomerType = 'Organization'
      AND (MaritalStatus IS NOT NULL OR YearlyIncome IS NOT NULL OR NumberOfChildren IS NOT NULL);
    INSERT INTO #R VALUES ('Domain', 'Orgs have NULL demographics',
        'Organization customers should not have individual demographics',
        CASE WHEN @org_demo = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@org_demo AS VARCHAR) + ' orgs with demographics');

    -- CustomerProfile covers all current persons (orgs have OrganizationProfile)
    IF OBJECT_ID(N'dbo.CustomerProfile', N'U') IS NOT NULL
    BEGIN
        DECLARE @missing_cp INT;
        SELECT @missing_cp = COUNT(*) FROM dbo.Customers c
        LEFT JOIN dbo.CustomerProfile cp ON cp.CustomerKey = c.CustomerKey
        WHERE c.IsCurrent = 1 AND c.CustomerType != 'Organization' AND cp.CustomerKey IS NULL;
        INSERT INTO #R VALUES ('Referential', 'CustomerProfile covers all current persons',
            'Every IsCurrent=1 non-org customer must have a CustomerProfile row',
            CASE WHEN @missing_cp = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@missing_cp AS VARCHAR) + ' missing');
    END

    -- OrgProfile only for orgs
    IF OBJECT_ID(N'dbo.OrganizationProfile', N'U') IS NOT NULL
    BEGIN
        DECLARE @op_wrong INT;
        SELECT @op_wrong = COUNT(*) FROM dbo.OrganizationProfile op
        JOIN dbo.Customers c ON c.CustomerKey = op.CustomerKey
        WHERE c.CustomerType <> 'Organization';
        INSERT INTO #R VALUES ('Referential', 'OrgProfile only for org customers',
            'OrganizationProfile must only link to Organization-type customers',
            CASE WHEN @op_wrong = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@op_wrong AS VARCHAR) + ' mislinked');

        -- Every org has OrgProfile
        DECLARE @org_missing INT;
        SELECT @org_missing = COUNT(*) FROM dbo.Customers c
        LEFT JOIN dbo.OrganizationProfile op ON op.CustomerKey = c.CustomerKey
        WHERE c.IsCurrent = 1 AND c.CustomerType = 'Organization' AND op.CustomerKey IS NULL;
        INSERT INTO #R VALUES ('Referential', 'Every org has OrgProfile',
            'Every IsCurrent=1 org customer must have an OrganizationProfile',
            CASE WHEN @org_missing = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@org_missing AS VARCHAR) + ' missing');
    END

    -- Geography FK
    DECLARE @bad_geo INT;
    SELECT @bad_geo = COUNT(DISTINCT c.GeographyKey)
    FROM dbo.Customers c LEFT JOIN dbo.Geography g ON g.GeographyKey = c.GeographyKey
    WHERE g.GeographyKey IS NULL;
    INSERT INTO #R VALUES ('Referential', 'Valid GeographyKey',
        'Every customer GeographyKey must exist in Geography',
        CASE WHEN @bad_geo = 0 THEN 'PASS' ELSE 'FAIL' END,
        CAST(@bad_geo AS VARCHAR) + ' orphaned keys');

    -- LoyaltyTier FK
    IF OBJECT_ID(N'dbo.LoyaltyTiers', N'U') IS NOT NULL
    BEGIN
        DECLARE @bad_lt INT;
        SELECT @bad_lt = COUNT(DISTINCT c.LoyaltyTierKey)
        FROM dbo.Customers c LEFT JOIN dbo.LoyaltyTiers lt ON lt.LoyaltyTierKey = c.LoyaltyTierKey
        WHERE lt.LoyaltyTierKey IS NULL AND c.LoyaltyTierKey IS NOT NULL;
        INSERT INTO #R VALUES ('Referential', 'Valid LoyaltyTierKey',
            'Every LoyaltyTierKey must exist in LoyaltyTiers',
            CASE WHEN @bad_lt = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@bad_lt AS VARCHAR) + ' orphaned keys');
    END

    -- INFO: row counts & distribution
    DECLARE @total INT, @indiv INT, @org INT;
    SELECT @total = COUNT(*),
           @indiv = SUM(CASE WHEN CustomerType = 'Individual' THEN 1 ELSE 0 END),
           @org   = SUM(CASE WHEN CustomerType = 'Organization' THEN 1 ELSE 0 END)
    FROM dbo.Customers WHERE IsCurrent = 1;
    INSERT INTO #R VALUES ('Info', 'Customer count',
        'Total IsCurrent=1 customers (Individual / Organization)',
        'INFO', CAST(@total AS VARCHAR) + ' (' + CAST(@indiv AS VARCHAR) + ' / ' + CAST(@org AS VARCHAR) + ')');

    -- ================================================================
    -- SCD2 CHECKS (only when active)
    -- ================================================================
    IF @has_scd2 = 1
    BEGIN
        -- Date chain gaps
        DECLARE @chain_gaps INT;
        SELECT @chain_gaps = COUNT(*) FROM dbo.Customers c1
        JOIN dbo.Customers c2
          ON c2.CustomerID = c1.CustomerID AND c2.VersionNumber = c1.VersionNumber + 1
        WHERE DATEDIFF(DAY, c1.EffectiveEndDate, c2.EffectiveStartDate) <> 1;
        INSERT INTO #R VALUES ('SCD2', 'No date chain gaps',
            'Each version EndDate+1 day = next version StartDate',
            CASE WHEN @chain_gaps = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@chain_gaps AS VARCHAR) + ' gaps');

        -- Orgs should not have versions
        DECLARE @org_ver INT;
        SELECT @org_ver = COUNT(*) FROM (
            SELECT CustomerID FROM dbo.Customers WHERE CustomerType = 'Organization'
            GROUP BY CustomerID HAVING COUNT(*) > 1
        ) x;
        INSERT INTO #R VALUES ('SCD2', 'Orgs have no versions',
            'Organization customers should stay at version 1',
            CASE WHEN @org_ver = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@org_ver AS VARCHAR) + ' orgs with versions');

        -- Children never decrease
        DECLARE @kids_dec INT;
        SELECT @kids_dec = COUNT(*) FROM dbo.Customers c1
        JOIN dbo.Customers c2
          ON c2.CustomerID = c1.CustomerID AND c2.VersionNumber = c1.VersionNumber + 1
        WHERE c2.NumberOfChildren < c1.NumberOfChildren;
        INSERT INTO #R VALUES ('SCD2', 'Children never decrease',
            'NumberOfChildren should only increase across versions',
            CASE WHEN @kids_dec = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@kids_dec AS VARCHAR) + ' decreases');

        -- Version distribution (INFO)
        DECLARE @v1 INT, @v2plus INT;
        SELECT @v1 = SUM(CASE WHEN vc = 1 THEN 1 ELSE 0 END),
               @v2plus = SUM(CASE WHEN vc > 1 THEN 1 ELSE 0 END)
        FROM (SELECT CustomerID, COUNT(*) AS vc FROM dbo.Customers GROUP BY CustomerID) x;
        INSERT INTO #R VALUES ('SCD2', 'Version distribution',
            'Customers with 1 version vs 2+ versions',
            'INFO', CAST(@v1 AS VARCHAR) + ' unchanged / ' + CAST(@v2plus AS VARCHAR) + ' with history');
    END

    SELECT Category, [Check], Description, Result, ActualValue FROM #R;
    DROP TABLE #R;
END;
GO
