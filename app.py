import os, time, math
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request, HTTPException
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import ccxt
import asyncio
import logging

load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---- ENV ----
SECRET = os.getenv("TV_WEBHOOK_SECRET")
EXCHANGE_ID = os.getenv("EXCHANGE", "bybit").lower()  # bybit for Bybit USDT-M Futures
ACCOUNT_TYPE = os.getenv("ACCOUNT_TYPE", "linear")  # "linear" for Bybit USDT-M perps

# Exchange-specific API credentials
if EXCHANGE_ID == "bybit":
    API_KEY = os.getenv("BYBIT_KEY", "")
    API_SEC = os.getenv("BYBIT_SECRET", "")
elif EXCHANGE_ID == "mexc":
    API_KEY = os.getenv("MEXC_KEY", "")
    API_SEC = os.getenv("MEXC_SECRET", "")
    ACCOUNT_TYPE = os.getenv("ACCOUNT_TYPE", "swap")  # MEXC uses "swap"
elif EXCHANGE_ID == "binanceusdm":
    API_KEY = os.getenv("BINANCE_KEY", "")
    API_SEC = os.getenv("BINANCE_SECRET", "")
    ACCOUNT_TYPE = os.getenv("ACCOUNT_TYPE", "swap")  # Binance uses "swap"
else:
    raise RuntimeError(f"Unsupported exchange: {EXCHANGE_ID}")

POS_USDT = float(os.getenv("POSITION_USDT", "20"))
LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "5"))

DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes", "on")
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "15"))
TP_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.0"))   # informational in DRY_RUN
SL_PCT = float(os.getenv("STOP_LOSS_PCT", "0.0"))     # informational in DRY_RUN

# Safety switches
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "true").lower() in ("1", "true", "yes", "on")
BAR_STALENESS_HOURS = int(os.getenv("BAR_STALENESS_HOURS", "48"))  # Ignore signals older than this

if not SECRET:
    raise RuntimeError("Missing TV_WEBHOOK_SECRET in .env")

# ---- EXCHANGE (dynamic based on EXCHANGE env var) ----
# Initialize exchange based on EXCHANGE_ID
if EXCHANGE_ID == "bybit":
    exchange = ccxt.bybit({
        "apiKey": API_KEY or "",
        "secret": API_SEC or "",
        "enableRateLimit": True,
        "options": {"defaultType": ACCOUNT_TYPE},  # 'linear' for Bybit USDT-M
    })
elif EXCHANGE_ID == "mexc":
    exchange = ccxt.mexc({
        "apiKey": API_KEY or "",
        "secret": API_SEC or "",
        "enableRateLimit": True,
        "options": {"defaultType": ACCOUNT_TYPE},  # 'swap' for MEXC
    })
elif EXCHANGE_ID == "binanceusdm":
    exchange = ccxt.binanceusdm({
        "apiKey": API_KEY or "",
        "secret": API_SEC or "",
        "enableRateLimit": True,
        "options": {"defaultType": ACCOUNT_TYPE},  # 'swap' for Binance
    })
else:
    raise RuntimeError(f"Unsupported exchange: {EXCHANGE_ID}")

logger.info(f"Initialized exchange: {EXCHANGE_ID} (account_type={ACCOUNT_TYPE})")

# ---- Background Tasks ----
async def start_email_idle_listener():
    """Start IMAP IDLE listener in background thread"""
    try:
        import threading
        from email_idle import run_idle_forever
        logger.info("Starting IMAP IDLE listener background thread...")
        # Run IDLE in a daemon thread (blocking function)
        thread = threading.Thread(target=run_idle_forever, name="imap-idle", daemon=True)
        thread.start()
        logger.info("IMAP IDLE listener started successfully")
    except Exception as e:
        logger.error(f"Failed to start IMAP IDLE listener: {e}")

def sync_positions_from_exchange():
    """
    Sync STATE with actual exchange positions on startup.
    This ensures bot state matches reality after restarts.
    """
    try:
        logger.info("Syncing positions from exchange...")
        positions = exchange.fetch_positions()
        synced_count = 0
        for pos in positions:
            symbol = pos.get("symbol")
            if not symbol:
                continue
            size = float(pos.get("contracts", 0))
            if size != 0:  # Only sync non-zero positions
                if symbol not in STATE:
                    STATE[symbol] = {
                        "side": "flat",
                        "entry": None,
                        "size": 0,
                        "last_fill_ts": 0.0,
                        "cooldown_until": 0.0,
                    }
                if size > 0:
                    STATE[symbol]["side"] = "long"
                    STATE[symbol]["size"] = size
                elif size < 0:
                    STATE[symbol]["side"] = "short"
                    STATE[symbol]["size"] = abs(size)
                STATE[symbol]["entry"] = float(pos.get("entryPrice", 0))
                synced_count += 1
                logger.info(f"Synced position: {symbol} {STATE[symbol]['side']} size={STATE[symbol]['size']}")
        logger.info(f"Position sync complete: {synced_count} active positions found")
    except Exception as e:
        logger.warning(f"Failed to sync positions from exchange on startup: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown"""
    # Startup
    logger.info("Application starting...")
    sync_positions_from_exchange()  # Sync with exchange before starting
    await start_email_idle_listener()
    yield
    # Shutdown
    logger.info("Application shutting down...")

# ---- APP ----
app = FastAPI(lifespan=lifespan)

# ---- State & helpers ----
SEEN_KEYS = set()  # idempotency (bar_ts:symbol_tv:side)

# simple in-memory position state per symbol
STATE: Dict[str, Dict[str, Any]] = {}  # {symbol: {side, entry, size, last_fill_ts, cooldown_until}}

# TV -> CCXT symbol map (extend as needed)
# Bybit USDT-M perps on TradingView appear as BYBIT:SOLUSDT.P or BYBIT:SOLUSDT
SYMBOL_MAP = {
    # Bybit symbols (active)
    "BYBIT:ETHUSDT": "ETH/USDT:USDT",
    "BYBIT:ETHUSDT.P": "ETH/USDT:USDT",  # TradingView perpetual suffix
    "BYBIT:BTCUSDT": "BTC/USDT:USDT",
    "BYBIT:BTCUSDT.P": "BTC/USDT:USDT",
    "BYBIT:SOLUSDT": "SOL/USDT:USDT",
    "BYBIT:SOLUSDT.P": "SOL/USDT:USDT",
    "BYBIT:CRVUSDT": "CRV/USDT:USDT",
    "BYBIT:CRVUSDT.P": "CRV/USDT:USDT",
    # Generic symbols (no exchange prefix)
    "ETHUSDT": "ETH/USDT:USDT",
    "BTCUSDT": "BTC/USDT:USDT",
    "SOLUSDT": "SOL/USDT:USDT",
    "CRVUSDT": "CRV/USDT:USDT",
    # Binance symbols (commented out - can be re-enabled if needed)
    # "BINANCE:ETHUSDT": "ETH/USDT:USDT",
    # "BINANCE:ETHUSDT.P": "ETH/USDT:USDT",
    # "BINANCE:BTCUSDT": "BTC/USDT:USDT",
    # "BINANCE:BTCUSDT.P": "BTC/USDT:USDT",
    # "BINANCE:SOLUSDT": "SOL/USDT:USDT",
    # "BINANCE:SOLUSDT.P": "SOL/USDT:USDT",
    # MEXC symbols commented out (can be re-enabled if needed)
    # "MEXC:ETHUSDT": "ETH/USDT:USDT",
    # "MEXC:ETHUSDT.P": "ETH/USDT:USDT",
    # "MEXC:BTCUSDT": "BTC/USDT:USDT",
    # "MEXC:BTCUSDT.P": "BTC/USDT:USDT",
    # "MEXC:SOLUSDT": "SOL/USDT:USDT",
    # "MEXC:SOLUSDT.P": "SOL/USDT:USDT",
}

# Fallback contract sizes (used if exchange doesn't provide market data)
CONTRACT_SIZE_FALLBACK = {
    "ETH/USDT:USDT": 0.01,   # 0.01 ETH per contract
    "BTC/USDT:USDT": 0.001,  # 0.001 BTC per contract
    "SOL/USDT:USDT": 0.1,    # 0.1 SOL per contract
    "CRV/USDT:USDT": 1.0,    # 1.0 CRV per contract (will be fetched from exchange if available)
}

def map_symbol(symbol_tv: str) -> str:
    """
    Map TradingView symbol to CCXT symbol format.
    First tries static SYMBOL_MAP, then falls back to programmatic normalization.
    ChatGPT recommendation: Remove BYBIT: prefix and .P suffix programmatically.
    """
    # Try static map first
    if symbol_tv in SYMBOL_MAP:
        return SYMBOL_MAP[symbol_tv]
    
    # Programmatic normalization (ChatGPT recommendation)
    normalized = symbol_tv
    # Remove BYBIT: prefix
    if normalized.startswith("BYBIT:"):
        normalized = normalized[6:]  # Remove "BYBIT:"
    # Remove .P suffix (perpetual contract indicator)
    if normalized.endswith(".P"):
        normalized = normalized[:-2]  # Remove ".P"
    
    # If normalized symbol looks like a base symbol (e.g., "CRVUSDT"), convert to CCXT format
    if normalized.endswith("USDT") and "/" not in normalized:
        # Convert CRVUSDT -> CRV/USDT:USDT for Bybit linear perps
        base = normalized[:-4]  # Remove "USDT"
        return f"{base}/USDT:USDT"
    
    # Fallback: return as-is or try static map again with normalized
    return SYMBOL_MAP.get(normalized, normalized)

def contract_size_for(symbol: str) -> float:
    """
    Get contract size from exchange market data (dynamic).
    Falls back to hardcoded values if exchange doesn't provide it.
    """
    try:
        exchange.load_markets()
        m = exchange.market(symbol)
        cs = m.get("contractSize", None)
        if cs is not None:
            return float(cs)
    except Exception:
        pass
    # Fallback to hardcoded values
    return CONTRACT_SIZE_FALLBACK.get(symbol, 1.0)

def now_ts() -> float:
    return time.time()

def fmt_ts(ts: float | int | str) -> str:
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        return str(ts)
    except Exception:
        return str(ts)

def get_last_price(symbol: str) -> float:
    ticker = exchange.fetch_ticker(symbol)
    return float(ticker["last"])

def fetch_exchange_position(symbol: str) -> Dict[str, Any]:
    """
    Fetch actual position from exchange (source of truth).
    Returns: {side: "long"|"short"|"flat", size: float, entry: float|None}
    """
    try:
        positions = exchange.fetch_positions([symbol])
        if not positions:
            return {"side": "flat", "size": 0.0, "entry": None}
        
        # CCXT returns list of positions, find the one for our symbol
        for pos in positions:
            if pos.get("symbol") == symbol:
                size = float(pos.get("contracts", 0))
                if size > 0:
                    side = "long"
                    entry = float(pos.get("entryPrice", 0))
                elif size < 0:
                    side = "short"
                    entry = float(pos.get("entryPrice", 0))
                    size = abs(size)  # Store as positive
                else:
                    side = "flat"
                    entry = None
                return {"side": side, "size": size, "entry": entry}
        
        return {"side": "flat", "size": 0.0, "entry": None}
    except Exception as e:
        logger.warning(f"Failed to fetch exchange position for {symbol}: {e}")
        # Fallback to in-memory state if exchange fetch fails
        return None

def position_for(symbol: str) -> Dict[str, Any]:
    """
    Get position state, syncing with exchange if needed.
    Exchange is source of truth - sync STATE before making decisions.
    """
    # First, try to sync with exchange (source of truth)
    exchange_pos = fetch_exchange_position(symbol)
    
    if exchange_pos is not None:
        # Sync STATE with exchange
        if symbol not in STATE:
            STATE[symbol] = {
                "side": "flat",
                "entry": None,
                "size": 0,
                "last_fill_ts": 0.0,
                "cooldown_until": 0.0,
            }
        
        # Update STATE with exchange data
        STATE[symbol]["side"] = exchange_pos["side"]
        STATE[symbol]["size"] = exchange_pos["size"]
        STATE[symbol]["entry"] = exchange_pos["entry"]
    
    # Return STATE (now synced with exchange)
    if symbol not in STATE:
        STATE[symbol] = {
            "side": "flat",
            "entry": None,
            "size": 0,
            "last_fill_ts": 0.0,
            "cooldown_until": 0.0,
        }
    return STATE[symbol]

def in_cooldown(pos: Dict[str, Any]) -> bool:
    return now_ts() < float(pos.get("cooldown_until", 0.0))

def apply_cooldown(pos: Dict[str, Any]):
    pos["cooldown_until"] = now_ts() + COOLDOWN_SEC

def calc_contracts(symbol: str, usd_notional: float, price: float) -> int:
    cs = contract_size_for(symbol)
    # contracts = notional / (price * contractSize)
    raw = usd_notional / (price * cs)
    return max(1, int(math.floor(raw)))

def _parse_bar_ts(bar_ts: Any) -> Optional[datetime]:
    """
    Parse bar_ts into a timezone-aware datetime (UTC).
    Accepts:
    - ISO8601 strings (e.g. 2025-01-13T12:00:00Z)
    - Unix seconds (int/float or numeric string)
    - Unix milliseconds (int/float or numeric string)
    """
    try:
        # numeric (int/float)
        if isinstance(bar_ts, (int, float)):
            ts = float(bar_ts)
            # heuristic: milliseconds if large
            if ts > 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)

        if isinstance(bar_ts, str):
            s = bar_ts.strip()
            # numeric string
            if s.isdigit():
                ts = float(s)
                if ts > 1e12:
                    ts = ts / 1000.0
                return datetime.fromtimestamp(ts, tz=timezone.utc)

            # ISO string
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        return None
    except Exception:
        return None


def validate_bar_timestamp(bar_ts: Any) -> bool:
    """Check if bar timestamp is not too stale (within BAR_STALENESS_HOURS)."""
    dt = _parse_bar_ts(bar_ts)
    if not dt:
        return False
    age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return age_hours <= BAR_STALENESS_HOURS

def execute_order(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Core order execution logic extracted from /tv endpoint.
    Can be called from webhook or email processing.
    
    Args:
        payload: Dict with keys: side, symbol_tv, bar_ts, secret (optional for internal calls)
    
    Returns:
        Dict with order execution result
    """
    # Safety check
    if not TRADING_ENABLED:
        return {"status": "trading_disabled", "message": "TRADING_ENABLED is false"}
    
    # Validate payload
    side = payload.get("side")
    symbol_tv = payload.get("symbol_tv")
    
    # Support multiple timestamp formats: bar_ts, time, time_unix_ms
    bar_ts = payload.get("bar_ts") or payload.get("time")
    
    # If time_unix_ms is provided (milliseconds), convert to seconds for parsing
    if not bar_ts and payload.get("time_unix_ms"):
        try:
            ts_ms = float(payload.get("time_unix_ms"))
            bar_ts = ts_ms / 1000.0  # Convert milliseconds to seconds
        except Exception:
            pass
    
    if side not in ("long", "short") or not symbol_tv or not bar_ts:
        return {"status": "error", "message": f"Missing required fields: side={side}, symbol_tv={symbol_tv}, bar_ts={bar_ts}. Available keys: {list(payload.keys())}"}
    
    # Validate secret if provided (for external calls)
    # Reject empty secrets - ChatGPT recommendation: empty secret should fail validation
    secret = payload.get("secret")
    if secret is not None:  # If secret field exists (even if empty)
        if not secret:  # Empty string
            logger.warning(f"Rejected alert: empty secret. Symbol={symbol_tv}, Side={side}. Fix TradingView alert to include secret={SECRET}")
            return {"status": "error", "message": f"Empty secret rejected. TradingView alert must include secret field with value: {SECRET}"}
        if secret != SECRET:  # Wrong secret
            logger.warning(f"Rejected alert: invalid secret. Symbol={symbol_tv}, Side={side}")
            return {"status": "error", "message": f"Invalid secret (expected: {SECRET[:10]}...)"}
    
    # Check bar staleness
    if not validate_bar_timestamp(bar_ts):
        return {"status": "stale_signal", "message": f"Bar timestamp is older than {BAR_STALENESS_HOURS} hours"}
    
    # Idempotency check
    dedupe_key = f"{bar_ts}:{symbol_tv}:{side}"
    if dedupe_key in SEEN_KEYS:
        return {"status": "duplicate_ignored"}
    SEEN_KEYS.add(dedupe_key)
    
    symbol = map_symbol(symbol_tv)
    
    # Load markets and set leverage
    try:
        exchange.load_markets()
    except Exception:
        pass
    try:
        exchange.set_leverage(LEVERAGE, symbol)
    except Exception as e:
        logger.warning(f"set_leverage failed for {symbol}: {e}")
    
    # Get current price
    try:
        last = get_last_price(symbol)
    except Exception as e:
        return {"status": "error", "message": f"Failed to fetch price for {symbol}: {e}"}
    
    # Position state & cooldown
    pos = position_for(symbol)
    if in_cooldown(pos):
        return {"status": "cooldown", "until": fmt_ts(pos["cooldown_until"]), "symbol": symbol}
    
    # If already in that side, ignore
    if pos["side"] == side:
        return {"status": "already_in_position", "symbol": symbol, "side": side}
    
    # If in opposite side, close it first
    flipped_from = None
    if pos["side"] in ("long", "short") and pos["side"] != side:
        flipped_from = pos["side"]
        # In DRY_RUN, we just log; in LIVE we'll send reduce-only close
        if not DRY_RUN and pos["size"] > 0:
            try:
                # Close existing position with reduce-only order
                if flipped_from == "long":
                    exchange.create_market_sell_order(symbol, pos["size"], params={"reduceOnly": True})
                else:
                    exchange.create_market_buy_order(symbol, pos["size"], params={"reduceOnly": True})
            except Exception as e:
                # Log error but continue with new position
                pass
        pos["side"] = "flat"
        pos["entry"] = None
        pos["size"] = 0
    
    # Calculate contract size
    contracts = calc_contracts(symbol, POS_USDT, last)
    
    # Calculate TP/SL levels
    if TP_PCT > 0:
        tp = last * (1 + TP_PCT if side == "long" else 1 - TP_PCT)
    else:
        tp = None
    if SL_PCT > 0:
        sl = last * (1 - SL_PCT if side == "long" else 1 + SL_PCT)
    else:
        sl = None
    
    # Place order
    if DRY_RUN:
        order_id = f"sim-{symbol}-{side}"
        pos["side"] = side
        pos["entry"] = last
        pos["size"] = contracts
        pos["last_fill_ts"] = now_ts()
        apply_cooldown(pos)
        
        return {
            "status": "simulated_ok",
            "symbol": symbol,
            "side": side,
            "amount_sent": contracts,
            "contracts_mode": True,
            "contractSize": contract_size_for(symbol),
            "price_used": last,
            "order_id": order_id,
            "flipped_from": flipped_from,
            "tp": tp,
            "sl": sl,
        }
    
    # LIVE trading path
    try:
        if side == "long":
            order = exchange.create_market_buy_order(symbol, contracts)
        else:
            order = exchange.create_market_sell_order(symbol, contracts)
        
        order_id = order.get("id")
        
        # Update state
        pos["side"] = side
        pos["entry"] = last
        pos["size"] = contracts
        pos["last_fill_ts"] = now_ts()
        apply_cooldown(pos)
        
        return {
            "status": "ok",
            "symbol": symbol,
            "side": side,
            "amount_sent": contracts,
            "contracts_mode": True,
            "contractSize": contract_size_for(symbol),
            "price_used": last,
            "order_id": order_id,
            "flipped_from": flipped_from,
            "tp": tp,
            "sl": sl,
        }
    except Exception as e:
        return {"status": "error", "message": f"Order error: {e}"}

# ---- Routes ----

@app.get("/")
def root():
    return {"ok": True, "dry_run": DRY_RUN, "account_type": ACCOUNT_TYPE, "exchange": EXCHANGE_ID}

@app.get("/health")
def health():
    """Health check endpoint for monitoring"""
    return {
        "status": "healthy",
        "trading_enabled": TRADING_ENABLED,
        "dry_run": DRY_RUN,
        "account_type": ACCOUNT_TYPE,
        "exchange": EXCHANGE_ID,
    }

@app.get("/state")
def state():
    return {"state": STATE, "seen_keys": len(SEEN_KEYS)}

@app.post("/tv")
async def tv(req: Request):
    """Webhook endpoint for TradingView alerts (direct POST)"""
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    if payload.get("secret") != SECRET:
        raise HTTPException(403, "Bad secret")
    
    # Use extracted execute_order function
    result = execute_order(payload)
    
    # Convert error status to HTTP exceptions for webhook compatibility
    if result.get("status") == "error":
        raise HTTPException(500, result.get("message", "Order execution failed"))
    if result.get("status") == "trading_disabled":
        raise HTTPException(503, "Trading is disabled")
    
    return result
