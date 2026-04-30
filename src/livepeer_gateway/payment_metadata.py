"""Canonical payment attribution metadata envelope for PymtHouse billing.

This module defines the stable metadata shape that ``python-gateway`` embeds in
every ``/generate-live-payment`` payload so PymtHouse can attribute usage to a
specific pipeline/model combination and gateway session.

PymtHouse treats this data as a *claim* and validates it by matching the signed
ticket price/unit facts against NaaP advertised pricing before recording
billable usage.  No changes to the go-livepeer remote signer are required: the
signer continues to sign the normal price facts; PymtHouse provides the trust
by verifying the claimed pipeline/model against the signed economics.

Version string: bump when the shape changes in a backward-incompatible way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

PAYMENT_METADATA_VERSION = "2026-04-usage-attribution-v1"

ATTRIBUTION_SOURCE_PYMTHOUSE_GATEWAY = "pymthouse_gateway"
ATTRIBUTION_SOURCE_PYTHON_GATEWAY = "python_gateway"
ATTRIBUTION_SOURCE_DIRECT_API = "direct_api"


@dataclass(frozen=True)
class PaymentAttributionMetadata:
    """Attribution envelope embedded in the generate-live-payment payload.

    Fields
    ------
    pipeline:
        Canonical pipeline identifier matching the NaaP catalog
        (e.g. ``"text-to-image"``).  Required for billable usage.
    model_id:
        Canonical model identifier within the pipeline
        (e.g. ``"SG161222/RealVisXL_V4.0_Lightning"``).  Required for billable usage.
    attribution_source:
        Identifies which component supplied the metadata.  Use one of the
        ``ATTRIBUTION_SOURCE_*`` constants.  Defaults to
        ``ATTRIBUTION_SOURCE_PYTHON_GATEWAY``.
    gateway_request_id:
        Opaque job/request/session identifier assigned by the calling gateway.
        Enables cross-system correlation between gateway job logs and PymtHouse
        usage records.  May be ``None`` for direct API callers.
    metadata_version:
        Version sentinel.  PymtHouse uses this to choose the correct parser if
        the shape evolves.  Defaults to ``PAYMENT_METADATA_VERSION``.
    """

    pipeline: str
    model_id: str
    attribution_source: str = ATTRIBUTION_SOURCE_PYTHON_GATEWAY
    gateway_request_id: Optional[str] = None
    metadata_version: str = field(default=PAYMENT_METADATA_VERSION)

    def __post_init__(self) -> None:
        if not self.pipeline or not self.pipeline.strip():
            raise ValueError("pipeline must be a non-empty string")
        if not self.model_id or not self.model_id.strip():
            raise ValueError("model_id must be a non-empty string")

    def to_payload_fields(self) -> dict[str, str]:
        """Return a flat dict of fields suitable for merging into a payment payload.

        All keys use camelCase to match the existing PymtHouse JSON conventions.
        """
        out: dict[str, str] = {
            "paymentMetadataVersion": self.metadata_version,
            "attributionSource": self.attribution_source,
            "pipeline": self.pipeline,
            "modelId": self.model_id,
        }
        if self.gateway_request_id is not None:
            out["gatewayRequestId"] = self.gateway_request_id
        return out
