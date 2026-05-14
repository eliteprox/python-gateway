import base64
import json
import unittest

from livepeer_gateway.byoc import _resolve_byoc_token


def _token(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


class BYOCTokenResolutionTest(unittest.TestCase):
    def test_token_orchestrators_take_precedence(self) -> None:
        resolved = _resolve_byoc_token(
            _token({"orchestrators": ["https://token-orch:8935"]}),
            orch_url="https://explicit-orch:8935",
            signer_url=None,
            signer_headers=None,
            discovery_url=None,
            discovery_headers=None,
        )

        self.assertEqual(resolved[0], ["https://token-orch:8935"])

    def test_token_supplies_missing_signer_and_discovery(self) -> None:
        resolved = _resolve_byoc_token(
            _token(
                {
                    "signer": "https://signer.example",
                    "discovery": "https://discovery.example",
                    "signer_headers": {"Authorization": "Bearer signer"},
                    "discovery_headers": {"Authorization": "Bearer discovery"},
                }
            ),
            orch_url=None,
            signer_url=None,
            signer_headers=None,
            discovery_url=None,
            discovery_headers=None,
        )

        self.assertEqual(resolved[1], "https://signer.example")
        self.assertEqual(resolved[2], {"Authorization": "Bearer signer"})
        self.assertEqual(resolved[3], "https://discovery.example")
        self.assertEqual(resolved[4], {"Authorization": "Bearer discovery"})

    def test_explicit_signer_and_discovery_win_when_set(self) -> None:
        resolved = _resolve_byoc_token(
            _token({"signer": "https://token-signer", "discovery": "https://token-discovery"}),
            orch_url=None,
            signer_url="https://explicit-signer",
            signer_headers=None,
            discovery_url="https://explicit-discovery",
            discovery_headers=None,
        )

        self.assertEqual(resolved[1], "https://explicit-signer")
        self.assertEqual(resolved[3], "https://explicit-discovery")


if __name__ == "__main__":
    unittest.main()
