#!/usr/bin/env python3
"""
Log out and clear all cached OIDC tokens (JWTs).

Removes tokens stored in ~/.cache/livepeer-gateway/tokens/ (or XDG_CACHE_HOME).
The next run of any gateway command will prompt for login again.
"""

import argparse

from livepeer_gateway.oidc_auth import clear_all_cached_tokens, clear_cached_token
from livepeer_gateway.oidc_auth import DEFAULT_CLIENT_ID, DEFAULT_SCOPES


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Log out and clear cached OIDC tokens. "
        "Removes JWTs from ~/.cache/livepeer-gateway/tokens/."
    )
    p.add_argument(
        "--billing-url",
        default=None,
        help="Clear only tokens for this billing URL. If omitted, clears all cached tokens.",
    )
    p.add_argument(
        "--client-id",
        default=DEFAULT_CLIENT_ID,
        help=f"OIDC client ID (default: {DEFAULT_CLIENT_ID}). Only used with --billing-url.",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress output.",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    if args.billing_url:
        base = args.billing_url.rstrip("/")
        clear_cached_token(base, client_id=args.client_id, scopes=DEFAULT_SCOPES)
        if not args.quiet:
            print(f"Cleared cached token for {base}")
    else:
        count = clear_all_cached_tokens()
        if not args.quiet:
            print(f"Logged out. Cleared {count} cached token(s).")


if __name__ == "__main__":
    main()
