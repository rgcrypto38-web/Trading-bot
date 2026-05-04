import ccxt
import pandas as pd
import json
import os
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# ── Paramètres ────────────────────────────────────────────────────────────────
CAPITAL_TOTAL           = 100.0
POSITION_SIZE           = 20.0
MAX_POSITIONS           = 5
TRAILING_STOP_PCT       = 1.5    # % depuis le plus haut
STOP_LOSS_PCT           = 2.0    # % depuis l'entrée
MAX_DAILY_DRAWDOWN_PCT  = 10
TIMEFRAME               = "1h"
PERSISTENCE_FILE        = "positions.json"

PARIS_TZ = timezone(timedelta(hours=2))


class TradingStrategy:
    def __init__(self, binance_key: str = None, binance_secret: str = None):
        self.exchange = ccxt.binance({
            "apiKey":          binance_key or "",
            "secret":          binance_secret or "",
            "enableRateLimit": True,
            "options":         {"defaultType": "spot"},
        })
        self.positions:           dict  = {}
        self.capital:             float = CAPITAL_TOTAL
        self.daily_start_capital: float = CAPITAL_TOTAL
        self.pnl:                 float = 0.0
        self.total_trades:        int   = 0
        self.wins:                int   = 0
        self.losses:              int   = 0
        self._usdc_pairs:         list  = []
        self._last_pair_refresh:  float = 0
        self._morning_done_date:  str   = ""   # date ISO du dernier rapport matin

        self._load_state()

    # ── Persistance ───────────────────────────────────────────────────────────
    def _save_state(self):
        state = {
            "positions":           self.positions,
            "capital":             self.capital,
            "daily_start_capital": self.daily_start_capital,
            "pnl":                 self.pnl,
            "total_trades":        self.total_trades,
            "wins":                self.wins,
            "losses":              self.losses,
            "morning_done_date":   self._morning_done_date,
            "saved_at":            datetime.utcnow().isoformat(),
        }
        try:
            with open(PERSISTENCE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.error(f"Erreur sauvegarde état : {e}")

    def _load_state(self):
        if not os.path.exists(PERSISTENCE_FILE):
            log.info("Aucun état persisté — démarrage à zéro")
            return
        try:
            with open(PERSISTENCE_FILE, "r") as f:
                state = json.load(f)
            self.positions           = state.get("positions", {})
            self.capital             = state.get("capital", CAPITAL_TOTAL)
            self.daily_start_capital = state.get("daily_start_capital", CAPITAL_TOTAL)
            self.pnl                 = state.get("pnl", 0.0)
            self.total_trades        = state.get("total_trades", 0)
            self.wins                = state.get("wins", 0)
            self.losses              = state.get("losses", 0)
            self._morning_done_date  = state.get("morning_done_date", "")
            log.info(f"État rechargé — {len(self.positions)} position(s)")
        except Exception as e:
            log.error(f"Erreur chargement état : {e}")

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
            self._usdc_pairs       = pairs
            self._last_pair_refresh = now
            log.info(f"{len(pairs)} paires USDC actives")
        except Exception as e:
            log.error(f"Erreur chargement marchés : {e}")
        return self._usdc_pairs

    # ── OHLCV ─────────────────────────────────────────────────────────────────
    def _fetch_ohlcv(self, symbol: str, timeframe: str = None, limit: int = 50) -> pd.DataFrame | None:
        tf = timeframe or TIMEFRAME
        try:
            data = self.exchange.fetch_ohlcv(symbol, tf, limit=limit)
            df   = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "vol"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms")
            return df
        except Exception:
            return None

    # ── Signal momentum + explication ─────────────────────────────────────────
    def _analyze_signal(self, symbol: str) -> dict:
        """
        Retourne un dict :
          valid     : bool — signal valide ou non
          price     : float
          ema20     : float
          ema50     : float
          vol_ratio : float   — volume dernière bougie / moyenne
          details   : str     — explication textuelle
        """
        result = {"valid": False, "price": 0.0, "ema20": 0.0, "ema50": 0.0,
                  "vol_ratio": 0.0, "details": ""}

        df = self._fetch_ohlcv(symbol)
        if df is None or len(df) < 50:
            result["details"] = "Données insuffisantes"
            return result

        df["ema20"]  = df["close"].ewm(span=20).mean()
        df["ema50"]  = df["close"].ewm(span=50).mean()
        df["vol_ma"] = df["vol"].rolling(20).mean()
        last = df.iloc[-1]

        ema_ok  = last["ema20"] > last["ema50"]
        close_ok = last["close"] > last["ema20"]
        vol_ratio = last["vol"] / last["vol_ma"] if last["vol_ma"] > 0 else 0
        vol_ok   = vol_ratio > 1.2

        result["price"]     = last["close"]
        result["ema20"]     = last["ema20"]
        result["ema50"]     = last["ema50"]
        result["vol_ratio"] = vol_ratio
        result["valid"]     = ema_ok and close_ok and vol_ok

        # Explication textuelle
        checks = []
        checks.append(f"{'✅' if ema_ok   else '❌'} EMA20 ({last['ema20']:.4f}) {'>' if ema_ok else '<'} EMA50 ({last['ema50']:.4f})")
        checks.append(f"{'✅' if close_ok else '❌'} Clôture ({last['close']:.4f}) {'>' if close_ok else '<'} EMA20")
        checks.append(f"{'✅' if vol_ok   else '❌'} Volume ×{vol_ratio:.2f} la moyenne (seuil ×1.2)")
        result["details"] = "\n".join(checks)

        return result

    # ── Vérification SL robuste (prix actuel + low bougie 1m) ─────────────────
    def _check_sl_triggered(self, symbol: str, current_price: float, sl_level: float) -> tuple[bool, float]:
        """
        Retourne (déclenché, prix_effectif).
        Vérifie le prix actuel ET le low de la dernière bougie 1m.
        Si le low est sous le SL, considère le SL déclenché même si le prix actuel est remonté.
        """
        # Vérification directe
        if current_price <= sl_level:
            return True, current_price

        # Vérification sur le low de la dernière bougie 1m
        try:
            df_1m = self._fetch_ohlcv(symbol, timeframe="1m", limit=3)
            if df_1m is not None and len(df_1m) >= 2:
                last_low = df_1m.iloc[-2]["low"]   # bougie précédente clôturée
                if last_low <= sl_level:
                    log.warning(f"[SL] {symbol} — low bougie 1m ({last_low:.4f}) sous SL ({sl_level:.4f})")
                    return True, sl_level  # on coupe au niveau du SL
        except Exception as e:
            log.error(f"Erreur check low 1m {symbol} : {e}")

        return False, current_price

    # ── Gain sécurisé par position ────────────────────────────────────────────
    def _secured_gain(self, pos: dict) -> dict:
        """
        Retourne le gain/perte minimum garanti si le trailing stop se déclenche maintenant.
        """
        ts_price   = pos["trailing_stop"]
        entry      = pos["entry"]
        size       = pos["size_usdc"]
        secured_pnl     = (ts_price - entry) / entry * size
        secured_pct     = (ts_price - entry) / entry * 100
        return {
            "ts_price":    ts_price,
            "secured_pnl": secured_pnl,
            "secured_pct": secured_pct,
        }

    # ── Ouverture de position ─────────────────────────────────────────────────
    def _open_position(self, symbol: str, analysis: dict) -> str:
        if symbol in self.positions:
            return ""
        if len(self.positions) >= MAX_POSITIONS:
            return ""
        if self.capital < POSITION_SIZE:
            return ""

        price = analysis["price"]
        qty   = POSITION_SIZE / price
        self.positions[symbol] = {
            "symbol":        symbol,
            "entry":         price,
            "qty":           qty,
            "size_usdc":     POSITION_SIZE,
            "highest":       price,
            "stop_loss":     price * (1 - STOP_LOSS_PCT    / 100),
            "trailing_stop": price * (1 - TRAILING_STOP_PCT / 100),
            "opened_at":     datetime.utcnow().isoformat(),
        }
        self.capital -= POSITION_SIZE
        self._save_state()
        log.info(f"[PAPER] ACHAT {symbol} @ {price:.4f}")

        sl_price = price * (1 - STOP_LOSS_PCT / 100)
        ts_price = price * (1 - TRAILING_STOP_PCT / 100)

        msg = (
            f"🟢 *Entrée PAPER* `{symbol}`\n"
            f"Prix : `{price:.4f}` | Taille : {POSITION_SIZE} USDC\n"
            f"Stop loss : `{sl_price:.4f}` (-{STOP_LOSS_PCT}%) | TS initial : `{ts_price:.4f}` (-{TRAILING_STOP_PCT}%)\n\n"
            f"📐 *Raison d'entrée :*\n{analysis['details']}"
        )
        return msg

    def _update_trailing_stop(self, pos: dict, current_price: float):
        if current_price > pos["highest"]:
            pos["highest"]       = current_price
            pos["trailing_stop"] = current_price * (1 - TRAILING_STOP_PCT / 100)

    def _close_position(self, symbol: str, price: float, reason: str) -> str:
        pos = self.positions.pop(symbol, None)
        if not pos:
            return ""
        pnl = (price - pos["entry"]) / pos["entry"] * pos["size_usdc"]
        self.pnl     += pnl
        self.capital += pos["size_usdc"] + pnl
        self.total_trades += 1
        if pnl >= 0:
            self.wins  += 1
            emoji = "✅"
        else:
            self.losses += 1
            emoji = "❌"
        self._save_state()
        log.info(f"[PAPER] CLÔTURE {symbol} @ {price:.4f} | PnL : {pnl:+.2f} USDC | {reason}")
        return (
            f"{emoji} *Clôture PAPER* `{symbol}` — {reason}\n"
            f"Prix : `{price:.4f}` | PnL : `{pnl:+.2f}` USDC"
        )

    # ── Drawdown journalier ───────────────────────────────────────────────────
    def _daily_drawdown_reached(self) -> bool:
        dd = (self.daily_start_capital - self.capital) / self.daily_start_capital * 100
        return dd >= MAX_DAILY_DRAWDOWN_PCT

    # ── Analyse matin (premier récap du jour) ─────────────────────────────────
    def morning_analysis(self) -> list[str]:
        """
        Ré-analyse chaque position ouverte sur les fondamentaux.
        Ferme automatiquement les positions dont le signal est invalide.
        Retourne une liste de messages Telegram.
        """
        today = datetime.now(PARIS_TZ).date().isoformat()
        if self._morning_done_date == today:
            return []
        self._morning_done_date = today
        self._save_state()

        if not self.positions:
            return []

        messages = ["☀️ *Analyse matinale des positions ouvertes*\n"]

        for symbol in list(self.positions.keys()):
            pos      = self.positions.get(symbol)
            analysis = self._analyze_signal(symbol)

            try:
                ticker = self.exchange.fetch_ticker(symbol)
                price  = ticker["last"]
            except Exception:
                price = pos["entry"]

            pnl_pct  = (price - pos["entry"]) / pos["entry"] * 100
            secured  = self._secured_gain(pos)
            valid    = analysis["valid"]
            verdict  = "✅ *Garder*" if valid else "❌ *Abandonner*"

            msg = (
                f"─────────────────\n"
                f"📌 `{symbol}` | PnL actuel : `{pnl_pct:+.2f}%`\n"
                f"🔒 Gain sécurisé (si TS) : `{secured['secured_pct']:+.2f}%` (`{secured['secured_pnl']:+.2f}` USDC)\n\n"
                f"*Indicateurs :*\n{analysis['details']}\n\n"
                f"Verdict : {verdict}"
            )
            messages.append(msg)

            # Fermeture automatique si signal invalide
            if not valid:
                close_msg = self._close_position(symbol, price, "Abandon matin — signal invalide")
                if close_msg:
                    messages.append(close_msg)

        return messages

    # ── Scan principal ────────────────────────────────────────────────────────
    def scan(self) -> list[str]:
        alerts = []

        if self._daily_drawdown_reached():
            log.warning("Drawdown journalier max atteint")
            return alerts

        # ── Mise à jour et sortie des positions existantes ────────────────────
        for symbol in list(self.positions.keys()):
            try:
                ticker        = self.exchange.fetch_ticker(symbol)
                current_price = ticker["last"]
                pos           = self.positions[symbol]

                self._update_trailing_stop(pos, current_price)

                # Vérification SL robuste (prix actuel + low bougie 1m)
                sl_hit, sl_price = self._check_sl_triggered(
                    symbol, current_price, pos["stop_loss"]
                )
                if sl_hit:
                    msg = self._close_position(symbol, sl_price, "Stop Loss")
                    if msg:
                        alerts.append(msg)
                    continue

                # Vérification Trailing Stop
                ts_hit, ts_price = self._check_sl_triggered(
                    symbol, current_price, pos["trailing_stop"]
                )
                if ts_hit:
                    msg = self._close_position(symbol, ts_price, "Trailing Stop")
                    if msg:
                        alerts.append(msg)

            except Exception as e:
                log.error(f"Erreur mise à jour {symbol} : {e}")

        # ── Recherche de nouvelles entrées ────────────────────────────────────
        if len(self.positions) < MAX_POSITIONS and not self._daily_drawdown_reached():
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
            "total_trades": self.total_trades,
            "wins":         self.wins,
            "losses":       self.losses,
        }

    def get_positions(self) -> list:
        result = []
        for symbol, pos in self.positions.items():
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                price  = ticker["last"]
                pnl_pct = (price - pos["entry"]) / pos["entry"] * 100
            except Exception:
                pnl_pct = 0.0
                price   = pos["entry"]
            secured = self._secured_gain(pos)
            result.append({
                "symbol":      symbol,
                "entry":       pos["entry"],
                "pnl_pct":     pnl_pct,
                "secured_pct": secured["secured_pct"],
                "secured_pnl": secured["secured_pnl"],
                "ts_price":    secured["ts_price"],
            })
        return result
