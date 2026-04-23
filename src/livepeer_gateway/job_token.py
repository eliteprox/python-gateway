"""BYOC orchestrator discovery via HTTP /process/token.

Mirrors go-livepeer's byoc.getJobOrchestrators() (byoc/job_gateway.go:339-403):
ask each candidate orchestrator for a JobToken containing a
per-(sender, capability) price and TicketParams, then synthesize the subset of
OrchestratorInfo that the remote signer and BYOC payment flow consume.

Only the BYOC request path (POST /process/request/{capability}) uses this.
LV2V and the get_orchestrator_info.py example still go through gRPC
GetOrchestrator, which is required there for auth_token, control_url, etc.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import lp_rpc_pb2
from .errors import (
    LivepeerGatewayError,
    NoOrchestratorAvailableError,
    OrchestratorRejection,
)
from .orchestrator import _extract_error_message, _http_origin, discover_orchestrators
from .remote_signer import SignerMaterial, _freeze_headers, get_orch_info_sig

_LOG = logging.getLogger(__name__)

_BATCH_SIZE = 5


@dataclass(frozen=True)
class JobToken:
    """Decoded /process/token response (mirrors go-livepeer byoc.JobToken)."""

    sender_address: Optional[dict[str, Any]]
    ticket_params: Optional[dict[str, Any]]
    balance: int
    price: Optional[dict[str, Any]]
    service_addr: str
    available_capacity: int
    worker_options: list[dict[str, Any]]


def _hex_or_b64_to_bytes(value: Any) -> bytes:
    """Decode a protobuf bytes field that was encoded by Go's encoding/json.

    Go's encoding/json base64-encodes []byte (std encoding, with padding).
    A few callers produce 0x-prefixed hex instead, so accept that too.
    """
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if not isinstance(value, str):
        raise LivepeerGatewayError(
            f"expected hex or base64 string, got {type(value).__name__}"
        )
    v = value.strip()
    if not v:
        return b""
    if v.startswith(("0x", "0X")):
        return bytes.fromhex(v[2:]) if v[2:] else b""
    return base64.b64decode(v, validate=False)


def _build_eth_address_header(sig: SignerMaterial) -> str:
    """Build the Livepeer-Eth-Address header payload from signer material.

    Shape mirrors go-livepeer byoc.JobSender (byoc/types.go:160):
        {"addr": "0x...", "sig": "0x..."}
    base64(std) of the JSON.

    Uses the raw hex strings returned by the signer (preserving EIP-55
    mixed-case on the address): the orchestrator's verifyTokenCreds
    (byoc/job_orchestrator.go:701) verifies the signature against the addr
    *string*, so any case-normalization breaks signature recovery.
    """
    addr_hex = sig.address_hex
    sig_hex = sig.sig_hex
    if not addr_hex or not sig_hex:
        raise LivepeerGatewayError("remote signer did not provide address/signature")
    payload = json.dumps(
        {"addr": addr_hex, "sig": sig_hex},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.b64encode(payload).decode("ascii")


def fetch_job_token(
    orch_url: str,
    *,
    signer_url: str,
    signer_headers: Optional[dict[str, str]] = None,
    capability: str,
    options_filter: Optional[dict[str, str]] = None,
    timeout: float = 5.0,
) -> JobToken:
    """GET {orch}/process/token and return a JobToken.

    Requires a configured remote signer: the orchestrator enforces a signed
    sender identity on this endpoint, so offchain mode is not supported here.
    """
    if not signer_url:
        raise LivepeerGatewayError(
            "fetch_job_token requires signer_url (BYOC offchain mode is not supported)"
        )
    if not capability:
        raise LivepeerGatewayError("fetch_job_token requires capability")

    sig_mat = get_orch_info_sig(signer_url, _freeze_headers(signer_headers))

    base = _http_origin(orch_url)
    url = f"{base}/process/token"
    if options_filter:
        query = urlencode(
            {
                "options_filter": json.dumps(
                    {str(k): str(v) for k, v in options_filter.items()},
                    separators=(",", ":"),
                )
            }
        )
        url = f"{url}?{query}"

    req = Request(
        url,
        method="GET",
        headers={
            "Livepeer-Eth-Address": _build_eth_address_header(sig_mat),
            "Livepeer-Capability": capability,
            "Accept": "application/json",
        },
    )
    ssl_ctx = ssl._create_unverified_context()
    try:
        with urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            raw = resp.read()
    except HTTPError as e:
        body = _extract_error_message(e)
        body_part = f"; body={body!r}" if body else ""
        raise LivepeerGatewayError(
            f"/process/token HTTP {e.code} (url={url}){body_part}"
        ) from e
    except (URLError, ConnectionRefusedError) as e:
        raise LivepeerGatewayError(
            f"/process/token failed to reach orchestrator: {e} (url={url})"
        ) from e

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise LivepeerGatewayError(
            f"/process/token returned invalid JSON: {e} (url={url})"
        ) from e

    if not isinstance(data, dict):
        raise LivepeerGatewayError(
            f"/process/token returned non-object JSON ({type(data).__name__}) (url={url})"
        )

    return JobToken(
        sender_address=data.get("sender_address")
        if isinstance(data.get("sender_address"), dict)
        else None,
        ticket_params=data.get("ticket_params")
        if isinstance(data.get("ticket_params"), dict)
        else None,
        balance=int(data.get("balance") or 0),
        price=data.get("price") if isinstance(data.get("price"), dict) else None,
        service_addr=str(data.get("service_addr") or ""),
        available_capacity=int(data.get("available_capacity") or 0),
        worker_options=list(data.get("worker_options") or []),
    )


def job_token_to_orch_info(jt: JobToken) -> lp_rpc_pb2.OrchestratorInfo:
    """Synthesize the subset of OrchestratorInfo that the remote signer
    (/generate-live-payment, server/remote_signer.go:253-263) and BYOC payment
    flow consume.

    A synthetic AuthToken with a 30-minute future expiration is attached: the
    LV2V-style signer routes through shouldRefreshSession
    (server/broadcast.go:1629-1658), which hard-errors "missing auth token" if
    the token is nil. BYOC's own HTTP dispatch path does not require the token
    (byoc/trickle.go:57), so a synthetic one is fine here. capabilities and
    hardware are still intentionally omitted.
    """
    if jt.price is None:
        raise LivepeerGatewayError(
            "JobToken has no price; orchestrator cannot serve this capability"
        )
    if jt.ticket_params is None:
        raise LivepeerGatewayError(
            "JobToken has no ticket_params; cannot build a payment"
        )

    info = lp_rpc_pb2.OrchestratorInfo()
    info.transcoder = jt.service_addr

    tp_src = jt.ticket_params
    tp = info.ticket_params
    tp.recipient = _hex_or_b64_to_bytes(tp_src.get("recipient"))
    tp.face_value = _hex_or_b64_to_bytes(tp_src.get("face_value"))
    tp.win_prob = _hex_or_b64_to_bytes(tp_src.get("win_prob"))
    tp.recipient_rand_hash = _hex_or_b64_to_bytes(tp_src.get("recipient_rand_hash"))
    tp.seed = _hex_or_b64_to_bytes(tp_src.get("seed"))
    exp_block = tp_src.get("expiration_block")
    if exp_block is not None:
        tp.expiration_block = _hex_or_b64_to_bytes(exp_block)

    exp_params = tp_src.get("expiration_params")
    if isinstance(exp_params, dict):
        ep = tp.expiration_params
        cr = exp_params.get("creation_round")
        if cr is not None:
            ep.creation_round = int(cr)
        crbh = exp_params.get("creation_round_block_hash")
        if crbh is not None:
            ep.creation_round_block_hash = _hex_or_b64_to_bytes(crbh)

    # OrchestratorInfo.address is the orch's ETH address; for BYOC this equals
    # ticket_params.recipient.
    info.address = tp.recipient

    info.price_info.pricePerUnit = int(jt.price.get("pricePerUnit") or 0)
    info.price_info.pixelsPerUnit = int(jt.price.get("pixelsPerUnit") or 0)

    # Synthetic auth token: go-livepeer's authTokenValidPeriod is 30 min and
    # shouldRefreshSession refreshes at the last 10% of the period, so any
    # expiration beyond ~27 minutes from now keeps the signer on the happy path.
    info.auth_token.session_id = "byoc-" + os.urandom(8).hex()
    info.auth_token.expiration = int(time.time()) + 30 * 60
    # Token bytes are opaque to the client; leave empty.

    return info


class JobTokenCursor:
    """Parallel batched cursor over /process/token results.

    Mirrors selection.SelectionCursor but uses HTTP /process/token to obtain
    BYOC per-(sender, capability) pricing and TicketParams. Candidates that
    return no price, no ticket_params, or zero available capacity are rejected
    and the cursor advances to the next batch.
    """

    def __init__(
        self,
        orch_list: Sequence[str],
        *,
        signer_url: str,
        signer_headers: Optional[dict[str, str]] = None,
        capability: str,
        options_filter: Optional[dict[str, str]] = None,
        timeout: float = 5.0,
    ) -> None:
        self._orch_list = list(orch_list)
        self._signer_url = signer_url
        self._signer_headers = signer_headers
        self._capability = capability
        self._options_filter = options_filter
        self._timeout = timeout
        self._batch_start = 0
        self._pending: list[Tuple[str, JobToken]] = []
        self.rejections: list[OrchestratorRejection] = []

    def next(self) -> Tuple[str, JobToken]:
        while True:
            if self._pending:
                selected = self._pending.pop(0)
                _LOG.debug("job_token_selector selected: %s", selected[0])
                return selected

            if self._batch_start >= len(self._orch_list):
                _LOG.debug(
                    "job_token_selector failed: all %d orchestrators rejected",
                    len(self._orch_list),
                )
                raise NoOrchestratorAvailableError(
                    f"All orchestrators failed ({len(self.rejections)} tried)",
                    rejections=list(self.rejections),
                )

            self._populate_next_batch()

    def _populate_next_batch(self) -> None:
        batch = self._orch_list[self._batch_start : self._batch_start + _BATCH_SIZE]
        self._batch_start += _BATCH_SIZE
        _LOG.debug("job_token_selector trying batch: %s", batch)

        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = {
                executor.submit(
                    fetch_job_token,
                    url,
                    signer_url=self._signer_url,
                    signer_headers=self._signer_headers,
                    capability=self._capability,
                    options_filter=self._options_filter,
                    timeout=self._timeout,
                ): url
                for url in batch
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    jt = future.result()
                except Exception as e:
                    _LOG.debug(
                        "job_token_selector candidate failed: %s (%s)", url, e
                    )
                    self.rejections.append(
                        OrchestratorRejection(url=url, reason=str(e))
                    )
                    continue
                if jt.price is None or jt.ticket_params is None:
                    self.rejections.append(
                        OrchestratorRejection(
                            url=url,
                            reason="orchestrator returned no price or ticket_params",
                        )
                    )
                    continue
                if jt.available_capacity <= 0:
                    self.rejections.append(
                        OrchestratorRejection(
                            url=url, reason="orchestrator has no available capacity"
                        )
                    )
                    continue
                self._pending.append((url, jt))


def job_token_selector(
    orchestrators: Optional[Sequence[str] | str] = None,
    *,
    signer_url: Optional[str] = None,
    signer_headers: Optional[dict[str, str]] = None,
    discovery_url: Optional[str] = None,
    discovery_headers: Optional[dict[str, str]] = None,
    capability: str,
    options_filter: Optional[dict[str, str]] = None,
    timeout: float = 5.0,
) -> JobTokenCursor:
    if not signer_url:
        raise LivepeerGatewayError("job_token_selector requires signer_url")
    if not capability:
        raise LivepeerGatewayError("job_token_selector requires capability")
    orch_list = discover_orchestrators(
        orchestrators,
        signer_url=signer_url,
        signer_headers=signer_headers,
        discovery_url=discovery_url,
        discovery_headers=discovery_headers,
    )
    if not orch_list:
        raise NoOrchestratorAvailableError("No orchestrators available to select")
    return JobTokenCursor(
        orch_list,
        signer_url=signer_url,
        signer_headers=signer_headers,
        capability=capability,
        options_filter=options_filter,
        timeout=timeout,
    )
