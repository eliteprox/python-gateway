"""Tests for the canonical payment attribution metadata envelope."""

from __future__ import annotations

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from livepeer_gateway.payment_metadata import (
    ATTRIBUTION_SOURCE_DIRECT_API,
    ATTRIBUTION_SOURCE_PYMTHOUSE_GATEWAY,
    ATTRIBUTION_SOURCE_PYTHON_GATEWAY,
    PAYMENT_METADATA_VERSION,
    PaymentAttributionMetadata,
)


class TestPaymentAttributionMetadata(unittest.TestCase):
    def test_to_payload_fields_canonical_keys(self):
        meta = PaymentAttributionMetadata(pipeline="text-to-image", model_id="stabilityai/sdxl")
        fields = meta.to_payload_fields()
        self.assertEqual(fields["paymentMetadataVersion"], PAYMENT_METADATA_VERSION)
        self.assertEqual(fields["attributionSource"], ATTRIBUTION_SOURCE_PYTHON_GATEWAY)
        self.assertEqual(fields["pipeline"], "text-to-image")
        self.assertEqual(fields["modelId"], "stabilityai/sdxl")
        self.assertNotIn("gatewayRequestId", fields)

    def test_to_payload_fields_uses_camel_case_model_id(self):
        meta = PaymentAttributionMetadata(pipeline="p", model_id="m")
        fields = meta.to_payload_fields()
        self.assertIn("modelId", fields)
        self.assertNotIn("model_id", fields)

    def test_to_payload_fields_with_gateway_request_id(self):
        meta = PaymentAttributionMetadata(
            pipeline="image-to-video",
            model_id="stable-video-diffusion",
            attribution_source=ATTRIBUTION_SOURCE_PYMTHOUSE_GATEWAY,
            gateway_request_id="job-abc-123",
        )
        fields = meta.to_payload_fields()
        self.assertEqual(fields["gatewayRequestId"], "job-abc-123")
        self.assertEqual(fields["attributionSource"], ATTRIBUTION_SOURCE_PYMTHOUSE_GATEWAY)

    def test_to_payload_fields_direct_api(self):
        meta = PaymentAttributionMetadata(
            pipeline="text-to-image",
            model_id="my-model",
            attribution_source=ATTRIBUTION_SOURCE_DIRECT_API,
        )
        self.assertEqual(meta.to_payload_fields()["attributionSource"], ATTRIBUTION_SOURCE_DIRECT_API)

    def test_validates_empty_pipeline(self):
        with self.assertRaises(ValueError):
            PaymentAttributionMetadata(pipeline="", model_id="model")

    def test_validates_whitespace_pipeline(self):
        with self.assertRaises(ValueError):
            PaymentAttributionMetadata(pipeline="   ", model_id="model")

    def test_validates_empty_model_id(self):
        with self.assertRaises(ValueError):
            PaymentAttributionMetadata(pipeline="pipeline", model_id="")

    def test_frozen(self):
        meta = PaymentAttributionMetadata(pipeline="p", model_id="m")
        with self.assertRaises((AttributeError, TypeError)):
            meta.pipeline = "other"  # type: ignore[misc]

    def test_version_constant_non_empty(self):
        self.assertTrue(PAYMENT_METADATA_VERSION)


class TestPaymentPayloadEmbedding(unittest.TestCase):
    """Verify that BasePaymentSession embeds metadata when attribution is set."""

    def _make_session(self, attribution=None):
        from unittest.mock import MagicMock
        from livepeer_gateway.payments_base import BasePaymentSession

        info = MagicMock()
        info.SerializeToString.return_value = b"\x00"

        class _Session(BasePaymentSession):
            def _offchain_payment(self):
                raise NotImplementedError

        session = _Session(
            signer_url=None,
            info=info,
            signer_headers=None,
            type="lv2v",
            capabilities=None,
            attribution=attribution,
        )
        session._manifest_id = "manifest-1"
        return session

    def test_payload_includes_metadata_fields(self):
        meta = PaymentAttributionMetadata(
            pipeline="text-to-image",
            model_id="stabilityai/sdxl",
            gateway_request_id="req-001",
        )
        session = self._make_session(attribution=meta)
        payload = session._build_payment_payload()
        self.assertEqual(payload["pipeline"], "text-to-image")
        self.assertEqual(payload["modelId"], "stabilityai/sdxl")
        self.assertEqual(payload["gatewayRequestId"], "req-001")
        self.assertEqual(payload["paymentMetadataVersion"], PAYMENT_METADATA_VERSION)
        self.assertEqual(payload["attributionSource"], ATTRIBUTION_SOURCE_PYTHON_GATEWAY)

    def test_payload_without_attribution_has_no_metadata_keys(self):
        session = self._make_session(attribution=None)
        payload = session._build_payment_payload()
        self.assertNotIn("pipeline", payload)
        self.assertNotIn("modelId", payload)
        self.assertNotIn("paymentMetadataVersion", payload)

    def test_payment_calls_include_pipeline_without_signer_changes(self):
        """Payment calls carry pipeline/model without requiring remote signer metadata support."""
        meta = PaymentAttributionMetadata(
            pipeline="text-to-image",
            model_id="stabilityai/sdxl",
            gateway_request_id="req-no-signer",
        )
        session = self._make_session(attribution=meta)
        payload = session._build_payment_payload()
        # The payload is sent to the remote signer without requiring signer-side changes
        self.assertIn("pipeline", payload)
        self.assertIn("modelId", payload)
        self.assertIn("RequestID", payload)


if __name__ == "__main__":
    unittest.main()
