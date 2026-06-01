"""
L'AIGUILLEUR. Decide quel moteur a le droit d'agir sur un symbole, ou aucun.
Regimes mutuellement exclusifs -> les deux moteurs ne se neutralisent jamais.

  ADX > 25         -> "TREND" -> breakout autorise (si BTC haussier)
  ADX < 20         -> "RANGE" -> mean reversion autorise
  20 <= ADX <= 25  -> "NONE"  -> zone morte (transitions : on s'abstient)

Filtre macro : pas de long breakout si BTC < EMA200 (HTF).

NB honnete : la detection de regime est le maillon faible (en retard d'une phase).
La zone morte est volontaire pour eviter le whipsaw. A scruter en priorite au backtest.
"""
import config as C
import indicators as ind


def detect_regime(df) -> str:
    if df is None or len(df) < C.ADX_PERIOD * 3:
        return "NONE"
    val = ind.adx(df, C.ADX_PERIOD).iloc[-1]
    if val != val:                      # NaN
        return "NONE"
    if val > C.ADX_TREND_THRESHOLD:
        return "TREND"
    if val < C.ADX_RANGE_THRESHOLD:
        return "RANGE"
    return "NONE"


def btc_bullish(btc_htf_df) -> bool:
    """True si BTC > EMA200 (HTF). Gate des longs breakout."""
    if btc_htf_df is None or len(btc_htf_df) < C.BTC_EMA_FILTER + 1:
        return False
    ema200 = ind.ema(btc_htf_df["close"], C.BTC_EMA_FILTER).iloc[-1]
    return bool(btc_htf_df["close"].iloc[-1] > ema200)


def allowed_tag(regime: str, btc_ok: bool):
    """Mappe regime -> tag de strategie autorise (ou None)."""
    if regime == "TREND" and btc_ok:
        return "BRK"
    if regime == "RANGE":
        return "MR"
    return None
