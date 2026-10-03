"""Email delivery seam. No credentials, network delivery or OTP logging in Alpha."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Protocol


class EmailDeliveryUnavailable(RuntimeError):
    """Safe error; provider exceptions must never escape into HTTP or logs."""


class EmailSender(Protocol):
    def send_verification(self, *, email: str, code: str, expires_in_s: int) -> None:
        """Deliver the code; raise on failure. Future production adapters implement this."""


@dataclass(frozen=True)
class VerificationEmail:
    email: str
    code: str = field(repr=False)
    expires_in_s: int


class MockEmailSender:
    """Deterministic in-process outbox for tests/dev; never prints or persists codes.

    Codes are still generated securely by the auth service. Tests consume the
    outbox directly instead of relying on network, timers or provider credentials.
    There is deliberately no HTTP endpoint exposing this outbox.
    """

    def __init__(self) -> None:
        self.messages: list[VerificationEmail] = []
        self._lock = threading.Lock()

    def send_verification(self, *, email: str, code: str, expires_in_s: int) -> None:
        with self._lock:
            self.messages.append(VerificationEmail(email, code, expires_in_s))

    def latest_code(self, email: str) -> str:
        with self._lock:
            return next(
                message.code for message in reversed(self.messages) if message.email == email
            )


class UnconfiguredEmailSender:
    def send_verification(self, *, email: str, code: str, expires_in_s: int) -> None:
        raise EmailDeliveryUnavailable("verification email delivery unavailable")
