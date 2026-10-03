"""Notify tool: post a message to the requester's channel.

Every message ends with the run's visible reference token (`Ref: BC-7F3K`), so a
later look at the channel can tell whether this run already posted it
(DURABILITY.md, reconcile for messages).
"""

from __future__ import annotations

import re
from typing import Any

import httpx

_REF = re.compile(r"Ref: (BC-[A-Z0-9]+)\s*$")


class NotifyError(Exception):
    """Shown to the model as the outcome of the action."""


class NotifyTool:
    def __init__(self, url: str, channel: str, token: str, reference: str) -> None:
        self.url = url
        self.channel = channel
        self.reference = reference
        self._headers = {"Authorization": f"Bearer {token}"}

    def post(self, message: str, idempotency_key: str) -> tuple[int, str]:
        """Send the message. Returns the status and what to tell the model."""
        body = {"channel": self.channel, "body": f"{message.strip()}\n\nRef: {self.reference}"}
        headers = {**self._headers, "Idempotency-Key": idempotency_key}
        response = httpx.post(self.url, json=body, headers=headers, timeout=15)
        if response.status_code >= 400:
            return response.status_code, f"posting failed: {response.text[:300]}"
        return (
            response.status_code,
            f"Posted to #{self.channel} (message {response.json().get('id')}).",
        )

    def messages(self) -> list[dict[str, Any]]:
        """The channel's messages, each with the reference token it carries (or "")."""
        response = httpx.get(
            self.url, params={"channel": self.channel}, headers=self._headers, timeout=15
        )
        if response.status_code >= 400:
            raise NotifyError(f"reading #{self.channel} failed with HTTP {response.status_code}")
        found = []
        for message in response.json():
            match = _REF.search(str(message.get("body", "")))
            found.append({**message, "channel": self.channel, "ref": match[1] if match else ""})
        return found
