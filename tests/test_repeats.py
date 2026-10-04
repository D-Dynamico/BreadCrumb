"""Repeat detection: the first slice of the loop detection in AGENT_DESIGN.md section 3."""

from __future__ import annotations

from breadcrumb.executor.repeats import WINDOW, RepeatGuard, Response, fingerprint

FORM = """URL: http://localhost:8103/payables/new
TITLE: New payable
- heading "New payable" [level=1]
- [8] combobox "Vendor":
  - option "Choose a vendor" [selected]
- [9] textbox "Invoice number"
- [10] button "Save payable"
- text: Due date is required
"""
FILLED = FORM.replace('[9] textbox "Invoice number"', '[9] textbox "Invoice number": INV-1')
READ = {"name": "a.pdf", "why": "look again", "remember": [], "plan": []}


def test_a_new_action_passes() -> None:
    guard = RepeatGuard()
    assert guard.check(1, "files_read", READ, "fp-a").response is Response.OK
    assert guard.check(2, "files_read", {**READ, "name": "b.pdf"}, "fp-a").response is Response.OK
    assert guard.check(3, "files_read", READ, "fp-b").response is Response.OK


def test_repeats_escalate_note_then_refuse_then_escalate() -> None:
    guard = RepeatGuard()
    guard.check(1, "files_read", READ, "fp")
    guard.check(2, "browser_open", {"url": "/x"}, "fp-page")
    first = guard.check(3, "files_read", {**READ, "why": "other words"}, "fp")
    assert first.response is Response.NOTE
    assert "step 1" in first.message and "remember" in first.message
    second = guard.check(4, "files_read", READ, "fp")
    assert second.response is Response.REFUSE
    assert "1, 3" in second.message and "plan" in second.message
    third = guard.check(5, "files_read", READ, "fp")
    assert third.response is Response.ESCALATE
    assert "files_read" in third.message


def test_repeats_outside_the_window_do_not_count() -> None:
    guard = RepeatGuard()
    guard.check(1, "files_read", READ, "fp")
    for step in range(2, 2 + WINDOW):
        guard.check(step, "browser_open", {"url": f"/p{step}"}, f"fp{step}")
    assert guard.check(2 + WINDOW, "files_read", READ, "fp").response is Response.OK


def test_progress_resets_the_count() -> None:
    guard = RepeatGuard()
    guard.check(1, "files_read", READ, "fp")
    assert guard.check(2, "files_read", READ, "fp").response is Response.NOTE
    guard.progress()  # a confirmed commit, or a subgoal marked done
    assert guard.check(3, "files_read", READ, "fp").response is Response.OK
    assert guard.check(4, "files_read", READ, "fp").response is Response.NOTE


def test_a_new_session_starts_with_a_clean_window() -> None:
    guard = RepeatGuard()
    guard.check(1, "browser_open", {"url": "/x"}, "fp")
    resumed = RepeatGuard()  # a resumed run builds a new guard
    assert resumed.check(2, "browser_open", {"url": "/x"}, "fp").response is Response.OK


def test_finish_is_never_a_repeat() -> None:
    guard = RepeatGuard()
    guard.check(1, "finish", {"summary": "x"}, "fp")
    assert guard.check(2, "finish", {"summary": "x"}, "fp").response is Response.OK


def test_browser_fingerprint_is_url_path_and_interactive_elements_with_values() -> None:
    page = fingerprint("browser http://localhost:8103/payables/new", FORM)
    assert page == fingerprint("browser x", FORM.replace("Due date is required", "other text"))
    assert page != fingerprint("browser x", FILLED)  # a typed value changes the state
    elsewhere = FORM.replace("/payables/new", "/payables/7")
    assert page != fingerprint("browser x", elsewhere)
    assert page == fingerprint("browser x", FORM.replace("localhost:8103", "127.0.0.1:8103"))


def test_a_new_download_changes_the_state() -> None:
    before = fingerprint("browser x", FORM)
    assert before != fingerprint("browser x", FORM, ("a.pdf",))


def test_file_and_api_fingerprint_is_source_and_content() -> None:
    assert fingerprint("file a.pdf", "p1 L1: x") == fingerprint("file a.pdf", "p1 L1: x")
    assert fingerprint("file a.pdf", "p1 L1: x") != fingerprint("file b.pdf", "p1 L1: x")
    assert fingerprint("API listX", "HTTP 200\n[]") != fingerprint("API listX", "HTTP 200\n[1]")
