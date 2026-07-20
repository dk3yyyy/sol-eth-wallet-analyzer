import asyncio
import hashlib
import logging
import math
import os
import ssl
import time
from typing import Any, Dict, List, Optional

import aiohttp
import certifi
from aiohttp import ClientTimeout

logger = logging.getLogger(__name__)

SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SOL_PRICE_API = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
ETH_PRICE_API = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"
DEXSCREENER_TOKEN_PAIRS_API = "https://api.dexscreener.com/token-pairs/v1/solana"
ETHERSCAN_API = "https://api.etherscan.io/v2/api"
ETHEREUM_CHAIN_ID = "1"

SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_PROGRAMS = (SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM)

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")
CACHE_DURATION = 300
MAX_CACHE_ENTRIES = 2048
REQUEST_ATTEMPTS = 3

ssl_context = ssl.create_default_context(cafile=certifi.where())


class ServiceError(RuntimeError):
    """A sanitized upstream-service failure safe for application control flow."""

    def __init__(self, operation: str, reason: str = "temporarily unavailable"):
        self.operation = operation
        self.reason = reason
        super().__init__(f"{operation} {reason}")


class CacheService:
    def __init__(self, max_entries: int = MAX_CACHE_ENTRIES):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._last_cleanup = time.monotonic()
        self._cleanup_interval = 600
        self._max_entries = max_entries

    def _cleanup(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup > self._cleanup_interval:
            self._cache = {
                key: value
                for key, value in self._cache.items()
                if now - value["timestamp"] <= CACHE_DURATION
            }
            self._last_cleanup = now
        if len(self._cache) > self._max_entries:
            oldest = sorted(self._cache, key=lambda key: self._cache[key]["timestamp"])
            for key in oldest[: len(self._cache) - self._max_entries]:
                self._cache.pop(key, None)

    def get(self, key: str) -> Optional[Any]:
        self._cleanup()
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry["timestamp"] >= CACHE_DURATION:
            self._cache.pop(key, None)
            return None
        return entry["data"]

    def set(self, key: str, data: Any) -> None:
        self._cache[key] = {"data": data, "timestamp": time.monotonic()}
        self._cleanup()

    def clear(self) -> None:
        self._cache.clear()
        self._last_cleanup = time.monotonic()

    @staticmethod
    def get_key(prefix: str, data: str) -> str:
        digest = hashlib.sha256(data.encode()).hexdigest()[:16]
        return f"{prefix}_{digest}"


cache_service = CacheService()


async def _request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    operation: str,
    timeout: float = 10,
    **kwargs: Any,
) -> Any:
    """Fetch JSON with bounded retries and sanitized failures."""
    last_error: Optional[BaseException] = None
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            async with session.request(
                method,
                url,
                timeout=ClientTimeout(total=timeout),
                **kwargs,
            ) as response:
                if response.status == 429 or response.status >= 500:
                    if attempt + 1 < REQUEST_ATTEMPTS:
                        retry_after = response.headers.get("Retry-After", "")
                        delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 0.5 * 2**attempt
                        await asyncio.sleep(min(delay, 4.0))
                        continue
                    raise ServiceError(operation, "is rate-limited or unavailable")
                if response.status >= 400:
                    raise ServiceError(operation, f"returned HTTP {response.status}")
                try:
                    return await response.json()
                except (aiohttp.ContentTypeError, ValueError) as exc:
                    raise ServiceError(operation, "returned an invalid response") from exc
        except ServiceError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = exc
            if attempt + 1 < REQUEST_ATTEMPTS:
                await asyncio.sleep(0.5 * 2**attempt)
                continue
    raise ServiceError(operation) from last_error


async def _run_with_session(session: Optional[aiohttp.ClientSession], callback):
    if session is not None:
        return await callback(session)
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    async with aiohttp.ClientSession(connector=connector) as owned_session:
        return await callback(owned_session)


def _rpc_result(data: Any, operation: str) -> Any:
    if not isinstance(data, dict) or data.get("error") is not None or "result" not in data:
        raise ServiceError(operation, "returned an RPC error")
    return data["result"]


async def get_sol_balance(
    wallet_address: str,
    session: Optional[aiohttp.ClientSession] = None,
    force_refresh: bool = False,
) -> float:
    cache_key = cache_service.get_key("sol_balance", wallet_address)
    if not force_refresh and (cached := cache_service.get(cache_key)) is not None:
        return cached

    async def fetch(active_session: aiohttp.ClientSession) -> float:
        data = await _request_json(
            active_session,
            "POST",
            SOLANA_RPC_URL,
            operation="Solana balance",
            json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet_address]},
        )
        result = _rpc_result(data, "Solana balance")
        value = result.get("value") if isinstance(result, dict) else None
        if not isinstance(value, int) or value < 0:
            raise ServiceError("Solana balance", "returned malformed data")
        balance = value / 1_000_000_000
        cache_service.set(cache_key, balance)
        return balance

    return await _run_with_session(session, fetch)


async def _get_asset_price(
    asset: str,
    url: str,
    session: Optional[aiohttp.ClientSession],
    force_refresh: bool,
) -> float:
    cache_key = cache_service.get_key(f"{asset}_price", "usd")
    if not force_refresh and (cached := cache_service.get(cache_key)) is not None:
        return cached

    async def fetch(active_session: aiohttp.ClientSession) -> float:
        data = await _request_json(active_session, "GET", url, operation=f"{asset.upper()} price")
        value = data.get(asset, {}).get("usd") if isinstance(data, dict) else None
        if not isinstance(value, (int, float)) or value <= 0:
            raise ServiceError(f"{asset.upper()} price", "returned malformed data")
        price = float(value)
        cache_service.set(cache_key, price)
        return price

    return await _run_with_session(session, fetch)


async def get_sol_price(
    session: Optional[aiohttp.ClientSession] = None,
    force_refresh: bool = False,
) -> float:
    return await _get_asset_price("solana", SOL_PRICE_API, session, force_refresh)


async def get_eth_price(
    session: Optional[aiohttp.ClientSession] = None,
    force_refresh: bool = False,
) -> float:
    return await _get_asset_price("ethereum", ETH_PRICE_API, session, force_refresh)


async def get_token_accounts(
    wallet_address: str,
    session: Optional[aiohttp.ClientSession] = None,
    force_refresh: bool = False,
) -> List[dict]:
    cache_key = cache_service.get_key("token_accounts", wallet_address)
    if not force_refresh and (cached := cache_service.get(cache_key)) is not None:
        return cached

    async def fetch_program(active_session: aiohttp.ClientSession, program_id: str) -> List[dict]:
        data = await _request_json(
            active_session,
            "POST",
            SOLANA_RPC_URL,
            operation="Solana token accounts",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    wallet_address,
                    {"programId": program_id},
                    {"encoding": "jsonParsed"},
                ],
            },
        )
        result = _rpc_result(data, "Solana token accounts")
        value = result.get("value") if isinstance(result, dict) else None
        if not isinstance(value, list):
            raise ServiceError("Solana token accounts", "returned malformed data")
        return value

    async def fetch(active_session: aiohttp.ClientSession) -> List[dict]:
        results = await asyncio.gather(
            *(fetch_program(active_session, program_id) for program_id in TOKEN_PROGRAMS),
            return_exceptions=True,
        )
        successful = [result for result in results if isinstance(result, list)]
        if len(successful) != len(TOKEN_PROGRAMS):
            raise ServiceError("Solana token accounts", "returned incomplete token-program data")
        accounts = [account for result in successful for account in result]
        cache_service.set(cache_key, accounts)
        return accounts

    return await _run_with_session(session, fetch)


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and math.isfinite(number) else None


async def get_token_data_dexscreener(
    session: aiohttp.ClientSession,
    mint: str,
    sol_price_usd: float,
    force_refresh: bool = False,
) -> Optional[Dict[str, Any]]:
    cache_key = cache_service.get_key("token_data", mint)
    if not force_refresh and (cached := cache_service.get(cache_key)) is not None:
        return cached

    data = await _request_json(
        session,
        "GET",
        f"{DEXSCREENER_TOKEN_PAIRS_API}/{mint}",
        operation="DexScreener token data",
        timeout=15,
    )
    pairs = data if isinstance(data, list) else data.get("pairs", []) if isinstance(data, dict) else []
    matching = []
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("chainId") != "solana":
            continue
        base = pair.get("baseToken", {})
        quote = pair.get("quoteToken", {})
        addresses = {str(base.get("address", "")).lower(), str(quote.get("address", "")).lower()}
        if mint.lower() in addresses:
            matching.append(pair)
    if not matching:
        return None

    pair = max(matching, key=lambda item: _number(item.get("liquidity", {}).get("usd")) or 0.0)
    base = pair.get("baseToken", {})
    quote = pair.get("quoteToken", {})
    mint_is_base = str(base.get("address", "")).lower() == mint.lower()
    token = base if mint_is_base else quote
    price_usd = _number(pair.get("priceUsd"))
    price_native = _number(pair.get("priceNative"))

    if mint_is_base:
        price_in_sol = price_native if str(quote.get("symbol", "")).lower() == "sol" else (
            price_usd / sol_price_usd if price_usd is not None and sol_price_usd > 0 else None
        )
    else:
        if price_native and price_native > 0:
            if str(base.get("symbol", "")).lower() == "sol":
                price_in_sol = 1 / price_native
                price_usd = price_in_sol * sol_price_usd if sol_price_usd > 0 else None
            elif price_usd is not None:
                price_usd = price_usd / price_native
                price_in_sol = price_usd / sol_price_usd if sol_price_usd > 0 else None
            else:
                price_in_sol = None
        else:
            price_in_sol = None
            price_usd = None

    token_data = {
        "name": token.get("name", "Unknown"),
        "symbol": token.get("symbol", "UNK"),
        "price_usd": price_usd,
        "price_in_sol": price_in_sol,
        "market_cap": pair.get("marketCap") or pair.get("fdv") if mint_is_base else None,
        "volume_24h": pair.get("volume", {}).get("h24"),
        "liquidity": pair.get("liquidity", {}).get("usd"),
        "price_change_24h": pair.get("priceChange", {}).get("h24"),
        "url": pair.get("url") or f"https://dexscreener.com/solana/{pair.get('pairAddress', mint)}",
    }
    cache_service.set(cache_key, token_data)
    return token_data


async def get_eth_balance(
    wallet_address: str,
    session: Optional[aiohttp.ClientSession] = None,
    force_refresh: bool = False,
) -> float:
    cache_key = cache_service.get_key("eth_balance", wallet_address)
    if not force_refresh and (cached := cache_service.get(cache_key)) is not None:
        return cached
    if not ETHERSCAN_API_KEY:
        raise ServiceError("Ethereum balance", "is not configured")

    async def fetch(active_session: aiohttp.ClientSession) -> float:
        data = await _request_json(
            active_session,
            "GET",
            ETHERSCAN_API,
            operation="Ethereum balance",
            params={
                "chainid": ETHEREUM_CHAIN_ID,
                "module": "account",
                "action": "balance",
                "address": wallet_address,
                "tag": "latest",
                "apikey": ETHERSCAN_API_KEY,
            },
        )
        if not isinstance(data, dict) or str(data.get("status")) != "1":
            raise ServiceError("Ethereum balance", "provider rejected the request")
        result = data.get("result")
        try:
            wei = int(result)
        except (TypeError, ValueError) as exc:
            raise ServiceError("Ethereum balance", "returned malformed data") from exc
        if wei < 0:
            raise ServiceError("Ethereum balance", "returned malformed data")
        balance = wei / 1_000_000_000_000_000_000
        cache_service.set(cache_key, balance)
        return balance

    return await _run_with_session(session, fetch)
