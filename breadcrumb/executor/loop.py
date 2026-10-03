"""The executor loop: observe, decide one action, act, record. Repeat.

Reads go straight to the tools. Commits (`browser_submit`, `http_write`, `notify`)
go through the gateway, which journals them before and after they touch the world.
After every step the run state is checkpointed, so a run killed at any moment can
be resumed: `Executor.resume` takes the lease, settles whatever was in flight by
looking in the apps, and continues from the last checkpoint (DURABILITY.md).

No contract or verifier yet (Phase 4): `finish` ends the run as FINISHED, which means
"the worker says it is done", not "verified". Budgets end the run as FAILED.
"""

from __future__ import annotations

import json
import os
import secrets
import string
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from breadcrumb.config import ROOT, Settings
from breadcrumb.executor.actions import ACTIONS, WRITE_ACTIONS
from breadcrumb.executor.context import build_prompt
from breadcrumb.executor.repeats import Check, RepeatGuard, Response, fingerprint
from breadcrumb.executor.state import PlanStep, RunState, StepRecord
from breadcrumb.gateway.gateway import (
    Committed,
    Fired,
    Gateway,
    GatewayRefusal,
    declaration_from,
)
from breadcrumb.journal.journal import Journal
from breadcrumb.journal.machine import State, Unanswerable
from breadcrumb.llm.client import ModelClient, ModelError
from breadcrumb.runs.crash import CrashPoints
from breadcrumb.runs.lease import Heartbeat
from breadcrumb.runs.store import RunStatus, RunStore
from breadcrumb.tools.browser import BrowserError, BrowserTool, WriteRecord
from breadcrumb.tools.files import FilesError, FilesTool
from breadcrumb.tools.http import HttpError, HttpTool
from breadcrumb.tools.notify import NotifyError, NotifyTool

SYSTEM_PROMPT = ROOT / "prompts" / "executor.txt"
TOOL_ERRORS = (BrowserError, FilesError, HttpError, NotifyError, httpx.HTTPError, GatewayRefusal)
StepPrinter = Callable[[StepRecord], None]
Notice = Callable[[str], None]


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{secrets.token_hex(2)}"


def new_reference() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "BC-" + "".join(secrets.choice(alphabet) for _ in range(4))


class Executor:
    def __init__(
        self,
        settings: Settings,
        model: ModelClient,
        store: RunStore,
        state: RunState,
        printer: StepPrinter | None = None,
        notice: Notice | None = None,
        resumed: bool = False,
    ) -> None:
        self.settings = settings
        self.model = model
        self.store = store
        self.state = state
        self.resumed = resumed
        self.pid = os.getpid()
        self.run_dir: Path = settings.runs_dir / self.state.run_id
        self.printer = printer
        self.notice = notice or (lambda _text: None)
        self.system = SYSTEM_PROMPT.read_text(encoding="utf-8")
        self.files = FilesTool(self.run_dir / "downloads")
        self.http = HttpTool(settings.api_base_url, settings.admin_api_token.get_secret_value())
        self.notify = NotifyTool(
            settings.notify_url,
            settings.notify_channel,
            settings.admin_api_token.get_secret_value(),
            self.state.reference,
        )
        self.browser = BrowserTool(
            settings.apps, self.run_dir / "downloads", self._violation, settings.headless
        )
        self.journal = Journal(store, self.state.run_id)
        # A resumed run never crashes on purpose: one injected crash, one interruption.
        crash = CrashPoints("" if resumed else settings.crash_point)
        self.gateway = Gateway(self.journal, self._lookup, crash)
        self.heartbeat = Heartbeat(store, self.state.run_id, self.pid, settings.heartbeat_seconds)
        # New for every session of the run: re-observing after a resume is not a repeat.
        self.repeats = RepeatGuard()

    @classmethod
    def start(
        cls,
        settings: Settings,
        model: ModelClient,
        store: RunStore,
        task: str,
        printer: StepPrinter | None = None,
        notice: Notice | None = None,
    ) -> Executor:
        state = RunState(run_id=new_run_id(), task=task, reference=new_reference())
        store.create_run(state.run_id, task, state.reference, os.getpid())
        return cls(settings, model, store, state, printer, notice)

    @classmethod
    def resume(
        cls,
        settings: Settings,
        model: ModelClient,
        store: RunStore,
        run_id: str,
        printer: StepPrinter | None = None,
        notice: Notice | None = None,
    ) -> Executor:
        """Take the lease of an interrupted run. Raises LeaseError if that is not allowed."""
        store.mark_stale()
        run = store.acquire(run_id, os.getpid())
        saved = store.load_checkpoint(run_id)
        state = (
            RunState.from_checkpoint(saved)
            if saved
            else RunState(run_id=run_id, task=run.task, reference=run.reference)
        )
        state.status, state.resumes = RunStatus.RECONCILING.value, run.resumes
        return cls(settings, model, store, state, printer, notice, resumed=True)

    # -- the loop ----------------------------------------------------------------
    def run(self) -> RunState:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat.start()
        try:
            if self.resumed:
                ended = self._reconcile()
                if ended is not None:
                    return ended
            else:
                started = {"task": self.state.task, "reference": self.state.reference}
                self._log({"event": "run_started", **started})
            return self._loop()
        finally:
            self.heartbeat.stop()

    def _loop(self) -> RunState:
        started, earlier = time.monotonic(), self.state.wall_seconds  # earlier sessions
        try:
            operations = self.http.describe()
        except Exception as exc:  # the API being down must not stop browser work
            operations = f"(API spec unavailable: {exc})"
        self.browser.start()
        try:
            for step in range(len(self.state.history) + 1, self.settings.max_steps + 1):
                if self.heartbeat.lost:
                    self._log({"event": "lease_lost"})
                    self.notice("Another worker took this run over; this one stops here.")
                    return self.state
                elapsed = earlier + time.monotonic() - started
                if elapsed > self.settings.max_wall_seconds:
                    return self._end(
                        RunStatus.FAILED, "Stopped: the time budget ran out.", "time_budget"
                    )
                if self.state.tokens > self.settings.max_tokens:
                    return self._end(
                        RunStatus.FAILED, "Stopped: the token budget ran out.", "token_budget"
                    )
                prompt = build_prompt(
                    self.state,
                    self.settings.apps,
                    operations,
                    self.browser.url,
                    step,
                    self.settings.max_steps,
                    self._journal_lines(),
                )
                try:
                    action, usage = self.model.decide(self.system, prompt, ACTIONS)
                except ModelError as exc:
                    return self._end(RunStatus.FAILED, f"Stopped: {exc}", "model_error")
                self.state.tokens += usage.total
                self.state.model_calls += 0 if usage.cached else 1
                state = fingerprint(self.state.observation_source, self.state.observation)
                seen = self.repeats.check(step, action.name, action.args, state)
                record, escalate, ended_by = self._act(step, action.name, action.args, seen)
                self.state.history.append(record)
                self.state.wall_seconds = elapsed
                self.store.save_checkpoint(self.state.run_id, step, self.state.to_checkpoint())
                self._log({"event": "step", **asdict(record), "usage": asdict(usage)})
                if self.printer:
                    self.printer(record)
                if escalate:
                    return self._end(RunStatus.ESCALATED, escalate.strip(), ended_by)
                if action.name == "finish":
                    summary = str(action.args.get("summary", ""))
                    return self._end(RunStatus.FINISHED, summary, "finish")
            return self._end(RunStatus.FAILED, "Stopped: the step budget ran out.", "step_budget")
        finally:
            self.browser.close()

    def _reconcile(self) -> RunState | None:
        """Resume: settle every effect that was in flight, then tell the worker."""
        self.notice(f"Resuming {self.state.run_id} (resume {self.state.resumes}). Reconciling...")
        self._log({"event": "resumed", "resume": self.state.resumes})
        settlement = self.gateway.settle_in_flight()
        lines = [f"- {s.entry.description or s.entry.action}: {s.text}" for s in settlement.entries]
        self.store.add_event(self.state.run_id, "reconciled", {"settled": lines})
        self._log({"event": "reconciled", "settled": lines})
        for line in lines or ["- nothing was in flight"]:
            self.notice(f"  {line}")
        if settlement.escalate:
            return self._end(RunStatus.ESCALATED, settlement.escalate.strip(), "reconcile")
        self.state.interruption = (
            f"This run stopped unexpectedly and was resumed (resume {self.state.resumes}). "
            "The browser is a fresh session: you are signed out and no page is open. "
            "Changes that were in progress were settled by looking in the apps:\n"
            + "\n".join(lines or ["- nothing was in progress"])
            + "\nThe journal above is the truth about what is done. Continue from there."
        )
        self.state.observation = "Fresh browser after resuming. Nothing is open yet."
        self.state.observation_source = "none"
        self.state.status = RunStatus.RUNNING.value
        self.store.set_status(self.state.run_id, RunStatus.RUNNING)
        return None

    # -- acting ------------------------------------------------------------------
    def _act(
        self, step: int, name: str, args: dict[str, Any], seen: Check
    ) -> tuple[StepRecord, str, str]:
        """Do one action. Returns its record, and an escalation and its cause, if any."""
        why = str(args.get("why", ""))
        notes = self.state.remember(list(args.get("remember") or []), step)
        done_before = {p.title for p in self.state.plan if p.status == "done"}
        if args.get("plan"):
            self.state.plan = [
                PlanStep(str(s.get("title", "")), str(s.get("status", "todo")))
                for s in args["plan"]
            ]
        progress = bool({p.title for p in self.state.plan if p.status == "done"} - done_before)
        escalate, ended_by = "", ""
        try:
            if seen.response in (Response.REFUSE, Response.ESCALATE):
                outcome, ok = seen.message, False  # not executed
                if seen.response is Response.ESCALATE:
                    escalate, ended_by = seen.message, "repeats"
            elif name in WRITE_ACTIONS:
                committed = self._commit(step, name, args)
                outcome, ok, escalate = committed.outcome, committed.ok, committed.escalate
                ended_by = "unclear_effect" if escalate else ""
                entry = committed.entry
                progress = progress or (entry is not None and entry.state is State.CONFIRMED)
            else:
                outcome, ok = self._dispatch(name, args), True
        except TOOL_ERRORS as exc:
            outcome, ok = f"error: {exc}", False
        except (ValueError, KeyError, TypeError) as exc:
            outcome, ok = f"error: bad arguments for {name}: {exc}", False
        writes = self.browser.take_writes()
        undeclared = [w for w in writes if w.declared is None]
        if undeclared:
            notes.append(
                "WARNING: that action sent a write that was not declared "
                f"({', '.join(f'{w.method} {w.url}' for w in undeclared)}). "
                "Use browser_submit for anything that saves or sends."
            )
        if seen.response is Response.NOTE:
            notes.append(seen.message)
        if notes:
            outcome = "\n".join([*notes, outcome])
        if progress:
            self.repeats.progress()
        return StepRecord(step, name, args, why, ok, outcome), escalate, ended_by

    def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        b = self.browser
        if name in ("browser_open", "browser_click", "browser_type", "browser_select", "login"):
            if name == "browser_open":
                page = b.open(str(args["url"]))
            elif name == "browser_click":
                page = b.click(int(args["element"]))
            elif name == "browser_type":
                page = b.type(int(args["element"]), str(args["text"]))
            elif name == "browser_select":
                page = b.select(int(args["element"]), str(args["option"]))
            else:
                page = b.login()
            return self._observed(page, f"browser {b.url}", "page loaded. " + _title(page))
        if name == "files_list":
            return self._observed(self.files.list(), "files list", "files listed")
        if name == "files_read":
            text = self.files.read(str(args["name"]))
            return self._observed(text, f"file {args['name']}", f"read {args['name']}")
        if name == "http_get":
            params = json.loads(str(args.get("params_json") or "{}"))
            text = self.http.get(str(args["operation_id"]), params)
            return self._observed(text, f"API {args['operation_id']}", text.splitlines()[0])
        if name == "finish":
            return "finished"
        raise ValueError(f"unknown action {name!r}")

    def _commit(self, step: int, name: str, args: dict[str, Any]) -> Committed:
        """A declared write: through the gateway, which journals it around the tool."""
        decl = declaration_from(
            name,
            args,
            run_id=self.state.run_id,
            reference=self.state.reference,
            channel=self.settings.notify_channel,
            step=step,
        )
        fire = self._fire_for(name, args, decl.idempotency_key)
        committed = self.gateway.commit(step, decl, fire)
        page = committed.observation
        if page and name == "notify":
            committed.outcome = f"{committed.outcome} {page}"
        elif page and name == "browser_submit":
            extra = self._observed(page, f"browser {self.browser.url}", "")
            committed.outcome = f"{committed.outcome} {_title(page)} {extra}".strip()
        elif page:
            committed.outcome = f"{committed.outcome} {page.splitlines()[0]}"
            self._observed(page, f"API {args.get('operation_id')}", "")
        return committed

    def _fire_for(self, name: str, args: dict[str, Any], key: str) -> Callable[[], Fired]:
        """Prepare the tool call. Bad arguments fail here, before anything is journaled."""
        if name == "browser_submit":
            element = int(args["element"])

            def submit() -> Fired:
                try:
                    page, error = self.browser.submit(element), ""
                except BrowserError as exc:
                    page, error = "", str(exc)
                sent = [w.status for w in self.browser.peek_writes() if w.declared == "submit"]
                return Fired(page, sent, error)

            return submit
        if name == "http_write":
            operation = str(args["operation_id"])
            params = json.loads(str(args.get("params_json") or "{}"))
            body = json.loads(str(args.get("body_json") or "{}"))
            self.http.check_write(operation)
            return lambda: _http_fired(lambda: self.http.write(operation, params, body, key))
        if name == "notify":
            message = str(args["message"])
            return lambda: _http_fired(lambda: self.notify.post(message, key))
        raise ValueError(f"{name!r} is not a commit")

    def _lookup(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        """Records for the gateway's natural-key check and for reconcile."""
        try:
            if spec.get("source") == "notify":
                return self.notify.messages()
            data = self.http.get_json(str(spec.get("operation")), dict(spec.get("params") or {}))
        except (HttpError, NotifyError, httpx.HTTPError, ValueError) as exc:
            raise Unanswerable(f"the lookup failed: {exc}") from exc
        if isinstance(data, dict):
            data = [data]  # a read of one record
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise Unanswerable("the lookup did not return records")
        return data

    def _journal_lines(self) -> list[str]:
        return [
            f"- {e.state.value}: {e.description or e.action} (attempt {e.attempt_no}; {e.note})"
            for e in self.journal.entries()
        ]

    def _observed(self, text: str, source: str, outcome: str) -> str:
        self.state.observation, self.state.observation_source = text, source
        downloads = [ln for ln in text.splitlines() if ln.startswith("DOWNLOADED")]
        return " ".join([outcome, *downloads]).strip()

    def _violation(self, write: WriteRecord) -> None:
        message = f"undeclared write {write.method} {write.url}"
        self.state.violations.append(message)
        self._log({"event": "integrity_violation", "detail": message})

    # -- ending and logging -------------------------------------------------------
    def _end(self, status: RunStatus, summary: str, ended_by: str) -> RunState:
        """End the run. `ended_by` says why, for the receipt: finish, step_budget,
        time_budget, token_budget, model_error, repeats, reconcile or unclear_effect."""
        self.state.status, self.state.summary = status.value, summary
        self.state.ended_by = ended_by
        self.store.set_status(self.state.run_id, status, summary)
        self.store.save_checkpoint(
            self.state.run_id, len(self.state.history), self.state.to_checkpoint()
        )
        record = {
            "run_id": self.state.run_id,
            "task": self.state.task,
            "status": status.value,
            "ended_by": ended_by,
            "summary": summary,
            "reference": self.state.reference,
            "steps": len(self.state.history),
            "resumes": self.state.resumes,
            "model_calls": self.state.model_calls,
            "tokens": self.state.tokens,
            "violations": self.state.violations,
            "facts": {k: asdict(f) for k, f in self.state.facts.items()},
            "journal": self._journal_lines(),
        }
        (self.run_dir / "summary.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        ended = {"status": status.value, "ended_by": ended_by}
        self.store.add_event(self.state.run_id, "ended", ended)
        self._log({"event": "run_ended", **ended, "summary": summary})
        return self.state

    def _log(self, event: dict[str, Any]) -> None:
        event = {"at": datetime.now(UTC).isoformat(timespec="seconds"), **event}
        with (self.run_dir / "steps.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def _http_fired(send: Callable[[], tuple[int, str]]) -> Fired:
    """Turn an HTTP write into a dispatch outcome. A failed connection sent nothing; a
    timeout or dropped connection after connecting may have been received."""
    try:
        status, text = send()
    except httpx.ConnectError as exc:
        return Fired("", [], f"could not connect: {exc}")
    except httpx.HTTPError as exc:
        return Fired("", [None], f"no answer: {exc}")
    return Fired(text, [status])


def _title(page: str) -> str:
    for line in page.splitlines()[:3]:
        if line.startswith("TITLE:"):
            return line
    return page.splitlines()[0][:120] if page else ""
