# 🚀 DK3Y Wallet Analyzer

An async Telegram bot that validates Solana and Ethereum addresses, reports native balances and USD values, and provides detailed SPL-token portfolio analytics for Solana wallets.

## ✨ Features

### 🎯 **Core Features**

- **Two-chain Support**: Analyze Solana and Ethereum addresses
- **Real-time Data**: Native-asset prices from CoinGecko and Solana token market data from DexScreener
- **Solana Portfolio Analytics**: legacy SPL Token and Token-2022 holdings, market metrics, allocation, and dust filtering
- **Interactive UI**: Pagination, progress indicators, explorer links
- **Truthful Failure States**: provider failures are reported as unavailable instead of being shown as zero balances

### 👑 **Admin Features**

- **User Tracking**: Auto-log new users with sequential numbering
- **Flexible Logging**: Private channel/group or direct messages
- **Statistics**: `/stats` command for user metrics and analytics
- **Milestone Alerts**: Notifications every 10 new users

## 🛠️ Quick Setup

### 1. **Prerequisites**

```bash
# Requirements
- Python 3.11+
- Telegram Bot Token (from @BotFather)
- Etherscan API Key (from etherscan.io/apis)
```

### 2. **Installation**

```bash
git clone https://github.com/dk3yyyy/sol-eth-wallet-analyzer.git
cd sol-eth-wallet-analyzer
pip install -r requirements.txt
```

### 3. **Configuration**

Create `.env` file:

```env
TELEGRAM_TOKEN=your_bot_token_here
ETHERSCAN_API_KEY=your_etherscan_key_here

# Optional custom Solana JSON-RPC endpoint
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com

# Admin Features (Optional)
ADMIN_CHAT_ID=your_chat_id                    # For stats access
LOG_CHANNEL_ID=-1001234567890                 # For user logging (recommended)
```

### 4. **Run**

```bash
python main.py
```

## 📊 Admin Setup

### **Option 1: Channel Logging (Recommended)**

1. Create private Telegram channel
2. Add bot as admin with "Post Messages" permission
3. Forward message from channel to [@userinfobot](https://t.me/userinfobot) to get ID
4. Add `LOG_CHANNEL_ID=-1001234567890` to `.env`

### **Option 2: Direct Messages**

1. Message [@userinfobot](https://t.me/userinfobot) to get your chat ID
2. Add `ADMIN_CHAT_ID=123456789` to `.env`

## 🎮 Usage

### **Commands**

- `/start` — Welcome & features overview
- `/status` — Bot health check  
- `/stats` — Admin user statistics

### **Wallet Analysis**

Send any wallet address:

- **Solana**: `11111112D4FgiiiikjQKNNh4rJN4rENWDCK8`
- **Ethereum**: `0x742d35Cc6634C0532925a3b8D4037C973B26Ed33`

The bot auto-detects the address type and provides:

- Native SOL or ETH balance and estimated USD value
- SPL-token holdings and market data for Solana addresses
- Solana portfolio allocation percentages
- Interactive navigation for large Solana portfolios

## 🔧 Configuration

| Variable | Description | Required |
|----------|-------------|----------|
| `TELEGRAM_TOKEN` | Bot token from BotFather | ✅ Required |
| `ETHERSCAN_API_KEY` | Etherscan API key | ✅ Required |
| `SOLANA_RPC_URL` | Solana JSON-RPC endpoint | ⚪ Optional |
| `ADMIN_CHAT_ID` | Your chat ID for admin access | ⚪ Optional |
| `LOG_CHANNEL_ID` | Channel/group ID for user logs | ⚪ Optional |

### **Bot Settings**

- **Cache**: 5 minutes for faster responses
- **Dust Filter**: $0.01 minimum token value  
- **Pagination**: 6 tokens per page
- **Batch limit**: 10 submitted addresses per message
- **Token concurrency**: At most 8 simultaneous DexScreener lookups per analysis
- **APIs**: Solana JSON-RPC, CoinGecko, DexScreener, Etherscan V2 (`chainid=1`)

## 🔒 Security & Performance

- ✅ Environment variables for secrets
- ✅ Smart caching system
- ✅ Bounded request concurrency, timeouts, retries, and bounded caches
- ✅ Explicit provider-error and partial-token-metadata reporting
- ✅ Atomic, synchronized user-data persistence
- ✅ Async processing for speed

### Data and privacy

The bot stores Telegram user IDs, names, usernames, language codes, join/last-active timestamps, and interaction counters in `user_data.json`. The file is written atomically with owner-only permissions on supported systems, but it is still plaintext. Operators are responsible for access control, backups, retention, deletion requests, and an appropriate privacy notice. Wallet addresses sent to the bot are processed by the configured Solana RPC, CoinGecko, DexScreener, or Etherscan as required for analysis.

### Failure behavior

- Missing or rejected Etherscan credentials make Ethereum balance data unavailable; they never produce a synthetic `0 ETH` result.
- Solana RPC errors, malformed responses, timeouts, and incomplete legacy/Token-2022 account queries stop that wallet report with a retryable warning.
- CoinGecko failures stop reports that require the affected native-asset price.
- Individual DexScreener metadata failures are omitted from token valuation and disclosed as partial data.
- HTTP 429 and server errors use bounded retries. Failed results are not cached as valid zero values.
- Refresh actions bypass the relevant five-minute caches.

## 🧪 Development and verification

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m bandit -q -r main.py services.py utils.py --severity-level medium --confidence-level medium
.venv/bin/python -m pip_audit -r requirements.txt
```

GitHub Actions runs tests on Python 3.11 and 3.13, plus lint, security, dependency-audit, and compilation gates. The tests mock external providers and do not send Telegram messages or require real API credentials.

### Disable and roll back

Stop the bot process to disable polling; no external scheduler is installed by this repository. Back up `user_data.json` before migrations. To roll back an application release, restore the previous code revision and its matching dependency set, then restart the process. Do not replace `user_data.json` while the bot is running.

## 🐛 Troubleshooting

**Common Issues:**

- `TELEGRAM_TOKEN not found` → Check `.env` file exists
- `ETHERSCAN_API_KEY not set` → Add API key to `.env`
- Slow responses → Large Solana portfolios require multiple bounded market-data calls; repeated requests use five-minute caches
- Provider unavailable → Retry after the upstream service recovers; the bot intentionally does not replace missing financial data with zero

## 👨‍💻 Developer

**Built by:** [dk3yyyy](https://github.com/dk3yyyy)

**Tech Stack:** Python, python-telegram-bot, aiohttp, asyncio

## 📄 License

MIT License - Free to use and modify

## ⭐ Support

If you find this useful:

- ⭐ Star the repository
- 🍴 Fork and contribute

## 💸 Tips

If you'd like to support the project, you can send tips to any of the following addresses:

- **SOL:** `CZXTNF5k7BWTW8fR7KGNjXTmyUedRgMMPXmi8jWKPfeK`
- **ETH:** `0x6327E5374d244a11cf1d68f189E55f27e3EEe043`
- **BTC:** `bc1qtwe8mxt8nu9guquh0s9g3ap9uuftd057qfp57s`
- **USDT (Tron):** `TJMSyxu2J8zvMCcv6buN7zJNkmWn1n9qMQ`

---

*Solana and Ethereum balance analysis with detailed SPL-token portfolio insights.*
