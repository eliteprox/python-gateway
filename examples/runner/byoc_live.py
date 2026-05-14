import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Optional

from livepeer_gateway import BYOCJobRequest, LivepeerGatewayError, SSEClient, start_byoc_job
from livepeer_gateway.byoc import _post_byoc_stop, _post_byoc_update


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
        raise SystemExit("pass only one of --params-json or --params-file")
    if file_path is not None:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    elif value is not None:
        data = json.loads(value)
    else:
        data = {}
    if not isinstance(data, dict):
        raise SystemExit("params must be a JSON object")
    return data


def _read_job(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit("job file must contain a JSON object")
    return data


def _write_job(path: Optional[str], data: dict[str, Any]) -> None:
    payload = json.dumps(data, indent=2, sort_keys=True)
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.write("\n")
    print(payload)


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--orchestrator", default=None, help="Optional orchestrator URL or comma-separated URLs.")
    parser.add_argument("--signer", default=None, help="Remote signer base URL.")
    parser.add_argument("--discovery", default=None, help="Optional discovery endpoint URL.")
    parser.add_argument(
        "--token",
        default=os.environ.get("LIVEPEER_TOKEN"),
        help="Gateway token containing signer/discovery/orchestrator info. Defaults to LIVEPEER_TOKEN.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=120, help="Signed BYOC request timeout.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start and control BYOC LivePipeline jobs.")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Start a BYOC live job.")
    _add_common_args(start)
    start.add_argument("--capability", required=True, help="Registered BYOC capability name.")
    start.add_argument("--stream-id", default=None, help="Optional stream ID.")
    start.add_argument("--params-json", default=None, help="Initial params JSON object.")
    start.add_argument("--params-file", default=None, help="Initial params JSON file.")
    start.add_argument("--no-video-ingress", action="store_true", help="Disable video ingress.")
    start.add_argument("--no-video-egress", action="store_true", help="Disable video egress.")
    start.add_argument("--enable-data-output", action="store_true", help="Enable data_url output.")
    start.add_argument("--job-file", default=None, help="Write job metadata to this file.")

    update = subparsers.add_parser("update", help="Update params for a live job.")
    update.add_argument("--job-file", required=True, help="Job metadata produced by start.")
    update.add_argument("--params-json", default=None, help="Params JSON object.")
    update.add_argument("--params-file", default=None, help="Params JSON file.")

    stop = subparsers.add_parser("stop", help="Stop a live job.")
    stop.add_argument("--job-file", required=True, help="Job metadata produced by start.")

    data = subparsers.add_parser("subscribe-data", help="Subscribe to a job data_url as SSE.")
    data.add_argument("--job-file", required=True, help="Job metadata produced by start.")
    data.add_argument("--timeout-seconds", type=float, default=None, help="Optional SSE timeout.")

    return parser.parse_args()


def _start(args: argparse.Namespace) -> None:
    params = _load_json_arg(args.params_json, args.params_file)
    job = start_byoc_job(
        _parse_orchestrator_arg(args.orchestrator),
        BYOCJobRequest(
            capability=args.capability,
            stream_id=args.stream_id,
            parameters=params,
            timeout_seconds=args.timeout_seconds,
            enable_video_ingress=not args.no_video_ingress,
            enable_video_egress=not args.no_video_egress,
            enable_data_output=args.enable_data_output,
        ),
        token=args.token,
        signer_url=args.signer,
        discovery_url=args.discovery,
    )
    data = {
        "job_id": job.job_id,
        "capability": job.capability,
        "publish_url": job.publish_url,
        "subscribe_url": job.subscribe_url,
        "control_url": job.control_url,
        "events_url": job.events_url,
        "data_url": job.data_url,
        "signed_job_header": job._signed_job_header,
        "stream_update_url": job._stream_update_url,
        "stream_stop_url": job._stream_stop_url,
        "timeout_seconds": args.timeout_seconds,
    }
    _write_job(args.job_file, data)


def _update(args: argparse.Namespace) -> None:
    job = _read_job(args.job_file)
    params = _load_json_arg(args.params_json, args.params_file)
    response = _post_byoc_update(
        job["stream_update_url"],
        payload=params,
        headers={"Livepeer": job["signed_job_header"]},
        timeout=float(job.get("timeout_seconds") or 120),
    )
    print(json.dumps(response["body"] if response["body"] is not None else response))


def _stop(args: argparse.Namespace) -> None:
    job = _read_job(args.job_file)
    response = _post_byoc_stop(
        job["stream_stop_url"],
        payload={"stream_id": job["job_id"]},
        headers={"Livepeer": job["signed_job_header"]},
        timeout=float(job.get("timeout_seconds") or 120),
    )
    print(json.dumps(response["body"] if response["body"] is not None else response))


async def _subscribe_data(args: argparse.Namespace) -> None:
    job = _read_job(args.job_file)
    data_url = job.get("data_url")
    if not data_url:
        raise SystemExit("job has no data_url")
    async for event in SSEClient.get(data_url, timeout=args.timeout_seconds):
        if event.event != "message":
            print(f"event: {event.event}")
        print(f"data: {event.data}\n", flush=True)


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.command == "start":
            _start(args)
        elif args.command == "update":
            _update(args)
        elif args.command == "stop":
            _stop(args)
        elif args.command == "subscribe-data":
            asyncio.run(_subscribe_data(args))
    except LivepeerGatewayError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
