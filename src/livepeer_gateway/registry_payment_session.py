"""Registry-backed live payment via remote signer (PymtHouse generate-live-payment)."""

from __future__ import annotations

from typing import Any, Optional

from .errors import PaymentError
from .orchestrator import _http_origin, _join_signer_endpoint, post_json
from .registry_types import RegistryRouteCandidate
from .remote_signer import GetPaymentResponse


def ticket_params_base_url_from_worker(worker_url: str) -> str:
    """Origin for payer-daemon ticket-params (scheme + host[:port], no path)."""
    return _http_origin(worker_url)


class RegistryPaymentSession:
    """
    Ask the remote signer for a payment using ``paymentMode: registry``,
    without legacy OrchestratorInfo.
    """

    def __init__(
        self,
        signer_url: str,
        candidate: RegistryRouteCandidate,
        *,
        job_type: str = "registry-session",
        signer_headers: Optional[dict[str, str]] = None,
        face_value_wei: Optional[int] = None,
        registry_price_per_unit_wei: Optional[int] = None,
        work_units: Optional[int] = None,
    ) -> None:
        self._signer_url = signer_url
        self._candidate = candidate
        self._job_type = job_type
        self._signer_headers = signer_headers
        self._face_value_wei = face_value_wei
        self._registry_price_per_unit_wei = registry_price_per_unit_wei
        self._work_units = work_units
        self._manifest_id: Optional[str] = None
        self._state: Optional[dict[str, str]] = None
        if self._face_value_wei is not None and self._face_value_wei <= 0:
            raise PaymentError("face_value_wei must be > 0 when set")
        if self._registry_price_per_unit_wei is not None and self._registry_price_per_unit_wei < 0:
            raise PaymentError("registry_price_per_unit_wei must be >= 0 when set")

    def set_manifest_id(self, manifest_id: str) -> None:
        if not isinstance(manifest_id, str) or not manifest_id.strip():
            raise PaymentError("manifest_id must be a non-empty string")
        self._manifest_id = manifest_id.strip()

    def _build_payload(self) -> dict[str, Any]:
        ticket_base = ticket_params_base_url_from_worker(self._candidate.worker_url)
        payload: dict[str, Any] = {
            "paymentMode": "registry",
            "recipient": self._candidate.orch_eth_address,
            "ticketParamsBaseUrl": ticket_base,
            "capability": self._candidate.capability_id,
            "offering": self._candidate.offering_id,
            "type": self._job_type,
        }
        if self._manifest_id is not None:
            payload["ManifestID"] = self._manifest_id
        if self._state is not None:
            payload["state"] = self._state

        if self._face_value_wei is not None:
            payload["faceValueWei"] = str(self._face_value_wei)
        else:
            if self._registry_price_per_unit_wei is None:
                raise PaymentError(
                    "Registry payment requires face_value_wei or registry_price_per_unit_wei"
                )
            wu = self._work_units if self._work_units is not None else 0
            if wu <= 0:
                raise PaymentError(
                    "Registry payment without face_value_wei requires positive work_units / InPixels"
                )
            payload["registryPricePerUnitWei"] = str(self._registry_price_per_unit_wei)
            payload["InPixels"] = int(wu)

        return payload

    def get_payment(self) -> GetPaymentResponse:
        if not self._signer_url:
            raise PaymentError("signer_url is required for registry payment")

        url = _join_signer_endpoint(self._signer_url, "/generate-live-payment")
        payload = self._build_payload()
        data = post_json(url, payload, headers=self._signer_headers)
        payment = data.get("payment")
        if not isinstance(payment, str) or not payment:
            raise PaymentError(
                f"registry GetPayment: missing/invalid 'payment' in response (url={url})"
            )
        seg_creds = data.get("segCreds")
        if seg_creds is not None and not isinstance(seg_creds, str):
            raise PaymentError(f"registry GetPayment: invalid 'segCreds' (url={url})")
        state = data.get("state")
        if isinstance(state, dict):
            self._state = state
        return GetPaymentResponse(payment=payment, seg_creds=seg_creds)
