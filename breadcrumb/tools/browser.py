"""Browser tool: a real Chromium page, observed through its accessibility tree.

Observation (D12): Playwright's ARIA snapshot of the page, with every interactive
element numbered, for example `[7] textbox "Invoice number": INV-1`. An element is
acted on by its role, accessible name and position among same-named elements, so
nothing is ever added to the page.

Writes (ground rule 2, D20): every request other than GET is checked against what
the worker declared. A submit or login it announced is expected; anything else is
recorded as an integrity violation and reported back.

The tool knows nothing about any app or task. It only has URLs and logins.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Download, Locator, Page, Request, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from breadcrumb.config import AppAccess

INTERACTIVE = {
    "link", "button", "textbox", "searchbox", "combobox", "listbox", "checkbox",
    "radio", "spinbutton", "slider", "switch", "tab", "menuitem", "option",
}  # fmt: skip
_LINE = re.compile(
    r'^(?P<indent>\s*)- (?P<role>[a-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>.*)$'
)
MAX_OBSERVATION = 14_000
ACTION_TIMEOUT_MS = 10_000


class BrowserError(Exception):
    """An action could not be done; the message is shown to the model."""


@dataclass(frozen=True)
class ElementRef:
    role: str
    name: str | None
    nth_named: int
    nth_role: int

    def describe(self) -> str:
        return f'{self.role} "{self.name}"' if self.name else self.role


@dataclass
class WriteRecord:
    method: str
    url: str
    declared: str | None


@dataclass
class _State:
    elements: dict[int, ElementRef] = field(default_factory=dict)
    downloads: list[Download] = field(default_factory=list)
    declared: str | None = None
    writes: list[WriteRecord] = field(default_factory=list)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _compact(lines: list[str]) -> list[str]:
    """Drop text the snapshot repeats: link URLs, labels echoed by the next element's
    name, a row's name (the text of all its cells) and a cell that only wraps a
    same-named link."""
    kept: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        nxt = _LINE.match(lines[i + 1]) if i + 1 < len(lines) else None
        if stripped.startswith("- /url:"):
            continue
        if stripped.startswith("- text: ") and nxt and nxt.group("name") == stripped[8:]:
            continue
        m = _LINE.match(line)
        if m and m.group("role") == "row" and m.group("name") is not None:
            line = f"{m.group('indent')}- row:"
        elif (
            m
            and m.group("role") == "cell"
            and nxt
            and nxt.group("name") == m.group("name")
            and _indent(lines[i + 1]) > _indent(line)
        ):
            continue
        kept.append(line)
    # A trailing ":" promises children; remove it where they were all dropped.
    return [
        ln[:-1]
        if ln.endswith(":") and (i + 1 == len(kept) or _indent(kept[i + 1]) <= _indent(ln))
        else ln
        for i, ln in enumerate(kept)
    ]


def number_snapshot(snapshot: str) -> tuple[str, dict[int, ElementRef]]:
    """Number interactive elements, after dropping repeated text."""
    out: list[str] = []
    elements: dict[int, ElementRef] = {}
    named: dict[tuple[str, str], int] = {}
    by_role: dict[str, int] = {}
    for line in _compact(snapshot.splitlines()):
        m = _LINE.match(line)
        if not m or m.group("role") not in INTERACTIVE:
            out.append(line)
            continue
        role, name = m.group("role"), m.group("name")
        nth_role = by_role.get(role, 0)
        by_role[role] = nth_role + 1
        nth_named = 0
        if name is not None:
            nth_named = named.get((role, name), 0)
            named[(role, name)] = nth_named + 1
        if role == "option":  # options are chosen through their combobox, not clicked
            out.append(line)
            continue
        number = len(elements) + 1
        elements[number] = ElementRef(role, name, nth_named, nth_role)
        out.append(f"{m.group('indent')}- [{number}] {line.strip()[2:]}")
    return "\n".join(out), elements


class BrowserTool:
    def __init__(
        self,
        apps: list[AppAccess],
        downloads_dir: Path,
        on_violation: Callable[[WriteRecord], None],
        headless: bool = True,
    ) -> None:
        self.apps = apps
        self.downloads_dir = downloads_dir
        self.on_violation = on_violation
        self.headless = headless
        self._state = _State()
        self._pw: Any = None
        self._browser: Any = None
        self._page: Page | None = None

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        context = self._browser.new_context(
            accept_downloads=True, viewport={"width": 1280, "height": 900}
        )
        context.set_default_timeout(ACTION_TIMEOUT_MS)
        self._page = context.new_page()
        self._page.on("request", self._watch_request)
        self._page.on("download", self._on_download)

    def _on_download(self, download: Download) -> None:
        self._state.downloads.append(download)

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise BrowserError("the browser is not started")
        return self._page

    @property
    def url(self) -> str:
        return self._page.url if self._page is not None else ""

    # -- writes watch ----------------------------------------------------------
    def _watch_request(self, request: Request) -> None:
        if request.method == "GET":
            return
        record = WriteRecord(request.method, request.url, self._state.declared)
        self._state.writes.append(record)
        if record.declared is None:
            self.on_violation(record)

    @contextmanager
    def _declared(self, what: str) -> Iterator[None]:
        self._state.declared = what
        try:
            yield
        finally:
            self._state.declared = None

    def take_writes(self) -> list[WriteRecord]:
        writes, self._state.writes = self._state.writes, []
        return writes

    # -- observing -------------------------------------------------------------
    def observe(self) -> str:
        page = self.page
        try:
            page.wait_for_load_state("load")
            raw = page.locator("body").aria_snapshot()
        except PlaywrightError as exc:
            raise BrowserError(f"could not read the page: {exc.message}") from exc
        text, self._state.elements = number_snapshot(raw)
        if len(text) > MAX_OBSERVATION:
            text = text[:MAX_OBSERVATION] + "\n... (page truncated)"
        notes = self._save_downloads()
        header = f"URL: {page.url}\nTITLE: {page.title()}"
        return "\n".join([header, *notes, text])

    def _save_downloads(self) -> list[str]:
        notes = []
        for download in self._state.downloads:
            self.downloads_dir.mkdir(parents=True, exist_ok=True)
            target = self.downloads_dir / download.suggested_filename
            download.save_as(target)
            notes.append(f"DOWNLOADED FILE: {target.name} (read it with files_read)")
        self._state.downloads.clear()
        return notes

    # -- acting ----------------------------------------------------------------
    def _locate(self, element: int) -> Locator:
        ref = self._state.elements.get(element)
        if ref is None:
            raise BrowserError(f"there is no element [{element}] on the current page")
        page = self.page
        if ref.name is not None:
            located = page.get_by_role(ref.role, name=ref.name, exact=True)  # type: ignore[arg-type]
            if located.count() > ref.nth_named:
                return located.nth(ref.nth_named)
        return page.get_by_role(ref.role).nth(ref.nth_role)  # type: ignore[arg-type]

    def _settle(self) -> None:
        page = self.page
        try:
            page.wait_for_load_state("load")
            page.wait_for_timeout(400)  # let downloads and HTMX swaps land
        except PlaywrightError:
            pass

    def _do(self, what: str, act: Callable[[], None]) -> str:
        try:
            act()
        except PlaywrightError as exc:
            raise BrowserError(f"{what} failed: {exc.message.splitlines()[0]}") from exc
        self._settle()
        return self.observe()

    def open(self, url: str) -> str:
        def go() -> None:
            self.page.goto(url)

        return self._do(f"opening {url}", go)

    def click(self, element: int) -> str:
        target = self._locate(element)
        return self._do(f"clicking [{element}]", lambda: target.click())

    def type(self, element: int, text: str) -> str:
        target = self._locate(element)
        return self._do(f"typing into [{element}]", lambda: target.fill(text))

    def select(self, element: int, option: str) -> str:
        target = self._locate(element)

        def choose() -> None:
            try:
                target.select_option(label=option)
            except PlaywrightError:
                target.select_option(value=option)

        return self._do(f"selecting {option!r} in [{element}]", choose)

    def submit(self, element: int) -> str:
        """Click a control that saves or sends something: a declared write."""
        target = self._locate(element)
        with self._declared("submit"):
            return self._do(f"submitting with [{element}]", lambda: target.click())

    def login(self) -> str:
        """Sign in to the app the browser is on, with the credentials from config."""
        page = self.page
        app = next((a for a in self.apps if page.url.startswith(a.origin)), None)
        if app is None:
            raise BrowserError("the current page does not belong to any known app")
        password = page.locator("input[type=password]")
        if password.count() == 0:
            raise BrowserError("there is no sign-in form on this page")
        form = page.locator("form").filter(has=password).first
        username = form.locator(
            "input:not([type=hidden]):not([type=password]):not([type=submit])"
        ).first

        def sign_in() -> None:
            username.fill(app.username)
            password.first.fill(app.password.get_secret_value())
            password.first.press("Enter")

        with self._declared("login"):
            return self._do(f"signing in to {app.name}", sign_in)
