from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import lp_rpc_pb2
from .errors import PaymentError
from .payments_base import BasePaymentSession, GetPaymentResponse


@dataclass(frozen=True)
class SignedBYOCJob:
    sender: str
    signature: str


class BYOCPaymentSession(BasePaymentSession):
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
    ) -> None:
        if not isinstance(capability_name, str) or not capability_name.strip():
            raise PaymentError("capability_name must be a non-empty string")
        if not isinstance(stream_payment_endpoint, str) or not stream_payment_endpoint.strip():
            raise PaymentError("stream_payment_endpoint must be a non-empty string")

        self._capability_name = capability_name.strip()
        self._stream_payment_endpoint = stream_payment_endpoint.strip()
        super().__init__(
            signer_url,
            info,
            signer_headers=signer_headers,
            payment_type="byoc",
            capabilities=capabilities,
            max_refresh_retries=max_refresh_retries,
        )
        self.set_manifest_id(self._capability_name)

    def _offchain_payment(self) -> GetPaymentResponse:
        return GetPaymentResponse(payment="", seg_creds="")

    def _extra_payment_payload(self) -> dict[str, str]:
        return {"capability": self._capability_name}

    def sign_byoc_job(
        self,
        job_id: str,
        capability: str,
        request: str,
        parameters: str,
        timeout_seconds: int,
    ) -> SignedBYOCJob:
        if not self._signer_url:
            raise PaymentError("sign_byoc_job requires signer_url")

        if not isinstance(request, str):
            raise PaymentError("request must be a JSON string")
        if not isinstance(parameters, str):
            raise PaymentError("parameters must be a JSON string")

        from .orchestrator import _join_signer_endpoint, post_json

        url = _join_signer_endpoint(self._signer_url, "/sign-byoc-job")
        data = post_json(
            url,
            {
                "id": job_id,
                "capability": capability,
                "request": request,
                "parameters": parameters,
                "timeout_seconds": timeout_seconds,
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
