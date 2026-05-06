import argparse
import asyncio
import logging
import traceback
from fractions import Fraction

import av

from livepeer_gateway.errors import LivepeerGatewayError, NoOrchestratorAvailableError
from livepeer_gateway.lv2v import StartJobRequest, start_lv2v
from livepeer_gateway.media_publish import MediaPublishConfig, VideoOutputConfig

DEFAULT_MODEL_ID = "noop"  # fix


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Start an LV2V job and publish raw frames via publish_url.")
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
        help="Billing gateway URL (e.g. http://localhost:3000). "
        "Auto-detects OIDC login or direct signer mode.",
    )
    p.add_argument(
        "--billing-access-token",
        default=None,
        metavar="JWT",
        help="Skip OIDC: Bearer token for the billing gateway signer API (use with --billing-url).",
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
        "--token",
        default=None,
        metavar="TOKEN",
        help="Base64-encoded JSON token (signer, signer_headers, discovery, orchestrators). "
        "Encode billing URL + JWT: python examples/encode_gateway_token.py --billing-url URL JWT",
    )
    p.add_argument(
        "--discovery",
        default=None,
        help="Discovery endpoint for orchestrators (overrides signer / billing discovery).",
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL_ID,
        help=f"Pipeline model to start via /live-video-to-video. Default: {DEFAULT_MODEL_ID}",
    )
    p.add_argument("--width", type=int, default=320, help="Frame width (default: 320).")
    p.add_argument("--height", type=int, default=180, help="Frame height (default: 180).")
    p.add_argument("--fps", type=float, default=30.0, help="Frames per second (default: 30).")
    p.add_argument("--count", type=int, default=90, help="Number of frames to send (default: 90).")
    p.add_argument(
        "--debug",
        "-d",
        action="store_true",
        help="Enable DEBUG logs for livepeer_gateway (discovery, orchestrator selection, HTTP).",
    )
    return p.parse_args()


def _solid_rgb_frame(width: int, height: int, rgb: tuple[int, int, int]) -> av.VideoFrame:
    frame = av.VideoFrame(width, height, "rgb24")
    r, g, b = rgb
    frame.planes[0].update(bytes([r, g, b]) * (width * height))
    return frame


async def main() -> None:
    args = _parse_args()
    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(levelname)s %(name)s: %(message)s",
        )
        logging.getLogger("livepeer_gateway").setLevel(logging.DEBUG)
    frame_interval = 1.0 / max(1e-6, args.fps)

    job = None
    try:
        job = start_lv2v(
            args.orchestrator,
            StartJobRequest(model_id=args.model),
            token=args.token,
            signer_url=args.signer,
            discovery_url=args.discovery,
            billing_url=args.billing_url,
            billing_access_token=args.billing_access_token,
            client_id=args.client_id,
            headless=not args.browser,
        )

        print("=== LiveVideoToVideo ===")
        print("publish_url:", job.publish_url)
        print()

        media = job.start_media(
            MediaPublishConfig(
                tracks=[VideoOutputConfig(fps=args.fps)],
            )
        )

        time_base = Fraction(1, int(round(args.fps)))
        for i in range(max(0, args.count)):
            color = (i * 5) % 255
            frame = _solid_rgb_frame(args.width, args.height, (color, 0, 255 - color))
            frame.pts = i
            frame.time_base = time_base
            await media.write_frame(frame)
            await asyncio.sleep(frame_interval)
    except NoOrchestratorAvailableError as e:
        print(f"ERROR: {e}")
        if e.rejections:
            print("Per-orchestrator failures:")
            for r in e.rejections:
                print(f"  - {r.url}")
                print(f"    {r.reason}")
    except LivepeerGatewayError as e:
        print(f"ERROR: {e}")
        if args.debug and e.__cause__:
            print(f"Cause: {e.__cause__!r}")
    except Exception:
        if args.debug:
            traceback.print_exc()
        raise
    finally:
        if job is not None:
            await job.close()


if __name__ == "__main__":
    asyncio.run(main())
