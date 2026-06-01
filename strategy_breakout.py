"""
Moteur 1 - BREAKOUT (regime directionnel).

Entree : cloture 1h > plus-haut Donchian20 + volume > 1.5x moyenne20.
Stop initial (1R) : max(entry - 1.5*ATR, niveau casse) -> le plus proche (meilleur R:R).
Sortie : AUCUN TP. Trailing a 3 ETAGES, pilote par le profit en R (largeur en ATR) :
    pic  < 2R  -> 3.0xATR
    pic >= 2R  -> 2.0xATR  + PLANCHER au seuil d'entree (le trade ne peut plus etre perdant)
    pic >= 5R  -> 1.5xATR
  AUCUN plafond : un gagnant peut courir librement.
Profil attendu : winrate ~35-45 %, porte par les rares gros gagnants.
"""
from typing import List, Dict
import config as C
import indicators as ind
from base_strategy import BaseStrategy, Signal, SignalType


class BreakoutStrategy(BaseStrategy):
    tag = "BRK"
    label = "Breakout"

    def scan(self, market: Dict[str, "pd.DataFrame"]) -> List[Signal]:  # noqa: F821
        out: List[Signal] = []
        need = C.BRK_DONCHIAN_PERIOD + C.BRK_ATR_PERIOD + 2
        for symbol, df in market.items():
            if df is None or len(df) < need:
                continue
            _, upper_dc = ind.donchian(df, C.BRK_DONCHIAN_PERIOD)
            atr = ind.atr(df, C.BRK_ATR_PERIOD)
            vol_ma = df["volume"].rolling(C.BRK_DONCHIAN_PERIOD).mean()

            last = df.iloc[-1]
            level, a, vma = upper_dc.iloc[-1], atr.iloc[-1], vol_ma.iloc[-1]
            if any(x != x for x in (level, a, vma)):       # NaN guard
                continue

            close = last["close"]
            if close > level and last["volume"] > C.BRK_VOLUME_MULT * vma:
                stop = max(close - C.BRK_STOP_ATR_MULT * a, level * (1 - C.BRK_LEVEL_BUFFER))
                size = self.position_size(C.CAPITAL_USDC, C.RISK_PER_TRADE, close, stop)
                if size <= 0:
                    continue
                out.append(Signal(
                    type=SignalType.ENTRY, tag=self.tag, symbol=symbol, price=close,
                    stop=stop, size=size, target=None,
                    reason=f"cassure Donchian{C.BRK_DONCHIAN_PERIOD} + volume "
                           f"x{last['volume']/vma:.1f} (1R={close - stop:.4f})",
                ))
        return out

    def check_exits(self, positions: List[dict], market: Dict[str, "pd.DataFrame"]) -> List[Signal]:  # noqa: F821
        out: List[Signal] = []
        for pos in positions:
            if pos.get("tag") != self.tag:
                continue
            df = market.get(pos["symbol"])
            if df is None or len(df) < C.BRK_ATR_PERIOD + 2:
                continue
            a = ind.atr(df, C.BRK_ATR_PERIOD).iloc[-1]
            last = df.iloc[-1]
            entry = pos["entry_price"]
            r_unit = entry - pos["init_stop"]

            # plus-haut atteint depuis l'entree -> pic exprime en R
            pos["highest"] = max(pos.get("highest", entry), last["high"])
            peak_r = (pos["highest"] - entry) / r_unit if r_unit > 0 else 0.0

            # etage du trailing selon le pic en R
            mult = C.BRK_TRAIL_STAGES[0][1]
            for min_r, m in C.BRK_TRAIL_STAGES:
                if peak_r >= min_r:
                    mult = m
            chandelier = pos["highest"] - mult * a

            # plancher au seuil d'entree des +2R
            floor = entry if peak_r >= C.BRK_BREAKEVEN_AT_R else -float("inf")

            # le stop ne descend jamais
            pos["stop"] = max(pos["stop"], chandelier, floor)
            pos["stage"] = f"{mult:g}xATR" + (" +BE" if peak_r >= C.BRK_BREAKEVEN_AT_R else "")

            # declenchement sur le BAS de la derniere bougie (capte les baisses lentes)
            if last["low"] <= pos["stop"]:
                out.append(self.build_exit(pos, pos["stop"], f"trailing {pos['stage']} (pic {peak_r:.1f}R)"))
        return out
