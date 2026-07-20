import asyncio
import json
import logging
import math
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
from colorama import Fore, Style, init
from dotenv import load_dotenv
from pyfiglet import Figlet
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Load .env before importing modules that read configuration at import time.
load_dotenv()

from services import (  # noqa: E402
    ServiceError,
    get_eth_balance,
    get_eth_price,
    get_sol_balance,
    get_sol_price,
    get_token_accounts,
    get_token_data_dexscreener,
    ssl_context,
)
from utils import (  # noqa: E402
    escape_markdown,
    escape_markdown_v2,
    format_large_number,
    format_percentage,
    validate_wallet_address,
)

# Logging configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# Configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if TELEGRAM_TOKEN is None:
    raise RuntimeError("TELEGRAM_TOKEN is not set in the environment variables!")

# Admin configuration
def optional_chat_id(name: str) -> Optional[int]:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a numeric Telegram chat ID") from exc


ADMIN_CHAT_ID = optional_chat_id("ADMIN_CHAT_ID")
LOG_CHANNEL_ID = optional_chat_id("LOG_CHANNEL_ID")

if not ADMIN_CHAT_ID and not LOG_CHANNEL_ID:
    print("⚠️  Warning: Neither ADMIN_CHAT_ID nor LOG_CHANNEL_ID set in .env file. Admin notifications disabled.")
elif LOG_CHANNEL_ID:
    print("✅ User logging will be sent to private channel/group.")
elif ADMIN_CHAT_ID:
    print("✅ User logging will be sent to admin direct message.")

# Constants
MAX_MESSAGE_LENGTH = 4000
TOKENS_PER_PAGE = 6
MIN_TOKEN_VALUE_USD = 0.01
MAX_WALLETS_PER_REQUEST = 10
MAX_TOKEN_CONCURRENCY = 8
START_TIME = time.monotonic()


def format_uptime(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def parse_token_balance(token_amount: dict) -> Optional[float]:
    """Return a finite non-negative UI balance from Solana parsed token data."""
    raw_value = token_amount.get("uiAmount")
    if raw_value is None:
        raw_value = token_amount.get("uiAmountString")
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


# User tracking
USER_DATA_FILE = "user_data.json"
known_users = {}
user_count = 0
USER_DATA_LOCK = asyncio.Lock()


def _read_user_data(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_user_data_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(data, temporary, indent=2)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


async def load_user_data() -> None:
    global user_count, known_users
    path = Path(USER_DATA_FILE)
    if not path.exists():
        known_users = {}
        user_count = 0
        return
    try:
        data = await asyncio.to_thread(_read_user_data, path)
        users = data.get("users") if isinstance(data, dict) else None
        count = data.get("user_count") if isinstance(data, dict) else None
        if not isinstance(users, dict) or not isinstance(count, int) or count < 0:
            raise ValueError("invalid user-data schema")
        known_users = users
        user_count = max(count, len(users))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        backup = path.with_name(f"{path.name}.corrupt.{time.time_ns()}")
        try:
            await asyncio.to_thread(os.replace, path, backup)
            logger.error(
                "Could not load user data (%s); moved it to %s",
                type(exc).__name__,
                backup.name,
            )
        except OSError:
            logger.error(
                "Could not load or back up unreadable user data: %s",
                type(exc).__name__,
            )
        known_users = {}
        user_count = 0


async def _save_user_data_unlocked() -> None:
    snapshot = {"user_count": user_count, "users": known_users}
    await asyncio.to_thread(_write_user_data_atomic, Path(USER_DATA_FILE), snapshot)


async def save_user_data() -> None:
    async with USER_DATA_LOCK:
        await _save_user_data_unlocked()


async def register_user(user) -> bool:
    """Register one Telegram user exactly once, independently of notifications."""
    global user_count
    user_key = str(user.id)
    async with USER_DATA_LOCK:
        if user_key in known_users:
            return False
        user_count += 1
        now = datetime.now().isoformat()
        known_users[user_key] = {
            "user_number": user_count,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "language_code": user.language_code,
            "join_date": now,
            "last_active": now,
            "interactions": {"total": 0, "scans": 0, "commands": 0},
        }
        await _save_user_data_unlocked()
        return True


async def increment_user_interaction(user_id: int, interaction_type: str) -> None:
    """Increment interaction count for a registered user."""
    user_key = str(user_id)
    async with USER_DATA_LOCK:
        user = known_users.get(user_key)
        if user is None:
            return
        interactions = user.setdefault("interactions", {"total": 0, "scans": 0, "commands": 0})
        interactions["total"] += 1
        if interaction_type == "scan":
            interactions["scans"] += 1
        elif interaction_type == "command":
            interactions["commands"] += 1
        user["last_active"] = datetime.now().isoformat()
        await _save_user_data_unlocked()

async def log_activity(application, user_id: int, activity: str, wallet_address: Optional[str] = None):
    """Log user activity to the admin channel/chat"""
    target_chat_id = LOG_CHANNEL_ID if LOG_CHANNEL_ID else ADMIN_CHAT_ID
    if not target_chat_id:
        return
    
    try:
        user_info = known_users.get(str(user_id), {})
        username = user_info.get('username')
        username_display = f"@{username}" if username else f"ID:{user_id}"
        interactions = user_info.get('interactions', {}).get('total', 0)
        
        # Truncate wallet address for privacy (first 6 + last 4 chars)
        wallet_display = ""
        if wallet_address:
            if len(wallet_address) > 12:
                wallet_display = f"\n💼 `{wallet_address[:6]}...{wallet_address[-4:]}`"
            else:
                wallet_display = f"\n💼 `{wallet_address}`"
        
        activity_msg = (
            f"📊 *Activity Log*\n"
            f"👤 {escape_markdown_v2(username_display)} \\(\\#{interactions}\\)\n"
            f"🔍 {escape_markdown_v2(activity)}{wallet_display}"
        )
        
        await application.bot.send_message(
            chat_id=target_chat_id,
            text=activity_msg,
            parse_mode="MarkdownV2"
        )
    except Exception as exc:
        logger.warning("Could not log activity: %s", type(exc).__name__)

async def log_command(application, user_id: int, command: str):
    """Log command usage to admin"""
    target_chat_id = LOG_CHANNEL_ID if LOG_CHANNEL_ID else ADMIN_CHAT_ID
    if not target_chat_id:
        return
    
    try:
        user_info = known_users.get(str(user_id), {})
        username = user_info.get('username')
        username_display = f"@{username}" if username else f"ID:{user_id}"
        
        cmd_msg = f"⌨️ {escape_markdown_v2(username_display)} used `/{escape_markdown_v2(command)}`"
        
        await application.bot.send_message(
            chat_id=target_chat_id,
            text=cmd_msg,
            parse_mode="MarkdownV2"
        )
    except Exception as exc:
        logger.warning("Could not log command: %s", type(exc).__name__)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    error_name = type(context.error).__name__
    logger.error("Exception while handling an update: %s", error_name)
    
    # Send detailed error to admin
    if ADMIN_CHAT_ID:
        try:
            await notify_admin_error(context.application, "System Error", error_name)
        except Exception as exc:
            logger.warning("Could not send system-error notification: %s", type(exc).__name__)

    # Notify user if it was an update from them
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ *An unexpected error occurred\\.*\nOur team has been notified\\.",
                parse_mode="MarkdownV2"
            )
        except Exception as exc:
            logger.warning("Could not send user-facing error message: %s", type(exc).__name__)

async def notify_admin_error(application, error_type: str, error_msg: str, user_id: Optional[int] = None):
    """Send critical error notifications to admin"""
    target_chat_id = ADMIN_CHAT_ID  # Errors always go to admin directly
    if not target_chat_id:
        return
    
    try:
        user_info = ""
        if user_id:
            user_data = known_users.get(str(user_id), {})
            username = user_data.get('username')
            user_info = f"\n👤 User: {escape_markdown_v2(f'@{username}' if username else f'ID:{user_id}')}"
        
        alert_msg = (
            f"🚨 *Error Alert*\n"
            f"⚠️ *Type:* {escape_markdown_v2(error_type)}{user_info}\n"
            f"📝 *Details:* `{escape_markdown_v2(str(error_msg)[:200])}`"
        )
        
        await application.bot.send_message(
            chat_id=target_chat_id,
            text=alert_msg,
            parse_mode="MarkdownV2"
        )
    except Exception as exc:
        logger.warning("Could not send error notification: %s", type(exc).__name__)

def print_banner():
    init(autoreset=True)
    if os.isatty(1):
        print("\033[2J\033[H", end="")
    terminal_width = shutil.get_terminal_size((100, 20)).columns
    f = Figlet(font='big', width=terminal_width)
    banner_text = "DK3Y Wallet Analyzer Bot"
    banner = f.renderText(banner_text)
    colors = [
        Fore.RED, Fore.GREEN, Fore.YELLOW, Fore.BLUE, Fore.MAGENTA, Fore.CYAN, Fore.WHITE,
        Fore.LIGHTRED_EX, Fore.LIGHTGREEN_EX, Fore.LIGHTYELLOW_EX, Fore.LIGHTBLUE_EX,
        Fore.LIGHTMAGENTA_EX, Fore.LIGHTCYAN_EX, Fore.LIGHTWHITE_EX
    ]
    color_count = len(colors)
    colored_banner = ""
    color_idx = 0
    for char in banner:
        if char != " " and char != "\n":
            colored_banner += colors[color_idx % color_count] + char + Style.RESET_ALL
            color_idx += 1
        else:
            colored_banner += char
    for line in colored_banner.rstrip().split('\n'):
        print(line.center(terminal_width))

async def ensure_user_registered(application, user) -> None:
    """Persist a user independently, then best-effort notify the configured admin."""
    if not user:
        return
    if await register_user(user):
        await notify_admin_new_user(
            application,
            user.id,
            user.username,
            user.first_name,
            user.last_name,
            user.language_code,
        )

def create_wallet_keyboard(wallet_address: str, wallet_type: str) -> InlineKeyboardMarkup:
    if wallet_type == 'solana':
        buttons = [
            [InlineKeyboardButton("🌐 Solscan", url=f"https://solscan.io/account/{wallet_address}")],
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{wallet_address}_solana")]
        ]
    else:  # ethereum
        buttons = [
            [InlineKeyboardButton("🌐 DeBank", url=f"https://debank.com/profile/{wallet_address}")],
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{wallet_address}_ethereum")]
        ]
    return InlineKeyboardMarkup(buttons)

def get_token_pagination_keyboard(wallet_address: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    buttons = []
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"tokens_{wallet_address}_{page-1}"))
        nav_buttons.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"tokens_{wallet_address}_{page+1}"))
        buttons.append(nav_buttons)
    return InlineKeyboardMarkup(buttons)

# Handlers
async def notify_admin_new_user(
    application,
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
    language_code: Optional[str] = None,
) -> None:
    """Best-effort notification; registration has already been persisted."""
    target_chat_id = LOG_CHANNEL_ID or ADMIN_CHAT_ID
    if not target_chat_id:
        return

    try:
        user_record = known_users.get(str(user_id), {})
        user_number = user_record.get("user_number", user_count)
        username_display = f"@{username}" if username else "No username"
        full_name = f"{first_name or ''} {last_name or ''}".strip() or "No name"
        joined = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        language = language_code.upper() if language_code else "N/A"
        admin_msg = (
            f"🆕 *New User \\#{user_number}*\n"
            f"👤 *Name:* {escape_markdown_v2(full_name)}\n"
            f"🆔 *Username:* {escape_markdown_v2(username_display)}\n"
            f"🔢 *User ID:* `{user_id}`\n"
            f"🌍 *Language:* `{escape_markdown_v2(language)}`\n"
            f"📅 *Joined:* {escape_markdown_v2(joined)}\n"
            f"📊 *Total Users:* `{user_count}`"
        )
        await application.bot.send_message(
            chat_id=target_chat_id,
            text=admin_msg,
            parse_mode="MarkdownV2",
        )
        if LOG_CHANNEL_ID and ADMIN_CHAT_ID and user_count % 10 == 0:
            try:
                await application.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=f"🎉 *Milestone Alert\\!*\n\nBot has reached *{user_count} total users*\\!",
                    parse_mode="MarkdownV2",
                )
            except Exception as exc:
                logger.warning("Could not send milestone notification: %s", type(exc).__name__)
    except Exception as exc:
        logger.warning("Could not send new-user notification: %s", type(exc).__name__)


def is_new_user(user_id: int) -> bool:
    return str(user_id) not in known_users

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message and update.effective_user:
        user = update.effective_user
        
        # Ensure user data is loaded and registered
        await ensure_user_registered(context.application, user)
        
        # Log command usage
        await log_command(context.application, user.id, "start")
        await increment_user_interaction(user.id, "command")
        
        welcome_msg = (
            "🚀 *DK3Y Wallet Analyzer*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "✨ *Enhanced Features:*\n"
            "• 🚀 Real-time price data & market metrics\n"
            "• 📊 Portfolio analytics & token filtering\n"
            "• 💎 Interactive buttons & refresh capability\n"
            "• ⚡ Smart caching for faster responses\n"
            "• 🎯 Dust token filtering (>$0\\.01)\n\n"
            "📤 *Send any wallet address:*\n"
            "🟣 *Solana:* `11111112D4FgiiiikjQKNNh4rJN4rENWDCK8`\n"
            "🔷 *Ethereum:* `0x742d35Cc6634C0532925a3b8D4037C973B26Ed33`\n\n"
            "🔥 *Try it now!*"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("ℹ️ About", callback_data="about")]
        ])
        await update.effective_message.reply_text(
            welcome_msg,
            parse_mode="Markdown",
            reply_markup=keyboard
        )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message and update.effective_user:
        await ensure_user_registered(context.application, update.effective_user)
        await log_command(context.application, update.effective_user.id, "status")
        await increment_user_interaction(update.effective_user.id, "command")
        uptime = escape_markdown(format_uptime(time.monotonic() - START_TIME))
        await update.effective_message.reply_text(
            f"✅ *Bot is running!*\n\n⏰ *Uptime:* `{uptime}`",
            parse_mode="Markdown",
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message and update.effective_user:
        await ensure_user_registered(context.application, update.effective_user)
        await log_command(context.application, update.effective_user.id, "help")
        await increment_user_interaction(update.effective_user.id, "command")
        
        help_text = (
            "❓ *DK3Y Wallet Analyzer Help*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "👋 *Getting Started:*\n"
            "Simply send any Solana or Ethereum wallet address to the bot\\. I will automatically detect the chain and provide a detailed analysis of the holdings\\.\n\n"
            "📜 *Available Commands:*\n"
            "• `/start` \\- Show welcome message\n"
            "• `/help` \\- Show this help message\n"
            "• `/status` \\- Check if the bot is online\n\n"
            "✨ *Pro Tips:*\n"
            "• You can send multiple addresses at once (one per line) for batch analysis\\.\n"
            "• Use the **Refresh** button on any report to get latest price data\\.\n"
            "• Only tokens worth more than **$0\\.01** are shown in the detailed list to keep things clean\\."
        )
        await update.effective_message.reply_text(help_text, parse_mode="MarkdownV2")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message:
        return
        
    if not ADMIN_CHAT_ID or update.effective_user.id != ADMIN_CHAT_ID:
        await update.effective_message.reply_text("❌ *Access Denied*", parse_mode="Markdown")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "❌ *Usage:* `/broadcast [your message]`\n\n"
            "This will send a message to all registered users\\.",
            parse_mode="MarkdownV2"
        )
        return

    broadcast_text = " ".join(context.args)
    
    # Format the message nicely
    formatted_msg = (
        f"📢 *Announcement from Admin*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{broadcast_text}"
    )
    
    sent_count = 0
    fail_count = 0
    
    status_msg = await update.effective_message.reply_text(f"⏳ Sending broadcast to {len(known_users)} users...")
    
    for user_id in list(known_users):
        try:
            await context.application.bot.send_message(
                chat_id=int(user_id),
                text=formatted_msg,
                parse_mode="Markdown"
            )
            sent_count += 1
            # Rate limiting prevention
            if sent_count % 20 == 0:
                await asyncio.sleep(1)
        except Exception as exc:
            logger.warning("Broadcast delivery failed for user %s: %s", user_id, type(exc).__name__)
            fail_count += 1
            
    await status_msg.edit_text(
        f"✅ *Broadcast Complete*\n\n"
        f"👤 *Total Users:* `{len(known_users)}`\n"
        f"✅ *Sent:* `{sent_count}`\n"
        f"❌ *Failed:* `{fail_count}`",
        parse_mode="Markdown"
    )

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message and update.effective_user:
        if not ADMIN_CHAT_ID or update.effective_user.id != ADMIN_CHAT_ID:
            await update.effective_message.reply_text("❌ *Access Denied*", parse_mode="Markdown")
            return
        
        # Log command usage
        await log_command(context.application, update.effective_user.id, "stats")
        
        try:
            if not known_users:
                await load_user_data()

            recent_users = []
            sorted_users = sorted(
                known_users.items(), 
                key=lambda x: x[1].get('user_number', 0), 
                reverse=True
            )
            
            for _user_id, user_info in sorted_users[:10]:
                username = user_info.get('username')
                full_name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip()
                user_num = user_info.get('user_number', 0)
                join_date = user_info.get('join_date', '')
                
                try:
                    if join_date:
                        join_dt = datetime.fromisoformat(join_date)
                        join_str = join_dt.strftime('%m-%d %H:%M')
                    else:
                        join_str = "Unknown"
                except (TypeError, ValueError):
                    join_str = "Unknown"
                
                username_display = f"@{username}" if username else "No username"
                name_display = full_name if full_name else "No name"
                
                recent_users.append(f"#{user_num} {escape_markdown(name_display)} ({escape_markdown(username_display)}) - {escape_markdown(join_str)}")
            
            log_destination = "Private Channel/Group" if LOG_CHANNEL_ID else "Direct Messages" if ADMIN_CHAT_ID else "Disabled"
            
            stats_msg = (
                f"📊 *Bot Statistics*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👥 *Total Users:* `{user_count}`\n"
                f"📍 *Logging to:* `{escape_markdown(log_destination)}`\n"
                f"📅 *Last Updated:* `{escape_markdown(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}`\n\n"
                f"🆕 *Recent Users (Last 10):*\n"
                f"{chr(10).join(recent_users) if recent_users else 'No users yet'}"
            )
            
            await update.effective_message.reply_text(stats_msg, parse_mode="Markdown")
        
        except Exception as exc:
            logger.warning("Could not build admin stats: %s", type(exc).__name__)
            await update.effective_message.reply_text(
                "❌ *Could not fetch stats right now.*",
                parse_mode="Markdown",
            )

async def create_enhanced_solana_analysis(
    wallet_address: str,
    progress_callback=None,
    *,
    force_refresh: bool = False,
):
    # Reuse one bounded HTTP session for the provider calls.
    connector = aiohttp.TCPConnector(ssl=ssl_context, limit=MAX_TOKEN_CONCURRENCY)
    async with aiohttp.ClientSession(connector=connector) as session:
        sol_balance, sol_price_usd, token_accounts = await asyncio.gather(
            get_sol_balance(wallet_address, session, force_refresh=force_refresh),
            get_sol_price(session, force_refresh=force_refresh),
            get_token_accounts(wallet_address, session, force_refresh=force_refresh),
        )
    
    if progress_callback:
        await progress_callback(
            "🔍 *Analyzing Solana wallet...*\n"
            "✅ Wallet balance loaded\n"
            "✅ Current prices fetched\n"
            "✅ Token accounts loaded\n"
            "⏳ Processing token data..."
        )
    
    sol_usd_value = sol_balance * sol_price_usd if sol_price_usd > 0 else 0.0
    
    # Process tokens
    mint_balances = {}
    for account in token_accounts:
        info = account.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        mint = info.get("mint")
        balance = parse_token_balance(info.get("tokenAmount", {}))
        if mint and balance is not None and balance > 0:
            mint_balances[mint] = mint_balances.get(mint, 0) + balance

    last_updated_str = datetime.now().strftime('%H:%M:%S')

    header_msg = (
        f"🟣 *Enhanced Solana Analysis*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 *SOL Balance:* `{escape_markdown(format_large_number(sol_balance))}` SOL\n"
        f"💵 *SOL Price:* `${escape_markdown(f'{sol_price_usd:,.2f}')}`\n"
        f"💎 *SOL Value:* `${escape_markdown(f'{sol_usd_value:,.2f}')}`\n"
        f"🪙 *SPL Tokens:* `{escape_markdown(str(len(mint_balances)))}` different tokens\n"
    )
    
    if not mint_balances:
        header_msg += "📭 *No SPL Tokens Found*\n\n"
        header_msg += f"🏦 *Total Portfolio Value:* `${escape_markdown(f'{sol_usd_value:,.2f}')}`"
        return header_msg, [], create_wallet_keyboard(wallet_address, 'solana')

    # Fetch token details
    token_details = []
    total_tokens_value_sol = 0.0
    total_tokens_value_usd = 0.0
    valuable_tokens = 0
    
    semaphore = asyncio.Semaphore(MAX_TOKEN_CONCURRENCY)

    async def fetch_token_data(session, mint):
        async with semaphore:
            try:
                return await get_token_data_dexscreener(
                    session,
                    mint,
                    sol_price_usd,
                    force_refresh=force_refresh,
                )
            except ServiceError as exc:
                logger.warning("Token metadata unavailable for %s…: %s", mint[:6], exc.operation)
                return None

    connector = aiohttp.TCPConnector(ssl=ssl_context, limit=MAX_TOKEN_CONCURRENCY)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [fetch_token_data(session, mint) for mint in mint_balances]
        token_data_list = await asyncio.gather(*tasks)
    unavailable_token_count = sum(token_data is None for token_data in token_data_list)

    for mint, balance, token_data in zip(
        mint_balances.keys(),
        mint_balances.values(),
        token_data_list,
        strict=True,
    ):
        if token_data and token_data["name"] != "Unknown":
            token_sol_value = balance * token_data["price_in_sol"] if token_data["price_in_sol"] else 0
            token_usd_value = balance * token_data["price_usd"] if token_data["price_usd"] else 0
            
            if token_usd_value >= MIN_TOKEN_VALUE_USD:
                valuable_tokens += 1
                total_tokens_value_sol += token_sol_value
                total_tokens_value_usd += token_usd_value
                
                token_details.append({
                    "name": token_data["name"],
                    "symbol": token_data["symbol"],
                    "mint": mint,
                    "balance": balance,
                    "token_sol_value": token_sol_value,
                    "token_usd_value": token_usd_value,
                    "price_usd": token_data["price_usd"],
                    "market_cap": token_data["market_cap"],
                    "volume_24h": token_data["volume_24h"],
                    "price_change_24h": token_data["price_change_24h"],
                    "url": token_data["url"]
                })
    
    token_details.sort(key=lambda x: x['token_usd_value'], reverse=True)
    
    if progress_callback:
        await progress_callback(
            "🔍 *Analyzing Solana wallet...*\n"
            "✅ Wallet balance loaded\n"
            "✅ Current prices fetched\n"
            "✅ Token accounts loaded\n"
            "✅ Token data processed\n"
            "⏳ Generating report..."
        )
    
    total_wallet_value = sol_usd_value + total_tokens_value_usd
    
    header_msg += "💼 *Portfolio Analytics:*\n"
    header_msg += f"🪙 *Valuable Tokens:* `{escape_markdown(str(valuable_tokens))}` (>${escape_markdown(str(MIN_TOKEN_VALUE_USD))})\n"
    header_msg += f"💰 *Token Value:* `{escape_markdown(format_large_number(total_tokens_value_sol))}` SOL (`${escape_markdown(f'{total_tokens_value_usd:,.2f}')}`)\n"
    header_msg += f"🏦 *Total Portfolio:* `${escape_markdown(f'{total_wallet_value:,.2f}')}`\n"
    if unavailable_token_count:
        header_msg += (
            f"⚠️ *Partial token data:* metadata was unavailable for "
            f"`{escape_markdown(str(unavailable_token_count))}` holding(s)\n"
        )
    if total_wallet_value > 0:
        header_msg += f"📊 *Token Allocation:* `{escape_markdown(f'{(total_tokens_value_usd/total_wallet_value*100):.1f}%')}`\n"
    else:
        header_msg += f"📊 *Token Allocation:* `{escape_markdown('0.0%')}`\n"
    header_msg += f"\n⏰ *Last Updated:* `{escape_markdown(last_updated_str)}`"
    
    token_messages = []
    if token_details:
        for i in range(0, len(token_details), TOKENS_PER_PAGE):
            chunk = token_details[i:i + TOKENS_PER_PAGE]
            page_num = (i // TOKENS_PER_PAGE) + 1
            total_pages = (len(token_details) + TOKENS_PER_PAGE - 1) // TOKENS_PER_PAGE
            
            token_msg = f"🪙 *Top Holdings - Page {escape_markdown(str(page_num))}/{escape_markdown(str(total_pages))}*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            
            for j, token in enumerate(chunk, 1):
                rank = i + j
                display_name = token['name'][:20] + "..." if len(token['name']) > 23 else token['name']
                
                token_msg += f"#{escape_markdown(str(rank))} *{escape_markdown(display_name)}* (`{escape_markdown(token['symbol'])}`)\n"
                token_msg += f"📊 *Balance:* `{escape_markdown(format_large_number(token['balance']))}`\n"
                token_msg += f"💰 *Value:* `${escape_markdown(format(token['token_usd_value'], ',.2f'))}`\n"
                
                extras = []
                if token['market_cap']:
                    extras.append(f"MC: ${escape_markdown(format_large_number(token['market_cap']))}")
                if token['price_change_24h'] is not None:
                    extras.append(escape_markdown(format_percentage(token['price_change_24h'])))
                
                if extras:
                    token_msg += f"📈 {' • '.join(extras)}\n"
                
                escaped_url = token['url'].replace('(', r'\(').replace(')', r'\)')
                token_msg += f"🔗 [DexScreener]({escaped_url})\n\n"
            
            if len(token_msg) > MAX_MESSAGE_LENGTH:
                token_msg = token_msg[:MAX_MESSAGE_LENGTH-100] + "...\n\n📱 *Message truncated*"
            
            token_messages.append(token_msg)

    if progress_callback:
        await progress_callback(
            "🔍 *Analyzing Solana wallet...*\n"
            "✅ Wallet balance loaded\n"
            "✅ Current prices fetched\n"
            "✅ Token accounts loaded\n"
            "✅ Token data processed\n"
            "✅ Report generated\n"
            "🎉 *Analysis complete!*"
        )

    return header_msg, token_messages, create_wallet_keyboard(wallet_address, 'solana')

async def create_enhanced_ethereum_analysis(wallet_address: str, *, force_refresh: bool = False):
    connector = aiohttp.TCPConnector(ssl=ssl_context, limit=4)
    async with aiohttp.ClientSession(connector=connector) as session:
        eth_balance, eth_price_usd = await asyncio.gather(
            get_eth_balance(wallet_address, session, force_refresh=force_refresh),
            get_eth_price(session, force_refresh=force_refresh),
        )
    
    eth_usd_value = eth_balance * eth_price_usd if eth_price_usd > 0 else 0.0
    
    response = (
        f"🔷 *Enhanced Ethereum Analysis*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 *ETH Balance:* `{escape_markdown(format_large_number(eth_balance))}` ETH\n"
        f"💵 *ETH Price:* `${escape_markdown(f'{eth_price_usd:,.2f}')}`\n"
        f"💎 *Portfolio Value:* `${escape_markdown(f'{eth_usd_value:,.2f}')}`\n\n"
        f"⏰ *Last Updated:* `{escape_markdown(datetime.now().strftime('%H:%M:%S'))}`"
    )
    
    return response, create_wallet_keyboard(wallet_address, 'ethereum')

async def handle_wallet_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message and update.effective_message.text:
        raw_text = update.effective_message.text.strip()
        
        # Split by newlines and filter empty lines
        lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
        if update.effective_user:
            await ensure_user_registered(context.application, update.effective_user)
        if len(lines) > MAX_WALLETS_PER_REQUEST:
            await update.effective_message.reply_text(
                f"❌ Please submit at most `{MAX_WALLETS_PER_REQUEST}` wallet addresses per request.",
                parse_mode="Markdown",
            )
            return

        # Validate all addresses first
        valid_wallets = []
        invalid_wallets = []
        
        for line in lines:
            is_valid, wallet_type = validate_wallet_address(line)
            if is_valid:
                valid_wallets.append((line, wallet_type))
            else:
                invalid_wallets.append(line)
        
        # If no valid wallets found
        if not valid_wallets:
            error_msg = "❌ *Invalid Wallet Address*\n\n"
            if len(lines) == 1:
                _, wallet_type = validate_wallet_address(lines[0])
                if wallet_type == 'invalid_ethereum':
                    error_msg += "🔷 Ethereum addresses must be 42 characters starting with `0x`"
                elif wallet_type == 'invalid_solana':
                    error_msg += "🟣 Solana addresses must be 32-44 characters, base58 (no 0, O, I, l), e.g. `4Nd1mY...`"
                else:
                    error_msg += "Please send a valid wallet address:\n🟣 *Solana:* Base58 format\n🔷 *Ethereum:* Hex format starting with `0x`"
            else:
                error_msg += f"None of the {len(lines)} addresses were valid."
            await update.effective_message.reply_text(error_msg, parse_mode="Markdown")
            return
        
        # Batch mode: multiple wallets
        is_batch = len(valid_wallets) > 1
        
        if is_batch:
            # Ensure user is registered
            if update.effective_user:
                await ensure_user_registered(context.application, update.effective_user)
            
            # Show batch processing message
            batch_msg = (
                f"📦 *Batch Processing*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"✅ *Valid:* `{len(valid_wallets)}` wallets\n"
            )
            if invalid_wallets:
                batch_msg += f"❌ *Invalid:* `{len(invalid_wallets)}` addresses\n"
            batch_msg += "\n⏳ Processing..."
            
            processing_msg = await update.effective_message.reply_text(batch_msg, parse_mode="Markdown")
            
            # Log batch activity
            if update.effective_user:
                await log_activity(context.application, update.effective_user.id, f"Batch scan: {len(valid_wallets)} wallets")
                await increment_user_interaction(update.effective_user.id, 'scan')
        else:
            processing_msg = None
        
        # Process each wallet
        successful_wallets = 0
        failed_wallets = 0
        for idx, (address, wallet_type) in enumerate(valid_wallets, 1):
            try:
                # Update progress for batch mode
                if is_batch and processing_msg:
                    try:
                        await processing_msg.edit_text(
                            f"📦 *Batch Processing*\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                            f"⏳ Processing wallet {idx}/{len(valid_wallets)}...\n"
                            f"📍 `{address[:6]}...{address[-4:]}`",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                else:
                    # Single wallet mode - show standard processing message
                    processing_msg = await update.effective_message.reply_text(
                        f"🔍 *Analyzing {escape_markdown(wallet_type.title())} wallet...*\n"
                        f"⏳ Fetching wallet balance...\n"
                        f"⏳ Getting current prices...\n"
                        f"⏳ Loading token accounts...\n"
                        f"⏳ Analyzing portfolio...",
                        parse_mode="Markdown"
                    )
                    
                    # Log activity for single wallet
                    if update.effective_user:
                        await log_activity(context.application, update.effective_user.id, f"Scanned {wallet_type.title()} wallet", address)
                        await increment_user_interaction(update.effective_user.id, 'scan')
                
                async def update_progress(
                    message_text,
                    message=processing_msg,
                    batch=is_batch,
                ):
                    if message and not batch:
                        try:
                            await message.edit_text(message_text, parse_mode="Markdown")
                        except Exception:
                            pass

                if wallet_type == 'ethereum':
                    message, keyboard = await create_enhanced_ethereum_analysis(address)
                    await update.effective_message.reply_text(
                        message,
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                        disable_web_page_preview=True
                    )
                else:
                    header_msg, token_messages, keyboard = await create_enhanced_solana_analysis(address, update_progress)
                    
                    await update.effective_message.reply_text(
                        header_msg,
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                        disable_web_page_preview=True
                    )
                    if token_messages:
                        page = 0
                        total_pages = len(token_messages)
                        nav_keyboard = get_token_pagination_keyboard(address, page, total_pages)
                        await update.effective_message.reply_text(
                            token_messages[page],
                            parse_mode="Markdown",
                            reply_markup=nav_keyboard,
                            disable_web_page_preview=True
                        )
                
                successful_wallets += 1

                # Small delay between wallets to avoid rate limiting
                if is_batch and idx < len(valid_wallets):
                    await asyncio.sleep(1)
                    
            except ServiceError as exc:
                failed_wallets += 1
                logger.warning(
                    "Provider failure while analyzing %s…%s: %s",
                    address[:6],
                    address[-4:],
                    exc.operation,
                )
                await update.effective_message.reply_text(
                    f"⚠️ *Wallet data is temporarily unavailable*\n`{address[:6]}...{address[-4:]}`\nPlease retry shortly.",
                    parse_mode="Markdown",
                )
                if update.effective_user:
                    await notify_admin_error(
                        context.application,
                        "Wallet Provider Failure",
                        exc.operation,
                        update.effective_user.id,
                    )
            except Exception as exc:
                failed_wallets += 1
                logger.error(
                    "Unexpected wallet-analysis failure for %s…%s: %s",
                    address[:6],
                    address[-4:],
                    type(exc).__name__,
                )
                await update.effective_message.reply_text(
                    f"❌ *Could not analyze wallet*\n`{address[:6]}...{address[-4:]}`\nPlease retry shortly.",
                    parse_mode="Markdown",
                )
                if update.effective_user:
                    await notify_admin_error(
                        context.application,
                        "Wallet Analysis Failed",
                        type(exc).__name__,
                        update.effective_user.id,
                    )
        
        # Delete processing message
        if processing_msg:
            try:
                await processing_msg.delete()
            except Exception as exc:
                logger.debug("Could not delete progress message: %s", type(exc).__name__)
        
        # Show batch summary if applicable
        if is_batch:
            summary = (
                "📦 *Batch Complete*\n\n"
                f"✅ Successful: `{successful_wallets}`\n"
                f"⚠️ Failed: `{failed_wallets}`"
            )
            if invalid_wallets:
                summary += f"\n❌ Invalid: `{len(invalid_wallets)}`"
            await update.effective_message.reply_text(summary, parse_mode="Markdown")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    if query.data == "about":
        about_msg = (
            "🤖 *DK3Y Wallet Analyzer*\n\n"
            "👤 *Developer:* [dk3yyyy](https://github.com/dk3yyyy)\n\n"
            "🛠️ *Built with:*\n"
            "• 🐍 Python + python-telegram-bot\n"
            "• 🌐 Real-time API integrations\n"
            "• ⚡ Advanced caching system\n"
            "• 🎨 Professional UI/UX\n\n"
            "🔥 *Features:*\n"
            "• 🔗 Multi-chain support\n"
            "• 📈 Market data integration\n"
            "• 📊 Portfolio analytics\n"
            "• 🧹 Dust token filtering\n"
            "• 🤖 Interactive interface"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
        await query.edit_message_text(about_msg, parse_mode="Markdown", disable_web_page_preview=False, reply_markup=keyboard)
    
    elif query.data == "back":
        welcome_msg = (
            "🚀 *DK3Y Wallet Analyzer*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "✨ *Enhanced Features:*\n"
            "• 🚀 Real-time price data & market metrics\n"
            "• 📊 Portfolio analytics & token filtering\n"
            "• 💎 Interactive buttons & refresh capability\n"
            "• ⚡ Smart caching for faster responses\n"
            "• 🎯 Dust token filtering (>$0\\.01)\n\n"
            "📤 *Send any wallet address:*\n"
            "🟣 *Solana:* `11111112D4FgiiiikjQKNNh4rJN4rENWDCK8`\n"
            "🔷 *Ethereum:* `0x742d35Cc6634C0532925a3b8D4037C973B26Ed33`\n\n"
            "🔥 *Try it now!*"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("ℹ️ About", callback_data="about")]])
        await query.edit_message_text(welcome_msg, parse_mode="Markdown", reply_markup=keyboard)

    elif query.data and query.data.startswith("refresh_"):
        try:
            _, wallet_address, wallet_type = query.data.split("_", 2)
            
            # Show refreshing state
            await query.edit_message_text(
                f"🔄 *Refreshing {wallet_type.title()} analysis...*\n"
                f"⏳ Fetching latest prices and balances...",
                parse_mode="Markdown"
            )
            
            if wallet_type == 'ethereum':
                message, keyboard = await create_enhanced_ethereum_analysis(
                    wallet_address,
                    force_refresh=True,
                )
                await query.edit_message_text(
                    message,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                    disable_web_page_preview=True
                )
            else:
                header_msg, token_messages, keyboard = await create_enhanced_solana_analysis(
                    wallet_address,
                    force_refresh=True,
                )
                
                await query.edit_message_text(
                    header_msg,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                    disable_web_page_preview=True
                )
                if token_messages:
                    page = 0
                    total_pages = len(token_messages)
                    nav_keyboard = get_token_pagination_keyboard(wallet_address, page, total_pages)
                    await query.message.reply_text(
                        token_messages[page],
                        parse_mode="Markdown",
                        reply_markup=nav_keyboard,
                        disable_web_page_preview=True
                    )
            
            # Log refresh activity
            if update.effective_user:
                await log_activity(context.application, update.effective_user.id, f"Refreshed {wallet_type.title()} wallet", wallet_address)
                await increment_user_interaction(update.effective_user.id, 'scan')
                
        except ServiceError as exc:
            logger.warning("Refresh provider failure: %s", exc.operation)
            await query.edit_message_text(
                "⚠️ *Latest wallet data is temporarily unavailable.*\nPlease retry shortly.",
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.error("Unexpected refresh failure: %s", type(exc).__name__)
            await query.edit_message_text(
                "❌ *Could not refresh this wallet.*\nPlease retry shortly.",
                parse_mode="Markdown",
            )

    elif query.data and query.data.startswith("tokens_"):
        try:
            _, wallet_address, page_str = query.data.split("_", 2)
            page = int(page_str)
            _, token_messages, _ = await create_enhanced_solana_analysis(wallet_address)
            total_pages = len(token_messages)
            if 0 <= page < total_pages:
                nav_keyboard = get_token_pagination_keyboard(wallet_address, page, total_pages)
                await query.edit_message_text(
                    token_messages[page],
                    parse_mode="Markdown",
                    reply_markup=nav_keyboard,
                    disable_web_page_preview=True
                )
        except ServiceError as exc:
            logger.warning("Token page provider failure: %s", exc.operation)
            await query.edit_message_text(
                "⚠️ *Token data is temporarily unavailable\\.*",
                parse_mode="MarkdownV2",
            )
        except Exception as exc:
            logger.error("Unexpected token-page failure: %s", type(exc).__name__)
            await query.edit_message_text(
                "❌ *Could not load that token page\\.*",
                parse_mode="MarkdownV2",
            )

async def main():
    print_banner()
    print("\n" * 3)
    print("🚀 DK3Y Wallet Analyzer Bot is starting...\n")
    
    if not TELEGRAM_TOKEN:
        print("❌ Error: TELEGRAM_TOKEN not found in environment variables")
        return
    
    # Load user data at startup
    await load_user_data()
    print(f"📊 Loaded data for {user_count} users")
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add Error Handler
    application.add_error_handler(error_handler)
    
    # Add Command Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("ping", status))
    application.add_handler(CommandHandler("stats", admin_stats))
    application.add_handler(CommandHandler("broadcast", broadcast))
    
    # Add Callback & Message Handlers
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_wallet_address))
    
    print("🚀 Bot is now running and listening for messages!")
    
    # The Application context owns initialize()/shutdown(); start/stop remain explicit.
    async with application:
        await application.start()
        if application.updater is None:
            raise RuntimeError("Telegram updater is unavailable")
        await application.updater.start_polling()
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Bot is shutting down...")
        finally:
            if application.updater.running:
                await application.updater.stop()
            if application.running:
                await application.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
