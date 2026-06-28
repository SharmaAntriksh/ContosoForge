SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

-- ============================================================================
-- verify.CrossDimension
--
-- Checks that remain cross-cutting and don't belong to a single domain proc.
-- Most FK checks have moved to Customers, Products, Stores, SalesRelationships.
-- ============================================================================

CREATE OR ALTER PROCEDURE [verify].[CrossDimension]
AS
BEGIN
    SET NOCOUNT ON;

    CREATE TABLE #R (
        Category    VARCHAR(50)  NOT NULL,
        [Check]     VARCHAR(200) NOT NULL,
        Description VARCHAR(500) NOT NULL,
        Result      VARCHAR(10)  NOT NULL,
        ActualValue VARCHAR(100) NOT NULL
    );

    -- Employee GeographyKey FK (GeographyKey=0 is the corporate sentinel
    -- for top-level employees not assigned to any store/geography)
    IF OBJECT_ID(N'dbo.Employees', N'U') IS NOT NULL
    BEGIN
        DECLARE @bad_emp_geo INT;
        SELECT @bad_emp_geo = COUNT(DISTINCT e.GeographyKey)
        FROM dbo.Employees e LEFT JOIN dbo.Geography g ON g.GeographyKey = e.GeographyKey
        WHERE e.GeographyKey IS NOT NULL AND e.GeographyKey <> 0 AND g.GeographyKey IS NULL;
        INSERT INTO #R VALUES ('Referential', 'Employee: valid GeographyKey',
            'Every non-corporate employee GeographyKey must exist in Geography',
            CASE WHEN @bad_emp_geo = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@bad_emp_geo AS VARCHAR) + ' orphaned keys');
    END

    -- ExchangeRates FromCurrencyKey FK
    IF OBJECT_ID(N'dbo.ExchangeRates', N'U') IS NOT NULL
       AND OBJECT_ID(N'dbo.Currency', N'U') IS NOT NULL
    BEGIN
        DECLARE @bad_fx_from INT;
        SELECT @bad_fx_from = COUNT(DISTINCT er.FromCurrencyKey)
        FROM dbo.ExchangeRates er LEFT JOIN dbo.Currency c ON c.CurrencyKey = er.FromCurrencyKey
        WHERE c.CurrencyKey IS NULL;
        INSERT INTO #R VALUES ('Referential', 'FX FromCurrencyKey exists in Currency',
            'All FromCurrencyKey values in ExchangeRates must exist in Currency',
            CASE WHEN @bad_fx_from = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@bad_fx_from AS VARCHAR) + ' orphaned currencies');
    END

    -- ExchangeRates ToCurrencyKey FK
    IF OBJECT_ID(N'dbo.ExchangeRates', N'U') IS NOT NULL
       AND OBJECT_ID(N'dbo.Currency', N'U') IS NOT NULL
    BEGIN
        DECLARE @bad_fx_to INT;
        SELECT @bad_fx_to = COUNT(DISTINCT er.ToCurrencyKey)
        FROM dbo.ExchangeRates er LEFT JOIN dbo.Currency c ON c.CurrencyKey = er.ToCurrencyKey
        WHERE c.CurrencyKey IS NULL;
        INSERT INTO #R VALUES ('Referential', 'FX ToCurrencyKey exists in Currency',
            'All ToCurrencyKey values in ExchangeRates must exist in Currency',
            CASE WHEN @bad_fx_to = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@bad_fx_to AS VARCHAR) + ' orphaned currencies');
    END

    -- SupplierKey FK (ProductProfile -> Suppliers)
    IF OBJECT_ID(N'dbo.Suppliers', N'U') IS NOT NULL
       AND OBJECT_ID(N'dbo.ProductProfile', N'U') IS NOT NULL
    BEGIN
        DECLARE @bad_sup INT;
        SELECT @bad_sup = COUNT(DISTINCT pp.SupplierKey)
        FROM dbo.ProductProfile pp LEFT JOIN dbo.Suppliers s ON s.SupplierKey = pp.SupplierKey
        WHERE pp.SupplierKey IS NOT NULL AND s.SupplierKey IS NULL;
        INSERT INTO #R VALUES ('Referential', 'ProductProfile: valid SupplierKey',
            'Every SupplierKey in ProductProfile must exist in Suppliers',
            CASE WHEN @bad_sup = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@bad_sup AS VARCHAR) + ' orphaned keys');
    END

    -- Active store employees should have assignments
    IF OBJECT_ID(N'dbo.EmployeeStoreAssignments', N'U') IS NOT NULL
       AND OBJECT_ID(N'dbo.Employees', N'U') IS NOT NULL
    BEGIN
        DECLARE @no_asgn INT;
        SELECT @no_asgn = COUNT(*) FROM dbo.Employees e
        LEFT JOIN dbo.EmployeeStoreAssignments a ON a.EmployeeKey = e.EmployeeKey
        WHERE e.IsActive = 1 AND e.StoreKey > 0 AND a.EmployeeKey IS NULL;
        INSERT INTO #R VALUES ('Referential', 'Active employees have assignments',
            'Every active store-level employee should have at least one assignment',
            CASE WHEN @no_asgn = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@no_asgn AS VARCHAR) + ' without assignments');
    END

    -- SalesPerson has IsSalesperson
    IF OBJECT_ID(N'dbo.Sales', N'U') IS NOT NULL
       AND OBJECT_ID(N'dbo.Employees', N'U') IS NOT NULL
    BEGIN
        DECLARE @no_flag INT;
        SELECT @no_flag = COUNT(DISTINCT f.EmployeeKey)
        FROM dbo.Sales f JOIN dbo.Employees e ON e.EmployeeKey = f.EmployeeKey
        WHERE e.IsSalesperson = 0;
        INSERT INTO #R VALUES ('Domain', 'SalesPerson has IsSalesperson=1',
            'Employees appearing in Sales should have IsSalesperson set',
            CASE WHEN @no_flag = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@no_flag AS VARCHAR) + ' without flag');
    END

    -- AcquisitionChannel FK
    IF OBJECT_ID(N'dbo.CustomerAcquisitionChannels', N'U') IS NOT NULL
    BEGIN
        DECLARE @bad_acq INT;
        SELECT @bad_acq = COUNT(DISTINCT c.CustomerAcquisitionChannelKey)
        FROM dbo.Customers c
        LEFT JOIN dbo.CustomerAcquisitionChannels cac
          ON cac.CustomerAcquisitionChannelKey = c.CustomerAcquisitionChannelKey
        WHERE cac.CustomerAcquisitionChannelKey IS NULL
          AND c.CustomerAcquisitionChannelKey IS NOT NULL;
        INSERT INTO #R VALUES ('Referential', 'Valid AcquisitionChannelKey',
            'Every AcquisitionChannelKey must exist in CustomerAcquisitionChannels',
            CASE WHEN @bad_acq = 0 THEN 'PASS' ELSE 'FAIL' END,
            CAST(@bad_acq AS VARCHAR) + ' orphaned keys');
    END

    SELECT Category, [Check], Description, Result, ActualValue FROM #R;
    DROP TABLE #R;
END;
GO
