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
API_KEY = os.getenv("MEXC_KEY")
API_SEC = os.getenv("MEXC_SECRET")
POS_USDT = float(os.getenv("POSITION_USDT", "20"))
LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "5"))
ACCOUNT_TYPE = os.getenv("ACCOUNT_TYPE", "swap")  # "swap" for USDT-M perp

DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes", "on")
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "15"))
TP_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.0"))   # informational in DRY_RUN
SL_PCT = float(os.getenv("STOP_LOSS_PCT", "0.0"))     # informational in DRY_RUN

# Safety switches
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "true").lower() in ("1", "true", "yes", "on")
BAR_STALENESS_HOURS = int(os.getenv("BAR_STALENESS_HOURS", "48"))  # Ignore signals older than this

if not SECRET:
    raise RuntimeError("Missing TV_WEBHOOK_SECRET in .env")

# ---- EXCHANGE (public endpoints are fine even without keys) ----
exchange = ccxt.mexc({
    "apiKey": API_KEY or "",
    "secret": API_SEC or "",
    "enableRateLimit": True,
    "options": {"defaultType": ACCOUNT_TYPE},
})

# ---- Background Tasks ----
async def start_email_poller():
    """Start email polling in background"""
    try:
        from email_poller import poll_emails_loop
        logger.info("Starting email poller background task...")
        # Create background task
        task = asyncio.create_task(poll_emails_loop())
        # Don't await - let it run in background
        logger.info("Email poller started successfully")
    except Exception as e:
        logger.error(f"Failed to start email poller: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown"""
    # Startup
    logger.info("Application starting...")
    await start_email_poller()
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
SYMBOL_MAP = {
    "MEXC:ETHUSDT": "ETH/USDT:USDT",
    "MEXC:ETHUSDT.P": "ETH/USDT:USDT",  # TradingView sometimes adds .P suffix
    "ETHUSDT": "ETH/USDT:USDT",
    "MEXC:BTCUSDT": "BTC/USDT:USDT",
    "MEXC:BTCUSDT.P": "BTC/USDT:USDT",
    "BTCUSDT": "BTC/USDT:USDT",
    "MEXC:SOLUSDT": "SOL/USDT:USDT",
    "MEXC:SOLUSDT.P": "SOL/USDT:USDT",
    "SOLUSDT": "SOL/USDT:USDT",
}

# contract sizes for linear USDT-M perps
CONTRACT_SIZE = {
    "ETH/USDT:USDT": 0.01,   # 0.01 ETH per contract
    "BTC/USDT:USDT": 0.001,  # 0.001 BTC per contract
    "SOL/USDT:USDT": 0.1,    # 0.1 SOL per contract
}

def map_symbol(symbol_tv: str) -> str:
    return SYMBOL_MAP.get(symbol_tv, symbol_tv)

def contract_size_for(symbol: str) -> float:
    return CONTRACT_SIZE.get(symbol, 0.01)

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

def position_for(symbol: str) -> Dict[str, Any]:
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
    bar_ts = payload.get("bar_ts")
    
    if side not in ("long", "short") or not symbol_tv or not bar_ts:
        return {"status": "error", "message": "Missing required fields: side, symbol_tv, bar_ts"}
    
    # Validate secret if provided (for external calls)
    if payload.get("secret") and payload.get("secret") != SECRET:
        return {"status": "error", "message": "Invalid secret"}
    
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
    except Exception:
        pass
    
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
    return {"ok": True, "dry_run": DRY_RUN, "account_type": ACCOUNT_TYPE}

@app.get("/health")
def health():
    """Health check endpoint for monitoring"""
    return {
        "status": "healthy",
        "trading_enabled": TRADING_ENABLED,
        "dry_run": DRY_RUN,
        "account_type": ACCOUNT_TYPE,
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
