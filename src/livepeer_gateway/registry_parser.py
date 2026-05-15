"""Fetch and parse coordinator `/.well-known/livepeer-registry.json` documents."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

import httpx

from .errors import LivepeerGatewayError
from .registry_types import (
    CoordinatorCapabilityTuple,
    CoordinatorEnvelopeSignature,
    CoordinatorManifestPayload,
    CoordinatorOrch,
    CoordinatorSignedManifest,
    CoordinatorWorkUnit,
    RegistryRouteCandidate,
)

_HEX_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _require_str(obj: Mapping[str, Any], key: str) -> str:
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        raise LivepeerGatewayError(f"registry manifest missing or invalid string field {key!r}")
    return v.strip()


def _optional_str(obj: Mapping[str, Any], key: str) -> str | None:
    v = obj.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s or None


def _require_dict(obj: Mapping[str, Any], key: str) -> dict[str, Any]:
    v = obj.get(key)
    if not isinstance(v, dict):
        raise LivepeerGatewayError(f"registry manifest missing object field {key!r}")
    return v


def _optional_dict(obj: Mapping[str, Any], key: str) -> dict[str, Any]:
    v = obj.get(key)
    if v is None:
        return {}
    if not isinstance(v, dict):
        return {}
    return dict(v)


def validate_worker_https(url: str) -> None:
    p = urlparse(url)
    if p.scheme != "https":
        raise LivepeerGatewayError(f"registry worker_url must use https scheme: {url!r}")
    if not p.netloc:
        raise LivepeerGatewayError(f"registry worker_url missing host: {url!r}")


def _parse_work_unit(raw: Mapping[str, Any]) -> CoordinatorWorkUnit:
    wu = _require_dict(raw, "work_unit")
    name = _require_str(wu, "name")
    return CoordinatorWorkUnit(name=name)


def _parse_capability(raw: Mapping[str, Any]) -> CoordinatorCapabilityTuple:
    worker = _require_str(raw, "worker_url")
    validate_worker_https(worker)
    return CoordinatorCapabilityTuple(
        capability_id=_require_str(raw, "capability_id"),
        offering_id=_require_str(raw, "offering_id"),
        interaction_mode=_require_str(raw, "interaction_mode"),
        work_unit=_parse_work_unit(raw),
        price_per_unit_wei=_require_str(raw, "price_per_unit_wei"),
        worker_url=worker,
        extra=_optional_dict(raw, "extra"),
        constraints=_optional_dict(raw, "constraints"),
    )


def _parse_orch(raw: Mapping[str, Any]) -> CoordinatorOrch:
    eth = _require_str(raw, "eth_address")
    if not _HEX_ADDR.match(eth):
        raise LivepeerGatewayError("registry manifest orch.eth_address must be 0x-prefixed 40 hex chars")
    return CoordinatorOrch(
        eth_address=eth.lower(),
        service_uri=_optional_str(raw, "service_uri"),
    )


def _parse_manifest_payload(raw: Mapping[str, Any]) -> CoordinatorManifestPayload:
    pub = raw.get("publication_seq")
    if not isinstance(pub, int) or pub < 0:
        raise LivepeerGatewayError("registry manifest publication_seq must be a non-negative integer")
    orch = _parse_orch(_require_dict(raw, "orch"))
    caps_raw = raw.get("capabilities")
    if not isinstance(caps_raw, list) or not caps_raw:
        raise LivepeerGatewayError("registry manifest capabilities must be a non-empty array")
    caps: list[CoordinatorCapabilityTuple] = []
    for i, item in enumerate(caps_raw):
        if not isinstance(item, dict):
            raise LivepeerGatewayError(f"registry manifest capabilities[{i}] must be an object")
        caps.append(_parse_capability(item))
    return CoordinatorManifestPayload(
        spec_version=_require_str(raw, "spec_version"),
        publication_seq=pub,
        issued_at=_require_str(raw, "issued_at"),
        expires_at=_require_str(raw, "expires_at"),
        orch=orch,
        capabilities=tuple(caps),
    )


def _parse_signature(raw: Mapping[str, Any]) -> CoordinatorEnvelopeSignature:
    return CoordinatorEnvelopeSignature(
        algorithm=_require_str(raw, "algorithm"),
        value=_require_str(raw, "value"),
        canonicalization=_optional_str(raw, "canonicalization"),
    )


def parse_coordinator_signed_manifest_bytes(data: bytes) -> CoordinatorSignedManifest:
    try:
        root = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise LivepeerGatewayError(f"registry JSON parse error: {e}") from e
    if not isinstance(root, dict):
        raise LivepeerGatewayError("registry document root must be a JSON object")
    man = _require_dict(root, "manifest")
    sig = _require_dict(root, "signature")
    return CoordinatorSignedManifest(
        manifest=_parse_manifest_payload(man),
        signature=_parse_signature(sig),
    )


def flatten_manifest_to_candidates(manifest: CoordinatorManifestPayload) -> tuple[RegistryRouteCandidate, ...]:
    orch_addr = manifest.orch.eth_address
    out: list[RegistryRouteCandidate] = []
    for cap in manifest.capabilities:
        out.append(
            RegistryRouteCandidate(
                orch_eth_address=orch_addr,
                worker_url=cap.worker_url.rstrip("/"),
                capability_id=cap.capability_id,
                offering_id=cap.offering_id,
                interaction_mode=cap.interaction_mode,
                price_per_unit_wei=cap.price_per_unit_wei,
                work_unit_name=cap.work_unit.name,
                extra=dict(cap.extra),
                constraints=dict(cap.constraints),
            )
        )
    return tuple(out)


def registry_well_known_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if not base:
        raise LivepeerGatewayError("registry base URL is empty")
    low = base.lower()
    if low.endswith("/.well-known/livepeer-registry.json"):
        return base
    return urljoin(base.rstrip("/") + "/", ".well-known/livepeer-registry.json")


def fetch_coordinator_registry(
    base_or_well_known_url: str,
    *,
    timeout_s: float = 30.0,
    headers: dict[str, str] | None = None,
) -> CoordinatorSignedManifest:
    url = registry_well_known_url(base_or_well_known_url)
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            r = client.get(url, headers=headers)
    except httpx.RequestError as e:
        raise LivepeerGatewayError(f"registry fetch failed: {e}") from e
    if r.status_code != 200:
        raise LivepeerGatewayError(
            f"registry fetch HTTP {r.status_code} from {url!r}",
        )
    return parse_coordinator_signed_manifest_bytes(r.content)
