"""
Orchestrateur. Boucle unique (pas de threading concurrent -> pas de race condition) :
  1) commandes Telegram (poll court)
  2) univers (recalcule 1x/jour) ; donnees ; regime par paire
  3) le regime aiguille vers LE moteur autorise -> entrees (sous garde-fous)
  4) sorties de toutes les positions via leur moteur
  5) persistance (positions + historique) ; recaps programmes ; evenements de regime

Mode PAPER : execution simulee au prix du signal, cout aller-retour a la cloture.
Persistance sur DATA_DIR (monter un volume Railway pour survivre aux redeploiements).
"""
import json
import time
import importlib
import datetime as dt
from typing import Dict, List

import ccxt
import pandas as pd

import config as C
import regime as R
import indicators as ind
from base_strategy import BaseStrategy, Signal, SignalType
from alerts import AlertManager, BUTTON_MAP


# ---------------------------------------------------------------------------
# Fonctions pures (testables hors-ligne)
# ---------------------------------------------------------------------------
def filter_universe(markets: dict, tickers: dict, open_symbols: set) -> List[str]:
    """Paires QUOTE spot actives, volume 24h >= seuil, hors tokens a levier/stables.
    Une paire avec position ouverte est conservee meme si elle sort du filtre."""
    out = []
    for sym, m in markets.items():
        if not (m.get("spot") and m.get("active") and m.get("quote") == C.QUOTE):
            continue
        base = m.get("base", "")
        if base in C.EXCLUDE_BASES or any(base.endswith(x) for x in C.EXCLUDE_TOKENS):
            continue
        qv = (tickers.get(sym) or {}).get("quoteVolume")
        if qv is not None and qv >= C.MIN_QUOTE_VOLUME_24H:
            out.append(sym)
    return sorted(set(out) | set(open_symbols))


def compute_metrics(trades: List[dict], strategies) -> List[dict]:
    """Metriques par strategie a partir de l'historique des trades clotures."""
    res = []
    for tag, strat in strategies.items():
        t = [x for x in trades if x["tag"] == tag]
        if not t:
            res.append({"tag": tag, "label": strat.label, "trades": 0})
            continue
        pnls = [x["pnl_usdc"] for x in t]
        rs = [x["r_multiple"] for x in t]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_rs = [r for r in rs if r > 0]
        loss_rs = [r for r in rs if r <= 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        # max drawdown sur la courbe de capital cumulee
        cum, peak, dd = 0.0, 0.0, 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            dd = min(dd, cum - peak)
        avg_win_r = sum(win_rs) / len(win_rs) if win_rs else 0.0
        avg_loss_r = sum(loss_rs) / len(loss_rs) if loss_rs else 0.0
        res.append({
            "tag": tag, "label": strat.label, "trades": len(t),
            "winrate": 100 * len(wins) / len(t),
            "expectancy_r": sum(rs) / len(rs),
            "avg_win_r": avg_win_r, "avg_loss_r": avg_loss_r,
            "payoff": (avg_win_r / abs(avg_loss_r)) if avg_loss_r else float("inf"),
            "pf": (gross_win / gross_loss) if gross_loss else float("inf"),
            "max_dd": dd,
        })
    return res


def local_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=C.TZ_OFFSET_HOURS)


# ---------------------------------------------------------------------------
# Donnees
# ---------------------------------------------------------------------------
def fetch_df(exchange, symbol, timeframe, limit):
    try:
        raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not raw:
            return None
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df.set_index("ts")
    except Exception as e:
        print(f"[data] {symbol} {timeframe}: {e}")
        return None


# ---------------------------------------------------------------------------
# Persistance
# ---------------------------------------------------------------------------
def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    try:
        with open(path, "w") as f:
            json.dump(obj, f, indent=2)
    except Exception as e:
        print(f"[persist] ecriture {path}: {e}")


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class Bot:
    def __init__(self):
        self.exchange = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})
        self.alerts = AlertManager()
        self.strategies = self._load_strategies()
        self.positions = _load(C.POSITIONS_FILE, [])      # rechargees -> pas re-alertees
        self.trades = _load(C.TRADES_FILE, [])
        self.universe: List[str] = []
        self.market: Dict[str, pd.DataFrame] = {}
        self.btc_ok = False
        self.paused = False
        self._tg_offset = None
        self._fired = set()            # (date, hour) recaps deja envoyes
        self._uni_day = None
        self._btc_state = None         # pour detecter le flip macro

    def _load_strategies(self):
        loaded = {}
        for e in C.STRATEGIES:
            if not e.get("enabled"):
                continue
            cls = getattr(importlib.import_module(e["module"]), e["class"])
            inst = cls()
            loaded[inst.tag] = inst
        return loaded

    # --- univers ----------------------------------------------------------
    def refresh_universe(self):
        try:
            markets = self.exchange.load_markets()
            cands = [s for s, m in markets.items()
                     if m.get("spot") and m.get("active") and m.get("quote") == C.QUOTE]
            tickers = self.exchange.fetch_tickers(cands)
            self.universe = filter_universe(markets, tickers, {p["symbol"] for p in self.positions})
            self._uni_day = local_now().date()
            print(f"[univers] {len(self.universe)} paires >= {C.MIN_QUOTE_VOLUME_24H/1e6:g}M USDC")
        except Exception as e:
            self.alerts.error(f"Univers : {e}")
            if not self.universe:
                self.universe = ["BTC/USDC", "ETH/USDC"]   # repli minimal

    # --- execution (paper) ------------------------------------------------
    def open_position(self, sig):
        self.positions.append({
            "symbol": sig.symbol, "tag": sig.tag, "entry_price": sig.price,
            "size": sig.size, "init_stop": sig.stop, "stop": sig.stop,
            "target": sig.target, "highest": sig.price, "stage": "3xATR",
            "opened_at": dt.datetime.utcnow().isoformat(),
        })
        self.alerts.emit(sig)

    def close_position(self, pos, sig):
        cost = pos["entry_price"] * pos["size"] * C.ROUND_TRIP_COST
        sig.pnl_usdc -= cost
        self.trades.append({"tag": pos["tag"], "r_multiple": sig.r_multiple,
                            "pnl_usdc": sig.pnl_usdc, "closed_at": dt.date.today().isoformat()})
        self.positions.remove(pos)
        self.alerts.emit(sig)

    def force_buy(self, raw_symbol: str):
        """Achat forcé manuel. Géré comme un breakout (trailing 3 étages).
        Outrepasse le régime, le scan et la pause ; respecte le plafond de
        positions, 1 position/paire et le risque 1 %."""
        symbol = raw_symbol.upper()
        if "/" not in symbol:
            symbol += "/" + C.QUOTE
        if any(p["symbol"] == symbol for p in self.positions):
            self.alerts.info(f"Position déjà ouverte sur {symbol}.")
            return
        if len(self.positions) >= C.MAX_POSITIONS:
            self.alerts.info(f"Plafond de {C.MAX_POSITIONS} positions atteint — achat refusé.")
            return
        df = fetch_df(self.exchange, symbol, C.TIMEFRAME, C.CANDLE_LIMIT)
        if df is None or len(df) < C.BRK_ATR_PERIOD + 2:
            self.alerts.error(f"Achat forcé {symbol} : paire ou données indisponibles.")
            return
        a = ind.atr(df, C.BRK_ATR_PERIOD).iloc[-1]
        price = float(df["close"].iloc[-1])
        stop = price - C.BRK_STOP_ATR_MULT * a
        size = BaseStrategy.position_size(C.CAPITAL_USDC, C.RISK_PER_TRADE, price, stop)
        if size <= 0 or a != a:
            self.alerts.error(f"Achat forcé {symbol} : dimensionnement impossible.")
            return
        if symbol not in self.universe:      # pour que les cycles suivants le gèrent
            self.universe.append(symbol)
        self.open_position(Signal(
            type=SignalType.ENTRY, tag="BRK", symbol=symbol, price=price,
            stop=stop, size=size, target=None,
            reason="achat forcé manuel (géré comme breakout : trailing 3 étages)"))

    def daily_pnl(self, tag=None):
        today = dt.date.today().isoformat()
        return sum(t["pnl_usdc"] for t in self.trades
                   if t["closed_at"] == today and (tag is None or t["tag"] == tag))

    def total_pnl(self, tag=None):
        return sum(t["pnl_usdc"] for t in self.trades if tag is None or t["tag"] == tag)

    # --- cycle ------------------------------------------------------------
    def cycle(self):
        now = local_now()
        if self._uni_day != now.date() and now.hour >= C.UNIVERSE_REFRESH_HOUR:
            self.refresh_universe()

        btc_htf = fetch_df(self.exchange, "BTC/USDC", C.HTF, C.CANDLE_LIMIT)
        self.btc_ok = R.btc_bullish(btc_htf)
        self._detect_btc_flip()

        self.market = {s: fetch_df(self.exchange, s, C.TIMEFRAME, C.CANDLE_LIMIT)
                       for s in self.universe}

        # 1) SORTIES d'abord (libere des places)
        for strat in self.strategies.values():
            for sig in strat.check_exits(list(self.positions), self.market):
                pos = next((p for p in self.positions
                            if p["symbol"] == sig.symbol and p["tag"] == sig.tag), None)
                if pos:
                    self.close_position(pos, sig)

        # 2) ENTREES (garde-fous : pause, coupe-circuit, plafond, 1 pos/paire)
        if not self.paused and self.daily_pnl() > -C.DAILY_CIRCUIT_BREAKER * C.CAPITAL_USDC:
            held = {p["symbol"] for p in self.positions}
            for sym in self.universe:
                if sym in held or len(self.positions) >= C.MAX_POSITIONS:
                    continue
                tag = R.allowed_tag(R.detect_regime(self.market.get(sym)), self.btc_ok)
                strat = self.strategies.get(tag)
                if not strat:
                    continue
                for sig in strat.scan({sym: self.market.get(sym)}):
                    if len(self.positions) >= C.MAX_POSITIONS:
                        break
                    if sig.symbol not in {p["symbol"] for p in self.positions}:
                        self.open_position(sig)
                strat.force_scan = False

        _save(C.POSITIONS_FILE, self.positions)
        _save(C.TRADES_FILE, self.trades)
        self._scheduled_recap(now)

    def _detect_btc_flip(self):
        if self._btc_state is None:
            self._btc_state = self.btc_ok
            return
        if self.btc_ok != self._btc_state:
            self._btc_state = self.btc_ok
            if self.btc_ok:
                self.alerts.regime_change("BTC repasse au-dessus EMA200 (4h) · breakout réactivé")
            else:
                self.alerts.regime_change("BTC repasse sous EMA200 (4h) · breakout suspendu, seul MR actif")

    # --- vues -------------------------------------------------------------
    def build_snapshot(self):
        strat_rows = []
        for tag, s in self.strategies.items():
            strat_rows.append({"tag": tag, "label": s.label,
                               "capital": C.CAPITAL_USDC + self.total_pnl(tag),
                               "pnl_today": self.daily_pnl(tag), "pnl_total": self.total_pnl(tag)})
        positions = []
        for p in self.positions:
            cur = self._current_price(p["symbol"], p["entry_price"])
            pnl_usdc = (cur - p["entry_price"]) * p["size"]
            r_unit = p["entry_price"] - p["init_stop"]
            positions.append({
                "tag": p["tag"], "symbol": p["symbol"],
                "pnl_usdc": pnl_usdc, "pnl_pct": (cur / p["entry_price"] - 1) * 100,
                "entry": p["entry_price"], "current": cur, "stop": p["stop"],
                "target": p.get("target"), "stage": p.get("stage"),
                "r_now": (cur - p["entry_price"]) / r_unit if r_unit > 0 else 0.0,
            })
        scan = []
        for sym in self.universe[:8]:
            reg = R.detect_regime(self.market.get(sym))
            note = {"TREND": "surveillé (breakout)", "RANGE": "surveillé (mean rev.)",
                    "NONE": "zone morte"}[reg]
            scan.append({"symbol": sym, "regime": reg, "note": note})
        return {
            "time": local_now().strftime("%d/%m %H:%M"),
            "strategies": strat_rows,
            "total_capital": C.CAPITAL_USDC * len(strat_rows) + self.total_pnl(),
            "total_pnl_today": self.daily_pnl(),
            "positions": positions, "max_positions": C.MAX_POSITIONS,
            "btc_bullish": self.btc_ok, "scan": scan,
        }

    def _current_price(self, symbol, fallback):
        df = self.market.get(symbol)
        if df is not None and len(df):
            return float(df["close"].iloc[-1])
        return fallback

    def _scheduled_recap(self, now):
        key = (now.date(), now.hour)
        if now.hour in C.RECAP_HOURS and key not in self._fired:
            self._fired.add(key)
            self.alerts.recap(self.build_snapshot(), on_demand=False)

    # --- commandes Telegram -----------------------------------------------
    def poll_commands(self):
        if not self.alerts.enabled:
            return
        import urllib.request
        try:
            url = f"https://api.telegram.org/bot{self.alerts.token}/getUpdates?timeout=0"
            if self._tg_offset is not None:
                url += f"&offset={self._tg_offset}"
            with urllib.request.urlopen(url, timeout=10) as r:
                for upd in json.loads(r.read()).get("result", []):
                    self._tg_offset = upd["update_id"] + 1
                    text = (upd.get("message", {}) or {}).get("text", "").strip().lower()
                    if text.startswith("/buy"):
                        parts = text.split()
                        if len(parts) >= 2:
                            self.force_buy(parts[1])
                        else:
                            self.alerts.info("Usage : /buy <paire>  (ex. /buy sol)")
                        continue
                    self.handle(BUTTON_MAP.get(text))
        except Exception as e:
            print(f"[tg] poll: {e}")

    def handle(self, action):
        if action == "status":
            self.alerts.recap(self.build_snapshot(), on_demand=True)
        elif action == "positions":
            snap = self.build_snapshot()
            self.alerts.positions_detail(snap["positions"])
        elif action == "perf":
            self.alerts.perf(compute_metrics(self.trades, self.strategies))
        elif action == "regime":
            self.alerts.recap(self.build_snapshot(), on_demand=True)
        elif action == "boost":
            for s in self.strategies.values():
                s.force_scan = True
            self.alerts.info("Boost : scan forcé au prochain cycle.")
        elif action == "pause":
            self.paused = True
            self.alerts.info("⏸ En pause — plus d'ouverture. Les positions restent gérées.")
        elif action == "start":
            self.paused = False
            self.alerts.info("▶️ Actif.")
        elif action == "closeall":
            n = len(self.positions)
            for p in list(self.positions):
                cur = self._current_price(p["symbol"], p["entry_price"])
                sig = self.strategies[p["tag"]].build_exit(p, cur, "fermeture manuelle")
                self.close_position(p, sig)
            self.alerts.info(f"💣 {n} position(s) fermée(s).")
        elif action == "help":
            self.alerts.info("Statut/Positions/Perf = vues. Boost = force le scan. "
                             "Pause = stoppe les ouvertures (positions toujours gérées). "
                             "Tout fermer = clôture tout. /buy <paire> = achat forcé manuel.")

    # --- boucle -----------------------------------------------------------
    def run(self):
        self.refresh_universe()
        self.alerts.startup([s.label for s in self.strategies.values()], len(self.universe))
        last = 0.0
        while True:
            self.poll_commands()
            if time.time() - last >= C.SCAN_INTERVAL_SEC:
                try:
                    self.cycle()
                except Exception as e:
                    self.alerts.error(f"Cycle : {e}")
                last = time.time()
            time.sleep(1)


if __name__ == "__main__":
    Bot().run()
