import ccxt
import pandas as pd
import json
import os
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# ── Paramètres ────────────────────────────────────────────────────────────────
CAPITAL_TOTAL          = 100.0
MAX_POSITIONS          = 5
POSITION_SIZE_MIN      = 15.0    # USDC minimum par position
TRAILING_STOP_PCT      = 2.0     # % depuis le plus haut
STOP_LOSS_PCT          = 2.0     # % depuis l'entrée
MAX_DAILY_LOSS_USDC    = 10.0    # perte journalière max en USDC
RSI_MAX_ENTRY          = 65      # ne pas entrer si RSI > cette valeur
VOLUME_MULTIPLIER      = 2.0     # volume min vs moyenne pour entrer
TIMEFRAME_SHORT        = "1h"
TIMEFRAME_LONG         = "4h"
PERSISTENCE_FILE       = "positions.json"

PARIS_TZ = timezone(timedelta(hours=2))


class TradingStrategy:
    def __init__(self, binance_key: str = None, binance_secret: str = None):
        self.exchange = ccxt.binance({
            "apiKey":          binance_key or "",
            "secret":          binance_secret or "",
            "enableRateLimit": True,
            "options":         {"defaultType": "spot"},
        })
        self.positions:          dict  = {}
        self.capital:            float = CAPITAL_TOTAL
        self.pnl:                float = 0.0
        self.daily_start_pnl:    float = 0.0
        self.total_trades:       int   = 0
        self.wins:               int   = 0
        self.losses:             int   = 0
        self._usdc_pairs:        list  = []
        self._last_pair_refresh: float = 0
        self._morning_done_date: str   = ""

        self._load_state()

    # ── Persistance ───────────────────────────────────────────────────────────
    def _save_state(self):
        state = {
            "positions":         self.positions,
            "capital":           self.capital,
            "pnl":               self.pnl,
            "daily_start_pnl":   self.daily_start_pnl,
            "total_trades":      self.total_trades,
            "wins":              self.wins,
            "losses":            self.losses,
            "morning_done_date": self._morning_done_date,
            "saved_at":          datetime.utcnow().isoformat(),
        }
        try:
            with open(PERSISTENCE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.error(f"Erreur sauvegarde : {e}")

    def _load_state(self):
        if not os.path.exists(PERSISTENCE_FILE):
            log.info("Aucun état persisté — démarrage à zéro")
            return
        try:
            with open(PERSISTENCE_FILE, "r") as f:
                state = json.load(f)
            self.positions         = state.get("positions", {})
            self.capital           = state.get("capital", CAPITAL_TOTAL)
            self.pnl               = state.get("pnl", 0.0)
            self.daily_start_pnl   = state.get("daily_start_pnl", 0.0)
            self.total_trades      = state.get("total_trades", 0)
            self.wins              = state.get("wins", 0)
            self.losses            = state.get("losses", 0)
            self._morning_done_date = state.get("morning_done_date", "")
            log.info(f"État rechargé — {len(self.positions)} position(s)")
        except Exception as e:
            log.error(f"Erreur chargement état : {e}")

    def reset_daily_pnl(self):
        self.daily_start_pnl = self.pnl
        self._save_state()
        log.info(f"Reset G/P journalier — base : {self.daily_start_pnl:.2f} USDC")

    # ── Drawdown ──────────────────────────────────────────────────────────────
    def _daily_drawdown_reached(self) -> bool:
        return (self.pnl - self.daily_start_pnl) <= -MAX_DAILY_LOSS_USDC

    # ── Taille de position dynamique ──────────────────────────────────────────
    def _position_size(self) -> float:
        slots_libres = MAX_POSITIONS - len(self.positions)
        if slots_libres <= 0:
            return 0.0
        taille = self.capital / slots_libres
        return taille if taille >= POSITION_SIZE_MIN else 0.0

    # ── Paires USDC ───────────────────────────────────────────────────────────
    def _get_usdc_pairs(self) -> list:
        import time
        now = time.time()
        if self._usdc_pairs and (now - self._last_pair_refresh) < 3600:
            return self._usdc_pairs
        try:
            markets = self.exchange.load_markets()
            pairs = [
                s for s, m in markets.items()
                if s.endswith("/USDC") and m.get("active") and m.get("spot")
            ]
            self._usdc_pairs        = pairs
            self._last_pair_refresh = now
            log.info(f"{len(pairs)} paires USDC actives")
        except Exception as e:
            log.error(f"Erreur chargement marchés : {e}")
        return self._usdc_pairs

    # ── OHLCV ─────────────────────────────────────────────────────────────────
    def _fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 60) -> pd.DataFrame | None:
        try:
            data = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df   = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "vol"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms")
            return df
        except Exception:
            return None

    # ── Calcul RSI ────────────────────────────────────────────────────────────
    def _rsi(self, series: pd.Series, period: int = 14) -> float:
        delta  = series.diff()
        gain   = delta.clip(lower=0).rolling(period).mean()
        loss   = (-delta.clip(upper=0)).rolling(period).mean()
        rs     = gain / loss.replace(0, 1e-10)
        rsi    = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1])

    # ── Analyse signal entrée (1h + 4h + RSI + volume) ───────────────────────
    def _analyze_signal(self, symbol: str) -> dict:
        result = {"valid": False, "price": 0.0, "details": ""}

        # ── Timeframe 1h ──────────────────────────────────────────────────────
        df1 = self._fetch_ohlcv(symbol, TIMEFRAME_SHORT, limit=60)
        if df1 is None or len(df1) < 50:
            result["details"] = "Données 1h insuffisantes"
            return result

        df1["ema20"]  = df1["close"].ewm(span=20).mean()
        df1["ema50"]  = df1["close"].ewm(span=50).mean()
        df1["vol_ma"] = df1["vol"].rolling(20).mean()
        last1 = df1.iloc[-1]

        ema1h_ok   = last1["ema20"] > last1["ema50"]
        close1h_ok = last1["close"] > last1["ema20"]
        vol_ratio  = last1["vol"] / last1["vol_ma"] if last1["vol_ma"] > 0 else 0
        vol_ok     = vol_ratio >= VOLUME_MULTIPLIER
        rsi_val    = self._rsi(df1["close"])
        rsi_ok     = rsi_val < RSI_MAX_ENTRY

        # ── Timeframe 4h ──────────────────────────────────────────────────────
        df4 = self._fetch_ohlcv(symbol, TIMEFRAME_LONG, limit=60)
        ema4h_ok = False
        ema4h_str = "Données insuffisantes"
        if df4 is not None and len(df4) >= 50:
            df4["ema20"] = df4["close"].ewm(span=20).mean()
            df4["ema50"] = df4["close"].ewm(span=50).mean()
            last4    = df4.iloc[-1]
            ema4h_ok = last4["ema20"] > last4["ema50"]
            ema4h_str = f"EMA20 ({last4['ema20']:.4f}) {'>' if ema4h_ok else '<'} EMA50 ({last4['ema50']:.4f})"

        result["price"] = last1["close"]
        result["valid"] = ema1h_ok and close1h_ok and vol_ok and rsi_ok and ema4h_ok
        result["details"] = "\n".join([
            f"{'✅' if ema1h_ok   else '❌'} [1h] EMA20 ({last1['ema20']:.4f}) {'>' if ema1h_ok else '<'} EMA50 ({last1['ema50']:.4f})",
            f"{'✅' if close1h_ok else '❌'} [1h] Clôture ({last1['close']:.4f}) {'>' if close1h_ok else '<'} EMA20",
            f"{'✅' if vol_ok     else '❌'} [1h] Volume ×{vol_ratio:.2f} (seuil ×{VOLUME_MULTIPLIER})",
            f"{'✅' if rsi_ok     else '❌'} [1h] RSI {rsi_val:.1f} ({'OK' if rsi_ok else f'> {RSI_MAX_ENTRY} — surachat'})",
            f"{'✅' if ema4h_ok   else '❌'} [4h] {ema4h_str}",
        ])
        return result

    # ── Analyse matin (1h + 4h, sans volume ni RSI) ───────────────────────────
    def _analyze_morning(self, symbol: str) -> dict:
        result = {"valid": False, "price": 0.0, "details": ""}

        df1 = self._fetch_ohlcv(symbol, TIMEFRAME_SHORT, limit=60)
        if df1 is None or len(df1) < 50:
            result["details"] = "Données 1h insuffisantes"
            return result

        df1["ema20"] = df1["close"].ewm(span=20).mean()
        df1["ema50"] = df1["close"].ewm(span=50).mean()
        last1 = df1.iloc[-1]

        ema1h_ok   = last1["ema20"] > last1["ema50"]
        close1h_ok = last1["close"] > last1["ema20"]

        df4 = self._fetch_ohlcv(symbol, TIMEFRAME_LONG, limit=60)
        ema4h_ok  = False
        ema4h_str = "Données insuffisantes"
        if df4 is not None and len(df4) >= 50:
            df4["ema20"] = df4["close"].ewm(span=20).mean()
            df4["ema50"] = df4["close"].ewm(span=50).mean()
            last4     = df4.iloc[-1]
            ema4h_ok  = last4["ema20"] > last4["ema50"]
            ema4h_str = f"EMA20 ({last4['ema20']:.4f}) {'>' if ema4h_ok else '<'} EMA50 ({last4['ema50']:.4f})"

        result["price"] = last1["close"]
        result["valid"] = ema1h_ok and close1h_ok and ema4h_ok
        result["details"] = "\n".join([
            f"{'✅' if ema1h_ok   else '❌'} [1h] EMA20 {'>' if ema1h_ok else '<'} EMA50",
            f"{'✅' if close1h_ok else '❌'} [1h] Clôture {'>' if close1h_ok else '<'} EMA20",
            f"{'✅' if ema4h_ok   else '❌'} [4h] {ema4h_str}",
        ])
        return result

    # ── Vérification SL robuste ───────────────────────────────────────────────
    def _check_sl_triggered(self, symbol: str, current_price: float, sl_level: float) -> tuple[bool, float]:
        if current_price <= sl_level:
            return True, current_price
        try:
            df_1m = self._fetch_ohlcv(symbol, timeframe="1m", limit=3)
            if df_1m is not None and len(df_1m) >= 2:
                last_low = df_1m.iloc[-2]["low"]
                if last_low <= sl_level:
                    log.warning(f"[SL] {symbol} — low 1m ({last_low:.4f}) sous SL ({sl_level:.4f})")
                    return True, sl_level
        except Exception as e:
            log.error(f"Erreur check low 1m {symbol} : {e}")
        return False, current_price

    # ── Gain sécurisé ─────────────────────────────────────────────────────────
    def _secured_gain(self, pos: dict) -> dict:
        ts_price    = pos["trailing_stop"]
        entry       = pos["entry"]
        qty         = pos["qty"]
        secured_pnl = (ts_price - entry) * qty
        secured_pct = (ts_price - entry) / entry * 100
        return {"ts_price": ts_price, "secured_pnl": secured_pnl, "secured_pct": secured_pct}

    # ── Ouverture ─────────────────────────────────────────────────────────────
    def _open_position(self, symbol: str, analysis: dict) -> str:
        if symbol in self.positions:
            return ""
        if len(self.positions) >= MAX_POSITIONS:
            return ""
        size = self._position_size()
        if size <= 0:
            return ""

        price = analysis["price"]
        qty   = size / price
        opened_at = datetime.utcnow().isoformat()

        self.positions[symbol] = {
            "symbol":        symbol,
            "entry":         price,
            "qty":           qty,
            "size_usdc":     size,
            "highest":       price,
            "stop_loss":     round(price * (1 - STOP_LOSS_PCT    / 100), 8),
            "trailing_stop": round(price * (1 - TRAILING_STOP_PCT / 100), 8),
            "opened_at":     opened_at,
        }
        self.capital -= size
        self._save_state()
        log.info(f"[PAPER] ACHAT {symbol} @ {price:.4f} | {size:.2f} USDC")

        sl_price = price * (1 - STOP_LOSS_PCT / 100)
        ts_init  = price * (1 - TRAILING_STOP_PCT / 100)
        opened_fmt = datetime.fromisoformat(opened_at).strftime("%d/%m %H:%M")

        return (
            f"✅ *Entrée — {symbol}*\n"
            f"Ouvert le {opened_fmt}\n"
            f"Prix : `{price:.4f}` | Taille : `{size:.2f}` USDC\n"
            f"SL : `{sl_price:.4f}` (-{STOP_LOSS_PCT}%) | TS : `{ts_init:.4f}` (-{TRAILING_STOP_PCT}%)\n\n"
            f"📐 *Signal :*\n{analysis['details']}"
        )

    def _update_trailing_stop(self, pos: dict, current_price: float):
        if current_price > pos["highest"]:
            pos["highest"]       = current_price
            pos["trailing_stop"] = round(current_price * (1 - TRAILING_STOP_PCT / 100), 8)

    # ── Clôture ───────────────────────────────────────────────────────────────
    def _close_position(self, symbol: str, price: float, reason: str) -> str:
        pos = self.positions.pop(symbol, None)
        if not pos:
            return ""
        pnl      = (price - pos["entry"]) * pos["qty"]
        pnl_pct  = (price - pos["entry"]) / pos["entry"] * 100
        self.pnl     += pnl
        self.capital += pos["size_usdc"] + pnl
        self.total_trades += 1
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1
        self._save_state()
        log.info(f"[PAPER] CLÔTURE {symbol} @ {price:.4f} | G/P : {pnl:+.2f} USDC | {reason}")

        opened_at  = pos.get("opened_at", "")
        closed_at  = datetime.utcnow().isoformat()
        opened_fmt = datetime.fromisoformat(opened_at).strftime("%d/%m %H:%M") if opened_at else "—"
        closed_fmt = datetime.fromisoformat(closed_at).strftime("%d/%m %H:%M")

        return (
            f"❌ *Clôture — {symbol}* — {reason}\n"
            f"Ouvert {opened_fmt} → Clôturé {closed_fmt}\n"
            f"Entrée : `{pos['entry']:.4f}` | Sortie : `{price:.4f}`\n"
            f"G/P : `{pnl:+.2f}` USDC (`{pnl_pct:+.2f}%`)"
        )

    # ── Analyse matin ─────────────────────────────────────────────────────────
    def morning_analysis(self) -> list[str]:
        today = datetime.now(PARIS_TZ).date().isoformat()
        if self._morning_done_date == today:
            return []
        self._morning_done_date = today
        self.reset_daily_pnl()
        self._save_state()

        if not self.positions:
            return ["☀️ *Analyse matinale* — Aucune position ouverte."]

        messages = ["☀️ *Analyse matinale des positions*\n"]
        for symbol in list(self.positions.keys()):
            pos      = self.positions.get(symbol)
            analysis = self._analyze_morning(symbol)
            try:
                price = self.exchange.fetch_ticker(symbol)["last"]
            except Exception:
                price = pos["entry"]

            pnl_pct = (price - pos["entry"]) / pos["entry"] * 100
            secured = self._secured_gain(pos)
            verdict = "✅ *Garder*" if analysis["valid"] else "❌ *Abandonner*"

            opened_fmt = ""
            if pos.get("opened_at"):
                opened_fmt = datetime.fromisoformat(pos["opened_at"]).strftime("%d/%m %H:%M")

            msg = (
                f"─────────────────\n"
                f"📌 `{symbol}` | Ouvert le {opened_fmt}\n"
                f"G/P actuel : `{pnl_pct:+.2f}%` | 💵 Gain si TS : `{secured['secured_pnl']:+.2f}` USDC\n\n"
                f"*Indicateurs :*\n{analysis['details']}\n\n"
                f"Verdict : {verdict}"
            )
            messages.append(msg)

            if not analysis["valid"]:
                close_msg = self._close_position(symbol, price, "Abandon matin — tendance invalide")
                if close_msg:
                    messages.append(close_msg)

        return messages

    # ── SCAN ──────────────────────────────────────────────────────────────────
    def scan(self) -> list[str]:
        alerts = []

        # Étape 1 — sorties toujours vérifiées en premier
        for symbol in list(self.positions.keys()):
            try:
                current_price = self.exchange.fetch_ticker(symbol)["last"]
                pos           = self.positions[symbol]
                self._update_trailing_stop(pos, current_price)

                sl_hit, sl_price = self._check_sl_triggered(symbol, current_price, pos["stop_loss"])
                if sl_hit:
                    msg = self._close_position(symbol, sl_price, "Stop Loss")
                    if msg:
                        alerts.append(msg)
                    continue

                ts_hit, ts_price = self._check_sl_triggered(symbol, current_price, pos["trailing_stop"])
                if ts_hit:
                    msg = self._close_position(symbol, ts_price, "Trailing Stop")
                    if msg:
                        alerts.append(msg)

            except Exception as e:
                log.error(f"Erreur mise à jour {symbol} : {e}")

        # Étape 2 — nouvelles entrées bloquées si drawdown
        if self._daily_drawdown_reached():
            log.warning("Drawdown journalier atteint — pas de nouvelles entrées")
            return alerts

        # Étape 3 — recherche de nouvelles entrées
        if len(self.positions) < MAX_POSITIONS and self._position_size() > 0:
            for symbol in self._get_usdc_pairs():
                if symbol in self.positions:
                    continue
                if len(self.positions) >= MAX_POSITIONS:
                    break
                try:
                    analysis = self._analyze_signal(symbol)
                    if analysis["valid"]:
                        msg = self._open_position(symbol, analysis)
                        if msg:
                            alerts.append(msg)
                except Exception as e:
                    log.debug(f"Skip {symbol} : {e}")

        return alerts

    # ── Getters ───────────────────────────────────────────────────────────────
    def get_stats(self) -> dict:
        return {
            "capital":      self.capital,
            "pnl":          self.pnl,
            "pnl_today":    self.pnl - self.daily_start_pnl,
            "total_trades": self.total_trades,
            "wins":         self.wins,
            "losses":       self.losses,
        }

    def get_positions(self) -> list:
        result = []
        for symbol, pos in self.positions.items():
            try:
                price   = self.exchange.fetch_ticker(symbol)["last"]
                pnl_pct = (price - pos["entry"]) / pos["entry"] * 100
            except Exception:
                pnl_pct = 0.0
                price   = pos["entry"]
            secured    = self._secured_gain(pos)
            opened_fmt = ""
            if pos.get("opened_at"):
                opened_fmt = datetime.fromisoformat(pos["opened_at"]).strftime("%d/%m %H:%M")
            result.append({
                "symbol":      symbol,
                "entry":       pos["entry"],
                "current":     price,
                "pnl_pct":     pnl_pct,
                "pnl_usdc":    (price - pos["entry"]) * pos["qty"],
                "secured_pnl": secured["secured_pnl"],
                "secured_pct": secured["secured_pct"],
                "ts_price":    secured["ts_price"],
                "opened_at":   opened_fmt,
            })
        return result
