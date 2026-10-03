"""The executor loop: observe, decide one action, act, record. Repeat.

Phase 2 shape: no journal or gateway yet (Phase 3), no contract or verifier yet
(Phase 4). Writes are still declared actions, and undeclared writes are reported.
`finish` ends the run as FINISHED, which means "the worker says it is done", not
"verified". Budgets end the run as FAILED with the reason.
"""

from __future__ import annotations

import json
import secrets
import string
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from breadcrumb.config import ROOT, Settings
from breadcrumb.executor.actions import ACTIONS
from breadcrumb.executor.context import build_prompt
from breadcrumb.executor.state import PlanStep, RunState, StepRecord
from breadcrumb.llm.client import ModelClient, ModelError
from breadcrumb.tools.browser import BrowserError, BrowserTool, WriteRecord
from breadcrumb.tools.files import FilesError, FilesTool
from breadcrumb.tools.http import HttpError, HttpTool
from breadcrumb.tools.notify import NotifyError, NotifyTool

SYSTEM_PROMPT = ROOT / "prompts" / "executor.txt"
TOOL_ERRORS = (BrowserError, FilesError, HttpError, NotifyError)
StepPrinter = Callable[[StepRecord], None]


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
        task: str,
        printer: StepPrinter | None = None,
    ) -> None:
        self.settings = settings
        self.model = model
        self.state = RunState(run_id=new_run_id(), task=task, reference=new_reference())
        self.run_dir: Path = settings.runs_dir / self.state.run_id
        self.printer = printer
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

    # -- the loop ----------------------------------------------------------------
    def run(self) -> RunState:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._log(
            {"event": "run_started", "task": self.state.task, "reference": self.state.reference}
        )
        started = time.monotonic()
        try:
            operations = self.http.describe()
        except Exception as exc:  # the API being down must not stop browser work
            operations = f"(API spec unavailable: {exc})"
        self.browser.start()
        try:
            for step in range(1, self.settings.max_steps + 1):
                if time.monotonic() - started > self.settings.max_wall_seconds:
                    return self._end("failed", "Stopped: the time budget ran out.")
                if self.state.tokens > self.settings.max_tokens:
                    return self._end("failed", "Stopped: the token budget ran out.")
                prompt = build_prompt(
                    self.state,
                    self.settings.apps,
                    operations,
                    self.browser.url,
                    step,
                    self.settings.max_steps,
                )
                try:
                    action, usage = self.model.decide(self.system, prompt, ACTIONS)
                except ModelError as exc:
                    return self._end("failed", f"Stopped: {exc}")
                self.state.tokens += usage.total
                self.state.model_calls += 0 if usage.cached else 1
                record = self._act(step, action.name, action.args)
                self.state.history.append(record)
                self._log({"event": "step", **asdict(record), "usage": asdict(usage)})
                if self.printer:
                    self.printer(record)
                if action.name == "finish":
                    return self._end("finished", str(action.args.get("summary", "")))
            return self._end("failed", "Stopped: the step budget ran out.")
        finally:
            self.browser.close()

    def _act(self, step: int, name: str, args: dict[str, Any]) -> StepRecord:
        why = str(args.get("why", ""))
        notes = self.state.remember(list(args.get("remember") or []), step)
        if args.get("plan"):
            self.state.plan = [
                PlanStep(str(s.get("title", "")), str(s.get("status", "todo")))
                for s in args["plan"]
            ]
        try:
            outcome = self._dispatch(step, name, args)
            ok = True
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
        if notes:
            outcome = "\n".join([*notes, outcome])
        return StepRecord(step, name, args, why, ok, outcome)

    def _dispatch(self, step: int, name: str, args: dict[str, Any]) -> str:
        b = self.browser
        if name in ("browser_open", "browser_click", "browser_type", "browser_select",
                    "browser_submit", "login"):  # fmt: skip
            if name == "browser_open":
                page = b.open(str(args["url"]))
            elif name == "browser_click":
                page = b.click(int(args["element"]))
            elif name == "browser_type":
                page = b.type(int(args["element"]), str(args["text"]))
            elif name == "browser_select":
                page = b.select(int(args["element"]), str(args["option"]))
            elif name == "browser_submit":
                page = b.submit(int(args["element"]))
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
        if name == "http_write":
            params = json.loads(str(args.get("params_json") or "{}"))
            body = json.loads(str(args.get("body_json") or "{}"))
            key = f"{self.state.run_id}:step-{step}"
            text = self.http.write(str(args["operation_id"]), params, body, key)
            return self._observed(text, f"API {args['operation_id']}", text.splitlines()[0])
        if name == "notify":
            return self.notify.post(str(args["message"]), f"{self.state.run_id}:step-{step}")
        if name == "finish":
            return "finished"
        raise ValueError(f"unknown action {name!r}")

    def _observed(self, text: str, source: str, outcome: str) -> str:
        self.state.observation, self.state.observation_source = text, source
        downloads = [ln for ln in text.splitlines() if ln.startswith("DOWNLOADED")]
        return " ".join([outcome, *downloads])

    def _violation(self, write: WriteRecord) -> None:
        message = f"undeclared write {write.method} {write.url}"
        self.state.violations.append(message)
        self._log({"event": "integrity_violation", "detail": message})

    # -- ending and logging -------------------------------------------------------
    def _end(self, status: str, summary: str) -> RunState:
        self.state.status, self.state.summary = status, summary
        record = {
            "run_id": self.state.run_id,
            "task": self.state.task,
            "status": status,
            "summary": summary,
            "reference": self.state.reference,
            "steps": len(self.state.history),
            "model_calls": self.state.model_calls,
            "tokens": self.state.tokens,
            "violations": self.state.violations,
            "facts": {k: asdict(f) for k, f in self.state.facts.items()},
        }
        (self.run_dir / "summary.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._log({"event": "run_ended", "status": status, "summary": summary})
        return self.state

    def _log(self, event: dict[str, Any]) -> None:
        event = {"at": datetime.now(UTC).isoformat(timespec="seconds"), **event}
        with (self.run_dir / "steps.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def _title(page: str) -> str:
    for line in page.splitlines()[:3]:
        if line.startswith("TITLE:"):
            return line
    return page.splitlines()[0][:120] if page else ""
