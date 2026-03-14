# Example: per-segment payments in a mixed sync/async scenario
#
# start_lv2v() is a sync function. When called from within a running
# event loop (eg inside an `async def`), the payment sender background
# task starts automatically. When called from plain sync code, a
# warning is logged and you must call job.start_payment_sender() once
# you enter an async context.
#
# GIANT DISCLAIMER: the mixed sync / async regime in this API is not
# super optimal, but that's how it works for now. This demonstrates the
# control flow that may need to be used.
#
# This example demonstrates a sync-start / async-run pattern if
# orchestrator selection and job creation are synchronous but the main
# workload is async (publishing frames, subscribing to output, etc).

import argparse
import asyncio
import json
import logging

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.lv2v import StartJobRequest, start_lv2v

DEFAULT_MODEL_ID = "noop"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Start an LV2V job with per-segment payments."
    )
    p.add_argument(
        "orchestrator",
        nargs="?",
        default=None,
        help="Orchestrator (host:port). If omitted, discovery is used.",
    )
    p.add_argument(
        "--signer",
        default=None,
        help="Remote signer URL (no path). If omitted, runs in offchain mode.",
    )
    p.add_argument(
        "--billing-url",
        default=None,
        help="Billing gateway URL (e.g. https://pymthouse.com). "
             "Auto-detects OIDC login or direct signer mode.",
    )
    p.add_argument(
        "--client-id",
        default=None,
        help="OIDC client ID for billing gateway (default: livepeer-sdk).",
    )
    p.add_argument(
        "--browser",
        action="store_true",
        default=False,
        help="Use browser-based PKCE login instead of Device Authorization Flow.",
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL_ID,
        help=f"Pipeline model. Default: {DEFAULT_MODEL_ID}",
    )
    p.add_argument(
        "--discovery",
        default=None,
        help="Discovery endpoint for orchestrators.",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=30.0,
        help="How long to keep the job alive, in seconds. Default: 30.",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(levelname)s %(name)s: %(message)s",
        )
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s %(name)s: %(message)s",
        )

    # ---- Sync part: create the job ----
    # start_lv2v is synchronous. Because there is no running event loop
    # here, the automatic start_payment_sender() call inside start_lv2v
    # will log a warning and skip. That is expected.
    try:
        job = start_lv2v(
            args.orchestrator,
            StartJobRequest(model_id=args.model),
            signer_url=args.signer,
            discovery_url=args.discovery,
            billing_url=args.billing_url,
            client_id=args.client_id,
            headless=not args.browser,
        )
    except LivepeerGatewayError as e:
        print(f"ERROR: {e}")
        return

    print("=== Job started ===")
    print(json.dumps(job.raw, indent=2, sort_keys=True))
    print()

    # ---- Async part: run the payment sender ----
    async def run() -> None:
        # Now we have a running event loop. Start the payment sender.
        # This is idempotent; if start_lv2v already started it (eg
        # when called from an async context), this is a no-op.
        job.start_payment_sender()
        print("Payment sender started (background task)")
        print(f"Running for {args.duration}s...")
        print()

        try:
            await asyncio.sleep(args.duration)
        except KeyboardInterrupt:
            print("Interrupted by user")
        finally:
            print("Closing job (cancels payment sender)...")
            await job.close()
            print("Done.")

    asyncio.run(run())


if __name__ == "__main__":
    main()
