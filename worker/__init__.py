"""Persistent AutoApply Cloud worker.

The Vercel application is the control plane.  This package is deliberately a
separate, long-running process that leases durable jobs from Supabase.
"""

__all__ = ["__version__"]

__version__ = "2.0.0"
