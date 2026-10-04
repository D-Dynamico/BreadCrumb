"""The model client: one small interface, one Gemini implementation.

Each call sends a system prompt, one user prompt and the action schemas, and
forces the model to answer with exactly one action. Calls are stateless
(`store=False`): the executor rebuilds the whole context every turn, so nothing
depends on conversation history kept by the provider.

The client also:
- paces itself under the provider's requests-per-minute limit,
- retries rate limits, server errors and dropped connections with backoff,
  never other errors,
- gives every request a hard deadline: a request that hangs counts as a
  dropped connection and is retried, so a run can never stall in one call,
- keeps a dev cache keyed by the exact request (D15), and
- counts real requests per day in `runs/llm_usage.json`.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import TimeoutError as Deadline
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import httpx
from google import genai

from breadcrumb.config import Settings

RETRYABLE = {429, 500, 502, 503, 504}
CALL_DEADLINE_SECONDS = 120.0


@dataclass(frozen=True)
class Action:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0
    cached: bool = False

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.thought_tokens


class ModelError(Exception):
    """The model could not produce an action (after retries where they make sense)."""


class ModelClient(Protocol):
    def decide(
        self, system: str, prompt: str, actions: list[dict[str, Any]]
    ) -> tuple[Action, Usage]: ...


def _status(exc: Exception) -> int | None:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def within_deadline(call: Any, seconds: float) -> Any:
    """Run a blocking call, giving up after `seconds` (raises Deadline).

    The SDK's own timeout was seen to be ignored while the API held a request open,
    so the deadline is enforced here. The call runs in a daemon thread: one that
    overruns is abandoned, its answer discarded, and it never keeps the process alive.
    """
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # handed back to the caller below
            box["error"] = exc

    worker = threading.Thread(target=run, name="model-call", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise Deadline(f"no answer within {seconds:.0f} seconds")
    if "error" in box:
        raise box["error"]
    return box["value"]


class GeminiClient:
    def __init__(self, settings: Settings) -> None:
        self.model = settings.llm_model
        self.thinking_level = settings.llm_thinking_level
        self.use_cache = settings.dev_cache
        self.min_interval = 60.0 / max(settings.llm_requests_per_minute, 1)
        self.cache_dir = settings.runs_dir / "llm_cache"
        self.usage_file = settings.runs_dir / "llm_usage.json"
        self._client = genai.Client(api_key=settings.llm_api_key.get_secret_value())
        self._last_call = 0.0
        self.deadline = CALL_DEADLINE_SECONDS

    def decide(
        self, system: str, prompt: str, actions: list[dict[str, Any]]
    ) -> tuple[Action, Usage]:
        request = {
            "model": self.model,
            "thinking": self.thinking_level,
            "system": system,
            "prompt": prompt,
            "actions": actions,
        }
        key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        cached = self._cache_get(key)
        if cached is not None:
            return Action(cached["name"], cached["args"]), Usage(cached=True)
        action, usage = self._call(system, prompt, actions)
        self._cache_put(key, {"name": action.name, "args": action.args})
        return action, usage

    # -- the real call ------------------------------------------------------
    def _call(
        self, system: str, prompt: str, actions: list[dict[str, Any]]
    ) -> tuple[Action, Usage]:
        tools = [{"type": "function", **a} for a in actions]
        delay = 5.0
        for attempt in range(6):
            self._pace()
            try:
                result: Any = within_deadline(
                    lambda: self._client.interactions.create(
                        model=self.model,
                        input=prompt,
                        system_instruction=system,
                        tools=tools,
                        generation_config={
                            "tool_choice": "any",
                            "thinking_level": self.thinking_level,
                        },
                        store=False,
                    ),
                    self.deadline,
                )
            except Exception as exc:
                status = _status(exc)
                dropped = status is None and isinstance(exc, httpx.TransportError | Deadline)
                if (status in RETRYABLE or dropped) and attempt < 5:
                    time.sleep(delay)
                    delay = min(delay * 2, 60.0)
                    continue
                raise ModelError(f"model call failed ({status}): {exc}") from exc
            finally:
                self._count_request()
            calls = [s for s in (result.steps or []) if s.type == "function_call"]
            if not calls:
                raise ModelError("the model answered without choosing an action")
            call: Any = calls[0]
            usage = result.usage
            return Action(str(call.name), dict(call.arguments or {})), Usage(
                input_tokens=(usage.total_input_tokens or 0) if usage else 0,
                output_tokens=(usage.total_output_tokens or 0) if usage else 0,
                thought_tokens=(usage.total_thought_tokens or 0) if usage else 0,
            )
        raise ModelError("model call failed after retries")

    def _pace(self) -> None:
        wait = self._last_call + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _count_request(self) -> None:
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        if self.usage_file.exists():
            counts = json.loads(self.usage_file.read_text(encoding="utf-8"))
        today = date.today().isoformat()
        counts[today] = counts.get(today, 0) + 1
        self.usage_file.write_text(json.dumps(counts, indent=1), encoding="utf-8")

    # -- dev cache -----------------------------------------------------------
    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        path = self._cache_path(key)
        if not self.use_cache or not path.exists():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def _cache_put(self, key: str, value: dict[str, Any]) -> None:
        if not self.use_cache:
            return
        path = self._cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")


def requests_today(settings: Settings) -> int:
    path = settings.runs_dir / "llm_usage.json"
    if not path.exists():
        return 0
    counts: dict[str, int] = json.loads(path.read_text(encoding="utf-8"))
    return counts.get(date.today().isoformat(), 0)


def make_client(settings: Settings) -> ModelClient:
    if settings.llm_provider != "gemini":
        raise ValueError(f"unsupported LLM_PROVIDER {settings.llm_provider!r}")
    return GeminiClient(settings)
