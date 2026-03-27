from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import httpx

from .byoc_payments import BYOCPaymentSession
from .capabilities import CapabilityId, build_capabilities
from .control import Control
from .errors import (
    LivepeerGatewayError,
    NoOrchestratorAvailableError,
    OrchestratorRejection,
    SkipPaymentCycle,
)
from .events import Events
from .media_output import LagPolicy, MediaOutput
from .media_publish import MediaPublish, MediaPublishConfig
from .orchestrator import _extract_error_message, _http_origin
from .selection import orchestrator_selector

_LOG = logging.getLogger(__name__)


def _header_get(headers: dict[str, str], key: str) -> Optional[str]:
    key_lower = key.lower()
    for name, value in headers.items():
        if name.lower() == key_lower:
            return value
    return None


@dataclass(frozen=True)
class BYOCJobRequest:
    capability: str
    request_id: Optional[str] = None
    stream_id: Optional[str] = None
    request: Optional[dict[str, Any]] = None
    parameters: Optional[dict[str, Any]] = None
    body: Optional[dict[str, Any]] = None
    timeout_seconds: int = 30
    enable_video_ingress: bool = True
    enable_video_egress: bool = True
    enable_data_output: bool = False

    def _job_id(self) -> str:
        if self.request_id and self.request_id.strip():
            return self.request_id.strip()
        if self.stream_id and self.stream_id.strip():
            return self.stream_id.strip()
        return uuid.uuid4().hex

    def _request_json(self, job_id: str) -> str:
        payload: dict[str, Any] = {}
        if self.request:
            payload.update(self.request)
        payload.setdefault("stream_id", self.stream_id or job_id)
        return json.dumps(payload, separators=(",", ":"))

    def _parameters_json(self) -> str:
        payload: dict[str, Any] = {}
        if self.parameters:
            payload.update(self.parameters)
        payload.setdefault("enable_video_ingress", self.enable_video_ingress)
        payload.setdefault("enable_video_egress", self.enable_video_egress)
        payload.setdefault("enable_data_output", self.enable_data_output)
        return json.dumps(payload, separators=(",", ":"))

    def _body(self, job_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.body:
            payload.update(self.body)
        payload.setdefault("stream_id", self.stream_id or job_id)
        return payload


@dataclass(frozen=True)
class BYOCJob:
    raw: dict[str, Any]
    job_id: str
    capability: str
    publish_url: Optional[str] = None
    subscribe_url: Optional[str] = None
    control_url: Optional[str] = None
    events_url: Optional[str] = None
    data_url: Optional[str] = None
    control: Optional[Control] = None
    events: Optional[Events] = None
    _media: Optional[MediaPublish] = field(default=None, repr=False, compare=False)
    _payment_session: Optional[BYOCPaymentSession] = field(default=None, repr=False, compare=False)
    _signed_job_header: Optional[str] = field(default=None, repr=False, compare=False)
    _payment_task: Optional[asyncio.Task] = field(default=None, repr=False, compare=False)

    @staticmethod
    def from_start_response(
        data: dict[str, Any],
        *,
        job_id: str,
        capability: str,
        payment_session: Optional[BYOCPaymentSession] = None,
        signed_job_header: Optional[str] = None,
    ) -> "BYOCJob":
        headers = data.get("headers")
        if not isinstance(headers, dict):
            headers = {}

        control_url = _header_get(headers, "X-Control-Url")
        events_url = _header_get(headers, "X-Events-Url")
        publish_url = _header_get(headers, "X-Publish-Url")
        subscribe_url = _header_get(headers, "X-Subscribe-Url")
        data_url = _header_get(headers, "X-Data-Url")

        return BYOCJob(
            raw=data,
            job_id=job_id,
            capability=capability,
            publish_url=publish_url,
            subscribe_url=subscribe_url,
            control_url=control_url,
            events_url=events_url,
            data_url=data_url,
            control=Control(control_url) if control_url else None,
            events=Events(events_url) if events_url else None,
            _payment_session=payment_session,
            _signed_job_header=signed_job_header,
        )

    def start_media(self, config: MediaPublishConfig) -> MediaPublish:
        if not self.publish_url:
            raise LivepeerGatewayError("No publish_url present on this BYOC job")
        if self._media is None:
            media = MediaPublish(
                self.publish_url,
                mime_type=config.mime_type,
                keyframe_interval_s=config.keyframe_interval_s,
                fps=config.fps,
            )
            object.__setattr__(self, "_media", media)
        return self._media

    def media_output(
        self,
        *,
        start_seq: int = -2,
        max_retries: int = 5,
        max_segment_bytes: Optional[int] = None,
        connection_close: bool = False,
        chunk_size: int = 64 * 1024,
        max_segments: int = 5,
        on_lag: LagPolicy = LagPolicy.LATEST,
    ) -> MediaOutput:
        if not self.subscribe_url:
            raise LivepeerGatewayError("No subscribe_url present on this BYOC job")
        return MediaOutput(
            self.subscribe_url,
            start_seq=start_seq,
            max_retries=max_retries,
            max_segment_bytes=max_segment_bytes,
            connection_close=connection_close,
            chunk_size=chunk_size,
            max_segments=max_segments,
            on_lag=on_lag,
        )

    @property
    def payment_session(self) -> Optional[BYOCPaymentSession]:
        return self._payment_session

    def start_payment_sender(self, *, interval_s: float = 5.0) -> Optional[asyncio.Task]:
        if getattr(self, "_payment_task", None) is not None:
            return self._payment_task
        if not self._payment_session or not self._signed_job_header:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _LOG.warning(
                "No running event loop; BYOC payment sender not started. "
                "Call job.start_payment_sender() from async code to enable."
            )
            return None

        task = loop.create_task(
            _byoc_payment_sender(
                self._signed_job_header,
                self._payment_session,
                interval_s=interval_s,
            )
        )
        object.__setattr__(self, "_payment_task", task)
        return task

    async def close(self) -> None:
        tasks = []
        payment_task = getattr(self, "_payment_task", None)
        if payment_task is not None and not payment_task.done():
            payment_task.cancel()
            tasks.append(payment_task)
        if self.control is not None:
            tasks.append(self.control.close_control())
        if self._media is not None:
            tasks.append(self._media.close())
        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result


async def _byoc_payment_sender(
    signed_job_header: str,
    session: BYOCPaymentSession,
    *,
    interval_s: float,
) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await asyncio.to_thread(session.send_stream_payment, signed_job_header)
        except SkipPaymentCycle as e:
            _LOG.debug("BYOC payment sender: skipping payment cycle (%s)", e)
        except Exception:
            _LOG.exception("BYOC payment sender failed")


def _post_byoc_start(
    url: str,
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    req_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "livepeer-python-gateway/0.1",
    }
    req_headers.update(headers)

    try:
        with httpx.Client(verify=False, timeout=timeout) as client:
            resp = client.post(url, content=json.dumps(payload).encode("utf-8"), headers=req_headers)
        if resp.status_code >= 400:
            body = _extract_error_message(resp)
            body_part = f"; body={body!r}" if body else ""
            raise LivepeerGatewayError(
                f"HTTP BYOC start error: HTTP {resp.status_code} from endpoint (url={url}){body_part}"
            )
        raw_body = resp.text
        response_headers = dict(resp.headers.items())
        status = resp.status_code
    except LivepeerGatewayError:
        raise
    except httpx.ConnectError as e:
        raise LivepeerGatewayError(
            f"HTTP BYOC start error: connection refused (is the server running? is the host/port correct?) (url={url})"
        ) from e
    except httpx.HTTPError as e:
        raise LivepeerGatewayError(
            f"HTTP BYOC start error: failed to reach endpoint: {e} (url={url})"
        ) from e
    except Exception as e:
        raise LivepeerGatewayError(
            f"HTTP BYOC start error: unexpected error: {e.__class__.__name__}: {e} (url={url})"
        ) from e

    body: Any = None
    if raw_body.strip():
        try:
            body = json.loads(raw_body)
        except Exception:
            body = raw_body
    return {"status_code": status, "headers": response_headers, "body": body}


def start_byoc_job(
    orch_url: Optional[Sequence[str] | str],
    req: BYOCJobRequest,
    *,
    token: Optional[str] = None,
    signer_url: Optional[str] = None,
    signer_headers: Optional[dict[str, str]] = None,
    discovery_url: Optional[str] = None,
    discovery_headers: Optional[dict[str, str]] = None,
) -> BYOCJob:
    if not isinstance(req.capability, str) or not req.capability.strip():
        raise LivepeerGatewayError("start_byoc_job requires a non-empty capability")

    resolved_signer_url = signer_url
    resolved_signer_headers = signer_headers
    resolved_discovery_url = discovery_url
    resolved_discovery_headers = discovery_headers
    if token is not None:
        from .lv2v import _parse_token

        token_data = _parse_token(token)
        if resolved_signer_url is None:
            resolved_signer_url = token_data.get("signer")
        if resolved_signer_headers is None:
            resolved_signer_headers = token_data.get("signer_headers")
        if resolved_discovery_url is None:
            resolved_discovery_url = token_data.get("discovery")
        if resolved_discovery_headers is None:
            resolved_discovery_headers = token_data.get("discovery_headers")

    capabilities = build_capabilities(CapabilityId.BYOC, req.capability.strip())
    cursor = orchestrator_selector(
        orch_url,
        signer_url=resolved_signer_url,
        signer_headers=resolved_signer_headers,
        discovery_url=resolved_discovery_url,
        discovery_headers=resolved_discovery_headers,
        capabilities=capabilities,
    )

    start_rejections: list[OrchestratorRejection] = []
    while True:
        try:
            selected_url, info = cursor.next()
        except NoOrchestratorAvailableError as e:
            all_rejections = list(e.rejections) + start_rejections
            if all_rejections:
                raise NoOrchestratorAvailableError(
                    f"All orchestrators failed ({len(all_rejections)} tried)",
                    rejections=all_rejections,
                ) from None
            raise

        try:
            session = BYOCPaymentSession(
                resolved_signer_url,
                info,
                capability_name=req.capability.strip(),
                signer_headers=resolved_signer_headers,
                capabilities=capabilities,
            )

            job_id = req._job_id()
            request_json = req._request_json(job_id)
            parameters_json = req._parameters_json()
            signed = session.sign_byoc_job(request_json, parameters_json)

            signed_payload = {
                "id": job_id,
                "request": request_json,
                "parameters": parameters_json,
                "capability": req.capability.strip(),
                "sender": signed.sender,
                "sig": signed.signature,
                "timeout_seconds": max(1, int(req.timeout_seconds)),
            }
            signed_job_header = base64.b64encode(
                json.dumps(signed_payload, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")

            payment = session.get_payment()
            headers = {
                "Livepeer": signed_job_header,
                "Livepeer-Payment": payment.payment,
                "Livepeer-Segment": payment.seg_creds or "",
            }
            base = _http_origin(info.transcoder)
            data = _post_byoc_start(
                f"{base}/ai/stream/start",
                payload=req._body(job_id),
                headers=headers,
                timeout=float(max(1, int(req.timeout_seconds))),
            )
            job = BYOCJob.from_start_response(
                data,
                job_id=job_id,
                capability=req.capability.strip(),
                payment_session=session,
                signed_job_header=signed_job_header,
            )
            job.start_payment_sender()
            return job
        except LivepeerGatewayError as e:
            _LOG.debug(
                "start_byoc_job candidate failed, trying fallback if available: %s (%s)",
                selected_url,
                str(e),
            )
            start_rejections.append(OrchestratorRejection(url=selected_url, reason=str(e)))
