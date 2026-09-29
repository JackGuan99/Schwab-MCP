"""Command line: ``schwab-mcp auth | serve | status``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import os
import sys
import webbrowser

import httpx

from . import __version__
from .auth import (
    AuthError,
    TokenManager,
    TokenStore,
    build_authorization_url,
    exchange_code,
    extract_code,
    new_state,
)
from .client import SchwabAPIError, SchwabClient
from .config import ConfigError, Settings, config_dir, load_env_files

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="schwab-mcp", description="Read-only Charles Schwab MCP server.")
    parser.add_argument("--version", action="version", version=f"schwab-mcp {__version__}")
    parser.add_argument(
        "--env-file",
        help=f"Load settings from this .env file (in addition to ./.env and {config_dir() / '.env'}).",
    )
    parser.add_argument("--log-level", default=os.environ.get("SCHWAB_MCP_LOG_LEVEL", "INFO"))
    sub = parser.add_subparsers(dest="command")

    p_auth = sub.add_parser(
        "auth", help="Log in to Schwab in a browser and save the token (repeat every 7 days)."
    )
    p_auth.add_argument("--no-browser", action="store_true", help="Only print the login URL.")

    sub.add_parser("status", help="Show login status and test the connection.")

    p_serve = sub.add_parser("serve", help="Run the MCP server (default command).")
    p_serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p_serve.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8765")))
    p_serve.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        metavar="HOST",
        help="Extra Host header to accept over HTTP, e.g. a tunnel domain. Repeatable.",
    )
    p_serve.add_argument(
        "--insecure-no-auth",
        action="store_true",
        help="Serve HTTP without SCHWAB_MCP_HTTP_TOKEN (loopback only).",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env_files(args.env_file)
    settings = Settings.from_env()

    try:
        if args.command == "auth":
            asyncio.run(run_auth(settings, open_browser=not args.no_browser))
        elif args.command == "status":
            asyncio.run(run_status(settings))
        else:
            serve(settings, args)
    except (ConfigError, AuthError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


# ----------------------------------------------------------------------- auth


async def run_auth(
    settings: Settings, *, open_browser: bool = True, transport: httpx.AsyncBaseTransport | None = None
) -> None:
    client_id, client_secret = settings.require_credentials()
    state = new_state()
    url = build_authorization_url(client_id, settings.callback_url, state)
    print(
        "\n1. Open this URL and log in to Schwab, then approve access for your accounts:\n\n"
        f"   {url}\n\n"
        f"2. Schwab redirects to {settings.callback_url}/?code=... . The page will fail to load - that is\n"
        "   expected. Copy the FULL address from the browser bar.\n",
        file=sys.stderr,
    )
    if open_browser:
        with contextlib.suppress(webbrowser.Error):
            webbrowser.open(url)
    redirected = input("3. Paste the redirected URL here: ").strip()
    code = extract_code(redirected, settings.callback_url, expected_state=state)

    async with httpx.AsyncClient(timeout=settings.request_timeout, transport=transport) as http:
        token = await exchange_code(http, client_id, client_secret, code, settings.callback_url)
        store = TokenStore(settings.token_path)
        store.save(token)
        print(f"\nSaved token to {settings.token_path}", file=sys.stderr)
        tokens = TokenManager(store, client_id, client_secret, http)
        try:
            numbers = await SchwabClient(tokens, http).get_account_numbers()
            print(
                f"Connected: {len(numbers)} account(s) visible. Login is valid for 7 days.", file=sys.stderr
            )
        except SchwabAPIError as exc:
            print(f"Token saved, but the test call failed: {exc}", file=sys.stderr)


async def run_status(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
    client_id, client_secret = settings.require_credentials()
    async with httpx.AsyncClient(timeout=settings.request_timeout, transport=transport) as http:
        tokens = TokenManager(TokenStore(settings.token_path), client_id, client_secret, http)
        status = tokens.status()
        if status.get("authenticated"):
            try:
                numbers = await SchwabClient(tokens, http).get_account_numbers()
                status["api_check"] = f"ok - {len(numbers)} account(s)"
            except (SchwabAPIError, AuthError) as exc:
                status["api_check"] = f"failed - {exc}"
        print(json.dumps(status, indent=2))


# ---------------------------------------------------------------------- serve


def serve(settings: Settings, args: argparse.Namespace) -> None:
    from .server import create_server

    settings.require_credentials()
    if not settings.token_path.exists():
        logging.getLogger(__name__).warning(
            "No Schwab token at %s yet - tools will fail until you run `schwab-mcp auth`.",
            settings.token_path,
        )
    server = create_server(settings)
    transport = getattr(args, "transport", "stdio")
    if transport == "stdio":
        server.run("stdio")
        return
    run_http(server, settings, args)


class BearerAuthMiddleware:
    """Minimal ASGI middleware: every HTTP request needs ``Authorization: Bearer <token>``."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            supplied = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def run_http(server, settings: Settings, args: argparse.Namespace) -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    if not settings.http_token and not args.insecure_no_auth:
        raise ConfigError(
            "HTTP transport needs SCHWAB_MCP_HTTP_TOKEN (a long random secret clients send as a "
            'Bearer token). Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    if not settings.http_token and args.host not in LOOPBACK:
        raise ConfigError("--insecure-no-auth is only allowed when binding to 127.0.0.1/localhost.")

    security = None
    if args.allowed_host:
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", *args.allowed_host]
        origins = [
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
            *(f"https://{h}" for h in args.allowed_host),
        ]
        security = TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)

    app = server.streamable_http_app(host=args.host, transport_security=security)
    if settings.http_token:
        app = BearerAuthMiddleware(app, settings.http_token)
    logging.getLogger(__name__).info("Serving MCP over HTTP at http://%s:%s/mcp", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
