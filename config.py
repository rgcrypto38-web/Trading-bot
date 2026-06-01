"""
Configuration centrale. Tous les parametres valides vivent ici.
Modifier une strategie = toucher uniquement son bloc.
"""
import os

# ---------------------------------------------------------------------------
# PERSISTANCE
#   Railway efface le systeme de fichiers a chaque redeploiement (ephemere).
#   Pour conserver les positions et l'historique entre les deploiements,
#   monter un VOLUME Railway et exposer son chemin via RAILWAY_VOLUME_MOUNT_PATH.
#   Sans volume, les donnees survivent a un simple redemarrage mais PAS a un push.
# ---------------------------------------------------------------------------
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ".")
POSITIONS_FILE = os.path.join(DATA_DIR, "positions.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")

# ---------------------------------------------------------------------------
# CAPITAL & RISQUE (couche commune a toutes les strategies)
# ---------------------------------------------------------------------------
CAPITAL_USDC = 100.0
RISK_PER_TRADE = 0.01          # 1 % du capital risque au stop = 1R
MAX_POSITIONS = 3              # plafond de positions simultanees (toutes strategies)
DAILY_CIRCUIT_BREAKER = 0.05   # pause des entrees apres -5 % sur la journee

# ---------------------------------------------------------------------------
# COUTS (a injecter dans tout backtest)  -> ~0,3 % aller-retour
#   Spot Binance standard 0,10 % ; paires USDC cote preneur 0,095 %.
# ---------------------------------------------------------------------------
FEE_RATE = 0.001
SLIPPAGE = 0.0005
ROUND_TRIP_COST = 2 * (FEE_RATE + SLIPPAGE)

# ---------------------------------------------------------------------------
# UNIVERS DYNAMIQUE (par le volume, pas une liste figee)
#   Au demarrage et 1x/jour : on garde les paires QUOTE spot dont le
#   volume 24h (en USDC) depasse le seuil. Une paire avec une position
#   ouverte n'est JAMAIS retiree tant qu'elle n'est pas cloturee.
# ---------------------------------------------------------------------------
QUOTE = "USDC"
MIN_QUOTE_VOLUME_24H = 5_000_000     # 5 M USDC
MIN_CANDLES = 250                    # historique mini (EMA200/Donchian valides)
EXCLUDE_TOKENS = ("UP", "DOWN", "BULL", "BEAR")  # tokens a effet de levier
EXCLUDE_BASES = ("USDT", "FDUSD", "TUSD", "DAI", "EUR", "USDP")  # stable/fiat
UNIVERSE_REFRESH_HOUR = 8            # reevaluation quotidienne

# ---------------------------------------------------------------------------
# MARCHE / UNITES DE TEMPS
# ---------------------------------------------------------------------------
TIMEFRAME = "1h"
HTF = "4h"
CANDLE_LIMIT = 300

# ---------------------------------------------------------------------------
# FILTRE DE REGIME (aiguilleur ADX)
# ---------------------------------------------------------------------------
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 25.0
ADX_RANGE_THRESHOLD = 20.0
BTC_EMA_FILTER = 200          # pas de long breakout si BTC < EMA200 (HTF)

# ---------------------------------------------------------------------------
# MOTEUR BREAKOUT (BRK)
# ---------------------------------------------------------------------------
BRK_DONCHIAN_PERIOD = 20
BRK_VOLUME_MULT = 1.5
BRK_ATR_PERIOD = 14
BRK_STOP_ATR_MULT = 1.5
BRK_LEVEL_BUFFER = 0.001
# Trailing a etages : (pic mini en R, largeur en xATR). Trie par R croissant.
#   pic < 2R -> 3xATR ; pic >= 2R -> 2xATR ; pic >= 5R -> 1.5xATR. Aucun plafond.
BRK_TRAIL_STAGES = [(0.0, 3.0), (2.0, 2.0), (5.0, 1.5)]
BRK_BREAKEVEN_AT_R = 2.0      # des +2R, plancher au seuil d'entree (plus jamais perdant)

# ---------------------------------------------------------------------------
# MOTEUR MEAN REVERSION (MR)
# ---------------------------------------------------------------------------
MR_BB_PERIOD = 20
MR_BB_MULT = 2.0
MR_RSI_PERIOD = 14
MR_RSI_OVERSOLD = 30.0
MR_ATR_PERIOD = 14
MR_STOP_ATR_MULT = 1.5

# ---------------------------------------------------------------------------
# REGISTRE DES STRATEGIES
#   Ajouter une strategie = creer strategy_xxx.py (contrat BaseStrategy)
#   + une ligne ici. main.py et alerts.py ne bougent pas.
# ---------------------------------------------------------------------------
STRATEGIES = [
    {"module": "strategy_breakout",       "class": "BreakoutStrategy",      "enabled": True},
    {"module": "strategy_mean_reversion", "class": "MeanReversionStrategy", "enabled": True},
]

# ---------------------------------------------------------------------------
# TELEGRAM (sans token -> tout va dans la console)
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# CADENCE / HORAIRES
# ---------------------------------------------------------------------------
SCAN_INTERVAL_SEC = 60
RECAP_HOURS = [8, 20]         # recaps consolides quotidiens (heure locale)
TZ_OFFSET_HOURS = 2           # decalage horaire local (Europe/Paris ete = UTC+2)
