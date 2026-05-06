import ccxt
import pandas as pd
import json
import os
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# ── Paramètres ────────────────────────────────────────────────────────────────
CAPITAL_TOTAL       = 100.0
MAX_POSITIONS       = 5
POSITION_SIZE_MIN   = 15.0   # USDC minimum par position
TRAILING_STOP_PCT   = 2.0    # % depuis le plus haut
STOP_LOSS_PCT       = 2.0    # % depuis l'entrée
MAX_DAILY_LOSS_USDC = 10.0   # perte journalière max en USDC
RSI_MAX_ENTRY       = 65     # ne pas entrer si RSI > cette valeur
VOLUME_MULTIPLIER   = 2.0    # volume min vs moyenne pour entrer
TIMEFRAME_SHORT     = "1h"
TIMEFRAME_LONG      = "4h"
PERSISTENCE_FILE    = "positions.json"

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
            self.positions          = state.get("positions", {})
            self.capital            = state.get("capital", CAPITAL_TOTAL)
            self.pnl                = state.get("pnl", 0.0)
            self.daily_start_pnl    = state.get("daily_start_pnl", 0.0)
            self.total_trades       = state.get("total_trades", 0)
            self.wins               = state.get("wins", 0)
            self.losses             = state.get("losses", 0)
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
        """
        Retourne (pnl_usdc, pnl_pct) toujours cohérents.
        Basé sur size_usdc pour éviter les dérives de qty.
        pnl_usdc positif <=> pnl_pct positif, toujours.
        """
        pnl_usdc = (size_usdc / entry) * price - size_usdc
        pnl_pct  = (price - entry) / entry * 100
        return round(pnl_usdc, 4), round(pnl_pct, 4)

    # ── Résultat minimum garanti si TS déclenché ──────────────────────────────
    @staticmethod
    def _calc_ts_result(size_usdc: float, entry: float, highest: float) -> tuple[float, float]:
        """
        Résultat si TS se déclenche depuis le plus haut actuel.
        Prix de sortie TS = highest * (1 - TRAILING_STOP_PCT / 100)
        """
        ts_exit  = highest * (1 - TRAILING_STOP_PCT / 100)
        pnl_usdc = (size_usdc / entry) * ts_exit - size_usdc
        pnl_pct  = (ts_exit - entry) / entry * 100
        return round(pnl_usdc, 4), round(pnl_pct, 4)

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

    # ── RSI ───────────────────────────────────────────────────────────────────
    def _rsi(self, series: pd.Series, period: int = 14) -> float:
        delta = series.diff()
        gain  = delta.clip(lower=0).rolling(period).mean()
        loss  = (-delta.clip(upper=0)).rolling(period).mean()
        rs    = gain / loss.replace(0, 1e-10)
        return float((100 - (100 / (1 + rs))).iloc[-1])

    # ── Signal d'entrée (1h + 4h + RSI + volume) ─────────────────────────────
    def _analyze_signal(self, symbol: str) -> dict:
        result = {"valid": False, "price": 0.0, "details": ""}

        df1 = self._fetch_ohlcv(symbol, TIMEFRAME_SHORT, limit=60)
        if df1 is None or len(df1) < 50:
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
        rsi_ok     = rsi_val < RSI_MAX_ENTRY

        df4 = self._fetch_ohlcv(symbol, TIMEFRAME_LONG, limit=60)
        ema4h_ok  = False
        ema4h_str = "Données insuffisantes"
        if df4 is not None and len(df4) >= 50:
            df4["ema20"] = df4["close"].ewm(span=20).mean()
            df4["ema50"] = df4["close"].ewm(span=50).mean()
            last4     = df4.iloc[-1]
            ema4h_ok  = bool(last4["ema20"] > last4["ema50"])
            ema4h_str = f"EMA20 ({last4['ema20']:.4f}) {'>' if ema4h_ok else '<'} EMA50 ({last4['ema50']:.4f})"

        result["price"] = float(last1["close"])
        result["valid"] = ema1h_ok and close1h_ok and vol_ok and rsi_ok and ema4h_ok
        result["details"] = "\n".join([
            f"{'✅' if ema1h_ok   else '❌'} [1h] EMA20 ({last1['ema20']:.4f}) {'>' if ema1h_ok else '<'} EMA50 ({last1['ema50']:.4f})",
            f"{'✅' if close1h_ok else '❌'} [1h] Clôture ({last1['close']:.4f}) {'>' if close1h_ok else '<'} EMA20",
            f"{'✅' if vol_ok     else '❌'} [1h] Volume ×{vol_ratio:.2f} (seuil ×{VOLUME_MULTIPLIER})",
            f"{'✅' if rsi_ok     else '❌'} [1h] RSI {rsi_val:.1f} ({'OK' if rsi_ok else f'> {RSI_MAX_ENTRY} surachat'})",
            f"{'✅' if ema4h_ok   else '❌'} [4h] {ema4h_str}",
        ])
        return result

    # ── Clôture manuelle ─────────────────────────────────────────────
    def close_position_manual(self, symbol: str) -> str:
        """Ferme une position au prix marché sur demande manuelle."""
        sym = symbol.upper()
        if sym not in self.positions:
            return f"❓ Aucune position ouverte sur `{sym}`."
        try:
            price = float(self.exchange.fetch_ticker(sym)["last"])
        except Exception as e:
            return f"⚠️ Impossible de récupérer le prix : {e}"
        return self._close_position(sym, price, "Clôture manuelle")

    def close_all_manual(self) -> list:
        """Ferme toutes les positions au prix marché."""
        messages = []
        for symbol in list(self.positions.keys()):
            msg = self.close_position_manual(symbol)
            if msg:
                messages.append(msg)
        return messages

    # ── Analyse matin (tendance uniquement, sans volume ni RSI) ───────────────
    def _analyze_morning(self, symbol: str) -> dict:
        result = {"valid": False, "price": 0.0, "details": ""}

        df1 = self._fetch_ohlcv(symbol, TIMEFRAME_SHORT, limit=60)
        if df1 is None or len(df1) < 50:
            result["details"] = "Données 1h insuffisantes"
            return result

        df1["ema20"] = df1["close"].ewm(span=20).mean()
        df1["ema50"] = df1["close"].ewm(span=50).mean()
        last1 = df1.iloc[-1]

        ema1h_ok   = bool(last1["ema20"] > last1["ema50"])
        close1h_ok = bool(last1["close"] > last1["ema20"])

        df4 = self._fetch_ohlcv(symbol, TIMEFRAME_LONG, limit=60)
        ema4h_ok  = False
        ema4h_str = "Données insuffisantes"
        if df4 is not None and len(df4) >= 50:
            df4["ema20"] = df4["close"].ewm(span=20).mean()
            df4["ema50"] = df4["close"].ewm(span=50).mean()
            last4     = df4.iloc[-1]
            ema4h_ok  = bool(last4["ema20"] > last4["ema50"])
            ema4h_str = f"EMA20 ({last4['ema20']:.4f}) {'>' if ema4h_ok else '<'} EMA50 ({last4['ema50']:.4f})"

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
            log.info(f"[{label}] {symbol} — prix {current_price:.6f} <= {stop_level:.6f}")
            return True, current_price
        try:
            df_1m = self._fetch_ohlcv(symbol, "1m", limit=3)
            if df_1m is not None and len(df_1m) >= 2:
                last_low = float(df_1m.iloc[-2]["low"])
                if last_low <= stop_level:
                    log.warning(f"[{label}] {symbol} — low 1m {last_low:.6f} <= {stop_level:.6f}")
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
        qty       = size / price
        opened_at = datetime.utcnow().isoformat()

        self.positions[symbol] = {
            "symbol":        symbol,
            "entry":         price,
            "qty":           qty,
            "size_usdc":     size,
            "highest":       price,
            "stop_loss":     round(price * (1 - STOP_LOSS_PCT    / 100), 8),
            "trailing_stop":       round(price * (1 - TRAILING_STOP_PCT / 100), 8),
            "opened_at":             opened_at,
            "ts_secured_notified":   False,
        }
        self.capital -= size
        self._save_state()
        log.info(f"[PAPER] ACHAT {symbol} @ {price:.6f} | {size:.2f} USDC")

        sl_price   = price * (1 - STOP_LOSS_PCT / 100)
        ts_init    = price * (1 - TRAILING_STOP_PCT / 100)
        opened_fmt = datetime.fromisoformat(opened_at).strftime("%d/%m %H:%M")

        return (
            f"✅ *Entrée — {symbol}*\n"
            f"Ouvert le {opened_fmt}\n"
            f"Prix : `{price:.6f}` | Investi : `{size:.2f}` USDC\n"
            f"SL : `{sl_price:.6f}` (-{STOP_LOSS_PCT}%) | TS : `{ts_init:.6f}` (-{TRAILING_STOP_PCT}%)\n\n"
            f"📐 *Signal :*\n{analysis['details']}"
        )

    def _update_trailing_stop(self, pos: dict, current_price: float) -> str:
        """
        Met à jour le TS si nouveau plus haut.
        Retourne une alerte si le TS dépasse le prix d'entrée pour la première fois.
        """
        alert = ""
        if current_price > pos["highest"]:
            pos["highest"]       = current_price
            pos["trailing_stop"] = round(current_price * (1 - TRAILING_STOP_PCT / 100), 8)
            log.debug("TS remonté %s highest=%.6f TS=%.6f",
                      pos["symbol"], current_price, pos["trailing_stop"])
            # Alerte unique quand TS > entrée (gain garanti)
            if not pos.get("ts_secured_notified", False) and pos["trailing_stop"] > pos["entry"]:
                pos["ts_secured_notified"] = True
                ts_pnl, ts_pct = self._calc_ts_result(
                    pos["size_usdc"], pos["entry"], pos["highest"]
                )
                sym    = pos["symbol"]
                high_s = f'{pos["highest"]:.6f}'
                ts_s   = f'{pos["trailing_stop"]:.6f}'
                pnl_s  = f'{ts_pnl:+.2f}'
                pct_s  = f'{ts_pct:+.2f}'
                alert  = (
                    f"🔒 *Position sécurisée — {sym}*\n"
                    f"Le TS est au-dessus du prix d'entrée — gain minimum garanti.\n"
                    f"Gain si TS : `{pnl_s}` USDC (`{pct_s}%`)\n"
                    f"Plus haut : `{high_s}` | TS : `{ts_s}`"
                )
        return alert

    # ── Clôture ───────────────────────────────────────────────────────────────
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
        self._save_state()
        log.info(f"[PAPER] CLÔTURE {symbol} @ {price:.6f} | G/P : {pnl_usdc:+.2f} USDC ({pnl_pct:+.2f}%) | {reason}")

        opened_at  = pos.get("opened_at", "")
        closed_at  = datetime.utcnow().isoformat()
        opened_fmt = datetime.fromisoformat(opened_at).strftime("%d/%m %H:%M") if opened_at else "—"
        closed_fmt = datetime.fromisoformat(closed_at).strftime("%d/%m %H:%M")

        return (
            f"❌ *Clôture — {symbol}* — {reason}\n"
            f"Ouvert {opened_fmt} → Clôturé {closed_fmt}\n"
            f"Entrée : `{pos['entry']:.6f}` | Sortie : `{price:.6f}`\n"
            f"Investi : `{pos['size_usdc']:.2f}` USDC\n"
            f"G/P : `{pnl_usdc:+.2f}` USDC (`{pnl_pct:+.2f}%`)"
        )

    # ── Clôture manuelle ─────────────────────────────────────────────
    def close_position_manual(self, symbol: str) -> str:
        """Ferme une position au prix marché sur demande manuelle."""
        sym = symbol.upper()
        if sym not in self.positions:
            return f"❓ Aucune position ouverte sur `{sym}`."
        try:
            price = float(self.exchange.fetch_ticker(sym)["last"])
        except Exception as e:
            return f"⚠️ Impossible de récupérer le prix : {e}"
        return self._close_position(sym, price, "Clôture manuelle")

    def close_all_manual(self) -> list:
        """Ferme toutes les positions au prix marché."""
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
            pos = self.positions.get(symbol)
            analysis = self._analyze_morning(symbol)
            try:
                price = float(self.exchange.fetch_ticker(symbol)["last"])
            except Exception:
                price = pos["entry"]

            pnl_usdc, pnl_pct = self._calc_pnl(pos["size_usdc"], pos["entry"], price)
            ts_pnl, ts_pct     = self._calc_ts_result(pos["size_usdc"], pos["entry"], pos["highest"])
            verdict            = "✅ *Garder*" if analysis["valid"] else "❌ *Abandonner*"
            opened_fmt         = datetime.fromisoformat(pos["opened_at"]).strftime("%d/%m %H:%M") if pos.get("opened_at") else "—"

            msg = (
                f"─────────────────\n"
                f"📌 `{symbol}` | Ouvert le {opened_fmt}\n"
                f"G/P actuel : `{pnl_usdc:+.2f}` USDC (`{pnl_pct:+.2f}%`)\n"
                f"💵 Résultat min si TS : `{ts_pnl:+.2f}` USDC (`{ts_pct:+.2f}%`)\n\n"
                f"*Indicateurs :*\n{analysis['details']}\n\n"
                f"Verdict : {verdict}"
            )
            messages.append(msg)

            if not analysis["valid"]:
                close_msg = self._close_position(symbol, price, "Abandon matin — tendance invalide")
                if close_msg:
                    messages.append(close_msg)

        return messages

    # ── Debug position (/debug SYMBOL) ────────────────────────────────────────
    def debug_position(self, symbol: str) -> str:
        sym = symbol.upper()
        pos = self.positions.get(sym)
        if not pos:
            return f"❓ Aucune position ouverte sur `{sym}`."
        try:
            current_price = float(self.exchange.fetch_ticker(sym)["last"])
        except Exception as e:
            return f"⚠️ Impossible de récupérer le prix : {e}"

        sl    = pos["stop_loss"]
        ts    = pos["trailing_stop"]
        high  = pos["highest"]
        entry = pos["entry"]
        size  = pos["size_usdc"]

        sl_triggered = current_price <= sl
        ts_triggered = current_price <= ts

        pnl_usdc, pnl_pct = self._calc_pnl(size, entry, current_price)
        ts_pnl, ts_pct     = self._calc_ts_result(size, entry, high)

        return (
            f"🔍 *Debug — {sym}*\n\n"
            f"Prix actuel : `{current_price:.6f}`\n"
            f"Entrée : `{entry:.6f}` | Plus haut : `{high:.6f}`\n"
            f"SL : `{sl:.6f}` {'🔴 DÉCLENCHÉ' if sl_triggered else '🟢 OK'}\n"
            f"TS : `{ts:.6f}` {'🔴 DÉCLENCHÉ' if ts_triggered else '🟢 OK'}\n\n"
            f"G/P actuel : `{pnl_usdc:+.2f}` USDC (`{pnl_pct:+.2f}%`)\n"
            f"Résultat min si TS : `{ts_pnl:+.2f}` USDC (`{ts_pct:+.2f}%`)\n"
            f"Investi : `{size:.2f}` USDC"
        )

    # ── SCAN ──────────────────────────────────────────────────────────────────
    def scan(self) -> list[str]:
        alerts = []

        # Étape 1 — sorties TOUJOURS vérifiées, même si drawdown atteint
        for symbol in list(self.positions.keys()):
            try:
                current_price = float(self.exchange.fetch_ticker(symbol)["last"])
                pos           = self.positions[symbol]
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
                log.error(f"Erreur mise à jour {symbol} : {e}")

        # Étape 2 — nouvelles entrées bloquées si drawdown
        if self._daily_drawdown_reached():
            log.warning("Drawdown journalier atteint — nouvelles entrées bloquées")
            return alerts

        # Étape 3 — nouvelles entrées
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

            pnl_usdc, pnl_pct = self._calc_pnl(pos["size_usdc"], pos["entry"], price)
            ts_pnl, ts_pct     = self._calc_ts_result(pos["size_usdc"], pos["entry"], pos["highest"])
            opened_fmt         = datetime.fromisoformat(pos["opened_at"]).strftime("%d/%m %H:%M") if pos.get("opened_at") else "—"

            result.append({
                "symbol":    symbol,
                "entry":     pos["entry"],
                "current":   price,
                "size_usdc": pos["size_usdc"],
                "highest":   pos["highest"],
                "ts_price":  pos["trailing_stop"],
                "pnl_usdc":  pnl_usdc,
                "pnl_pct":   pnl_pct,
                "ts_pnl":    ts_pnl,
                "ts_pct":    ts_pct,
                "opened_at": opened_fmt,
            })
        return result
