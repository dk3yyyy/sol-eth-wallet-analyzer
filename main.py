import os
import shutil
import json
import logging
import asyncio
import aiofiles
import aiohttp
from datetime import datetime
from typing import Optional, Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from dotenv import load_dotenv
from colorama import init, Fore, Style
from pyfiglet import Figlet

# Import from new modules
from services import (
    get_sol_balance, get_sol_price, get_token_accounts, get_token_data_dexscreener,
    get_eth_balance, get_eth_price, ssl_context
)
from utils import (
    escape_markdown, escape_markdown_v2, format_large_number, format_percentage,
    validate_wallet_address
)

# Load environment variables
load_dotenv()

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
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")

if ADMIN_CHAT_ID:
    ADMIN_CHAT_ID = int(ADMIN_CHAT_ID)
if LOG_CHANNEL_ID:
    LOG_CHANNEL_ID = int(LOG_CHANNEL_ID)

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

# User Tracking
USER_DATA_FILE = "user_data.json"
known_users = {}
user_count = 0

async def load_user_data():
    global user_count, known_users
    try:
        if os.path.exists(USER_DATA_FILE):
            async with aiofiles.open(USER_DATA_FILE, 'r') as f:
                content = await f.read()
                data = json.loads(content)
                user_count = data.get('user_count', 0)
                known_users = data.get('users', {})
        else:
            known_users = {}
            user_count = 0
    except Exception as e:
        logger.error(f"Error loading user data: {e}")
        known_users = {}

async def save_user_data():
    try:
        data = {
            'user_count': user_count,
            'users': known_users
        }
        async with aiofiles.open(USER_DATA_FILE, 'w') as f:
            await f.write(json.dumps(data, indent=2))
    except Exception as e:
        logger.error(f"Error saving user data: {e}")

async def increment_user_interaction(user_id: int, interaction_type: str):
    """Increment interaction count for a user"""
    global known_users
    user_key = str(user_id)
    if user_key in known_users:
        if 'interactions' not in known_users[user_key]:
            known_users[user_key]['interactions'] = {'total': 0, 'scans': 0, 'commands': 0}
        known_users[user_key]['interactions']['total'] += 1
        if interaction_type == 'scan':
            known_users[user_key]['interactions']['scans'] += 1
        elif interaction_type == 'command':
            known_users[user_key]['interactions']['commands'] += 1
        known_users[user_key]['last_active'] = datetime.now().isoformat()
        await save_user_data()

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
    except Exception as e:
        logger.error(f"Error logging activity: {e}")

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
    except Exception as e:
        logger.error(f"Error logging command: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger.error(f"Exception while handling an update: {context.error}")
    
    # Send detailed error to admin
    if ADMIN_CHAT_ID:
        try:
            error_msg = str(context.error)
            await notify_admin_error(context.application, "System Error", error_msg)
        except Exception:
            pass

    # Notify user if it was an update from them
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ *An unexpected error occurred\\.*\nOur team has been notified\\.",
                parse_mode="MarkdownV2"
            )
        except Exception:
            pass

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
    except Exception as e:
        logger.error(f"Error sending error notification: {e}")

# Helper Functions
def print_banner():
    if os.name == 'nt':
        os.system('cls')
    else:
        os.system('clear')
    terminal_width = shutil.get_terminal_size((100, 20)).columns
    f = Figlet(font='big', width=terminal_width)
    banner_text = "DK3Y Wallet Analyzer Bot"
    banner = f.renderText(banner_text)
    init(autoreset=True)
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
    """Ensure user is in known_users and data is saved"""
    if not user:
        return
        
    if is_new_user(user.id):
        await notify_admin_new_user(
            application,
            user.id,
            user.username,
            user.first_name,
            user.last_name,
            user.language_code
        )
    await increment_user_interaction(user.id, 'command')

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
async def notify_admin_new_user(application, user_id: int, username: Optional[str], first_name: Optional[str], last_name: Optional[str], language_code: Optional[str] = None):
    target_chat_id = LOG_CHANNEL_ID if LOG_CHANNEL_ID else ADMIN_CHAT_ID
    
    if not target_chat_id:
        return
    
    try:
        global user_count, known_users
        user_count += 1
        
        known_users[str(user_id)] = {
            'user_number': user_count,
            'username': username,
            'first_name': first_name,
            'last_name': last_name,
            'language_code': language_code,
            'join_date': datetime.now().isoformat(),
            'last_active': datetime.now().isoformat(),
            'interactions': {'total': 0, 'scans': 0, 'commands': 0}
        }
        
        await save_user_data()
        
        username_display = f"@{username}" if username else "No username"
        full_name = f"{first_name or ''} {last_name or ''}".strip() or "No name"
        
        join_date_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        lang_display = language_code.upper() if language_code else "N/A"
        
        if LOG_CHANNEL_ID:
            separator = "━━━━━━━━━━━━━━━━━━━━━━"
            admin_msg = (
                f"🆕 **New User \\#{user_count}**\n"
                f"{escape_markdown_v2(separator)}\n"
                f"👤 **Name:** {escape_markdown_v2(full_name)}\n"
                f"🆔 **Username:** {escape_markdown_v2(username_display)}\n"
                f"🔢 **User ID:** `{user_id}`\n"
                f"🌍 **Language:** `{escape_markdown_v2(lang_display)}`\n"
                f"📅 **Joined:** {escape_markdown_v2(join_date_str)}\n"
                f"📊 **Total Users:** `{user_count}`"
            )
        else:
            admin_msg = (
                f"🆕 *New User\\!* \\[{user_count}\\]\n"
                f"👤 *Name:* {escape_markdown_v2(full_name)}\n"
                f"🆔 *Username:* {escape_markdown_v2(username_display)}\n"
                f"🔢 *User ID:* `{user_id}`\n"
                f"🌍 *Language:* `{escape_markdown_v2(lang_display)}`\n"
                f"📅 *Joined:* {escape_markdown_v2(join_date_str)}"
            )
        
        await application.bot.send_message(
            chat_id=target_chat_id,
            text=admin_msg,
            parse_mode="MarkdownV2"
        )
        
        if LOG_CHANNEL_ID and ADMIN_CHAT_ID and user_count % 10 == 0:
            milestone_msg = f"🎉 *Milestone Alert\\!*\n\nBot has reached **{user_count} total users**\\!"
            try:
                await application.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=milestone_msg,
                    parse_mode="MarkdownV2"
                )
            except Exception:
                pass
        
    except Exception as e:
        logger.error(f"Error notifying admin about new user: {e}")

def is_new_user(user_id: int) -> bool:
    return str(user_id) not in known_users

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message and update.effective_user:
        user = update.effective_user
        
        # Ensure user data is loaded and registered
        await ensure_user_registered(context.application, user)
        
        # Log command usage
        if not is_new_user(user.id):
            await log_command(context.application, user.id, "start")
        
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
        # Log command usage
        await log_command(context.application, update.effective_user.id, "status")
        await increment_user_interaction(update.effective_user.id, 'command')
        
        await update.effective_message.reply_text(
            f"✅ *Bot is running!*\n\n⏰ *Uptime:* `{escape_markdown(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}`",
            parse_mode="Markdown"
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message and update.effective_user:
        await log_command(context.application, update.effective_user.id, "help")
        await increment_user_interaction(update.effective_user.id, 'command')
        
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
    
    for user_id in known_users.keys():
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
        except Exception as e:
            logger.error(f"Failed to send broadcast to {user_id}: {e}")
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
            
            for user_id, user_info in sorted_users[:10]:
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
                except:
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
        
        except Exception as e:
            await update.effective_message.reply_text(
                f"❌ *Error fetching stats:* `{escape_markdown(str(e))}`",
                parse_mode="Markdown"
            )

async def create_enhanced_solana_analysis(wallet_address: str, progress_callback=None):
    # Fetch data
    sol_balance_task = get_sol_balance(wallet_address)
    sol_price_task = get_sol_price()
    token_accounts_task = get_token_accounts(wallet_address)
    
    sol_balance, sol_price_usd, token_accounts = await asyncio.gather(
        sol_balance_task, sol_price_task, token_accounts_task
    )
    
    if progress_callback:
        await progress_callback(
            f"🔍 *Analyzing Solana wallet...*\n"
            f"✅ Wallet balance loaded\n"
            f"✅ Current prices fetched\n"
            f"✅ Token accounts loaded\n"
            f"⏳ Processing token data..."
        )
    
    sol_usd_value = sol_balance * sol_price_usd if sol_price_usd > 0 else 0.0
    
    # Process tokens
    mint_balances = {}
    for account in token_accounts:
        info = account.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        mint = info.get("mint")
        balance = info.get("tokenAmount", {}).get("uiAmount", 0)
        if mint and balance > 0:
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
        header_msg += f"📭 *No SPL Tokens Found*\n\n"
        header_msg += f"🏦 *Total Portfolio Value:* `${escape_markdown(f'{sol_usd_value:,.2f}')}`"
        return header_msg, [], create_wallet_keyboard(wallet_address, 'solana')

    # Fetch token details
    token_details = []
    total_tokens_value_sol = 0.0
    total_tokens_value_usd = 0.0
    valuable_tokens = 0
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
        tasks = [get_token_data_dexscreener(session, mint, sol_price_usd) for mint in mint_balances.keys()]
        token_data_list = await asyncio.gather(*tasks)
    
    for mint, balance, token_data in zip(mint_balances.keys(), mint_balances.values(), token_data_list):
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
            f"🔍 *Analyzing Solana wallet...*\n"
            f"✅ Wallet balance loaded\n"
            f"✅ Current prices fetched\n"
            f"✅ Token accounts loaded\n"
            f"✅ Token data processed\n"
            f"⏳ Generating report..."
        )
    
    total_wallet_value = sol_usd_value + total_tokens_value_usd
    
    header_msg += f"💼 *Portfolio Analytics:*\n"
    header_msg += f"🪙 *Valuable Tokens:* `{escape_markdown(str(valuable_tokens))}` (>${escape_markdown(str(MIN_TOKEN_VALUE_USD))})\n"
    header_msg += f"💰 *Token Value:* `{escape_markdown(format_large_number(total_tokens_value_sol))}` SOL (`${escape_markdown(f'{total_tokens_value_usd:,.2f}')}`)\n"
    header_msg += f"🏦 *Total Portfolio:* `${escape_markdown(f'{total_wallet_value:,.2f}')}`\n"
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
            f"🔍 *Analyzing Solana wallet...*\n"
            f"✅ Wallet balance loaded\n"
            f"✅ Current prices fetched\n"
            f"✅ Token accounts loaded\n"
            f"✅ Token data processed\n"
            f"✅ Report generated\n"
            f"🎉 *Analysis complete!*"
        )

    return header_msg, token_messages, create_wallet_keyboard(wallet_address, 'solana')

async def create_enhanced_ethereum_analysis(wallet_address: str):
    eth_balance, eth_price_usd = await asyncio.gather(
        get_eth_balance(wallet_address),
        get_eth_price()
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
        lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
        
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
            batch_msg += f"\n⏳ Processing..."
            
            processing_msg = await update.effective_message.reply_text(batch_msg, parse_mode="Markdown")
            
            # Log batch activity
            if update.effective_user:
                await log_activity(context.application, update.effective_user.id, f"Batch scan: {len(valid_wallets)} wallets")
                await increment_user_interaction(update.effective_user.id, 'scan')
        else:
            processing_msg = None
        
        # Process each wallet
        for idx, (address, wallet_type) in enumerate(valid_wallets, 1):
            try:
                # Update progress for batch mode
                if is_batch and processing_msg:
                    try:
                        await processing_msg.edit_text(
                            f"� *Batch Processing*\n"
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
                        f"�🔍 *Analyzing {escape_markdown(wallet_type.title())} wallet...*\n"
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
                
                async def update_progress(message_text):
                    if processing_msg and not is_batch:
                        try:
                            await processing_msg.edit_text(message_text, parse_mode="Markdown")
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
                
                # Small delay between wallets to avoid rate limiting
                if is_batch and idx < len(valid_wallets):
                    await asyncio.sleep(1)
                    
            except Exception as e:
                logger.error(f"Error analyzing wallet {address}: {e}")
                await update.effective_message.reply_text(
                    f"❌ *Error analyzing wallet*\n`{address[:6]}...{address[-4:]}`\n`{escape_markdown(str(e)[:100])}`",
                    parse_mode="Markdown"
                )
                if update.effective_user:
                    await notify_admin_error(context.application, "Wallet Analysis Failed", str(e), update.effective_user.id)
        
        # Delete processing message
        if processing_msg:
            try:
                await processing_msg.delete()
            except Exception:
                pass
        
        # Show batch summary if applicable
        if is_batch:
            summary = f"✅ *Batch Complete*\n\nProcessed `{len(valid_wallets)}` wallets successfully."
            if invalid_wallets:
                summary += f"\n❌ Skipped `{len(invalid_wallets)}` invalid addresses."
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
                message, keyboard = await create_enhanced_ethereum_analysis(wallet_address)
                await query.edit_message_text(
                    message,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                    disable_web_page_preview=True
                )
            else:
                header_msg, token_messages, keyboard = await create_enhanced_solana_analysis(wallet_address)
                
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
                
        except Exception as e:
            logger.error(f"Error in refresh callback: {e}")
            await query.edit_message_text(f"❌ *Refresh Error:* `{escape_markdown(str(e))}`", parse_mode="Markdown")

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
        except Exception as e:
            await query.edit_message_text(f"❌ *Error:* `{escape_markdown(str(e))}`", parse_mode="Markdown")

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
    
    # Manual initialization and polling for full async control
    async with application:
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        
        # Keep the bot running until interrupted
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Bot is shutting down...")
        finally:
            if application.updater.running:
                await application.updater.stop()
            await application.stop()
            await application.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
