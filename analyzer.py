import asyncio
import math
from datetime import datetime, timezone
from typing import Any

import aiohttp

from services import (
    ServiceError,
    get_eth_balance,
    get_eth_price,
    get_sol_balance,
    get_sol_price,
    get_token_accounts,
    get_token_data_dexscreener,
    ssl_context,
)
from utils import validate_wallet_address

MIN_TOKEN_VALUE_USD = 0.01
MAX_TOKEN_CONCURRENCY = 8


def _updated_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _analyze_ethereum(address: str, *, force_refresh: bool) -> dict[str, Any]:
    connector = aiohttp.TCPConnector(ssl=ssl_context, limit=4)
    async with aiohttp.ClientSession(connector=connector) as session:
        balance, price_usd = await asyncio.gather(
            get_eth_balance(address, session, force_refresh=force_refresh),
            get_eth_price(session, force_refresh=force_refresh),
        )

    value_usd = balance * price_usd
    return {
        "address": address,
        "chain": "ethereum",
        "native_asset": {
            "symbol": "ETH",
            "balance": balance,
            "price_usd": price_usd,
            "value_usd": value_usd,
        },
        "total_value_usd": value_usd,
        "tokens": [],
        "token_summary": {
            "holding_count": 0,
            "valued_count": 0,
            "unavailable_count": 0,
            "value_usd": 0.0,
            "allocation_percent": 0.0,
        },
        "warnings": [],
        "explorer_url": f"https://etherscan.io/address/{address}",
        "updated_at": _updated_at(),
    }


async def _analyze_solana(address: str, *, force_refresh: bool) -> dict[str, Any]:
    connector = aiohttp.TCPConnector(ssl=ssl_context, limit=MAX_TOKEN_CONCURRENCY)
    async with aiohttp.ClientSession(connector=connector) as session:
        balance, price_usd, accounts = await asyncio.gather(
            get_sol_balance(address, session, force_refresh=force_refresh),
            get_sol_price(session, force_refresh=force_refresh),
            get_token_accounts(address, session, force_refresh=force_refresh),
        )

        mint_balances: dict[str, float] = {}
        for account in accounts:
            info = account.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            mint = info.get("mint")
            token_amount = info.get("tokenAmount", {})
            raw_balance = token_amount.get("uiAmount")
            if raw_balance is None:
                raw_balance = token_amount.get("uiAmountString")
            try:
                token_balance = float(raw_balance)
            except (TypeError, ValueError):
                continue
            if not mint or not math.isfinite(token_balance) or token_balance <= 0:
                continue
            mint_balances[mint] = mint_balances.get(mint, 0.0) + token_balance

        semaphore = asyncio.Semaphore(MAX_TOKEN_CONCURRENCY)

        async def fetch_metadata(mint: str):
            async with semaphore:
                try:
                    return await get_token_data_dexscreener(
                        session,
                        mint,
                        price_usd,
                        force_refresh=force_refresh,
                    )
                except ServiceError:
                    return None

        metadata = await asyncio.gather(*(fetch_metadata(mint) for mint in mint_balances))

    tokens = []
    unavailable_count = 0
    for (mint, token_balance), token_data in zip(mint_balances.items(), metadata, strict=True):
        if not token_data or token_data.get("price_usd") is None:
            unavailable_count += 1
            continue
        token_value_usd = token_balance * token_data["price_usd"]
        if token_value_usd < MIN_TOKEN_VALUE_USD:
            continue
        tokens.append(
            {
                "name": token_data.get("name") or "Unknown",
                "symbol": token_data.get("symbol") or "UNK",
                "mint": mint,
                "balance": token_balance,
                "price_usd": token_data["price_usd"],
                "value_usd": token_value_usd,
                "market_cap_usd": token_data.get("market_cap"),
                "volume_24h_usd": token_data.get("volume_24h"),
                "liquidity_usd": token_data.get("liquidity"),
                "price_change_24h_percent": token_data.get("price_change_24h"),
                "market_url": token_data.get("url"),
                "logo_available": bool(token_data.get("logo_url")),
            }
        )

    tokens.sort(key=lambda token: token["value_usd"], reverse=True)
    native_value_usd = balance * price_usd
    token_value_usd = sum(token["value_usd"] for token in tokens)
    total_value_usd = native_value_usd + token_value_usd
    allocation = token_value_usd / total_value_usd * 100 if total_value_usd else 0.0
    warning_suffix = "holding" if unavailable_count == 1 else "holdings"
    warnings = (
        [f"Market data was unavailable for {unavailable_count} {warning_suffix}."]
        if unavailable_count
        else []
    )

    return {
        "address": address,
        "chain": "solana",
        "native_asset": {
            "symbol": "SOL",
            "balance": balance,
            "price_usd": price_usd,
            "value_usd": native_value_usd,
        },
        "total_value_usd": total_value_usd,
        "tokens": tokens,
        "token_summary": {
            "holding_count": len(mint_balances),
            "valued_count": len(tokens),
            "unavailable_count": unavailable_count,
            "value_usd": token_value_usd,
            "allocation_percent": allocation,
        },
        "warnings": warnings,
        "explorer_url": f"https://solscan.io/account/{address}",
        "updated_at": _updated_at(),
    }


async def analyze_wallet(address: str, *, force_refresh: bool = False) -> dict[str, Any]:
    normalized = address.strip()
    valid, chain = validate_wallet_address(normalized)
    if not valid:
        raise ValueError("Enter a valid Solana or Ethereum wallet address.")
    if chain == "ethereum":
        return await _analyze_ethereum(normalized, force_refresh=force_refresh)
    return await _analyze_solana(normalized, force_refresh=force_refresh)
