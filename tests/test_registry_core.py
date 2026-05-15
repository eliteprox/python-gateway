"""Tests for coordinator registry parsing, selection, broker headers, and dispatch wiring."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import httpx

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.broker_http import (
    broker_v1_cap_url,
    build_livepeer_headers,
    parse_work_units,
)
from livepeer_gateway.registry_mode_plugins import (
    HttpStreamPlugin,
    parse_session_open_json,
    plugin_for_candidate,
    registry_dispatch_cap,
)
from livepeer_gateway.registry_parser import (
    flatten_manifest_to_candidates,
    parse_coordinator_signed_manifest_bytes,
)
from livepeer_gateway.registry_selector import select_registry_candidates
from livepeer_gateway.registry_types import RegistryRouteCandidate


def _minimal_registry_doc() -> bytes:
    doc = {
        "manifest": {
            "spec_version": "0.1.0",
            "publication_seq": 1,
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": "2027-01-01T00:00:00Z",
            "orch": {
                "eth_address": "0xd00354656922168815fcd1e51cbddb9e359e3c7f",
            },
            "capabilities": [
                {
                    "capability_id": "daydream:scope:v1",
                    "offering_id": "default",
                    "interaction_mode": "session-control-external-media@v0",
                    "work_unit": {"name": "session"},
                    "price_per_unit_wei": "1000",
                    "worker_url": "https://ai-rig-worker.example.com/path",
                    "extra": {"tier": "gpu"},
                    "constraints": {"region": "us-east"},
                },
                {
                    "capability_id": "other:cap",
                    "offering_id": "o1",
                    "interaction_mode": "http-reqresp@v0",
                    "work_unit": {"name": "request"},
                    "price_per_unit_wei": "500",
                    "worker_url": "https://http-worker.example.com",
                },
            ],
        },
        "signature": {
            "algorithm": "ed25519",
            "value": "deadbeef",
        },
    }
    return json.dumps(doc).encode("utf-8")


class RegistryParseSelectTests(unittest.TestCase):
    def test_parse_flatten_and_select(self) -> None:
        signed = parse_coordinator_signed_manifest_bytes(_minimal_registry_doc())
        self.assertEqual(signed.manifest.orch.eth_address, "0xd00354656922168815fcd1e51cbddb9e359e3c7f")
        cands = flatten_manifest_to_candidates(signed.manifest)
        self.assertEqual(len(cands), 2)
        picked = select_registry_candidates(
            cands,
            capability_id="daydream:scope:v1",
            offering_id="default",
            interaction_mode="session-control-external-media@v0",
        )
        self.assertEqual(len(picked), 1)
        self.assertTrue(picked[0].worker_url.endswith("example.com/path"))

    def test_select_max_price(self) -> None:
        signed = parse_coordinator_signed_manifest_bytes(_minimal_registry_doc())
        cands = flatten_manifest_to_candidates(signed.manifest)
        cheap = select_registry_candidates(
            cands,
            interaction_mode="http-reqresp@v0",
            max_price_per_unit_wei=400,
        )
        self.assertEqual(len(cheap), 0)
        ok = select_registry_candidates(
            cands,
            interaction_mode="http-reqresp@v0",
            max_price_per_unit_wei=600,
        )
        self.assertEqual(len(ok), 1)


class BrokerHeaderTests(unittest.TestCase):
    def test_broker_v1_cap_url_joins(self) -> None:
        self.assertEqual(
            broker_v1_cap_url("https://worker.example/base/"),
            "https://worker.example/base/v1/cap",
        )

    def test_build_headers_and_work_units(self) -> None:
        h = build_livepeer_headers(
            mode="http-reqresp@v0",
            capability_id="c",
            offering_id="o",
            payment_b64="AAA",
            request_id="rid-1",
        )
        self.assertEqual(h["Livepeer-Mode"], "http-reqresp@v0")
        self.assertEqual(h["Livepeer-Request-Id"], "rid-1")
        self.assertEqual(parse_work_units({"Livepeer-Work-Units": "3"}), 3)


class ModePluginTests(unittest.TestCase):
    def test_plugin_resolution_and_stream_accept(self) -> None:
        c = RegistryRouteCandidate(
            orch_eth_address="0xd00354656922168815fcd1e51cbddb9e359e3c7f",
            worker_url="https://w.example",
            capability_id="c",
            offering_id="o",
            interaction_mode="http-stream@v0",
            price_per_unit_wei="1",
            work_unit_name="x",
            extra={},
            constraints={},
        )
        p = plugin_for_candidate(c)
        self.assertIsInstance(p, HttpStreamPlugin)

    def test_parse_session_open_json_success(self) -> None:
        r = httpx.Response(
            202,
            content=b'{"session_id":"s1","control_url":"wss://x","media":{"schema":"z","scope_url":"https://y/"}}',
        )
        data = parse_session_open_json(r)
        self.assertEqual(data.get("session_id"), "s1")

    def test_parse_session_open_json_error_status(self) -> None:
        r = httpx.Response(500, content=b"oops")
        with self.assertRaises(LivepeerGatewayError):
            parse_session_open_json(r)


class RegistryDispatchWireTests(unittest.TestCase):
    def test_registry_dispatch_calls_signer_then_broker(self) -> None:
        candidate = RegistryRouteCandidate(
            orch_eth_address="0xd00354656922168815fcd1e51cbddb9e359e3c7f",
            worker_url="https://ai-rig-worker.example.com",
            capability_id="daydream:scope:v1",
            offering_id="default",
            interaction_mode="http-reqresp@v0",
            price_per_unit_wei="10",
            work_unit_name="request",
            extra={},
            constraints={},
        )
        broker_resp = MagicMock(spec=httpx.Response)
        broker_resp.status_code = 200
        with patch(
            "livepeer_gateway.registry_payment_session.post_json",
            return_value={"payment": "cGF5", "state": {}},
        ) as pj:
            with patch(
                "livepeer_gateway.registry_mode_plugins.post_v1_cap",
                return_value=broker_resp,
            ) as pc:
                out = registry_dispatch_cap(
                    signer_url="https://signer.example/api/signer",
                    candidate=candidate,
                    face_value_wei=99,
                    body=b'{"q":1}',
                    content_type="application/json",
                )
        self.assertIs(out, broker_resp)
        self.assertEqual(pj.call_count, 1)
        args, kwargs = pj.call_args
        payload = args[1]
        self.assertEqual(payload.get("paymentMode"), "registry")
        self.assertEqual(payload.get("ticketParamsBaseUrl"), "https://ai-rig-worker.example.com")
        self.assertEqual(payload.get("faceValueWei"), "99")
        self.assertEqual(pc.call_count, 1)


if __name__ == "__main__":
    unittest.main()
