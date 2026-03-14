import argparse
import asyncio
import json

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.lv2v import StartJobRequest, start_lv2v


DEFAULT_MODEL_ID = "noop"  # fix


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Start an LV2V job and subscribe to its events channel.")
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
        help=f"Pipeline model to start via /live-video-to-video. Default: {DEFAULT_MODEL_ID}",
    )
    p.add_argument("--count", type=int, default=0, help="Max events to print (0 = no limit).")
    return p.parse_args()


async def main() -> None:
    args = _parse_args()

    try:
        job = start_lv2v(
            args.orchestrator,
            StartJobRequest(model_id=args.model),
            signer_url=args.signer,
            billing_url=args.billing_url,
            client_id=args.client_id,
            headless=not args.browser,
        )

        print("=== LiveVideoToVideo ===")
        print("events_url:", job.events_url)
        print()

        if not job.events:
            raise LivepeerGatewayError("No events_url present on this LiveVideoToVideo job")

        seen = 0
        async for event in job.events():
            print(json.dumps(event, indent=2, sort_keys=True))
            print()
            seen += 1
            if args.count and seen >= args.count:
                break

    except LivepeerGatewayError as e:
        print(f"ERROR: {e}")
    finally:
        try:
            if "job" in locals():
                await job.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())

