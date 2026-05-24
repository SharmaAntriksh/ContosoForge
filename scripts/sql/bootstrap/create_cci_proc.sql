/*
DBA utility procedure: CREATE, DROP, REBUILD, or report STATUS of clustered
columnstore indexes on user tables in the current database.

Targets all user tables by default, or specific tables via a comma-delimited
list. Names may be bare ("Sales") or schema-qualified ("dbo.Sales"). Unknown
names abort with an error.

Behavior:
  - CREATE skips tables that already have a CCI, and skips (does not abort)
    tables that have a clustered rowstore index — the proc reports them so
    you can convert PKs to NONCLUSTERED separately. Other targets still get
    processed.
  - DROP and REBUILD locate the existing CCI by index type (so user-renamed
    CCIs are handled), skip tables without one, and report what happened.
  - Every per-table operation is wrapped in TRY/CATCH: one failure does not
    abort the rest of the batch.
  - Do not call inside an open transaction (XACT_ABORT would doom it).

Compatibility: SQL Server 2016+ (uses STRING_SPLIT, STRING_AGG).

Install into the target database after import:
    sqlcmd -S server -d MyDB -i create_cci_proc.sql

Usage:
    EXEC [admin].[ManageColumnstoreIndexes] @Help = 1;
*/

IF SCHEMA_ID('admin') IS NULL
    EXEC('CREATE SCHEMA [admin] AUTHORIZATION [dbo];');
GO

CREATE OR ALTER PROCEDURE [admin].[ManageColumnstoreIndexes]
    @Action  varchar(10)   = 'CREATE',   -- 'CREATE' | 'DROP' | 'REBUILD' | 'STATUS'
    @Tables  nvarchar(max) = NULL,       -- comma-delimited; NULL = all user tables
    @Help    bit           = 0
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    -----------------------------------------------------------------
    -- Help / usage
    -----------------------------------------------------------------
    IF @Help = 1
    BEGIN
        PRINT N'=================================================================';
        PRINT N'  [admin].[ManageColumnstoreIndexes]';
        PRINT N'  Create, drop, rebuild, or report status of CLUSTERED';
        PRINT N'  COLUMNSTORE indexes. Schema-agnostic.';
        PRINT N'=================================================================';
        PRINT N'';
        PRINT N'PARAMETERS:';
        PRINT N'  @Action  varchar(10)   = ''CREATE''  (CREATE | DROP | REBUILD | STATUS)';
        PRINT N'  @Tables  nvarchar(max) = NULL       comma-delimited names';
        PRINT N'                                       ("Sales" or "dbo.Sales")';
        PRINT N'                                       NULL = all user tables';
        PRINT N'  @Help    bit           = 0';
        PRINT N'';
        PRINT N'NOTES:';
        PRINT N'  - CREATE skips tables with a clustered rowstore index and reports';
        PRINT N'    them as blocked; the rest of the batch still runs.';
        PRINT N'  - DROP/REBUILD locate the existing CCI by index type, so';
        PRINT N'    user-renamed CCIs are handled correctly.';
        PRINT N'  - Re-CREATE after DROP produces an index named [CCI], even if the';
        PRINT N'    prior index had a different name.';
        PRINT N'  - Per-table TRY/CATCH means one failure does not abort the rest.';
        PRINT N'  - Do not call inside an open transaction (XACT_ABORT would doom it).';
        PRINT N'';
        PRINT N'EXAMPLES:';
        PRINT N'';
        PRINT N'  -- 1. Create CCI on ALL eligible user tables';
        PRINT N'  EXEC [admin].[ManageColumnstoreIndexes];';
        PRINT N'';
        PRINT N'  -- 2. Create CCI on specific tables (schema-qualified ok)';
        PRINT N'  EXEC [admin].[ManageColumnstoreIndexes]';
        PRINT N'      @Tables = N''Sales, dbo.SalesReturn, Complaints'';';
        PRINT N'';
        PRINT N'  -- 3. Drop CCI from specific tables';
        PRINT N'  EXEC [admin].[ManageColumnstoreIndexes]';
        PRINT N'      @Action = ''DROP'', @Tables = N''Sales, SalesReturn'';';
        PRINT N'';
        PRINT N'  -- 4. Drop ALL CCIs';
        PRINT N'  EXEC [admin].[ManageColumnstoreIndexes] @Action = ''DROP'';';
        PRINT N'';
        PRINT N'  -- 5. Rebuild (recompress + clean deleted rows)';
        PRINT N'  EXEC [admin].[ManageColumnstoreIndexes] @Action = ''REBUILD'';';
        PRINT N'';
        PRINT N'  -- 6. Show which tables have CCI + sizes + which are blocked';
        PRINT N'  EXEC [admin].[ManageColumnstoreIndexes] @Action = ''STATUS'';';
        PRINT N'=================================================================';
        RETURN;
    END;

    -----------------------------------------------------------------
    -- Guard rails
    -----------------------------------------------------------------
    IF @@TRANCOUNT > 0
        THROW 51001, N'Do not call [admin].[ManageColumnstoreIndexes] inside an open transaction. XACT_ABORT is ON; a caught DDL failure would doom the outer transaction.', 1;

    IF @Action NOT IN ('CREATE', 'DROP', 'REBUILD', 'STATUS')
        THROW 51000, N'Invalid @Action. Use CREATE, DROP, REBUILD, or STATUS.', 1;

    -----------------------------------------------------------------
    -- Parse @Tables. Supports "Sales" and "dbo.Sales".
    -----------------------------------------------------------------
    DECLARE @TargetList TABLE (
        schema_name sysname NULL,
        table_name  sysname NOT NULL
    );

    IF @Tables IS NOT NULL
    BEGIN
        INSERT INTO @TargetList (schema_name, table_name)
        SELECT
            NULLIF(PARSENAME(trimmed, 2), N''),
            PARSENAME(trimmed, 1)
        FROM (
            SELECT LTRIM(RTRIM(value)) AS trimmed
            FROM STRING_SPLIT(@Tables, ',')
        ) x
        WHERE trimmed <> N''
          AND PARSENAME(trimmed, 1) IS NOT NULL;
    END;

    -----------------------------------------------------------------
    -- Resolve targets
    -----------------------------------------------------------------
    DECLARE @Targets TABLE (
        schema_name sysname NOT NULL,
        table_name  sysname NOT NULL,
        object_id   int     NOT NULL PRIMARY KEY
    );

    INSERT INTO @Targets (schema_name, table_name, object_id)
    SELECT s.name, t.name, t.object_id
    FROM sys.tables t
    JOIN sys.schemas s ON s.schema_id = t.schema_id
    WHERE t.is_ms_shipped = 0
      AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'admin')
      AND (
          @Tables IS NULL
          OR EXISTS (
              SELECT 1 FROM @TargetList tl
              WHERE tl.table_name = t.name
                AND (tl.schema_name IS NULL OR tl.schema_name = s.name)
          )
      );

    -- Catch typos and schema mismatches early
    IF @Tables IS NOT NULL
    BEGIN
        DECLARE @missing nvarchar(max) = NULL;
        SELECT @missing = STRING_AGG(
            CASE WHEN tl.schema_name IS NULL THEN tl.table_name
                 ELSE tl.schema_name + N'.' + tl.table_name END,
            N', ')
        FROM @TargetList tl
        WHERE NOT EXISTS (
            SELECT 1 FROM @Targets t
            WHERE t.table_name = tl.table_name
              AND (tl.schema_name IS NULL OR t.schema_name = tl.schema_name)
        );

        IF @missing IS NOT NULL
        BEGIN
            -- THROW caps message at 2048 chars; truncate to leave headroom.
            DECLARE @missing_msg nvarchar(2048) =
                LEFT(N'Unknown or non-targetable table(s) in @Tables: ' + @missing, 2000);
            THROW 51002, @missing_msg, 1;
        END;
    END;

    DECLARE @target_count int = (SELECT COUNT(*) FROM @Targets);

    IF @target_count = 0
    BEGIN
        PRINT N'[OK] No user tables in scope. Nothing to do.';
        RETURN;
    END;

    -----------------------------------------------------------------
    -- STATUS
    -----------------------------------------------------------------
    IF @Action = 'STATUS'
    BEGIN
        PRINT CONCAT(N'[STATUS] ', @target_count, N' target table(s)');
        PRINT N'';

        DECLARE @cci_total int = 0, @blocked_total int = 0;
        SELECT @cci_total = COUNT(*)
        FROM @Targets tt
        WHERE EXISTS (SELECT 1 FROM sys.indexes i
                      WHERE i.object_id = tt.object_id AND i.type = 5);

        SELECT @blocked_total = COUNT(*)
        FROM @Targets tt
        WHERE EXISTS (SELECT 1 FROM sys.indexes i
                      WHERE i.object_id = tt.object_id AND i.type = 1);

        PRINT CONCAT(N'  With CCI:                  ', @cci_total);
        PRINT CONCAT(N'  Heap (CCI-eligible):       ', @target_count - @cci_total - @blocked_total);
        PRINT CONCAT(N'  Blocked (clustered PK):    ', @blocked_total);

        IF @cci_total > 0
        BEGIN
            PRINT N'';
            PRINT N'  CCI details (largest first):';

            DECLARE @s_schema sysname, @s_table sysname, @s_name sysname,
                    @s_size_mb decimal(12,1), @s_rows bigint;

            DECLARE cur_status CURSOR LOCAL FAST_FORWARD READ_ONLY FOR
                SELECT tt.schema_name, tt.table_name, i.name,
                       CAST(SUM(ps.used_page_count) * 8.0 / 1024 AS decimal(12,1)),
                       SUM(ps.row_count)
                FROM @Targets tt
                JOIN sys.indexes i ON i.object_id = tt.object_id AND i.type = 5
                JOIN sys.dm_db_partition_stats ps ON ps.object_id = i.object_id AND ps.index_id = i.index_id
                GROUP BY tt.schema_name, tt.table_name, i.name
                ORDER BY SUM(ps.used_page_count) DESC;

            OPEN cur_status;
            FETCH NEXT FROM cur_status INTO @s_schema, @s_table, @s_name, @s_size_mb, @s_rows;
            WHILE @@FETCH_STATUS = 0
            BEGIN
                PRINT CONCAT(N'    ', QUOTENAME(@s_schema), N'.', QUOTENAME(@s_table),
                             N'  ', @s_name,
                             N'  ', @s_size_mb, N' MB',
                             N'  ', @s_rows, N' rows');
                FETCH NEXT FROM cur_status INTO @s_schema, @s_table, @s_name, @s_size_mb, @s_rows;
            END;
            CLOSE cur_status;
            DEALLOCATE cur_status;
        END;

        IF @blocked_total > 0
        BEGIN
            PRINT N'';
            PRINT N'  Blocked tables (convert clustered PK to NONCLUSTERED to enable CCI):';

            DECLARE @b_schema sysname, @b_table sysname;
            DECLARE cur_blocked CURSOR LOCAL FAST_FORWARD READ_ONLY FOR
                SELECT tt.schema_name, tt.table_name
                FROM @Targets tt
                WHERE EXISTS (SELECT 1 FROM sys.indexes i
                              WHERE i.object_id = tt.object_id AND i.type = 1)
                ORDER BY tt.schema_name, tt.table_name;

            OPEN cur_blocked;
            FETCH NEXT FROM cur_blocked INTO @b_schema, @b_table;
            WHILE @@FETCH_STATUS = 0
            BEGIN
                PRINT CONCAT(N'    ', QUOTENAME(@b_schema), N'.', QUOTENAME(@b_table));
                FETCH NEXT FROM cur_blocked INTO @b_schema, @b_table;
            END;
            CLOSE cur_blocked;
            DEALLOCATE cur_blocked;
        END;

        RETURN;
    END;

    PRINT CONCAT(N'[OK] Action=', @Action,
                 N' | Targets=', @target_count,
                 CASE WHEN @Tables IS NOT NULL
                      THEN N' | Tables=' + @Tables
                      ELSE N' | Tables=ALL'
                 END);

    -----------------------------------------------------------------
    -- CREATE / DROP / REBUILD
    --
    -- Cursor SELECT pre-resolves the CCI index name (if any) and the
    -- blocked-by-rowstore flag, so the loop body issues zero per-row
    -- metadata lookups against sys.indexes.
    -----------------------------------------------------------------
    DECLARE @schema sysname, @table sysname,
            @cci_name sysname, @is_blocked bit,
            @obj nvarchar(517), @sql nvarchar(max);
    DECLARE @created int = 0, @dropped int = 0, @rebuilt int = 0,
            @skipped int = 0, @failed int = 0;

    DECLARE cur CURSOR LOCAL FAST_FORWARD READ_ONLY FOR
        SELECT tt.schema_name, tt.table_name,
               cci.name,
               CASE WHEN rs.object_id IS NOT NULL THEN 1 ELSE 0 END
        FROM @Targets tt
        LEFT JOIN sys.indexes cci ON cci.object_id = tt.object_id AND cci.type = 5
        LEFT JOIN sys.indexes rs  ON rs.object_id  = tt.object_id AND rs.type  = 1
        ORDER BY tt.schema_name, tt.table_name;

    OPEN cur;
    FETCH NEXT FROM cur INTO @schema, @table, @cci_name, @is_blocked;

    WHILE @@FETCH_STATUS = 0
    BEGIN
        SET @obj = QUOTENAME(@schema) + N'.' + QUOTENAME(@table);

        IF @Action = 'CREATE'
        BEGIN
            IF @is_blocked = 1
            BEGIN
                SET @skipped += 1;
                PRINT CONCAT(N'  [SKIP]    ', @obj, N' (clustered rowstore present — convert PK to NONCLUSTERED first)');
            END
            ELSE IF @cci_name IS NOT NULL
            BEGIN
                SET @skipped += 1;
                PRINT CONCAT(N'  [SKIP]    ', @obj, N' (CCI already exists)');
            END
            ELSE
            BEGIN
                SET @sql = N'CREATE CLUSTERED COLUMNSTORE INDEX [CCI] ON ' + @obj + N';';
                BEGIN TRY
                    EXEC sys.sp_executesql @sql;
                    SET @created += 1;
                    PRINT CONCAT(N'  [CREATE]  ', @obj);
                END TRY
                BEGIN CATCH
                    SET @failed += 1;
                    PRINT CONCAT(N'  [FAIL]    ', @obj, N' — ', ERROR_MESSAGE());
                END CATCH;
            END;
        END
        ELSE IF @Action = 'DROP'
        BEGIN
            IF @cci_name IS NULL
            BEGIN
                SET @skipped += 1;
                PRINT CONCAT(N'  [SKIP]    ', @obj, N' (no CCI found)');
            END
            ELSE
            BEGIN
                SET @sql = N'DROP INDEX ' + QUOTENAME(@cci_name) + N' ON ' + @obj + N';';
                BEGIN TRY
                    EXEC sys.sp_executesql @sql;
                    SET @dropped += 1;
                    PRINT CONCAT(N'  [DROP]    ', QUOTENAME(@cci_name), N' ON ', @obj);
                END TRY
                BEGIN CATCH
                    SET @failed += 1;
                    PRINT CONCAT(N'  [FAIL]    ', @obj, N' — ', ERROR_MESSAGE());
                END CATCH;
            END;
        END
        ELSE -- REBUILD
        BEGIN
            IF @cci_name IS NULL
            BEGIN
                SET @skipped += 1;
                PRINT CONCAT(N'  [SKIP]    ', @obj, N' (no CCI to rebuild)');
            END
            ELSE
            BEGIN
                SET @sql = N'ALTER INDEX ' + QUOTENAME(@cci_name) + N' ON ' + @obj + N' REBUILD;';
                BEGIN TRY
                    EXEC sys.sp_executesql @sql;
                    SET @rebuilt += 1;
                    PRINT CONCAT(N'  [REBUILD] ', QUOTENAME(@cci_name), N' ON ', @obj);
                END TRY
                BEGIN CATCH
                    SET @failed += 1;
                    PRINT CONCAT(N'  [FAIL]    ', @obj, N' — ', ERROR_MESSAGE());
                END CATCH;
            END;
        END;

        FETCH NEXT FROM cur INTO @schema, @table, @cci_name, @is_blocked;
    END;

    CLOSE cur;
    DEALLOCATE cur;

    -----------------------------------------------------------------
    -- Summary
    -----------------------------------------------------------------
    DECLARE @cci_count int = (
        SELECT COUNT(*)
        FROM sys.indexes i
        WHERE i.type = 5
          AND i.object_id IN (SELECT object_id FROM @Targets)
    );

    PRINT N'';
    IF @Action = 'CREATE'
        PRINT CONCAT(N'[OK] ', @created, N' created, ', @skipped, N' skipped',
                     CASE WHEN @failed > 0 THEN CONCAT(N', ', @failed, N' failed') ELSE N'' END,
                     N' — ', @cci_count, N'/', @target_count, N' target tables now have CCI');
    ELSE IF @Action = 'DROP'
        PRINT CONCAT(N'[OK] ', @dropped, N' dropped, ', @skipped, N' skipped',
                     CASE WHEN @failed > 0 THEN CONCAT(N', ', @failed, N' failed') ELSE N'' END);
    ELSE -- REBUILD
        PRINT CONCAT(N'[OK] ', @rebuilt, N' rebuilt, ', @skipped, N' skipped',
                     CASE WHEN @failed > 0 THEN CONCAT(N', ', @failed, N' failed') ELSE N'' END);
END;
GO
