from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import httpx

from . import lp_rpc_pb2
from .errors import LivepeerGatewayError, PaymentError
from .payments_base import BasePaymentSession, GetPaymentResponse
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignerMaterial:
    """
    Material returned by the remote signer.
    address: 20-byte broadcaster ETH address
    sig: signature bytes (length depends on scheme; commonly 65 bytes for ECDSA)
    """
    address: bytes
    sig: bytes


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
    from .orchestrator import _extract_error_message, _join_signer_endpoint, post_json

    # check for offchain mode
    if not signer_url:
        return SignerMaterial(address=None, sig=None)

    # Accept either a signer base URL (for example: .../api/signer)
    # or a full URL ending with /sign-orchestrator-info.
    signer_url = _join_signer_endpoint(signer_url, "/sign-orchestrator-info")
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

        address = _hex_to_bytes(str(data["address"]), expected_len=20)
        sig = _hex_to_bytes(str(data["signature"]))  # signature length may vary

    except LivepeerGatewayError as e:
        cause = e.__cause__ or e

        if isinstance(cause, httpx.ConnectError):
            raise RemoteSignerError(
                signer_url,
                "connection refused (is the signer running? is the host/port correct?)",
                cause=cause,
            ) from None

        if isinstance(cause, httpx.HTTPError):
            raise RemoteSignerError(
                signer_url,
                f"failed to reach signer: {cause}",
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
            str(e),
            cause=cause if isinstance(cause, BaseException) else e,
        ) from None

    return SignerMaterial(address=address, sig=sig)


class PaymentSession(BasePaymentSession):
    def __init__(
        self,
        signer_url: Optional[str],
        info: lp_rpc_pb2.OrchestratorInfo,
        *,
        signer_headers: Optional[dict[str, str]] = None,
        type: str = "lv2v",
        capabilities: Optional[lp_rpc_pb2.Capabilities] = None,
        use_tofu: bool = True,
        max_refresh_retries: int = 3,
    ) -> None:
        super().__init__(
            signer_url,
            info,
            signer_headers=signer_headers,
            payment_type=type,
            capabilities=capabilities,
            max_refresh_retries=max_refresh_retries,
        )

    def _offchain_payment(self) -> GetPaymentResponse:
        seg = lp_rpc_pb2.SegData()
        if not self._info.HasField("auth_token"):
            raise PaymentError("Orchestrator did not provide an auth token.")
        seg.auth_token.CopyFrom(self._info.auth_token)
        seg_b64 = base64.b64encode(seg.SerializeToString()).decode("ascii")
        return GetPaymentResponse(seg_creds=seg_b64, payment="")
