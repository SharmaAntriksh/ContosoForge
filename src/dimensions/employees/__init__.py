"""Employee dimension package.

Submodules:
  generator                   — employee hierarchy and staffing
  employee_store_assignments  — bridge table (home store assignments)
  transfers                   — transfer scheduling logic
"""
from src.dimensions.employees.generator import generate_employee_dimension
from src.dimensions.employees.runner import run_employees
from src.dimensions.employees.employee_store_assignments import (
    generate_employee_store_assignments,
    run_employee_store_assignments,
)

__all__ = [
    "generate_employee_dimension",
    "run_employees",
    "generate_employee_store_assignments",
    "run_employee_store_assignments",
]
