"""Settings for the worker, read from the environment and `.env`.

The worker knows the company's systems the way a new employee would: their names,
URLs, its own logins and an API token. Nothing here is specific to any task.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AppAccess:
    name: str
    description: str
    url: str
    username: str
    password: SecretStr

    @property
    def origin(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    llm_provider: str = "gemini"
    llm_model: str
    llm_api_key: SecretStr
    llm_thinking_level: str = "low"
    llm_requests_per_minute: int = 15
    dev_cache: bool = True

    mailbox_url: str = "http://localhost:8101"
    mailbox_user: str = "ops@acme.test"
    mailbox_password: SecretStr = SecretStr("")
    vendor_portal_url: str = "http://localhost:8102"
    vendor_portal_user: str = ""
    vendor_portal_password: SecretStr = SecretStr("")
    admin_url: str = "http://localhost:8103"
    admin_user: str = ""
    admin_password: SecretStr = SecretStr("")
    api_base_url: str = "http://localhost:8103/api"
    admin_api_token: SecretStr = SecretStr("")
    notify_url: str = "http://localhost:8103/api/messages"
    notify_channel: str = "finance-ops"

    approval_threshold_inr: int = 100000
    max_steps: int = 60
    max_wall_seconds: int = 900
    max_tokens: int = 400000
    runs_dir: Path = ROOT / "runs"
    headless: bool = True
    lease_timeout_seconds: int = 30
    heartbeat_seconds: int = 5
    crash_point: str = ""

    @property
    def runs_db(self) -> Path:
        return self.runs_dir / "runs.db"

    @property
    def apps(self) -> list[AppAccess]:
        return [
            AppAccess(
                "Mail",
                "the shared operations inbox: where requests, documents and notices from "
                "inside and outside the company arrive, with attachments; also used to send mail",
                self.mailbox_url,
                self.mailbox_user,
                self.mailbox_password,
            ),
            AppAccess(
                "Vendor portal",
                "an external site where some suppliers publish documents for the company",
                self.vendor_portal_url,
                self.vendor_portal_user,
                self.vendor_portal_password,
            ),
            AppAccess(
                "Admin",
                "the internal system of record (payables, vendors, people, tickets, team "
                "messages). It holds work that has already been entered, not new incoming items",
                self.admin_url,
                self.admin_user,
                self.admin_password,
            ),
        ]


@cache
def settings() -> Settings:
    return Settings()  # required fields come from .env
