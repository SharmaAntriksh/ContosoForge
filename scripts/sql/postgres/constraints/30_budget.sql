/*
  30_budget.sql – Budget fact constraints for Postgres.
  Hand-translated from scripts/sql/bootstrap/constraints/30_budget.sql.

  Tables:
    BudgetYearly   – annual grain  (Country, Category, BudgetYear, Scenario)
    BudgetMonthly  – monthly grain (Country, Category, BudgetYear, BudgetMonthStart, Scenario)
*/

-- BudgetYearly composite PK
DO $$
BEGIN
    IF to_regclass('"public"."BudgetYearly"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_BudgetYearly')
    THEN
        ALTER TABLE "public"."BudgetYearly"
        ADD CONSTRAINT "PK_BudgetYearly"
            PRIMARY KEY ("Country", "Category", "BudgetYear", "Scenario");
    END IF;
END $$;

-- BudgetMonthly composite PK
DO $$
BEGIN
    IF to_regclass('"public"."BudgetMonthly"') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'PK_BudgetMonthly')
    THEN
        ALTER TABLE "public"."BudgetMonthly"
        ADD CONSTRAINT "PK_BudgetMonthly"
            PRIMARY KEY ("Country", "Category", "BudgetYear", "BudgetMonthStart", "Scenario");
    END IF;
END $$;
