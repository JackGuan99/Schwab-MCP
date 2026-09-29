from __future__ import annotations

import base64
import os
import stat
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import FakeSchwab, write_token

from schwab_mcp.auth import (
    AuthError,
    Token,
    TokenManager,
    TokenStore,
    build_authorization_url,
    exchange_code,
    extract_code,
)

CALLBACK = "https://127.0.0.1:8182"


def test_authorization_url():
    url = build_authorization_url("app-key", CALLBACK, "st")
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://api.schwabapi.com/v1/oauth/authorize?")
    assert q == {
        "response_type": ["code"],
        "client_id": ["app-key"],
        "redirect_uri": [CALLBACK],
        "state": ["st"],
    }


def test_extract_code_decodes_and_checks_state():
    url = f"{CALLBACK}/?code=C0.abc%40&session=xyz&state=st"
    assert extract_code(url, CALLBACK, expected_state="st") == "C0.abc@"
    with pytest.raises(AuthError, match="state mismatch"):
        extract_code(url, CALLBACK, expected_state="other")
    with pytest.raises(AuthError, match="does not start with"):
        extract_code("https://example.com/?code=x", CALLBACK)
    with pytest.raises(AuthError, match="access_denied"):
        extract_code(f"{CALLBACK}/?error=access_denied", CALLBACK)
    with pytest.raises(AuthError, match="No `code`"):
        extract_code(f"{CALLBACK}/?foo=1", CALLBACK)


async def test_exchange_code_uses_basic_auth_and_form_body():
    fake = FakeSchwab()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        token = await exchange_code(http, "app-key", "app-secret", "C0.abc@", CALLBACK, now=1000.0)
    req = fake.requests[0]
    assert req.method == "POST" and req.url.path == "/v1/oauth/token"
    assert req.headers["Authorization"] == "Basic " + base64.b64encode(b"app-key:app-secret").decode()
    body = parse_qs(req.content.decode())
    assert body == {"grant_type": ["authorization_code"], "code": ["C0.abc@"], "redirect_uri": [CALLBACK]}
    assert token.expires_at == 2800.0 and token.refresh_token_issued_at == 1000.0


async def test_token_endpoint_error_is_readable():
    def handler(_):
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "code expired"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(AuthError, match="code expired"):
            await exchange_code(http, "k", "s", "c", CALLBACK)


def test_token_store_is_private_and_roundtrips(tmp_path):
    path = tmp_path / "sub" / "token.json"
    token = Token("a", "r", expires_at=1.0, refresh_token_issued_at=2.0)
    TokenStore(path).save(token)
    assert TokenStore(path).load() == token
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_corrupt_token_file(tmp_path):
    path = tmp_path / "token.json"
    path.write_text("{not json")
    with pytest.raises(AuthError, match="unreadable"):
        TokenStore(path).load()


async def test_refresh_when_expired_keeps_login_time(tmp_path):
    path = tmp_path / "token.json"
    original = write_token(path, expires_in=-1)
    fake = FakeSchwab()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        mgr = TokenManager(TokenStore(path), "k", "s", http)
        assert await mgr.get_access_token() == "new-access"
        assert await mgr.get_access_token() == "new-access"  # cached, no second refresh
    assert len(fake.requests) == 1
    body = parse_qs(fake.requests[0].content.decode())
    assert body == {"grant_type": ["refresh_token"], "refresh_token": ["refresh-0"]}
    saved = TokenStore(path).load()
    assert saved.access_token == "new-access"
    assert saved.refresh_token_issued_at == original.refresh_token_issued_at


async def test_concurrent_callers_share_one_refresh(tmp_path):
    import asyncio

    path = tmp_path / "token.json"
    write_token(path, expires_in=-1)
    fake = FakeSchwab()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        mgr = TokenManager(TokenStore(path), "k", "s", http)
        results = await asyncio.gather(*(mgr.get_access_token() for _ in range(5)))
    assert set(results) == {"new-access"} and len(fake.requests) == 1


async def test_expired_refresh_token_asks_for_reauth(tmp_path):
    path = tmp_path / "token.json"
    write_token(path, expires_in=-1, issued_ago=7 * 24 * 3600 + 5)
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeSchwab().handler)) as http:
        mgr = TokenManager(TokenStore(path), "k", "s", http)
        with pytest.raises(AuthError, match="schwab-mcp auth"):
            await mgr.get_access_token()


async def test_revoked_refresh_token_asks_for_reauth(tmp_path):
    path = tmp_path / "token.json"
    write_token(path, expires_in=-1)

    def handler(_):
        return httpx.Response(400, json={"error": "invalid_client", "error_description": "Unauthorized"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        mgr = TokenManager(TokenStore(path), "k", "s", http)
        with pytest.raises(AuthError, match="schwab-mcp auth"):
            await mgr.get_access_token()


async def test_picks_up_new_token_file_without_restart(tmp_path):
    path = tmp_path / "token.json"
    write_token(path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeSchwab().handler)) as http:
        mgr = TokenManager(TokenStore(path), "k", "s", http)
        assert await mgr.get_access_token() == "access-0"
        newer = Token(
            "access-from-reauth", "r2", expires_at=time.time() + 1800, refresh_token_issued_at=time.time()
        )
        TokenStore(path).save(newer)
        os.utime(path, (time.time() + 5, time.time() + 5))  # make sure mtime differs on coarse filesystems
        assert await mgr.get_access_token() == "access-from-reauth"


def test_status_warns_before_expiry(tmp_path):
    path = tmp_path / "token.json"
    write_token(path, issued_ago=6.5 * 24 * 3600)
    mgr = TokenManager(TokenStore(path), "k", "s", http=None)  # type: ignore[arg-type]
    status = mgr.status()
    assert status["authenticated"] is True
    assert status["login_expires_in_hours"] == pytest.approx(12, abs=0.2)
    assert "less than a day" in status["warning"]


def test_status_without_token(tmp_path):
    mgr = TokenManager(TokenStore(tmp_path / "missing.json"), "k", "s", http=None)  # type: ignore[arg-type]
    assert mgr.status()["authenticated"] is False
