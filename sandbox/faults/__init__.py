"""Fault injection in front of every sandbox app (SANDBOX.md, fault profiles).

One middleware, driven by the `FAULTS` profile and seeded by `FAULT_SEED`, so a
profile misbehaves the same way on every run. It only changes what the client sees
and when. It never corrupts data, and it never makes a write fail while reporting
success. The one documented exception is in `session`: a write can succeed while the
client is shown a login page, the realistic "maybe committed" case.

- `flaky`: some requests answer 503 before reaching the app; some are slow.
- `session`: sessions expire after a number of requests, and once right after a
  successful form submit (the write happened; the response redirects to login).
- `drift`: buttons are relabelled ("Save payable" becomes "Submit entry") and a
  "What's new" panel appears once. Built last and partly, as D36 allows: field
  order is not changed.
- `chaos`: all of the above at lower rates.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from sandbox.common.config import setting
from sandbox.common.db import db

PROFILES = ("none", "flaky", "session", "drift", "chaos")


@dataclass(frozen=True)
class Profile:
    error_rate: float = 0.0  # share of requests answered 503 before the app sees them
    slow_rate: float = 0.0  # share of requests delayed 2 to 4 seconds
    expire_after: int = 0  # a session dies after this many requests (0: never)
    expire_after_submit: bool = False  # once per app: the session dies right after a save
    relabel: bool = False  # buttons get other labels
    whats_new: bool = False  # a "What's new" panel appears once


SETTINGS = {
    "none": Profile(),
    "flaky": Profile(error_rate=0.15, slow_rate=0.15),
    "session": Profile(expire_after=25, expire_after_submit=True),
    "drift": Profile(relabel=True, whats_new=True),
    "chaos": Profile(
        error_rate=0.05,
        slow_rate=0.05,
        expire_after=50,
        expire_after_submit=True,
        relabel=True,
        whats_new=True,
    ),
}

RELABEL = {
    "Save payable": "Submit entry",
    "Post message": "Send to channel",
    "Create ticket": "Open ticket",
    "Add employee": "Create person record",
    "Send": "Send now",
    "Search": "Find",
    "Filter": "Apply",
}
WHATS_NEW = (
    "<dialog open aria-label=\"What's new\"><p>What's new: faster search and a cleaner "
    'layout.</p><form method="dialog"><button>Got it</button></form></dialog>'
)
UNAVAILABLE = (
    "<!doctype html><title>Service unavailable</title><h1>Service temporarily "
    "unavailable</h1><p>Please try again in a moment.</p>"
)


def active_profile() -> str:
    profile = setting("FAULTS")
    if profile not in PROFILES:
        raise ValueError(f"unknown fault profile {profile!r}; choose from {PROFILES}")
    return profile


class _Faults:
    def __init__(self, app_name: str, profile: Profile) -> None:
        self.cookie = f"{app_name}_session"
        self.profile = profile
        seed = setting("FAULT_SEED") if _has("FAULT_SEED") else "1"
        self.rng = random.Random(f"{seed}:{app_name}")
        self.requests: dict[str, int] = {}
        self.submit_expired = False
        self.shown_whats_new = False

    async def __call__(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path, method = request.url.path, request.method
        if path.startswith("/static") or path == "/logout":
            return await call_next(request)
        p = self.profile
        is_login_post = method == "POST" and path == "/login"
        if p.slow_rate and self.rng.random() < p.slow_rate:
            await asyncio.sleep(2 + 2 * self.rng.random())
        if p.error_rate and not is_login_post and self.rng.random() < p.error_rate:
            return Response(UNAVAILABLE, status_code=503, media_type="text/html")
        token = request.cookies.get(self.cookie)
        if token and p.expire_after:
            self.requests[token] = self.requests.get(token, 0) + 1
            if self.requests[token] > p.expire_after:
                _end_session(token)  # the app now sends this request to the login page
        response = await call_next(request)
        if (
            token
            and p.expire_after_submit
            and not self.submit_expired
            and method == "POST"
            and path != "/login"
            and not path.startswith("/api")
            and response.status_code == 303
        ):
            self.submit_expired = True  # the write happened; the session dies now
            _end_session(token)
            response.headers["location"] = "/login?next=" + quote(
                response.headers.get("location", "/")
            )
        if (p.relabel or p.whats_new) and response.headers.get("content-type", "").startswith(
            "text/html"
        ):
            return await self._drift(response)
        return response

    async def _drift(self, response: Response) -> Response:
        body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]
        html = body.decode("utf-8")
        if self.profile.relabel:
            for old, new in RELABEL.items():
                html = html.replace(f">{old}</button>", f">{new}</button>")
        if self.profile.whats_new and not self.shown_whats_new and "<main" in html:
            self.shown_whats_new = True
            html = html.replace("<main", WHATS_NEW + "<main", 1)
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(html, status_code=response.status_code, headers=headers)


def _has(name: str) -> bool:
    try:
        setting(name)
    except KeyError:
        return False
    return True


def _end_session(token: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def install(app: FastAPI, app_name: str) -> None:
    profile = SETTINGS[active_profile()]
    if profile == Profile():
        return
    app.middleware("http")(_Faults(app_name, profile))
