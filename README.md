# Trading Bot — Railway + Telegram

Bot de trading automatisé en mode **PAPER** (simulation, aucun fonds réel engagé).

## Stack
- Python 3.11+
- `ccxt` — connexion Binance
- `pandas` — calcul indicateurs
- `requests` — Telegram Bot API (long polling)

## Stratégie
| Paramètre | Valeur |
|---|---|
| Mode | PAPER (simulation) |
| Capital | 100 USDC |
| Positions | 5 × 20 USDC |
| Trailing stop | 1.5% |
| Stop loss | 2.0% |
| Drawdown max/jour | 10% |
| Timeframe | 1h |
| Signal | EMA20 > EMA50 + volume |
| Univers | Toutes paires USDC actives Binance |

## Variables d'environnement (Railway)

À configurer dans Railway → Service → Variables :

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | Token du bot Telegram (via @BotFather) |
| `TELEGRAM_CHAT_ID` | Ton Chat ID Telegram |
| `BINANCE_KEY` | Clé API Binance |
| `BINANCE_SECRET` | Secret API Binance |

## Commandes Telegram

| Commande | Action |
|---|---|
| `/start` | Démarrer le bot |
| `/stop` | Arrêter le bot |
| `/status` | PnL et capital courant |
| `/positions` | Positions ouvertes |
| `/help` | Liste des commandes |

## Déploiement Railway

1. Push ce dépôt sur GitHub
2. Créer un nouveau projet Railway → "Deploy from GitHub repo"
3. Ajouter les 4 variables d'environnement
4. Railway détecte `railway.json` automatiquement
5. Le bot envoie un message Telegram au démarrage

## Structure des fichiers

```
trading-bot/
├── main.py          # Boucle Telegram + orchestration
├── strategy.py      # Logique trading PAPER
├── requirements.txt # Dépendances Python
├── railway.json     # Config Railway
└── README.md
```
