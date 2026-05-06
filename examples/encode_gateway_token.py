#!/usr/bin/env python3
"""Encode billing URL + access token into one ``--token`` string (see write_frames.py)."""

from __future__ import annotations

import argparse
import sys

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.token import encode_gateway_token


def main() -> None:
    p = argparse.ArgumentParser(
        description="Print a base64 gateway token from billing URL and signer access token (JWT)."
    )
    p.add_argument(
        "--billing-url",
        required=True,
        help="Billing gateway base URL (e.g. http://localhost:3001).",
    )
    p.add_argument(
        "--discovery",
        default=None,
        help="Optional orchestrator discovery URL override.",
    )
    p.add_argument(
        "access_token",
        nargs="?",
        default=None,
        help="Signer access token / JWT; if omitted, read from stdin.",
    )
    args = p.parse_args()
    tok = args.access_token
    if tok is None:
        tok = sys.stdin.read()
    try:
        print(encode_gateway_token(args.billing_url, tok, discovery_url=args.discovery))
    except LivepeerGatewayError as e:
        raise SystemExit(str(e)) from e


if __name__ == "__main__":
    main()
