"""Web plumbing shared by the three apps: logins, sessions, CSRF, templates.

Each app gets its own cookie name. Browsers share cookies across ports on the same
host, so separate names are what keep the apps' sessions separate.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from sandbox import faults
from sandbox.common.config import STATIC_DIR, setting
from sandbox.common.db import db, now_iso
from sandbox.common.money import format_inr

Verify = Callable[[sqlite3.Connection, str, str], bool]


class LoginRequired(Exception):
    """Raised by the `user` dependency; turned into a redirect to the login page."""


def _safe_next(target: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") else "/"


class WebApp:
    def __init__(self, name: str, title: str, templates_dir: Path, verify: Verify) -> None:
        self.name = name
        self.cookie = f"{name}_session"
        self.verify = verify
        self.templates = Jinja2Templates(directory=str(templates_dir))
        self.templates.env.filters["inr"] = format_inr
        self.templates.env.globals["app_title"] = title
        self.app = FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None)
        self.app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
        self.app.add_exception_handler(LoginRequired, self._to_login)
        self.app.add_exception_handler(StarletteHTTPException, self._error_page)
        faults.install(self.app, name)
        self._add_login_routes()

    # -- sessions -----------------------------------------------------------
    def user(self, request: Request) -> str:
        """FastAPI dependency: the logged-in username, or a redirect to login."""
        token = request.cookies.get(self.cookie)
        if token:
            with db() as conn:
                row = conn.execute(
                    "SELECT username FROM sessions WHERE token = ? AND app = ?",
                    (token, self.name),
                ).fetchone()
            if row:
                username = str(row["username"])
                request.state.user = username
                return username
        raise LoginRequired()

    async def _to_login(self, request: Request, exc: Exception) -> Response:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)

    async def _error_page(self, request: Request, exc: Exception) -> Response:
        assert isinstance(exc, StarletteHTTPException)
        return self.render(request, "error.html", status_code=exc.status_code, detail=exc.detail)

    # -- CSRF ---------------------------------------------------------------
    def csrf_for(self, request: Request) -> str:
        token = request.cookies.get(self.cookie, "")
        key = setting("SANDBOX_SECRET").encode()
        return hmac.new(key, f"{self.name}:{token}".encode(), hashlib.sha256).hexdigest()[:32]

    def check_csrf(self, request: Request, submitted: str) -> None:
        if not hmac.compare_digest(self.csrf_for(request), submitted):
            raise HTTPException(403, "This form has expired. Reload the page and try again.")

    # -- rendering ----------------------------------------------------------
    def render(
        self, request: Request, template: str, status_code: int = 200, **context: Any
    ) -> HTMLResponse:
        context.setdefault("csrf_token", self.csrf_for(request))
        context.setdefault("current_user", getattr(request.state, "user", None))
        return self.templates.TemplateResponse(request, template, context, status_code=status_code)

    def _add_login_routes(self) -> None:
        app = self.app

        @app.get("/login", response_class=HTMLResponse)
        def login_form(request: Request, next: str = "/") -> HTMLResponse:
            return self.render(request, "login.html", next=_safe_next(next), error=None)

        @app.post("/login")
        def login(
            request: Request,
            username: Annotated[str, Form()],
            password: Annotated[str, Form()],
            next: Annotated[str, Form()] = "/",
        ) -> Response:
            with db() as conn:
                if not self.verify(conn, username.strip(), password):
                    return self.render(
                        request,
                        "login.html",
                        status_code=401,
                        next=_safe_next(next),
                        username=username,
                        error="Wrong username or password.",
                    )
                token = secrets.token_urlsafe(24)
                conn.execute(
                    "INSERT INTO sessions (token, app, username, created_at) VALUES (?, ?, ?, ?)",
                    (token, self.name, username.strip(), now_iso()),
                )
            response = RedirectResponse(_safe_next(next), status_code=303)
            response.set_cookie(self.cookie, token, httponly=True, samesite="lax")
            return response

        @app.post("/logout")
        def logout(request: Request) -> Response:
            token = request.cookies.get(self.cookie)
            if token:
                with db() as conn:
                    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            response = RedirectResponse("/login", status_code=303)
            response.delete_cookie(self.cookie)
            return response
