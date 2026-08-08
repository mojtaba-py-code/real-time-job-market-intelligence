"""HTTP routers."""

from __future__ import annotations

from app.api.routers import admin, alerts, analytics, companies, health, jobs, profiles, skills

__all__ = [
    "admin",
    "alerts",
    "analytics",
    "companies",
    "health",
    "jobs",
    "profiles",
    "skills",
]
