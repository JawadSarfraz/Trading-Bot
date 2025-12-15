# TV-Bybit Trading Bot

An automated trading bot that receives TradingView alerts (via email or webhook) and executes futures orders on Bybit USDT-M Futures exchange.

## Overview

This bot acts as a bridge between TradingView alerts and Bybit USDT-M Futures trading:

- **Email Integration**: Polls Gmail for TradingView email alerts (works with free TradingView accounts)
- **Webhook Support**: Also supports direct webhook POST requests from TradingView
- Validates and processes trading signals (long/short)
- Places market orders on Bybit USDT-M Futures exchange
- Supports both DRY_RUN (simulation) and LIVE trading modes

## Features

- ✅ **Email Integration**: Polls Gmail IMAP for TradingView email alerts (every 10-15 minutes)
- ✅ **Webhook Integration**: Receives TradingView alerts via HTTP POST (alternative method)
- ✅ **Idempotency**: Prevents duplicate orders using email Message-ID + bar timestamp + symbol + side
- ✅ **Cooldown Mechanism**: Configurable cooldown period between orders
- ✅ **Position Flipping**: Automatically closes opposite positions before opening new ones (with reduce-only orders)
- ✅ **Contract Sizing**: Calculates contract size based on USDT position size
- ✅ **Symbol Mapping**: Maps TradingView symbols to CCXT format (ETH, BTC, SOL, etc.)
- ✅ **DRY_RUN Mode**: Test without real money
- ✅ **Safety Features**: Trading kill switch, bar staleness validation, position management
- ✅ **Leverage Support**: Configurable leverage for futures trading
- ✅ **Persistence**: SQLite database tracks processed emails to prevent duplicates

## Prerequisites

- Python 3.8+
- Bybit account with Unified Trading Account (USDT-M Futures) enabled and API keys (for live trading)
- TradingView account (free plan works with email alerts)
- Gmail account with IMAP enabled (for email-based alerts)
- VPS/Server for deployment (recommended) or Cloud Run

## Installation

1. **Clone the repository**

   ```bash
   git clone <your-repo-url>
   cd tv-mexc-bot
   ```

2. **Create virtual environment**

   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

4. **Create `.env` file**
   ```bash
   cp .env.example .env  # If you have an example file
   # Or create .env manually
   ```

## Configuration

Create a `.env` file in the project root with the following variables:

```bash
# Required
TV_WEBHOOK_SECRET=your_secret_key_here

# Exchange Configuration
EXCHANGE=bybit                    # Exchange ID (bybit for Bybit USDT-M Futures)
ACCOUNT_TYPE=linear               # "linear" for Bybit USDT-M perps

# Bybit API (required for live trading)
BYBIT_KEY=your_bybit_api_key
BYBIT_SECRET=your_bybit_secret

# Trading Configuration
POSITION_USDT=20              # Position size in USDT
DEFAULT_LEVERAGE=5            # Leverage multiplier
ACCOUNT_TYPE=swap             # "swap" for USDT-M futures

# Risk Management
COOLDOWN_SEC=15               # Seconds between orders
TAKE_PROFIT_PCT=0.0          # Take profit percentage (0 = disabled)
STOP_LOSS_PCT=0.0            # Stop loss percentage (0 = disabled)
BAR_STALENESS_HOURS=48       # Ignore signals older than this (hours)

# Safety Switches
TRADING_ENABLED=true          # Kill switch: set to false to disable all trading
DRY_RUN=true                  # Set to "false" for real trading

# Email Configuration (for email-based alerts)
IMAP_HOST=imap.gmail.com
IMAP_USER=your_email@gmail.com
IMAP_PASSWORD=your_gmail_app_password  # Gmail App Password (not regular password)
IMAP_LABEL=tv-alerts          # Gmail label for TradingView emails
IMAP_FAILED_LABEL=tv-alerts-failed  # Label for failed email parsing

# Email Polling
POLL_INTERVAL_SEC=600         # Poll every 10 minutes (600 seconds)

# Persistence
PERSISTENCE_DB_PATH=processed_emails.db  # SQLite database path
PRUNE_DAYS=30                 # Keep processed email records for 30 days
```

### Bybit API Setup

**Important:** Before using the bot with Bybit, you must:

1. **Activate Bybit Unified Trading Account:**

   - Go to Bybit → Derivatives → USDT-M Futures
   - Complete Unified Trading Account activation if not already done
   - Fund your USDT-M Futures wallet (Unified Trading balance)

2. **Create API Key with Futures Permissions:**

   - Go to Bybit → Account & Security → API Management → Create New Key
   - **Enable "Read" and "Trade"** permissions for Derivatives
   - **Keep "Withdrawals" disabled** for security
   - (Recommended) Set IP whitelist to your server's IP address
   - Save the API key and secret (secret is shown only once)

3. **Verify API Key:**
   - The key must have Derivatives trading permissions
   - Test with a small position first (`POSITION_USDT=5`)
   - Ensure you're using one-way mode (not hedge mode)

### Environment Variables Explained

**Trading Configuration:**

- **TV_WEBHOOK_SECRET**: Secret key to authenticate TradingView alerts
- **EXCHANGE**: Exchange ID (default: `bybit` for Bybit USDT-M Futures)
- **ACCOUNT_TYPE**: Account type (default: `linear` for Bybit USDT-M perps)
- **BYBIT_KEY / BYBIT_SECRET**: API credentials from Bybit (Derivatives Trading permission required)
- **POSITION_USDT**: Notional position size in USDT (e.g., 20 = $20 position)
- **DEFAULT_LEVERAGE**: Leverage multiplier (e.g., 5 = 5x leverage)
- **ACCOUNT_TYPE**: Account type, use "linear" for Bybit USDT-M perpetual futures
- **COOLDOWN_SEC**: Minimum seconds between orders for the same symbol
- **BAR_STALENESS_HOURS**: Ignore signals older than this (default: 48 hours)

**Safety:**

- **TRADING_ENABLED**: Kill switch - set to `false` to disable all trading
- **DRY_RUN**: When `true`, simulates orders without real money

**Email Configuration:**

- **IMAP_HOST**: IMAP server (default: imap.gmail.com)
- **IMAP_USER**: Gmail address
- **IMAP_PASSWORD**: Gmail App Password (create in Google Account settings)
- **IMAP_LABEL**: Gmail label for TradingView emails (create filter to auto-label)
- **IMAP_FAILED_LABEL**: Label for emails that failed to parse
- **POLL_INTERVAL_SEC**: How often to check for new emails (default: 600 = 10 minutes)

## Usage

### Start the Server

```bash
# Activate virtual environment
source venv/bin/activate

# Run the server
uvicorn app:app --host 0.0.0.0 --port 8000

# Or with auto-reload for development
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

### Expose to Internet (for TradingView webhooks)

**Option 1: ngrok (for testing)**

```bash
ngrok http 8000
# Use the provided HTTPS URL in TradingView webhook
```

**Option 2: Deploy to VPS/Cloud**

- Deploy to a server with public IP
- Use nginx reverse proxy with SSL
- Or use services like Railway, Render, Fly.io

### Configure TradingView Alert

1. In your TradingView Pine Script, add webhook alert:

   ```pine
   // Example alert message format
   alert_message = '{"secret":"your_secret","side":"long","symbol_tv":"BYBIT:ETHUSDT.P","bar_ts":"' + str.tostring(time) + '"}'
   ```

2. In TradingView alert settings:

   - **Webhook URL**: `https://your-server.com/tv` (or ngrok URL)
   - **Message**: JSON payload with `secret`, `side`, `symbol_tv`, `bar_ts`

3. Alert conditions should trigger on bar close to avoid repaint

## API Endpoints

### `GET /`

Health check endpoint.

**Response:**

```json
{
  "ok": true,
  "dry_run": true,
  "account_type": "swap"
}
```

### `GET /state`

Get current position state and statistics.

**Response:**

```json
{
  "state": {
    "ETH/USDT:USDT": {
      "side": "long",
      "entry": 2500.0,
      "size": 4,
      "last_fill_ts": 1234567890.0,
      "cooldown_until": 1234567905.0
    }
  },
  "seen_keys": 42
}
```

### `POST /tv`

Main webhook endpoint for TradingView alerts.

**Request Body:**

```json
{
  "secret": "your_secret",
  "side": "long",
  "symbol_tv": "BYBIT:ETHUSDT.P",
  "bar_ts": "2025-01-13T12:00:00Z"
}
```

**Response (DRY_RUN):**

```json
{
  "status": "simulated_ok",
  "symbol": "ETH/USDT:USDT",
  "side": "long",
  "amount_sent": 4,
  "contracts_mode": true,
  "contractSize": 0.01,
  "price_used": 2500.0,
  "order_id": "sim-ETH/USDT:USDT-long",
  "flipped_from": null,
  "tp": null,
  "sl": null
}
```

**Response (LIVE):**

```json
{
  "status": "ok",
  "symbol": "ETH/USDT:USDT",
  "side": "long",
  "amount_sent": 4,
  "contracts_mode": true,
  "contractSize": 0.01,
  "price_used": 2500.0,
  "order_id": "12345678",
  "flipped_from": null,
  "tp": null,
  "sl": null
}
```

**Possible Status Values:**

- `simulated_ok` - Order simulated (DRY_RUN mode)
- `ok` - Order placed successfully (LIVE mode)
- `duplicate_ignored` - Duplicate request (same bar_ts + symbol + side)
- `cooldown` - Order rejected due to cooldown period
- `already_in_position` - Already in the requested position side

## Symbol Mapping

The bot maps TradingView symbols to CCXT format. Currently supported:

| TradingView Format | CCXT Format     |
| ------------------ | --------------- |
| `BYBIT:ETHUSDT`    | `ETH/USDT:USDT` |
| `BYBIT:ETHUSDT.P`  | `ETH/USDT:USDT` |
| `BYBIT:BTCUSDT`    | `BTC/USDT:USDT` |
| `BYBIT:BTCUSDT.P`  | `BTC/USDT:USDT` |
| `BYBIT:SOLUSDT`    | `SOL/USDT:USDT` |
| `BYBIT:SOLUSDT.P`  | `SOL/USDT:USDT` |
| `ETHUSDT`          | `ETH/USDT:USDT` |
| `BTCUSDT`          | `BTC/USDT:USDT` |
| `SOLUSDT`          | `SOL/USDT:USDT` |

**Note:** TradingView perpetual contracts appear as `BYBIT:SOLUSDT.P` (with `.P` suffix). The bot supports both formats.

To add more symbols, edit the `SYMBOL_MAP` dictionary in `app.py`.

## Contract Sizing

Contract sizes are defined per symbol. Current defaults:

- ETH: 0.01 ETH per contract
- BTC: 0.001 BTC per contract
- SOL: 0.1 SOL per contract

The bot calculates number of contracts based on:

```
contracts = POSITION_USDT / (price * contract_size)
```

## How It Works

### Email-Based Flow (Recommended)

1. **TradingView Alert** → Sends email to Gmail with JSON payload
2. **Email Polling** → Background service polls Gmail every 10-15 minutes
3. **Email Parsing** → Extracts JSON from email body
4. **Idempotency Check** → Checks email Message-ID in SQLite database
5. **Validation** → Validates secret, bar timestamp (staleness check), symbol, side
6. **Cooldown Check** → Rejects if within cooldown period
7. **Position Check** → Ignores if already in same position
8. **Position Flipping** → Closes opposite position with reduce-only order (LIVE mode)
9. **Order Execution** → Places market order on Bybit USDT-M Futures
10. **State Update** → Updates in-memory position state and marks email as processed

### Webhook-Based Flow (Alternative)

1. **TradingView Alert** → Sends POST request to `/tv` endpoint
2. **Validation** → Checks secret, validates payload
3. **Idempotency Check** → Prevents duplicate orders using `bar_ts:symbol:side`
4. **Cooldown Check** → Rejects if within cooldown period
5. **Position Check** → Ignores if already in same position
6. **Position Flipping** → Closes opposite position if exists
7. **Order Execution** → Places market order on Bybit USDT-M Futures
8. **State Update** → Updates in-memory position state

## Deployment

### VPS Deployment (Recommended)

1. **Choose VPS Provider:**

   - Hetzner, DigitalOcean, Linode, or AWS Lightsail (~$5-10/month)

2. **Setup Server:**

   ```bash
   # Install Docker
   curl -fsSL https://get.docker.com -o get-docker.sh
   sh get-docker.sh

   # Clone repository
   git clone <your-repo-url>
   cd tv-mexc-bot

   # Create .env file with all configuration
   nano .env

   # Build and run Docker container
   docker build -t tv-mexc-bot .
   docker run -d \
     --name tv-bot \
     --restart unless-stopped \
     -p 8000:8080 \
     -v $(pwd)/processed_emails.db:/app/processed_emails.db \
     --env-file .env \
     tv-mexc-bot
   ```

3. **Systemd Service (Optional):**
   Create `/etc/systemd/system/tv-bot.service`:

   ```ini
   [Unit]
   Description=TV-Bybit Trading Bot
   After=docker.service
   Requires=docker.service

   [Service]
   Type=simple
   Restart=always
   ExecStart=/usr/bin/docker start -a tv-bot
   ExecStop=/usr/bin/docker stop tv-bot

   [Install]
   WantedBy=multi-user.target
   ```

   Enable and start:

   ```bash
   sudo systemctl enable tv-bot
   sudo systemctl start tv-bot
   ```

4. **Mount Persistence Volume:**
   - Mount `processed_emails.db` to host filesystem so it persists across container restarts
   - Example: `-v /opt/tv-bot/data:/app/data`

### Cloud Run Deployment (Alternative)

1. Build and push Docker image to Google Container Registry
2. Deploy to Cloud Run with environment variables
3. Set up Cloud Scheduler to call `/poll-email` endpoint (not needed with background poller)
4. Mount persistent volume for SQLite database

## Current Limitations

⚠️ **Important Notes:**

- **In-Memory State**: Position state is lost on server restart (email tracking persists)
- **No Position Sync**: Doesn't fetch actual positions from Bybit on startup
- **TP/SL Not Placed**: Take profit and stop loss are calculated but not placed as orders
- **Email Polling Delay**: 10-15 minute polling interval (acceptable for 1D timeframe signals)

## Testing

### Test with DRY_RUN

```bash
curl -X POST http://localhost:8000/tv \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "your_secret",
    "side": "long",
    "symbol_tv": "BYBIT:ETHUSDT.P",
    "bar_ts": "2025-01-13T12:00:00Z"
  }'
```

### Check State

```bash
curl http://localhost:8000/state
```

## Security Considerations

- ✅ Never commit `.env` file to git
- ✅ Use strong `TV_WEBHOOK_SECRET`
- ✅ Restrict Bybit API permissions (Derivatives Trading only, keep Withdrawals disabled)
- ✅ Use HTTPS in production
- ✅ Consider IP whitelisting if possible
- ✅ Monitor logs for suspicious activity

## Troubleshooting

**Order fails:**

- Check Bybit API keys are correct and have Derivatives trading permissions enabled
- Verify Unified Trading Account is activated and has sufficient balance
- Check leverage settings match account limits
- Review Bybit API error messages
- Ensure IP whitelist (if enabled) includes your server IP
- Ensure you're using one-way mode (not hedge mode) in Bybit

**Webhook not received:**

- Verify server is accessible from internet
- Check TradingView alert is configured correctly
- Verify secret matches in both places
- Check server logs for incoming requests

**Duplicate orders:**

- Check `bar_ts` is unique per bar
- Verify idempotency is working (check `/state` endpoint)

## Files Structure

```
tv-mexc-bot/
├── app.py                 # Main FastAPI application + order execution logic
├── email_service.py       # IMAP email fetching and parsing
├── email_poller.py        # Background email polling service
├── persistence.py          # SQLite database for processed email tracking
├── requirements.txt        # Python dependencies
├── Dockerfile             # Docker container configuration
├── .env                   # Environment variables (not in git)
├── processed_emails.db    # SQLite database (created at runtime)
├── README.md              # This file
└── venv/                  # Virtual environment
```

## License

[Your License Here]

## Disclaimer

⚠️ **Trading cryptocurrencies involves substantial risk of loss. This bot is provided as-is without any warranties. Use at your own risk. Always test thoroughly with small amounts before deploying with real money.**
