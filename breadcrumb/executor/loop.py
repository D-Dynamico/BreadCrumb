"""The executor: compile the contract, then observe, decide one action, act, record.

A run starts by turning the request into a contract (AGENT_DESIGN.md section 1); a
blocking question waits for the user's answer first. Each turn the model chooses one
action. Reads go straight to the tools, with the recovery table retrying transient
failures and signing in again when a session expires. Commits go through the
gateway, which checks them against the contract, asks for approval when the risk
tier says so, and journals them before and after they touch the world. Every step is
checkpointed, so `Executor.resume` can take over a run killed at any moment.

`finish` asks the verifier, which checks every contract check against fresh reads of
the apps. All verified: DONE. A failure: one repair pass, then DONE or FAILED. Some
checks unverifiable: FINISHED, which never claims more than was checked. Every run
ends with a receipt.
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
from breadcrumb.contract.compiler import compile_contract
from breadcrumb.contract.model import ApiSpec, Contract, derive_checks
from breadcrumb.executor.actions import ACTIONS, WRITE_ACTIONS
from breadcrumb.executor.context import build_prompt
from breadcrumb.executor.repeats import Check, RepeatGuard, Response, fingerprint
from breadcrumb.executor.state import PlanStep, RunState, StepRecord
from breadcrumb.gateway.declare import Declaration, GatewayRefusal, Policy, declare
from breadcrumb.gateway.gateway import Committed, Fired, Gateway
from breadcrumb.journal.journal import Entry, Journal
from breadcrumb.journal.machine import State, Unanswerable
from breadcrumb.ledger.values import kind_of
from breadcrumb.llm.client import ModelClient, ModelError, Usage
from breadcrumb.receipt import receipt
from breadcrumb.recovery.policy import MAX_RETRIES, Situation, Strategy, choose
from breadcrumb.runs import waits
from breadcrumb.runs.crash import CrashPoints
from breadcrumb.runs.lease import Heartbeat
from breadcrumb.runs.store import RunStatus, RunStore, Wait
from breadcrumb.tools.browser import BrowserError, BrowserTool, WriteRecord
from breadcrumb.tools.files import FilesError, FilesTool
from breadcrumb.tools.http import HttpError, HttpTool
from breadcrumb.tools.notify import NotifyError, NotifyTool
from breadcrumb.verifier.verifier import overall, verify

SYSTEM_PROMPT = ROOT / "prompts" / "executor.txt"
TOOL_ERRORS = (BrowserError, FilesError, HttpError, NotifyError, httpx.HTTPError, GatewayRefusal)
StepPrinter = Callable[[StepRecord], None]
Notice = Callable[[str], None]
MAX_CLARIFICATIONS = 2
LAST_FILE_CHARS = 3000


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{secrets.token_hex(2)}"


def new_reference() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "BC-" + "".join(secrets.choice(alphabet) for _ in range(4))


def _leaves(value: Any, path: str = "") -> list[tuple[str, str]]:
    """Every scalar in a JSON body, with its path: what an API write actually sends."""
    if isinstance(value, dict):
        return [x for k, v in value.items() for x in _leaves(v, f"{path}.{k}".strip("."))]
    if isinstance(value, list):
        return [x for i, v in enumerate(value) for x in _leaves(v, f"{path}[{i}]")]
    return [(path, str(value))] if value is not None else []


def _sensitive(diff: dict[str, Any]) -> tuple[Any, ...]:
    """What an approval is really about: the record, its values, and every amount, date
    or address typed. Wording and free text may differ when a form is refilled."""
    typed = sorted(str(v) for _, v in diff.get("typed") or [] if kind_of(v) != "text")
    return (diff.get("deliverable"), diff.get("key"), diff.get("values"), typed)


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
        self.crash = CrashPoints("" if resumed else settings.crash_point)
        self.gateway = Gateway(self.journal, self._lookup, self.crash, self._approve)
        self.policy = Policy(float(settings.approval_threshold_inr), settings.internal_email_domain)
        self.heartbeat = Heartbeat(store, self.state.run_id, self.pid, settings.heartbeat_seconds)
        # New for every session of the run: re-observing after a resume is not a repeat.
        self.repeats = RepeatGuard()
        self.waited = 0.0  # seconds spent waiting for people, not counted as work time

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

    # -- the run -------------------------------------------------------------------
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
            if self.state.contract is None:
                ended = self._make_contract()
                if ended is not None:
                    return ended
            self._deliver_answers()
            return self._loop()
        finally:
            self.heartbeat.stop()

    def _status(self, status: RunStatus) -> None:
        self.state.status = status.value
        self.store.set_status(self.state.run_id, status)

    def _contract(self) -> Contract | None:
        return Contract.model_validate(self.state.contract) if self.state.contract else None

    def _count(self, usage: Usage) -> None:
        self.state.tokens += usage.total
        self.state.model_calls += 0 if usage.cached else 1

    # -- the contract ----------------------------------------------------------------
    def _make_contract(self) -> RunState | None:
        self._status(RunStatus.COMPILING)
        try:
            spec = ApiSpec.from_openapi(self.http.openapi())
        except (httpx.HTTPError, ValueError) as exc:
            return self._end(RunStatus.FAILED, f"Stopped: could not read the API spec ({exc}).",
                             "contract")  # fmt: skip
        task = self.state.task
        for round_no in range(MAX_CLARIFICATIONS + 1):
            try:
                compiled = compile_contract(self.model, task, self.settings.apps, spec)
            except ModelError as exc:
                return self._end(RunStatus.FAILED, f"Stopped: {exc}", "model_error")
            for usage in compiled.usages:
                self._count(usage)
            if compiled.contract is None:
                reasons = "; ".join(compiled.errors[:5])
                return self._end(
                    RunStatus.FAILED,
                    f"Stopped: could not make a valid contract from the request ({reasons}).",
                    "contract",
                )
            questions = compiled.contract.blocking_questions
            if not questions:
                break
            if round_no == MAX_CLARIFICATIONS:
                return self._end(
                    RunStatus.ESCALATED,
                    "Stopped: the request is still unclear after asking: " + " ".join(questions),
                    "unanswered",
                )
            answer = self._ask("Before I start: " + " ".join(questions))
            task = f"{task}\n\nThe user clarified: {answer}"
        contract = compiled.contract
        assert contract is not None
        self.state.task = task
        self.state.contract = contract.model_dump()
        for check in derive_checks(contract):
            if (
                check.type == "field_unchanged"
                and check.lookup_operation not in self.state.snapshots
            ):
                try:
                    self.state.snapshots[check.lookup_operation] = self._lookup(
                        {"source": "api", "operation": check.lookup_operation, "params": {}}
                    )
                except Unanswerable as exc:
                    self._log({"event": "snapshot_failed", "detail": str(exc)})
        self.store.save_checkpoint(self.state.run_id, 0, self.state.to_checkpoint())
        summary = {
            "goal": contract.goal,
            "deliverables": [f"{d.id} ({d.kind})" for d in contract.deliverables],
            "facts": [f.key for f in contract.facts],
        }
        self.store.add_event(self.state.run_id, "contract", summary)
        self._log({"event": "contract", "contract": self.state.contract})
        self.notice(f"Contract: {contract.goal}")
        for d in contract.deliverables:
            self.notice(f"  deliverable {d.id}: {d.kind}, {d.description}")
        self.notice(f"  checks: {len(derive_checks(contract))}\n")
        self._status(RunStatus.RUNNING)
        return None

    # -- durable waits (D35) -----------------------------------------------------------
    def _wait_for(self, wait: Wait, status: RunStatus) -> Wait:
        """Block until a person decides. The run keeps its lease while it waits."""
        previous = RunStatus(self.state.status)
        self._status(status)
        command = "answer" if wait.kind == "question" else "approve"
        self.notice(f"\n{waits.describe(wait)}")
        self.notice(f"Waiting. Decide with: uv run breadcrumb {command} {self.state.run_id}")
        started = time.monotonic()
        while wait.status == "pending":
            time.sleep(self.settings.wait_poll_seconds)
            got = self.store.get_wait(wait.wait_id)
            assert got is not None
            wait = got
        self.waited += time.monotonic() - started
        self.notice(f"Decision: {wait.status}{': ' + wait.answer if wait.answer else ''}\n")
        self._status(previous if previous not in (status,) else RunStatus.RUNNING)
        return wait

    def _ask(self, question: str) -> str:
        wait = self.store.add_wait(
            "wq-" + secrets.token_hex(4), self.state.run_id, "question", {"question": question}
        )
        wait = self._wait_for(wait, RunStatus.AWAITING_CLARIFICATION)
        self.state.delivered.append(wait.wait_id)
        return wait.answer

    def _approve(self, entry: Entry, decl: Declaration) -> tuple[bool, str]:
        """The gateway's approver: the requester sees the exact diff and decides."""
        diff = decl.diff
        wait = None
        for earlier in self.store.waits(self.state.run_id):
            if earlier.kind != "approval" or earlier.entry_id != entry.entry_id:
                continue
            if _sensitive(earlier.payload) != _sensitive(diff):
                continue  # something changed since: ask again
            if earlier.status == "approved":
                return True, f"approved earlier ({earlier.wait_id})"
            if earlier.status == "rejected":
                return False, earlier.answer
            wait = earlier
        if wait is None:
            wait = self.store.add_wait(
                "wa-" + secrets.token_hex(4), self.state.run_id, "approval", diff, entry.entry_id
            )
            self.store.save_checkpoint(self.state.run_id, len(self.state.history),
                                       self.state.to_checkpoint())  # fmt: skip
            self.crash.waiting_for_approval()
        wait = self._wait_for(wait, RunStatus.AWAITING_APPROVAL)
        if wait.status == "rejected":
            self._user_answer(wait.answer or "rejected, no reason given", wait.wait_id)
            return False, wait.answer
        return True, ""

    def _user_answer(self, text: str, wait_id: str) -> None:
        n = sum(1 for k in self.state.facts if k.startswith("user_answer_")) + 1
        self.state.remember(
            [{"key": f"user_answer_{n}", "value": text, "source": "user", "type": "text"}],
            len(self.state.history),
        )
        if wait_id not in self.state.delivered:
            self.state.delivered.append(wait_id)

    def _deliver_answers(self) -> None:
        """After a resume: pass on decisions made while no worker was running, and keep
        waiting for a question that is still open."""
        lines = []
        for wait in self.store.waits(self.state.run_id):
            if wait.wait_id in self.state.delivered:
                continue
            if wait.kind == "question":
                if wait.status == "pending":
                    wait = self._wait_for(wait, RunStatus.AWAITING_CLARIFICATION)
                self._user_answer(wait.answer, wait.wait_id)
                lines.append(f"- You asked: {wait.payload.get('question')} The user answered: "
                             f"{wait.answer}")  # fmt: skip
            elif wait.kind == "approval":
                what = wait.payload.get("description", "")
                lines.append(
                    f"- Approval for '{what}': {wait.status}"
                    + (" (do it again: fill in the same values and submit; it will go through)"
                       if wait.status == "approved" else "")
                    + (f" (the user said: {wait.answer})" if wait.answer else "")
                )  # fmt: skip
        if lines and self.resumed:
            self.state.interruption += "\nDecisions from the user:\n" + "\n".join(lines)

    # -- the loop ----------------------------------------------------------------------
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
                elapsed = earlier + time.monotonic() - started - self.waited
                if elapsed > self.settings.max_wall_seconds:
                    return self._end(RunStatus.FAILED, "Stopped: the time budget ran out.",
                                     "time_budget")  # fmt: skip
                if self.state.tokens > self.settings.max_tokens:
                    return self._end(RunStatus.FAILED, "Stopped: the token budget ran out.",
                                     "token_budget")  # fmt: skip
                prompt = build_prompt(
                    self.state,
                    self.settings.apps,
                    operations,
                    self.browser.url,
                    step,
                    self.settings.max_steps,
                    self._journal_lines(),
                    self.browser.typed,
                )
                try:
                    action, usage = self.model.decide(self.system, prompt, ACTIONS)
                except ModelError as exc:
                    return self._end(RunStatus.FAILED, f"Stopped: {exc}", "model_error")
                self._count(usage)
                state = fingerprint(
                    self.state.observation_source, self.state.observation, self.files.names()
                )
                seen = self._with_unvisited(
                    self.repeats.check(step, action.name, action.args, state)
                )
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
                    ended = self._verify(str(action.args.get("summary", "")))
                    if ended is not None:
                        return ended
            return self._end(RunStatus.FAILED, "Stopped: the step budget ran out.",
                             "step_budget")  # fmt: skip
        finally:
            self.browser.close()

    def _verify(self, summary: str) -> RunState | None:
        """The worker says it is done. The verifier decides (AGENT_DESIGN.md section 8)."""
        contract = self._contract()
        if contract is None:
            return self._end(RunStatus.FINISHED, summary, "finish")
        self._status(RunStatus.VERIFYING)
        verdicts = verify(
            derive_checks(contract),
            self.state.facts,
            self._lookup,
            self.state.reference,
            self.state.snapshots,
        )
        self.state.verdicts = [v.to_dict() for v in verdicts]
        result = overall(verdicts)
        self.store.add_event(self.state.run_id, "verified", {"result": result,
                             "checks": self.state.verdicts})  # fmt: skip
        self._log({"event": "verified", "result": result, "checks": self.state.verdicts})
        for v in verdicts:
            self.notice(f"  check {v.status:<12} {v.check}: {v.detail}")
        if result == "verified":
            return self._end(RunStatus.DONE, summary, "verified")
        problems = [f"- {v.check}: {v.detail}" for v in verdicts if v.status == "failed"]
        if result == "failed" and not self.state.repair_used:
            self.state.repair_used = True
            self.state.note = (
                "You called finish, but checking the apps found problems:\n"
                + "\n".join(problems)
                + "\nFix what you can, then call finish again. If something cannot be done, "
                "call finish and say exactly why."
            )
            self.notice("Verification failed; one repair pass.")
            self._status(RunStatus.RUNNING)
            return None
        if result == "failed":
            text = f"{summary}\nVerification failed:\n" + "\n".join(problems)
            return self._end(RunStatus.FAILED, text, "verification")
        unknown = [f"- {v.check}: {v.detail}" for v in verdicts if v.status == "unverifiable"]
        text = f"{summary}\nNot everything could be verified:\n" + "\n".join(unknown)
        return self._end(RunStatus.FINISHED, text, "unverified")

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
        self._status(RunStatus.RUNNING)
        return None

    # -- acting ------------------------------------------------------------------------
    def _act(
        self, step: int, name: str, args: dict[str, Any], seen: Check
    ) -> tuple[StepRecord, str, str]:
        """Do one action. Returns its record, and an escalation and its cause, if any."""
        why = str(args.get("why", ""))
        contract = self._contract()
        types: dict[str, str] = {f.key: f.type for f in contract.facts} if contract else {}
        notes = self.state.remember(list(args.get("remember") or []), step, types)
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
            elif name == "ask_user":
                answer = self._ask(str(args["question"]))
                self._user_answer(answer, "")
                outcome, ok = f"The user answered: {answer}", True
            else:
                outcome, ok = self._dispatch(name, args), True
        except TOOL_ERRORS as exc:
            outcome, ok = f"error: {exc}", False
        except (ValueError, KeyError, TypeError) as exc:
            outcome, ok = f"error: bad arguments for {name}: {exc}", False
        if seen.response not in (Response.REFUSE, Response.ESCALATE):
            ok, outcome = self._recover(name, args, ok, outcome, notes)
        self._note_visit()
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

    def _with_unvisited(self, seen: Check) -> Check:
        """A repeat note or refusal also names the systems not opened yet in this run."""
        if seen.response not in (Response.NOTE, Response.REFUSE):
            return seen
        unvisited = [a.name for a in self.settings.apps if a.name not in self.state.visited]
        if not unvisited:
            return seen
        hint = f" Systems you have not opened yet in this run: {', '.join(unvisited)}."
        return Check(seen.response, seen.message + hint)

    def _note_visit(self) -> None:
        url = self.browser.url
        for app in self.settings.apps:
            if url.startswith(app.origin) and app.name not in self.state.visited:
                self.state.visited.append(app.name)

    def _recover(
        self, name: str, args: dict[str, Any], ok: bool, outcome: str, notes: list[str]
    ) -> tuple[bool, str]:
        """Apply the recovery table: retry transient reads, sign in again when needed."""
        browser = name.startswith("browser") or name == "login"
        for attempt in range(MAX_RETRIES):
            situation = Situation(
                name,
                ok,
                outcome,
                self.browser.page_status if browser else None,
                self.browser.on_sign_in_page if browser else False,
            )
            strategy = choose(situation)
            if strategy is Strategy.RELOGIN:
                try:
                    page = self.browser.login()
                    outcome = self._observed(page, f"browser {self.browser.url}",
                                             "page loaded. " + _title(page))  # fmt: skip
                    notes.append("That page asked for a sign-in, so the system signed in "
                                 "for you and returned to it.")  # fmt: skip
                except BrowserError as exc:
                    notes.append(f"That page asked for a sign-in, and signing in failed: {exc}")
                    return ok, outcome
                continue  # the page after signing in may itself need a retry
            if strategy is not Strategy.RETRY:
                return ok, outcome
            time.sleep(2**attempt)
            try:
                if browser and (self.browser.page_status or 0) >= 500:
                    page = self.browser.reload()
                    outcome = self._observed(page, f"browser {self.browser.url}",
                                             "page loaded. " + _title(page))  # fmt: skip
                    if name == "login":
                        outcome = self._dispatch(name, args)
                else:
                    outcome = self._dispatch(name, args)
                ok = True
            except TOOL_ERRORS as exc:
                outcome, ok = f"error: {exc}", False
            notes.append(f"(retried after a transient error, attempt {attempt + 1})")
        return ok, outcome

    def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        b = self.browser
        if name in ("browser_open", "browser_view", "browser_click", "browser_type",
                    "browser_select", "login"):  # fmt: skip
            if name == "browser_view":
                page = b.observe()
            elif name == "browser_open":
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
            self.state.last_file = f"{args['name']}\n{text[:LAST_FILE_CHARS]}"
            return self._observed(
                text,
                f"file {args['name']}",
                f"read {args['name']}. Put the values you need in remember in your next "
                "action; the file stays visible under 'Last file read'.",
            )
        if name == "http_get":
            params = json.loads(str(args.get("params_json") or "{}"))
            text = self.http.get(str(args["operation_id"]), params)
            return self._observed(text, f"API {args['operation_id']}", text.splitlines()[0])
        if name == "finish":
            return "finished"
        raise ValueError(f"unknown action {name!r}")

    def _commit(self, step: int, name: str, args: dict[str, Any]) -> Committed:
        """A declared write: checked against the contract, then through the gateway."""
        contract = self._contract()
        if contract is None:
            raise GatewayRefusal("not done: there is no contract for this run")
        if name == "http_write":
            body = json.loads(str(args.get("body_json") or "{}"))
            typed = _leaves(body)
        elif name == "browser_submit":
            typed = self.browser.typed
        else:
            typed = []
        decl = declare(
            name,
            args,
            run_id=self.state.run_id,
            reference=self.state.reference,
            channel=self.settings.notify_channel,
            contract=contract,
            facts=self.state.facts,
            typed=typed,
            policy=self.policy,
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
                if sent and self.browser.on_sign_in_page:
                    # Sent, then shown a sign-in page: saved, or refused for lack of a
                    # session? Only looking can tell (the "maybe committed" case).
                    return Fired(page, [None], "landed on a sign-in page after sending")
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
        """Records for the natural-key check, reconcile and the verifier. Transient
        failures are retried; anything else means looking cannot answer."""
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                if spec.get("source") == "notify":
                    return self.notify.messages()
                data = self.http.get_json(str(spec.get("operation")),
                                          dict(spec.get("params") or {}))  # fmt: skip
            except (HttpError, NotifyError, httpx.HTTPError, ValueError) as exc:
                last = exc
                time.sleep(2**attempt)
                continue
            if isinstance(data, dict):
                data = [data]  # a read of one record
            if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
                raise Unanswerable("the lookup did not return records")
            return data
        raise Unanswerable(f"the lookup failed: {last}")

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

    # -- ending and logging ---------------------------------------------------------
    def _end(self, status: RunStatus, summary: str, ended_by: str) -> RunState:
        """End the run. `ended_by` says why (see breadcrumb.receipt.receipt.ENDED_BY)."""
        self.state.status, self.state.summary = status.value, summary
        self.state.ended_by = ended_by
        self.store.set_status(self.state.run_id, status, summary)
        self.store.save_checkpoint(
            self.state.run_id, len(self.state.history), self.state.to_checkpoint()
        )
        ended = {"status": status.value, "ended_by": ended_by}
        self.store.add_event(self.state.run_id, "ended", ended)
        self._log({"event": "run_ended", **ended, "summary": summary})
        built = receipt.build(
            self.state,
            self.journal.entries(),
            self.store.waits(self.state.run_id),
            self.store.events(self.state.run_id),
        )
        path = receipt.write(self.run_dir, built)
        (self.run_dir / "summary.json").write_text(
            json.dumps(built, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        self.notice(f"Receipt: {path}")
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
