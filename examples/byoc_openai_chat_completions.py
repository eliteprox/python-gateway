"""
Send BYOC jobs to an OpenAI Chat Completions-style capability.

This follows the same patterns as :mod:`byoc_text_reversal` and
:mod:`byoc_write_text`: :func:`livepeer_gateway.byoc.byoc_request` with V1
remote signing (``bb7507ea`` / :func:`byoc_request` in ``livepeer_gateway.byoc``).

Default capability is ``openai-chat-completions``. The request body is an
OpenAI-compatible object (``model``, ``messages``, etc.). The orchestrator
forwards the JSON to your runner (for example, ``/v1/chat/completions`` on
the worker).

- Non-streaming (default): buffer the response and print a short assistant
  reply when possible, or full JSON.
- ``--stream``: set ``"stream": true`` in the JSON body and use
  ``byoc_request(..., stream=True)``; raw chunks are written to stdout
  (e.g. SSE or newline-delimited JSON, depending on the worker).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from livepeer_gateway import ByocResponse, ByocStreamResponse, byoc_request
from livepeer_gateway.errors import LivepeerGatewayError

DEFAULT_CAPABILITY = "openai-chat-completions"
DEFAULT_MODEL = "gpt-4o-mini"
_DEFAULT_USER = "Reply with one short friendly sentence, no other text."
DEFAULT_IN_PIXELS = 300
DEFAULT_HEADER_TIMEOUT = 120
DEFAULT_HTTP_TIMEOUT = 120.0


def _json_dump(data: Any) -> str:
    if isinstance(data, (dict, list)):
        return json.dumps(data, indent=2, sort_keys=True)
    return str(data)


def _print_openai_result(body: dict[str, Any]) -> None:
    """Pretty-print a chat completion JSON body when the shape is familiar."""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        print(_json_dump(body))
        return
    c0 = choices[0]
    if not isinstance(c0, dict):
        print(_json_dump(body))
        return
    msg = c0.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str) and content:
            print(content)
            return
    print(_json_dump(body))


def _parse_orchestrator_arg(orchestrator_arg: str | None) -> str | list[str] | None:
    if orchestrator_arg is None:
        return None
    parts = [p.strip() for p in orchestrator_arg.split(",") if p.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return parts


def _parse_options_filter(pairs: list[str] | None) -> dict[str, str] | None:
    if not pairs:
        return None
    out: dict[str, str] = {}
    for part in pairs:
        if "=" not in part:
            raise ValueError(f"options filter must be key=value, got: {part!r}")
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip()
        if not k:
            raise ValueError(f"invalid options filter: {part!r}")
        out[k] = v
    return out or None


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BYOC openai-chat-completions: batch or streaming /process/request."
    )
    p.add_argument(
        "orchestrator",
        nargs="?",
        default=None,
        help="Orchestrator base URL or comma-separated URLs; omit to use discovery.",
    )
    p.add_argument(
        "--capability",
        default=DEFAULT_CAPABILITY,
        help=f"BYOC capability name. Default: {DEFAULT_CAPABILITY!r}.",
    )
    p.add_argument(
        "--signer",
        required=True,
        help="Remote signer base URL (no path). Required for BYOC signing.",
    )
    p.add_argument(
        "--discovery",
        default=None,
        help="Optional discovery URL.",
    )
    p.add_argument(
        "--token",
        default=None,
        help="Optional encoded gateway token (orchestrators, signer, discovery).",
    )
    p.add_argument(
        "--json-body",
        metavar="PATH",
        help="Path to a JSON file: full request body (model, messages, ...).",
    )
    p.add_argument(
        "--json",
        dest="json_inline",
        default=None,
        help="Full request body as a JSON string (overrides --model / --user).",
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Chat model id for the request body. Default: {DEFAULT_MODEL!r}.",
    )
    p.add_argument(
        "--system",
        default=None,
        help="Optional system message (single string).",
    )
    p.add_argument(
        "--user",
        "-U",
        action="append",
        dest="user_messages",
        metavar="TEXT",
        help="User message content. Repeat to send several consecutive jobs.",
    )
    p.add_argument(
        "--filter",
        action="append",
        dest="options_filters",
        metavar="KEY=VAL",
        help="Runner `options_filter` (repeat). Passed in the Livepeer header job parameters.",
    )
    p.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Optional temperature for the request body (when not using --json / --json-body).",
    )
    p.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Optional max_tokens for the request body.",
    )
    p.add_argument(
        "--in-pixels",
        type=int,
        default=DEFAULT_IN_PIXELS,
        help="BYOC pre-fund budget (typical: seconds of compute). Default: %d."
        % DEFAULT_IN_PIXELS,
    )
    p.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_HEADER_TIMEOUT,
        help="Per-job timeout in the Livepeer header. Default: %d." % DEFAULT_HEADER_TIMEOUT,
    )
    p.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_HTTP_TIMEOUT,
        help="HTTP read timeout. Default: %.0f."
        % DEFAULT_HTTP_TIMEOUT,
    )
    p.add_argument(
        "--stream",
        action="store_true",
        help="Request server streaming (adds stream:true; uses byoc_request(stream=True)).",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Debug logging.",
    )
    return p.parse_args()


def _build_request_body(
    args: argparse.Namespace,
    user_content: str,
) -> dict[str, Any]:
    messages: list[dict[str, str]] = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": user_content})
    body: dict[str, Any] = {
        "model": args.model,
        "messages": messages,
    }
    if args.temperature is not None:
        body["temperature"] = args.temperature
    if args.max_tokens is not None:
        body["max_tokens"] = args.max_tokens
    if args.stream:
        body["stream"] = True
    return body


def _load_body_from_args(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Build one or more request bodies: full JSON vs simple model/user list."""
    if args.json_body:
        with open(args.json_body, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise SystemExit(f"--json-body must be a JSON object, got {type(data).__name__}")
        if list(args.user_messages or ()):
            print(
                "Warning: --user is ignored when --json-body is set",
                file=sys.stderr,
            )
        return [data]
    if args.json_inline is not None:
        data = json.loads(args.json_inline)
        if not isinstance(data, dict):
            raise SystemExit(f"--json must be a JSON object, got {type(data).__name__}")
        if list(args.user_messages or ()):
            print(
                "Warning: --user is ignored when --json is set",
                file=sys.stderr,
            )
        if args.stream and "stream" not in data:
            data = {**data, "stream": True}
        return [data]
    users = list(args.user_messages) if args.user_messages else [_DEFAULT_USER]
    return [_build_request_body(args, u) for u in users]


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        options_filter = _parse_options_filter(args.options_filters)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    try:
        bodies = _load_body_from_args(args)
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    if args.stream and not args.json_body and args.json_inline is None:
        for b in bodies:
            b.setdefault("stream", True)

    orch = _parse_orchestrator_arg(args.orchestrator)
    exit_code = 0
    for i, body in enumerate(bodies):
        if i:
            print()
        print("--- request %d/%d ---" % (i + 1, len(bodies)))
        try:
            r = byoc_request(
                capability_name=args.capability,
                body=body,
                in_pixels=args.in_pixels,
                orch_url=orch,
                token=args.token,
                signer_url=args.signer,
                discovery_url=args.discovery,
                options_filter=options_filter,
                content_type="application/json",
                timeout_seconds=args.timeout_seconds,
                request_timeout=args.request_timeout,
                stream=bool(args.stream),
            )
        except LivepeerGatewayError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            exit_code = 1
            continue
        if isinstance(r, ByocStreamResponse):
            try:
                print("Orchestrator: %s" % r.orch_url)
                print("HTTP status:  %d" % r.status_code)
                for chunk in r.chunks():
                    sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            finally:
                r.close()
            if r.status_code >= 400:
                exit_code = 1
            continue
        if not isinstance(r, ByocResponse):
            print(f"ERROR: unexpected {type(r).__name__}", file=sys.stderr)
            exit_code = 1
            continue
        print("Orchestrator: %s" % r.orch_url)
        print("HTTP status:  %d" % r.status_code)
        try:
            data = r.json()
        except Exception:
            print(r.text())
        else:
            if isinstance(data, dict):
                _print_openai_result(data)
            else:
                print(_json_dump(data))
        if r.status_code >= 400:
            exit_code = 1

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
