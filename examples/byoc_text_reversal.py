import argparse
import asyncio
import json
import logging
from typing import Optional

from livepeer_gateway.byoc import BYOCJobRequest, start_byoc_job
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
        "--output-grace-seconds",
        type=float,
        default=1.5,
        help="Extra time to wait for final event/data output after stop. Default: 1.5.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args()


def _parse_orchestrator_arg(orchestrator_arg: Optional[str]):
    if orchestrator_arg is None:
        return None
    parts = [part.strip() for part in orchestrator_arg.split(",") if part.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return parts


async def _amain() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    orch_url = _parse_orchestrator_arg(args.orchestrator)

    job = None
    try:
        job = start_byoc_job(
            orch_url,
            BYOCJobRequest(
                capability=args.capability,
                enable_video_ingress=False,
                enable_video_egress=False,
                # reverse_server publishes JSON to events_url only, not data_url.
                enable_data_output=False,
                timeout_seconds=args.timeout_seconds,
            ),
            token=args.token,
            signer_url=args.signer,
            discovery_url=args.discovery,
        )

        print("=== BYOC text-reversal ===")
        print(f"job_id:      {job.job_id}")
        print(f"capability:  {job.capability}")
        print(f"control_url: {job.control_url}")
        print(f"events_url:  {job.events_url}")
        print()

        job.start_payment_sender()

        if job.control is None:
            print("ERROR: job has no control_url; cannot send text.")
            return

        result_received = asyncio.Event()
        received_payload: dict = {}

        async def read_results() -> None:
            if job.events is None:
                print("WARN: no events channel on job; results will not be captured.")
                result_received.set()
                return
            async for msg in job.events():
                if isinstance(msg, dict) and isinstance(msg.get("reversed"), str):
                    received_payload.update(msg)
                    result_received.set()
                    return

        reader_task = asyncio.create_task(read_results())

        await asyncio.sleep(0.15)
        await job.control.write({"text": args.text})
        print(f"sent: {{\"text\": {args.text!r}}}")

        try:
            await asyncio.wait_for(result_received.wait(), timeout=args.timeout_seconds)
        except asyncio.TimeoutError:
            print("ERROR: timed out waiting for reversal result.")
            return

        original = received_payload.get("original") or received_payload.get("text") or args.text
        reversed_text = received_payload.get("reversed")
        print(f"result: {original!r} -> {reversed_text!r}")
        print("payload:", json.dumps(received_payload, indent=2, sort_keys=True))

        stop_resp = await job.stop()
        print(f"stop: status={stop_resp['status_code']}")

        await asyncio.sleep(max(0.0, args.output_grace_seconds))
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)
    except LivepeerGatewayError as err:
        print(f"ERROR: {err}")
    finally:
        if job is not None:
            await job.close()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
