"""
Moteur 2 - MEAN REVERSION (regime de range).

Entree : prix <= Bollinger basse(20,2) OU RSI(14) < 30.
Cible  : retour a la moyenne = bande mediane (SMA20). R:R modere (~1:1 a 1.5:1).
Stop DUR (non negociable) : entry - 1.5*ATR. Risque catastrophique = le range
         qui casse en tendance baissiere -> jamais sans stop.
Long-only (spot). Profil attendu : winrate ~55-65 %.
"""
from typing import List, Dict
import config as C
import indicators as ind
from base_strategy import BaseStrategy, Signal, SignalType


class MeanReversionStrategy(BaseStrategy):
    tag = "MR"
    label = "Mean Reversion"

    def scan(self, market: Dict[str, "pd.DataFrame"]) -> List[Signal]:  # noqa: F821
        out: List[Signal] = []
        need = C.MR_BB_PERIOD + C.MR_ATR_PERIOD + 2
        for symbol, df in market.items():
            if df is None or len(df) < need:
                continue
            lower_bb, mid_bb, _ = ind.bollinger(df["close"], C.MR_BB_PERIOD, C.MR_BB_MULT)
            rsi = ind.rsi(df["close"], C.MR_RSI_PERIOD)
            atr = ind.atr(df, C.MR_ATR_PERIOD)

            last = df.iloc[-1]
            lb, mid, r, a = lower_bb.iloc[-1], mid_bb.iloc[-1], rsi.iloc[-1], atr.iloc[-1]
            if any(x != x for x in (lb, mid, r, a)):       # NaN guard
                continue

            close = last["close"]
            if (close <= lb or r < C.MR_RSI_OVERSOLD) and mid > close:
                stop = close - C.MR_STOP_ATR_MULT * a
                size = self.position_size(C.CAPITAL_USDC, C.RISK_PER_TRADE, close, stop)
                if size <= 0:
                    continue
                out.append(Signal(
                    type=SignalType.ENTRY, tag=self.tag, symbol=symbol, price=close,
                    stop=stop, size=size, target=mid,
                    reason=f"survente (BB-bas / RSI {r:.0f}) -> cible SMA{C.MR_BB_PERIOD} {mid:.4f}",
                ))
        return out

    def check_exits(self, positions: List[dict], market: Dict[str, "pd.DataFrame"]) -> List[Signal]:  # noqa: F821
        out: List[Signal] = []
        for pos in positions:
            if pos.get("tag") != self.tag:
                continue
            df = market.get(pos["symbol"])
            if df is None or len(df) < 2:
                continue
            last = df.iloc[-1]
            target = pos.get("target")
            if last["low"] <= pos["stop"]:                 # stop dur d'abord
                out.append(self.build_exit(pos, pos["stop"], "stop dur touche"))
            elif target is not None and last["high"] >= target:
                out.append(self.build_exit(pos, target, f"cible SMA{C.MR_BB_PERIOD} atteinte"))
        return out
