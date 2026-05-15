"""Thin interaction-mode plugins: POST /v1/cap with Livepeer-* headers + payment."""

from __future__ import annotations

import json
from typing import ClassVar, Optional, Protocol, runtime_checkable

import httpx

from .broker_http import build_livepeer_headers, post_v1_cap
from .errors import LivepeerGatewayError
from .registry_types import RegistryRouteCandidate


@runtime_checkable
class RegistryModePlugin(Protocol):
    interaction_mode: str

    def supports(self, candidate: RegistryRouteCandidate) -> bool:
        ...

    def post_cap(
        self,
        worker_url: str,
        capability_id: str,
        offering_id: str,
        payment_b64: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        request_id: str | None = None,
        timeout_s: float = 120.0,
    ) -> httpx.Response:
        ...


class _BrokerModeBase:
    interaction_mode: ClassVar[str]
    _accept: ClassVar[Optional[str]] = None
    _default_body: ClassVar[Optional[bytes]] = None
    _default_content_type: ClassVar[Optional[str]] = None

    def supports(self, candidate: RegistryRouteCandidate) -> bool:
        return candidate.interaction_mode == self.interaction_mode

    def post_cap(
        self,
        worker_url: str,
        capability_id: str,
        offering_id: str,
        payment_b64: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        request_id: str | None = None,
        timeout_s: float = 120.0,
    ) -> httpx.Response:
        h = build_livepeer_headers(
            mode=self.interaction_mode,
            capability_id=capability_id,
            offering_id=offering_id,
            payment_b64=payment_b64,
            request_id=request_id,
        )
        eff_body = self._default_body if body is None else body
        eff_ct = self._default_content_type if content_type is None else content_type
        return post_v1_cap(
            worker_url,
            h,
            body=eff_body,
            content_type=eff_ct,
            accept=self._accept,
            timeout_s=timeout_s,
        )


class HttpReqrespPlugin(_BrokerModeBase):
    interaction_mode = "http-reqresp@v0"


class HttpStreamPlugin(_BrokerModeBase):
    interaction_mode = "http-stream@v0"
    _accept = "text/event-stream"


class HttpMultipartPlugin(_BrokerModeBase):
    interaction_mode = "http-multipart@v0"

    def post_cap(
        self,
        worker_url: str,
        capability_id: str,
        offering_id: str,
        payment_b64: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        request_id: str | None = None,
        timeout_s: float = 120.0,
    ) -> httpx.Response:
        if body is not None and not content_type:
            raise LivepeerGatewayError(
                "http-multipart@v0 requires content_type when body is set"
            )
        return super().post_cap(
            worker_url,
            capability_id,
            offering_id,
            payment_b64,
            body=body,
            content_type=content_type,
            request_id=request_id,
            timeout_s=timeout_s,
        )


class SessionControlExternalMediaPlugin(_BrokerModeBase):
    interaction_mode = "session-control-external-media@v0"
    _default_body = b"{}"
    _default_content_type = "application/json"


class RtmpIngressHlsEgressPlugin(_BrokerModeBase):
    interaction_mode = "rtmp-ingress-hls-egress@v0"
    _default_body = b"{}"
    _default_content_type = "application/json"


_REGISTRY_PLUGINS: tuple[RegistryModePlugin, ...] = (
    HttpReqrespPlugin(),
    HttpStreamPlugin(),
    HttpMultipartPlugin(),
    SessionControlExternalMediaPlugin(),
    RtmpIngressHlsEgressPlugin(),
)


def plugin_for_candidate(candidate: RegistryRouteCandidate) -> RegistryModePlugin:
    for p in _REGISTRY_PLUGINS:
        if p.supports(candidate):
            return p
    raise LivepeerGatewayError(
        f"No registry mode plugin for interaction_mode={candidate.interaction_mode!r}"
    )


def registry_dispatch_cap(
    *,
    signer_url: str,
    candidate: RegistryRouteCandidate,
    signer_headers: Optional[dict[str, str]] = None,
    face_value_wei: Optional[int] = None,
    registry_price_per_unit_wei: Optional[int] = None,
    work_units: Optional[int] = None,
    manifest_id: Optional[str] = None,
    body: bytes | None = None,
    content_type: str | None = None,
    request_id: str | None = None,
    timeout_s: float = 120.0,
) -> httpx.Response:
    """
    Acquire a registry-mode payment from the signer, then POST ``/v1/cap`` on
    ``candidate.worker_url`` using the mode plugin for ``candidate.interaction_mode``.
    """
    from .registry_payment_session import RegistryPaymentSession

    ps = RegistryPaymentSession(
        signer_url,
        candidate,
        signer_headers=signer_headers,
        face_value_wei=face_value_wei,
        registry_price_per_unit_wei=registry_price_per_unit_wei,
        work_units=work_units,
    )
    if manifest_id:
        ps.set_manifest_id(manifest_id)
    pay = ps.get_payment()
    plugin = plugin_for_candidate(candidate)
    return plugin.post_cap(
        candidate.worker_url,
        candidate.capability_id,
        candidate.offering_id,
        pay.payment,
        body=body,
        content_type=content_type,
        request_id=request_id,
        timeout_s=timeout_s,
    )


def parse_session_open_json(resp: httpx.Response) -> dict[str, object]:
    """
    Parse JSON session-open body (session-control-external-media / rtmp-ingress-hls-egress).
    Expects 2xx (typically 202); raises LivepeerGatewayError otherwise.
    """
    if resp.status_code >= 400:
        tail = (resp.text or "")[:800]
        raise LivepeerGatewayError(f"session-open failed HTTP {resp.status_code}: {tail!r}")
    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        raise LivepeerGatewayError(f"session-open invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise LivepeerGatewayError("session-open JSON must be an object")
    return data
