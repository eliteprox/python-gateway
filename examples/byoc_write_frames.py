import argparse
import asyncio
from fractions import Fraction

import av

from livepeer_gateway.byoc import BYOCJobRequest, start_byoc_job
from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.media_publish import MediaPublishConfig, VideoOutputConfig


DEFAULT_CAPABILITY = "text-reversal"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Start a BYOC stream job and publish raw frames via publish_url."
    )
    p.add_argument(
        "orchestrator",
        nargs="?",
        default=None,
        help="Orchestrator (host:port). If omitted, discovery is used.",
    )
    p.add_argument(
        "--capability",
        default=DEFAULT_CAPABILITY,
        help=f"BYOC capability name (default: {DEFAULT_CAPABILITY}).",
    )
    p.add_argument(
        "--signer",
        required=True,
        help="Remote signer URL (no path). Required for BYOC job signing.",
    )
    p.add_argument(
        "--discovery",
        default=None,
        help="Discovery endpoint for orchestrators.",
    )
    p.add_argument(
        "--stream-id",
        default=None,
        help="Optional stream ID to include in BYOC request details.",
    )
    p.add_argument("--width", type=int, default=320, help="Frame width (default: 320).")
    p.add_argument("--height", type=int, default=180, help="Frame height (default: 180).")
    p.add_argument("--fps", type=float, default=30.0, help="Frames per second (default: 30).")
    p.add_argument("--count", type=int, default=90, help="Number of frames to send (default: 90).")
    return p.parse_args()


def _solid_rgb_frame(width: int, height: int, rgb: tuple[int, int, int]) -> av.VideoFrame:
    frame = av.VideoFrame(width, height, "rgb24")
    r, g, b = rgb
    frame.planes[0].update(bytes([r, g, b]) * (width * height))
    return frame


async def main() -> None:
    args = _parse_args()
    if args.fps <= 0:
        raise SystemExit("--fps must be > 0")
    frame_interval = 1.0 / args.fps

    job = None
    try:
        job = start_byoc_job(
            args.orchestrator,
            BYOCJobRequest(
                capability=args.capability,
                stream_id=args.stream_id,
            ),
            signer_url=args.signer,
            discovery_url=args.discovery,
        )

        print("=== BYOC stream ===")
        print("job_id:", job.job_id)
        print("capability:", job.capability)
        print("publish_url:", job.publish_url)
        print()

        media = job.start_media(
            MediaPublishConfig(tracks=[VideoOutputConfig(fps=args.fps)])
        )
        job.start_payment_sender()

        time_base = Fraction(1, max(1, int(round(args.fps))))
        for i in range(max(0, args.count)):
            color = (i * 5) % 255
            frame = _solid_rgb_frame(args.width, args.height, (color, 0, 255 - color))
            frame.pts = i
            frame.time_base = time_base
            await media.write_frame(frame)
            await asyncio.sleep(frame_interval)
    except LivepeerGatewayError as e:
        print(f"ERROR: {e}")
    finally:
        if job is not None:
            await job.close()


if __name__ == "__main__":
    asyncio.run(main())
