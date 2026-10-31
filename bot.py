#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Binance Early-Expansion Sniper Bot — Paper Trading Edition
نظام درجات متعدد المصادر + مرشح جودة + إدارة مخاطر + محاكاة تنفيذ
بيانات سوق حقيقية | صفقات محاكاة محلية | بدون حساب أو مفاتيح
"""

import time, json, math, logging
import requests
from datetime import datetime, timezone
from collections import deque

# ================== الإعدادات ==================
CONFIG = {
    # --- الاتصال (بيانات فقط، قراءة عامة) ---
    "REST_BASE": "https://api.binance.com",

    # --- رأس المال والمخاطر ---
    "QUOTE_ASSET": "USDT",
    "PAPER_START_BALANCE": 10000.0,   # رصيد المحاكاة الابتدائي
    "RISK_PER_TRADE_PCT": 10.0,       # نسبة من الرصيد لكل صفقة
    "MAX_OPEN_POSITIONS": 3,
    "MAX_DAILY_TRADES": 10,
    "MAX_CONSECUTIVE_LOSSES": 3,
    "DAILY_MAX_LOSS_PCT": 5.0,
    "TAKE_PROFIT_PCT": 4.0,
    "STOP_LOSS_PCT": 2.0,
    "TRAILING_STOP_PCT": 1.5,

    # --- عتبات نظام الدرجات ---
    "ENTRY_SCORE_THRESHOLD": 70,
    "MIN_INDEPENDENT_SIGNALS": 3,
    "RISK_PENALTY_MAX": 40,

    # --- مرشح الجودة ---
    "MIN_QUOTE_VOLUME_24H": 5_000_000,
    "MAX_SPREAD_PCT": 0.15,
    "MIN_ORDERBOOK_DEPTH_USDT": 50_000,
    "MAX_RECENT_PUMP_PCT": 25,
    "MIN_LISTING_AGE_HOURS": 48,
    "MIN_TRADES_FOR_DISPERSION": 300,

    # --- كشف التوسع المبكر ---
    "VOLUME_SPIKE_X": 2.5,
    "VOLUME_GROWTH_BARS": 3,
    "SCAN_INTERVAL_SEC": 15,
    "TOP_N_UNIVERSE": 120,
    "UNIVERSE_REFRESH_EVERY": 100,    # كل كم دورة مسح تُحدّث القائمة

    # --- رسوم المحاكاة ---
    "PAPER_FEE_PCT": 0.1,
}

# ================== السجلات ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("sniper.log", encoding="utf-8"),
              logging.StreamHandler()]
)
log = logging.getLogger("sniper")

SIGNAL_LOG = "signals.jsonl"
POSITIONS_FILE = "positions.json"
PAPER_STATE_FILE = "paper_state.json"

# ================== طبقة البيانات ==================
class BinanceData:
    def __init__(self, base):
        self.base = base
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "sniper-research/1.0"})

    def _get(self, path, params=None):
        for attempt in range(3):
            try:
                r = self.s.get(self.base + path, params=params, timeout=10)
                if r.status_code == 429:
                    time.sleep(2 ** attempt * 2)
                    continue
                if r.status_code == 418:   # حظر مؤقت — انتظر أطول
                    time.sleep(60)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                log.warning(f"GET {path} فشل ({attempt+1}/3): {e}")
                time.sleep(1 + attempt)
        return None

    def exchange_info(self):
        return self._get("/api/v3/exchangeInfo")

    def ticker_24h(self):
        return self._get("/api/v3/ticker/24hr")

    def klines(self, symbol, interval="1m", limit=60):
        return self._get("/api/v3/klines",
                         {"symbol": symbol, "interval": interval, "limit": limit})

    def depth(self, symbol, limit=100):
        return self._get("/api/v3/depth", {"symbol": symbol, "limit": limit})

    def recent_trades(self, symbol, limit=500):
        return self._get("/api/v3/trades", {"symbol": symbol, "limit": limit})

# ================== محرك الإشارات (نظام الدرجات) ==================
class SignalEngine:
    WEIGHTS = {
        "volume_spike":      25,
        "volume_growth":     15,
        "trade_dispersion":  15,
        "depth_improving":   15,
        "price_volume_sync": 15,
        "orderbook_absorb":  15,
    }

    @staticmethod
    def _depth_usdt(d):
        if not d or not d.get("bids") or not d.get("asks"):
            return 0.0, 0.0
        bid = sum(float(p) * float(q) for p, q in d["bids"][:50])
        ask = sum(float(p) * float(q) for p, q in d["asks"][:50])
        return bid, ask

    def score(self, symbol, klines, trades, depth_prev, depth_now, ticker):
        pts = {}

        vols = [float(k[5]) for k in klines[:-1]]
        if len(vols) < 20:
            return {"symbol": symbol, "score": 0, "points": {},
                    "independent_signals": 0, "risk_penalty": 0,
                    "ts": datetime.now(timezone.utc).isoformat()}

        # 1) ارتفاع الحجم الفوري مقابل المتوسط القصير
        avg = sum(vols[-20:]) / 20
        cur = float(klines[-1][5])
        if avg > 0 and cur / avg >= CONFIG["VOLUME_SPIKE_X"]:
            pts["volume_spike"] = self.WEIGHTS["volume_spike"]

        # 2) نمو حجم متدرج عبر عدة فترات (لا قفزة واحدة)
        n = CONFIG["VOLUME_GROWTH_BARS"]
        if len(vols) >= n + 5:
            last_n = vols[-n:]
            base = vols[-(n + 5):-n]
            bavg = sum(base) / len(base) if base else 0
            if bavg > 0 and all(v > bavg * 1.3 for v in last_n):
                pts["volume_growth"] = self.WEIGHTS["volume_growth"]

        # 3) توزع الصفقات: صغار/متوسطة، لا مهيمن واحد
        if trades:
            qts = sorted(float(t["qty"]) for t in trades)
            total = sum(qts)
            if total > 0 and len(trades) >= CONFIG["MIN_TRADES_FOR_DISPERSION"]:
                top10_share = sum(qts[-10:]) / total
                if top10_share < 0.35:
                    pts["trade_dispersion"] = self.WEIGHTS["trade_dispersion"]

        # 4) تحسن عمق دفتر الأوامر + امتصاص العروض
        bid_prev, ask_prev = self._depth_usdt(depth_prev)
        bid_now, ask_now = self._depth_usdt(depth_now)
        d_prev, d_now = bid_prev + ask_prev, bid_now + ask_now
        if d_prev > 0 and d_now / d_prev >= 1.3:
            pts["depth_improving"] = self.WEIGHTS["depth_improving"]
            if ask_prev > 0 and ask_now >= ask_prev * 0.8:
                pts["orderbook_absorb"] = self.WEIGHTS["orderbook_absorb"]

        # 5) تزامن السعر مع الحجم (صعود منظم لا شمعة عمودية)
        price_chg = (float(klines[-1][4]) - float(klines[-5][4])) / float(klines[-5][4]) * 100
        vol_chg = (vols[-1] - vols[-5]) / vols[-5] * 100 if vols[-5] > 0 else 0
        if 0.3 < price_chg < 6 and vol_chg > 30:
            pts["price_volume_sync"] = self.WEIGHTS["price_volume_sync"]

        # --- عقوبات المخاطر ---
        risk_penalty = 0
        body = (float(klines[-1][4]) - float(klines[-1][1])) / float(klines[-1][1]) * 100
        if body > 10:
            risk_penalty += 25      # شمعة عمودية مفاجئة = دخول متأخر
        chg = float(ticker.get("priceChangePercent", 0) or 0)
        if chg > CONFIG["MAX_RECENT_PUMP_PCT"]:
            risk_penalty += 30      # مضخة بلا نمو سيولة

        return {
            "symbol": symbol,
            "score": max(0, sum(pts.values()) - risk_penalty),
            "points": pts,
            "independent_signals": len(pts),
            "risk_penalty": risk_penalty,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

# ================== مرشح جودة العملة ==================
class QualityFilter:
    def __init__(self, data: BinanceData):
        self.data = data
        self._spread_cache = {}

    def spread_pct(self, depth):
        try:
            bid = float(depth["bids"][0][0]); ask = float(depth["asks"][0][0])
            return (ask - bid) / ask * 100
        except (IndexError, KeyError, TypeError):
            return 999.0

    def depth_usdt(self, depth):
        b, a = SignalEngine._depth_usdt(depth)
        return b + a

    def passes(self, symbol, ticker, listing_ts_map):
        qv = float(ticker.get("quoteVolume", 0) or 0)
        if qv < CONFIG["MIN_QUOTE_VOLUME_24H"]:
            return False, f"سيولة 24h منخفضة ({qv:,.0f})"

        d = self.data.depth(symbol)
        if not d:
            return False, "فشل جلب دفتر الأوامر"
        sp = self.spread_pct(d)
        if sp > CONFIG["MAX_SPREAD_PCT"]:
            return False, f"سبريد واسع ({sp:.3f}%)"
        dp = self.depth_usdt(d)
        if dp < CONFIG["MIN_ORDERBOOK_DEPTH_USDT"]:
            return False, f"عمق ضعيف (${dp:,.0f})"

        ts = listing_ts_map.get(symbol)
        if ts and (time.time() * 1000 - ts) < CONFIG["MIN_LISTING_AGE_HOURS"] * 3600 * 1000:
            return False, "إدراج جديد جداً (فتح رموز قريب)"

        return True, "OK"

# ================== محاكاة التداول الورقي ==================
class PaperExecutor:
    """بيانات حقيقية + صفقات محاكاة على سعر السوق الفعلي"""
    def __init__(self):
        try:
            self.state = json.load(open(PAPER_STATE_FILE))
        except Exception:
            self.state = {"balances": {CONFIG["QUOTE_ASSET"]: CONFIG["PAPER_START_BALANCE"]},
                          "orders": []}

    def _save(self):
        json.dump(self.state, open(PAPER_STATE_FILE, "w"))

    def balance(self, asset):
        return self.state["balances"].get(asset, 0.0)

    def total_equity(self):
        return self.state["balances"].get(CONFIG["QUOTE_ASSET"], 0.0)

    def buy(self, symbol, quantity, price):
        cost = quantity * price * (1 + CONFIG["PAPER_FEE_PCT"] / 100)
        bal = self.balance(CONFIG["QUOTE_ASSET"])
        if cost > bal:
            log.warning(f"[محاكاة] رصيد غير كافٍ: يحتاج {cost:.2f} / متاح {bal:.2f}")
            return {"status": "REJECTED", "msg": "insufficient balance"}
        base = symbol[:-len(CONFIG["QUOTE_ASSET"])]
        self.state["balances"][CONFIG["QUOTE_ASSET"]] = bal - cost
        self.state["balances"][base] = self.state["balances"].get(base, 0) + quantity
        self.state["orders"].append({
            "side": "BUY", "symbol": symbol, "qty": quantity, "price": price,
            "ts": datetime.now(timezone.utc).isoformat()})
        self._save()
        log.info(f"[محاكاة] شراء {quantity:.6f} {symbol} @ {price:.8f}")
        return {"status": "FILLED"}

    def sell(self, symbol, quantity, price):
        base = symbol[:-len(CONFIG["QUOTE_ASSET"])]
        held = self.state["balances"].get(base, 0)
        if held + 1e-9 < quantity:
            log.warning(f"[محاكاة] كمية غير كافية للبيع: {held} < {quantity}")
            return {"status": "REJECTED"}
        proceeds = quantity * price * (1 - CONFIG["PAPER_FEE_PCT"] / 100)
        self.state["balances"][base] = held - quantity
        self.state["balances"][CONFIG["QUOTE_ASSET"]] = \
            self.balance(CONFIG["QUOTE_ASSET"]) + proceeds
        self.state["orders"].append({
            "side": "SELL", "symbol": symbol, "qty": quantity, "price": price,
            "ts": datetime.now(timezone.utc).isoformat()})
        self._save()
        log.info(f"[محاكاة] بيع {quantity:.6f} {symbol} @ {price:.8f} "
                 f"| رصيد {CONFIG['QUOTE_ASSET']} = {self.balance(CONFIG['QUOTE_ASSET']):.2f}")
        return {"status": "FILLED"}

# ================== إدارة المخاطر ==================
class RiskManager:
    def __init__(self):
        self.consecutive_losses = 0
        self.daily_trades = 0
        self.daily_pnl_pct = 0.0
        self.day = datetime.now(timezone.utc).date()
        self.positions = self._load()

    def _rollover(self):
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day, self.daily_trades, self.daily_pnl_pct = today, 0, 0.0

    def can_trade(self):
        self._rollover()
        if self.consecutive_losses >= CONFIG["MAX_CONSECUTIVE_LOSSES"]:
            return False, "قاطع: خسائر متتالية"
        if self.daily_pnl_pct <= -CONFIG["DAILY_MAX_LOSS_PCT"]:
            return False, "قاطع: خسارة يومية قصوى"
        if self.daily_trades >= CONFIG["MAX_DAILY_TRADES"]:
            return False, "حد الصفقات اليومي"
        if len(self.positions) >= CONFIG["MAX_OPEN_POSITIONS"]:
            return False, "أقصى عدد مراكز مفتوحة"
        return True, "OK"

    def open_position(self, symbol, entry, qty):
        self.positions[symbol] = {
            "entry": entry, "qty": qty, "peak": entry,
            "opened": datetime.now(timezone.utc).isoformat()}
        self.daily_trades += 1
        self._save()

    def close_position(self, symbol, exit_price):
        p = self.positions.pop(symbol, None)
        if not p:
            return 0.0
        pnl_pct = (exit_price - p["entry"]) / p["entry"] * 100
        if pnl_pct < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        self.daily_pnl_pct += pnl_pct
        self._save()
        return pnl_pct

    def _save(self):
        json.dump(self.positions, open(POSITIONS_FILE, "w"))

    def _load(self):
        try:
            return json.load(open(POSITIONS_FILE))
        except Exception:
            return {}

# ================== البوت الرئيسي ==================
class SniperBot:
    def __init__(self):
        self.data = BinanceData(CONFIG["REST_BASE"])
        self.engine = SignalEngine()
        self.qfilter = QualityFilter(self.data)
        self.exec = PaperExecutor()
        self.risk = RiskManager()
        self.depth_cache = {}
        self.universe = []
        self.listing_ts = {}
        self.symbol_meta = {}
        self._ticker_cache = {"data": None, "ts": 0}

    def _ticker(self):
        # تخزين مؤقت لقائمة الأسعار 24h لمدة 60 ثانية (توفير لحدود الطلبات)
        if time.time() - self._ticker_cache["ts"] > 60 or self._ticker_cache["data"] is None:
            self._ticker_cache["data"] = self.data.ticker_24h()
            self._ticker_cache["ts"] = time.time()
        return self._ticker_cache["data"]

    def build_universe(self):
        info = self.data.exchange_info()
        tickers = self._ticker()
        if not info or not tickers:
            log.error("فشل بناء قائمة المراقبة")
            return
        self.symbol_meta = {s["symbol"]: s for s in info["symbols"]}
        self.listing_ts = {s["symbol"]: s.get("onboardDate", 0)
                           for s in info["symbols"] if s.get("onboardDate")}
        q = CONFIG["QUOTE_ASSET"]
        ranked = []
        for t in tickers:
            sym = t["symbol"]
            if not sym.endswith(q):
                continue
            meta = self.symbol_meta.get(sym)
            if not meta or meta.get("status") != "TRADING":
                continue
            ranked.append((float(t["quoteVolume"]), sym))
        ranked.sort(reverse=True)
        self.universe = [s for _, s in ranked[:CONFIG["TOP_N_UNIVERSE"]]]
        log.info(f"قائمة المراقبة: {len(self.universe)} زوج")

    def log_signal(self, sig, executed, reason):
        rec = {**sig, "executed": executed, "reason": reason}
        try:
            with open(SIGNAL_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error(f"فشل كتابة سجل الإشارات: {e}")

    def scan_symbol(self, symbol):
        k = self.data.klines(symbol, "1m", 60)
        tr = self.data.recent_trades(symbol)
        dep = self.data.depth(symbol)
        if not k or not tr or not dep:
            return
        prev_dep = self.depth_cache.get(symbol)
        self.depth_cache[symbol] = dep
        if prev_dep is None:
            return   # أول قراءة — نبني المرجع فقط

        ticker = next((t for t in (self._ticker() or []) if t["symbol"] == symbol), None)
        if not ticker:
            return

        ok, why = self.qfilter.passes(symbol, ticker, self.listing_ts)
        if not ok:
            return

        sig = self.engine.score(symbol, k, tr, prev_dep, dep, ticker)

        qualifies = (sig["score"] >= CONFIG["ENTRY_SCORE_THRESHOLD"]
                     and sig["independent_signals"] >= CONFIG["MIN_INDEPENDENT_SIGNALS"]
                     and sig["risk_penalty"] < CONFIG["RISK_PENALTY_MAX"])

        if not qualifies:
            return

        if symbol in self.risk.positions:
            self.log_signal(sig, False, "مركز مفتوح بالفعل")
            return
        can, rwhy = self.risk.can_trade()
        if not can:
            self.log_signal(sig, False, rwhy)
            return

        price = float(dep["asks"][0][0])
        bal = self.exec.balance(CONFIG["QUOTE_ASSET"])
        budget = bal * CONFIG["RISK_PER_TRADE_PCT"] / 100
        qty = budget / price

        meta = self.symbol_meta.get(symbol, {})
        lot = next((f for f in meta.get("filters", [])
                    if f["filterType"] == "LOT_SIZE"), None)
        if lot:
            step = float(lot["stepSize"])
            qty = math.floor(qty / step) * step
        if qty <= 0:
            self.log_signal(sig, False, "كمية صفر بعد التقريب")
            return

        log.info(f"إشارة {symbol} | درجة={sig['score']} "
                 f"| مصادر={sig['independent_signals']} | نقاط={list(sig['points'].keys())}")
        order = self.exec.buy(symbol, qty, price)
        if order and order.get("status") == "FILLED":
            self.risk.open_position(symbol, price, qty)
            self.log_signal(sig, True, "تم الشراء (محاكاة)")
        else:
            self.log_signal(sig, False, f"فشل الأمر: {order}")

    def manage_positions(self):
        for symbol, p in list(self.risk.positions.items()):
            d = self.data.depth(symbol)
            if not d or not d["bids"]:
                continue
            bid = float(d["bids"][0][0])
            entry = p["entry"]
            p["peak"] = max(p["peak"], bid)
            pnl = (bid - entry) / entry * 100

            if pnl <= -CONFIG["STOP_LOSS_PCT"]:
                self._exit(symbol, bid, "وقف خسارة")
            elif pnl >= CONFIG["TAKE_PROFIT_PCT"]:
                self._exit(symbol, bid, "جني أرباح")
            elif pnl > 0 and (p["peak"] - bid) / p["peak"] * 100 >= CONFIG["TRAILING_STOP_PCT"]:
                self._exit(symbol, bid, "وقف متحرك")

    def _exit(self, symbol, price, reason):
        p = self.risk.positions.get(symbol)
        if not p:
            return
        o = self.exec.sell(symbol, p["qty"], price)
        pnl = self.risk.close_position(symbol, price)
        log.info(f"خروج {symbol} ({reason}): PnL={pnl:+.2f}%")

    def status_report(self):
        bal = self.exec.balance(CONFIG["QUOTE_ASSET"])
        open_pnl = []
        for sym, p in self.risk.positions.items():
            d = self.data.depth(sym)
            if d and d["bids"]:
                bid = float(d["bids"][0][0])
                open_pnl.append((bid - p["entry"]) / p["entry"] * 100)
        total = sum(open_pnl)
        log.info(f"[حالة] رصيد={bal:.2f} | مراكز مفتوحة={len(self.risk.positions)} "
                 f"| PnL مفتوح={total:+.2f}% | صفقات اليوم={self.risk.daily_trades}")

    def run(self):
        log.info("=== بدء تشغيل البوت (وضع المحاكاة — بيانات حقيقية) ===")
        bal = self.exec.balance(CONFIG["QUOTE_ASSET"])
        log.info(f"رصيد المحاكاة: {bal:.2f} {CONFIG['QUOTE_ASSET']}")
        self.build_universe()
        i = 0
        while True:
            try:
                for sym in self.universe:
                    self.scan_symbol(sym)
                self.manage_positions()
                i += 1
                if i % 10 == 0:
                    self.status_report()
                if i % CONFIG["UNIVERSE_REFRESH_EVERY"] == 0:
                    self.build_universe()
                time.sleep(CONFIG["SCAN_INTERVAL_SEC"])
                if i % 50 == 0:
                    log.info(f"[دورة {i}] مكتملة — النظام يعمل")
            except KeyboardInterrupt:
                log.info("إيقاف يدوي")
                break
            except Exception as e:
                log.error(f"خطأ في الحلقة: {e}", exc_info=True)
                time.sleep(10)

if __name__ == "__main__":
    SniperBot().run()
