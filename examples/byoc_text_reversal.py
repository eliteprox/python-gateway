import argparse
import json
import logging

from livepeer_gateway import ByocResponse, ByocStreamResponse, byoc_request
from livepeer_gateway.errors import LivepeerGatewayError


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a BYOC text-reversal request through the Python gateway."
    )
    parser.add_argument(
        "--text",
        default="hello from byoc",
        help="Input text to reverse. Default: 'hello from byoc'.",
    )
    parser.add_argument(
        "--capability",
        default="text-reversal",
        help="BYOC capability name. Default: text-reversal.",
    )
    parser.add_argument(
        "--in-pixels",
        type=int,
        default=120,
        help=(
            "Payment budget units for BYOC requests. "
            "For BYOC this is effectively pre-funded seconds. Default: 120."
        ),
    )
    parser.add_argument(
        "--orchestrator",
        default=None,
        help="Optional orchestrator URL or comma-separated URLs.",
    )
    parser.add_argument(
        "--signer",
        default=None,
        help="Remote signer base URL.",
    )
    parser.add_argument(
        "--discovery",
        default=None,
        help="Optional discovery endpoint URL.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Optional gateway token containing signer/discovery/orchestrator info.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=30,
        help="Timeout encoded into the BYOC Livepeer header. Default: 30.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=30.0,
        help="HTTP request timeout in seconds. Default: 30.0.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args()


def _parse_orchestrator_arg(orchestrator_arg: str | None) -> str | list[str] | None:
    if orchestrator_arg is None:
        return None
    parts = [part.strip() for part in orchestrator_arg.split(",") if part.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return parts


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    orch_url = _parse_orchestrator_arg(args.orchestrator)
    payload = {"text": args.text}

    try:
        response = byoc_request(
            capability_name=args.capability,
            body=payload,
            in_pixels=args.in_pixels,
            orch_url=orch_url,
            token=args.token,
            signer_url=args.signer,
            discovery_url=args.discovery,
            content_type="application/json",
            timeout_seconds=args.timeout_seconds,
            request_timeout=args.request_timeout,
            stream=False,
        )
    except LivepeerGatewayError as err:
        print(f"ERROR: {err}")
        return

    if isinstance(response, ByocStreamResponse):
        print("Unexpected streaming response for text-reversal request.")
        print("Use stream=False for this example.")
        return

    if not isinstance(response, ByocResponse):
        print(f"Unexpected response type: {type(response).__name__}")
        return

    print("=== BYOC text-reversal response ===")
    print(f"Orchestrator: {response.orch_url}")
    print(f"HTTP status:  {response.status_code}")
    print("Headers:", json.dumps(response.headers, indent=2, sort_keys=True))
    print()

    try:
        body_json = response.json()
        print("Body (json):", json.dumps(body_json, indent=2, sort_keys=True))
    except Exception:
        print("Body (text):", response.text())


if __name__ == "__main__":
    main()
