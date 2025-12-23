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
ENABLE_IMAP = os.getenv("ENABLE_IMAP", "false").lower() in ("1", "true", "yes", "on")

async def start_email_idle_listener():
    """Start IMAP IDLE listener in background thread (P2: optional via ENABLE_IMAP)"""
    if not ENABLE_IMAP:
        logger.info("IMAP IDLE listener disabled (ENABLE_IMAP=false). Using webhooks only.")
        return
    
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

def place_tp_sl_orders(symbol: str, side: str, contracts: int, tp: Optional[float], sl: Optional[float]) -> Dict[str, Any]:
    """
    P0: Place TP/SL orders on Bybit exchange (reduce-only, exchange-native).
    Uses Bybit's conditional orders API via CCXT.
    Returns dict with tp_order_id and sl_order_id (or None if not placed).
    """
    result = {"tp_order_id": None, "sl_order_id": None, "tp_error": None, "sl_error": None}
    
    if DRY_RUN:
        logger.info(f"[DRY_RUN] Would place TP={tp}, SL={sl} for {symbol} ({side}, {contracts} contracts)")
        return result
    
    if not tp and not sl:
        logger.info(f"No TP/SL to place for {symbol}")
        return result
    
    try:
        # Bybit conditional orders: use create_order with specific params
        # TP: Conditional limit order (reduce-only)
        # SL: Conditional stop-market order (reduce-only)
        
        if tp:
            try:
                # For long: TP is above entry, sell limit at tp price
                # For short: TP is below entry, buy limit at tp price
                if side == "long":
                    # Take profit: conditional limit sell order at tp price
                    tp_order = exchange.create_order(
                        symbol,
                        "limit",
                        "sell",
                        contracts,
                        tp,
                        params={
                            "reduceOnly": True,
                            "timeInForce": "GTC",  # Good Till Cancel
                        }
                    )
                else:  # short
                    # Take profit: conditional limit buy order at tp price
                    tp_order = exchange.create_order(
                        symbol,
                        "limit",
                        "buy",
                        contracts,
                        tp,
                        params={
                            "reduceOnly": True,
                            "timeInForce": "GTC",
                        }
                    )
                result["tp_order_id"] = tp_order.get("id")
                logger.info(f"Placed TP order {result['tp_order_id']} at {tp} for {symbol}")
            except Exception as e:
                result["tp_error"] = str(e)
                logger.error(f"Failed to place TP order for {symbol}: {e}")
        
        if sl:
            try:
                # Get current price to determine triggerDirection
                try:
                    current_price = get_last_price(symbol)
                except Exception:
                    current_price = last  # Fallback to entry price
                
                # Bybit triggerDirection logic:
                # "ascending" = trigger price is ABOVE current/mark price
                # "descending" = trigger price is BELOW current/mark price
                
                if side == "long":
                    # Long SL: SL is below entry/current → trigger when price FALLS to SL
                    # Since SL < current_price, use "descending"
                    trigger_direction = "descending" if sl < current_price else "ascending"
                    sl_order = exchange.create_order(
                        symbol,
                        "stop",
                        "sell",
                        contracts,
                        None,  # stop order, price comes from stopPrice
                        params={
                            "reduceOnly": True,
                            "stopPrice": sl,  # Trigger price for stop order
                            "triggerDirection": trigger_direction,  # "descending" when SL below current
                        }
                    )
                else:  # short
                    # Short SL: SL is above entry/current → trigger when price RISES to SL
                    # Since SL > current_price, use "ascending"
                    trigger_direction = "ascending" if sl > current_price else "descending"
                    sl_order = exchange.create_order(
                        symbol,
                        "stop",
                        "buy",
                        contracts,
                        None,
                        params={
                            "reduceOnly": True,
                            "stopPrice": sl,
                            "triggerDirection": trigger_direction,  # "ascending" when SL above current
                        }
                    )
                result["sl_order_id"] = sl_order.get("id")
                logger.info(f"Placed SL order {result['sl_order_id']} at {sl} for {symbol} (triggerDirection={trigger_direction}, current={current_price})")
            except Exception as e:
                result["sl_error"] = str(e)
                logger.error(f"Failed to place SL order for {symbol}: {e}")
        
    except Exception as e:
        logger.error(f"Error placing TP/SL orders for {symbol}: {e}")
        result["tp_error"] = str(e)
        result["sl_error"] = str(e)
    
    return result

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
    
    # P1: Persistent idempotency check (survives restarts)
    # Key format: exchange_symbol_side_timeframe_time_unix_ms
    timeframe = payload.get("timeframe", "unknown")
    time_unix_ms = payload.get("time_unix_ms") or str(int(float(bar_ts) * 1000))
    dedupe_key = f"{EXCHANGE_ID}_{symbol_tv}_{side}_{timeframe}_{time_unix_ms}"
    
    # Check in-memory cache first (fast path)
    if dedupe_key in SEEN_KEYS:
        return {"status": "duplicate_ignored", "message": "Signal already processed (in-memory)"}
    
    # Check persistent storage (SQLite)
    from persistence import is_signal_processed, mark_signal_processed
    if is_signal_processed(dedupe_key):
        logger.info(f"Duplicate signal detected: {dedupe_key}")
        return {"status": "duplicate_ignored", "message": "Signal already processed (persistent)"}
    
    # Mark as seen (will mark as processed after successful execution)
    SEEN_KEYS.add(dedupe_key)
    
    symbol = map_symbol(symbol_tv)
    
    # P1: Use TradingView payload values (tp, sl, notional, leverage, margin_mode)
    # Priority: payload > env vars > defaults
    notional = payload.get("notional")
    if notional is not None:
        try:
            notional = float(notional)
        except (ValueError, TypeError):
            notional = POS_USDT
    else:
        notional = POS_USDT
    
    leverage = payload.get("leverage")
    if leverage is not None:
        try:
            leverage = int(leverage)
        except (ValueError, TypeError):
            leverage = LEVERAGE
    else:
        leverage = LEVERAGE
    
    margin_mode = payload.get("margin_mode", "isolated")  # Default to isolated for safety
    
    # Load markets and set leverage/margin mode
    try:
        exchange.load_markets()
    except Exception:
        pass
    
    # Set leverage (from payload or env)
    try:
        exchange.set_leverage(leverage, symbol)
        logger.info(f"Set leverage to {leverage}x for {symbol}")
    except Exception as e:
        logger.warning(f"set_leverage failed for {symbol}: {e}")
    
    # P2: Enforce margin mode explicitly
    try:
        if EXCHANGE_ID == "bybit":
            # Bybit uses set_margin_mode via params
            exchange.set_margin_mode(margin_mode, symbol)
            logger.info(f"Set margin mode to {margin_mode} for {symbol}")
    except Exception as e:
        logger.warning(f"set_margin_mode failed for {symbol}: {e} (may not be supported)")
    
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
                logger.warning(f"Failed to close opposite position: {e}")
        pos["side"] = "flat"
        pos["entry"] = None
        pos["size"] = 0
    
    # Calculate contract size using payload notional or env var
    contracts = calc_contracts(symbol, notional, last)
    
    # P0: Calculate TP/SL levels - Priority: payload absolute prices > payload pct > env pct
    tp = None
    sl = None
    
    # First, try absolute prices from payload
    if payload.get("tp") is not None:
        try:
            tp = float(payload.get("tp"))
            logger.info(f"Using TP from payload: {tp}")
        except (ValueError, TypeError):
            pass
    
    if payload.get("sl") is not None:
        try:
            sl = float(payload.get("sl"))
            logger.info(f"Using SL from payload: {sl}")
        except (ValueError, TypeError):
            pass
    
    # If not in payload, try percentage from payload
    if tp is None and payload.get("tp_pct") is not None:
        try:
            tp_pct = float(payload.get("tp_pct"))
            tp = last * (1 + tp_pct if side == "long" else 1 - tp_pct)
            logger.info(f"Computed TP from payload tp_pct: {tp}")
        except (ValueError, TypeError):
            pass
    
    if sl is None and payload.get("sl_pct") is not None:
        try:
            sl_pct = float(payload.get("sl_pct"))
            sl = last * (1 - sl_pct if side == "long" else 1 + sl_pct)
            logger.info(f"Computed SL from payload sl_pct: {sl}")
        except (ValueError, TypeError):
            pass
    
    # Fallback to env vars if still None
    if tp is None and TP_PCT > 0:
        tp = last * (1 + TP_PCT if side == "long" else 1 - TP_PCT)
        logger.info(f"Computed TP from env TP_PCT: {tp}")
    
    if sl is None and SL_PCT > 0:
        sl = last * (1 - SL_PCT if side == "long" else 1 + SL_PCT)
        logger.info(f"Computed SL from env SL_PCT: {sl}")
    
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
        fill_price = order.get("price") or order.get("average") or last  # Use actual fill price if available
        
        # P0: Place TP/SL orders on exchange immediately after entry
        tp_sl_result = place_tp_sl_orders(symbol, side, contracts, tp, sl)
        
        # Update state
        pos["side"] = side
        pos["entry"] = fill_price
        pos["size"] = contracts
        pos["last_fill_ts"] = now_ts()
        apply_cooldown(pos)
        
        # P1: Mark signal as processed in persistent storage (only on success)
        from persistence import mark_signal_processed
        timeframe = payload.get("timeframe", "unknown")
        time_unix_ms = payload.get("time_unix_ms") or str(int(float(bar_ts) * 1000))
        mark_signal_processed(dedupe_key, EXCHANGE_ID, symbol_tv, side, timeframe, time_unix_ms, "ok")
        
        return {
            "status": "ok",
            "symbol": symbol,
            "side": side,
            "amount_sent": contracts,
            "contracts_mode": True,
            "contractSize": contract_size_for(symbol),
            "price_used": fill_price,
            "order_id": order_id,
            "flipped_from": flipped_from,
            "tp": tp,
            "sl": sl,
            "tp_order_id": tp_sl_result.get("tp_order_id"),
            "sl_order_id": tp_sl_result.get("sl_order_id"),
            "tp_error": tp_sl_result.get("tp_error"),
            "sl_error": tp_sl_result.get("sl_error"),
            "notional_used": notional,
            "leverage_used": leverage,
            "margin_mode_used": margin_mode,
        }
    except Exception as e:
        logger.exception(f"Order execution failed for {symbol}: {e}")
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
    """
    Webhook endpoint for TradingView alerts (direct POST).
    P1: Returns HTTP 200 for validation errors to prevent TradingView retries.
    """
    try:
        payload = await req.json()
    except Exception:
        # P1: Return 200 with error status (not 400) to prevent retries
        return {"status": "error", "message": "Invalid JSON"}

    if payload.get("secret") != SECRET:
        # P1: Return 200 with error status (not 403) to prevent retries
        return {"status": "error", "message": "Bad secret"}
    
    # Use extracted execute_order function
    result = execute_order(payload)
    
    # P1: Return HTTP 200 for all validation/business rule errors
    # Only use non-200 for temporary infrastructure failures
    status = result.get("status", "unknown")
    
    if status in ("error", "stale_signal", "cooldown", "already_in_position", 
                  "duplicate_ignored", "trading_disabled"):
        # Validation/business rule errors - return 200 to prevent retries
        return result
    
    # Only use non-200 for truly temporary failures (exchange down, etc.)
    # For now, we'll return 200 for everything to be safe
    return result
