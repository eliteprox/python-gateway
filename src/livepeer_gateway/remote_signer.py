from __future__ import annotations

import base64
import json
import logging
import re
import ssl
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import lp_rpc_pb2
from .errors import LivepeerGatewayError, PaymentError, SignerRefreshRequired, SkipPaymentCycle
_LOG = logging.getLogger(__name__)


# Payment types accepted by the remote signer's /generate-live-payment endpoint.
# Mirrors the RemoteType_* constants in go-livepeer/server/remote_signer.go.
PAYMENT_TYPE_LV2V = "lv2v"
PAYMENT_TYPE_BYOC_REQUEST = "byoc-request"

@dataclass(frozen=True)
class GetPaymentResponse:
    payment: str
    seg_creds: Optional[str] = None


@dataclass(frozen=True)
class SignerMaterial:
    """
    Material returned by the remote signer.
    address: 20-byte broadcaster ETH address
    sig: signature bytes (length depends on scheme; commonly 65 bytes for ECDSA)
    address_hex / sig_hex: raw strings as returned by the signer, preserving
        any 0x prefix and original casing. The BYOC orchestrator's
        verifyTokenCreds (byoc/job_orchestrator.go:701) signs the addr *string*,
        so any case-normalization (e.g. bytes.hex() → lowercase) flips an
        EIP-55 mixed-case address to lowercase and breaks signature recovery.
    """
    address: Optional[bytes]
    sig: Optional[bytes]
    address_hex: Optional[str] = None
    sig_hex: Optional[str] = None


@dataclass
class RemoteSignerError(LivepeerGatewayError):
    signer_url: str
    message: str
    cause: Optional[BaseException] = None

    def __str__(self) -> str:
        return f"Remote signer error: {self.message} (url={self.signer_url})"


_HEX_RE = re.compile(r"^(0x)?[0-9a-fA-F]*$")


def _freeze_headers(
    headers: Optional[dict[str, str]],
) -> Optional[frozenset[tuple[str, str]]]:
    """Convert a headers dict to a frozenset for use with @lru_cache."""
    if headers is None:
        return None
    return frozenset(headers.items())

def _hex_to_bytes(s: str, *, expected_len: Optional[int] = None) -> bytes:
    s = s.strip()
    if not _HEX_RE.match(s):
        raise ValueError(f"Not a hex string: {s!r}")
    if s.startswith(("0x", "0X")):
        s = s[2:]
    if len(s) % 2 == 1:
        # allow odd-length hex (pad left)
        s = "0" + s
    b = bytes.fromhex(s)
    if expected_len is not None and len(b) != expected_len:
        raise ValueError(f"Expected {expected_len} bytes, got {len(b)} bytes")
    return b


@lru_cache(maxsize=None)
def get_orch_info_sig(
    signer_url: str,
    # frozenset instead of dict because @lru_cache requires hashable arguments.
    _signer_headers: Optional[frozenset[tuple[str, str]]] = None,
) -> SignerMaterial:
    """
    Fetch signer material exactly once per (signer_url, headers) combination
    for the lifetime of the process. Subsequent calls return cached data.
    """
    from .orchestrator import _extract_error_message, _http_origin, post_json

    # check for offchain mode
    if not signer_url:
        return SignerMaterial(address=None, sig=None)

    # Accept either a base URL or a full URL that includes /sign-orchestrator-info.
    # Normalize to an https:// origin and append the expected path.
    signer_url = f"{_http_origin(signer_url)}/sign-orchestrator-info"
    headers = dict(_signer_headers) if _signer_headers else None

    try:
        # Some signers accept/expect POST with an empty JSON object.
        data = post_json(signer_url, {}, headers=headers, timeout=5.0)

        # Expected response shape (example):
        # {
        #   "address": "0x0123...abcd",   # 20-byte ETH address hex
        #   "signature": "0x..."          # signature hex
        # }
        if "address" not in data or "signature" not in data:
            raise RemoteSignerError(
                signer_url,
                f"Remote signer JSON must contain 'address' and 'signature': {data!r}",
                cause=None,
            ) from None

        address_hex = str(data["address"])
        sig_hex = str(data["signature"])
        address = _hex_to_bytes(address_hex, expected_len=20)
        sig = _hex_to_bytes(sig_hex)  # signature length may vary

    except LivepeerGatewayError as e:
        # post_json wraps the underlying exception as __cause__; convert back into
        # a signer-specific error message.
        cause = e.__cause__ or e

        if isinstance(cause, HTTPError):
            body = _extract_error_message(cause)
            body_part = f"; body={body!r}" if body else ""
            raise RemoteSignerError(
                signer_url,
                f"HTTP {cause.code} from signer{body_part}",
                cause=cause,
            ) from None

        if isinstance(cause, ConnectionRefusedError):
            raise RemoteSignerError(
                signer_url,
                "connection refused (is the signer running? is the host/port correct?)",
                cause=cause,
            ) from None

        if isinstance(cause, URLError):
            raise RemoteSignerError(
                signer_url,
                f"failed to reach signer: {getattr(cause, 'reason', cause)}",
                cause=cause,
            ) from None

        if isinstance(cause, json.JSONDecodeError):
            raise RemoteSignerError(
                signer_url,
                f"signer did not return valid JSON: {cause}",
                cause=cause,
            ) from None

        raise RemoteSignerError(
            signer_url,
            f"unexpected error: {cause.__class__.__name__}: {cause}",
            cause=cause if isinstance(cause, BaseException) else e,
        ) from None

    return SignerMaterial(
        address=address,
        sig=sig,
        address_hex=address_hex,
        sig_hex=sig_hex,
    )


@dataclass(frozen=True)
class ByocJobSigningInput:
    """
    Payload we send to the remote signer's ``POST /sign-byoc-job``. The
    signer concatenates ``request + parameters`` and signs the result
    with the gateway's eth key, mirroring the embedded-key path at
    ``byoc/utils.go::(*gatewayJob).sign()`` in go-livepeer.
    """
    request: str
    parameters: str


@dataclass(frozen=True)
class ByocJobSignature:
    """
    Signer's response. ``sender`` is an EIP-55 mixed-case eth address.
    ``signature`` is a ``0x``-prefixed 65-byte ECDSA signature.

    Both fields MUST be placed into ``JobRequest.Sender`` /
    ``JobRequest.Sig`` verbatim. Any case-normalising round-trip
    (``bytes.fromhex(addr).hex()`` or similar) flips EIP-55 casing to
    lowercase, and the orchestrator's ``VerifySig`` then recovers a
    different address from the signature.
    """
    sender: str
    signature: str


def sign_byoc_job(
    signer_url: str,
    signing_input: ByocJobSigningInput,
    *,
    signer_headers: Optional[dict[str, str]] = None,
    timeout: float = 5.0,
) -> ByocJobSignature:
    """
    Request a one-off job signature from the remote signer.

    Called once per BYOC job (not cached across requests) because the
    ``request`` and ``parameters`` payload differs per call. The
    gateway-side embedded-key equivalent is
    ``byoc/utils.go::(*gatewayJob).sign()``.
    """
    if not signer_url:
        raise PaymentError(
            "sign_byoc_job requires a signer_url (no offchain mode fallback)"
        )

    from .orchestrator import _extract_error_message, _http_origin, post_json

    url = f"{_http_origin(signer_url)}/sign-byoc-job"
    payload: dict[str, Any] = {
        "request": signing_input.request,
        "parameters": signing_input.parameters,
    }

    try:
        data = post_json(url, payload, headers=signer_headers, timeout=timeout)
    except LivepeerGatewayError as e:
        cause = e.__cause__ or e
        if isinstance(cause, HTTPError):
            body = _extract_error_message(cause)
            body_part = f"; body={body!r}" if body else ""
            raise RemoteSignerError(
                url,
                f"HTTP {cause.code} from signer{body_part}",
                cause=cause,
            ) from None
        if isinstance(cause, (ConnectionRefusedError, URLError)):
            raise RemoteSignerError(
                url,
                f"failed to reach signer: {getattr(cause, 'reason', cause)}",
                cause=cause,
            ) from None
        raise RemoteSignerError(
            url,
            f"unexpected error: {cause.__class__.__name__}: {cause}",
            cause=cause if isinstance(cause, BaseException) else e,
        ) from None

    sender = data.get("sender")
    signature = data.get("signature")
    if not isinstance(sender, str) or not sender:
        raise RemoteSignerError(
            url,
            f"sign-byoc-job response missing/invalid 'sender': {data!r}",
            cause=None,
        )
    if not isinstance(signature, str) or not signature:
        raise RemoteSignerError(
            url,
            f"sign-byoc-job response missing/invalid 'signature': {data!r}",
            cause=None,
        )
    # Intentionally passing through verbatim — no case normalisation.
    return ByocJobSignature(sender=sender, signature=signature)


class PaymentSession:
    def __init__(
        self,
        signer_url: Optional[str],
        info: lp_rpc_pb2.OrchestratorInfo,
        *,
        signer_headers: Optional[dict[str, str]] = None,
        type: str,
        capabilities: Optional[lp_rpc_pb2.Capabilities] = None,
        use_tofu: bool = True,
        max_refresh_retries: int = 3,
        in_pixels: Optional[int] = None,
    ) -> None:
        """
        ``in_pixels`` supplies an explicit compute budget for types that don't
        use signer-side auto-calculation. Required for
        ``PAYMENT_TYPE_BYOC_REQUEST``; for BYOC pricing (``PixelsPerUnit``
        denominates wei-per-second) this represents "seconds of compute to
        pre-fund." Ignored by ``PAYMENT_TYPE_LV2V`` which auto-calculates.
        """
        self._signer_url = signer_url
        self._signer_headers = signer_headers
        self._info = info
        self._type = type
        self._manifest_id: Optional[str] = None
        self._capabilities = capabilities
        self._use_tofu = use_tofu
        self._max_refresh_retries = max(0, int(max_refresh_retries))
        self._state: Optional[dict[str, str]] = None
        if in_pixels is not None and int(in_pixels) <= 0:
            raise PaymentError("in_pixels must be a positive integer")
        self._in_pixels: Optional[int] = int(in_pixels) if in_pixels is not None else None

    def set_manifest_id(self, manifest_id: str) -> None:
        if not isinstance(manifest_id, str) or not manifest_id.strip():
            raise PaymentError("manifest_id must be a non-empty string")
        self._manifest_id = manifest_id.strip()

    def get_payment(self) -> GetPaymentResponse:
        """
        Generate a payment via the remote signer.

        Handles signer state round-tripping internally.
        On HTTP 480, refreshes OrchestratorInfo and retries
        (up to max_refresh_retries).
        Returns payment + seg_creds for use as HTTP headers.
        """

        # Offchain mode: still send the expected headers, but with empty content.
        if not self._signer_url:
            seg = lp_rpc_pb2.SegData()
            if not self._info.HasField("auth_token"):
                raise PaymentError(
                    "Orchestrator did not provide an auth token."
                )
            seg.auth_token.CopyFrom(self._info.auth_token)
            seg = base64.b64encode(seg.SerializeToString()).decode("ascii")
            return GetPaymentResponse(seg_creds=seg, payment="")

        def _payment_request() -> GetPaymentResponse:
            from .orchestrator import _http_origin, post_json

            base = _http_origin(self._signer_url)
            url = f"{base}/generate-live-payment"

            pb = self._info.SerializeToString()
            orch_b64 = base64.b64encode(pb).decode("ascii")
            payload: dict[str, Any] = {
                "orchestrator": orch_b64,
                "type": self._type,
            }
            if self._in_pixels is not None:
                payload["inPixels"] = self._in_pixels
            if self._manifest_id is not None:
                payload["ManifestID"] = self._manifest_id
            if self._state is not None:
                payload["state"] = self._state

            data = post_json(url, payload, headers=self._signer_headers)
            payment = data.get("payment")
            if not isinstance(payment, str) or not payment:
                raise PaymentError(
                    f"GetPayment error: missing/invalid 'payment' in response (url={url})"
                )

            seg_creds = data.get("segCreds")
            if seg_creds is not None and not isinstance(seg_creds, str):
                raise PaymentError(
                    f"GetPayment error: invalid 'segCreds' in response (url={url})"
                )

            state = data.get("state")
            if not isinstance(state, dict):
                raise PaymentError(
                    f"Remote signer response missing 'state' object (url={url})"
                )

            self._state = state
            return GetPaymentResponse(payment=payment, seg_creds=seg_creds)

        attempts = 0
        while True:
            try:
                return _payment_request()
            except SignerRefreshRequired as e:
                if attempts >= self._max_refresh_retries:
                    raise PaymentError(
                        f"Signer refresh required after {attempts} retries: {e}"
                    ) from e
                if not self._info.transcoder:
                    raise PaymentError(
                        "OrchestratorInfo missing transcoder URL for refresh"
                    )
                from .orch_info import get_orch_info

                self._info = get_orch_info(
                    self._info.transcoder,
                    signer_url=self._signer_url,
                    signer_headers=self._signer_headers,
                    capabilities=self._capabilities,
                    use_tofu=self._use_tofu,
                )
                attempts += 1

    def send_payment(self) -> None:
        """
        Generate a payment (via get_payment) and forward it
        to the orchestrator via POST {orch}/payment.
        """
        from .orchestrator import _extract_error_message, _http_origin

        p = self.get_payment()
        if not self._info.transcoder:
            raise PaymentError("OrchestratorInfo missing transcoder URL for payment")
        base = _http_origin(self._info.transcoder)
        url = f"{base}/payment"
        headers = {
            "Livepeer-Payment": p.payment,
            "Livepeer-Segment": p.seg_creds or "",
        }
        req = Request(url, data=b"", headers=headers, method="POST")
        ssl_ctx = ssl._create_unverified_context()
        try:
            with urlopen(req, timeout=5.0, context=ssl_ctx) as resp:
                resp.read()
        except HTTPError as e:
            body = _extract_error_message(e)
            body_part = f"; body={body!r}" if body else ""
            raise PaymentError(
                f"HTTP payment error: HTTP {e.code} from endpoint (url={url}){body_part}"
            ) from e
        except ConnectionRefusedError as e:
            raise PaymentError(
                f"HTTP payment error: connection refused (is the server running? is the host/port correct?) (url={url})"
            ) from e
        except URLError as e:
            raise PaymentError(
                f"HTTP payment error: failed to reach endpoint: {getattr(e, 'reason', e)} (url={url})"
            ) from e
        except Exception as e:
            raise PaymentError(
                f"HTTP payment error: unexpected error: {e.__class__.__name__}: {e} (url={url})"
            ) from e
