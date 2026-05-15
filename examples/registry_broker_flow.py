#!/usr/bin/env python3
"""
Coordinator registry → select candidate → registry payment → POST /v1/cap.

Signer resolution matches ``write_frames.py``:
  - ``--signer`` alone, or
  - ``--billing-url`` with OIDC (optional ``--client-id``, ``--browser``) or ``--billing-access-token``, or
  - ``--token`` (base64 gateway JSON) for embedded signer/billing.

For HTTP-family modes (e.g. ``http-reqresp@v0``), use ``--body-json`` and optional ``--content-type``.
Session modes (``session-control-external-media@v0``, ``rtmp-ingress-hls-egress@v0``) parse JSON from 202 responses.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.lv2v import _resolve_billing
from livepeer_gateway.registry_mode_plugins import parse_session_open_json, registry_dispatch_cap
from livepeer_gateway.registry_parser import fetch_coordinator_registry, flatten_manifest_to_candidates
from livepeer_gateway.registry_selector import select_registry_candidates
from livepeer_gateway.token import parse_token


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--registry-url",
        default=os.environ.get("REGISTRY_URL", "https://coordinator.xodeapp.xyz").strip(),
        help="Coordinator base URL or full /.well-known/livepeer-registry.json URL.",
    )
    p.add_argument(
        "--signer",
        default=os.environ.get("SIGNER_URL", "").strip() or None,
        help="Remote signer base URL (overrides billing resolution when set alone).",
    )
    p.add_argument(
        "--billing-url",
        default=None,
        help="PymtHouse / billing gateway URL; resolves {base}/api/signer + Bearer when OIDC or token is used.",
    )
    p.add_argument(
        "--billing-access-token",
        default=None,
        metavar="JWT",
        help="Skip OIDC: Bearer for {billing-url}/api/signer.",
    )
    p.add_argument(
        "--client-id",
        default=None,
        help="OIDC client ID for billing gateway (default inside gateway: livepeer-sdk).",
    )
    p.add_argument(
        "--browser",
        action="store_true",
        help="Browser PKCE login instead of device flow (with --billing-url + OIDC).",
    )
    p.add_argument(
        "--token",
        default=None,
        help="Base64 gateway token (signer / billing / signer_headers).",
    )
    p.add_argument(
        "--capability-id",
        default=os.environ.get("CAPABILITY_ID", "").strip() or None,
        help="Filter registry candidates by capability_id.",
    )
    p.add_argument(
        "--offering-id",
        default=os.environ.get("OFFERING_ID", "").strip() or None,
        help="Filter registry candidates by offering_id.",
    )
    p.add_argument(
        "--interaction-mode",
        default=os.environ.get("INTERACTION_MODE", "http-reqresp@v0").strip(),
        help="Filter by interaction_mode (default: http-reqresp@v0).",
    )
    p.add_argument(
        "--face-value-wei",
        type=int,
        default=int(os.environ.get("FACE_VALUE_WEI", "0") or "0"),
        help="Explicit payment face value in wei (overrides price × work units when > 0).",
    )
    p.add_argument(
        "--work-units",
        type=int,
        default=int(os.environ.get("WORK_UNITS", "1") or "1"),
        help="Work units for registryPricePerUnitWei pricing when --face-value-wei is 0.",
    )
    p.add_argument(
        "--manifest-id",
        default=os.environ.get("MANIFEST_ID") or None,
        help="Optional ManifestID forwarded to generate-live-payment.",
    )
    p.add_argument(
        "--body-json",
        default="{}",
        help="JSON string for http-reqresp / http-stream / http-multipart POST body (default: {}).",
    )
    p.add_argument(
        "--content-type",
        default="application/json",
        help="Content-Type for POST body (default: application/json).",
    )
    p.add_argument(
        "--request-id",
        default=None,
        help="Optional Livepeer-Request-Id header value.",
    )
    p.add_argument(
        "--output-file",
        default=None,
        help="Write non-session HTTP response bytes to this file instead of stdout.",
    )
    p.add_argument(
        "--debug",
        "-d",
        action="store_true",
        help="Enable DEBUG logs for livepeer_gateway.",
    )
    return p.parse_args()


def _resolve_signer(
    args: argparse.Namespace,
) -> tuple[str, Optional[dict[str, str]]]:
    token_data: Optional[dict[str, Any]] = None
    if args.token:
        token_data = parse_token(args.token)

    signer = (token_data.get("signer") if token_data else None) or args.signer
    signer_headers = (token_data.get("signer_headers") if token_data else None)
    billing_url = (token_data.get("billing") if token_data else None) or args.billing_url
    billing_access_token = (token_data.get("billing_access_token") if token_data else None) or (
        args.billing_access_token
    )

    if billing_access_token and not billing_url:
        raise LivepeerGatewayError(
            "billing_access_token requires billing_url (or token key \"billing\")"
        )

    billing_kwargs: dict[str, Any] = {"headless": not args.browser}
    if args.client_id is not None:
        billing_kwargs["client_id"] = args.client_id
    if billing_access_token is not None:
        billing_kwargs["billing_access_token"] = billing_access_token

    signer_url, resolved_headers, _ = _resolve_billing(
        billing_url,
        signer,
        signer_headers,
        None,
        **billing_kwargs,
    )
    if not signer_url or not str(signer_url).strip():
        raise LivepeerGatewayError(
            "No signer URL: pass --signer, or --billing-url (+ OIDC or --billing-access-token), "
            "or --token with signer/billing."
        )
    return str(signer_url).strip(), resolved_headers


def main() -> int:
    args = _parse_args()
    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(levelname)s %(name)s: %(message)s",
        )
        logging.getLogger("livepeer_gateway").setLevel(logging.DEBUG)

    try:
        signer_url, signer_headers = _resolve_signer(args)
    except LivepeerGatewayError as e:
        print(str(e), file=sys.stderr)
        return 2

    body_bytes: Optional[bytes] = None
    content_type: Optional[str] = None

    signed = fetch_coordinator_registry(args.registry_url)
    candidates = flatten_manifest_to_candidates(signed.manifest)
    picked = select_registry_candidates(
        candidates,
        capability_id=args.capability_id,
        offering_id=args.offering_id,
        interaction_mode=args.interaction_mode or None,
    )
    if not picked:
        print("No matching registry candidates.", file=sys.stderr)
        return 3

    candidate = picked[0]
    price = int(candidate.price_per_unit_wei, 10)
    face = args.face_value_wei
    wu = args.work_units

    mode = candidate.interaction_mode
    if mode.startswith("http-"):
        try:
            json.loads(args.body_json)
        except json.JSONDecodeError as e:
            print(f"Invalid --body-json: {e}", file=sys.stderr)
            return 4
        body_bytes = args.body_json.encode("utf-8")
        content_type = args.content_type

    resp = registry_dispatch_cap(
        signer_url=signer_url,
        candidate=candidate,
        signer_headers=signer_headers,
        face_value_wei=face if face > 0 else None,
        registry_price_per_unit_wei=None if face > 0 else price,
        work_units=None if face > 0 else wu,
        manifest_id=args.manifest_id,
        body=body_bytes,
        content_type=content_type,
        request_id=args.request_id,
    )
    if candidate.interaction_mode in (
        "session-control-external-media@v0",
        "rtmp-ingress-hls-egress@v0",
    ):
        doc = parse_session_open_json(resp)
        print(json.dumps(doc, indent=2))
    else:
        work_units = resp.headers.get("Livepeer-Work-Units")
        if args.output_file:
            out = Path(args.output_file)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(resp.content)
            print(
                f"{resp.status_code} {work_units} wrote {len(resp.content)} bytes to {out}",
                file=sys.stderr,
            )
        else:
            print(resp.status_code, work_units, file=sys.stderr)
            sys.stdout.buffer.write(resp.content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
