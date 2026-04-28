import argparse
import asyncio
import json
from typing import Optional

from livepeer_gateway import JSONLReader
from livepeer_gateway.byoc import BYOCJobRequest, start_byoc_job
from livepeer_gateway.errors import LivepeerGatewayError


DEFAULT_CAPABILITY = "text-reversal"
_DEFAULT_CONTROL_TEXTS = ("hello-byoc", "livepeer", "draw")


def _json_dump(data: dict) -> str:
    return json.dumps(data, sort_keys=True)


def _render_text_result(msg: dict) -> str:
    original = msg.get("original")
    reversed_text = msg.get("reversed")
    text = msg.get("text")
    if isinstance(original, str) and isinstance(reversed_text, str):
        return f"{original!r} -> {reversed_text!r}"
    if isinstance(text, str) and isinstance(reversed_text, str):
        return f"{text!r} -> {reversed_text!r}"
    if isinstance(reversed_text, str):
        return reversed_text
    return _json_dump(msg)


def _is_text_result(msg: dict) -> bool:
    return isinstance(msg.get("reversed"), str)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Start a text-only BYOC stream and drive it entirely via control messages."
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
    p.add_argument(
        "--stream-start",
        default=None,
        metavar="PATH_OR_URL",
        help=(
            "HTTP path on the orchestrator transcoder (default /ai/stream/start) or a full URL. "
            "Livepeer gateway BYOC uses /process/stream/start."
        ),
    )
    p.add_argument(
        "--stream-payment",
        default=None,
        metavar="PATH_OR_URL",
        help=(
            "Payment path (default /ai/stream/payment) or full URL; must match the server "
            "that handles Livepeer-Payment for this job."
        ),
    )
    p.add_argument(
        "--text",
        action="append",
        dest="control_texts",
        metavar="STRING",
        help=(
            "Send a control JSON message {\"text\": STRING}. "
            "Repeat to send multiple commands."
        ),
    )
    p.add_argument(
        "--control-interval",
        type=float,
        default=1.0,
        help="Seconds between successive control messages (default: 1).",
    )
    p.add_argument(
        "--no-control",
        action="store_true",
        help="Do not send any control-channel messages.",
    )
    p.add_argument(
        "--output-grace-seconds",
        type=float,
        default=1.5,
        help="Extra time to wait for final event/data output after stop (default: 1.5).",
    )
    p.add_argument(
        "--results-timeout",
        type=float,
        default=30.0,
        help="Max seconds to wait for expected results after control messages finish (default: 30).",
    )
    return p.parse_args()


def _control_texts_for_run(args: argparse.Namespace) -> list[str]:
    if args.no_control:
        return []
    if args.control_texts:
        return list(args.control_texts)
    if args.capability == DEFAULT_CAPABILITY:
        return list(_DEFAULT_CONTROL_TEXTS)
    return []


async def main() -> None:
    args = _parse_args()

    job = None
    try:
        req_opts: dict[str, str] = {}
        if args.stream_start is not None:
            req_opts["stream_start_endpoint"] = args.stream_start
        if args.stream_payment is not None:
            req_opts["stream_payment_endpoint"] = args.stream_payment

        job = start_byoc_job(
            args.orchestrator,
            BYOCJobRequest(
                capability=args.capability,
                stream_id=args.stream_id,
                enable_video_ingress=False,
                enable_video_egress=False,
                enable_data_output=True,
                **req_opts,
            ),
            signer_url=args.signer,
            discovery_url=args.discovery,
        )

        print("=== BYOC text-only stream ===")
        print("job_id:", job.job_id)
        print("capability:", job.capability)
        print("control_url:", job.control_url)
        print("events_url:", job.events_url)
        print("data_url:", job.data_url)
        print()

        job.start_payment_sender()

        texts = _control_texts_for_run(args)
        expected_results = len(texts)
        results_received = 0
        results_complete = asyncio.Event()

        async def print_channel(name: str, stream, *, count_results: bool = False) -> None:
            nonlocal results_received
            async for msg in stream:
                print(f"{name}: {_render_text_result(msg)}")
                if count_results and _is_text_result(msg):
                    results_received += 1
                    if results_received >= expected_results:
                        results_complete.set()

        async def control_messages() -> None:
            if not texts:
                results_complete.set()
                return
            if job.control is None:
                print("WARN: control messages requested but job has no control_url; skipping.")
                results_complete.set()
                return
            interval = max(0.05, float(args.control_interval))
            await asyncio.sleep(0.15)
            for i, s in enumerate(texts):
                if i > 0:
                    await asyncio.sleep(interval)
                await job.control.write({"text": s})
                print(f"control[{i}]: {{\"text\": {s!r}}}")

        control_task = asyncio.create_task(control_messages())
        output_tasks: list[asyncio.Task] = []
        primary_results_task: Optional[asyncio.Task] = None

        if job.events is not None:
            event_task = asyncio.create_task(
                print_channel(
                    "event",
                    job.events(),
                    count_results=not bool(job.data_url),
                )
            )
            output_tasks.append(event_task)
            if primary_results_task is None and not job.data_url:
                primary_results_task = event_task

        if job.data_url:
            data_task = asyncio.create_task(
                print_channel(
                    "data",
                    JSONLReader(job.data_url)(),
                    count_results=True,
                )
            )
            output_tasks.append(data_task)
            primary_results_task = data_task

        stop_sent = False
        try:
            await control_task
            if expected_results > 0 and primary_results_task is not None:
                wait_task = asyncio.create_task(results_complete.wait())
                try:
                    done, _pending = await asyncio.wait(
                        {wait_task, primary_results_task},
                        timeout=max(0.05, float(args.results_timeout)),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not wait_task.done():
                        wait_task.cancel()
                        await asyncio.gather(wait_task, return_exceptions=True)
                if not results_complete.is_set():
                    if primary_results_task in done:
                        print(
                            f"WARN: results channel ended before {expected_results} "
                            f"result(s) received; stopping job."
                        )
                    else:
                        print(
                            f"ERROR: timed out after {args.results_timeout}s waiting for "
                            f"{expected_results} result(s); stopping job."
                        )
            stop_resp = await job.stop()
            stop_sent = True
            print(f"stop: {_json_dump(stop_resp.get('body') or {'status_code': stop_resp['status_code']})}")
        finally:
            if not control_task.done():
                await control_task
            if not stop_sent:
                stop_resp = await job.stop()
                print(f"stop: {_json_dump(stop_resp.get('body') or {'status_code': stop_resp['status_code']})}")
            await asyncio.sleep(max(0.0, args.output_grace_seconds))
            for task in output_tasks:
                task.cancel()
            if output_tasks:
                await asyncio.gather(*output_tasks, return_exceptions=True)
    except LivepeerGatewayError as e:
        print(f"ERROR: {e}")
    finally:
        if job is not None:
            await job.close()


if __name__ == "__main__":
    asyncio.run(main())
