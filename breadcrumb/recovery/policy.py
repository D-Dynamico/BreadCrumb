"""The recovery policy: a deterministic table from what went wrong to what to do
(AGENT_DESIGN.md section 7).

The executor applies RETRY and RELOGIN itself, without asking the model. RECONCILE
is the gateway's (an unclear commit is settled by looking, never retried blindly).
REPLAN means the model decides what to try next, with the error in front of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

MAX_RETRIES = 3
COMMITS = frozenset({"browser_submit", "http_write", "notify"})
READS = frozenset(
    {"browser_open", "browser_view", "browser_click", "browser_type", "browser_select", "login",
     "files_list", "files_read", "http_get"}
)  # fmt: skip
INTERACTIONS = frozenset({"browser_click", "browser_type", "browser_select"})
_TRANSIENT = re.compile(
    r"^HTTP 5\d\d|timeout|timed out|server disconnected|net::ERR_|temporarily unavailable"
    r"|connection (refused|reset)",
    re.IGNORECASE | re.MULTILINE,
)
_HTTP_ERROR = re.compile(r"^HTTP 4\d\d", re.MULTILINE)


class Strategy(StrEnum):
    NONE = "none"  # nothing went wrong
    RETRY = "retry"  # transient: try the same read again, with backoff, up to 3 times
    RELOGIN = "relogin"  # the session expired: sign in again, the app returns to the page
    REPLAN = "replan"  # show the error to the model and let it choose
    RECONCILE = "reconcile"  # an unclear commit: the gateway settles it by looking


# The table, as documentation and for the receipt.
TABLE: list[tuple[str, str, Strategy]] = [
    ("Transient read", "5xx on a page or API read, a timeout, a dropped connection",
     Strategy.RETRY),
    ("Session expired", "a browser action lands on a sign-in page", Strategy.RELOGIN),
    ("Transient commit", "5xx or no answer after a submit", Strategy.RECONCILE),
    ("Element missing", "the numbered element is gone", Strategy.REPLAN),
    ("Validation error", "the app refused the input (4xx)", Strategy.REPLAN),
    ("Loop", "the same action on the same state (repeat detection)", Strategy.REPLAN),
]  # fmt: skip


@dataclass(frozen=True)
class Situation:
    action: str
    ok: bool
    outcome: str
    page_status: int | None = None  # the browser page's HTTP status, after the action
    on_sign_in_page: bool = False


def choose(s: Situation) -> Strategy:
    if s.on_sign_in_page and s.action != "login" and s.action.startswith(("browser", "notify")):
        return Strategy.RELOGIN
    if s.action in COMMITS:
        return Strategy.NONE  # the gateway has already settled it
    if s.action not in READS:
        return Strategy.NONE
    page_failed = s.page_status is not None and s.page_status >= 500
    if s.action in INTERACTIONS:
        # A timeout on a click or a choice usually means the element or option is not
        # there; only a failed page is worth reloading.
        transient = page_failed
    else:
        transient = bool(_TRANSIENT.search(s.outcome)) or page_failed
    if transient:
        return Strategy.RETRY
    if not s.ok or _HTTP_ERROR.search(s.outcome) or s.on_sign_in_page:
        return Strategy.REPLAN
    return Strategy.NONE
