# TV-MEXC Trading Bot

An automated trading bot that receives TradingView webhook alerts and executes futures orders on MEXC exchange.

## Overview

This bot acts as a bridge between TradingView alerts and MEXC futures trading:

- Receives webhook POST requests from TradingView Pine Script alerts
- Validates and processes trading signals (long/short)
- Places market orders on MEXC futures exchange
- Supports both DRY_RUN (simulation) and LIVE trading modes

## Features

- ✅ **Webhook Integration**: Receives TradingView alerts via HTTP POST
- ✅ **Idempotency**: Prevents duplicate orders using bar timestamp + symbol + side
- ✅ **Cooldown Mechanism**: Configurable cooldown period between orders
- ✅ **Position Flipping**: Automatically closes opposite positions before opening new ones
- ✅ **Contract Sizing**: Calculates contract size based on USDT position size
- ✅ **Symbol Mapping**: Maps TradingView symbols to CCXT format
- ✅ **DRY_RUN Mode**: Test without real money
- ✅ **Leverage Support**: Configurable leverage for futures trading

## Prerequisites

- Python 3.8+
- MEXC account with API keys (for live trading)
- TradingView account with webhook access (or alternative setup)
- Server/VPS or local machine with public URL (for webhooks)

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

# MEXC API (required for live trading)
MEXC_KEY=your_mexc_api_key
MEXC_SECRET=your_mexc_secret

# Trading Configuration
POSITION_USDT=20              # Position size in USDT
DEFAULT_LEVERAGE=5            # Leverage multiplier
ACCOUNT_TYPE=swap             # "swap" for USDT-M futures

# Risk Management
COOLDOWN_SEC=15              # Seconds between orders
TAKE_PROFIT_PCT=0.0          # Take profit percentage (0 = disabled)
STOP_LOSS_PCT=0.0            # Stop loss percentage (0 = disabled)

# Mode
DRY_RUN=true                  # Set to "false" for real trading
```

### Environment Variables Explained

- **TV_WEBHOOK_SECRET**: Secret key to authenticate TradingView webhook requests
- **MEXC_KEY / MEXC_SECRET**: API credentials from MEXC (Futures Trading permission required)
- **POSITION_USDT**: Notional position size in USDT (e.g., 20 = $20 position)
- **DEFAULT_LEVERAGE**: Leverage multiplier (e.g., 5 = 5x leverage)
- **ACCOUNT_TYPE**: Account type, use "swap" for USDT-M perpetual futures
- **COOLDOWN_SEC**: Minimum seconds between orders for the same symbol
- **DRY_RUN**: When `true`, simulates orders without real money

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
   alert_message = '{"secret":"your_secret","side":"long","symbol_tv":"MEXC:ETHUSDT","bar_ts":"' + str.tostring(time) + '"}'
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
  "symbol_tv": "MEXC:ETHUSDT",
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
| `MEXC:ETHUSDT`     | `ETH/USDT:USDT` |
| `ETHUSDT`          | `ETH/USDT:USDT` |
| `MEXC:BTCUSDT`     | `BTC/USDT:USDT` |
| `BTCUSDT`          | `BTC/USDT:USDT` |

To add more symbols, edit the `SYMBOL_MAP` dictionary in `app.py`.

## Contract Sizing

Contract sizes are defined per symbol. Current defaults:

- ETH: 0.01 ETH per contract
- BTC: 0.001 BTC per contract

The bot calculates number of contracts based on:

```
contracts = POSITION_USDT / (price * contract_size)
```

## How It Works

1. **TradingView Alert** → Sends POST request to `/tv` endpoint
2. **Validation** → Checks secret, validates payload
3. **Idempotency Check** → Prevents duplicate orders using `bar_ts:symbol:side`
4. **Cooldown Check** → Rejects if within cooldown period
5. **Position Check** → Ignores if already in same position
6. **Position Flipping** → Closes opposite position if exists (DRY_RUN only simulates)
7. **Order Execution** → Places market order on MEXC
8. **State Update** → Updates in-memory position state

## Current Limitations

⚠️ **Important Notes:**

- **In-Memory State**: Position state is lost on server restart
- **No Position Sync**: Doesn't fetch actual positions from MEXC
- **TP/SL Not Placed**: Take profit and stop loss are calculated but not placed as orders
- **No Error Recovery**: Network failures may require manual intervention
- **Position Closing**: In LIVE mode, opposite positions are not automatically closed (only in DRY_RUN)

## Testing

### Test with DRY_RUN

```bash
curl -X POST http://localhost:8000/tv \
  -H "Content-Type: application/json" \
  -d '{
    "secret": "your_secret",
    "side": "long",
    "symbol_tv": "MEXC:ETHUSDT",
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
- ✅ Restrict MEXC API permissions (Futures Trading only)
- ✅ Use HTTPS in production
- ✅ Consider IP whitelisting if possible
- ✅ Monitor logs for suspicious activity

## Troubleshooting

**Order fails:**

- Check MEXC API keys are correct
- Verify account has sufficient balance
- Check leverage settings match account limits
- Review MEXC API error messages

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
├── app.py              # Main FastAPI application
├── requirements.txt    # Python dependencies
├── .env               # Environment variables (not in git)
├── README.md          # This file
└── venv/              # Virtual environment
```

## License

[Your License Here]

## Disclaimer

⚠️ **Trading cryptocurrencies involves substantial risk of loss. This bot is provided as-is without any warranties. Use at your own risk. Always test thoroughly with small amounts before deploying with real money.**
