
import asyncio
import hashlib
import json
import logging
import os
import random
import string
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any

import numpy as np
import requests
from telegram import Bot


# =========================================================
# 1. CONFIG
# =========================================================

# ---------- Bitunix ----------
API_KEY = "67ddaa38bcc4a103cdd8effc3cdc5cf6"
API_SECRET = "3966972df4a20a03b80bb31df6980fd9"
BASE_URL = "https://fapi.bitunix.com"

# ---------- Telegram ----------
BOT_TOKEN = "8361089099:AAHd419NPgMetrZq_Acn5v4XJyWsO4C8Wbw"
CHAT_ID = "745887761"   # пример: "745887761" или "-1003845889495"

# ---------- Trading mode ----------
DRY_RUN = False   # True = без реальных сделок, False = реальные сделки

# ---------- Trading params ----------
START_BALANCE = 100.0
RISK_PER_TRADE = 0.02   # 2%
LEVERAGE = 10

# --- TP / SL (проценты по позиции, с учетом плеча) ---
TP1_PCT = 0.30
TP2_PCT = 0.60
TP3_PCT = 0.90
SL_PCT  = 0.30

# --- Timing ---
TIMEOUT_MINUTES = 10
POLL_SECONDS = 2

# --- Exchange / Universe ---
QUOTE_ASSET = "USDT"
DEFAULT_SYMBOL = "BTCUSDT"
SCREENER_LOOKBACK = 5
SCREENER_MIN_MOVE_PCT = 0.15
SCREENER_MAX_SYMBOLS = 15

# --- Misc ---
DEBUG = True
STATE_FILE = "position_state.json"
REQUEST_TIMEOUT = 15
ERROR_NOTIFY_COOLDOWN_SEC = 300


# =========================================================
# 2. LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("bitunix-bot")


# =========================================================
# 3. HELPERS
# =========================================================

def now_ms() -> str:
    return str(int(time.time() * 1000))


def make_nonce(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def compact_json(data: Dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def build_query_string(params: Dict[str, Any]) -> str:
    if not params:
        return ""
    items = sorted((k, v) for k, v in params.items() if v is not None)
    return "".join(f"{k}{v}" for k, v in items)


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


# =========================================================
# 4. BITUNIX HTTP CLIENT
# =========================================================

class BitunixHTTP:
    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None, base_url: str = BASE_URL):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "language": "en-US",
        })

    def _sign_headers(self, params: Optional[Dict[str, Any]] = None, body: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        params = params or {}
        body = body or {}

        nonce = make_nonce()
        timestamp = now_ms()
        query_str = build_query_string(params)
        body_str = compact_json(body) if body else ""

        digest = sha256_hex(nonce + timestamp + self.api_key + query_str + body_str)
        sign = sha256_hex(digest + self.api_secret)

        return {
            "api-key": self.api_key,
            "nonce": nonce,
            "timestamp": timestamp,
            "sign": sign,
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        private: bool = False,
    ) -> Dict[str, Any]:
        params = params or {}
        body = body or {}
        headers = {}

        if private:
            if not self.api_key or not self.api_secret:
                raise RuntimeError("Private API requested but API credentials are missing.")
            headers.update(self._sign_headers(params=params, body=body))

        url = f"{self.base_url}{path}"

        if method.upper() == "GET":
            resp = self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        elif method.upper() == "POST":
            resp = self.session.post(
                url,
                params=params,
                data=compact_json(body),
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
        else:
            raise ValueError(f"Unsupported method: {method}")

        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"Bitunix API error: {data}")

        return data


public_http = BitunixHTTP()
private_http = BitunixHTTP(API_KEY, API_SECRET)


# =========================================================
# 5. MARKET DATA
# =========================================================

class BitunixMarketData:
    def __init__(self, public_client: BitunixHTTP):
        self.public = public_client

    def get_last_price(self, symbol: str) -> Optional[float]:
        try:
            data = self.public.request(
                "GET",
                "/api/v1/futures/market/tickers",
                params={"symbols": symbol},
                private=False
            )

            payload = data.get("data", [])
            if isinstance(payload, dict):
                payload = [payload]

            if not payload:
                return None

            item = payload[0]
            for key in ("last", "lastPrice", "close"):
                value = safe_float(item.get(key))
                if value is not None:
                    return value

            logger.warning("Unknown ticker schema for %s: %s", symbol, item)
            return None

        except Exception as e:
            logger.exception("Price error for %s: %s", symbol, e)
            return None

    def get_candles(self, symbol: str, tf: str = "1m", limit: int = 50) -> List[list]:
        try:
            data = self.public.request(
                "GET",
                "/api/v1/futures/market/kline",
                params={
                    "symbol": symbol,
                    "interval": tf,
                    "limit": limit,
                },
                private=False
            )

            payload = data.get("data", [])
            if not isinstance(payload, list):
                return []

            out = []
            for c in payload:
                if not isinstance(c, dict):
                    continue

                ts = c.get("time") or c.get("ts") or c.get("openTime") or 0
                o = safe_float(c.get("open"))
                h = safe_float(c.get("high"))
                l = safe_float(c.get("low"))
                cl = safe_float(c.get("close"))
                v = safe_float(c.get("volume") or c.get("vol"), 0.0)

                if None in (o, h, l, cl):
                    continue

                out.append([
                    int(ts),
                    o,
                    h,
                    l,
                    cl,
                    v if v is not None else 0.0,
                ])

            return out

        except Exception as e:
            logger.exception("Candle error for %s: %s", symbol, e)
            return []


market_data = BitunixMarketData(public_http)


def build_market_context(symbol: str) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "ts": time.time(),
        "last_price": market_data.get_last_price(symbol),
        "candles_1m": market_data.get_candles(symbol, "1m", 15),
        "candles_5m": market_data.get_candles(symbol, "5m", 20),
    }


# =========================================================
# 6. UNIVERSE LOADER
# =========================================================

class BitunixFuturesUniverseLoader:
    def __init__(self, public_client: BitunixHTTP):
        self.public = public_client
        self._symbols: List[str] = []

    def load(self) -> List[str]:
        symbols: List[str] = []

        try:
            data = self.public.request(
                "GET",
                "/api/v1/futures/market/trading_pairs",
                params={},
                private=False
            )

            payload = data.get("data", [])
            if isinstance(payload, dict):
                payload = [payload]

            for item in payload:
                if not isinstance(item, dict):
                    continue

                symbol = item.get("symbol")
                if not symbol:
                    continue

                if symbol.endswith(QUOTE_ASSET):
                    symbols.append(symbol)

        except Exception as e:
            logger.warning("Universe load failed, fallback to default symbol only: %s", e)
            symbols = [DEFAULT_SYMBOL]

        symbols = sorted(set(symbols))
        if not symbols:
            symbols = [DEFAULT_SYMBOL]

        self._symbols = symbols
        return symbols

    @property
    def symbols(self) -> List[str]:
        return self._symbols


universe_loader = BitunixFuturesUniverseLoader(public_http)
UNIVERSE_SYMBOLS = universe_loader.load()


# =========================================================
# 7. VOLATILITY SCREENER
# =========================================================

class VolatilityScreener:
    def __init__(
        self,
        market_data: BitunixMarketData,
        symbols: List[str],
        lookback: int = SCREENER_LOOKBACK,
        min_move_pct: float = SCREENER_MIN_MOVE_PCT,
        max_symbols: int = SCREENER_MAX_SYMBOLS
    ):
        self.md = market_data
        self.symbols = symbols
        self.lookback = lookback
        self.min_move_pct = min_move_pct
        self.max_symbols = max_symbols

    def scan(self) -> List[str]:
        hot = []

        for symbol in self.symbols:
            try:
                candles = self.md.get_candles(symbol, "1m", self.lookback + 1)
                if len(candles) < self.lookback + 1:
                    continue

                open_price = candles[0][1]
                last_close = candles[-1][4]

                if open_price <= 0:
                    continue

                move_pct = abs(last_close - open_price) / open_price * 100

                if move_pct >= self.min_move_pct:
                    hot.append((symbol, move_pct))

            except Exception:
                continue

        hot.sort(key=lambda x: x[1], reverse=True)
        selected = [s for s, _ in hot[:self.max_symbols]]

        if DEBUG:
            logger.info("[SCREENER] Hot symbols: %s", selected)

        return selected


volatility_screener = VolatilityScreener(
    market_data=market_data,
    symbols=UNIVERSE_SYMBOLS
)


# =========================================================
# 8. STRATEGY INTERFACE
# =========================================================

@dataclass
class Signal:
    symbol: str
    side: str
    entry_price: float
    strategy: str
    context: Dict[str, Any]


class StrategyInterface:
    name: str = "BASE"

    def generate(self, context: Dict[str, Any]) -> Optional[Signal]:
        raise NotImplementedError("Strategy must implement generate().")


# =========================================================
# 9. STRATEGY FSI_v1.3_HARD
# =========================================================

class StrategyFSI_v1_3_HARD(StrategyInterface):
    name = "FSI_v1.3_HARD"

    def __init__(self):
        self.reset_debug()

    def reset_debug(self):
        self.db_volume = 0
        self.db_expansion = 0
        self.db_impulse = 0
        self.db_accel = 0
        self.db_pullback = 0
        self.db_bias = 0
        self.db_final = 0

    def print_debug(self):
        if DEBUG:
            logger.info(
                "[DEBUG FSI] volume=%s | expansion=%s | impulse=%s | accel=%s | "
                "pullback=%s | bias=%s | signals=%s",
                self.db_volume,
                self.db_expansion,
                self.db_impulse,
                self.db_accel,
                self.db_pullback,
                self.db_bias,
                self.db_final
            )
            self.reset_debug()

    def generate(self, context: Dict[str, Any]) -> Optional[Signal]:
        candles_1m = context["candles_1m"]
        candles_5m = context["candles_5m"]
        last_price = context["last_price"]
        symbol = context["symbol"]

        if last_price is None:
            return None

        if len(candles_1m) < 12 or len(candles_5m) < 10:
            return None

        volumes = np.array([c[5] for c in candles_1m[-12:]], dtype=float)
        recent_vol_avg = volumes[-3:].mean()
        prev_vol_avg = volumes[:-3].mean()

        if prev_vol_avg > 0 and recent_vol_avg < prev_vol_avg * 1.5:
            return None

        self.db_volume += 1

        highs_recent = np.array([c[2] for c in candles_1m[-5:]], dtype=float)
        lows_recent = np.array([c[3] for c in candles_1m[-5:]], dtype=float)
        highs_prev = np.array([c[2] for c in candles_1m[-10:-5]], dtype=float)
        lows_prev = np.array([c[3] for c in candles_1m[-10:-5]], dtype=float)

        range_recent = highs_recent.max() - lows_recent.min()
        range_prev = highs_prev.max() - lows_prev.min()

        if range_prev > 0 and (range_recent / range_prev) < 1.1:
            return None

        self.db_expansion += 1

        closes = np.array([c[4] for c in candles_1m[-8:]], dtype=float)
        impulse_move = closes[-1] - closes[0]
        impulse_pct = abs(impulse_move) / closes[0]

        if impulse_pct < 0.004:
            return None

        self.db_impulse += 1

        impulse_high = highs_recent.max()
        impulse_low = lows_recent.min()
        impulse_range = impulse_high - impulse_low

        if impulse_range <= 0:
            return None

        move_last = abs(closes[-1] - closes[-2])
        move_prev = abs(closes[-2] - closes[-3])

        if move_last < move_prev * 0.8:
            return None

        self.db_accel += 1

        if impulse_move > 0:
            side = "LONG"
            pullback = (impulse_high - last_price) / impulse_range
        else:
            side = "SHORT"
            pullback = (last_price - impulse_low) / impulse_range

        if not (0.30 <= pullback <= 0.55):
            return None

        self.db_pullback += 1

        last_two = closes[-2:]

        if side == "LONG":
            if last_two[1] < last_two[0]:
                return None
        else:
            if last_two[1] > last_two[0]:
                return None

        closes_5m = np.array([c[4] for c in candles_5m[-10:]], dtype=float)
        sma_5m = closes_5m.mean()

        if side == "LONG" and last_price < sma_5m:
            return None
        if side == "SHORT" and last_price > sma_5m:
            return None

        self.db_bias += 1

        if side == "LONG":
            tp1 = last_price * (1 + 0.012)
            sl = last_price * (1 - 0.012)
        else:
            tp1 = last_price * (1 - 0.012)
            sl = last_price * (1 + 0.012)

        dist_to_tp1 = abs(tp1 - last_price)
        dist_to_sl = abs(last_price - sl)

        if dist_to_tp1 < dist_to_sl * 0.6:
            return None

        self.db_final += 1

        return Signal(
            symbol=symbol,
            side=side,
            entry_price=last_price,
            strategy=self.name,
            context=context
        )


strategy = StrategyFSI_v1_3_HARD()


# =========================================================
# 10. POSITION
# =========================================================

@dataclass
class Position:
    symbol: str
    side: str
    entry_price: float
    entry_time: float
    size_usd: float
    strategy: str

    tp1: float
    tp2: float
    tp3: float
    sl: float

    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    closed: bool = False

    realized_pnl: float = 0.0
    realized_pct: float = 0.0
    commission: float = 0.0

    remaining_fraction: float = 1.0
    exit_reason: Optional[str] = None
    exit_price: Optional[float] = None
    leverage: int = LEVERAGE
    position_id: Optional[str] = None
    order_id: Optional[str] = None

    tp1_order_id: Optional[str] = None
    tp2_order_id: Optional[str] = None
    tp3_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None

    def is_timeout(self, now: float) -> bool:
        return (now - self.entry_time) >= TIMEOUT_MINUTES * 60

    def close(self, price: float, reason: str):
        self.closed = True
        self.exit_price = price
        self.exit_reason = reason

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Position":
        return cls(**data)


# =========================================================
# 11. TRADE ENGINE CORE
# =========================================================

class TradeEngine:
    def __init__(self, timeout_minutes: int):
        self.timeout_sec = timeout_minutes * 60

    def process_price(self, pos: Position, price: float, now: float):
        if pos.closed:
            return None

        if not pos.tp1_hit:
            if (pos.side == "LONG" and price >= pos.tp1) or (pos.side == "SHORT" and price <= pos.tp1):
                return "TP1"

        if pos.tp1_hit and not pos.tp2_hit:
            if (pos.side == "LONG" and price >= pos.tp2) or (pos.side == "SHORT" and price <= pos.tp2):
                return "TP2"

        if pos.tp2_hit and not pos.tp3_hit:
            if (pos.side == "LONG" and price >= pos.tp3) or (pos.side == "SHORT" and price <= pos.tp3):
                return "TP3"

        if pos.side == "LONG":
            if price <= pos.sl:
                return "SL"
        else:
            if price >= pos.sl:
                return "SL"

        if pos.is_timeout(now):
            return "TIMEOUT"

        return None


trade_engine = TradeEngine(timeout_minutes=TIMEOUT_MINUTES)


# =========================================================
# 12. BITUNIX EXECUTOR
# =========================================================

class BitunixExecutor:
    def __init__(self, private_client: BitunixHTTP):
        self.private = private_client
        self.entry_in_progress = False

    def get_account(self, margin_coin: str = "USDT") -> Dict[str, Any]:
        data = self.private.request(
            "GET",
            "/api/v1/futures/account",
            params={"marginCoin": margin_coin},
            private=True
        )

        payload = data.get("data")

        if payload is None:
            raise RuntimeError(f"Empty account response: {data}")

        if isinstance(payload, list):
            if not payload:
                raise RuntimeError(f"Account list is empty: {data}")
            return payload[0]

        if isinstance(payload, dict):
            return payload

        raise RuntimeError(f"Unexpected account response shape: {data}")

    def get_available_balance(self) -> float:
        account = self.get_account(QUOTE_ASSET)

        candidate_keys = [
            "available",
            "availableBalance",
            "availableMargin",
            "canUseAmount",
            "marginBalance",
            "accountEquity",
            "equity",
            "balance",
        ]

        for key in candidate_keys:
            value = safe_float(account.get(key))
            if value is not None:
                return value

        raise RuntimeError(f"Could not determine available balance from account response: {account}")

    def get_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {}
        if symbol:
            params["symbol"] = symbol

        data = self.private.request(
            "GET",
            "/api/v1/futures/position/get_pending_positions",
            params=params,
            private=True
        )

        payload = data.get("data", [])
        if isinstance(payload, dict):
            return [payload]
        if isinstance(payload, list):
            return payload

        return []

    def has_position(self, symbol: str) -> bool:
        try:
            positions = self.get_positions(symbol)
            for p in positions:
                qty = safe_float(p.get("qty") or p.get("positionQty") or p.get("volume") or p.get("amount"), 0.0)
                if qty and abs(qty) > 0:
                    return True
            return False
        except Exception as e:
            logger.exception("Position check error for %s: %s", symbol, e)
            return False

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        positions = self.get_positions(symbol)
        if not positions:
            return None
        return positions[0]

    def set_leverage(self, symbol: str) -> Optional[int]:
        for lev in [LEVERAGE, 20, 15, 10, 5]:
            try:
                self.private.request(
                    "POST",
                    "/api/v1/futures/account/change_leverage",
                    body={
                        "symbol": symbol,
                        "leverage": lev,
                        "marginCoin": QUOTE_ASSET,
                    },
                    private=True
                )
                return lev
            except Exception:
                continue
        return None

    def place_position_tpsl(self, pos: Position, tp_price: Optional[float], sl_price: Optional[float]) -> Optional[str]:
        if not pos.position_id:
            raise RuntimeError(f"position_id is missing for {pos.symbol}")

        body = {
            "symbol": pos.symbol,
            "positionId": str(pos.position_id),
        }

        if tp_price is not None:
            body["tpPrice"] = self._fmt_price(tp_price)
            body["tpStopType"] = "LAST_PRICE"

        if sl_price is not None:
            body["slPrice"] = self._fmt_price(sl_price)
            body["slStopType"] = "LAST_PRICE"

        if "tpPrice" not in body and "slPrice" not in body:
            raise RuntimeError("At least tpPrice or slPrice is required")

        data = self.private.request(
            "POST",
            "/api/v1/futures/tpsl/position/place_order",
            body=body,
            private=True
        )

        payload = data.get("data", {})
        if isinstance(payload, dict):
            return payload.get("orderId")
        return None

    def place_reduce_limit_order(self, symbol: str, qty: float, side: str, price: float) -> Optional[str]:
        data = self.private.request(
            "POST",
            "/api/v1/futures/trade/place_order",
            body={
                "symbol": symbol,
                "qty": self._fmt_qty(qty),
                "side": side,
                "tradeSide": "CLOSE",
                "orderType": "LIMIT",
                "price": self._fmt_price(price),
                "effect": "GTC",
                "reduceOnly": True,
                "clientId": f"tp_{int(time.time() * 1000)}",
            },
            private=True
        )

        payload = data.get("data", {})
        if isinstance(payload, dict):
            return payload.get("orderId")
        return None

    def cancel_orders(self, symbol: str, order_ids: List[str]):
        clean_ids = [oid for oid in order_ids if oid]
        if not clean_ids:
            return

        order_list = [{"orderId": oid} for oid in clean_ids]

        self.private.request(
            "POST",
            "/api/v1/futures/trade/cancel_orders",
            body={
                "symbol": symbol,
                "orderList": order_list,
            },
            private=True
        )

    def normalize_size(self, symbol: str, size_usd: float, price: float) -> float:
        if price <= 0:
            return 0.001
        qty = size_usd / price
        return max(round(qty, 6), 0.001)

    def open_position(self, pos: Position):
        if DRY_RUN:
            return

        if self.entry_in_progress:
            return

        self.entry_in_progress = True

        try:
            lev = self.set_leverage(pos.symbol) or LEVERAGE
            pos.leverage = lev

            qty = self.normalize_size(pos.symbol, pos.size_usd, pos.entry_price)
            side = "BUY" if pos.side == "LONG" else "SELL"

            order = self.private.request(
                "POST",
                "/api/v1/futures/trade/place_order",
                body={
                    "symbol": pos.symbol,
                    "qty": self._fmt_qty(qty),
                    "side": side,
                    "tradeSide": "OPEN",
                    "orderType": "MARKET",
                    "reduceOnly": False,
                    "clientId": f"entry_{int(time.time())}",
                },
                private=True
            )

            order_data = order.get("data", {})
            if isinstance(order_data, dict):
                pos.order_id = order_data.get("orderId")

            time.sleep(2)

            exchange_pos = self.get_position(pos.symbol)
            if not exchange_pos:
                raise RuntimeError("Position not found after order placement")

            pos.position_id = exchange_pos.get("positionId")

            avg_price = safe_float(
                exchange_pos.get("avgOpenPrice")
                or exchange_pos.get("avgPrice")
                or exchange_pos.get("entryPrice"),
                pos.entry_price
            )
            if avg_price is not None:
                pos.entry_price = avg_price

            real_qty = safe_float(
                exchange_pos.get("qty")
                or exchange_pos.get("positionQty")
                or exchange_pos.get("volume"),
                qty
            )
            if real_qty is None:
                real_qty = qty

            if pos.side == "LONG":
                pos.tp1 = pos.entry_price * (1 + TP1_PCT / pos.leverage)
                pos.tp2 = pos.entry_price * (1 + TP2_PCT / pos.leverage)
                pos.tp3 = pos.entry_price * (1 + TP3_PCT / pos.leverage)
                pos.sl  = pos.entry_price * (1 - SL_PCT / pos.leverage)
                close_side = "SELL"
            else:
                pos.tp1 = pos.entry_price * (1 - TP1_PCT / pos.leverage)
                pos.tp2 = pos.entry_price * (1 - TP2_PCT / pos.leverage)
                pos.tp3 = pos.entry_price * (1 - TP3_PCT / pos.leverage)
                pos.sl  = pos.entry_price * (1 + SL_PCT / pos.leverage)
                close_side = "BUY"

            tp1_qty = round(real_qty * 0.20, 6)
            tp2_qty = round(real_qty * 0.50, 6)
            tp3_qty = round(real_qty - tp1_qty - tp2_qty, 6)

            if tp1_qty <= 0:
                tp1_qty = 0.001
            if tp2_qty <= 0:
                tp2_qty = 0.001
            if tp3_qty <= 0:
                tp3_qty = 0.001

            pos.sl_order_id = self.place_position_tpsl(pos, tp_price=None, sl_price=pos.sl)
            pos.tp1_order_id = self.place_reduce_limit_order(pos.symbol, tp1_qty, close_side, pos.tp1)
            pos.tp2_order_id = self.place_reduce_limit_order(pos.symbol, tp2_qty, close_side, pos.tp2)
            pos.tp3_order_id = self.place_reduce_limit_order(pos.symbol, tp3_qty, close_side, pos.tp3)

        finally:
            self.entry_in_progress = False

    def move_stop(self, pos: Position, new_sl: float):
        if DRY_RUN:
            return
        self.place_position_tpsl(pos, tp_price=None, sl_price=new_sl)

    def cancel_all_tps(self, pos: Position):
        if DRY_RUN:
            return
        self.cancel_orders(
            pos.symbol,
            [pos.tp1_order_id, pos.tp2_order_id, pos.tp3_order_id]
        )

    def close_position(self, pos: Position):
        if DRY_RUN:
            return

        try:
            self.cancel_all_tps(pos)
        except Exception:
            pass

        exchange_pos = self.get_position(pos.symbol)
        if not exchange_pos:
            logger.info("No live position to close for %s", pos.symbol)
            return

        qty = safe_float(exchange_pos.get("qty") or exchange_pos.get("positionQty") or exchange_pos.get("volume"), 0.0)
        if not qty or qty <= 0:
            logger.info("Zero position qty for %s", pos.symbol)
            return

        side = "SELL" if pos.side == "LONG" else "BUY"

        self.private.request(
            "POST",
            "/api/v1/futures/trade/place_order",
            body={
                "symbol": pos.symbol,
                "qty": self._fmt_qty(qty),
                "side": side,
                "tradeSide": "CLOSE",
                "orderType": "MARKET",
                "reduceOnly": True,
                "clientId": f"close_{int(time.time())}",
            },
            private=True
        )

    def sync_trade_result(self, pos: Position, current_price: Optional[float] = None):
        if DRY_RUN:
            return

        price = current_price if current_price is not None else market_data.get_last_price(pos.symbol)
        if price is None:
            return

        direction = 1 if pos.side == "LONG" else -1
        move_pct = (price - pos.entry_price) / pos.entry_price * direction

        margin = pos.size_usd / max(pos.leverage, 1)
        pos.realized_pnl = margin * move_pct * pos.leverage
        pos.realized_pct = move_pct * pos.leverage * 100
        pos.commission = 0.0

    @staticmethod
    def _fmt_qty(value: float) -> str:
        return f"{value:.6f}".rstrip("0").rstrip(".")

    @staticmethod
    def _fmt_price(value: Optional[float]) -> Optional[str]:
        if value is None:
            return None
        return f"{value:.6f}".rstrip("0").rstrip(".")


executor = BitunixExecutor(private_http)


# =========================================================
# 13. ACCOUNTING
# =========================================================

class AccountingEngine:
    def __init__(self, start_balance: float, risk_per_trade: float, leverage: int):
        self.balance = start_balance
        self.risk_per_trade = risk_per_trade
        self.leverage = leverage

    def sync_balance(self):
        if DRY_RUN:
            return

        try:
            self.balance = executor.get_available_balance()
        except Exception as e:
            logger.exception("Balance sync error: %s", e)

    def calc_position_size(self) -> float:
        if not DRY_RUN:
            self.sync_balance()

        margin = self.balance * self.risk_per_trade
        return margin * self.leverage

    def apply_partial_close(self, pos: Position, level: str):
        if DRY_RUN:
            return

        try:
            if level == "TP1":
                executor.move_stop(pos, pos.entry_price)
            elif level == "TP2":
                executor.move_stop(pos, pos.tp1)
        except Exception as e:
            logger.exception("SL move error: %s", e)

    def apply_final_close(self, pos: Position, current_price: Optional[float] = None):
        if DRY_RUN:
            return
        executor.sync_trade_result(pos, current_price=current_price)
        self.sync_balance()


accounting = AccountingEngine(
    start_balance=START_BALANCE,
    risk_per_trade=RISK_PER_TRADE,
    leverage=LEVERAGE
)


# =========================================================
# 14. INTEGRATOR
# =========================================================

class TradeAccountingIntegrator:
    def __init__(self, engine: TradeEngine, accounting: AccountingEngine):
        self.engine = engine
        self.accounting = accounting
        self.execution_lock = False

    def on_price_update(self, pos: Position, price: float, now: float):
        if self.execution_lock:
            return None

        if pos.closed:
            return None

        self.execution_lock = True

        try:
            base_timeout = self.engine.timeout_sec

            if pos.tp1_hit:
                self.engine.timeout_sec = base_timeout + 180
            else:
                self.engine.timeout_sec = base_timeout

            try:
                event = self.engine.process_price(pos, price, now)
            finally:
                self.engine.timeout_sec = base_timeout

            if event == "TP1" and not pos.tp1_hit:
                fraction = 0.20
                self.accounting.apply_partial_close(pos=pos, level="TP1")
                pos.remaining_fraction = max(pos.remaining_fraction - fraction, 0)
                pos.tp1_hit = True
                pos.sl = pos.entry_price
                return "TP1"

            if event == "TP2" and not pos.tp2_hit:
                fraction = 0.50
                self.accounting.apply_partial_close(pos=pos, level="TP2")
                pos.remaining_fraction = max(pos.remaining_fraction - fraction, 0)
                pos.tp2_hit = True
                pos.sl = pos.tp1
                return "TP2"

            if event == "TP3" and not pos.tp3_hit:
                pos.remaining_fraction = 0
                pos.tp3_hit = True
                pos.closed = True
                pos.exit_reason = "TP3"
                pos.exit_price = pos.tp3
                self.accounting.apply_final_close(pos, current_price=pos.exit_price)
                return "TP3"

            if event in ("SL", "TIMEOUT"):
                pos.remaining_fraction = 0
                pos.closed = True
                pos.exit_reason = event
                pos.exit_price = price

                if not DRY_RUN:
                    executor.close_position(pos)

                self.accounting.apply_final_close(pos, current_price=price)
                return event

            return None

        finally:
            self.execution_lock = False


integrator = TradeAccountingIntegrator(
    engine=trade_engine,
    accounting=accounting
)


# =========================================================
# 15. TELEGRAM FORMATTER
# =========================================================

def format_signal_message(pos: Position) -> str:
    side_icon = "💹 LONG" if pos.side == "LONG" else "🛑 SHORT"

    tp1_line = f"🎯TP1: {pos.tp1:.6f}" + ("✅" if pos.tp1_hit else "")
    tp2_line = f"🎯TP2: {pos.tp2:.6f}" + ("✅" if pos.tp2_hit else "")
    tp3_line = f"🎯TP3: {pos.tp3:.6f}" + ("✅" if pos.exit_reason == "TP3" else "")

    sl_hit = pos.exit_reason == "SL" and not (pos.tp1_hit or pos.tp2_hit)
    sl_suffix = "❌" if sl_hit else ""
    sl_line = f"⛔️SL: {pos.sl:.6f}{sl_suffix}"

    if pos.exit_reason == "TP3":
        close_line = "Позиция закрыта✅"
    elif pos.exit_reason == "SL":
        close_line = "Позиция закрыта✅" if (pos.tp1_hit or pos.tp2_hit) else "Позиция закрыта❌"
    elif pos.exit_reason == "TIMEOUT":
        close_line = "Позиция закрыта✅⏱️" if (pos.tp1_hit or pos.tp2_hit) else "Позиция закрыта⏱️"
    elif pos.exit_reason == "FATAL_ERROR":
        close_line = "Позиция закрыта❌⚠️"
    else:
        close_line = ""

    commission_value = pos.commission
    commission_str = f"{commission_value:.2f}$"

    msg = (
        "СИГНАЛ❗️\n\n"
        f"🪙{pos.symbol}\n"
        f"📌 Стратегия: {pos.strategy}\n\n"
        f"{side_icon}\n"
        f"⚖️ Риск: {int(RISK_PER_TRADE*100)}% | 🚀Плечо: {pos.leverage}x\n"
        f"🎫Вход: {pos.entry_price:.6f}\n\n"
        f"{tp1_line}\n"
        f"{tp2_line}\n"
        f"{tp3_line}\n"
        f"{sl_line}\n\n"
        f"💵Баланс: ${accounting.balance:.2f}\n"
        f"📊PnL сделки: {pos.realized_pnl:+.2f}$ ({pos.realized_pct:+.2f}%)\n"
        f"🎰Комиссия: {commission_str}\n"
        f"{close_line}"
    )

    return msg


# =========================================================
# 16. TELEGRAM SENDER
# =========================================================

class TelegramMessenger:
    def __init__(self, bot_token: str, channel_id: str):
        self.bot = Bot(token=bot_token)
        self.channel_id = channel_id
        self.message_id: Optional[int] = None

    async def send_startup_message(self):
        msg = await self.bot.send_message(
            chat_id=self.channel_id,
            text="🤖Бот запущен🤖"
        )
        self.message_id = msg.message_id

    async def send_signal(self, text: str):
        msg = await self.bot.send_message(
            chat_id=self.channel_id,
            text=text
        )
        self.message_id = msg.message_id

    async def edit_signal(self, text: str):
        if self.message_id is None:
            return

        try:
            await self.bot.edit_message_text(
                chat_id=self.channel_id,
                message_id=self.message_id,
                text=text
            )
        except Exception as e:
            if "message is not modified" in str(e).lower():
                return
            logger.exception("Telegram edit error: %s", e)

    async def send_error_once(self, text: str):
        try:
            await self.bot.send_message(chat_id=self.channel_id, text=text)
        except Exception as e:
            logger.exception("Telegram send error: %s", e)


tg = TelegramMessenger(
    bot_token=BOT_TOKEN,
    channel_id=CHAT_ID
)


# =========================================================
# 17. ORCHESTRATOR
# =========================================================

class Orchestrator:
    def __init__(self):
        self.market_data = market_data
        self.universe = UNIVERSE_SYMBOLS
        self.screener = volatility_screener
        self.strategy = strategy
        self.engine = trade_engine
        self.accounting = accounting
        self.integrator = integrator
        self.tg = tg

        self.active_position: Optional[Position] = None
        self.execution_lock = False

        self.last_scan_time = 0
        self.cached_hot_symbols: List[str] = []

        self.loop = asyncio.get_event_loop()

        self.last_error_text: Optional[str] = None
        self.last_error_ts = 0

        self.restore_position_state()

    def restore_position_state(self):
        try:
            if not os.path.exists(STATE_FILE):
                return

            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            pos = Position.from_dict(data)
            self.active_position = pos
            self.execution_lock = True

            logger.info("[STATE] position restored")
        except Exception as e:
            logger.exception("[STATE RESTORE ERROR] %s", e)

    def save_position_state(self):
        try:
            if self.active_position is None:
                if os.path.exists(STATE_FILE):
                    os.remove(STATE_FILE)
                return

            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.active_position.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.exception("[STATE SAVE ERROR] %s", e)

    async def safe_scan(self):
        return await self.loop.run_in_executor(None, self.screener.scan)

    async def safe_build_context(self, symbol: str):
        return await self.loop.run_in_executor(None, build_market_context, symbol)

    async def safe_get_price(self, symbol: str):
        return await self.loop.run_in_executor(None, self.market_data.get_last_price, symbol)

    async def shutdown_with_error(self, exc: Exception):
        logger.exception("[FATAL] %s", exc)

        if self.active_position is not None and not self.active_position.closed:
            try:
                if not DRY_RUN:
                    executor.close_position(self.active_position)

                self.active_position.closed = True
                self.active_position.exit_reason = "FATAL_ERROR"
                self.active_position.exit_price = self.market_data.get_last_price(self.active_position.symbol)

                await self.tg.send_error_once(
                    f"❌ Критическая ошибка: {type(exc).__name__}: {exc}\n"
                    f"Позиция закрыта. Бот остановлен."
                )

                try:
                    await self.tg.edit_signal(format_signal_message(self.active_position))
                except Exception:
                    pass

            except Exception as close_err:
                await self.tg.send_error_once(
                    f"❌ Критическая ошибка: {type(exc).__name__}: {exc}\n"
                    f"⚠️ Не удалось гарантированно закрыть позицию: {close_err}\n"
                    f"Бот остановлен."
                )
        else:
            await self.tg.send_error_once(
                f"❌ Критическая ошибка: {type(exc).__name__}: {exc}\n"
                f"Бот остановлен."
            )

        self.save_position_state()
        raise exc

    async def run(self):
        await self.tg.send_startup_message()

        while True:
            try:
                if self.active_position is not None and not DRY_RUN:
                    try:
                        if not executor.has_position(self.active_position.symbol):
                            logger.info("[SYNC] position not found on exchange → force close")

                            self.active_position.closed = True
                            self.active_position.exit_reason = "EXCHANGE_SYNC"

                            await self.tg.edit_signal(format_signal_message(self.active_position))

                            self.active_position = None
                            self.execution_lock = False
                            self.save_position_state()
                            continue

                    except Exception as e:
                        await self.shutdown_with_error(e)

                if self.active_position is None and not self.execution_lock:
                    if time.time() - self.last_scan_time > 10:
                        self.cached_hot_symbols = await self.safe_scan()
                        self.last_scan_time = time.time()

                if self.active_position is None and not self.execution_lock:
                    for symbol in self.cached_hot_symbols:
                        try:
                            context = await self.safe_build_context(symbol)

                            if context["last_price"] is None:
                                continue

                            signal = self.strategy.generate(context)
                            if signal is None:
                                continue

                            size_usd = self.accounting.calc_position_size()
                            entry = signal.entry_price

                            if signal.side == "LONG":
                                tp1 = entry * (1 + TP1_PCT / LEVERAGE)
                                tp2 = entry * (1 + TP2_PCT / LEVERAGE)
                                tp3 = entry * (1 + TP3_PCT / LEVERAGE)
                                sl  = entry * (1 - SL_PCT / LEVERAGE)
                            else:
                                tp1 = entry * (1 - TP1_PCT / LEVERAGE)
                                tp2 = entry * (1 - TP2_PCT / LEVERAGE)
                                tp3 = entry * (1 - TP3_PCT / LEVERAGE)
                                sl  = entry * (1 + SL_PCT / LEVERAGE)

                            pos = Position(
                                symbol=signal.symbol,
                                side=signal.side,
                                entry_price=entry,
                                entry_time=time.time(),
                                size_usd=size_usd,
                                strategy=signal.strategy,
                                tp1=tp1,
                                tp2=tp2,
                                tp3=tp3,
                                sl=sl
                            )

                            self.execution_lock = True
                            self.active_position = pos

                            if not DRY_RUN:
                                executor.open_position(pos)

                            self.save_position_state()
                            await self.tg.send_signal(format_signal_message(pos))
                            break

                        except Exception as e:
                            await self.shutdown_with_error(e)

                    if DEBUG and self.cached_hot_symbols:
                        try:
                            self.strategy.print_debug()
                        except Exception:
                            pass

                else:
                    pos = self.active_position
                    if pos is None:
                        await asyncio.sleep(POLL_SECONDS)
                        continue

                    price = await self.safe_get_price(pos.symbol)

                    if price is None:
                        await asyncio.sleep(1)
                        continue

                    event = self.integrator.on_price_update(pos, price, time.time())

                    direction = 1 if pos.side == "LONG" else -1
                    move_pct = (price - pos.entry_price) / pos.entry_price * direction
                    margin = pos.size_usd / max(pos.leverage, 1)
                    pos.realized_pnl = margin * move_pct * pos.leverage
                    pos.realized_pct = move_pct * pos.leverage * 100

                    if event:
                        self.save_position_state()
                        await self.tg.edit_signal(format_signal_message(pos))

                    if pos.closed:
                        self.save_position_state()
                        await self.tg.edit_signal(format_signal_message(pos))
                        self.active_position = None
                        self.execution_lock = False
                        self.save_position_state()

                await asyncio.sleep(POLL_SECONDS)

            except KeyboardInterrupt:
                raise

            except Exception as e:
                await self.shutdown_with_error(e)


# =========================================================
# 18. MAIN
# =========================================================

async def main():
    if "PASTE_" in API_KEY or "PASTE_" in API_SECRET:
        raise RuntimeError("Сначала вставь Bitunix API_KEY и API_SECRET.")
    if "PASTE_" in BOT_TOKEN:
        raise RuntimeError("Сначала вставь Telegram BOT_TOKEN.")
    if "PASTE_" in str(CHAT_ID):
        raise RuntimeError("Сначала вставь Telegram CHAT_ID.")

    logger.info("Universe size: %s", len(UNIVERSE_SYMBOLS))
    logger.info("Sample universe: %s", UNIVERSE_SYMBOLS[:10])

    orchestrator = Orchestrator()
    await orchestrator.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
        sys.exit(0)