import os, time, math
from datetime import datetime, timezone
from typing import Dict, Any

from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv
import ccxt

load_dotenv()

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

if not SECRET:
    raise RuntimeError("Missing TV_WEBHOOK_SECRET in .env")

# ---- EXCHANGE (public endpoints are fine even without keys) ----
exchange = ccxt.mexc({
    "apiKey": API_KEY or "",
    "secret": API_SEC or "",
    "enableRateLimit": True,
    "options": {"defaultType": ACCOUNT_TYPE},
})

# ---- APP ----
app = FastAPI()

# ---- State & helpers ----
SEEN_KEYS = set()  # idempotency (bar_ts:symbol_tv:side)

# simple in-memory position state per symbol
STATE: Dict[str, Dict[str, Any]] = {}  # {symbol: {side, entry, size, last_fill_ts, cooldown_until}}

# TV -> CCXT symbol map (extend as needed)
SYMBOL_MAP = {
    "MEXC:ETHUSDT": "ETH/USDT:USDT",
    "ETHUSDT": "ETH/USDT:USDT",
    "MEXC:BTCUSDT": "BTC/USDT:USDT",
    "BTCUSDT": "BTC/USDT:USDT",
}

# contract sizes for linear USDT-M perps (approx; fine for DRY_RUN sizing)
CONTRACT_SIZE = {
    "ETH/USDT:USDT": 0.01,   # 0.01 ETH per contract
    "BTC/USDT:USDT": 0.001,  # 0.001 BTC per contract
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

# ---- Routes ----

@app.get("/")
def root():
    return {"ok": True, "dry_run": DRY_RUN, "account_type": ACCOUNT_TYPE}

@app.get("/state")
def state():
    return {"state": STATE, "seen_keys": len(SEEN_KEYS)}

@app.post("/tv")
async def tv(req: Request):
    # ---- parse & validate ----
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    if payload.get("secret") != SECRET:
        raise HTTPException(403, "Bad secret")

    side = payload.get("side")           # "long" | "short"
    symbol_tv = payload.get("symbol_tv") # e.g. "MEXC:ETHUSDT"
    bar_ts = payload.get("bar_ts")       # ISO string from TV

    if side not in ("long", "short") or not symbol_tv or not bar_ts:
        raise HTTPException(400, "Missing required fields: side, symbol_tv, bar_ts")

    # idempotency key: 1 order per bar per side per symbol
    dedupe_key = f"{bar_ts}:{symbol_tv}:{side}"
    if dedupe_key in SEEN_KEYS:
        return {"status": "duplicate_ignored"}
    SEEN_KEYS.add(dedupe_key)

    symbol = map_symbol(symbol_tv)

    # load markets once (ccxt caches internally); set leverage best-effort
    try:
        exchange.load_markets()
    except Exception:
        pass
    try:
        # harmless if already set / or in DRY_RUN
        exchange.set_leverage(LEVERAGE, symbol)
    except Exception:
        pass

    # get mark/last price for sizing
    try:
        last = get_last_price(symbol)
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch price for {symbol}: {e}")

    # position state & cooldown
    pos = position_for(symbol)
    if in_cooldown(pos):
        return {"status": "cooldown", "until": fmt_ts(pos["cooldown_until"]), "symbol": symbol}

    # if we're already in that side, ignore
    if pos["side"] == side:
        return {"status": "already_in_position", "symbol": symbol, "side": side}

    # if we are in the opposite side, "close" it first (DRY_RUN)
    flipped_from = None
    if pos["side"] in ("long", "short") and pos["side"] != side:
        flipped_from = pos["side"]
        # In DRY_RUN, we just log the PnL baseline; in LIVE we'll send reduce-only close
        pos["side"] = "flat"
        pos["entry"] = None
        pos["size"] = 0

    # sizing
    contracts = calc_contracts(symbol, POS_USDT, last)

    # optional TP/SL levels (informational in DRY_RUN)
    if TP_PCT > 0:
        tp = last * (1 + TP_PCT if side == "long" else 1 - TP_PCT)
    else:
        tp = None
    if SL_PCT > 0:
        sl = last * (1 - SL_PCT if side == "long" else 1 + SL_PCT)
    else:
        sl = None

    # ---- Place order ----
    if DRY_RUN:
        # simulate a market fill
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

    # ---- LIVE path (to be enabled when funded) ----
    try:
        # IMPORTANT: for MEXC linear swaps you usually send amount=contracts in market orders.
        # ccxt will handle correct side; add any required params here if needed.
        if side == "long":
            order = exchange.create_market_buy_order(symbol, contracts)
        else:
            order = exchange.create_market_sell_order(symbol, contracts)
    except Exception as e:
        raise HTTPException(500, f"Order error: {e}")

    # update state
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
        "order_id": order.get("id"),
        "flipped_from": flipped_from,
        "tp": tp,
        "sl": sl,
    }
