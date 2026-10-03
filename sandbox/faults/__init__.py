"""Fault injection in front of every sandbox app.

Phase 1 installs the hook with the `none` profile only, so every app already runs
behind it. The other profiles (flaky, session, drift, chaos) arrive in Phase 4;
asking for one before then fails at startup instead of silently running clean.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from sandbox.common.config import setting

PROFILES = ("none", "flaky", "session", "drift", "chaos")


def active_profile() -> str:
    profile = setting("FAULTS")
    if profile not in PROFILES:
        raise ValueError(f"unknown fault profile {profile!r}; choose from {PROFILES}")
    return profile


def install(app: FastAPI, app_name: str) -> None:
    profile = active_profile()
    if profile != "none":
        raise NotImplementedError(f"fault profile {profile!r} arrives in Phase 4")

    @app.middleware("http")
    async def fault_hook(request: Request, call_next: RequestResponseEndpoint) -> Response:
        return await call_next(request)
