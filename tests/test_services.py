from unittest.mock import AsyncMock

import pytest

import services


@pytest.fixture(autouse=True)
def clear_service_cache():
    services.cache_service.clear()
    yield
    services.cache_service.clear()


@pytest.mark.asyncio
async def test_ethereum_balance_uses_json_rpc_without_an_api_key(monkeypatch):
    request_json = AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": "0x14d1120d7b160000"})
    monkeypatch.setattr(services, "_request_json", request_json)
    monkeypatch.setattr(services, "ETHEREUM_RPC_URL", "https://ethereum.example.test")

    balance = await services.get_eth_balance("0x" + "1" * 40, force_refresh=True)

    assert balance == 1.5
    _, method, url = request_json.await_args.args[:3]
    payload = request_json.await_args.kwargs["json"]
    assert method == "POST"
    assert url == "https://ethereum.example.test"
    assert payload == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getBalance",
        "params": ["0x" + "1" * 40, "latest"],
    }


@pytest.mark.asyncio
async def test_ethereum_api_error_is_not_reported_as_zero(monkeypatch):
    request_json = AsyncMock(
        return_value={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005}}
    )
    monkeypatch.setattr(services, "_request_json", request_json)

    with pytest.raises(services.ServiceError, match="Ethereum balance"):
        await services.get_eth_balance("0x" + "1" * 40, force_refresh=True)


@pytest.mark.asyncio
async def test_native_price_prefers_coinbase_spot(monkeypatch):
    request_json = AsyncMock(
        return_value={"data": {"amount": "1855.845", "base": "ETH", "currency": "USD"}}
    )
    monkeypatch.setattr(services, "_request_json", request_json)

    assert await services.get_eth_price(session=object(), force_refresh=True) == 1855.845
    assert request_json.await_args.args[2] == services.COINBASE_ETH_PRICE_API


@pytest.mark.asyncio
async def test_native_price_falls_back_when_primary_is_rate_limited(monkeypatch):
    request_json = AsyncMock(
        side_effect=[
            services.ServiceError("SOLANA price", "is rate-limited or unavailable"),
            {"solana": {"usd": 73.86}},
        ]
    )
    monkeypatch.setattr(services, "_request_json", request_json)

    assert await services.get_sol_price(session=object(), force_refresh=True) == 73.86
    assert [call.args[2] for call in request_json.await_args_list] == [
        services.COINBASE_SOL_PRICE_API,
        services.SOL_PRICE_API,
    ]


@pytest.mark.asyncio
async def test_solana_rpc_error_is_not_reported_as_zero(monkeypatch):
    request_json = AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005}})
    monkeypatch.setattr(services, "_request_json", request_json)

    with pytest.raises(services.ServiceError, match="Solana balance"):
        await services.get_sol_balance("11111111111111111111111111111111", force_refresh=True)


@pytest.mark.asyncio
async def test_dexscreener_selects_highest_liquidity_pair(monkeypatch):
    low = {
        "chainId": "solana",
        "baseToken": {"address": "mint", "name": "Token", "symbol": "TOK"},
        "quoteToken": {"symbol": "SOL"},
        "priceUsd": "2.0",
        "priceNative": "0.02",
        "liquidity": {"usd": 10},
        "volume": {"h24": 1},
        "priceChange": {"h24": 0},
    }
    high = {**low, "priceUsd": "3.0", "liquidity": {"usd": 1000}}
    request_json = AsyncMock(return_value=[low, high])
    monkeypatch.setattr(services, "_request_json", request_json)

    token = await services.get_token_data_dexscreener(object(), "mint", 100.0, force_refresh=True)

    assert token["price_usd"] == 3.0
    assert token["liquidity"] == 1000


@pytest.mark.asyncio
async def test_token_accounts_include_legacy_and_token_2022(monkeypatch):
    seen_programs = set()

    async def request_json(_session, _method, _url, **kwargs):
        program = kwargs["json"]["params"][1]["programId"]
        seen_programs.add(program)
        return {"jsonrpc": "2.0", "result": {"value": [{"program": program}]}}

    monkeypatch.setattr(services, "_request_json", request_json)
    accounts = await services.get_token_accounts("wallet", session=object(), force_refresh=True)

    assert seen_programs == {services.SPL_TOKEN_PROGRAM, services.TOKEN_2022_PROGRAM}
    assert {account["program"] for account in accounts} == seen_programs


@pytest.mark.asyncio
async def test_incomplete_token_program_data_is_an_explicit_failure(monkeypatch):
    async def request_json(_session, _method, _url, **kwargs):
        program = kwargs["json"]["params"][1]["programId"]
        if program == services.TOKEN_2022_PROGRAM:
            raise services.ServiceError("Solana token accounts")
        return {"jsonrpc": "2.0", "result": {"value": []}}

    monkeypatch.setattr(services, "_request_json", request_json)
    with pytest.raises(services.ServiceError, match="incomplete token-program data"):
        await services.get_token_accounts("wallet", session=object(), force_refresh=True)


@pytest.mark.asyncio
async def test_force_refresh_bypasses_cached_balance(monkeypatch):
    request_json = AsyncMock(
        side_effect=[
            {"jsonrpc": "2.0", "result": {"value": 1_000_000_000}},
            {"jsonrpc": "2.0", "result": {"value": 2_000_000_000}},
        ]
    )
    monkeypatch.setattr(services, "_request_json", request_json)

    assert await services.get_sol_balance("wallet", session=object()) == 1.0
    assert await services.get_sol_balance("wallet", session=object()) == 1.0
    assert await services.get_sol_balance("wallet", session=object(), force_refresh=True) == 2.0
    assert request_json.await_count == 2


@pytest.mark.asyncio
async def test_malformed_ethereum_hex_balance_is_not_reported_as_zero(monkeypatch):
    request_json = AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": "not-hex"})
    monkeypatch.setattr(services, "_request_json", request_json)

    with pytest.raises(services.ServiceError, match="malformed data"):
        await services.get_eth_balance("0x" + "1" * 40, session=object(), force_refresh=True)


class _Response:
    def __init__(self, status, data, headers=None):
        self.status = status
        self.data = data
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.data


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, *_args, **_kwargs):
        self.calls += 1
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_request_json_retries_retryable_statuses(monkeypatch):
    session = _Session(
        [
            _Response(500, {}),
            _Response(429, {}, {"Retry-After": "0"}),
            _Response(200, {"ok": True}),
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(services.asyncio, "sleep", sleep)

    result = await services._request_json(session, "GET", "https://example.test", operation="test")

    assert result == {"ok": True}
    assert session.calls == 3
    assert sleep.await_count == 2


def test_number_rejects_non_finite_values():
    assert services._number("nan") is None
    assert services._number("inf") is None
    assert services._number("-inf") is None
