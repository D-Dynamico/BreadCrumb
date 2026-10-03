"""Notify tool: post a message to the requester's channel.

Every message ends with the run's visible reference token (`Ref: BC-7F3K`), so a
later look at the channel can tell whether this run already posted it
(DURABILITY.md, reconcile for messages).
"""

from __future__ import annotations

import httpx


class NotifyError(Exception):
    """Shown to the model as the outcome of the action."""


class NotifyTool:
    def __init__(self, url: str, channel: str, token: str, reference: str) -> None:
        self.url = url
        self.channel = channel
        self.reference = reference
        self._headers = {"Authorization": f"Bearer {token}"}

    def post(self, message: str, idempotency_key: str) -> str:
        body = {"channel": self.channel, "body": f"{message.strip()}\n\nRef: {self.reference}"}
        headers = {**self._headers, "Idempotency-Key": idempotency_key}
        response = httpx.post(self.url, json=body, headers=headers, timeout=15)
        if response.status_code >= 400:
            raise NotifyError(f"posting failed with HTTP {response.status_code}: {response.text}")
        return f"Posted to #{self.channel} (message {response.json().get('id')})."
