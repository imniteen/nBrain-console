"""Browser OAuth against a loopback redirect, for providers that will not register a client.

`fastmcp` already does this for servers that support dynamic client registration, but it
hardcodes an `http://localhost:PORT/callback` redirect. Slack refuses to accept an http
redirect URL at all — "A Redirect URL must also use HTTPS" — so connecting to Slack's MCP
server needs a loopback listener that speaks TLS. That is what this module adds.

The certificate is self-signed and generated once into the vault, which means the browser
shows a warning the first time and remembers the exception afterwards. Trusting it properly
is a one-line `mkcert` job; see `cert_advice()`.

Nothing here is Slack-specific: it is a plain authorisation-code flow with PKCE, so the same
code serves any provider that wants a pre-registered confidential client."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import ipaddress
import logging
import os
import secrets
import shutil
import ssl
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

CERT_FILE = "loopback-cert.pem"
KEY_FILE = "loopback-key.pem"
COLLECTION = "nbrain-connector"


class OAuthFlowError(RuntimeError):
    """The flow could not be completed; the message is safe to show the user."""


@dataclass
class Provider:
    """Everything needed to talk to one provider's authorisation server."""

    name: str
    authorize_url: str
    token_url: str
    client_id: str
    client_secret: str | None = None
    scopes: list[str] = field(default_factory=list)
    # Providers disagree about how scopes travel. Slack's ordinary /oauth/v2/authorize splits bot
    # scopes (`scope`) from user scopes (`user_scope`), but its user-only endpoint — the one the
    # MCP server uses — reads the user scopes straight out of `scope`, and answers "No scopes
    # requested" if they arrive in `user_scope` instead.
    scope_param: str = "scope"
    scope_separator: str = " "
    audience: str | None = None
    extra_authorize: dict[str, str] = field(default_factory=dict)


# ----- redirect uri -----
def redirect_uri(port: int, *, secure: bool = True) -> str:
    """The exact string the user registers with the provider. Must match byte for byte."""
    scheme = "https" if secure else "http"
    return f"{scheme}://localhost:{port}/oauth/callback"


def cert_advice() -> str:
    if shutil.which("mkcert"):
        return "mkcert is installed: run `mkcert -install` once and nbrain will use a trusted certificate."
    return (
        "Your browser will warn once about nbrain's self-signed certificate — accept it and it "
        "will not ask again. Installing mkcert removes the warning entirely."
    )


def ensure_cert(vault: Path) -> tuple[Path, Path]:
    """A long-lived self-signed certificate for localhost, generated on first use."""
    cert, key = vault / "nbrain" / CERT_FILE, vault / "nbrain" / KEY_FILE
    if cert.exists() and key.exists():
        return cert, key
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    )
    certificate = builder.sign(private, hashes.SHA256())
    cert.parent.mkdir(parents=True, exist_ok=True)
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    with contextlib.suppress(OSError):
        os.chmod(key, 0o600)
    log.info("generated a self-signed loopback certificate at %s", cert)
    return cert, key


# ----- callback listener -----
_DONE_PAGE = """<!doctype html><meta charset=utf-8><title>nbrain</title>
<style>body{{font:16px/1.5 -apple-system,Segoe UI,sans-serif;display:grid;place-items:center;
height:100vh;margin:0;color:#1d2939;background:#f9fafb}}div{{text-align:center;max-width:26rem}}
h1{{font-size:1.25rem;margin:0 0 .5rem}}p{{color:#667085;margin:0}}</style>
<div><h1>{heading}</h1><p>{detail}</p></div>"""


class _Catcher(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}
    done: threading.Event

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/oauth/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        type(self).result.update(params)
        ok = "code" in params and "error" not in params
        body = _DONE_PAGE.format(
            heading="Connected" if ok else "Not connected",
            detail="You can close this tab and go back to nbrain."
            if ok
            else params.get("error_description") or params.get("error", "The provider refused the request."),
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())
        type(self).done.set()

    def log_message(self, *_: Any) -> None:
        """Silence the stdlib access log; it would print the authorisation code."""


def _serve(port: int, vault: Path, *, secure: bool) -> tuple[http.server.HTTPServer, type[_Catcher], threading.Event]:
    done = threading.Event()
    handler = type("Catcher", (_Catcher,), {"result": {}, "done": done})
    try:
        server = http.server.HTTPServer(("127.0.0.1", port), handler)
    except OSError as err:
        raise OAuthFlowError(
            f"port {port} is busy, so the callback listener could not start — stop whatever is "
            f"using it, or change web.oauth_port ({err})"
        ) from err
    if secure:
        cert, key = ensure_cert(vault)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, handler, done


# ----- the flow -----
def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_url(provider: Provider, redirect: str, *, state: str, challenge: str) -> str:
    params = {
        "client_id": provider.client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **provider.extra_authorize,
    }
    if provider.scopes:
        params[provider.scope_param] = provider.scope_separator.join(provider.scopes)
    if provider.audience:
        params["audience"] = provider.audience
    return f"{provider.authorize_url}?{urllib.parse.urlencode(params)}"


def normalise_token(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten the shapes providers actually return into access/refresh/expiry.

    Slack answers `{"ok": true, "authed_user": {"access_token": ...}}` rather than the plain
    RFC 6749 body, and reports failure with HTTP 200 and `ok: false`."""
    if payload.get("ok") is False:
        raise OAuthFlowError(f"the provider refused the exchange: {payload.get('error', 'unknown error')}")
    source = payload
    user = payload.get("authed_user")
    if isinstance(user, dict) and user.get("access_token"):
        source = user
    token = source.get("access_token")
    if not token:
        raise OAuthFlowError("the provider's reply contained no access token")
    out: dict[str, Any] = {"access_token": token, "token_type": source.get("token_type", "Bearer")}
    if refresh := source.get("refresh_token"):
        out["refresh_token"] = refresh
    if (expires := source.get("expires_in")) is not None:
        with contextlib.suppress(TypeError, ValueError):
            out["expires_at"] = time.time() + float(expires)
    if scope := source.get("scope"):
        out["scope"] = scope
    return out


async def exchange(provider: Provider, *, code: str, redirect: str, verifier: str) -> dict[str, Any]:
    data = {
        "client_id": provider.client_id,
        "code": code,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    if provider.client_secret:
        data["client_secret"] = provider.client_secret
    async with httpx.AsyncClient(timeout=30) as http_client:
        reply = await http_client.post(provider.token_url, data=data)
    reply.raise_for_status()
    return normalise_token(reply.json())


async def refresh(provider: Provider, token: dict[str, Any]) -> dict[str, Any] | None:
    """Trade a refresh token for a fresh access token, or None when there is nothing to use."""
    if not token.get("refresh_token"):
        return None
    data = {
        "client_id": provider.client_id,
        "grant_type": "refresh_token",
        "refresh_token": token["refresh_token"],
    }
    if provider.client_secret:
        data["client_secret"] = provider.client_secret
    async with httpx.AsyncClient(timeout=30) as http_client:
        reply = await http_client.post(provider.token_url, data=data)
    reply.raise_for_status()
    fresh = normalise_token(reply.json())
    # Providers that do not rotate refresh tokens simply omit it; keep the one we have.
    fresh.setdefault("refresh_token", token["refresh_token"])
    return fresh


async def run_flow(
    provider: Provider,
    *,
    vault: Path,
    port: int,
    secure: bool = True,
    open_browser: bool = True,
    timeout: float = 300.0,
    on_url: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Open the consent screen and wait for the redirect. Returns the normalised token.

    `on_url` receives the consent URL before the browser is opened. A launch can silently do
    nothing — no default browser, a background process, a remote session — and without the URL
    in hand the user is left staring at a page that says it is waiting for them."""
    import anyio

    redirect = redirect_uri(port, secure=secure)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    server, handler, done = _serve(port, vault, secure=secure)
    try:
        url = authorize_url(provider, redirect, state=state, challenge=challenge)
        if on_url is not None:
            on_url(url)
        if open_browser:
            import webbrowser

            webbrowser.open(url)
        log.info("waiting for the %s consent screen", provider.name)
        # Poll rather than block on the whole timeout, so a cancel does not leave the user
        # waiting out five minutes after a rejected redirect URL.
        def wait() -> str:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if done.wait(0.25):
                    return "done"
                if cancel is not None and cancel.is_set():
                    return "cancelled"
            return "timeout"

        outcome = await anyio.to_thread.run_sync(wait)
        if outcome == "cancelled":
            raise OAuthFlowError("cancelled before the provider redirected back")
        if outcome == "timeout":
            raise OAuthFlowError(
                f"gave up after {int(timeout)}s waiting for {provider.name} to redirect back. "
                "If the provider showed an error, it never sent you back here."
            )
        result = dict(handler.result)
    finally:
        server.shutdown()
        server.server_close()

    if error := result.get("error"):
        raise OAuthFlowError(f"{provider.name} refused: {result.get('error_description') or error}")
    if not secrets.compare_digest(result.get("state", ""), state):
        # A mismatch means the redirect did not come from the request we started.
        raise OAuthFlowError("the redirect carried the wrong state value, so it was discarded")
    if not (code := result.get("code")):
        raise OAuthFlowError("the redirect carried no authorisation code")
    return await exchange(provider, code=code, redirect=redirect, verifier=verifier)
