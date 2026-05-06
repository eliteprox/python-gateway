from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Optional

from .errors import LivepeerGatewayError


def encode_gateway_token(billing_base_url: str, billing_access_token: str, *, discovery_url: Optional[str] = None) -> str:
    """
    Build a base64 gateway ``token`` JSON string from a billing base URL and a
    pre-issued signer access token (JWT). Equivalent to passing the same values
    via ``billing_url`` + ``billing_access_token`` keyword arguments.
    """
    base = billing_base_url.rstrip("/")
    tok = billing_access_token.strip()
    if not tok:
        raise LivepeerGatewayError("billing_access_token must be non-empty")
    payload: dict[str, Any] = {
        "billing": base,
        "billing_access_token": tok,
    }
    if discovery_url is not None:
        disc = discovery_url.strip()
        if disc:
            payload["discovery"] = disc
    compact = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(compact).decode("ascii")


def _is_str_dict(v: object) -> bool:
    return isinstance(v, dict) and all(isinstance(k, str) and isinstance(val, str) for k, val in v.items())


def parse_token(token: str) -> dict[str, Any]:
    token = token.strip()
    try:
        decoded = base64.b64decode(token, validate=True)
    except (binascii.Error, ValueError) as e:
        raise LivepeerGatewayError("Invalid token: expected base64-encoded JSON") from e

    try:
        payload = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise LivepeerGatewayError("Invalid token: expected UTF-8 JSON payload") from e

    if not isinstance(payload, dict):
        raise LivepeerGatewayError("Invalid token: payload must be a JSON object")

    signer = payload.get("signer")
    discovery = payload.get("discovery")
    billing = payload.get("billing")
    if signer is not None and not isinstance(signer, str):
        raise LivepeerGatewayError("Invalid token: signer must be a string")
    if discovery is not None and not isinstance(discovery, str):
        raise LivepeerGatewayError("Invalid token: discovery must be a string")
    if billing is not None and not isinstance(billing, str):
        raise LivepeerGatewayError("Invalid token: billing must be a string")

    billing_access_token_raw = payload.get("billing_access_token")
    if billing_access_token_raw is not None:
        if not isinstance(billing_access_token_raw, str) or not billing_access_token_raw.strip():
            raise LivepeerGatewayError(
                "Invalid token: billing_access_token must be a non-empty string when present"
            )

    signer_headers = payload.get("signer_headers")
    discovery_headers = payload.get("discovery_headers")
    orchestrators = payload.get("orchestrators")
    if signer_headers is not None and not _is_str_dict(signer_headers):
        raise LivepeerGatewayError("Invalid token: signer_headers must be a {string: string} object")
    if discovery_headers is not None and not _is_str_dict(discovery_headers):
        raise LivepeerGatewayError("Invalid token: discovery_headers must be a {string: string} object")
    if orchestrators is not None and not isinstance(orchestrators, list):
        raise LivepeerGatewayError("Invalid token: orchestrators must be an array of strings")

    normalized_orchestrators: Optional[list[str]] = None
    if isinstance(orchestrators, list):
        normalized_orchestrators = []
        for item in orchestrators:
            if not isinstance(item, str) or not item.strip():
                raise LivepeerGatewayError(
                    "Invalid token: orchestrators must contain only non-empty strings"
                )
            normalized_orchestrators.append(item.strip())

    billing_access_token_norm: Optional[str] = None
    if isinstance(billing_access_token_raw, str):
        billing_access_token_norm = billing_access_token_raw.strip()

    return {
        "orchestrators": normalized_orchestrators,
        "signer": signer,
        "discovery": discovery,
        "billing": billing,
        "billing_access_token": billing_access_token_norm,
        "signer_headers": signer_headers,
        "discovery_headers": discovery_headers,
    }
