"""Importing this package registers every handler on the shared router."""

from ..routes import router
from . import common, contacts, followup, leads, meetings, profile, reports, tasks  # noqa: F401  (imported for registration)

__all__ = ["router"]
