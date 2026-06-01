# Trading-bot — bot crypto à deux moteurs (PAPER)

Bot de trading crypto en **mode papier**, déployé sur **Railway**, piloté via **Telegram**, connecté à **Binance spot** par **CCXT**. Développé via GitHub mobile → Railway.

> Statut : refonte complète (mai 2026). Architecture à deux moteurs spécialisés par régime + aiguilleur. **Le backtest avec coûts reste à faire** — tant qu'il n'est pas validé, l'edge est une hypothèse, pas un acquis.

---

## Philosophie

- **Espérance positive, pas winrate.** Un breakout gagne ~35-45 % du temps mais laisse courir ses rares gros gagnants. La métrique de référence est le **multiple de R** (R = risque initial d'un trade), pas le pourcentage de trades gagnants.
- **Un moteur par régime.** La tendance et le range demandent des logiques opposées ; un aiguilleur ADX active l'un *ou* l'autre, jamais les deux.
- **Une stratégie émet des signaux, jamais des messages.** Toute la communication Telegram est centralisée dans `alerts.py` — c'est ce qui élimine à la racine les doublons et les divergences de format.
- **Discipline de fichiers.** Chaque module reste bien sous ~50 k caractères (contrainte de l'éditeur GitHub mobile).

---

## Architecture

| Fichier | Rôle |
|---|---|
| `config.py` | Tous les paramètres + registre des stratégies. Le seul endroit à éditer pour régler le bot. |
| `indicators.py` | Indicateurs purs : ATR, RSI, EMA, SMA, Bollinger, Donchian, ADX. |
| `base_strategy.py` | Le contrat : `Signal` (dataclass) + `BaseStrategy`. Dimensionnement par le risque, calcul du R. |
| `strategy_breakout.py` | Moteur **BRK** (tendance). |
| `strategy_mean_reversion.py` | Moteur **MR** (range). |
| `regime.py` | L'aiguilleur ADX + filtre macro BTC. |
| `alerts.py` | **Seule** couche Telegram : formatage et envoi de tous les messages. |
| `main.py` | Orchestrateur : univers, boucle unique, exécution paper, persistance, commandes. |

**Ajouter une stratégie** = créer `strategy_xxx.py` (respectant `BaseStrategy`) + une ligne dans `STRATEGIES` de `config.py`. `main.py` et `alerts.py` ne bougent pas ; elle apparaît seule dans les récaps et la perf via son `tag`.

---

## Les deux moteurs

L'aiguilleur (`regime.py`) calcule l'**ADX(14)** par paire :

- **ADX > 25 → TREND** : moteur Breakout actif (si BTC haussier).
- **ADX < 20 → RANGE** : moteur Mean Reversion actif.
- **20–25 → zone morte** : aucun moteur (on s'abstient dans les transitions).

Filtre macro : **pas de long breakout si BTC < EMA200 (4h)**.

### Breakout (BRK) — régime directionnel
- **Entrée** : clôture 1h au-dessus du plus-haut Donchian 20 **+ volume > 1,5× la moyenne 20**.
- **Stop initial (1R)** : `max(entrée − 1,5×ATR, niveau cassé)`.
- **Sortie** : aucun take-profit. Trailing à **3 étages**, piloté par le profit en R (largeur en ATR), **sans plafond** :

| Pic atteint | Largeur du trailing | Plancher |
|---|---|---|
| < +2R | 3,0×ATR | — |
| ≥ +2R | 2,0×ATR | **seuil d'entrée** (le trade ne peut plus être perdant) |
| ≥ +5R | 1,5×ATR | seuil d'entrée |

### Mean Reversion (MR) — régime de range
- **Entrée** : prix ≤ bande de Bollinger basse (20, 2σ) **ou** RSI(14) < 30.
- **Cible** : retour à la moyenne (SMA 20).
- **Stop dur** : entrée − 1,5×ATR (non négociable).

---

## Gestion du risque

- **1 % du capital risqué par trade** : taille = risque ÷ distance au stop.
- **3 positions simultanées** maximum (toutes stratégies confondues).
- **Coupe-circuit** : plus d'ouverture après −5 % sur la journée.
- **Coûts** modélisés à **~0,3 % aller-retour** (frais 0,1 % + slippage) — à injecter dans tout backtest.

---

## Univers dynamique

Pas de liste figée. Au démarrage et **une fois par jour (8h)**, le bot retient les paires **/USDC spot** dont le **volume 24h ≥ 5 M USDC**, en excluant les tokens à effet de levier (UP/DOWN/BULL/BEAR) et les paires stable/fiat. Une paire portant une **position ouverte n'est jamais retirée** tant qu'elle n'est pas clôturée.

---

## Messages Telegram

- **Événementiel** : entrée (prix, taille, stop = 1R, motif) ; sortie (P/L en %, en USDC et en **multiple de R**) ; **changement de régime macro** (BTC franchit l'EMA200).
- **Programmé** : **récap consolidé 2×/jour (8h et 20h)** — capital et P/L par stratégie, positions ouvertes, régime, **scan marché**.
- **À la demande** (clavier) : **Statut** (récap instantané), **Positions** (détail), **Perf** (winrate, R moyen/espérance, gagnant/perdant moyen en R, payoff, PF, drawdown).
- **Système** : démarrage (après purge de la file, anti-doublons), erreurs (anti-spam).

**Achat forcé** : `/buy <paire>` (ex. `/buy sol`) ouvre manuellement une position, gérée comme un breakout (stop 1,5×ATR, risque 1 %, trailing 3 étages). Outrepasse le régime, le scan et la pause ; respecte le plafond de positions et 1 position/paire.

Clavier : `Démarrer · Pause · Statut · Positions · Perf · Régime · Fermer · Tout fermer · Boost · Aide`. Aucune commande par stratégie : tout se décline par `tag`.

---

## Persistance & déploiement

Les positions (`positions.json`) et l'historique des trades (`trades.json`) sont sauvegardés en continu et **rechargés au redémarrage** sans réémettre d'alertes — le trailing reprend où il en était.

⚠️ **Le système de fichiers de Railway est éphémère** : il est effacé à chaque redéploiement. Pour que les données survivent à un `git push`, **monter un volume Railway** et exposer son chemin via la variable `RAILWAY_VOLUME_MOUNT_PATH` (lue automatiquement par `config.py`). Sans volume, les données survivent à un simple redémarrage mais **pas** à un nouveau déploiement.

### Variables d'environnement
| Variable | Rôle |
|---|---|
| `TELEGRAM_TOKEN` | Token du bot Telegram (sinon : sortie console). |
| `TELEGRAM_CHAT_ID` | Chat destinataire des messages. |
| `RAILWAY_VOLUME_MOUNT_PATH` | Chemin du volume persistant (recommandé). |

### Lancer
```bash
pip install -r requirements.txt
python main.py
```
Sur Railway, `railway.json` définit déjà `python main.py` comme commande de démarrage.

---

## Limites & prochaine étape

- **Le backtest avec coûts n'est pas encore fait.** C'est lui qui validera (ou non) l'espérance positive et les seuils choisis (2R / 5R, multiples ATR, seuil de volume). À livrer en script autonome.
- La **détection de régime** est le maillon faible (en retard d'une phase) ; la zone morte 20–25 limite le whipsaw mais reste à éprouver.
- Tous les seuils sont des **hypothèses de départ raisonnables**, pas des optima prouvés.
