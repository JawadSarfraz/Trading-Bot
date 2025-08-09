import os, time
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv
import ccxt

load_dotenv()

SECRET = os.getenv("TV_WEBHOOK_SECRET")
API_KEY = os.getenv("MEXC_KEY")
API_SEC = os.getenv("MEXC_SECRET")
POS_USDT = float(os.getenv("POSITION_USDT", "20"))
LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "5"))
ACCOUNT_TYPE = os.getenv("ACCOUNT_TYPE", "swap")  # "swap" for USDT-M perp

if not all([SECRET, API_KEY, API_SEC]):
    raise RuntimeError("Missing env vars. Check .env")

exchange = ccxt.mexc({
    "apiKey": API_KEY,
    "secret": API_SEC,
    "enableRateLimit": True,
    "options": {"defaultType": ACCOUNT_TYPE},
})

app = FastAPI()
SEEN = set()  # simple idempotency

# Minimal symbol map for MVP (extend later)
SYMBOL_MAP = {
    "MEXC:BTCUSDT": "BTC/USDT:USDT",
    "BTCUSDT":      "BTC/USDT:USDT",
}

def map_symbol(symbol_tv: str) -> str:
    return SYMBOL_MAP.get(symbol_tv, "BTC/USDT:USDT")

@app.post("/tv")
async def tv(req: Request):
    try:
        p = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    if p.get("secret") != SECRET:
        raise HTTPException(403, "Bad secret")

    side = p.get("side")             # "long" or "short"
    symbol_tv = p.get("symbol_tv")   # e.g. "MEXC:BTCUSDT"
    bar_ts = p.get("bar_ts")         # ISO time string from TV
    if side not in ("long","short") or not symbol_tv or not bar_ts:
        raise HTTPException(400, "Missing required fields")

    # idempotency: 1 order per bar per side per symbol
    key = f"{bar_ts}:{symbol_tv}:{side}"
    if key in SEEN:
        return {"status": "duplicate_ignored"}
    SEEN.add(key)

    symbol = map_symbol(symbol_tv)

    # set leverage once per symbol (ignore errors if already set)
    try:
        exchange.set_leverage(LEVERAGE, symbol)
    except Exception:
        pass

    # sizing: fixed notional -> qty = usdt / price
    markets = exchange.load_markets()
    m = markets[symbol]
    last = exchange.fetch_ticker(symbol)["last"]
    qty = round(POS_USDT / last, m["precision"]["amount"])

    # place market order
    try:
        if side == "long":
            order = exchange.create_market_buy_order(symbol, qty)
        else:
            order = exchange.create_market_sell_order(symbol, qty)
    except Exception as e:
        raise HTTPException(500, f"Order error: {e}")

    return {
        "status": "ok",
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price_used": last,
        "order_id": order.get("id")
    }
