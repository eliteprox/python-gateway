import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Optional

from livepeer_gateway import (
    BYOCProcessRequest,
    LivepeerGatewayError,
    process_byoc_request,
    stream_byoc_request,
)


def _parse_orchestrator_arg(orchestrator_arg: Optional[str]):
    if orchestrator_arg is None:
        return None
    parts = [part.strip() for part in orchestrator_arg.split(",") if part.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return parts


def _load_json_arg(value: Optional[str], file_path: Optional[str]) -> dict[str, Any]:
    if value is not None and file_path is not None:
        raise SystemExit("pass only one of --body-json or --body-file")
    if file_path is not None:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    elif value is not None:
        data = json.loads(value)
    else:
        data = {}
    if not isinstance(data, dict):
        raise SystemExit("request body must be a JSON object")
    return data


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Call a BYOC Pipeline through the Python SDK.")
    parser.add_argument("--capability", required=True, help="Registered BYOC capability name.")
    parser.add_argument("--route", default="run", help="Worker route after /process/request/.")
    parser.add_argument("--stream", action="store_true", help="Read the response as SSE.")
    parser.add_argument("--body-json", default=None, help="JSON object request body.")
    parser.add_argument("--body-file", default=None, help="Path to a JSON request body file.")
    parser.add_argument("--orchestrator", default=None, help="Optional orchestrator URL or comma-separated URLs.")
    parser.add_argument("--signer", default=None, help="Remote signer base URL.")
    parser.add_argument("--discovery", default=None, help="Optional discovery endpoint URL.")
    parser.add_argument(
        "--token",
        default=os.environ.get("LIVEPEER_TOKEN"),
        help="Gateway token containing signer/discovery/orchestrator info. Defaults to LIVEPEER_TOKEN.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=30, help="Signed BYOC request timeout.")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
    return parser.parse_args()


async def _stream(args: argparse.Namespace, body: dict[str, Any]) -> None:
    stream = stream_byoc_request(
        _parse_orchestrator_arg(args.orchestrator),
        BYOCProcessRequest(
            capability=args.capability,
            route=args.route,
            body=body,
            timeout_seconds=args.timeout_seconds,
        ),
        token=args.token,
        signer_url=args.signer,
        discovery_url=args.discovery,
    )
    async for event in stream.events:
        if event.event != "message":
            print(f"event: {event.event}")
        print(f"data: {event.data}\n", flush=True)
        if event.data == "[DONE]":
            return


def _request(args: argparse.Namespace, body: dict[str, Any]) -> None:
    response = process_byoc_request(
        _parse_orchestrator_arg(args.orchestrator),
        BYOCProcessRequest(
            capability=args.capability,
            route=args.route,
            body=body,
            timeout_seconds=args.timeout_seconds,
        ),
        token=args.token,
        signer_url=args.signer,
        discovery_url=args.discovery,
    )
    if isinstance(response.body, (dict, list)):
        print(json.dumps(response.body, separators=(",", ":")))
    elif response.body is None:
        print("")
    else:
        print(response.body)


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    body = _load_json_arg(args.body_json, args.body_file)
    try:
        if args.stream:
            asyncio.run(_stream(args, body))
        else:
            _request(args, body)
    except LivepeerGatewayError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
