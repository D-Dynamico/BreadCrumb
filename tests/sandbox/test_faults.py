"""The fault profiles change what a client sees, never what was written (SANDBOX.md)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.testclient import TestClient

from sandbox import faults
from sandbox.faults import Profile, _Faults

PAGE = (
    '<html><body><main><form><button type="submit">Save payable</button></form></main>'
    "</body></html>"
)


def _client(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, list[str]]:
    """A tiny app behind the fault middleware; returns the client and the writes made."""
    monkeypatch.setattr(faults, "_end_session", lambda token: None)
    saved: list[str] = []
    app = FastAPI()

    @app.get("/page", response_class=HTMLResponse)
    def page() -> str:
        return PAGE

    @app.post("/things")
    def save(request: Request) -> RedirectResponse:
        saved.append("thing")
        return RedirectResponse("/things/1", status_code=303)

    @app.post("/login")
    def login() -> RedirectResponse:
        return RedirectResponse("/", status_code=303)

    app.middleware("http")(_Faults("demo", profile))
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("demo_session", "tok")
    return client, saved


def test_flaky_refuses_before_the_app_sees_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    client, saved = _client(Profile(error_rate=1.0), monkeypatch)
    assert client.get("/page").status_code == 503
    assert client.post("/things").status_code == 503
    assert saved == []  # a refused write was never made
    assert client.post("/login").status_code == 303  # signing in is never refused


def test_session_expires_right_after_a_successful_save_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ended: list[str] = []
    client, saved = _client(Profile(expire_after_submit=True), monkeypatch)
    monkeypatch.setattr(faults, "_end_session", ended.append)
    first = client.post("/things")
    assert first.status_code == 303 and first.headers["location"] == "/login?next=/things/1"
    assert saved == ["thing"]  # the write happened
    second = client.post("/things")
    assert second.headers["location"] == "/things/1"  # only once


def test_sessions_expire_after_a_number_of_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    ended: list[str] = []
    client, _ = _client(Profile(expire_after=2), monkeypatch)
    monkeypatch.setattr(faults, "_end_session", ended.append)
    for _ in range(3):
        client.get("/page")
    assert ended == ["tok"]


def test_drift_relabels_buttons_and_shows_whats_new_once(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(Profile(relabel=True, whats_new=True), monkeypatch)
    first = client.get("/page").text
    assert ">Submit entry</button>" in first and "Save payable" not in first
    assert "What's new" in first
    assert "What's new" not in client.get("/page").text


def test_every_profile_is_defined() -> None:
    assert set(faults.SETTINGS) == set(faults.PROFILES)
    assert faults.SETTINGS["none"] == Profile()
