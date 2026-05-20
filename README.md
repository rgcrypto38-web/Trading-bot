# 🤖 Trading Bot Crypto — Stratégie de Trend-Following ATR

**Status:** V16 (Stable – Mode PAPER sur Binance)  
**Capital:** 100 USDC (simulé)  
**Plateforme:** Railway + Telegram + Binance (CCXT)

---

## 📋 Table des matières
1. [Architecture générale](#architecture)
2. [Changements V15](#changements-v15--bear-mode--ts-adaptatif--cooldowns)
3. [Corrections V16](#corrections-v16--race-conditions)
4. [Stratégie A — Trend-following (actuellement en production)](#stratégie-a)
5. [Stratégie B — Momentum (roadmap)](#stratégie-b--roadmap)
6. [Commandes Telegram](#commandes-telegram)
7. [Fichiers et structure](#fichiers)

---

## Architecture

```
main.py             → Telegram polling + gestion des threads + logging
strategy.py         → Logique de trading (scan, open, close, indicateurs)
positions.json      → État persistant (positions, cooldowns, skip list)
requirements.txt    → dépendances (ccxt, flask, pandas, requests)
railway.json        → Config Railway
```

**Flux:**
1. Bot démarre → lance `trading_loop()` en thread détaché
2. Chaque 60s : vérifie positions ouvertes, stops, TP, scanne nouvelles entrées
3. À 7h : analyse matinale (ordre de consolidation des meilleures paires du jour)
4. À 8h/12h/18h : diagnostic marché (Top 10 paires près du signal)
5. 7h–22h : récaps horaires quand positions ouvertes
6. Telegram : commandes pour `/buy`, `/skip`, `/stats`, `/boost`, etc.

---

## Changements V15 — Bear Mode + TS Adaptatif + Cooldowns

### 1. **Bear Mode Intelligent** (au lieu de blocage total)
**Ancien (V14):** Si BTC < EMA200 → bloc total, zéro entrée  
**Nouveau (V15):** Si BTC < EMA200 → **filtres resserrés**

| Filtre | Normal | Bear Mode |
|--------|--------|-----------|
| RSI min | 45 | 55 (+10) |
| Volume | ×2 | ×3 (+50%) |
| EMA, spread, liquidité | Inchangés | Inchangés |

→ Permet des trades en baissier, mais **plus sélectifs**. Pas d'arrêt complet.

### 2. **Trailing Stop Adaptatif** (au lieu de fixe ×3.0)
Multiplicateur ATR ajusté selon la **volatilité relative** de la paire :

```python
volatilité = (ATR / prix) * 100

Si < 1%       → TS = ×3.5 ATR  (paire calme : BTC, ETH)
Si 1–3%       → TS = ×3.0 ATR  (normal)
Si 3–5%       → TS = ×2.5 ATR  (volatilité haute)
Si > 5%       → TS = ×2.0 ATR  (très volatile : small caps)
```

Bénéfice : **sécurise rapidement** sur les paires instables, laisse respirer les stables.

### 3. **Re-entry Cooldowns** (anti-revenge trading)

Après fermeture d'une position, un cooldown = délai avant de re-trader la paire :

| Raison fermeture | Cooldown |
|-----------------|----------|
| Stop Loss direct | **48h** (punition) |
| Abandon matin (>5j sans TP1) | **24h** |
| Trailing Stop APRÈS TP1 | **12h** (sortie propre) |
| Trailing Stop SANS TP1 | **48h** (sorti trop tôt) |

Persisté dans `positions.json`. Vérification automatique au scan.

### 4. **Skip List Manuelle** (`/skip SYMBOL`)
Blacklist temporaire d'une paire (24h par défaut, ajustable).  
Utile si une paire fait un dump ou si un signal est faux.

### 5. **Commandes Telegram Enrichies**

| Commande | Effet |
|----------|-------|
| `/buy SYMBOL` | Force entrée manuelle (ignore tous les filtres) |
| `/skip SYMBOL` | Blacklist 24h |
| `/stats` | Winrate, drawdown, Sharpe, cooldowns actifs |
| `/boost` | Scan immédiat (au lieu d'attendre 60s) |
| `📈 Stats` | Bouton pour `/stats` |
| `⚡ Boost` | Bouton pour `/boost` |

---

## Corrections V16 — Race Conditions

### **Bug #1 : Multiples threads `trading_loop`**
**Cause :** Aucune protection → clics rapides "Arrêter → Démarrer" créaient 2+ threads en parallèle  
**Effet :** Messages en double (morning, diagnostics, récaps)  
**Fix :**
- Variable globale `_trading_thread` pour tracker le thread
- Au démarrage : vérifier que le thread précédent est bien terminé (`.is_alive()`)
- À l'arrêt : `.join(timeout=10)` pour attendre proprement

### **Bug #2 : Morning Analysis lancée 60 fois**
**Cause :** Boucle 60s itère pendant toute l'heure 7h → appel 60 fois à `morning_analysis()`  
**Fix :** Variable `last_morning_hour` qui track la dernière heure déjà traitée

### **Bug #3 : Message démarrage incohérent**
**Cause :** Message affichait `TS ×3.0` alors que code utilisait TS adaptatif  
**Fix :** Message mis à jour avec `TS adaptatif ×2.0–×3.5 (selon volatilité)`

---

## Stratégie A

### Signaux d'entrée (tous doivent être VRAIS)
- **EMA 1h :** EMA20 > EMA50
- **EMA 4h :** EMA20 > EMA50
- **Pente EMA20 1h :** croissante
- **Volume 1h :** volume > moyenne 24h × 2 (×3 en bear mode)
- **RSI 1h :** entre 45–85 (55–85 en bear mode)
- **Spread :** < 0.15%
- **Liquidité :** > 10M USDC 24h
- **BTC :** Prix > EMA200 4h OU Bear Mode actif

### Gestion de position
| Élément | Valeur |
|---------|--------|
| Taille position | Variable (capital / 5) |
| Stop Loss | Entry − 2.5×ATR |
| Trailing Stop | Entry − ×ATR adaptatif (voir V15) |
| TP1 | +3×ATR → Vend 25% |
| TP2 | +5×ATR → Vend 25% |
| Reste | 50% en TS |

### Filtres supplémentaires
- **Capital initial :** 100 USDC PAPER
- **Positions max :** 5 simultanées
- **Abandon matin :** Position > 5j sans TP1 → fermeture automatique 7h
- **Drawdown journalier :** Si −25% du capital → block entrées (stops/TP restent actifs)

---

## Stratégie B — Roadmap

**Status:** ⏳ **À développer après stabilisation de la Stratégie A**

### Concept
Complémentaire à la Stratégie A. Capitale **200 USDC total** (100 USDC + 100 USDC stratégie B).

| Aspect | Strat A | Strat B |
|--------|---------|---------|
| **Signaux** | EMA trend | Volume + momentum breakout |
| **Capital alloué** | 100 USDC | 100 USDC |
| **Filtres** | Classiques | **Resserrés** |
| **Liquidité min** | 10M USDC | **1M USDC** (small caps) |
| **RSI** | 45–85 | **60–75** (momentum sans extrême) |
| **Volume trigger** | ×2 | **×3 sur 1h** vs moyenne 24h |
| **Entrée** | +3%–5% | **+5% sur 1h** (mouvement visible) |
| **Taille position** | Max 5 positions | **Max 2 positions** |
| **SL** | ×2.5 ATR | **×1.5 ATR** (serré) |
| **TP rapide** | +3/+5 ATR | **+5% / +10%** |
| **TS** | Adaptatif | **Agressif (×1.5 ATR après TP1)** |

### Timeline
1. **Phase 1 (maintenant)** : Stabiliser Stratégie A en V15/V16
2. **Phase 2 (semaine X)** : Dupliquer strategy.py en strategy_b.py (architecture parallèle)
3. **Phase 3** : Tests PAPER 2 semaines
4. **Phase 4** : Monitoring équité (100 + 100 = 200 USDC)

---

## Commandes Telegram

### Contrôle Bot
| Bouton | Commande | Action |
|--------|----------|--------|
| ▶️ Démarrer | `/start` | Lance bot + boucle de trading |
| ⏸ Pause | `/pause` | Pause trading, stops/TP restent actifs |
| ⏹ Arrêter | `/stop` | Arrêt complet (positions sauvegardées) |

### Information
| Bouton | Commande | Action |
|--------|----------|--------|
| 📊 Statut | `/status` | Capital, G/P jour, G/P total, positions |
| 📋 Trades | `/positions` | Détail chaque position ouverte |
| 📈 Stats | `/stats` | Winrate, drawdown, Sharpe, cooldowns |
| ⚙️ Aide | `/help` | Cette aide |

### Gestion positions
| Commande | Action |
|----------|--------|
| `/debug SYMBOL` | Diagnostiquer signal d'une paire |
| `/close SYMBOL` | Fermer une position (avec confirmation) |
| `/closeall` | Fermer toutes les positions |
| `/buy SYMBOL` | **Forcer entrée manuelle** (ignore filtres ⚠️) |
| `/skip SYMBOL` | Blacklist 24h (ignore ce pair) |

### Operationnel
| Commande | Action |
|----------|--------|
| `/boost` | Scan immédiat (au lieu d'attendre 60s) |
| `⚡ Boost` | Idem (bouton) |

---

## Fichiers

### Python
- **`main.py`** (723 lignes v16)
  - Telegram polling + commandes
  - Gestion threads + state global
  - Boucles : trading_loop, telegram_loop
  - Fonctions : send_message, build_status, build_trades, build_stats

- **`strategy.py`** (1129 lignes v15)
  - Classe TradingStrategy
  - Fetch OHLCV, calcul ATR/EMA/RSI
  - _analyze_signal (avec bear_mode)
  - _get_ts_mult (TS adaptatif)
  - open/close/update positions
  - morning_analysis, scan_market_summary
  - Persistance via positions.json

- **`requirements.txt`**
  ```
  ccxt>=4.5.54
  flask>=3.1.3
  pandas>=3.0.3
  requests>=2.34.2
  ```

### Config
- **`railway.json`** : Config déploiement Railway
- **`positions.json`** : État persistant (positions, cooldowns, skip list)
- **`.env`** (local) : TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, BINANCE_KEY, BINANCE_SECRET

---

## Métriques et Monitoring

### Dashboard Telegram (`/stats`)
```
📈 Statistiques — 🟢 Actif
💼 Capital : 100.00 USDC
📊 G/P total : +12.34 USDC | Aujourd'hui : +5.67 USDC
🔢 Trades : 15 (✅ 10 / ❌ 5)

📐 Métriques :
  Winrate : 66.7%
  Profit Factor : 2.45
  Expectancy : +0.82 USDC/trade
  Max Drawdown : 8.50 USDC
  Sharpe : 1.23

🔄 Cooldowns :
  BTC — cooldown 24h restantes
  SOL — skip 8h restantes
```

---

## Logs & Debugging

**Niveau INFO :**
```
2026-05-20 07:00:00 [INFO] Analyse matinale — Aucune position
2026-05-20 07:00:05 [INFO] Top 10 paires près du signal : BTC, ETH, SOL...
2026-05-20 08:30:45 [INFO] Signal complet sur SOL/USDC → Entrée 25.50 USDC
2026-05-20 09:15:30 [INFO] TP1 atteint sur SOL → Vente 25%, reste 50% TS
```

**Niveau ERROR :**
```
2026-05-20 10:45:12 [ERROR] Erreur boucle : connection timeout
2026-05-20 10:45:12 [ERROR] ⚠️ Erreur : connection timeout (envoyé à Telegram)
```

---

## Déploiement & Maintenance

### Railway Setup
1. Connecter le repo GitHub
2. Variables d'env : TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, BINANCE_KEY, BINANCE_SECRET
3. Build auto à chaque push vers `main`
4. Logs : onglet Deployments → voir les 10 derniers

### Redéploiement forcé
Si le bot reste bloqué :
1. Railway → Variables → ajouter `REBUILD=1`
2. Sauvegarder (redéploiement auto)
3. Attendre ~5 min
4. Retirer `REBUILD=1`

### Hard Reset (si nécessaire)
1. Railway → Settings → supprimer le service
2. Reconnecter le repo GitHub
3. Railway rebuild de zéro

---

## Notes & Limitations

### Limitations connues
- **PAPER mode seulement** : pas d'argent réel risqué
- **Spread fictif** : Binance spot a des spreads réels que le mode PAPER ignore
- **Slippage absent** : exécutions supposées au prix actuel
- **Liquidité limitée** : filtres 10M USDC élimine 90% des paires Binance

### À faire avant LIVE
- [ ] Backtest 6 mois sur historique réel
- [ ] Mode shadow live (trade réels avec ordres annulées)
- [ ] Monitorer Sharpe, Profit Factor > 1.5, drawdown max < 20%
- [ ] Stratégie B implémentée et testée
- [ ] Gestion des frais Binance (-0.1% par ordre)

---

## Contact & Support

**Problèmes courants :**
- Bot tourne pas : vérifier Railway → Deployments → voir les logs
- Commandes non reconnues : redémarrer le bot (`/stop` → `/start`)
- Double messages : correction V16 appliquée, ne devrait plus arriver

---

**Dernière mise à jour :** 2026-05-20 — V16 (race conditions fixes)
