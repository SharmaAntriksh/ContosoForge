/*
DBA utility procedure: DROP or RESTORE primary key, unique, foreign key
constraints, and standalone unique indexes on user tables in the current
database.

DROP captures full DDL (including FILLFACTOR, DATA_COMPRESSION, filegroup,
sort direction, NOT FOR REPLICATION, ON DELETE/UPDATE actions, disabled
state, etc.), then drops in dependency order: FKs first, then PKs/UQs, then
standalone unique indexes. Reports space freed per object.

RESTORE re-creates everything from [admin].[_PK_Backup] in inverse order
(PKs/UQs first, then unique indexes, then FKs).

Why also FKs and unique indexes?
  - Dropping a PK while an FK references it fails. So FKs must come first.
  - UQ and standalone unique indexes both create nonclustered indexes that
    force BULK INSERT TABLOCK to escalate from a BU lock to an X lock,
    serializing parallel loads. To avoid surprise serialization, the proc
    handles both. Set @IncludeUniqueIndexes = 0 to skip plain unique indexes.

Targeting:
  - @Tables = NULL targets all user tables.
  - @Tables = N'Sales, dbo.Customers' supports both bare and schema-qualified
    names. Unknown names abort with an error.
  - When @Tables is set, FKs incoming to a target table (e.g. FK_Sales_Customers
    when targeting Customers) are also dropped — otherwise the PK drop fails.

Compatibility: SQL Server 2017+ (uses STRING_SPLIT and STRING_AGG WITHIN GROUP).

Install into the target database after import:
    sqlcmd -S server -d MyDB -i create_pk_proc.sql

Usage:
    EXEC [admin].[ManagePrimaryKeys] @Help = 1;
*/

IF SCHEMA_ID('admin') IS NULL
    EXEC('CREATE SCHEMA [admin] AUTHORIZATION [dbo];');
GO

-- Backup table for FK/PK/UQ/UX definitions (survives across calls).
IF OBJECT_ID('admin._PK_Backup') IS NULL
BEGIN
    CREATE TABLE [admin].[_PK_Backup] (
        id                int IDENTITY(1,1) PRIMARY KEY,
        constraint_type   varchar(2)    NOT NULL,  -- 'FK', 'PK', 'UQ', 'UX' (unique index)
        schema_name       sysname       NOT NULL,
        table_name        sysname       NOT NULL,
        constraint_name   sysname       NOT NULL,
        referenced_schema sysname       NULL,      -- FK only
        referenced_table  sysname       NULL,      -- FK only
        definition_sql    nvarchar(max) NOT NULL,
        dropped_at        datetime2     NOT NULL DEFAULT SYSDATETIME()
    );
END;

-- Forward-compatible upgrade: add referenced_{schema,table} columns if missing.
IF COL_LENGTH('admin._PK_Backup', 'referenced_schema') IS NULL
    EXEC('ALTER TABLE [admin].[_PK_Backup] ADD referenced_schema sysname NULL;');
IF COL_LENGTH('admin._PK_Backup', 'referenced_table') IS NULL
    EXEC('ALTER TABLE [admin].[_PK_Backup] ADD referenced_table sysname NULL;');
GO

-- Shared helper: produces the four "common" DDL pieces for any index, so the
-- PK/UQ and UX cursors don't each reimplement option assembly and don't issue
-- per-row metadata lookups inside their loop. Inline TVF → the optimizer folds
-- it into the calling query.
CREATE OR ALTER FUNCTION [admin].[_fn_IndexDDLPieces](@object_id int, @index_id int)
RETURNS TABLE
AS RETURN
SELECT
    -- Key columns, ordered, with DESC where applicable
    (SELECT STRING_AGG(
            QUOTENAME(COL_NAME(ic.object_id, ic.column_id))
            + CASE WHEN ic.is_descending_key = 1 THEN N' DESC' ELSE N'' END,
            N', ') WITHIN GROUP (ORDER BY ic.key_ordinal)
     FROM sys.index_columns ic
     WHERE ic.object_id = @object_id
       AND ic.index_id  = @index_id
       AND ic.is_included_column = 0
       AND ic.key_ordinal > 0
    ) AS key_cols,

    -- INCLUDE columns (NULL when none)
    (SELECT STRING_AGG(QUOTENAME(COL_NAME(ic.object_id, ic.column_id)), N', ')
            WITHIN GROUP (ORDER BY ic.index_column_id)
     FROM sys.index_columns ic
     WHERE ic.object_id = @object_id
       AND ic.index_id  = @index_id
       AND ic.is_included_column = 1
    ) AS include_cols,

    -- Full WITH (...) clause (empty string if no non-default options)
    (
        SELECT CASE WHEN LEN(o.opts) > 0
                    THEN N' WITH (' + STUFF(o.opts, 1, 2, N'') + N')'
                    ELSE N'' END
        FROM (
            SELECT
                CASE WHEN i.fill_factor > 0      THEN N', FILLFACTOR = ' + CAST(i.fill_factor AS nvarchar(10)) ELSE N'' END
              + CASE WHEN i.is_padded = 1        THEN N', PAD_INDEX = ON'         ELSE N'' END
              + CASE WHEN i.ignore_dup_key = 1   THEN N', IGNORE_DUP_KEY = ON'    ELSE N'' END
              + CASE WHEN i.allow_row_locks = 0  THEN N', ALLOW_ROW_LOCKS = OFF'  ELSE N'' END
              + CASE WHEN i.allow_page_locks = 0 THEN N', ALLOW_PAGE_LOCKS = OFF' ELSE N'' END
              + ISNULL(
                  (SELECT TOP 1
                      CASE WHEN p.data_compression_desc <> N'NONE'
                           THEN N', DATA_COMPRESSION = ' + p.data_compression_desc
                           ELSE N'' END
                   FROM sys.partitions p
                   WHERE p.object_id = @object_id AND p.index_id = @index_id
                   ORDER BY p.partition_number),
                  N'')
                AS opts
            FROM sys.indexes i
            WHERE i.object_id = @object_id AND i.index_id = @index_id
        ) o
    ) AS with_opts,

    -- ON [filegroup]  or  ON [scheme]([partition_col])
    (SELECT N' ON ' + QUOTENAME(ds.name)
          + CASE WHEN ds.type = 'PS'
                 THEN ISNULL(
                          (SELECT N'(' + QUOTENAME(COL_NAME(ic.object_id, ic.column_id)) + N')'
                           FROM sys.index_columns ic
                           WHERE ic.object_id = @object_id
                             AND ic.index_id  = @index_id
                             AND ic.partition_ordinal > 0),
                          N'')
                 ELSE N'' END
     FROM sys.indexes i
     JOIN sys.data_spaces ds ON ds.data_space_id = i.data_space_id
     WHERE i.object_id = @object_id AND i.index_id = @index_id
    ) AS on_clause;
GO

CREATE OR ALTER PROCEDURE [admin].[ManagePrimaryKeys]
    @Action               varchar(10)   = 'DROP',  -- 'DROP' | 'RESTORE' | 'RECREATE' | 'STATUS'
    @Tables               nvarchar(max) = NULL,    -- comma-delimited; NULL = all user tables
    @IncludeUniqueIndexes bit           = 1,       -- also drop standalone UNIQUE INDEXes
    @Help                 bit           = 0
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
        PRINT N'  [admin].[ManagePrimaryKeys]';
        PRINT N'  Drop or restore PRIMARY KEY, UNIQUE, FOREIGN KEY constraints';
        PRINT N'  and standalone UNIQUE INDEXes. Schema-agnostic.';
        PRINT N'=================================================================';
        PRINT N'';
        PRINT N'PARAMETERS:';
        PRINT N'  @Action               varchar(10)   = ''DROP''  (DROP | RESTORE | RECREATE | STATUS)';
        PRINT N'  @Tables               nvarchar(max) = NULL     comma-delimited names';
        PRINT N'                                                  ("Sales" or "dbo.Sales")';
        PRINT N'                                                  NULL = all user tables';
        PRINT N'  @IncludeUniqueIndexes bit           = 1        also drop CREATE UNIQUE INDEX';
        PRINT N'  @Help                 bit           = 0';
        PRINT N'';
        PRINT N'NOTES:';
        PRINT N'  - DROP captures FILLFACTOR, DATA_COMPRESSION, filegroup, sort';
        PRINT N'    direction, ON DELETE/UPDATE, NOT FOR REPLICATION, disabled state.';
        PRINT N'  - When @Tables is set, FKs *referencing* the target tables are also';
        PRINT N'    dropped (otherwise the PK drop would fail with msg 3725).';
        PRINT N'  - Save+drop is per-constraint TRY/CATCH: one failure does not stop';
        PRINT N'    the rest, and failed drops do not leave phantom backup rows.';
        PRINT N'  - RESTORE silently skips constraints that already exist (e.g. after';
        PRINT N'    a previous partial DROP) and cleans the stale backup row.';
        PRINT N'  - Do not call inside an open transaction (XACT_ABORT would doom it).';
        PRINT N'';
        PRINT N'EXAMPLES:';
        PRINT N'';
        PRINT N'  -- 1. Drop everything on all user tables';
        PRINT N'  EXEC [admin].[ManagePrimaryKeys];';
        PRINT N'';
        PRINT N'  -- 2. Drop on specific tables (incoming FKs handled automatically)';
        PRINT N'  EXEC [admin].[ManagePrimaryKeys]';
        PRINT N'      @Tables = N''Sales, dbo.Customers'';';
        PRINT N'';
        PRINT N'  -- 3. Restore everything previously dropped';
        PRINT N'  EXEC [admin].[ManagePrimaryKeys] @Action = ''RESTORE'';';
        PRINT N'';
        PRINT N'  -- 4. Restore only what was dropped for a given table';
        PRINT N'  EXEC [admin].[ManagePrimaryKeys]';
        PRINT N'      @Action = ''RESTORE'', @Tables = N''Customers'';';
        PRINT N'';
        PRINT N'  -- 5. Recreate everything (DROP then RESTORE, with fresh DDL)';
        PRINT N'  EXEC [admin].[ManagePrimaryKeys] @Action = ''RECREATE'';';
        PRINT N'';
        PRINT N'  -- 6. Check current state + index sizes';
        PRINT N'  EXEC [admin].[ManagePrimaryKeys] @Action = ''STATUS'';';
        PRINT N'=================================================================';
        RETURN;
    END;

    -----------------------------------------------------------------
    -- Guard rails
    -----------------------------------------------------------------
    IF @@TRANCOUNT > 0
        THROW 51001, N'Do not call [admin].[ManagePrimaryKeys] inside an open transaction. XACT_ABORT is ON; a caught DROP failure would doom the outer transaction.', 1;

    IF @Action NOT IN ('DROP', 'RESTORE', 'RECREATE', 'STATUS')
        THROW 51000, N'Invalid @Action. Use DROP, RESTORE, RECREATE, or STATUS.', 1;

    IF @Action = 'RECREATE'
    BEGIN
        PRINT N'[OK] Action=RECREATE — DROP phase';
        EXEC [admin].[ManagePrimaryKeys]
            @Action               = 'DROP',
            @Tables               = @Tables,
            @IncludeUniqueIndexes = @IncludeUniqueIndexes;

        PRINT N'';
        PRINT N'[OK] Action=RECREATE — RESTORE phase';
        EXEC [admin].[ManagePrimaryKeys]
            @Action = 'RESTORE',
            @Tables = @Tables;
        RETURN;
    END;

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
    -- Resolve targets (schema + name + object_id)
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

    -----------------------------------------------------------------
    -- STATUS
    -----------------------------------------------------------------
    IF @Action = 'STATUS'
    BEGIN
        PRINT CONCAT(N'[STATUS] ', @target_count, N' target table(s)');
        PRINT N'';

        DECLARE @pk_count int = 0, @uq_count int = 0;
        SELECT
            @pk_count = SUM(CASE WHEN kc.type = 'PK' THEN 1 ELSE 0 END),
            @uq_count = SUM(CASE WHEN kc.type = 'UQ' THEN 1 ELSE 0 END)
        FROM @Targets tt
        JOIN sys.key_constraints kc ON kc.parent_object_id = tt.object_id;

        DECLARE @ux_count int = 0;
        SELECT @ux_count = COUNT(*)
        FROM @Targets tt
        JOIN sys.indexes i ON i.object_id = tt.object_id
        WHERE i.is_unique = 1 AND i.is_unique_constraint = 0
          AND i.is_primary_key = 0 AND i.type IN (1, 2) AND i.name IS NOT NULL;

        PRINT CONCAT(N'  Primary keys:        ', ISNULL(@pk_count, 0));
        PRINT CONCAT(N'  Unique constraints:  ', ISNULL(@uq_count, 0));
        PRINT CONCAT(N'  Unique indexes:      ', ISNULL(@ux_count, 0));

        IF ISNULL(@pk_count, 0) + ISNULL(@uq_count, 0) + ISNULL(@ux_count, 0) > 0
        BEGIN
            DECLARE @s_schema sysname, @s_table sysname, @s_name sysname, @s_ctype varchar(2);
            DECLARE @s_mb decimal(12,1);

            DECLARE cur_status CURSOR LOCAL FAST_FORWARD READ_ONLY FOR
                SELECT tt.schema_name, tt.table_name, kc.name, kc.type,
                       CAST(SUM(ps.used_page_count) * 8.0 / 1024 AS decimal(12,1))
                FROM @Targets tt
                JOIN sys.key_constraints kc ON kc.parent_object_id = tt.object_id AND kc.type IN ('PK', 'UQ')
                JOIN sys.indexes i ON i.object_id = kc.parent_object_id AND i.name = kc.name
                JOIN sys.dm_db_partition_stats ps ON ps.object_id = i.object_id AND ps.index_id = i.index_id
                GROUP BY tt.schema_name, tt.table_name, kc.name, kc.type
                UNION ALL
                SELECT tt.schema_name, tt.table_name, i.name, 'UX',
                       CAST(SUM(ps.used_page_count) * 8.0 / 1024 AS decimal(12,1))
                FROM @Targets tt
                JOIN sys.indexes i ON i.object_id = tt.object_id
                JOIN sys.dm_db_partition_stats ps ON ps.object_id = i.object_id AND ps.index_id = i.index_id
                WHERE i.is_unique = 1 AND i.is_unique_constraint = 0
                  AND i.is_primary_key = 0 AND i.type IN (1, 2) AND i.name IS NOT NULL
                GROUP BY tt.schema_name, tt.table_name, i.name
                ORDER BY 5 DESC;

            OPEN cur_status;
            FETCH NEXT FROM cur_status INTO @s_schema, @s_table, @s_name, @s_ctype, @s_mb;
            WHILE @@FETCH_STATUS = 0
            BEGIN
                PRINT CONCAT(N'    ', @s_ctype, N' ', QUOTENAME(@s_schema), N'.', QUOTENAME(@s_table),
                             N'  ', @s_name, N'  ', @s_mb, N' MB');
                FETCH NEXT FROM cur_status INTO @s_schema, @s_table, @s_name, @s_ctype, @s_mb;
            END;
            CLOSE cur_status;
            DEALLOCATE cur_status;
        END;

        DECLARE @fk_count int = 0;
        SELECT @fk_count = COUNT(*)
        FROM sys.foreign_keys fk
        WHERE EXISTS (SELECT 1 FROM @Targets tt WHERE tt.object_id = fk.parent_object_id)
           OR EXISTS (SELECT 1 FROM @Targets tt WHERE tt.object_id = fk.referenced_object_id);

        PRINT CONCAT(N'  Foreign keys (in or out): ', @fk_count);

        DECLARE @backup_count int = 0;
        SELECT @backup_count = COUNT(*) FROM [admin].[_PK_Backup];
        IF @backup_count > 0
            PRINT CONCAT(N'  Backed-up constraints: ', @backup_count, N' (available for RESTORE)');

        RETURN;
    END;

    -----------------------------------------------------------------
    -- DROP
    --
    -- Order:
    --   1. FKs (incoming or outgoing for any target table)
    --   2. PK / UQ constraints on target tables
    --   3. Standalone UNIQUE INDEXes on target tables (if @IncludeUniqueIndexes)
    --
    -- Each item: save DDL to _PK_Backup, then DROP, wrapped in TRY/CATCH.
    -- On failure the backup row is rolled back, so _PK_Backup never drifts
    -- from real schema state.
    -----------------------------------------------------------------
    IF @Action = 'DROP'
    BEGIN
        PRINT CONCAT(N'[OK] Action=DROP | Targets=', @target_count,
                     CASE WHEN @Tables IS NOT NULL
                          THEN N' | Tables=' + @Tables
                          ELSE N' | Tables=ALL'
                     END);

        -- Clear stale backup rows for in-scope objects (parent OR referenced),
        -- but ONLY for constraints that still exist in the catalog — i.e. the
        -- rows we are about to re-capture below. Backups of constraints already
        -- dropped by an earlier DROP run are left untouched, so running
        -- @Action='DROP' twice can't wipe a live backup and strand a later
        -- RESTORE with an empty table.
        DELETE b FROM [admin].[_PK_Backup] b
        WHERE (
            EXISTS (
                SELECT 1 FROM @Targets tt
                WHERE tt.schema_name = b.schema_name AND tt.table_name = b.table_name
            )
            OR EXISTS (
                SELECT 1 FROM @Targets tt
                WHERE tt.schema_name = b.referenced_schema AND tt.table_name = b.referenced_table
            )
        )
        AND (
            EXISTS (
                SELECT 1 FROM sys.key_constraints kc
                JOIN sys.tables t  ON t.object_id = kc.parent_object_id
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                WHERE kc.name = b.constraint_name AND s.name = b.schema_name
            )
            OR EXISTS (
                SELECT 1 FROM sys.foreign_keys fk
                JOIN sys.tables t  ON t.object_id = fk.parent_object_id
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                WHERE fk.name = b.constraint_name AND s.name = b.schema_name
            )
        );

        -- ============ FK PHASE ============
        DECLARE @fk_dropped int = 0, @fk_failed int = 0;
        DECLARE @fk_object_id int, @fk_schema sysname, @fk_table sysname, @fk_name sysname,
                @fk_ref_schema sysname, @fk_ref_table sysname,
                @fk_on_delete nvarchar(60), @fk_on_update nvarchar(60),
                @fk_is_disabled bit, @fk_is_not_for_replication bit, @fk_is_not_trusted bit;
        DECLARE @fk_cols nvarchar(max), @fk_ref_cols nvarchar(max),
                @fk_def_sql nvarchar(max), @fk_drop_sql nvarchar(max);
        DECLARE @fk_save_id int;

        DECLARE cur_fk CURSOR LOCAL FAST_FORWARD READ_ONLY FOR
            SELECT fk.object_id,
                   s.name, t.name, fk.name,
                   rs.name, rt.name,
                   fk.delete_referential_action_desc,
                   fk.update_referential_action_desc,
                   fk.is_disabled,
                   fk.is_not_for_replication,
                   fk.is_not_trusted
            FROM sys.foreign_keys fk
            JOIN sys.tables t  ON t.object_id  = fk.parent_object_id
            JOIN sys.schemas s ON s.schema_id  = t.schema_id
            JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id
            JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
            WHERE EXISTS (SELECT 1 FROM @Targets tt WHERE tt.object_id = fk.parent_object_id)
               OR EXISTS (SELECT 1 FROM @Targets tt WHERE tt.object_id = fk.referenced_object_id);

        OPEN cur_fk;
        FETCH NEXT FROM cur_fk INTO @fk_object_id, @fk_schema, @fk_table, @fk_name,
                                     @fk_ref_schema, @fk_ref_table,
                                     @fk_on_delete, @fk_on_update,
                                     @fk_is_disabled, @fk_is_not_for_replication, @fk_is_not_trusted;

        WHILE @@FETCH_STATUS = 0
        BEGIN
            SELECT @fk_cols     = STRING_AGG(QUOTENAME(COL_NAME(fkc.parent_object_id, fkc.parent_column_id)), N', ')
                                  WITHIN GROUP (ORDER BY fkc.constraint_column_id),
                   @fk_ref_cols = STRING_AGG(QUOTENAME(COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id)), N', ')
                                  WITHIN GROUP (ORDER BY fkc.constraint_column_id)
            FROM sys.foreign_key_columns fkc
            WHERE fkc.constraint_object_id = @fk_object_id;

            SET @fk_def_sql = N'ALTER TABLE ' + QUOTENAME(@fk_schema) + N'.' + QUOTENAME(@fk_table)
                            + CASE WHEN @fk_is_not_trusted = 1 THEN N' WITH NOCHECK' ELSE N' WITH CHECK' END
                            + N' ADD CONSTRAINT ' + QUOTENAME(@fk_name)
                            + N' FOREIGN KEY (' + @fk_cols + N') REFERENCES '
                            + QUOTENAME(@fk_ref_schema) + N'.' + QUOTENAME(@fk_ref_table)
                            + N' (' + @fk_ref_cols + N')';

            IF @fk_on_delete <> N'NO_ACTION'
                SET @fk_def_sql += N' ON DELETE ' + REPLACE(@fk_on_delete, N'_', N' ');
            IF @fk_on_update <> N'NO_ACTION'
                SET @fk_def_sql += N' ON UPDATE ' + REPLACE(@fk_on_update, N'_', N' ');
            IF @fk_is_not_for_replication = 1
                SET @fk_def_sql += N' NOT FOR REPLICATION';
            SET @fk_def_sql += N';';

            -- Preserve disabled state
            IF @fk_is_disabled = 1
                SET @fk_def_sql += N' ALTER TABLE ' + QUOTENAME(@fk_schema) + N'.' + QUOTENAME(@fk_table)
                                + N' NOCHECK CONSTRAINT ' + QUOTENAME(@fk_name) + N';';

            SET @fk_drop_sql = N'ALTER TABLE ' + QUOTENAME(@fk_schema) + N'.' + QUOTENAME(@fk_table)
                             + N' DROP CONSTRAINT ' + QUOTENAME(@fk_name) + N';';

            SET @fk_save_id = NULL;
            BEGIN TRY
                INSERT INTO [admin].[_PK_Backup]
                    (constraint_type, schema_name, table_name, constraint_name,
                     referenced_schema, referenced_table, definition_sql)
                VALUES ('FK', @fk_schema, @fk_table, @fk_name,
                        @fk_ref_schema, @fk_ref_table, @fk_def_sql);
                SET @fk_save_id = SCOPE_IDENTITY();

                EXEC sys.sp_executesql @fk_drop_sql;
                SET @fk_dropped += 1;
            END TRY
            BEGIN CATCH
                SET @fk_failed += 1;
                IF @fk_save_id IS NOT NULL
                    DELETE FROM [admin].[_PK_Backup] WHERE id = @fk_save_id;
                PRINT CONCAT(N'  [FAIL] FK ', QUOTENAME(@fk_schema), N'.', QUOTENAME(@fk_table),
                             N'  ', @fk_name, N' — ', ERROR_MESSAGE());
            END CATCH;

            FETCH NEXT FROM cur_fk INTO @fk_object_id, @fk_schema, @fk_table, @fk_name,
                                         @fk_ref_schema, @fk_ref_table,
                                         @fk_on_delete, @fk_on_update,
                                         @fk_is_disabled, @fk_is_not_for_replication, @fk_is_not_trusted;
        END;
        CLOSE cur_fk;
        DEALLOCATE cur_fk;

        IF @fk_dropped > 0
            PRINT CONCAT(N'  [DROP] ', @fk_dropped, N' foreign key constraint(s)');

        -- ============ PK / UQ PHASE ============
        --
        -- DDL pieces (key cols, WITH options, ON clause) come from
        -- [admin].[_fn_IndexDDLPieces] — same source as the UX phase below.
        DECLARE @pk_dropped int = 0, @uq_dropped int = 0, @pk_failed int = 0;
        DECLARE @total_freed decimal(14,1) = 0;
        DECLARE @pk_schema sysname, @pk_table sysname, @pk_name sysname,
                @pk_constraint_type varchar(2), @pk_index_type tinyint,
                @pk_size_mb decimal(12,1),
                @pk_key_cols nvarchar(max), @pk_with_opts nvarchar(max), @pk_on_clause nvarchar(max),
                @pk_def_sql nvarchar(max), @pk_drop_sql nvarchar(max);
        DECLARE @pk_save_id int;

        DECLARE cur_pk CURSOR LOCAL FAST_FORWARD READ_ONLY FOR
            SELECT tt.schema_name, tt.table_name, kc.name, kc.type, i.type,
                   pieces.key_cols, pieces.with_opts, pieces.on_clause,
                   CAST(SUM(ps.used_page_count) * 8.0 / 1024 AS decimal(12,1))
            FROM @Targets tt
            JOIN sys.key_constraints kc ON kc.parent_object_id = tt.object_id AND kc.type IN ('PK', 'UQ')
            JOIN sys.indexes i ON i.object_id = kc.parent_object_id AND i.name = kc.name
            JOIN sys.dm_db_partition_stats ps ON ps.object_id = i.object_id AND ps.index_id = i.index_id
            OUTER APPLY [admin].[_fn_IndexDDLPieces](kc.parent_object_id, i.index_id) pieces
            GROUP BY tt.schema_name, tt.table_name, kc.name, kc.type, i.type,
                     pieces.key_cols, pieces.with_opts, pieces.on_clause
            ORDER BY SUM(ps.used_page_count) DESC;

        OPEN cur_pk;
        FETCH NEXT FROM cur_pk INTO @pk_schema, @pk_table, @pk_name,
                                    @pk_constraint_type, @pk_index_type,
                                    @pk_key_cols, @pk_with_opts, @pk_on_clause,
                                    @pk_size_mb;

        WHILE @@FETCH_STATUS = 0
        BEGIN
            SET @pk_def_sql = N'ALTER TABLE ' + QUOTENAME(@pk_schema) + N'.' + QUOTENAME(@pk_table)
                            + N' ADD CONSTRAINT ' + QUOTENAME(@pk_name)
                            + CASE @pk_constraint_type WHEN 'PK' THEN N' PRIMARY KEY ' ELSE N' UNIQUE ' END
                            + CASE WHEN @pk_index_type = 1 THEN N'CLUSTERED' ELSE N'NONCLUSTERED' END
                            + N' (' + @pk_key_cols + N')'
                            + @pk_with_opts
                            + @pk_on_clause
                            + N';';

            SET @pk_drop_sql = N'ALTER TABLE ' + QUOTENAME(@pk_schema) + N'.' + QUOTENAME(@pk_table)
                             + N' DROP CONSTRAINT ' + QUOTENAME(@pk_name) + N';';

            SET @pk_save_id = NULL;
            BEGIN TRY
                INSERT INTO [admin].[_PK_Backup]
                    (constraint_type, schema_name, table_name, constraint_name, definition_sql)
                VALUES (@pk_constraint_type, @pk_schema, @pk_table, @pk_name, @pk_def_sql);
                SET @pk_save_id = SCOPE_IDENTITY();

                EXEC sys.sp_executesql @pk_drop_sql;

                IF @pk_constraint_type = 'PK' SET @pk_dropped += 1;
                ELSE                          SET @uq_dropped += 1;
                SET @total_freed += @pk_size_mb;

                IF @pk_size_mb >= 1.0
                    PRINT CONCAT(N'  [DROP] ', @pk_constraint_type, N' ',
                                 QUOTENAME(@pk_schema), N'.', QUOTENAME(@pk_table),
                                 N'  ', @pk_name, N'  freed ', @pk_size_mb, N' MB');
            END TRY
            BEGIN CATCH
                SET @pk_failed += 1;
                IF @pk_save_id IS NOT NULL
                    DELETE FROM [admin].[_PK_Backup] WHERE id = @pk_save_id;
                PRINT CONCAT(N'  [FAIL] ', @pk_constraint_type, N' ',
                             QUOTENAME(@pk_schema), N'.', QUOTENAME(@pk_table),
                             N'  ', @pk_name, N' — ', ERROR_MESSAGE());
            END CATCH;

            FETCH NEXT FROM cur_pk INTO @pk_schema, @pk_table, @pk_name,
                                        @pk_constraint_type, @pk_index_type,
                                        @pk_key_cols, @pk_with_opts, @pk_on_clause,
                                        @pk_size_mb;
        END;
        CLOSE cur_pk;
        DEALLOCATE cur_pk;

        -- ============ UNIQUE INDEX PHASE (non-constraint) ============
        DECLARE @ux_dropped int = 0, @ux_failed int = 0;

        IF @IncludeUniqueIndexes = 1
        BEGIN
            DECLARE @ix_schema sysname, @ix_table sysname, @ix_name sysname,
                    @ix_type tinyint, @ix_has_filter bit, @ix_filter nvarchar(max),
                    @ix_key_cols nvarchar(max), @ix_include_cols nvarchar(max),
                    @ix_with_opts nvarchar(max), @ix_on_clause nvarchar(max),
                    @ix_size_mb decimal(12,1),
                    @ix_def_sql nvarchar(max), @ix_drop_sql nvarchar(max);
            DECLARE @ix_save_id int;

            DECLARE cur_ux CURSOR LOCAL FAST_FORWARD READ_ONLY FOR
                SELECT tt.schema_name, tt.table_name, i.name, i.type,
                       i.has_filter, i.filter_definition,
                       pieces.key_cols, pieces.include_cols, pieces.with_opts, pieces.on_clause,
                       CAST(SUM(ps.used_page_count) * 8.0 / 1024 AS decimal(12,1))
                FROM @Targets tt
                JOIN sys.indexes i ON i.object_id = tt.object_id
                JOIN sys.dm_db_partition_stats ps ON ps.object_id = i.object_id AND ps.index_id = i.index_id
                OUTER APPLY [admin].[_fn_IndexDDLPieces](tt.object_id, i.index_id) pieces
                WHERE i.is_unique = 1
                  AND i.is_unique_constraint = 0
                  AND i.is_primary_key = 0
                  AND i.type IN (1, 2)
                  AND i.name IS NOT NULL
                GROUP BY tt.schema_name, tt.table_name, i.name, i.type,
                         i.has_filter, i.filter_definition,
                         pieces.key_cols, pieces.include_cols, pieces.with_opts, pieces.on_clause
                ORDER BY SUM(ps.used_page_count) DESC;

            OPEN cur_ux;
            FETCH NEXT FROM cur_ux INTO @ix_schema, @ix_table, @ix_name, @ix_type,
                                         @ix_has_filter, @ix_filter,
                                         @ix_key_cols, @ix_include_cols, @ix_with_opts, @ix_on_clause,
                                         @ix_size_mb;

            WHILE @@FETCH_STATUS = 0
            BEGIN
                SET @ix_def_sql = N'CREATE UNIQUE '
                                + CASE WHEN @ix_type = 1 THEN N'CLUSTERED' ELSE N'NONCLUSTERED' END
                                + N' INDEX ' + QUOTENAME(@ix_name) + N' ON '
                                + QUOTENAME(@ix_schema) + N'.' + QUOTENAME(@ix_table)
                                + N' (' + @ix_key_cols + N')'
                                + CASE WHEN @ix_include_cols IS NOT NULL
                                       THEN N' INCLUDE (' + @ix_include_cols + N')' ELSE N'' END
                                + CASE WHEN @ix_has_filter = 1 AND @ix_filter IS NOT NULL
                                       THEN N' WHERE ' + @ix_filter ELSE N'' END
                                + @ix_with_opts
                                + @ix_on_clause
                                + N';';

                SET @ix_drop_sql = N'DROP INDEX ' + QUOTENAME(@ix_name) + N' ON '
                                 + QUOTENAME(@ix_schema) + N'.' + QUOTENAME(@ix_table) + N';';

                SET @ix_save_id = NULL;
                BEGIN TRY
                    INSERT INTO [admin].[_PK_Backup]
                        (constraint_type, schema_name, table_name, constraint_name, definition_sql)
                    VALUES ('UX', @ix_schema, @ix_table, @ix_name, @ix_def_sql);
                    SET @ix_save_id = SCOPE_IDENTITY();

                    EXEC sys.sp_executesql @ix_drop_sql;
                    SET @ux_dropped += 1;
                    SET @total_freed += @ix_size_mb;

                    IF @ix_size_mb >= 1.0
                        PRINT CONCAT(N'  [DROP] UX ',
                                     QUOTENAME(@ix_schema), N'.', QUOTENAME(@ix_table),
                                     N'  ', @ix_name, N'  freed ', @ix_size_mb, N' MB');
                END TRY
                BEGIN CATCH
                    SET @ux_failed += 1;
                    IF @ix_save_id IS NOT NULL
                        DELETE FROM [admin].[_PK_Backup] WHERE id = @ix_save_id;
                    PRINT CONCAT(N'  [FAIL] UX ',
                                 QUOTENAME(@ix_schema), N'.', QUOTENAME(@ix_table),
                                 N'  ', @ix_name, N' — ', ERROR_MESSAGE());
                END CATCH;

                FETCH NEXT FROM cur_ux INTO @ix_schema, @ix_table, @ix_name, @ix_type,
                                             @ix_has_filter, @ix_filter,
                                             @ix_key_cols, @ix_include_cols, @ix_with_opts, @ix_on_clause,
                                             @ix_size_mb;
            END;
            CLOSE cur_ux;
            DEALLOCATE cur_ux;
        END;

        PRINT N'';
        PRINT CONCAT(N'[OK] Dropped ', @pk_dropped, N' PK(s), ',
                     @uq_dropped, N' UQ(s), ',
                     @ux_dropped, N' UX(s), ',
                     @fk_dropped, N' FK(s) — freed ', @total_freed, N' MB');

        IF (@pk_failed + @fk_failed + @ux_failed) > 0
            PRINT CONCAT(N'     ', @pk_failed + @fk_failed + @ux_failed,
                         N' object(s) could not be dropped — see [FAIL] lines above');

        PRINT N'     Definitions saved to [admin].[_PK_Backup] for RESTORE.';
        RETURN;
    END;

    -----------------------------------------------------------------
    -- RESTORE
    --
    -- Filter semantics (when @Tables is set):
    --   PK/UQ/UX: restore when its parent table matches.
    --   FK:      restore when its parent OR referenced table matches.
    --            That makes RESTORE the true inverse of DROP @Tables=…
    --
    -- Order: PK (1), UQ (2), UX (3), FK (4) — so things FKs depend on
    -- exist by the time the FKs are recreated.
    --
    -- Already-exists guard: if the target constraint/index is already
    -- present (e.g. a previous partial DROP), skip silently and clean
    -- the stale backup row.
    -----------------------------------------------------------------
    IF @Action = 'RESTORE'
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM [admin].[_PK_Backup])
        BEGIN
            PRINT N'[OK] No backed-up constraints found. Nothing to restore.';
            RETURN;
        END;

        DECLARE @restored int = 0, @skipped int = 0, @restore_errors int = 0;
        DECLARE @r_id int, @r_type varchar(2), @r_name sysname, @r_sql nvarchar(max),
                @r_schema sysname, @r_table sysname,
                @r_ref_schema sysname, @r_ref_table sysname;
        DECLARE @already_exists bit;
        DECLARE @r_parent_object_id int;

        DECLARE cur_restore CURSOR LOCAL FAST_FORWARD READ_ONLY FOR
            SELECT b.id, b.constraint_type, b.constraint_name, b.definition_sql,
                   b.schema_name, b.table_name, b.referenced_schema, b.referenced_table
            FROM [admin].[_PK_Backup] b
            WHERE @Tables IS NULL
               OR EXISTS (
                   SELECT 1 FROM @TargetList tl
                   WHERE tl.table_name = b.table_name
                     AND (tl.schema_name IS NULL OR tl.schema_name = b.schema_name)
               )
               OR (b.constraint_type = 'FK' AND b.referenced_table IS NOT NULL AND EXISTS (
                   SELECT 1 FROM @TargetList tl
                   WHERE tl.table_name = b.referenced_table
                     AND (tl.schema_name IS NULL OR tl.schema_name = b.referenced_schema)
               ))
            ORDER BY
                CASE b.constraint_type
                    WHEN 'PK' THEN 1
                    WHEN 'UQ' THEN 2
                    WHEN 'UX' THEN 3
                    WHEN 'FK' THEN 4
                    ELSE 9 END,
                b.id;

        OPEN cur_restore;
        FETCH NEXT FROM cur_restore INTO @r_id, @r_type, @r_name, @r_sql,
                                         @r_schema, @r_table, @r_ref_schema, @r_ref_table;

        WHILE @@FETCH_STATUS = 0
        BEGIN
            -- Resolve parent table id once. NULL means the table is gone, so
            -- "already exists" is false and the restore will fail loudly.
            SET @r_parent_object_id = OBJECT_ID(QUOTENAME(@r_schema) + N'.' + QUOTENAME(@r_table));
            SET @already_exists = 0;

            IF @r_type IN ('PK', 'UQ') AND EXISTS (
                SELECT 1 FROM sys.key_constraints
                WHERE parent_object_id = @r_parent_object_id AND name = @r_name
            )
                SET @already_exists = 1;
            ELSE IF @r_type = 'FK' AND EXISTS (
                SELECT 1 FROM sys.foreign_keys
                WHERE parent_object_id = @r_parent_object_id AND name = @r_name
            )
                SET @already_exists = 1;
            ELSE IF @r_type = 'UX' AND EXISTS (
                SELECT 1 FROM sys.indexes
                WHERE object_id = @r_parent_object_id AND name = @r_name
            )
                SET @already_exists = 1;

            IF @already_exists = 1
            BEGIN
                SET @skipped += 1;
                PRINT CONCAT(N'  [SKIP]    ', @r_type, N': ', @r_name,
                             N' — already exists, removing stale backup row');
                DELETE FROM [admin].[_PK_Backup] WHERE id = @r_id;
            END
            ELSE
            BEGIN
                BEGIN TRY
                    EXEC sys.sp_executesql @r_sql;
                    SET @restored += 1;
                    PRINT CONCAT(N'  [RESTORE] ', @r_type, N': ', @r_name);
                    DELETE FROM [admin].[_PK_Backup] WHERE id = @r_id;
                END TRY
                BEGIN CATCH
                    SET @restore_errors += 1;
                    PRINT CONCAT(N'  [FAIL]    ', @r_type, N': ', @r_name,
                                 N' — ', ERROR_MESSAGE());
                END CATCH;
            END;

            FETCH NEXT FROM cur_restore INTO @r_id, @r_type, @r_name, @r_sql,
                                             @r_schema, @r_table, @r_ref_schema, @r_ref_table;
        END;
        CLOSE cur_restore;
        DEALLOCATE cur_restore;

        PRINT N'';
        PRINT CONCAT(N'[OK] Restored ', @restored, N' constraint(s)',
                     CASE WHEN @skipped > 0
                          THEN CONCAT(N', ', @skipped, N' already present (skipped)')
                          ELSE N'' END,
                     CASE WHEN @restore_errors > 0
                          THEN CONCAT(N', ', @restore_errors, N' failed')
                          ELSE N'' END);
        RETURN;
    END;
END;
GO
