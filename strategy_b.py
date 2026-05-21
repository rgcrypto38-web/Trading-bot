import ccxt
import pandas as pd
import json
import os
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# STRATÉGIE B — MOMENTUM BREAKOUT
# Logique : détecter les ruptures de momentum sur small caps (1M USDC min)
# Signal  : Volume spike ×3 (1h vs moy 24h) + RSI 60–75 + prix +5% sur 1h
# Différences vs A : pas d'EMA, TP% fixe, SL serré ×1.5 ATR, 5 positions max
# ══════════════════════════════════════════════════════════════════════════════

# ── Capital et positions ──────────────────────────────────────────────────────
CAPITAL_TOTAL_B     = 100.0
MAX_POSITIONS_B     = 5
POSITION_SIZE_MIN_B = 10.0        # USDC minimum par position

# ── Stops dynamiques ATR ──────────────────────────────────────────────────────
ATR_PERIOD_B        = 14
ATR_SL_MULT_B       = 1.5         # SL serré — momentum peut retourner vite
ATR_TS_MULT_B       = 1.5         # TS serré — même logique
SL_MIN_PCT_B        = 1.5         # Garde-fou plancher SL (%)
SL_MAX_PCT_B        = 6.0         # Garde-fou plafond SL (%)
TS_MIN_PCT_B        = 1.5
TS_MAX_PCT_B        = 8.0

# ── Take profit partiels (% fixe, pas ATR) ───────────────────────────────────
TP1_PCT_B           = 6.0         # TP1 = entrée × 1.06
TP2_PCT_B           = 12.0        # TP2 = entrée × 1.12
TP1_RATIO_B         = 0.40        # 40% de la position vendue au TP1
TP2_RATIO_B         = 0.40        # 40% vendu au TP2
# Reste 20% en trailing stop ×1.5 ATR

# ── Filtres signal ────────────────────────────────────────────────────────────
RSI_MIN_B           = 60          # Zone momentum
RSI_MAX_B           = 75          # Pas encore surachat
RSI_MIN_BEAR_B      = 65          # Bear mode : momentum plus sélectif
VOLUME_MULT_B       = 3.0         # Spike volume ×3 vs moy 24h
VOLUME_MULT_BEAR_B  = 4.0         # Bear mode : spike plus fort requis
PRICE_MOMENTUM_PCT  = 5.0         # Prix +5% sur la dernière bougie 1h
MAX_SPREAD_PCT_B    = 0.20        # Spread légèrement plus tolérant (small caps)

# ── Liquidité (plus basse que A — small caps acceptés) ───────────────────────
MIN_VOLUME_24H_USDC_B = 1_000_000  # 1M USDC

# ── Filtre régime BTC ─────────────────────────────────────────────────────────
BTC_SYMBOL_B        = "BTC/USDC"
BTC_EMA_PERIOD_B    = 200

# ── Risk management ───────────────────────────────────────────────────────────
MAX_DAILY_LOSS_USDC_B = 8.0       # Perte journalière max B

# ── Abandon (B est court terme — 2 jours max sans TP1) ───────────────────────
ABANDON_DAYS_B      = 2

# ── Re-entry cooldowns ────────────────────────────────────────────────────────
COOLDOWN_SL_B       = 24          # SL direct
COOLDOWN_ABANDON_B  = 12          # Abandon matin
COOLDOWN_TS_TP1_B   = 6           # TS avec TP1 déjà fait (sortie propre)
COOLDOWN_TS_ONLY_B  = 24          # TS sans TP1 (sorti trop tôt)

# ── Persistance ───────────────────────────────────────────────────────────────
PERSISTENCE_FILE_B  = "positions_b.json"

PARIS_TZ = timezone(timedelta(hours=2))


# ══════════════════════════════════════════════════════════════════════════════
# CLASSE STRATÉGIE B
# ══════════════════════════════════════════════════════════════════════════════

class StrategyB:
    def __init__(self, binance_key: str = None, binance_secret: str = None):
        self.exchange = ccxt.binance({
            "apiKey":          binance_key or "",
            "secret":          binance_secret or "",
            "enableRateLimit": True,
            "options":         {"defaultType": "spot"},
        })
        self.positions:          dict  = {}
        self.capital:            float = CAPITAL_TOTAL_B
        self.pnl:                float = 0.0
        self.daily_start_pnl:    float = 0.0
        self.total_trades:       int   = 0
        self.wins:               int   = 0
        self.losses:             int   = 0
        self.total_pnl_history:  list  = []
        self._usdc_pairs:        list  = []
        self._last_pair_refresh: float = 0
        self._morning_done_date: str   = ""
        self._cooldowns:         dict  = {}
        self._skip_list:         dict  = {}

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
            "cooldowns":         self._cooldowns,
            "skip_list":         self._skip_list,
            "saved_at":          datetime.utcnow().isoformat(),
        }
        try:
            with open(PERSISTENCE_FILE_B, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.error(f"[B] Erreur sauvegarde : {e}")

    def _load_state(self):
        if not os.path.exists(PERSISTENCE_FILE_B):
            log.info("[B] Aucun état persisté — démarrage à zéro")
            return
        try:
            with open(PERSISTENCE_FILE_B, "r") as f:
                state = json.load(f)
            self.positions          = state.get("positions", {})
            self.capital            = state.get("capital", CAPITAL_TOTAL_B)
            self.pnl                = state.get("pnl", 0.0)
            self.daily_start_pnl    = state.get("daily_start_pnl", 0.0)
            self.total_trades       = state.get("total_trades", 0)
            self.wins               = state.get("wins", 0)
            self.losses             = state.get("losses", 0)
            self.total_pnl_history  = state.get("total_pnl_history", [])
            self._morning_done_date = state.get("morning_done_date", "")
            self._cooldowns         = state.get("cooldowns", {})
            self._skip_list         = state.get("skip_list", {})
            log.info(f"[B] État rechargé — {len(self.positions)} position(s)")
        except Exception as e:
            log.error(f"[B] Erreur chargement état : {e}")

    def reset_daily_pnl(self):
        self.daily_start_pnl = self.pnl
        self._save_state()

    # ── Drawdown ──────────────────────────────────────────────────────────────
    def _daily_drawdown_reached(self) -> bool:
        return (self.pnl - self.daily_start_pnl) <= -MAX_DAILY_LOSS_USDC_B

    # ── Taille de position dynamique ──────────────────────────────────────────
    def _position_size(self) -> float:
        slots_libres = MAX_POSITIONS_B - len(self.positions)
        if slots_libres <= 0:
            return 0.0
        plafond = (
            (self.capital + sum(p["size_usdc"] for p in self.positions.values()))
            / MAX_POSITIONS_B * 1.20
        )
        taille = min(self.capital / slots_libres, plafond)
        return taille if taille >= POSITION_SIZE_MIN_B else 0.0

    # ── Formules P&L ──────────────────────────────────────────────────────────
    @staticmethod
    def _calc_pnl(size_usdc: float, entry: float, price: float) -> tuple[float, float]:
        pnl_usdc = (size_usdc / entry) * price - size_usdc
        pnl_pct  = (price - entry) / entry * 100
        return round(pnl_usdc, 4), round(pnl_pct, 4)

    @staticmethod
    def _calc_ts_result(size_usdc: float, entry: float, highest: float,
                        atr: float) -> tuple[float, float]:
        ts_exit  = highest - atr * ATR_TS_MULT_B
        pnl_usdc = (size_usdc / entry) * ts_exit - size_usdc
        pnl_pct  = (ts_exit - entry) / entry * 100
        return round(pnl_usdc, 4), round(pnl_pct, 4)

    # ── Métriques de performance ──────────────────────────────────────────────
    def get_metrics(self) -> dict:
        history = self.total_pnl_history
        if not history:
            return {
                "winrate": 0.0, "profit_factor": 0.0,
                "expectancy": 0.0, "max_drawdown": 0.0, "sharpe": 0.0,
            }
        pnls        = [h[0] for h in history]
        wins_vals   = [p for p in pnls if p > 0]
        losses_vals = [abs(p) for p in pnls if p < 0]
        winrate       = len(wins_vals) / len(pnls) * 100 if pnls else 0.0
        avg_win       = sum(wins_vals) / len(wins_vals) if wins_vals else 0.0
        avg_loss      = sum(losses_vals) / len(losses_vals) if losses_vals else 0.0
        profit_factor = sum(wins_vals) / sum(losses_vals) if losses_vals else float("inf")
        expectancy    = (winrate / 100 * avg_win) - ((1 - winrate / 100) * avg_loss)
        cumulative, peak, max_dd = 0.0, 0.0, 0.0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
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

    # ── Paires USDC avec filtre liquidité 1M ─────────────────────────────────
    def _get_usdc_pairs(self) -> list:
        import time
        now = time.time()
        if self._usdc_pairs and (now - self._last_pair_refresh) < 3600:
            return self._usdc_pairs
        try:
            markets    = self.exchange.load_markets()
            candidates = [
                s for s, m in markets.items()
                if s.endswith("/USDC") and m.get("active") and m.get("spot")
                and s != BTC_SYMBOL_B
            ]
            liquid = []
            for symbol in candidates:
                try:
                    ticker = self.exchange.fetch_ticker(symbol)
                    vol24  = float(ticker.get("quoteVolume") or 0)
                    if vol24 >= MIN_VOLUME_24H_USDC_B:
                        liquid.append(symbol)
                except Exception:
                    pass
            self._usdc_pairs        = liquid
            self._last_pair_refresh = now
            log.info(f"[B] {len(liquid)} paires liquides ≥1M USDC")
        except Exception as e:
            log.error(f"[B] Erreur chargement marchés : {e}")
        return self._usdc_pairs

    # ── OHLCV ─────────────────────────────────────────────────────────────────
    def _fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 30) -> pd.DataFrame | None:
        try:
            data = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df   = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "vol"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms")
            return df
        except Exception:
            return None

    # ── ATR ───────────────────────────────────────────────────────────────────
    def _atr(self, df: pd.DataFrame, period: int = ATR_PERIOD_B) -> float:
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

    # ── Filtre régime BTC ─────────────────────────────────────────────────────
    def _btc_regime_ok(self) -> bool:
        try:
            df = self._fetch_ohlcv(BTC_SYMBOL_B, "1h", limit=220)
            if df is None or len(df) < BTC_EMA_PERIOD_B:
                return True
            df["ema200"] = df["close"].ewm(span=BTC_EMA_PERIOD_B).mean()
            last = df.iloc[-1]
            ok   = bool(last["close"] > last["ema200"])
            log.info(f"[B] Régime BTC : {'HAUSSIER' if ok else 'BAISSIER'}")
            return ok
        except Exception as e:
            log.error(f"[B] Erreur filtre BTC : {e}")
            return True

    # ── Filtre spread ─────────────────────────────────────────────────────────
    def _spread_ok(self, symbol: str) -> bool:
        try:
            ob  = self.exchange.fetch_order_book(symbol, limit=1)
            bid = ob["bids"][0][0] if ob["bids"] else 0
            ask = ob["asks"][0][0] if ob["asks"] else 0
            if bid <= 0 or ask <= 0:
                return True
            spread = (ask - bid) / ((ask + bid) / 2) * 100
            return spread <= MAX_SPREAD_PCT_B
        except Exception:
            return True

    # ── Calcul SL / TS initial ────────────────────────────────────────────────
    def _compute_stops(self, entry: float, atr: float) -> tuple[float, float]:
        # SL fixe ×1.5 ATR
        sl_raw = entry - atr * ATR_SL_MULT_B
        sl_pct = (entry - sl_raw) / entry * 100
        sl_pct = max(SL_MIN_PCT_B, min(SL_MAX_PCT_B, sl_pct))
        sl_price = entry * (1 - sl_pct / 100)

        # TS initial identique au SL (serré, remonte avec le prix)
        ts_raw = entry - atr * ATR_TS_MULT_B
        ts_pct = (entry - ts_raw) / entry * 100
        ts_pct = max(TS_MIN_PCT_B, min(TS_MAX_PCT_B, ts_pct))
        ts_price = entry * (1 - ts_pct / 100)

        return round(sl_price, 8), round(ts_price, 8)

    # ── Signal d'entrée B ─────────────────────────────────────────────────────
    def _analyze_signal(self, symbol: str, bear_mode: bool = False) -> dict:
        """
        Signal momentum breakout :
        1. Prix +5% sur la dernière bougie 1h fermée
        2. Volume ×3 (×4 bear) vs moyenne des 24 dernières heures
        3. RSI dans la zone 60–75 (65–75 en bear mode)
        Pas de filtre EMA — pure momentum.
        """
        result = {"valid": False, "price": 0.0, "atr": 0.0, "details": ""}

        rsi_min  = RSI_MIN_BEAR_B  if bear_mode else RSI_MIN_B
        vol_mult = VOLUME_MULT_BEAR_B if bear_mode else VOLUME_MULT_B

        # 30 bougies : 24 pour moy volume + 2 pour momentum + buffer
        df = self._fetch_ohlcv(symbol, "1h", limit=30)
        if df is None or len(df) < 26:
            result["details"] = "Données insuffisantes"
            return result

        last = df.iloc[-1]   # bougie courante (peut être incomplète)
        prev = df.iloc[-2]   # dernière bougie fermée
        ante = df.iloc[-3]   # avant-dernière fermée

        # ── Filtre stablecoin ─────────────────────────────────────────────────
        atr_val = self._atr(df)
        if float(prev["close"]) > 0 and (atr_val / float(prev["close"]) * 100) < 0.10:
            result["details"] = "Exclu — stablecoin/immobile"
            return result

        # ── Prix +5% sur 1h (dernière bougie fermée vs avant-dernière) ────────
        if float(ante["close"]) <= 0:
            result["details"] = "Prix invalide"
            return result
        momentum_pct = (float(prev["close"]) - float(ante["close"])) / float(ante["close"]) * 100
        momentum_ok  = momentum_pct >= PRICE_MOMENTUM_PCT

        # ── Volume spike : dernière bougie fermée vs moy des 24h précédentes ──
        vol_history  = df["vol"].iloc[-26:-2]   # 24 bougies avant la bougie fermée
        vol_mean_24h = float(vol_history.mean()) if len(vol_history) > 0 else 0.0
        vol_ratio    = float(prev["vol"]) / vol_mean_24h if vol_mean_24h > 0 else 0.0
        vol_ok       = vol_ratio >= vol_mult

        # ── RSI ───────────────────────────────────────────────────────────────
        rsi_val = self._rsi(df["close"])
        rsi_ok  = rsi_min <= rsi_val <= RSI_MAX_B

        # ── Spread ────────────────────────────────────────────────────────────
        spread_ok = self._spread_ok(symbol)

        result["price"] = float(prev["close"])   # prix de la dernière bougie fermée
        result["atr"]   = atr_val
        result["valid"] = momentum_ok and vol_ok and rsi_ok and spread_ok
        result["details"] = "\n".join([
            f"{'✅' if momentum_ok else '❌'} Momentum 1h : {momentum_pct:+.2f}% (seuil +{PRICE_MOMENTUM_PCT:.0f}%)",
            f"{'✅' if vol_ok     else '❌'} Volume spike ×{vol_ratio:.2f} vs 24h (seuil ×{vol_mult:.1f}{'⚠️ bear' if bear_mode else ''})",
            f"{'✅' if rsi_ok     else '❌'} RSI {rsi_val:.1f} (zone {rsi_min}–{RSI_MAX_B}{'⚠️ bear' if bear_mode else ''})",
            f"{'✅' if spread_ok  else '❌'} Spread ({'OK' if spread_ok else f'> {MAX_SPREAD_PCT_B}%'})",
            f"📊 ATR : {atr_val:.6f}",
        ])
        return result

    # ── Vérification SL/TS ────────────────────────────────────────────────────
    def _check_stop_triggered(self, symbol: str, current_price: float,
                               stop_level: float, label: str = "STOP") -> tuple[bool, float]:
        if current_price <= stop_level:
            log.info(f"[B][{label}] {symbol} prix {current_price:.6f} <= {stop_level:.6f}")
            return True, current_price
        try:
            df_1m = self._fetch_ohlcv(symbol, "1m", limit=3)
            if df_1m is not None and len(df_1m) >= 2:
                last_low = float(df_1m.iloc[-2]["low"])
                if last_low <= stop_level:
                    log.warning(f"[B][{label}] {symbol} low 1m {last_low:.6f} <= {stop_level:.6f}")
                    return True, stop_level
        except Exception as e:
            log.error(f"[B] Erreur check stop 1m {symbol} : {e}")
        return False, current_price

    # ── Mise à jour TS ────────────────────────────────────────────────────────
    def _update_trailing_stop(self, pos: dict, current_price: float) -> str:
        """TS fixe ×1.5 ATR — remonte avec le prix, ne redescend jamais."""
        alert = ""
        if current_price > pos["highest"]:
            pos["highest"] = current_price
            atr = pos.get("atr", 0)
            if atr > 0:
                new_ts  = current_price - atr * ATR_TS_MULT_B
                ts_pct  = (current_price - new_ts) / current_price * 100
                ts_pct  = max(TS_MIN_PCT_B, min(TS_MAX_PCT_B, ts_pct))
                new_ts  = current_price * (1 - ts_pct / 100)
            else:
                new_ts = current_price * (1 - TS_MIN_PCT_B / 100)
            pos["trailing_stop"] = round(new_ts, 8)

            # Alerte unique quand TS > entrée (position sécurisée)
            if not pos.get("ts_secured_notified", False) and pos["trailing_stop"] > pos["entry"]:
                pos["ts_secured_notified"] = True
                atr_v = pos.get("atr", 0)
                ts_pnl, ts_pct_val = self._calc_ts_result(
                    pos["size_usdc"], pos["entry"], pos["highest"], atr_v
                )
                sym   = pos["symbol"]
                alert = (
                    f"🔒 *[B] Position sécurisée — {sym}*\n"
                    f"TS au-dessus de l'entrée — gain minimum garanti.\n"
                    f"Gain si TS : `{ts_pnl:+.2f}` USDC (`{ts_pct_val:+.2f}%`)\n"
                    f"Plus haut : `{pos['highest']:.6f}` | TS : `{pos['trailing_stop']:.6f}`"
                )
        return alert

    # ── TP partiels ───────────────────────────────────────────────────────────
    def _check_tp(self, symbol: str, pos: dict, current_price: float) -> str:
        """
        TP1 : +6% fixe → vend 40% de la position initiale
        TP2 : +12% fixe → vend 40% de la position initiale
        Reste 20% en trailing stop ×1.5 ATR
        """
        msg = ""

        # TP1
        if not pos.get("tp1_done") and current_price >= pos["tp1_price"]:
            sell_usdc    = pos["size_usdc_initial"] * TP1_RATIO_B
            sell_usdc    = min(sell_usdc, pos["size_usdc"])
            pnl_u, pnl_p = self._calc_pnl(sell_usdc, pos["entry"], current_price)
            pos["size_usdc"]        -= sell_usdc
            self.capital            += sell_usdc + pnl_u
            self.pnl                += pnl_u
            pos["tp1_done"]          = True
            pos["secured_pnl_usdc"]  = pos.get("secured_pnl_usdc", 0.0) + pnl_u
            self.total_pnl_history.append([round(pnl_u, 4), round(pnl_p, 4)])
            self._save_state()
            log.info(f"[B][TP1] {symbol} @ {current_price:.6f} | +{pnl_u:.2f} USDC")
            msg = (
                f"🎯 *[B] TP1 — {symbol}*\n"
                f"Vente 40% à `{current_price:.6f}` (+6%)\n"
                f"G/P : `{pnl_u:+.2f}` USDC (`{pnl_p:+.2f}%`)\n"
                f"Reste : `{pos['size_usdc']:.2f}` USDC (60% position)"
            )

        # TP2
        elif pos.get("tp1_done") and not pos.get("tp2_done") and current_price >= pos["tp2_price"]:
            sell_usdc    = pos["size_usdc_initial"] * TP2_RATIO_B
            sell_usdc    = min(sell_usdc, pos["size_usdc"])
            pnl_u, pnl_p = self._calc_pnl(sell_usdc, pos["entry"], current_price)
            pos["size_usdc"]        -= sell_usdc
            self.capital            += sell_usdc + pnl_u
            self.pnl                += pnl_u
            pos["tp2_done"]          = True
            pos["secured_pnl_usdc"]  = pos.get("secured_pnl_usdc", 0.0) + pnl_u
            self.total_pnl_history.append([round(pnl_u, 4), round(pnl_p, 4)])
            self._save_state()
            log.info(f"[B][TP2] {symbol} @ {current_price:.6f} | +{pnl_u:.2f} USDC")
            msg = (
                f"🎯 *[B] TP2 — {symbol}*\n"
                f"Vente 40% à `{current_price:.6f}` (+12%)\n"
                f"G/P : `{pnl_u:+.2f}` USDC (`{pnl_p:+.2f}%`)\n"
                f"Reste : `{pos['size_usdc']:.2f}` USDC (20% en TS)"
            )

        return msg

    # ── Ouverture de position ─────────────────────────────────────────────────
    def _open_position(self, symbol: str, analysis: dict) -> str:
        if symbol in self.positions:
            return ""
        if len(self.positions) >= MAX_POSITIONS_B:
            return ""
        size = self._position_size()
        if size <= 0:
            return ""

        price     = analysis["price"]
        atr       = analysis["atr"]
        qty       = size / price
        opened_at = datetime.utcnow().isoformat()
        sl_price, ts_price = self._compute_stops(price, atr)

        # TP en % fixe
        tp1_price = round(price * (1 + TP1_PCT_B / 100), 8)
        tp2_price = round(price * (1 + TP2_PCT_B / 100), 8)

        self.positions[symbol] = {
            "symbol":              symbol,
            "entry":               price,
            "qty":                 qty,
            "size_usdc":           size,
            "size_usdc_initial":   size,
            "highest":             price,
            "atr":                 atr,
            "stop_loss":           sl_price,
            "trailing_stop":       ts_price,
            "tp1_price":           tp1_price,
            "tp2_price":           tp2_price,
            "tp1_done":            False,
            "tp2_done":            False,
            "opened_at":           opened_at,
            "ts_secured_notified": False,
        }
        self.capital -= size
        self._save_state()

        sl_pct   = (price - sl_price)  / price * 100
        ts_pct   = (price - ts_price)  / price * 100
        opened_f = datetime.fromisoformat(opened_at).strftime("%d/%m %H:%M")

        log.info(f"[B][PAPER] ACHAT {symbol} @ {price:.6f} | {size:.2f} USDC | ATR={atr:.6f}")
        return (
            f"✅ *[B] Entrée — {symbol}*\n"
            f"Ouvert le {opened_f}\n"
            f"Prix : `{price:.6f}` | Investi : `{size:.2f}` USDC | ATR : `{atr:.6f}`\n"
            f"SL : `{sl_price:.6f}` (-{sl_pct:.1f}%) | TS : `{ts_price:.6f}` (-{ts_pct:.1f}%)\n"
            f"TP1 : `{tp1_price:.6f}` (+{TP1_PCT_B:.0f}%, vente 40%) | "
            f"TP2 : `{tp2_price:.6f}` (+{TP2_PCT_B:.0f}%, vente 40%)\n"
            f"Reste 20% en TS ×{ATR_TS_MULT_B} ATR\n\n"
            f"📊 *Signal B :*\n{analysis['details']}"
        )

    # ── Clôture complète ──────────────────────────────────────────────────────
    def _close_position(self, symbol: str, price: float, reason: str) -> str:
        pos = self.positions.pop(symbol, None)
        if not pos:
            return ""

        pnl_usdc, pnl_pct = self._calc_pnl(pos["size_usdc"], pos["entry"], price)
        self.pnl     += pnl_usdc
        self.capital += pos["size_usdc"] + pnl_usdc
        self.total_trades += 1

        if pnl_usdc >= 0:
            self.wins += 1
        else:
            self.losses += 1
        self.total_pnl_history.append([round(pnl_usdc, 4), round(pnl_pct, 4)])

        # ── Cooldowns re-entry ────────────────────────────────────────────────
        tp1_done = pos.get("tp1_done", False)
        if reason == "Stop Loss":
            cooldown_h = COOLDOWN_SL_B
        elif "Abandon" in reason:
            cooldown_h = COOLDOWN_ABANDON_B
        elif reason == "Trailing Stop" and tp1_done:
            cooldown_h = COOLDOWN_TS_TP1_B
        else:
            cooldown_h = COOLDOWN_TS_ONLY_B
        self._cooldowns[symbol] = (
            datetime.utcnow() + timedelta(hours=cooldown_h)
        ).isoformat()

        self._save_state()

        opened_fmt = (datetime.fromisoformat(pos["opened_at"]).strftime("%d/%m %H:%M")
                      if pos.get("opened_at") else "—")
        closed_fmt = datetime.utcnow().strftime("%d/%m %H:%M")
        size_init  = pos.get("size_usdc_initial", pos["size_usdc"])
        secured    = pos.get("secured_pnl_usdc", 0.0)
        tp1_done   = pos.get("tp1_done", False)
        tp2_done   = pos.get("tp2_done", False)

        total_pnl_usdc = secured + pnl_usdc
        total_pnl_pct  = total_pnl_usdc / size_init * 100 if size_init > 0 else 0.0

        # Reconstitution détail TP
        tp1_usdc, tp2_usdc = 0.0, 0.0
        if tp1_done and tp2_done:
            tp1_usdc = secured * (TP1_RATIO_B / (TP1_RATIO_B + TP2_RATIO_B))
            tp2_usdc = secured * (TP2_RATIO_B / (TP1_RATIO_B + TP2_RATIO_B))
        elif tp1_done:
            tp1_usdc = secured

        tp1_pct = tp1_usdc / size_init * 100 if size_init > 0 and tp1_done else 0.0
        tp2_pct = tp2_usdc / (size_init * TP1_RATIO_B) * 100 if size_init > 0 and tp2_done else 0.0

        log.info(f"[B][PAPER] CLÔTURE {symbol} @ {price:.6f} | "
                 f"G/P : {pnl_usdc:+.2f} USDC ({pnl_pct:+.2f}%) | {reason}")

        lines = [
            f"❌ *[B] Clôture — {symbol}* — {reason}",
            f"Ouvert {opened_fmt} → Clôturé {closed_fmt}",
            f"Entrée : `{pos['entry']:.6f}` | Sortie : `{price:.6f}`",
            f"Investi initial : `{size_init:.2f}` USDC",
        ]
        if tp1_done:
            lines.append(f"🎯 TP1 : `{tp1_usdc:+.2f}` USDC (`{tp1_pct:+.2f}%`)")
        if tp2_done:
            lines.append(f"🎯 TP2 : `{tp2_usdc:+.2f}` USDC (`{tp2_pct:+.2f}%`)")
        lines.append(f"📉 Clôture reste : `{pnl_usdc:+.2f}` USDC (`{pnl_pct:+.2f}%`)")
        lines.append(
            f"📊 *Total : `{total_pnl_usdc:+.2f}` USDC "
            f"(`{total_pnl_pct:+.2f}%` sur investi initial)*"
        )
        return "\n".join(lines)

    # ── Clôture manuelle ──────────────────────────────────────────────────────
    def close_position_manual(self, symbol: str) -> str:
        sym = symbol.upper()
        if sym not in self.positions:
            return f"❓ [B] Aucune position ouverte sur `{sym}`."
        try:
            price = float(self.exchange.fetch_ticker(sym)["last"])
        except Exception as e:
            return f"⚠️ [B] Impossible de récupérer le prix : {e}"
        return self._close_position(sym, price, "Clôture manuelle")

    def close_all_manual(self) -> list:
        messages = []
        for symbol in list(self.positions.keys()):
            msg = self.close_position_manual(symbol)
            if msg:
                messages.append(msg)
        return messages

    # ── Scan principal ────────────────────────────────────────────────────────
    def scan(self) -> list[str]:
        alerts = []

        # Étape 1 : gestion positions ouvertes (TOUJOURS, même si drawdown)
        for symbol in list(self.positions.keys()):
            try:
                current_price = float(self.exchange.fetch_ticker(symbol)["last"])
                pos           = self.positions[symbol]

                tp_msg = self._check_tp(symbol, pos, current_price)
                if tp_msg:
                    alerts.append(tp_msg)

                ts_alert = self._update_trailing_stop(pos, current_price)
                if ts_alert:
                    alerts.append(ts_alert)

                sl_hit, sl_price = self._check_stop_triggered(
                    symbol, current_price, pos["stop_loss"], label="SL"
                )
                if sl_hit:
                    msg = self._close_position(symbol, sl_price, "Stop Loss")
                    if msg:
                        alerts.append(msg)
                    continue

                ts_hit, ts_price = self._check_stop_triggered(
                    symbol, current_price, pos["trailing_stop"], label="TS"
                )
                if ts_hit:
                    msg = self._close_position(symbol, ts_price, "Trailing Stop")
                    if msg:
                        alerts.append(msg)

            except Exception as e:
                log.error(f"[B] Erreur mise à jour {symbol} : {e}")

        # Étape 2 : blocage nouvelles entrées si drawdown
        if self._daily_drawdown_reached():
            log.warning("[B] Drawdown journalier atteint — nouvelles entrées bloquées")
            return alerts

        # Étape 3 : régime BTC — bear mode ajuste les filtres (ne bloque pas B)
        btc_ok    = self._btc_regime_ok()
        bear_mode = not btc_ok
        if bear_mode:
            log.info("[B] Régime BTC baissier — filtres resserrés (Vol×4, RSI 65–75)")

        # Étape 4 : nouvelles entrées
        if len(self.positions) < MAX_POSITIONS_B and self._position_size() > 0:
            now_utc = datetime.utcnow()
            for symbol in self._get_usdc_pairs():
                if symbol in self.positions:
                    continue
                if len(self.positions) >= MAX_POSITIONS_B:
                    break

                # Cooldown re-entry
                if symbol in self._cooldowns:
                    try:
                        until = datetime.fromisoformat(self._cooldowns[symbol])
                        if now_utc < until:
                            continue
                        else:
                            del self._cooldowns[symbol]
                    except Exception:
                        del self._cooldowns[symbol]

                # Skip list manuelle
                if symbol in self._skip_list:
                    try:
                        until = datetime.fromisoformat(self._skip_list[symbol])
                        if now_utc < until:
                            continue
                        else:
                            del self._skip_list[symbol]
                    except Exception:
                        del self._skip_list[symbol]

                try:
                    analysis = self._analyze_signal(symbol, bear_mode=bear_mode)
                    if analysis["valid"]:
                        msg = self._open_position(symbol, analysis)
                        if msg:
                            alerts.append(msg)
                except Exception as e:
                    log.debug(f"[B] Skip {symbol} : {e}")

        return alerts

    # ── Analyse matin (7h) ────────────────────────────────────────────────────
    def morning_analysis(self) -> list[str]:
        """
        B est court terme : abandon des positions > 2 jours sans TP1.
        """
        today = datetime.now(PARIS_TZ).date().isoformat()
        if self._morning_done_date == today:
            return []
        self._morning_done_date = today
        self.reset_daily_pnl()
        self._save_state()

        if not self.positions:
            return ["☀️ *[B] Analyse matinale* — Aucune position ouverte."]

        messages = ["☀️ *[B] Analyse matinale des positions*\n"]
        for symbol in list(self.positions.keys()):
            pos = self.positions.get(symbol)
            try:
                price = float(self.exchange.fetch_ticker(symbol)["last"])
            except Exception:
                price = pos["entry"]

            pnl_usdc, pnl_pct = self._calc_pnl(pos["size_usdc"], pos["entry"], price)
            opened_fmt = (datetime.fromisoformat(pos["opened_at"]).strftime("%d/%m %H:%M")
                          if pos.get("opened_at") else "—")
            days_open  = (datetime.utcnow() - datetime.fromisoformat(pos["opened_at"])).days \
                          if pos.get("opened_at") else 0
            too_old    = days_open >= ABANDON_DAYS_B and not pos.get("tp1_done", False)

            tp_status = ""
            if pos.get("tp1_done") and pos.get("tp2_done"):
                tp_status = "TP1 ✅ TP2 ✅ — reste 20% en TS"
            elif pos.get("tp1_done"):
                tp_status = "TP1 ✅ — en attente TP2"
            else:
                tp_status = "TP1 ⏳ TP2 ⏳"

            if too_old:
                verdict = f"❌ *Abandonner* — momentum retombé depuis {days_open}j sans TP1"
            else:
                verdict = "✅ *Garder* — dans les délais"

            messages.append(
                f"─────────────────\n"
                f"📌 `{symbol}` | Ouvert le {opened_fmt} ({days_open}j)\n"
                f"G/P actuel : `{pnl_usdc:+.2f}` USDC (`{pnl_pct:+.2f}%`)\n"
                f"TP : {tp_status}\n"
                f"Verdict : {verdict}"
            )

            if too_old:
                close_msg = self._close_position(
                    symbol, price, f"Abandon B — momentum retombé ({days_open}j sans TP1)"
                )
                if close_msg:
                    messages.append(close_msg)

        return messages

    # ── Diagnostic marché B ───────────────────────────────────────────────────
    def scan_market_summary(self, force: bool = False) -> str:
        """
        Top 10 paires les plus proches du signal B.
        force=True : affiche même si positions ouvertes (via /boostb).
        """
        nb_pos = len(self.positions)
        if nb_pos > 0 and not force:
            return ""

        now_str  = datetime.now(PARIS_TZ).strftime("%H:%M")
        btc_ok   = self._btc_regime_ok()
        regime   = "✅ Régime BTC haussier" if btc_ok else "❌ Régime BTC baissier"
        pos_line = ("Aucune position ouverte"
                    if nb_pos == 0 else f"⚠️ {nb_pos} position(s) — diagnostic forcé")

        lines = [
            f"📡 *[B] Scan marché — {now_str}*",
            pos_line,
            regime,
            "",
            "*Top 10 paires les plus proches du signal B :*",
        ]

        if not btc_ok:
            lines.append("🔴 *Bear mode actif — filtres resserrés (Vol×4, RSI 65–75)*")
            lines.append("_Entrées autorisées mais conditionnées — surveillance active :_")
            lines.append("")

        pairs   = self._get_usdc_pairs()[:60]  # B a plus de paires (1M USDC)
        results = []

        for symbol in pairs:
            try:
                analysis = self._analyze_signal(symbol)
                details  = analysis.get("details", "")
                score    = details.count("✅")
                total    = score + details.count("❌")
                results.append({
                    "symbol":  symbol,
                    "score":   score,
                    "total":   total,
                    "valid":   analysis["valid"],
                    "details": details,
                })
            except Exception as e:
                log.debug(f"[B] scan_market_summary skip {symbol} : {e}")

        results.sort(key=lambda x: (x["valid"], x["score"]), reverse=True)
        top10 = results[:10]

        if not top10:
            lines.append("ℹ️ Aucune paire analysée.")
            return "\n".join(lines)

        for r in top10:
            symbol = r["symbol"]
            score  = r["score"]
            total  = r["total"]
            if r["valid"]:
                dot    = "🟢"
                status = "✅ Signal B complet"
            else:
                failed = []
                for line in r["details"].split("\n"):
                    if not line.startswith("❌"):
                        continue
                    if "Momentum" in line:
                        failed.append("Momentum")
                    elif "Volume" in line:
                        failed.append("Vol")
                    elif "RSI" in line:
                        failed.append("RSI")
                    elif "Spread" in line:
                        failed.append("Spread")
                fail_str = f" — ❌ {', '.join(failed[:3])}" if failed else ""
                dot      = "🟡" if score >= (total * 0.6) else "🔴"
                status   = f"({score}/{total}){fail_str}"
            lines.append(f"{dot} `{symbol}` — {status}")

        return "\n".join(lines)

    # ── Entrée forcée (/buyb SYMBOL) ─────────────────────────────────────────
    def open_position_manual(self, symbol: str) -> str:
        sym = symbol.upper()
        if sym in self.positions:
            return f"⚠️ [B] Position déjà ouverte sur `{sym}`."
        if len(self.positions) >= MAX_POSITIONS_B:
            return f"⚠️ [B] Nombre max de positions atteint ({MAX_POSITIONS_B})."
        size = self._position_size()
        if size <= 0:
            return "⚠️ [B] Capital insuffisant pour ouvrir une position."
        try:
            price = float(self.exchange.fetch_ticker(sym)["last"])
        except Exception as e:
            return f"⚠️ [B] Impossible de récupérer le prix : {e}"
        try:
            df  = self._fetch_ohlcv(sym, "1h", limit=20)
            atr = self._atr(df) if df is not None and len(df) >= 14 else price * 0.02
        except Exception:
            atr = price * 0.02
        analysis = {
            "valid":   True,
            "price":   price,
            "atr":     atr,
            "details": "⚡ Entrée manuelle forcée — filtres ignorés",
        }
        self._cooldowns.pop(sym, None)
        self._skip_list.pop(sym, None)
        return self._open_position(sym, analysis)

    # ── Skip (/skipb SYMBOL) ─────────────────────────────────────────────────
    def skip_symbol(self, symbol: str, hours: int = 24) -> str:
        sym   = symbol.upper()
        until = (datetime.utcnow() + timedelta(hours=hours)).isoformat()
        self._skip_list[sym] = until
        self._save_state()
        return f"⛔ [B] `{sym}` ignoré pendant {hours}h."

    # ── Debug (/debugb SYMBOL) ───────────────────────────────────────────────
    def debug_position(self, symbol: str) -> str:
        sym = symbol.upper()
        pos = self.positions.get(sym)
        if not pos:
            return f"❓ [B] Aucune position ouverte sur `{sym}`."
        try:
            current_price = float(self.exchange.fetch_ticker(sym)["last"])
        except Exception as e:
            return f"⚠️ [B] Impossible de récupérer le prix : {e}"

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
            f"🔍 *[B] Debug — {sym}*\n\n"
            f"Prix actuel : `{current_price:.6f}`\n"
            f"Entrée : `{pos['entry']:.6f}` | Plus haut : `{pos['highest']:.6f}`\n"
            f"ATR : `{atr_v:.6f}`\n\n"
            f"SL : `{pos['stop_loss']:.6f}` {'🔴 DÉCLENCHÉ' if sl_triggered else '🟢 OK'}\n"
            f"TS : `{pos['trailing_stop']:.6f}` {'🔴 DÉCLENCHÉ' if ts_triggered else '🟢 OK'}\n"
            f"TP1 : `{pos.get('tp1_price', 0):.6f}` (+{TP1_PCT_B:.0f}%) "
            f"{'✅ FAIT' if pos.get('tp1_done') else ('🎯 ATTEINT' if tp1_triggered else '⏳')}\n"
            f"TP2 : `{pos.get('tp2_price', 0):.6f}` (+{TP2_PCT_B:.0f}%) "
            f"{'✅ FAIT' if pos.get('tp2_done') else ('🎯 ATTEINT' if tp2_triggered else '⏳')}\n\n"
            f"G/P actuel : `{pnl_usdc:+.2f}` USDC (`{pnl_pct:+.2f}%`)\n"
            f"Résultat min si TS : `{ts_pnl:+.2f}` USDC (`{ts_pct:+.2f}%`)\n"
            f"Investi restant : `{pos['size_usdc']:.2f}` USDC "
            f"(initial : `{pos.get('size_usdc_initial', pos['size_usdc']):.2f}` USDC)"
        )

    # ── Cooldowns ─────────────────────────────────────────────────────────────
    def get_cooldowns_status(self) -> str:
        now = datetime.utcnow()
        lines = []
        for symbol, until_iso in self._cooldowns.items():
            try:
                until = datetime.fromisoformat(until_iso)
                if now < until:
                    h = int((until - now).total_seconds() / 3600)
                    lines.append(f"🔄 `{symbol}` — cooldown {h}h restantes")
            except Exception:
                pass
        for symbol, until_iso in self._skip_list.items():
            try:
                until = datetime.fromisoformat(until_iso)
                if now < until:
                    h = int((until - now).total_seconds() / 3600)
                    lines.append(f"⛔ `{symbol}` — skip {h}h restantes")
            except Exception:
                pass
        return "\n".join(lines) if lines else "✅ Aucun cooldown ni blacklist actif."

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
                "symbol":            symbol,
                "entry":             pos["entry"],
                "current":           price,
                "size_usdc":         pos["size_usdc"],
                "size_usdc_initial": pos.get("size_usdc_initial", pos["size_usdc"]),
                "highest":           pos["highest"],
                "ts_price":          pos["trailing_stop"],
                "tp1_price":         pos.get("tp1_price", 0),
                "tp2_price":         pos.get("tp2_price", 0),
                "tp1_done":          pos.get("tp1_done", False),
                "tp2_done":          pos.get("tp2_done", False),
                "pnl_usdc":          pnl_usdc,
                "pnl_pct":           pnl_pct,
                "ts_pnl":            ts_pnl,
                "ts_pct":            ts_pct,
                "secured_pnl_usdc":  pos.get("secured_pnl_usdc", 0.0),
                "opened_at":         opened_fmt,
            })
        return result
