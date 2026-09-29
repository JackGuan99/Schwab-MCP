from __future__ import annotations

import argparse
import json

import httpx
import pytest
from conftest import FakeSchwab

from schwab_mcp import cli
from schwab_mcp.auth import TokenStore
from schwab_mcp.config import ConfigError, Settings


async def test_auth_flow_saves_token(tmp_path, monkeypatch, capsys):
    settings = Settings(client_id="app-key", client_secret="app-secret", token_path=tmp_path / "t.json")
    monkeypatch.setattr(cli, "new_state", lambda: "fixed-state")
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: "https://127.0.0.1:8182/?code=C0.x%40&state=fixed-state"
    )
    fake = FakeSchwab()

    await cli.run_auth(settings, open_browser=False, transport=httpx.MockTransport(fake.handler))

    assert TokenStore(settings.token_path).load().access_token == "new-access"
    assert fake.paths() == ["/v1/oauth/token", "/trader/v1/accounts/accountNumbers"]
    err = capsys.readouterr().err
    assert "client_id=app-key" in err and "2 account(s) visible" in err


async def test_status_reports_api_check(settings, capsys):
    await cli.run_status(settings, transport=httpx.MockTransport(FakeSchwab().handler))
    status = json.loads(capsys.readouterr().out)
    assert status["authenticated"] is True and status["api_check"] == "ok - 2 account(s)"


def test_http_requires_token_unless_loopback_opt_out(settings):
    args = argparse.Namespace(insecure_no_auth=False, host="127.0.0.1", port=0, allowed_host=[])
    with pytest.raises(ConfigError, match="SCHWAB_MCP_HTTP_TOKEN"):
        cli.run_http(object(), settings, args)
    args = argparse.Namespace(insecure_no_auth=True, host="0.0.0.0", port=0, allowed_host=[])
    with pytest.raises(ConfigError, match="loopback|127.0.0.1"):
        cli.run_http(object(), settings, args)


async def test_bearer_middleware():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = cli.BearerAuthMiddleware(app, "s3cret")
    transport = httpx.ASGITransport(app=wrapped)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/mcp")).status_code == 401
        assert (await client.post("/mcp", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await client.post("/mcp", headers={"Authorization": "Bearer s3cret"})).status_code == 200
    assert seen == ["/mcp"]
