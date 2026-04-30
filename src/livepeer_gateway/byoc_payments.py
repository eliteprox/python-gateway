from __future__ import annotations

import base64
import ssl
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import lp_rpc_pb2
from .errors import LivepeerGatewayError, PaymentError, SignerRefreshRequired
from .remote_signer import GetPaymentResponse


@dataclass(frozen=True)
class SignedBYOCJob:
    sender: str
    signature: str


class BYOCPaymentSession:
    """
    BYOC payment session.

    - Signs job credentials via the remote signer's ``POST /sign-byoc-job``
      endpoint (V1 binary signing format, server-side flatten).
    - Generates time-based BYOC payments via ``/generate-live-payment``
      using the BYOC capability name as the payment ``type``.
    - Sends recurring stream payments to the orchestrator's
      ``/ai/stream/payment`` endpoint (or operator override).
    """

    def __init__(
        self,
        signer_url: Optional[str],
        info: lp_rpc_pb2.OrchestratorInfo,
        *,
        capability_name: str,
        signer_headers: Optional[dict[str, str]] = None,
        capabilities: Optional[lp_rpc_pb2.Capabilities] = None,
        max_refresh_retries: int = 3,
        stream_payment_endpoint: str = "/ai/stream/payment",
        use_tofu: bool = True,
    ) -> None:
        if not isinstance(capability_name, str) or not capability_name.strip():
            raise PaymentError("capability_name must be a non-empty string")
        if not isinstance(stream_payment_endpoint, str) or not stream_payment_endpoint.strip():
            raise PaymentError("stream_payment_endpoint must be a non-empty string")

        self._signer_url = signer_url
        self._signer_headers = signer_headers
        self._info = info
        self._capability_name = capability_name.strip()
        self._capabilities = capabilities
        self._max_refresh_retries = max(0, int(max_refresh_retries))
        self._state: Optional[dict[str, Any]] = None
        self._stream_payment_endpoint = stream_payment_endpoint.strip()
        self._timeout_seconds: int = 0
        self._use_tofu = use_tofu

    def set_timeout_seconds(self, timeout_seconds: int) -> None:
        self._timeout_seconds = max(0, int(timeout_seconds))

    def _build_payment_payload(self) -> dict[str, Any]:
        pb = self._info.SerializeToString()
        payload: dict[str, Any] = {
            "orchestrator": base64.b64encode(pb).decode("ascii"),
            "type": self._capability_name,
            "RequestID": str(uuid.uuid4()),
        }
        if self._timeout_seconds > 0:
            payload["timeoutSeconds"] = self._timeout_seconds
        if self._state is not None:
            payload["state"] = self._state
        if self._capabilities is not None:
            payload["capabilities"] = base64.b64encode(
                self._capabilities.SerializeToString()
            ).decode("ascii")
        return payload

    def _refresh_orchestrator_info(self) -> None:
        if not self._info.transcoder:
            raise PaymentError("OrchestratorInfo missing transcoder URL for refresh")

        from .orch_info import get_orch_info

        self._info = get_orch_info(
            self._info.transcoder,
            signer_url=self._signer_url,
            signer_headers=self._signer_headers,
            capabilities=self._capabilities,
            use_tofu=self._use_tofu,
        )

    def _request_payment(self) -> GetPaymentResponse:
        from .orchestrator import _join_signer_endpoint, post_json

        url = _join_signer_endpoint(self._signer_url, "/generate-live-payment")
        data = post_json(url, self._build_payment_payload(), headers=self._signer_headers)

        payment = data.get("payment")
        if not isinstance(payment, str) or not payment:
            raise PaymentError(f"GetPayment error: missing/invalid 'payment' in response (url={url})")

        seg_creds = data.get("segCreds")
        if seg_creds is not None and not isinstance(seg_creds, str):
            raise PaymentError(f"GetPayment error: invalid 'segCreds' in response (url={url})")

        state = data.get("state")
        if not isinstance(state, dict):
            raise PaymentError(f"Remote signer response missing 'state' object (url={url})")

        self._state = state
        return GetPaymentResponse(payment=payment, seg_creds=seg_creds)

    def get_payment(self) -> GetPaymentResponse:
        if not self._signer_url:
            return GetPaymentResponse(payment="", seg_creds="")

        attempts = 0
        while True:
            try:
                return self._request_payment()
            except SignerRefreshRequired as e:
                if attempts >= self._max_refresh_retries:
                    raise PaymentError(f"Signer refresh required after {attempts} retries: {e}") from e
                self._refresh_orchestrator_info()
                attempts += 1

    def _post_empty(self, url: str, headers: dict[str, str], *, op: str, timeout: float = 5.0) -> None:
        from .orchestrator import _extract_error_message

        req = Request(url, data=b"", headers=headers, method="POST")
        ssl_ctx = ssl._create_unverified_context()
        try:
            with urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
                resp.read()
        except HTTPError as e:
            body = _extract_error_message(e)
            body_part = f"; body={body!r}" if body else ""
            raise PaymentError(
                f"HTTP {op} error: HTTP {e.code} from endpoint (url={url}){body_part}"
            ) from e
        except ConnectionRefusedError as e:
            raise PaymentError(
                f"HTTP {op} error: connection refused (is the server running? is the host/port correct?) (url={url})"
            ) from e
        except URLError as e:
            raise PaymentError(
                f"HTTP {op} error: failed to reach endpoint: {getattr(e, 'reason', e)} (url={url})"
            ) from e
        except LivepeerGatewayError:
            raise
        except Exception as e:
            raise PaymentError(
                f"HTTP {op} error: unexpected error: {e.__class__.__name__}: {e} (url={url})"
            ) from e

    def sign_byoc_job(
        self,
        job_id: str,
        capability: str,
        request: str,
        parameters: str,
        timeout_seconds: int,
    ) -> SignedBYOCJob:
        """
        Ask the remote signer to sign a BYOC job credential using the V1
        binary signing format. The signer is authoritative for the wire layout;
        we only forward the structured fields.
        """
        if not self._signer_url:
            raise PaymentError("sign_byoc_job requires signer_url")
        if not isinstance(request, str):
            raise PaymentError("request must be a JSON string")
        if not isinstance(parameters, str):
            raise PaymentError("parameters must be a JSON string")

        self.set_timeout_seconds(timeout_seconds)

        from .orchestrator import _join_signer_endpoint, post_json

        url = _join_signer_endpoint(self._signer_url, "/sign-byoc-job")
        data = post_json(
            url,
            {
                "id": job_id,
                "capability": capability,
                "request": request,
                "parameters": parameters,
                "timeout_seconds": self._timeout_seconds,
                "signature_format": "v1",
            },
            headers=self._signer_headers,
        )
        sender = data.get("sender")
        signature = data.get("signature")
        if not isinstance(sender, str) or not sender:
            raise PaymentError(f"Invalid signer response: missing sender (url={url})")
        if not isinstance(signature, str) or not signature:
            raise PaymentError(f"Invalid signer response: missing signature (url={url})")
        return SignedBYOCJob(sender=sender, signature=signature)

    def send_stream_payment(self, job_header: str) -> None:
        """
        Send a single recurring BYOC stream payment to the orchestrator.

        ``job_header`` is the base64-encoded ``Livepeer:`` header established
        at job start; we attach a fresh payment ticket to it.
        """
        if not isinstance(job_header, str) or not job_header:
            raise PaymentError("job_header must be a non-empty base64 string")
        if not self._info.transcoder:
            raise PaymentError("OrchestratorInfo missing transcoder URL for stream payment")

        from .orchestrator import resolve_transcoder_http_url

        p = self.get_payment()
        url = resolve_transcoder_http_url(self._info.transcoder, self._stream_payment_endpoint)
        headers = {
            "Livepeer": job_header,
            "Livepeer-Payment": p.payment,
            "Livepeer-Segment": p.seg_creds or "",
        }
        self._post_empty(url, headers, op="stream payment")
