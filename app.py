# app.py
import os, math
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv
import ccxt

load_dotenv()

SECRET        = os.getenv("TV_WEBHOOK_SECRET")
API_KEY       = os.getenv("MEXC_KEY")
API_SEC       = os.getenv("MEXC_SECRET")
POS_USDT      = float(os.getenv("POSITION_USDT", "20"))     # fixed notional per signal
LEVERAGE      = int(os.getenv("DEFAULT_LEVERAGE", "5"))
ACCOUNT_TYPE  = os.getenv("ACCOUNT_TYPE", "swap")           # USDT-M Perp
DRY_RUN       = os.getenv("DRY_RUN", "1") == "1"            # default ON while unfunded

if not all([SECRET, API_KEY, API_SEC]):
    raise RuntimeError("Missing env vars. Check .env")

exchange = ccxt.mexc({
    "apiKey": API_KEY,
    "secret": API_SEC,
    "enableRateLimit": True,
    "options": {"defaultType": ACCOUNT_TYPE},               # futures/swap context
})

app = FastAPI()
SEEN = set()  # idempotency

# --- Symbol map (extend as you add pairs) ---
SYMBOL_MAP = {
    # TradingView ticker (chart)         # ccxt contract symbol
    "MEXC:ETHUSDT": "ETH/USDT:USDT",
    "ETHUSDT":      "ETH/USDT:USDT",

    "MEXC:BTCUSDT": "BTC/USDT:USDT",
    "BTCUSDT":      "BTC/USDT:USDT",
}

def map_symbol(symbol_tv: str) -> str:
    return SYMBOL_MAP.get(symbol_tv, "ETH/USDT:USDT")  # default to ETH

@app.get("/health")
def health():
    return {
        "ok": True,
        "secret_loaded": bool(SECRET),
        "ccxt_version": getattr(ccxt, "__version__", "unknown"),
        "dry_run": DRY_RUN,
        "account_type": ACCOUNT_TYPE,
    }

@app.get("/debug/{symbol_tv}")
def dbg(symbol_tv: str):
    symbol = map_symbol(symbol_tv)
    markets = exchange.load_markets()
    m = markets[symbol]
    return {
        "symbol_tv": symbol_tv,
        "symbol_mapped": symbol,
        "type": m.get("type"),
        "contract": m.get("contract"),
        "linear": m.get("linear"),
        "contractSize": m.get("contractSize"),
        "limits": m.get("limits", {}),
        "has_createOrder": exchange.has.get("createOrder"),
    }

@app.post("/tv")
async def tv(req: Request):
    # ---- 1) parse & auth ----
    try:
        p = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    if p.get("secret") != SECRET:
        raise HTTPException(403, "Bad secret")

    side      = p.get("side")                  # "long" | "short"
    symbol_tv = p.get("symbol_tv")             # e.g., "MEXC:ETHUSDT"
    bar_ts    = p.get("bar_ts")                # ISO time
    if side not in ("long", "short") or not symbol_tv or not bar_ts:
        raise HTTPException(400, "Missing required fields")

    # ---- 2) idempotency ----
    key = f"{bar_ts}:{symbol_tv}:{side}"
    if key in SEEN:
        return {"status": "duplicate_ignored"}
    SEEN.add(key)

    # ---- 3) market & leverage ----
    symbol = map_symbol(symbol_tv)
    try:
        markets = exchange.load_markets()
        m = markets[symbol]
    except Exception as e:
        raise HTTPException(400, f"Unsupported symbol: {symbol} ({e})")

    try:
        exchange.set_leverage(LEVERAGE, symbol)   # ignore if already set
    except Exception:
        pass

    # ---- 4) pricing ----
    try:
        last = float(exchange.fetch_ticker(symbol)["last"])
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch price: {e}")

    # ---- 5) sizing: notional -> contracts (integer) ----
    try:
        if m.get("contract"):
            contract_size = float(m.get("contractSize") or 1.0)    # e.g., ETH ~ 0.01
            base_qty      = POS_USDT / last                        # ETH amount desired
            contracts_f   = base_qty / contract_size
            contracts     = int(math.floor(contracts_f))
            min_contracts = int(m.get("limits", {}).get("amount", {}).get("min") or 1)
            if contracts < min_contracts:
                raise HTTPException(
                    400,
                    f"Too small: {contracts} < min {min_contracts}. "
                    f"Increase POSITION_USDT or leverage."
                )
            amount = contracts
        else:
            # (Spot path, not used here)
            raw_qty = POS_USDT / last
            qty_str = exchange.amount_to_precision(symbol, raw_qty)
            amount  = float(qty_str)
            if amount <= 0:
                raise HTTPException(400, "Computed qty is zero; increase POSITION_USDT")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Sizing error: {e}")

    # ---- 6) order placement ----
    if DRY_RUN:
        order = {"id": f"sim-{symbol}-{side}"}
        status = "simulated_ok"
    else:
        try:
            if side == "long":
                order = exchange.create_market_buy_order(symbol, amount)
            else:
                order = exchange.create_market_sell_order(symbol, amount)
            status = "ok"
        except Exception as e:
            # If CCXT version complains about createSwapOrder, upgrade ccxt and/or tweak ACCOUNT_TYPE.
            raise HTTPException(502, f"Order error: {e}")

    return {
        "status": status,
        "symbol": symbol,
        "side": side,
        "amount_sent": amount,
        "contracts_mode": bool(m.get("contract")),
        "contractSize": m.get("contractSize"),
        "price_used": last,
        "order_id": order.get("id"),
    }
