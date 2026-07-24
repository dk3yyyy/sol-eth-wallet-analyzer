# 🚀 DK3Y Wallet Analyzer

An async Telegram bot that validates Solana and Ethereum addresses, reports native balances and USD values, and provides detailed SPL-token portfolio analytics for Solana wallets.

## ✨ Features

### 🎯 **Core Features**

- **Two-chain Support**: Analyze Solana and Ethereum addresses
- **Real-time Data**: Native-asset prices from CoinGecko and Solana token market data from DexScreener
- **Solana Portfolio Analytics**: SPL-token allocation, market metrics, and dust filtering
- **Interactive UI**: Pagination, progress indicators, explorer links

### 👑 **Admin Features**

- **User Tracking**: Auto-log new users with sequential numbering
- **Flexible Logging**: Private channel/group or direct messages
- **Statistics**: `/stats` command for user metrics and analytics
- **Milestone Alerts**: Notifications every 10 new users

## 🛠️ Quick Setup

### 1. **Prerequisites**

```bash
# Requirements
- Python 3.8+
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
| `ADMIN_CHAT_ID` | Your chat ID for admin access | ⚪ Optional |
| `LOG_CHANNEL_ID` | Channel/group ID for user logs | ⚪ Optional |

### **Bot Settings**

- **Cache**: 5 minutes for faster responses
- **Dust Filter**: $0.01 minimum token value  
- **Pagination**: 6 tokens per page
- **APIs**: Solana RPC, CoinGecko, DexScreener, Etherscan

## 🔒 Security & Performance

- ✅ Environment variables for secrets
- ✅ Smart caching system
- ✅ Rate limiting protection
- ✅ Graceful error handling
- ✅ Async processing for speed

## 🐛 Troubleshooting

**Common Issues:**

- `TELEGRAM_TOKEN not found` → Check `.env` file exists
- `ETHERSCAN_API_KEY not set` → Add API key to `.env`
- Slow responses → Normal for large portfolios, caching helps

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
