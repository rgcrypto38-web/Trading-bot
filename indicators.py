"""
Indicateurs techniques partages. Fonctions pures sur DataFrame OHLCV.
Colonnes attendues : ['open','high','low','close','volume'], index temporel croissant.
"""
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()   # Wilder
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, 1e-12)
    return 100.0 - (100.0 / (1.0 + rs))


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False).mean()  # Wilder


def bollinger(close: pd.Series, period: int = 20, mult: float = 2.0):
    mid = sma(close, period)
    std = close.rolling(period).std(ddof=0)
    return mid - mult * std, mid, mid + mult * std   # lower, mid, upper


def donchian(df: pd.DataFrame, period: int = 20):
    """Plus-haut / plus-bas sur les `period` bougies PRECEDENTES (decale de 1
    pour eviter le look-ahead)."""
    upper = df["high"].rolling(period).max().shift(1)
    lower = df["low"].rolling(period).min().shift(1)
    return lower, upper


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ADX de Wilder : FORCE de la tendance (pas la direction)."""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move
    tr = true_range(df)
    atr_w = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_w.replace(0.0, 1e-12)
    minus_di = 100.0 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_w.replace(0.0, 1e-12)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, 1e-12)
    return dx.ewm(alpha=1 / period, adjust=False).mean()
