/*
  create_pk_proc.sql – admin.manage_primary_keys procedure (Postgres).

  Dev-ergonomics tool: drop every PRIMARY KEY and FOREIGN KEY constraint
  on user tables, hold their DDL in admin._pk_backup, then restore on
  demand. Mirrors the spirit of SQL Server's [admin].[ManagePrimaryKeys]
  proc but is intentionally minimal — Postgres has no parallel-BULK-INSERT
  contention to design around.

  Why FKs are also managed
  ────────────────────────
  Postgres refuses to drop a PK that is referenced by any FK; you would
  need DROP CONSTRAINT … CASCADE which silently destroys the FK. To make
  "DROP → bulk-edit → RESTORE" actually round-trip, the proc captures
  both PKs and FKs, drops FKs first, then PKs, and restores in reverse.

  Usage
  ─────
      CALL admin.manage_primary_keys('DROP');     -- backs up + drops PKs and FKs
      -- ... bulk edits, tests, etc. ...
      CALL admin.manage_primary_keys('RESTORE');  -- recreates FKs and PKs

  Excludes constraints under ``pg_catalog``, ``information_schema``, and
  ``admin`` itself so the proc cannot disable system state or its own.
*/

CREATE SCHEMA IF NOT EXISTS admin;

CREATE TABLE IF NOT EXISTS admin._pk_backup (
    table_schema    text NOT NULL,
    table_name      text NOT NULL,
    constraint_name text NOT NULL,
    constraint_type "char" NOT NULL,    -- 'p' = primary, 'f' = foreign
    constraint_def  text NOT NULL,
    PRIMARY KEY (table_schema, table_name, constraint_name)
);

CREATE OR REPLACE PROCEDURE admin.manage_primary_keys(action text)
LANGUAGE plpgsql AS $$
DECLARE
    a text := upper(coalesce(action, ''));
    r record;
    dropped_pk int := 0;
    dropped_fk int := 0;
    restored_pk int := 0;
    restored_fk int := 0;
BEGIN
    IF a NOT IN ('DROP', 'RESTORE') THEN
        RAISE EXCEPTION
            'Unknown action %; expected DROP or RESTORE', action;
    END IF;

    IF a = 'DROP' THEN
        -- Drop foreign keys first so primary keys are no longer protected.
        FOR r IN
            SELECT n.nspname AS table_schema,
                   c.relname AS table_name,
                   con.conname AS constraint_name,
                   con.contype AS constraint_type,
                   pg_get_constraintdef(con.oid) AS constraint_def
            FROM pg_constraint con
            JOIN pg_class c     ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE con.contype IN ('p', 'f')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'admin')
            ORDER BY (con.contype = 'f') DESC  -- FKs first
        LOOP
            INSERT INTO admin._pk_backup
                (table_schema, table_name, constraint_name, constraint_type, constraint_def)
            VALUES (r.table_schema, r.table_name, r.constraint_name,
                    r.constraint_type, r.constraint_def)
            ON CONFLICT (table_schema, table_name, constraint_name) DO UPDATE
                SET constraint_type = EXCLUDED.constraint_type,
                    constraint_def  = EXCLUDED.constraint_def;

            EXECUTE format('ALTER TABLE %I.%I DROP CONSTRAINT %I',
                           r.table_schema, r.table_name, r.constraint_name);

            IF r.constraint_type = 'p' THEN
                dropped_pk := dropped_pk + 1;
            ELSE
                dropped_fk := dropped_fk + 1;
            END IF;
        END LOOP;
        RAISE NOTICE 'Dropped % primary key(s) and % foreign key(s); backup retained',
            dropped_pk, dropped_fk;

    ELSE  -- RESTORE
        -- Recreate PKs first so the FKs can find their targets.
        FOR r IN
            SELECT table_schema, table_name, constraint_name, constraint_type, constraint_def
            FROM admin._pk_backup
            ORDER BY (constraint_type = 'p') DESC  -- PKs first
        LOOP
            -- Per-item guard: one already-present constraint must not roll back
            -- the whole restore. SQL Server's proc degrades the same way
            -- (TRY/CATCH per constraint); match that here instead of failing the
            -- entire CALL on the first duplicate.
            BEGIN
                EXECUTE format('ALTER TABLE %I.%I ADD CONSTRAINT %I %s',
                               r.table_schema, r.table_name, r.constraint_name, r.constraint_def);
                IF r.constraint_type = 'p' THEN
                    restored_pk := restored_pk + 1;
                ELSE
                    restored_fk := restored_fk + 1;
                END IF;
            EXCEPTION
                WHEN duplicate_object OR duplicate_table THEN
                    RAISE NOTICE 'Constraint % on %.% already exists; skipped',
                        r.constraint_name, r.table_schema, r.table_name;
            END;
        END LOOP;
        DELETE FROM admin._pk_backup;
        RAISE NOTICE 'Restored % primary key(s) and % foreign key(s); backup cleared',
            restored_pk, restored_fk;
    END IF;
END $$;
