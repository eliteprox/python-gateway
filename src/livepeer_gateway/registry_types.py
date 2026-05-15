"""Coordinator signed manifest types (livepeer-registry.json envelope)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class CoordinatorWorkUnit:
    name: str


@dataclass(frozen=True)
class CoordinatorOrch:
    eth_address: str
    service_uri: str | None = None


@dataclass(frozen=True)
class CoordinatorCapabilityTuple:
    capability_id: str
    offering_id: str
    interaction_mode: str
    work_unit: CoordinatorWorkUnit
    price_per_unit_wei: str
    worker_url: str
    extra: Mapping[str, Any] = field(default_factory=dict)
    constraints: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CoordinatorManifestPayload:
    spec_version: str
    publication_seq: int
    issued_at: str
    expires_at: str
    orch: CoordinatorOrch
    capabilities: tuple[CoordinatorCapabilityTuple, ...]


@dataclass(frozen=True)
class CoordinatorEnvelopeSignature:
    algorithm: str
    value: str
    canonicalization: str | None = None


@dataclass(frozen=True)
class CoordinatorSignedManifest:
    manifest: CoordinatorManifestPayload
    signature: CoordinatorEnvelopeSignature


@dataclass(frozen=True)
class RegistryRouteCandidate:
    """Flattened capability row for gateway selection."""

    orch_eth_address: str
    worker_url: str
    capability_id: str
    offering_id: str
    interaction_mode: str
    price_per_unit_wei: str
    work_unit_name: str
    extra: Mapping[str, Any]
    constraints: Mapping[str, Any]
