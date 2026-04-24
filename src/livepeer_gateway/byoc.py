from __future__ import annotations

import base64
import json
import logging
import secrets
import ssl
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Sequence, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import lp_rpc_pb2
from .errors import (
    LivepeerGatewayError,
    NoOrchestratorAvailableError,
    OrchestratorRejection,
    PaymentError,
    SkipPaymentCycle,
)
from .job_token import job_token_selector, job_token_to_orch_info
from .orchestrator import _extract_error_message, _http_origin
from .remote_signer import (
    PAYMENT_TYPE_BYOC_REQUEST,
    ByocJobSigningInput,
    PaymentSession,
    _freeze_headers,
    get_orch_info_sig,
    sign_byoc_job,
)
from .token import parse_token

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ByocResponse:
    """Non-streaming BYOC response."""

    status_code: int
    headers: dict[str, str]
    body: bytes
    orch_url: str

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8")


@dataclass
class ByocStreamResponse:
    """
    Streaming BYOC response (e.g. SSE).

    The underlying HTTP response stays open until either ``chunks()`` is
    exhausted or ``close()`` is called. Always use as a context manager or
    ensure ``close()`` runs; otherwise the orchestrator will keep billing
    for the connection.
    """

    status_code: int
    headers: dict[str, str]
    orch_url: str
    _response: Any = field(default=None, repr=False)

    def chunks(self, chunk_size: int = 4096) -> Iterator[bytes]:
        if self._response is None:
            return
        try:
            while True:
                chunk = self._response.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        resp = self._response
        self._response = None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def __enter__(self) -> "ByocStreamResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _build_request_str(body: Any) -> str:
    """
    Canonical request string used in both the signed payload and the
    Livepeer header.

    The signer signs a V1 flattened payload that includes this ``request``
    field and ``parameters`` plus ``id``, ``capability``, and
    ``timeout_seconds``. These values must match exactly what lands in the
    Livepeer header's JobRequest fields, otherwise the orchestrator's
    VerifySig fails.
    """
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8")
    if isinstance(body, str):
        return body
    return json.dumps(body)


def _build_parameters_str(options_filter: Optional[dict[str, str]]) -> str:
    """
    Canonical parameters string — empty when no ``options_filter`` is
    supplied, otherwise a compact JSON-encoded ``{"options_filter": ...}``
    object. Must byte-match what goes on the Livepeer header and what
    gets signed.
    """
    if not options_filter:
        return ""
    return json.dumps(
        {"options_filter": {str(k): str(v) for k, v in options_filter.items()}},
        separators=(",", ":"),
    )


def _build_livepeer_header(
    *,
    id: str,
    capability: str,
    request_str: str,
    parameters_str: str,
    timeout_seconds: int,
    sender: Optional[str] = None,
    sig: Optional[str] = None,
) -> str:
    """
    Build the base64-JSON value for the ``Livepeer`` HTTP header used by
    ``POST /process/request/{capability}``.

    Mirrors ``byoc.JobRequest`` in go-livepeer:
    - ``request`` and ``parameters`` are nested JSON strings (not embedded
      objects).
    - ``sender`` / ``sig`` are present on signed requests (the orch runs
      ``VerifySig(sender, request+parameters, sig)``).
    """
    job_req: dict[str, Any] = {
        "id": id,
        "request": request_str,
        "parameters": parameters_str,
        "capability": capability,
        "timeout_seconds": int(timeout_seconds),
    }
    if sender:
        # Preserve EIP-55 casing — do not normalise through any hex round-trip.
        job_req["sender"] = sender
    if sig:
        job_req["sig"] = sig
    encoded = json.dumps(job_req, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(encoded).decode("ascii")


def _zero_ticket_payment_header(
    info: lp_rpc_pb2.OrchestratorInfo,
    signer_url: Optional[str],
    signer_headers: Optional[dict[str, str]],
) -> str:
    """
    Build a Payment envelope with no tickets, for use when the signer
    returns HTTP 482 (existing balance already covers the request).

    Mirrors the ``!createTickets`` path in
    ``go-livepeer/byoc/payment.go:107-112``.
    """
    if not signer_url:
        raise PaymentError(
            "offchain mode cannot produce a zero-ticket payment"
        )
    signer = get_orch_info_sig(signer_url, _freeze_headers(signer_headers))
    if signer.address is None:
        raise PaymentError("remote signer did not provide a sender address")

    payment = lp_rpc_pb2.Payment()
    payment.sender = bytes(signer.address)
    if info.HasField("price_info"):
        payment.expected_price.CopyFrom(info.price_info)
    return base64.b64encode(payment.SerializeToString()).decode("ascii")


def _seg_creds_for_zero_ticket(info: lp_rpc_pb2.OrchestratorInfo) -> str:
    """
    Build the Livepeer-Segment header for a zero-ticket payment: a SegData
    carrying only the orchestrator-provided auth_token.
    """
    seg = lp_rpc_pb2.SegData()
    if info.HasField("auth_token"):
        seg.auth_token.CopyFrom(info.auth_token)
    return base64.b64encode(seg.SerializeToString()).decode("ascii")


def _encode_body(
    body: Union[bytes, str, dict, list],
    content_type: str,
) -> tuple[bytes, str]:
    if isinstance(body, (bytes, bytearray)):
        return bytes(body), content_type
    if isinstance(body, str):
        return body.encode("utf-8"), content_type
    # dict or list: JSON-encode
    return json.dumps(body).encode("utf-8"), content_type or "application/json"


def _open_byoc_request(
    orch_url: str,
    capability: str,
    body_bytes: bytes,
    *,
    content_type: str,
    livepeer_header: str,
    livepeer_payment: str,
    livepeer_segment: str,
    request_timeout: float,
) -> Any:
    """
    POST the request to the orchestrator and return the urllib response
    (caller owns closing it).
    """
    base = _http_origin(orch_url)
    url = f"{base}/process/request/{capability}"
    headers = {
        "Content-Type": content_type,
        "Livepeer": livepeer_header,
        "Livepeer-Payment": livepeer_payment,
        "Livepeer-Segment": livepeer_segment,
        "Accept": "*/*",
        "User-Agent": "livepeer-python-gateway/0.1",
    }
    req = Request(url, data=body_bytes, headers=headers, method="POST")
    ssl_ctx = ssl._create_unverified_context()
    return urlopen(req, timeout=request_timeout, context=ssl_ctx)


def byoc_request(
    capability_name: str,
    body: Union[bytes, str, dict, list],
    *,
    in_pixels: int,
    orch_url: Optional[Sequence[str] | str] = None,
    token: Optional[str] = None,
    signer_url: Optional[str] = None,
    signer_headers: Optional[dict[str, str]] = None,
    discovery_url: Optional[str] = None,
    discovery_headers: Optional[dict[str, str]] = None,
    options_filter: Optional[dict[str, str]] = None,
    content_type: str = "application/json",
    timeout_seconds: int = 30,
    request_timeout: float = 30.0,
    use_tofu: bool = True,
    stream: bool = False,
) -> Union[ByocResponse, ByocStreamResponse]:
    """
    Send a BYOC batch request to an orchestrator whose runner satisfies the
    capability (and optional ``options_filter``) and return the response.

    Handles orchestrator selection, remote-signer payment generation, and
    the signer's HTTP 482 ("no tickets needed") case by emitting a
    zero-ticket payment envelope.

    ``in_pixels`` is the compute budget requested from the signer. For BYOC
    pricing (``PixelsPerUnit`` denominates wei-per-second), this is
    "seconds of compute to pre-fund" — typically sized to cover the
    orchestrator's 60s prefund floor plus the expected request duration.

    ``options_filter`` is passed through in the ``Livepeer`` header's
    JobParameters. The orchestrator uses it to pick a matching runner and
    will reject the request if no runner matches.

    ``stream=True`` returns a :class:`ByocStreamResponse` whose body must
    be consumed (or ``close()``-d) to release the upstream connection.
    """
    if not capability_name:
        raise LivepeerGatewayError("byoc_request requires capability_name")
    if not isinstance(in_pixels, int) or in_pixels <= 0:
        raise LivepeerGatewayError("byoc_request requires a positive in_pixels")

    token_data = parse_token(token) if token is not None else None

    resolved_orch_url = token_data.get("orchestrators") if token_data else None
    if resolved_orch_url is None:
        resolved_orch_url = orch_url

    resolved_signer_url = token_data.get("signer") if token_data else None
    if resolved_signer_url is None:
        resolved_signer_url = signer_url

    resolved_signer_headers = token_data.get("signer_headers") if token_data else None
    if resolved_signer_headers is None:
        resolved_signer_headers = signer_headers

    resolved_discovery_url = token_data.get("discovery") if token_data else None
    if resolved_discovery_url is None:
        resolved_discovery_url = discovery_url

    resolved_discovery_headers = (
        token_data.get("discovery_headers") if token_data else None
    )
    if resolved_discovery_headers is None:
        resolved_discovery_headers = discovery_headers

    body_bytes, encoded_ct = _encode_body(body, content_type)

    # Compose job fields once, before signing. The exact values that go to
    # the signer must also be what lands in the Livepeer header's JobRequest
    # fields (id/capability/request/parameters/timeout_seconds), otherwise
    # orchestrator VerifySig fails.
    request_str = _build_request_str(body)
    parameters_str = _build_parameters_str(options_filter)
    job_id = secrets.token_hex(16)
    if not resolved_signer_url:
        raise LivepeerGatewayError(
            "byoc_request requires a signer_url (BYOC jobs must be signed by the remote signer)"
        )
    job_sig = sign_byoc_job(
        resolved_signer_url,
        ByocJobSigningInput(
            id=job_id,
            capability=capability_name,
            request=request_str,
            parameters=parameters_str,
            timeout_seconds=timeout_seconds,
        ),
        signer_headers=resolved_signer_headers,
    )
    livepeer_header = _build_livepeer_header(
        id=job_id,
        capability=capability_name,
        request_str=request_str,
        parameters_str=parameters_str,
        timeout_seconds=timeout_seconds,
        sender=job_sig.sender,
        sig=job_sig.signature,
    )

    # BYOC uses HTTP /process/token for per-(sender, capability) discovery,
    # which go-livepeer requires for non-zero PriceInfo + TicketParams
    # (byoc/job_gateway.go:339-403; byoc/types.go:135-145). gRPC
    # GetOrchestrator is not a substitute: it returns the orchestrator's
    # generic transcoding price, which is typically zero on a BYOC-only orch
    # and trips the signer's "missing or zero priceInfo" guard
    # (server/remote_signer.go:253-258).
    #
    # use_tofu is ignored here (the /process/token path uses plain HTTPS);
    # it's still forwarded to PaymentSession for the signer-480 refresh path.
    cursor = job_token_selector(
        resolved_orch_url,
        signer_url=resolved_signer_url,
        signer_headers=resolved_signer_headers,
        discovery_url=resolved_discovery_url,
        discovery_headers=resolved_discovery_headers,
        capability=capability_name,
        options_filter=options_filter,
        timeout=request_timeout,
    )

    rejections: list[OrchestratorRejection] = []
    while True:
        try:
            selected_url, job_token = cursor.next()
        except NoOrchestratorAvailableError as e:
            all_rejections = list(e.rejections) + rejections
            if all_rejections:
                raise NoOrchestratorAvailableError(
                    f"All orchestrators failed ({len(all_rejections)} tried)",
                    rejections=all_rejections,
                ) from None
            raise

        try:
            info = job_token_to_orch_info(job_token)
            session = PaymentSession(
                resolved_signer_url,
                info,
                signer_headers=resolved_signer_headers,
                type=PAYMENT_TYPE_BYOC_REQUEST,
                capabilities=None,
                use_tofu=use_tofu,
                in_pixels=in_pixels,
            )
            try:
                pmt = session.get_payment()
                livepeer_payment = pmt.payment
                livepeer_segment = pmt.seg_creds or ""
            except SkipPaymentCycle:
                # Signer: existing balance covers the fee, no new tickets.
                # BYOC /process/request still requires a payment header; emit
                # a zero-ticket envelope (sender + expected_price only),
                # matching go-livepeer/byoc/payment.go.
                livepeer_payment = _zero_ticket_payment_header(
                    info,
                    resolved_signer_url,
                    resolved_signer_headers,
                )
                livepeer_segment = _seg_creds_for_zero_ticket(info)

            # BYOC job traffic hits the orchestrator's HTTP endpoint, which
            # may be on a different host:port than the gRPC target (the orch
            # advertises it via OrchestratorInfo.transcoder). Fall back to
            # selected_url only when the field is empty.
            http_url = info.transcoder or selected_url

            try:
                resp = _open_byoc_request(
                    http_url,
                    capability_name,
                    body_bytes,
                    content_type=encoded_ct,
                    livepeer_header=livepeer_header,
                    livepeer_payment=livepeer_payment,
                    livepeer_segment=livepeer_segment,
                    request_timeout=request_timeout,
                )
            except HTTPError as e:
                body_text = _extract_error_message(e)
                body_part = f"; body={body_text!r}" if body_text else ""
                raise LivepeerGatewayError(
                    f"BYOC orch error: HTTP {e.code} (url={http_url}){body_part}"
                ) from e
            except (URLError, ConnectionRefusedError) as e:
                raise LivepeerGatewayError(
                    f"BYOC orch error: failed to reach orchestrator: {e} (url={http_url})"
                ) from e

            status = resp.status
            headers = {k: v for k, v in resp.getheaders()}
            if stream:
                return ByocStreamResponse(
                    status_code=status,
                    headers=headers,
                    orch_url=http_url,
                    _response=resp,
                )
            with closing(resp):
                raw = resp.read()
            return ByocResponse(
                status_code=status,
                headers=headers,
                body=raw,
                orch_url=http_url,
            )
        except LivepeerGatewayError as e:
            _LOG.debug(
                "byoc_request candidate failed, trying fallback if available: %s (%s)",
                selected_url,
                str(e),
            )
            rejections.append(OrchestratorRejection(url=selected_url, reason=str(e)))
