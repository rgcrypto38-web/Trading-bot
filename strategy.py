import ccxt
import pandas as pd
import json
import os
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# PARAMÈTRES CONFIGURABLES
# ══════════════════════════════════════════════════════════════════════════════

# Capital et positions
CAPITAL_TOTAL       = 100.0
MAX_POSITIONS       = 5
POSITION_SIZE_MIN   = 15.0       # USDC minimum par position

# Stops dynamiques ATR
ATR_PERIOD          = 14         # Période ATR
ATR_SL_MULT         = 2.5        # SL = entrée - ATR × ce multiplicateur
ATR_TS_MULT         = 3.0        # TS = plus_haut - ATR × ce multiplicateur
SL_MIN_PCT          = 2.0        # Garde-fou plancher SL (%)
SL_MAX_PCT          = 8.0        # Garde-fou plafond SL (%)
TS_MIN_PCT          = 2.0        # Garde-fou plancher TS (%)
TS_MAX_PCT          = 10.0       # Garde-fou plafond TS (%)

# Take profit partiels
TP1_ATR_MULT        = 2.0        # TP1 = entrée + ATR × ce multiplicateur
TP2_ATR_MULT        = 4.0        # TP2 = entrée + ATR × ce multiplicateur
TP1_RATIO           = 0.30       # % de la position vendue au TP1
TP2_RATIO           = 0.30       # % de la position vendue au TP2
# Le reste (40%) reste en trailing stop

# Filtres d'entrée
RSI_MIN_ENTRY       = 50         # RSI minimum (évite les marchés mous)
RSI_MAX_ENTRY       = 85         # RSI maximum (évite surachat extrême)
VOLUME_MULTIPLIER   = 2.0        # Volume min vs moyenne 20 périodes
MIN_VOLUME_24H_USDC = 50_000_000 # Volume 24h minimum en USDC (liquidité)
MAX_SPREAD_PCT      = 0.15       # Spread max bid/ask en %
EMA_SLOPE_MIN       = 0.0        # Pente EMA20 minimale (>0 = haussière)

# Filtre régime BTC
BTC_EMA_PERIOD      = 200        # EMA BTC utilisée pour le filtre de régime
BTC_SYMBOL          = "BTC/USDC"

# Risk management
MAX_DAILY_LOSS_USDC = 10.0       # Perte journalière max en USDC

# Timeframes
TIMEFRAME_SHORT     = "1h"
TIMEFRAME_LONG      = "4h"

# Persistance
PERSISTENCE_FILE    = "positions.json"

PARIS_TZ = timezone(timedelta(hours=2))


# ══════════════════════════════════════════════════════════════════════════════
# STRATÉGIE
# ══════════════════════════════════════════════════════════════════════════════

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
        self.wins:                int  = 0
        self.losses:             int   = 0
        self.total_pnl_history:  list  = []  # [(pnl_usdc, pnl_pct)] pour métriques
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
            "total_pnl_history": self.total_pnl_history,
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
            self.positions          = state.get("positions", {})
            self.capital            = state.get("capital", CAPITAL_TOTAL)
            self.pnl                = state.get("pnl", 0.0)
            self.daily_start_pnl    = state.get("daily_start_pnl", 0.0)
            self.total_trades       = state.get("total_trades", 0)
            self.wins               = state.get("wins", 0)
            self.losses             = state.get("losses", 0)
            self.total_pnl_history  = state.get("total_pnl_history", [])
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

    # ── Formule G/P unifiée ───────────────────────────────────────────────────
    @staticmethod
    def _calc_pnl(size_usdc: float, entry: float, price: float) -> tuple[float, float]:
        pnl_usdc = (size_usdc / entry) * price - size_usdc
        pnl_pct  = (price - entry) / entry * 100
        return round(pnl_usdc, 4), round(pnl_pct, 4)

    # ── Résultat minimum garanti si TS déclenché ──────────────────────────────
    @staticmethod
    def _calc_ts_result(size_usdc: float, entry: float, highest: float,
                        atr: float) -> tuple[float, float]:
        ts_exit  = highest - atr * ATR_TS_MULT
        pnl_usdc = (size_usdc / entry) * ts_exit - size_usdc
        pnl_pct  = (ts_exit - entry) / entry * 100
        return round(pnl_usdc, 4), round(pnl_pct, 4)

    # ── Métriques de performance ──────────────────────────────────────────────
    def get_metrics(self) -> dict:
        """
        Calcule Sharpe ratio, Profit Factor, Max Drawdown,
        Expectancy et Winrate depuis l'historique des trades.
        """
        history = self.total_pnl_history
        if not history:
            return {
                "winrate": 0.0, "profit_factor": 0.0,
                "expectancy": 0.0, "max_drawdown": 0.0, "sharpe": 0.0,
            }

        pnls = [h[0] for h in history]  # pnl_usdc par trade

        wins_vals   = [p for p in pnls if p > 0]
        losses_vals = [abs(p) for p in pnls if p < 0]

        winrate        = len(wins_vals) / len(pnls) * 100 if pnls else 0.0
        avg_win        = sum(wins_vals) / len(wins_vals) if wins_vals else 0.0
        avg_loss       = sum(losses_vals) / len(losses_vals) if losses_vals else 0.0
        profit_factor  = sum(wins_vals) / sum(losses_vals) if losses_vals else float("inf")
        expectancy     = (winrate / 100 * avg_win) - ((1 - winrate / 100) * avg_loss)

        # Max drawdown (séquence cumulée)
        cumulative = 0.0
        peak       = 0.0
        max_dd     = 0.0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        # Sharpe simplifié (ratio rendement / std)
        if len(pnls) > 1:
            mean_pnl = sum(pnls) / len(pnls)
            variance = sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls)
            std_pnl  = variance ** 0.5
            sharpe   = (mean_pnl / std_pnl) if std_pnl > 0 else 0.0
        else:
            sharpe = 0.0

        return {
            "winrate":       round(winrate, 1),
            "profit_factor": round(profit_factor, 2),
            "expectancy":    round(expectancy, 4),
            "max_drawdown":  round(max_dd, 2),
            "sharpe":        round(sharpe, 2),
        }

    # ── Paires USDC avec filtre de liquidité ──────────────────────────────────
    def _get_usdc_pairs(self) -> list:
        import time
        now = time.time()
        if self._usdc_pairs and (now - self._last_pair_refresh) < 3600:
            return self._usdc_pairs
        try:
            markets = self.exchange.load_markets()
            # Filtre de base : actif, spot, USDC
            candidates = [
                s for s, m in markets.items()
                if s.endswith("/USDC") and m.get("active") and m.get("spot")
                and s != BTC_SYMBOL  # BTC géré séparément pour le filtre régime
            ]
            # Filtre liquidité : volume 24h >= 50M USDC
            liquid = []
            for symbol in candidates:
                try:
                    ticker = self.exchange.fetch_ticker(symbol)
                    vol24  = float(ticker.get("quoteVolume") or 0)
                    if vol24 >= MIN_VOLUME_24H_USDC:
                        liquid.append(symbol)
                except Exception:
                    pass
            self._usdc_pairs        = liquid
            self._last_pair_refresh = now
            log.info(f"{len(liquid)} paires liquides (sur {len(candidates)} USDC actives)")
        except Exception as e:
            log.error(f"Erreur chargement marchés : {e}")
        return self._usdc_pairs

    # ── OHLCV ─────────────────────────────────────────────────────────────────
    def _fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 220) -> pd.DataFrame | None:
        try:
            data = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df   = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "vol"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms")
            return df
        except Exception:
            return None

    # ── ATR ───────────────────────────────────────────────────────────────────
    def _atr(self, df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
        """Average True Range sur la période donnée."""
        high  = df["high"]
        low   = df["low"]
        close = df["close"].shift(1)
        tr    = pd.concat([
            high - low,
            (high - close).abs(),
            (low  - close).abs(),
        ], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])

    # ── RSI ───────────────────────────────────────────────────────────────────
    def _rsi(self, series: pd.Series, period: int = 14) -> float:
        delta = series.diff()
        gain  = delta.clip(lower=0).rolling(period).mean()
        loss  = (-delta.clip(upper=0)).rolling(period).mean()
        rs    = gain / loss.replace(0, 1e-10)
        return float((100 - (100 / (1 + rs))).iloc[-1])

    # ── Pente EMA ─────────────────────────────────────────────────────────────
    def _ema_slope(self, series: pd.Series, span: int = 20, lookback: int = 3) -> float:
        """Pente de l'EMA sur les dernières `lookback` bougies."""
        ema = series.ewm(span=span).mean()
        return float(ema.iloc[-1] - ema.iloc[-1 - lookback])

    # ── Filtre régime BTC ─────────────────────────────────────────────────────
    def _btc_regime_ok(self) -> bool:
        """
        Retourne True si BTC > EMA200 sur 1h (régime haussier global).
        En cas d'erreur API, retourne True pour ne pas bloquer le bot.
        """
        try:
            df = self._fetch_ohlcv(BTC_SYMBOL, TIMEFRAME_SHORT, limit=220)
            if df is None or len(df) < BTC_EMA_PERIOD:
                return True
            df["ema200"] = df["close"].ewm(span=BTC_EMA_PERIOD).mean()
            last = df.iloc[-1]
            ok   = bool(last["close"] > last["ema200"])
            log.info(f"Régime BTC : {'HAUSSIER' if ok else 'BAISSIER'} "
                     f"(prix {last['close']:.2f} / EMA200 {last['ema200']:.2f})")
            return ok
        except Exception as e:
            log.error(f"Erreur filtre BTC : {e}")
            return True

    # ── Filtre spread ─────────────────────────────────────────────────────────
    def _spread_ok(self, symbol: str) -> bool:
        """Vérifie que le spread bid/ask est < MAX_SPREAD_PCT."""
        try:
            ob  = self.exchange.fetch_order_book(symbol, limit=1)
            bid = ob["bids"][0][0] if ob["bids"] else 0
            ask = ob["asks"][0][0] if ob["asks"] else 0
            if bid <= 0 or ask <= 0:
                return True
            spread = (ask - bid) / ((ask + bid) / 2) * 100
            return spread <= MAX_SPREAD_PCT
        except Exception:
            return True  # en cas d'erreur, on ne bloque pas

    # ── Calcul des niveaux SL/TS depuis ATR ───────────────────────────────────
    def _compute_stops(self, entry: float, atr: float) -> tuple[float, float]:
        """
        Retourne (stop_loss, trailing_stop_initial) calculés depuis ATR.
        Garde-fous min/max en % appliqués.
        """
        # SL
        sl_raw   = entry - atr * ATR_SL_MULT
        sl_pct   = (entry - sl_raw) / entry * 100
        sl_pct   = max(SL_MIN_PCT, min(SL_MAX_PCT, sl_pct))
        sl_price = entry * (1 - sl_pct / 100)

        # TS initial (même logique, depuis l'entrée)
        ts_raw   = entry - atr * ATR_TS_MULT
        ts_pct   = (entry - ts_raw) / entry * 100
        ts_pct   = max(TS_MIN_PCT, min(TS_MAX_PCT, ts_pct))
        ts_price = entry * (1 - ts_pct / 100)

        return round(sl_price, 8), round(ts_price, 8)

    # ── Signal d'entrée complet ───────────────────────────────────────────────
    def _analyze_signal(self, symbol: str) -> dict:
        result = {"valid": False, "price": 0.0, "atr": 0.0, "details": ""}

        # ── Données 1h ────────────────────────────────────────────────────────
        df1 = self._fetch_ohlcv(symbol, TIMEFRAME_SHORT)
        if df1 is None or len(df1) < 60:
            result["details"] = "Données 1h insuffisantes"
            return result

        df1["ema20"]  = df1["close"].ewm(span=20).mean()
        df1["ema50"]  = df1["close"].ewm(span=50).mean()
        df1["vol_ma"] = df1["vol"].rolling(20).mean()
        last1 = df1.iloc[-1]

        ema1h_ok   = bool(last1["ema20"] > last1["ema50"])
        close1h_ok = bool(last1["close"] > last1["ema20"])
        vol_ratio  = float(last1["vol"] / last1["vol_ma"]) if last1["vol_ma"] > 0 else 0.0
        vol_ok     = vol_ratio >= VOLUME_MULTIPLIER
        rsi_val    = self._rsi(df1["close"])
        rsi_ok     = RSI_MIN_ENTRY <= rsi_val <= RSI_MAX_ENTRY
        slope      = self._ema_slope(df1["close"])
        slope_ok   = slope > EMA_SLOPE_MIN
        atr_val    = self._atr(df1)

        # ── Données 4h ────────────────────────────────────────────────────────
        df4 = self._fetch_ohlcv(symbol, TIMEFRAME_LONG)
        ema4h_ok  = False
        ema4h_str = "Données insuffisantes"
        if df4 is not None and len(df4) >= 50:
            df4["ema20"] = df4["close"].ewm(span=20).mean()
            df4["ema50"] = df4["close"].ewm(span=50).mean()
            last4        = df4.iloc[-1]
            ema4h_ok     = bool(last4["ema20"] > last4["ema50"])
            ema4h_str    = (f"EMA20 ({last4['ema20']:.4f}) "
                            f"{'>' if ema4h_ok else '<'} EMA50 ({last4['ema50']:.4f})")

        # ── Spread ────────────────────────────────────────────────────────────
        spread_ok = self._spread_ok(symbol)

        result["price"] = float(last1["close"])
        result["atr"]   = atr_val
        result["valid"] = (ema1h_ok and close1h_ok and vol_ok and rsi_ok
                           and slope_ok and ema4h_ok and spread_ok)
        result["details"] = "\n".join([
            f"{'✅' if ema1h_ok   else '❌'} [1h] EMA20 ({last1['ema20']:.4f}) {'>' if ema1h_ok else '<'} EMA50 ({last1['ema50']:.4f})",
            f"{'✅' if close1h_ok else '❌'} [1h] Clôture ({last1['close']:.4f}) {'>' if close1h_ok else '<'} EMA20",
            f"{'✅' if slope_ok   else '❌'} [1h] Pente EMA20 : {slope:+.6f} ({'OK' if slope_ok else 'plate/baissière'})",
            f"{'✅' if vol_ok     else '❌'} [1h] Volume ×{vol_ratio:.2f} (seuil ×{VOLUME_MULTIPLIER})",
            f"{'✅' if rsi_ok     else '❌'} [1h] RSI {rsi_val:.1f} (zone {RSI_MIN_ENTRY}–{RSI_MAX_ENTRY})",
            f"{'✅' if ema4h_ok   else '❌'} [4h] {ema4h_str}",
            f"{'✅' if spread_ok  else '❌'} Spread ({'OK' if spread_ok else f'> {MAX_SPREAD_PCT}%'})",
            f"📊 ATR : {atr_val:.6f}",
        ])
        return result

    # ── Analyse matin ─────────────────────────────────────────────────────────
    def _analyze_morning(self, symbol: str) -> dict:
        """Tendance 1h + 4h uniquement, sans volume ni RSI."""
        result = {"valid": False, "price": 0.0, "details": ""}

        df1 = self._fetch_ohlcv(symbol, TIMEFRAME_SHORT)
        if df1 is None or len(df1) < 50:
            result["details"] = "Données 1h insuffisantes"
            return result

        df1["ema20"] = df1["close"].ewm(span=20).mean()
        df1["ema50"] = df1["close"].ewm(span=50).mean()
        last1 = df1.iloc[-1]

        ema1h_ok   = bool(last1["ema20"] > last1["ema50"])
        close1h_ok = bool(last1["close"] > last1["ema20"])

        df4 = self._fetch_ohlcv(symbol, TIMEFRAME_LONG)
        ema4h_ok  = False
        ema4h_str = "Données insuffisantes"
        if df4 is not None and len(df4) >= 50:
            df4["ema20"] = df4["close"].ewm(span=20).mean()
            df4["ema50"] = df4["close"].ewm(span=50).mean()
            last4        = df4.iloc[-1]
            ema4h_ok     = bool(last4["ema20"] > last4["ema50"])
            ema4h_str    = (f"EMA20 ({last4['ema20']:.4f}) "
                            f"{'>' if ema4h_ok else '<'} EMA50 ({last4['ema50']:.4f})")

        result["price"] = float(last1["close"])
        result["valid"] = ema1h_ok and close1h_ok and ema4h_ok
        result["details"] = "\n".join([
            f"{'✅' if ema1h_ok   else '❌'} [1h] EMA20 {'>' if ema1h_ok else '<'} EMA50",
            f"{'✅' if close1h_ok else '❌'} [1h] Clôture {'>' if close1h_ok else '<'} EMA20",
            f"{'✅' if ema4h_ok   else '❌'} [4h] {ema4h_str}",
        ])
        return result

    # ── Vérification SL/TS robuste ────────────────────────────────────────────
    def _check_stop_triggered(self, symbol: str, current_price: float,
                               stop_level: float, label: str = "STOP") -> tuple[bool, float]:
        if current_price <= stop_level:
            log.info(f"[{label}] {symbol} prix {current_price:.6f} <= {stop_level:.6f}")
            return True, current_price
        try:
            df_1m = self._fetch_ohlcv(symbol, "1m", limit=3)
            if df_1m is not None and len(df_1m) >= 2:
                last_low = float(df_1m.iloc[-2]["low"])
                if last_low <= stop_level:
                    log.warning(f"[{label}] {symbol} low 1m {last_low:.6f} <= {stop_level:.6f}")
                    return True, stop_level
        except Exception as e:
            log.error(f"Erreur check stop 1m {symbol} : {e}")
        return False, current_price

    # ── Ouverture ─────────────────────────────────────────────────────────────
    def _open_position(self, symbol: str, analysis: dict) -> str:
        if symbol in self.positions:
            return ""
        if len(self.positions) >= MAX_POSITIONS:
            return ""
        size = self._position_size()
        if size <= 0:
            return ""

        price     = analysis["price"]
        atr       = analysis["atr"]
        qty       = size / price
        opened_at = datetime.utcnow().isoformat()
        sl_price, ts_price = self._compute_stops(price, atr)

        # Calcul des TP en valeur absolue
        tp1_price = price + atr * TP1_ATR_MULT
        tp2_price = price + atr * TP2_ATR_MULT

        self.positions[symbol] = {
            "symbol":              symbol,
            "entry":               price,
            "qty":                 qty,
            "size_usdc":           size,
            "size_usdc_initial":   size,   # taille initiale conservée pour référence
            "highest":             price,
            "atr":                 atr,
            "stop_loss":           sl_price,
            "trailing_stop":       ts_price,
            "tp1_price":           round(tp1_price, 8),
            "tp2_price":           round(tp2_price, 8),
            "tp1_done":            False,
            "tp2_done":            False,
            "opened_at":           opened_at,
            "ts_secured_notified": False,
        }
        self.capital -= size
        self._save_state()

        sl_pct   = (price - sl_price) / price * 100
        ts_pct   = (price - ts_price) / price * 100
        tp1_pct  = (tp1_price - price) / price * 100
        tp2_pct  = (tp2_price - price) / price * 100
        opened_f = datetime.fromisoformat(opened_at).strftime("%d/%m %H:%M")

        log.info(f"[PAPER] ACHAT {symbol} @ {price:.6f} | {size:.2f} USDC | ATR={atr:.6f}")
        return (
            f"✅ *Entrée — {symbol}*\n"
            f"Ouvert le {opened_f}\n"
            f"Prix : `{price:.6f}` | Investi : `{size:.2f}` USDC | ATR : `{atr:.6f}`\n"
            f"SL : `{sl_price:.6f}` (-{sl_pct:.1f}%) | TS : `{ts_price:.6f}` (-{ts_pct:.1f}%)\n"
            f"TP1 : `{tp1_price:.6f}` (+{tp1_pct:.1f}%, vente 30%) | "
            f"TP2 : `{tp2_price:.6f}` (+{tp2_pct:.1f}%, vente 30%)\n\n"
            f"📐 *Signal :*\n{analysis['details']}"
        )

    # ── Mise à jour TS + alerte sécurisation ─────────────────────────────────
    def _update_trailing_stop(self, pos: dict, current_price: float) -> str:
        alert = ""
        if current_price > pos["highest"]:
            pos["highest"] = current_price
            atr = pos.get("atr", 0)
            if atr > 0:
                new_ts = current_price - atr * ATR_TS_MULT
                ts_pct = (current_price - new_ts) / current_price * 100
                ts_pct = max(TS_MIN_PCT, min(TS_MAX_PCT, ts_pct))
                new_ts = current_price * (1 - ts_pct / 100)
            else:
                new_ts = current_price * (1 - TS_MIN_PCT / 100)
            pos["trailing_stop"] = round(new_ts, 8)
            log.debug("TS remonté %s highest=%.6f TS=%.6f",
                      pos["symbol"], current_price, pos["trailing_stop"])

            # Alerte unique quand TS > entrée
            if not pos.get("ts_secured_notified", False) and pos["trailing_stop"] > pos["entry"]:
                pos["ts_secured_notified"] = True
                atr_v    = pos.get("atr", 0)
                ts_pnl, ts_pct_val = self._calc_ts_result(
                    pos["size_usdc"], pos["entry"], pos["highest"], atr_v
                )
                sym   = pos["symbol"]
                alert = (
                    f"\U0001f512 *Position sécurisée — {sym}*\n"
                    f"Le TS est au-dessus du prix d'entrée — gain minimum garanti.\n"
                    f"Gain si TS : `{ts_pnl:+.2f}` USDC (`{ts_pct_val:+.2f}%`)\n"
                    f"Plus haut : `{pos['highest']:.6f}` | TS : `{pos['trailing_stop']:.6f}`"
                )
        return alert

    # ── Vente partielle TP ────────────────────────────────────────────────────
    def _check_tp(self, symbol: str, pos: dict, current_price: float) -> str:
        """
        Vérifie et exécute les prises de profits partielles TP1 et TP2.
        Retourne un message si un TP est déclenché.
        """
        msg = ""

        # TP1 — vend 30% de la position initiale
        if not pos.get("tp1_done") and current_price >= pos["tp1_price"]:
            sell_ratio  = TP1_RATIO
            sell_usdc   = pos["size_usdc_initial"] * sell_ratio
            sell_usdc   = min(sell_usdc, pos["size_usdc"])  # ne pas vendre plus que dispo
            pnl_u, pnl_p = self._calc_pnl(sell_usdc, pos["entry"], current_price)
            pos["size_usdc"] -= sell_usdc
            self.capital     += sell_usdc + pnl_u
            self.pnl         += pnl_u
            pos["tp1_done"]   = True
            self.total_pnl_history.append([round(pnl_u, 4), round(pnl_p, 4)])
            self._save_state()
            log.info(f"[TP1] {symbol} @ {current_price:.6f} | +{pnl_u:.2f} USDC")
            msg = (
                f"🎯 *TP1 — {symbol}*\n"
                f"Vente 30% à `{current_price:.6f}`\n"
                f"G/P : `{pnl_u:+.2f}` USDC (`{pnl_p:+.2f}%`)\n"
                f"Reste en position : `{pos['size_usdc']:.2f}` USDC"
            )

        # TP2 — vend 30% de la position initiale
        elif pos.get("tp1_done") and not pos.get("tp2_done") and current_price >= pos["tp2_price"]:
            sell_ratio  = TP2_RATIO
            sell_usdc   = pos["size_usdc_initial"] * sell_ratio
            sell_usdc   = min(sell_usdc, pos["size_usdc"])
            pnl_u, pnl_p = self._calc_pnl(sell_usdc, pos["entry"], current_price)
            pos["size_usdc"] -= sell_usdc
            self.capital     += sell_usdc + pnl_u
            self.pnl         += pnl_u
            pos["tp2_done"]   = True
            self.total_pnl_history.append([round(pnl_u, 4), round(pnl_p, 4)])
            self._save_state()
            log.info(f"[TP2] {symbol} @ {current_price:.6f} | +{pnl_u:.2f} USDC")
            msg = (
                f"🎯 *TP2 — {symbol}*\n"
                f"Vente 30% à `{current_price:.6f}`\n"
                f"G/P : `{pnl_u:+.2f}` USDC (`{pnl_p:+.2f}%`)\n"
                f"Reste en TS : `{pos['size_usdc']:.2f}` USDC (40% initial)"
            )

        return msg

    # ── Clôture complète ──────────────────────────────────────────────────────
    def _close_position(self, symbol: str, price: float, reason: str) -> str:
        pos = self.positions.pop(symbol, None)
        if not pos:
            return ""

        pnl_usdc, pnl_pct = self._calc_pnl(pos["size_usdc"], pos["entry"], price)
        self.pnl     += pnl_usdc
        self.capital += pos["size_usdc"] + pnl_usdc
        self.total_trades += 1

        # Pour les métriques, on comptabilise le résultat global de la position
        # (les TP partiels ont déjà été ajoutés à l'historique séparément)
        if pnl_usdc >= 0:
            self.wins += 1
        else:
            self.losses += 1
        self.total_pnl_history.append([round(pnl_usdc, 4), round(pnl_pct, 4)])
        self._save_state()

        opened_at  = pos.get("opened_at", "")
        closed_at  = datetime.utcnow().isoformat()
        opened_fmt = datetime.fromisoformat(opened_at).strftime("%d/%m %H:%M") if opened_at else "—"
        closed_fmt = datetime.fromisoformat(closed_at).strftime("%d/%m %H:%M")

        log.info(f"[PAPER] CLÔTURE {symbol} @ {price:.6f} | "
                 f"G/P : {pnl_usdc:+.2f} USDC ({pnl_pct:+.2f}%) | {reason}")
        return (
            f"❌ *Clôture — {symbol}* — {reason}\n"
            f"Ouvert {opened_fmt} → Clôturé {closed_fmt}\n"
            f"Entrée : `{pos['entry']:.6f}` | Sortie : `{price:.6f}`\n"
            f"Investi initial : `{pos['size_usdc_initial']:.2f}` USDC\n"
            f"G/P clôture : `{pnl_usdc:+.2f}` USDC (`{pnl_pct:+.2f}%`)"
        )

    # ── Clôture manuelle ──────────────────────────────────────────────────────
    def close_position_manual(self, symbol: str) -> str:
        sym = symbol.upper()
        if sym not in self.positions:
            return f"❓ Aucune position ouverte sur `{sym}`."
        try:
            price = float(self.exchange.fetch_ticker(sym)["last"])
        except Exception as e:
            return f"⚠️ Impossible de récupérer le prix : {e}"
        return self._close_position(sym, price, "Clôture manuelle")

    def close_all_manual(self) -> list:
        messages = []
        for symbol in list(self.positions.keys()):
            msg = self.close_position_manual(symbol)
            if msg:
                messages.append(msg)
        return messages

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
                price = float(self.exchange.fetch_ticker(symbol)["last"])
            except Exception:
                price = pos["entry"]

            atr_v             = pos.get("atr", 0)
            pnl_usdc, pnl_pct = self._calc_pnl(pos["size_usdc"], pos["entry"], price)
            ts_pnl, ts_pct    = self._calc_ts_result(
                pos["size_usdc"], pos["entry"], pos["highest"], atr_v
            )
            verdict    = "✅ *Garder*" if analysis["valid"] else "❌ *Abandonner*"
            opened_fmt = datetime.fromisoformat(pos["opened_at"]).strftime("%d/%m %H:%M") if pos.get("opened_at") else "—"

            tp_status = ""
            if pos.get("tp1_done") and pos.get("tp2_done"):
                tp_status = "TP1 ✅ TP2 ✅ — reste 40% en TS"
            elif pos.get("tp1_done"):
                tp_status = "TP1 ✅ — en attente TP2"
            else:
                tp_status = "TP1 ⏳ TP2 ⏳"

            msg = (
                f"─────────────────\n"
                f"📌 `{symbol}` | Ouvert le {opened_fmt}\n"
                f"G/P actuel : `{pnl_usdc:+.2f}` USDC (`{pnl_pct:+.2f}%`)\n"
                f"💵 Résultat min si TS : `{ts_pnl:+.2f}` USDC (`{ts_pct:+.2f}%`)\n"
                f"TP : {tp_status}\n\n"
                f"*Indicateurs :*\n{analysis['details']}\n\n"
                f"Verdict : {verdict}"
            )
            messages.append(msg)

            if not analysis["valid"]:
                close_msg = self._close_position(symbol, price, "Abandon matin — tendance invalide")
                if close_msg:
                    messages.append(close_msg)

        return messages

    # ── Debug (/debug SYMBOL) ─────────────────────────────────────────────────
    def debug_position(self, symbol: str) -> str:
        sym = symbol.upper()
        pos = self.positions.get(sym)
        if not pos:
            return f"❓ Aucune position ouverte sur `{sym}`."
        try:
            current_price = float(self.exchange.fetch_ticker(sym)["last"])
        except Exception as e:
            return f"⚠️ Impossible de récupérer le prix : {e}"

        atr_v             = pos.get("atr", 0)
        sl_triggered      = current_price <= pos["stop_loss"]
        ts_triggered      = current_price <= pos["trailing_stop"]
        tp1_triggered     = current_price >= pos.get("tp1_price", float("inf"))
        tp2_triggered     = current_price >= pos.get("tp2_price", float("inf"))
        pnl_usdc, pnl_pct = self._calc_pnl(pos["size_usdc"], pos["entry"], current_price)
        ts_pnl, ts_pct    = self._calc_ts_result(
            pos["size_usdc"], pos["entry"], pos["highest"], atr_v
        )

        return (
            f"🔍 *Debug — {sym}*\n\n"
            f"Prix actuel : `{current_price:.6f}`\n"
            f"Entrée : `{pos['entry']:.6f}` | Plus haut : `{pos['highest']:.6f}`\n"
            f"ATR : `{atr_v:.6f}`\n\n"
            f"SL : `{pos['stop_loss']:.6f}` {'🔴 DÉCLENCHÉ' if sl_triggered else '🟢 OK'}\n"
            f"TS : `{pos['trailing_stop']:.6f}` {'🔴 DÉCLENCHÉ' if ts_triggered else '🟢 OK'}\n"
            f"TP1 : `{pos.get('tp1_price', 0):.6f}` "
            f"{'✅ FAIT' if pos.get('tp1_done') else ('🎯 ATTEINT' if tp1_triggered else '⏳')}\n"
            f"TP2 : `{pos.get('tp2_price', 0):.6f}` "
            f"{'✅ FAIT' if pos.get('tp2_done') else ('🎯 ATTEINT' if tp2_triggered else '⏳')}\n\n"
            f"G/P actuel : `{pnl_usdc:+.2f}` USDC (`{pnl_pct:+.2f}%`)\n"
            f"Résultat min si TS : `{ts_pnl:+.2f}` USDC (`{ts_pct:+.2f}%`)\n"
            f"Investi restant : `{pos['size_usdc']:.2f}` USDC "
            f"(initial : `{pos.get('size_usdc_initial', pos['size_usdc']):.2f}` USDC)"
        )

    # ── SCAN ──────────────────────────────────────────────────────════════════
    def scan(self) -> list[str]:
        alerts = []

        # ── Étape 1 : gestion des positions ouvertes (TOUJOURS, même si drawdown)
        for symbol in list(self.positions.keys()):
            try:
                current_price = float(self.exchange.fetch_ticker(symbol)["last"])
                pos           = self.positions[symbol]

                # TP partiels d'abord
                tp_msg = self._check_tp(symbol, pos, current_price)
                if tp_msg:
                    alerts.append(tp_msg)

                # Mise à jour TS + alerte sécurisation
                ts_alert = self._update_trailing_stop(pos, current_price)
                if ts_alert:
                    alerts.append(ts_alert)

                # SL
                sl_hit, sl_price = self._check_stop_triggered(
                    symbol, current_price, pos["stop_loss"], label="SL"
                )
                if sl_hit:
                    msg = self._close_position(symbol, sl_price, "Stop Loss")
                    if msg:
                        alerts.append(msg)
                    continue

                # TS
                ts_hit, ts_price = self._check_stop_triggered(
                    symbol, current_price, pos["trailing_stop"], label="TS"
                )
                if ts_hit:
                    msg = self._close_position(symbol, ts_price, "Trailing Stop")
                    if msg:
                        alerts.append(msg)

            except Exception as e:
                log.error(f"Erreur mise à jour {symbol} : {e}")

        # ── Étape 2 : blocage nouvelles entrées si drawdown
        if self._daily_drawdown_reached():
            log.warning("Drawdown journalier atteint — nouvelles entrées bloquées")
            return alerts

        # ── Étape 3 : filtre régime BTC (une vérification par scan)
        if not self._btc_regime_ok():
            log.info("Régime BTC baissier — nouvelles entrées bloquées")
            return alerts

        # ── Étape 4 : nouvelles entrées
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
                price = float(self.exchange.fetch_ticker(symbol)["last"])
            except Exception:
                price = pos["entry"]

            atr_v             = pos.get("atr", 0)
            pnl_usdc, pnl_pct = self._calc_pnl(pos["size_usdc"], pos["entry"], price)
            ts_pnl, ts_pct    = self._calc_ts_result(
                pos["size_usdc"], pos["entry"], pos["highest"], atr_v
            )
            opened_fmt = (datetime.fromisoformat(pos["opened_at"]).strftime("%d/%m %H:%M")
                          if pos.get("opened_at") else "—")

            result.append({
                "symbol":           symbol,
                "entry":            pos["entry"],
                "current":          price,
                "size_usdc":        pos["size_usdc"],
                "size_usdc_initial": pos.get("size_usdc_initial", pos["size_usdc"]),
                "highest":          pos["highest"],
                "ts_price":         pos["trailing_stop"],
                "tp1_price":        pos.get("tp1_price", 0),
                "tp2_price":        pos.get("tp2_price", 0),
                "tp1_done":         pos.get("tp1_done", False),
                "tp2_done":         pos.get("tp2_done", False),
                "pnl_usdc":         pnl_usdc,
                "pnl_pct":          pnl_pct,
                "ts_pnl":           ts_pnl,
                "ts_pct":           ts_pct,
                "opened_at":        opened_fmt,
            })
        return result
