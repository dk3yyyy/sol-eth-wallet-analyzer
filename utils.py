import re
from typing import Optional, Tuple

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {char: index for index, char in enumerate(_BASE58_ALPHABET)}


def escape_markdown(text: str) -> str:
    """Escape Telegram legacy-Markdown control characters."""
    if not isinstance(text, str):
        text = str(text)
    escape_chars = r"_*[`"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)


def escape_markdown_v2(text: str) -> str:
    """Escape Telegram MarkdownV2 control characters."""
    if not isinstance(text, str):
        text = str(text)
    escape_chars = r"_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)


def format_large_number(num: Optional[float]) -> str:
    """Format large numbers into readable K/M/B/T strings."""
    if num is None or num == 0:
        return "0.00"
    if num >= 1e12:
        return f"{num / 1e12:.2f}T"
    if num >= 1e9:
        return f"{num / 1e9:.2f}B"
    if num >= 1e6:
        return f"{num / 1e6:.2f}M"
    if num >= 1e3:
        return f"{num / 1e3:.2f}K"
    if num >= 1:
        return f"{num:.2f}"
    if num >= 0.01:
        return f"{num:.4f}"
    return f"{num:.8f}"


def format_percentage(pct: Optional[float]) -> str:
    """Format a percentage with an indicator."""
    if pct is None:
        return ""
    if pct > 0:
        return f"🟢 +{pct:.2f}%"
    if pct < 0:
        return f"🔴 -{abs(pct):.2f}%"
    return "⚪ 0.00%"


def _base58_decode(value: str) -> bytes:
    number = 0
    for char in value:
        if char not in _BASE58_INDEX:
            raise ValueError("invalid base58 character")
        number = number * 58 + _BASE58_INDEX[char]
    payload = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeroes + payload


def validate_wallet_address(address: str) -> Tuple[bool, str]:
    """Validate Ethereum shape or a 32-byte Solana public key."""
    address = address.strip()
    if address.startswith("0x"):
        if len(address) == 42 and re.fullmatch(r"0x[a-fA-F0-9]{40}", address):
            return True, "ethereum"
        return False, "invalid_ethereum"

    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address):
        try:
            if len(_base58_decode(address)) == 32:
                return True, "solana"
        except ValueError:
            pass
    return False, "invalid_solana"
