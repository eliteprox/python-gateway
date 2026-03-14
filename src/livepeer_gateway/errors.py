from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


class LivepeerGatewayError(RuntimeError):
    """Base error for the library."""


@dataclass
class OrchestratorRejection:
    """Records a single orchestrator that was tried and rejected."""
    url: str
    reason: str


class NoOrchestratorAvailableError(LivepeerGatewayError):
    """Raised when no orchestrator could be selected."""

    def __init__(self, message: str, rejections: list[OrchestratorRejection] | None = None) -> None:
        super().__init__(message)
        self.rejections: list[OrchestratorRejection] = rejections or []

    def __str__(self) -> str:
        base = super().__str__()
        if not self.rejections:
            return base

        lines = [base, "Attempt details:"]
        for idx, rejection in enumerate(self.rejections, start=1):
            lines.append(f"  {idx}. orch={rejection.url} | {rejection.reason}")
        return "\n".join(lines)


class SignerRefreshRequired(LivepeerGatewayError):
    """Raised when the remote signer returns HTTP 480 and a refresh is required."""


class SkipPaymentCycle(LivepeerGatewayError):
    """Raised when the signer returns HTTP 482 to skip a payment cycle."""


class PaymentError(LivepeerGatewayError):
    """Raised when a PaymentSession operation fails."""
