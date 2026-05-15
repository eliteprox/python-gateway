"""HTTP broker helpers: Livepeer-* headers and POST /v1/cap."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urljoin

import httpx

from .errors import LivepeerGatewayError

LIVEPEER_SPEC_VERSION = "0.1"

HEADER_CAPABILITY = "Livepeer-Capability"
HEADER_OFFERING = "Livepeer-Offering"
HEADER_PAYMENT = "Livepeer-Payment"
HEADER_SPEC_VERSION = "Livepeer-Spec-Version"
HEADER_MODE = "Livepeer-Mode"
HEADER_REQUEST_ID = "Livepeer-Request-Id"
HEADER_WORK_UNITS = "Livepeer-Work-Units"
HEADER_ERROR = "Livepeer-Error"


def broker_v1_cap_url(worker_base_url: str) -> str:
    base = worker_base_url.rstrip("/") + "/"
    return urljoin(base, "v1/cap")


def build_livepeer_headers(
    *,
    mode: str,
    capability_id: str,
    offering_id: str,
    payment_b64: str,
    request_id: str | None = None,
) -> dict[str, str]:
    h = {
        HEADER_CAPABILITY: capability_id,
        HEADER_OFFERING: offering_id,
        HEADER_PAYMENT: payment_b64,
        HEADER_SPEC_VERSION: LIVEPEER_SPEC_VERSION,
        HEADER_MODE: mode,
    }
    if request_id:
        h[HEADER_REQUEST_ID] = request_id
    return h


def parse_work_units(headers: Mapping[str, str]) -> int:
    raw = headers.get(HEADER_WORK_UNITS) or headers.get(HEADER_WORK_UNITS.lower())
    if raw is None:
        return 0
    try:
        return max(0, int(str(raw).strip(), 10))
    except ValueError:
        return 0


@dataclass(frozen=True)
class BrokerCapError(LivepeerGatewayError):
    status_code: int
    detail: str

    def __str__(self) -> str:
        return f"broker /v1/cap HTTP {self.status_code}: {self.detail}"


def post_v1_cap(
    worker_base_url: str,
    livepeer_headers: dict[str, str],
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    accept: str | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout_s: float = 120.0,
) -> httpx.Response:
    url = broker_v1_cap_url(worker_base_url)
    headers = dict(livepeer_headers)
    if content_type:
        headers["Content-Type"] = content_type
    if accept:
        headers["Accept"] = accept
    if extra_headers:
        headers.update(extra_headers)
    with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
        return client.post(url, headers=headers, content=body)
