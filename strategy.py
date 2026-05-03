import ccxt
import pandas as pd
import json
import os
import logging
from datetime import datetime

log = logging.getLogger(__name__)

# ── Paramètres ────────────────────────────────────────────────────────────────
CAPITAL_TOTAL = 100.0
POSITION_SIZE = 20.0
MAX_POSITIONS = 5
TRAILING_STOP_PCT = 1.5
STOP_LOSS_PCT = 2.0
MAX_DAILY_DRAWDOWN_PCT = 10
TIMEFRAME = "1h"
PERSISTENCE_FILE = "positions.json"


class TradingStrategy:
    def __init__(self, binance_key: str = None, binance_secret: str = None):
        self.exchange = ccxt.binance({
            "apiKey": binance_key or "",
            "secret": binance_secret or "",
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })
        self.positions: dict = {}
        self.capital = CAPITAL_TOTAL
        self.daily_start_capital = CAPITAL_TOTAL
        self.pnl = 0.0
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self._usdc_pairs: list = []
        self._last_pair_refresh = 0

        # Rechargement état persisté au démarrage
        self._load_state()

    # ── Persistance ───────────────────────────────────────────────────────────
    def _save_state(self):
        state = {
            "positions": self.positions,
            "capital": self.capital,
            "daily_start_capital": self.daily_start_capital,
            "pnl": self.pnl,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "saved_at": datetime.utcnow().isoformat(),
        }
        try:
            with open(PERSISTENCE_FILE, "w") as f:
                json.dump(state, f, indent=2)
            log.debug("État sauvegardé")
        except Exception as e:
            log.error(f"Erreur sauvegarde état : {e}")

    def _load_state(self):
        if not os.path.exists(PERSISTENCE_FILE):
            log.info("Aucun état persisté trouvé — démarrage à zéro")
            return
        try:
            with open(PERSISTENCE_FILE, "r") as f:
                state = json.load(f)
            self.positions = state.get("positions", {})
            self.capital = state.get("capital", CAPITAL_TOTAL)
            self.daily_start_capital = state.get("daily_start_capital", CAPITAL_TOTAL)
            self.pnl = state.get("pnl", 0.0)
            self.total_trades = state.get("total_trades", 0)
            self.wins = state.get("wins", 0)
            self.losses = state.get("losses", 0)
            saved_at = state.get("saved_at", "inconnue")
            log.info(f"État rechargé — {len(self.positions)} position(s) | sauvegardé le {saved_at}")
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
                if s.endswith("/USDC")
                and m.get("active")
                and m.get("spot")
            ]
            self._usdc_pairs = pairs
            self._last_pair_refresh = now
            log.info(f"{len(pairs)} paires USDC actives trouvées")
        except Exception as e:
            log.error(f"Erreur chargement marchés : {e}")
        return self._usdc_pairs

    # ── OHLCV + signal momentum ───────────────────────────────────────────────
    def _fetch_ohlcv(self, symbol: str) -> pd.DataFrame | None:
        try:
            data = self.exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=50)
            df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "vol"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms")
            return df
        except Exception:
            return None

    def _momentum_signal(self, df: pd.DataFrame) -> bool:
        """
        Signal long :
        - EMA20 > EMA50
        - Clôture > EMA20
        - Volume dernière bougie > moyenne × 1.2
        """
        if df is None or len(df) < 50:
            return False
        df["ema20"] = df["close"].ewm(span=20).mean()
        df["ema50"] = df["close"].ewm(span=50).mean()
        df["vol_ma"] = df["vol"].rolling(20).mean()
        last = df.iloc[-1]
        return (
            last["ema20"] > last["ema50"]
            and last["close"] > last["ema20"]
            and last["vol"] > last["vol_ma"] * 1.2
        )

    # ── Gestion des positions ─────────────────────────────────────────────────
    def _open_position(self, symbol: str, price: float) -> str:
        if symbol in self.positions:
            return ""
        if len(self.positions) >= MAX_POSITIONS:
            return ""
        if self.capital < POSITION_SIZE:
            return ""

        qty = POSITION_SIZE / price
        self.positions[symbol] = {
            "symbol": symbol,
            "entry": price,
            "qty": qty,
            "size_usdc": POSITION_SIZE,
            "highest": price,
            "stop_loss": price * (1 - STOP_LOSS_PCT / 100),
            "trailing_stop": price * (1 - TRAILING_STOP_PCT / 100),
            "opened_at": datetime.utcnow().isoformat(),
        }
        self.capital -= POSITION_SIZE
        self._save_state()
        log.info(f"[PAPER] ACHAT {symbol} @ {price:.4f} | {POSITION_SIZE} USDC")
        return (
            f"🟢 *Entrée PAPER* `{symbol}`\n"
            f"Prix : `{price:.4f}` | Taille : {POSITION_SIZE} USDC\n"
            f"Stop loss : `{price * (1 - STOP_LOSS_PCT/100):.4f}`"
        )

    def _update_trailing_stop(self, pos: dict, current_price: float):
        if current_price > pos["highest"]:
            pos["highest"] = current_price
            pos["trailing_stop"] = current_price * (1 - TRAILING_STOP_PCT / 100)

    def _close_position(self, symbol: str, price: float, reason: str) -> str:
        pos = self.positions.pop(symbol, None)
        if not pos:
            return ""
        pnl = (price - pos["entry"]) / pos["entry"] * pos["size_usdc"]
        self.pnl += pnl
        self.capital += pos["size_usdc"] + pnl
        self.total_trades += 1
        if pnl >= 0:
            self.wins += 1
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
        drawdown = (self.daily_start_capital - self.capital) / self.daily_start_capital * 100
        return drawdown >= MAX_DAILY_DRAWDOWN_PCT

    # ── Scan principal ────────────────────────────────────────────────────────
    def scan(self) -> list[str]:
        alerts = []

        if self._daily_drawdown_reached():
            log.warning("Drawdown journalier max atteint — pas de nouvelles entrées")
            return alerts

        pairs = self._get_usdc_pairs()

        # Mise à jour positions existantes
        for symbol in list(self.positions.keys()):
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                price = ticker["last"]
                pos = self.positions[symbol]
                self._update_trailing_stop(pos, price)

                if price <= pos["stop_loss"]:
                    msg = self._close_position(symbol, price, "Stop Loss")
                    if msg:
                        alerts.append(msg)
                elif price <= pos["trailing_stop"]:
                    msg = self._close_position(symbol, price, "Trailing Stop")
                    if msg:
                        alerts.append(msg)
            except Exception as e:
                log.error(f"Erreur mise à jour {symbol} : {e}")

        # Recherche de nouvelles entrées
        if len(self.positions) < MAX_POSITIONS:
            for symbol in pairs:
                if symbol in self.positions:
                    continue
                if len(self.positions) >= MAX_POSITIONS:
                    break
                try:
                    df = self._fetch_ohlcv(symbol)
                    if self._momentum_signal(df):
                        ticker = self.exchange.fetch_ticker(symbol)
                        price = ticker["last"]
                        msg = self._open_position(symbol, price)
                        if msg:
                            alerts.append(msg)
                except Exception as e:
                    log.debug(f"Skip {symbol} : {e}")

        return alerts

    # ── Getters pour Telegram ─────────────────────────────────────────────────
    def get_stats(self) -> dict:
        return {
            "capital": self.capital,
            "pnl": self.pnl,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
        }

    def get_positions(self) -> list:
        result = []
        for symbol, pos in self.positions.items():
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                price = ticker["last"]
                pnl_pct = (price - pos["entry"]) / pos["entry"] * 100
            except Exception:
                pnl_pct = 0.0
            result.append({
                "symbol": symbol,
                "entry": pos["entry"],
                "pnl_pct": pnl_pct,
            })
        return result
