"""Canonical household analytics derived from private normalized data."""

from .generator import AnalyticsError, build, plan, verify

__all__ = ["AnalyticsError", "build", "plan", "verify"]
