import asyncio
import hashlib
import logging
import math
import os
import ssl
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import aiohttp
import certifi
from aiohttp import ClientTimeout

logger = logging.getLogger(__name__)

SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
ETHEREUM_RPC_URL = os.getenv("ETHEREUM_RPC_URL", "https://ethereum-rpc.publicnode.com")
SOL_PRICE_API = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
ETH_PRICE_API = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"
DEXSCREENER_TOKEN_PAIRS_API = "https://api.dexscreener.com/token-pairs/v1/solana"

SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_PROGRAMS = (SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM)

CACHE_DURATION = 300
MAX_CACHE_ENTRIES = 2048
REQUEST_ATTEMPTS = 3
MAX_TOKEN_LOGO_BYTES = 512 * 1024
TOKEN_LOGO_CONTENT_TYPES = frozenset({"image/avif", "image/jpeg", "image/png", "image/webp"})
DEXSCREENER_LOGO_HOST = "cdn.dexscreener.com"
MAX_TOKEN_LOGO_CACHE_BYTES = 8 * 1024 * 1024
MAX_TOKEN_LOGO_CACHE_ENTRIES = 256
MAX_CONCURRENT_TOKEN_LOGOS = 8
MAX_PENDING_TOKEN_LOGOS = 32
TOKEN_LOGO_NEGATIVE_TTL_SECONDS = 60

ssl_context = ssl.create_default_context(cafile=certifi.where())


class ServiceError(RuntimeError):
    """A sanitized upstream-service failure safe for application control flow."""

    def __init__(self, operation: str, reason: str = "temporarily unavailable"):
        self.operation = operation
        self.reason = reason
        super().__init__(f"{operation} {reason}")


@dataclass(frozen=True)
class TokenLogo:
    content: bytes
    content_type: str


class TokenLogoCache:
    def __init__(
        self,
        *,
        max_bytes: int = MAX_TOKEN_LOGO_CACHE_BYTES,
        max_entries: int = MAX_TOKEN_LOGO_CACHE_ENTRIES,
        ttl_seconds: int = CACHE_DURATION,
        negative_ttl_seconds: int = TOKEN_LOGO_NEGATIVE_TTL_SECONDS,
    ):
        self._cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._negative_ttl_seconds = negative_ttl_seconds
        self._total_bytes = 0
        self._lock = threading.RLock()

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    def _remove(self, key: str) -> None:
        with self._lock:
            entry = self._cache.pop(key, None)
            if entry is not None:
                self._total_bytes -= entry["size"]

    def get(
        self,
        key: str,
        *,
        now: Optional[float] = None,
    ) -> tuple[bool, Optional[TokenLogo]]:
        with self._lock:
            timestamp = time.monotonic() if now is None else now
            entry = self._cache.get(key)
            if entry is None:
                return False, None
            ttl = self._negative_ttl_seconds if entry["data"] is None else self._ttl_seconds
            if timestamp - entry["timestamp"] >= ttl:
                self._remove(key)
                return False, None
            self._cache.move_to_end(key)
            return True, entry["data"]

    def set(
        self,
        key: str,
        data: Optional[TokenLogo],
        *,
        now: Optional[float] = None,
    ) -> None:
        with self._lock:
            size = len(data.content) if data is not None else 0
            self._remove(key)
            if size > self._max_bytes:
                return
            self._cache[key] = {
                "data": data,
                "size": size,
                "timestamp": time.monotonic() if now is None else now,
            }
            self._total_bytes += size
            while self._total_bytes > self._max_bytes or len(self._cache) > self._max_entries:
                oldest_key = next(iter(self._cache))
                self._remove(oldest_key)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._total_bytes = 0


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
token_logo_cache = TokenLogoCache()
_token_logo_inflight: Dict[
    tuple[asyncio.AbstractEventLoop, str],
    asyncio.Task[Optional[TokenLogo]],
] = {}
_token_logo_capacity_lock = threading.Lock()
_active_token_logo_fetches = 0


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


def _validated_dexscreener_logo_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != DEXSCREENER_LOGO_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not parsed.path.startswith("/cms/images/")
    ):
        return None
    return value


def _matching_solana_pairs(data: Any, mint: str) -> List[dict]:
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
    return matching


def _token_logo_url(pairs: List[dict], mint: str) -> Optional[str]:
    candidates = []
    for pair in pairs:
        base = pair.get("baseToken", {})
        if str(base.get("address", "")).lower() != mint.lower():
            continue
        logo_url = _validated_dexscreener_logo_url(pair.get("info", {}).get("imageUrl"))
        if logo_url:
            candidates.append((pair, logo_url))
    if not candidates:
        return None
    _, logo_url = max(
        candidates,
        key=lambda item: _number(item[0].get("liquidity", {}).get("usd")) or 0.0,
    )
    return logo_url


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
    matching = _matching_solana_pairs(data, mint)
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
        "logo_url": _token_logo_url(matching, mint),
    }
    cache_service.set(cache_key, token_data)
    return token_data


async def _fetch_token_logo_uncached(
    mint: str,
    session: Optional[aiohttp.ClientSession],
) -> Optional[TokenLogo]:
    token_data = cache_service.get(cache_service.get_key("token_data", mint))
    logo_url = _validated_dexscreener_logo_url(
        token_data.get("logo_url") if isinstance(token_data, dict) else None
    )

    async def fetch(active_session: aiohttp.ClientSession) -> Optional[TokenLogo]:
        nonlocal logo_url
        if logo_url is None:
            data = await _request_json(
                active_session,
                "GET",
                f"{DEXSCREENER_TOKEN_PAIRS_API}/{mint}",
                operation="DexScreener token logo metadata",
                timeout=10,
            )
            logo_url = _token_logo_url(_matching_solana_pairs(data, mint), mint)
        if logo_url is None:
            return None

        try:
            async with active_session.get(
                logo_url,
                allow_redirects=False,
                headers={"Accept": "image/avif,image/webp,image/png,image/jpeg"},
                timeout=ClientTimeout(total=10),
            ) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if response.status != 200 or content_type not in TOKEN_LOGO_CONTENT_TYPES:
                    return None
                if response.content_length is not None and response.content_length > MAX_TOKEN_LOGO_BYTES:
                    return None
                content = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    content.extend(chunk)
                    if len(content) > MAX_TOKEN_LOGO_BYTES:
                        return None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

        if not content:
            return None
        return TokenLogo(bytes(content), content_type)

    return await _run_with_session(session, fetch)


async def _load_and_cache_token_logo(
    mint: str,
    session: Optional[aiohttp.ClientSession],
) -> Optional[TokenLogo]:
    global _active_token_logo_fetches
    while True:
        with _token_logo_capacity_lock:
            if _active_token_logo_fetches < MAX_CONCURRENT_TOKEN_LOGOS:
                _active_token_logo_fetches += 1
                break
        await asyncio.sleep(0.01)
    try:
        try:
            logo = await _fetch_token_logo_uncached(mint, session)
        except ServiceError:
            logo = None
    finally:
        with _token_logo_capacity_lock:
            _active_token_logo_fetches -= 1
    token_logo_cache.set(mint, logo)
    return logo


def _discard_logo_task(
    key: tuple[asyncio.AbstractEventLoop, str],
    task: asyncio.Task[Optional[TokenLogo]],
) -> None:
    with _token_logo_capacity_lock:
        if _token_logo_inflight.get(key) is task:
            _token_logo_inflight.pop(key, None)


async def fetch_token_logo(
    mint: str,
    session: Optional[aiohttp.ClientSession] = None,
) -> Optional[TokenLogo]:
    if not isinstance(mint, str) or not 32 <= len(mint) <= 44 or any(
        character not in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        for character in mint
    ):
        return None

    found, cached = token_logo_cache.get(mint)
    if found:
        return cached

    key = (asyncio.get_running_loop(), mint)
    with _token_logo_capacity_lock:
        task = _token_logo_inflight.get(key)
        if task is None:
            if len(_token_logo_inflight) >= MAX_PENDING_TOKEN_LOGOS:
                return None
            task = asyncio.create_task(_load_and_cache_token_logo(mint, session))
            _token_logo_inflight[key] = task
            task.add_done_callback(lambda completed: _discard_logo_task(key, completed))
    return await asyncio.shield(task)


async def get_eth_balance(
    wallet_address: str,
    session: Optional[aiohttp.ClientSession] = None,
    force_refresh: bool = False,
) -> float:
    cache_key = cache_service.get_key("eth_balance", wallet_address)
    if not force_refresh and (cached := cache_service.get(cache_key)) is not None:
        return cached

    async def fetch(active_session: aiohttp.ClientSession) -> float:
        data = await _request_json(
            active_session,
            "POST",
            ETHEREUM_RPC_URL,
            operation="Ethereum balance",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getBalance",
                "params": [wallet_address, "latest"],
            },
        )
        result = _rpc_result(data, "Ethereum balance")
        try:
            if not isinstance(result, str) or not result.startswith("0x"):
                raise ValueError
            wei = int(result, 16)
        except (TypeError, ValueError) as exc:
            raise ServiceError("Ethereum balance", "returned malformed data") from exc
        if wei < 0:
            raise ServiceError("Ethereum balance", "returned malformed data")
        balance = wei / 1_000_000_000_000_000_000
        cache_service.set(cache_key, balance)
        return balance

    return await _run_with_session(session, fetch)
