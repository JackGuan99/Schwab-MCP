from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from conftest import FakeSchwab, write_token

from schwab_mcp import client as client_mod
from schwab_mcp.auth import TokenManager, TokenStore
from schwab_mcp.client import SchwabAPIError, SchwabClient, epoch_ms, iso_utc


@pytest.fixture
def no_sleep(monkeypatch):
    async def _sleep(_):
        return None

    monkeypatch.setattr(client_mod.asyncio, "sleep", _sleep)


def make(tmp_path, handler):
    path = tmp_path / "token.json"
    write_token(path)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SchwabClient(TokenManager(TokenStore(path), "k", "s", http), http), http


def test_date_formats():
    dt = datetime(2025, 9, 1, 13, 30, 5, tzinfo=timezone.utc)
    assert iso_utc(dt) == "2025-09-01T13:30:05.000Z"
    assert epoch_ms(dt) == 1756733405000


async def test_401_triggers_one_refresh_and_retry(tmp_path):
    fake = FakeSchwab()
    fake.fail_next = [httpx.Response(401, json={"message": "token expired"})]
    client, http = make(tmp_path, fake.handler)
    async with http:
        numbers = await client.get_account_numbers()
    assert len(numbers) == 2
    assert fake.paths() == [
        "/trader/v1/accounts/accountNumbers",
        "/v1/oauth/token",
        "/trader/v1/accounts/accountNumbers",
    ]
    assert fake.requests[-1].headers["Authorization"] == "Bearer new-access"


async def test_429_is_retried(tmp_path, no_sleep):
    fake = FakeSchwab()
    fake.fail_next = [httpx.Response(429, headers={"Retry-After": "1"}, json={"message": "slow down"})]
    client, http = make(tmp_path, fake.handler)
    async with http:
        quotes = await client.get_quotes(["AAPL"])
    assert "AAPL" in quotes and len(fake.requests) == 2


async def test_errors_surface_schwab_detail(tmp_path, no_sleep):
    def handler(_):
        return httpx.Response(
            400, json={"errors": [{"status": 400, "title": "Bad Request", "detail": "Invalid symbol format"}]}
        )

    client, http = make(tmp_path, handler)
    async with http:
        with pytest.raises(SchwabAPIError, match="Invalid symbol format") as err:
            await client.get_quotes(["???"])
    assert err.value.status_code == 400


async def test_persistent_5xx_gives_up(tmp_path, no_sleep):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, text="down")

    client, http = make(tmp_path, handler)
    async with http:
        with pytest.raises(SchwabAPIError, match="503"):
            await client.get_movers("$SPX")
    assert len(calls) == client_mod.MAX_RETRIES + 1


async def test_none_params_are_dropped(tmp_path):
    fake = FakeSchwab()
    client, http = make(tmp_path, fake.handler)
    async with http:
        await client.get_option_chain("AAPL", contract_type="CALL")
    params = fake.requests[0].url.params
    assert dict(params) == {"symbol": "AAPL", "contractType": "CALL", "includeUnderlyingQuote": "true"}
