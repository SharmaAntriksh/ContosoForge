"""Custom exception hierarchy for the Synthetic Data Generator.

All pipeline-specific exceptions inherit from ``PipelineError``.
Downstream code should catch ``PipelineError`` (or a subclass) rather
than bare ``RuntimeError`` / ``ValueError`` so that error handling
remains consistent and callers can distinguish pipeline failures from
unexpected Python errors.
"""
from __future__ import annotations


class PipelineError(Exception):
    """Base class for all pipeline errors."""


class ConfigError(PipelineError):
    """Raised when configuration is invalid, missing, or inconsistent."""


class DimensionError(PipelineError):
    """Raised when a dimension generator fails."""


class SalesError(PipelineError):
    """Raised when the sales fact pipeline fails."""


class PackagingError(PipelineError):
    """Raised when output packaging fails."""


class ValidationError(ConfigError):
    """Raised when a business-rule validation fails at config-load time.

    Separate from ``ConfigError`` so callers can distinguish "bad YAML
    syntax" from "valid YAML but logically inconsistent settings".
    """


class SqlServerImportError(PipelineError):
    """Raised when SQL Server import fails."""


class PostgresImportError(PipelineError):
    """Raised when Postgres import fails."""
