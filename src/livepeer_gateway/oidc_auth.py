"""
OAuth 2.0 Authorization Code Flow with PKCE for native/desktop applications,
and Device Authorization Flow (RFC 8628) for CLI/IoT/headless environments.

Uses authlib's OAuth2Client for OAuth client behavior and HTTP transport.
All token requests include a ``resource`` parameter (RFC 8707) so the
provider issues audience-bound JWT access tokens.

Typical usage::

    from livepeer_gateway.oidc_auth import ensure_valid_token

    # Browser-based login (default)
    tokens = ensure_valid_token("https://pymthouse.example.com")

    # Headless / Device flow (CLI, IoT, smart TV)
    tokens = ensure_valid_token("https://pymthouse.example.com", headless=True)

    signer_headers = {"Authorization": f"Bearer {tokens['access_token']}"}
"""

from __future__ import annotations

import hashlib
import http.server
import json
import logging
import os
import socket
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from authlib.common.security import generate_token
from authlib.integrations.httpx_client import OAuth2Client
from authlib.oauth2.auth import OAuth2Token

_LOG = logging.getLogger(__name__)


def _ensure_https_for_display(url: str) -> str:
    """Upgrade http to https for non-localhost URLs so users open the secure page."""
    if not url or not url.startswith("http://"):
        return url
    try:
        parsed = urlparse(url)
        if parsed.hostname in (None, "localhost", "127.0.0.1") or parsed.hostname.endswith(".local"):
            return url
        return url.replace("http://", "https://", 1)
    except Exception:
        return url

DEFAULT_CLIENT_ID = "livepeer-sdk"
DEFAULT_SCOPES = "openid profile sign:job"
_CALLBACK_PATH = "/callback"
_AUTH_TIMEOUT_S = 300  # 5 minutes to complete browser login


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class OIDCConfig:
    """Parsed OIDC discovery document (only the fields we need)."""
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str
    jwks_uri: str
    device_authorization_endpoint: Optional[str] = None


def _oauth_verify() -> bool:
    return not bool(os.environ.get("LIVEPEER_ALLOW_INSECURE_TLS"))


def _build_oauth2_client(
    *,
    client_id: Optional[str] = None,
    scopes: Optional[str] = None,
    redirect_uri: Optional[str] = None,
    token: Optional[dict[str, Any]] = None,
    code_challenge_method: Optional[str] = None,
) -> OAuth2Client:
    return OAuth2Client(
        client_id=client_id,
        scope=scopes,
        redirect_uri=redirect_uri,
        token=token,
        token_endpoint_auth_method="none",
        code_challenge_method=code_challenge_method,
        timeout=15.0,
        verify=_oauth_verify(),
        headers={"Accept": "application/json"},
    )


# ---------------------------------------------------------------------------
# OIDC discovery
# ---------------------------------------------------------------------------

def discover(base_url: str) -> OIDCConfig:
    """Fetch and parse the OIDC discovery document."""
    url = base_url.rstrip("/") + "/.well-known/openid-configuration"
    with _build_oauth2_client() as client:
        resp = client.request("GET", url, withhold_token=True)
    if resp.status_code >= 400:
        raise _OIDCError(f"HTTP {resp.status_code} from {url}: {resp.text}")
    data = resp.json()
    return OIDCConfig(
        issuer=data["issuer"],
        authorization_endpoint=data["authorization_endpoint"],
        token_endpoint=data["token_endpoint"],
        userinfo_endpoint=data.get("userinfo_endpoint", ""),
        jwks_uri=data.get("jwks_uri", ""),
        device_authorization_endpoint=data.get("device_authorization_endpoint"),
    )


def probe_oidc(base_url: str) -> bool:
    """Return True if the base URL exposes an OIDC discovery endpoint."""
    url = base_url.rstrip("/") + "/.well-known/openid-configuration"
    try:
        with _build_oauth2_client() as client:
            resp = client.request("GET", url, withhold_token=True, timeout=5.0)
            return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Loopback callback server
# ---------------------------------------------------------------------------

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles the OAuth redirect callback on localhost."""

    code: Optional[str] = None
    error: Optional[str] = None
    state: Optional[str] = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != _CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return

        qs = parse_qs(parsed.query)
        self.__class__.state = qs.get("state", [None])[0]

        if "error" in qs:
            self.__class__.error = qs["error"][0]
            self._respond("Authorization denied. You can close this window.")
            return

        if "code" in qs:
            self.__class__.code = qs["code"][0]
            self._respond("Authorization successful! You can close this window.")
            return

        self.__class__.error = "missing_code"
        self._respond("Missing authorization code. You can close this window.")

    def _respond(self, body: str) -> None:
        html = (
            "<!DOCTYPE html><html><head><title>Livepeer SDK</title></head>"
            f"<body><p>{body}</p></body></html>"
        )
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        _LOG.debug("OIDC callback server: " + fmt, *args)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Core login flow (Authorization Code + PKCE)
# ---------------------------------------------------------------------------

def login(
    base_url: str,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    scopes: str = DEFAULT_SCOPES,
) -> OAuth2Token:
    """
    Run the full OAuth 2.0 Authorization Code + PKCE flow.

    1. Start an ephemeral HTTP server on 127.0.0.1 with a random port.
    2. Open the system browser to the authorize endpoint.
    3. Wait for the callback with the authorization code.
    4. Exchange the code for tokens at the token endpoint.
    """
    config = discover(base_url)
    code_verifier = generate_token(48)
    port = _find_free_port()
    redirect_uri = f"http://127.0.0.1:{port}{_CALLBACK_PATH}"

    _CallbackHandler.code = None
    _CallbackHandler.error = None
    _CallbackHandler.state = None

    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = _AUTH_TIMEOUT_S

    with _build_oauth2_client(
        client_id=client_id,
        scopes=scopes,
        redirect_uri=redirect_uri,
        code_challenge_method="S256",
    ) as client:
        authorize_url, state = client.create_authorization_url(
            config.authorization_endpoint,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
            resource=config.issuer,
        )

        _LOG.info("Opening browser for OIDC login...")
        print(f"\nOpening browser for login: {authorize_url}\n")
        webbrowser.open(authorize_url)

        result: dict[str, Any] = {}

        def _serve() -> None:
            try:
                server.handle_request()
                result["code"] = _CallbackHandler.code
                result["error"] = _CallbackHandler.error
                result["state"] = _CallbackHandler.state
            except Exception as exc:
                result["error"] = str(exc)
            finally:
                server.server_close()

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        thread.join(timeout=_AUTH_TIMEOUT_S)

        if thread.is_alive():
            server.server_close()
            raise _OIDCError("Login timed out — no callback received within 5 minutes")

        if result.get("error"):
            raise _OIDCError(f"Authorization failed: {result['error']}")

        code = result.get("code")
        if not code:
            raise _OIDCError("No authorization code received")

        received_state = result.get("state")
        authorization_response = f"{redirect_uri}?code={code}&state={received_state}"

        try:
            return client.fetch_token(
                config.token_endpoint,
                authorization_response=authorization_response,
                code_verifier=code_verifier,
                redirect_uri=redirect_uri,
                resource=config.issuer,
                state=state,
            )
        except Exception as exc:
            raise _OIDCError(f"Token exchange failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Device Authorization Flow (RFC 8628)
# ---------------------------------------------------------------------------

_DEVICE_POLL_TIMEOUT_S = 600  # 10 minutes max polling


def device_login(
    base_url: str,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    scopes: str = DEFAULT_SCOPES,
    on_device_auth: Optional[Callable[[str, str, int], None]] = None,
) -> OAuth2Token:
    """
    Run the Device Authorization Flow (RFC 8628).

    1. Request a device code from the provider.
    2. Display the user code and verification URL.
    3. Poll the token endpoint until the user authorizes or the code expires.

    Includes ``resource`` parameter (RFC 8707) so the resulting access token
    is a JWT with audience bound to the issuer.
    """
    config = discover(base_url)

    if not config.device_authorization_endpoint:
        raise _OIDCError(
            "Device Authorization Flow not supported by this provider. "
            "The discovery document has no device_authorization_endpoint."
        )

    # Step 1: Request device code (with resource indicator)
    with _build_oauth2_client(client_id=client_id, scopes=scopes) as client:
        resp = client.request(
            "POST",
            config.device_authorization_endpoint,
            withhold_token=True,
            data={
                "client_id": client_id,
                "scope": scopes,
                "resource": config.issuer,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if resp.status_code >= 400:
        raise _OIDCError(
            f"Device authorization request failed (HTTP {resp.status_code}): {resp.text}"
        )

    data = resp.json()
    device_code = data["device_code"]
    user_code = data["user_code"]
    verification_uri = _ensure_https_for_display(data.get("verification_uri", ""))
    verification_uri_complete = _ensure_https_for_display(data.get("verification_uri_complete", ""))
    expires_in = int(data.get("expires_in", 600))
    interval = int(data.get("interval", 5))

    # Step 2: Display instructions to user
    auth_url = verification_uri_complete or verification_uri
    if on_device_auth:
        try:
            on_device_auth(auth_url, user_code, expires_in)
        except Exception:
            _LOG.warning("on_device_auth callback failed", exc_info=True)
    print("\n" + "=" * 50)
    print("  DEVICE AUTHORIZATION")
    print("=" * 50)
    if verification_uri_complete:
        print(f"\n  Go to: {verification_uri_complete}")
        print(f"\n  Or visit: {verification_uri}")
        print(f"  and enter code: {user_code}")
    else:
        print(f"\n  Go to: {verification_uri}")
        print(f"  Enter code: {user_code}")
    print(f"\n  Code expires in {expires_in // 60} minutes.")
    print("=" * 50 + "\n")

    # Step 3: Poll for completion (with resource indicator)
    deadline = time.time() + min(expires_in, _DEVICE_POLL_TIMEOUT_S)
    poll_interval = interval

    with _build_oauth2_client(client_id=client_id, scopes=scopes) as client:
        while time.time() < deadline:
            time.sleep(poll_interval)

            resp = client.request(
                "POST",
                config.token_endpoint,
                withhold_token=True,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": client_id,
                    "device_code": device_code,
                    "resource": config.issuer,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if resp.status_code == 200:
                _LOG.info("Device authorized successfully")
                return OAuth2Token.from_dict(resp.json())

            try:
                err_data = resp.json()
            except Exception:
                raise _OIDCError(
                    f"Token poll failed (HTTP {resp.status_code}): {resp.text}"
                )

            error = err_data.get("error", "")

            if error == "authorization_pending":
                _LOG.debug("Authorization pending, polling again in %ds", poll_interval)
                continue

            if error == "slow_down":
                poll_interval += 5
                _LOG.debug("Slow down requested, interval now %ds", poll_interval)
                continue

            if error == "access_denied":
                raise _OIDCError("User denied the device authorization request")

            if error == "expired_token":
                raise _OIDCError("Device code expired before user authorized")

            raise _OIDCError(
                f"Device code token exchange failed: {err_data.get('error_description', error)}"
            )

    raise _OIDCError("Device authorization timed out — user did not authorize in time")


def refresh(
    base_url: str,
    refresh_token: str,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
) -> OAuth2Token:
    """Exchange a refresh token for a new token set."""
    config = discover(base_url)
    with _build_oauth2_client(
        client_id=client_id,
        token={"refresh_token": refresh_token},
    ) as client:
        try:
            return client.refresh_token(
                config.token_endpoint,
                refresh_token=refresh_token,
                resource=config.issuer,
            )
        except Exception as exc:
            raise _OIDCError(f"Refresh failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Token caching (XDG-style)
# ---------------------------------------------------------------------------

def _cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "livepeer-gateway" / "tokens"


def _cache_key(
    base_url: str,
    client_id: str = DEFAULT_CLIENT_ID,
    scopes: str = DEFAULT_SCOPES,
) -> str:
    key_material = f"{base_url}|{client_id}|{scopes}"
    return hashlib.sha256(key_material.encode()).hexdigest()[:16]


def load_cached_token(
    base_url: str,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    scopes: str = DEFAULT_SCOPES,
) -> Optional[OAuth2Token]:
    """Load a cached token set for the given base URL."""
    path = _cache_dir() / f"{_cache_key(base_url, client_id, scopes)}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
        return OAuth2Token.from_dict(data)
    except Exception:
        _LOG.debug("Failed to load cached token from %s", path, exc_info=True)
        return None


def save_cached_token(
    base_url: str,
    tokens: OAuth2Token,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    scopes: str = DEFAULT_SCOPES,
) -> None:
    """Persist a token set to the cache directory."""
    cache = _cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{_cache_key(base_url, client_id, scopes)}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(tokens)), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.rename(path)


def clear_cached_token(
    base_url: str,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    scopes: str = DEFAULT_SCOPES,
) -> None:
    """Remove the cached token for the given base URL."""
    path = _cache_dir() / f"{_cache_key(base_url, client_id, scopes)}.json"
    path.unlink(missing_ok=True)


def clear_all_cached_tokens() -> int:
    """
    Remove all cached OIDC tokens (logout). Returns the number of tokens cleared.
    """
    cache = _cache_dir()
    if not cache.exists():
        return 0
    count = 0
    for path in cache.glob("*.json"):
        path.unlink(missing_ok=True)
        count += 1
    return count


# ---------------------------------------------------------------------------
# High-level helper
# ---------------------------------------------------------------------------

def ensure_valid_token(
    base_url: str,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    scopes: str = DEFAULT_SCOPES,
    headless: bool = True,
    on_device_auth: Optional[Callable[[str, str, int], None]] = None,
) -> OAuth2Token:
    """
    Return a valid access token, using cache/refresh/login as needed.

    1. Load cached token set for this base URL.
    2. If valid and not expired -> return it.
    3. If expired but has a refresh token -> try to refresh.
    4. Otherwise -> run interactive login:
       - By default, uses Device Authorization Flow (RFC 8628).
       - If ``headless=False``, use browser-based Authorization Code + PKCE flow.

    All token requests include ``resource`` (RFC 8707) so access tokens are
    audience-bound JWTs.
    """
    if headless and os.environ.get("LIVEPEER_AUTH_BROWSER", "").lower() in ("1", "true", "yes"):
        headless = False

    cached = load_cached_token(base_url, client_id=client_id, scopes=scopes)

    if cached and not cached.is_expired():
        _LOG.debug("Using cached OIDC token for %s", base_url)
        return cached

    if cached and cached.get("refresh_token"):
        _LOG.info("Access token expired, refreshing...")
        try:
            tokens = refresh(base_url, cached["refresh_token"], client_id=client_id)
            save_cached_token(base_url, tokens, client_id=client_id, scopes=scopes)
            return tokens
        except Exception:
            _LOG.warning("Token refresh failed, falling back to login", exc_info=True)

    if headless:
        _LOG.info("Starting OIDC device authorization flow for %s", base_url)
        tokens = device_login(
            base_url,
            client_id=client_id,
            scopes=scopes,
            on_device_auth=on_device_auth,
        )
    else:
        _LOG.info("Starting OIDC browser login for %s", base_url)
        tokens = login(base_url, client_id=client_id, scopes=scopes)
    save_cached_token(base_url, tokens, client_id=client_id, scopes=scopes)
    return tokens


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------

class _OIDCError(Exception):
    """Internal OIDC authentication error."""
