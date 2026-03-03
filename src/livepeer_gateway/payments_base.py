from __future__ import annotations

import base64
import ssl
from dataclasses import dataclass
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import lp_rpc_pb2
from .errors import PaymentError, SignerRefreshRequired


@dataclass(frozen=True)
class GetPaymentResponse:
    payment: str
    seg_creds: Optional[str] = None


class BasePaymentSession:
    def __init__(
        self,
        signer_url: Optional[str],
        info: lp_rpc_pb2.OrchestratorInfo,
        *,
        signer_headers: Optional[dict[str, str]],
        payment_type: str,
        capabilities: Optional[lp_rpc_pb2.Capabilities],
        max_refresh_retries: int = 3,
    ) -> None:
        self._signer_url = signer_url
        self._signer_headers = signer_headers
        self._info = info
        self._payment_type = payment_type
        self._manifest_id: Optional[str] = None
        self._capabilities = capabilities
        self._max_refresh_retries = max(0, int(max_refresh_retries))
        self._state: Optional[dict[str, Any]] = None

    def set_manifest_id(self, manifest_id: str) -> None:
        if not isinstance(manifest_id, str) or not manifest_id.strip():
            raise PaymentError("manifest_id must be a non-empty string")
        self._manifest_id = manifest_id.strip()

    def _offchain_payment(self) -> GetPaymentResponse:
        raise NotImplementedError

    def _build_payment_payload(self) -> dict[str, Any]:
        pb = self._info.SerializeToString()
        orch_b64 = base64.b64encode(pb).decode("ascii")
        payload: dict[str, Any] = {
            "orchestrator": orch_b64,
            "type": self._payment_type,
        }
        if self._manifest_id is not None:
            payload["ManifestID"] = self._manifest_id
        if self._state is not None:
            payload["state"] = self._state
        if self._capabilities is not None:
            caps_b64 = base64.b64encode(self._capabilities.SerializeToString()).decode("ascii")
            payload["capabilities"] = caps_b64
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
        )

    def _request_payment(self) -> GetPaymentResponse:
        from .orchestrator import _http_origin, post_json

        base = _http_origin(self._signer_url)
        url = f"{base}/generate-live-payment"
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
            return self._offchain_payment()

        attempts = 0
        while True:
            try:
                return self._request_payment()
            except SignerRefreshRequired as e:
                if attempts >= self._max_refresh_retries:
                    raise PaymentError(f"Signer refresh required after {attempts} retries: {e}") from e
                self._refresh_orchestrator_info()
                attempts += 1

    def _post_empty(self, url: str, headers: dict[str, str], *, op: str) -> None:
        from .orchestrator import _extract_error_message

        req = Request(url, data=b"", headers=headers, method="POST")
        ssl_ctx = ssl._create_unverified_context()
        try:
            with urlopen(req, timeout=5.0, context=ssl_ctx) as resp:
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
        except Exception as e:
            raise PaymentError(
                f"HTTP {op} error: unexpected error: {e.__class__.__name__}: {e} (url={url})"
            ) from e

    def send_payment(self) -> None:
        from .orchestrator import _http_origin

        p = self.get_payment()
        if not self._info.transcoder:
            raise PaymentError("OrchestratorInfo missing transcoder URL for payment")

        base = _http_origin(self._info.transcoder)
        url = f"{base}/payment"
        headers = {
            "Livepeer-Payment": p.payment,
            "Livepeer-Segment": p.seg_creds or "",
        }
        self._post_empty(url, headers, op="payment")
