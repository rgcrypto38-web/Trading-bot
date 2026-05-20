import os
import time
import threading
import logging
from datetime import datetime, timezone, timedelta
import requests
from strategy import TradingStrategy

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
BINANCE_KEY      = os.environ.get("BINANCE_KEY")
BINANCE_SECRET   = os.environ.get("BINANCE_SECRET")

if not TELEGRAM_TOKEN:
    raise EnvironmentError("Variable TELEGRAM_TOKEN manquante")
if not TELEGRAM_CHAT_ID:
    raise EnvironmentError("Variable TELEGRAM_CHAT_ID manquante")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ── Fuseau horaire ────────────────────────────────────────────────────────────
PARIS_TZ         = timezone(timedelta(hours=2))  # UTC+2 été / hours=1 en hiver
RECAP_START_HOUR   = 7
RECAP_END_HOUR     = 22
MORNING_HOUR       = 7
DIAGNOSTIC_HOURS   = {8, 12, 18}  # heures de diagnostic si aucune position

# ── Clavier Telegram ──────────────────────────────────────────────────────────
KEYBOARD = {
    "keyboard": [
        ["▶️ Démarrer", "⏸ Pause"],
        ["⏹ Arrêter",  "📊 Statut"],
        ["📋 Trades",   "🔍 Debug"],
        ["❌ Fermer",   "💣 Tout fermer"],
        ["📈 Stats",    "⚡ Boost"],
        ["⚙️ Aide"],
    ],
    "resize_keyboard": True,
    "persistent":      True,
}

BUTTON_MAP = {
    "▶️ démarrer": "start",
    "⏸ pause":     "pause",
    "⏹ arrêter":   "stop",
    "📊 statut":    "status",
    "📋 trades":    "positions",
    "🔍 debug":     "debug_prompt",
    "❌ fermer":    "close_prompt",
    "💣 tout fermer": "closeall_prompt",
    "📈 stats":     "stats",
    "⚡ boost":     "boost",
    "⚙️ aide":      "help",
    "/start":      "start",
    "/stop":       "stop",
    "/pause":      "pause",
    "/status":     "status",
    "/positions":  "positions",
    "/help":       "help",
    "/stats":      "stats",
    "/boost":      "boost",
}

# États conversationnels : chat_id → {"waiting": "debug"|"close"}
waiting_input: dict = {}

# Confirmations en attente : chat_id → {"action": "close"|"closeall", "symbol": str|None}
pending_confirmations: dict = {}

# ── État global ───────────────────────────────────────────────────────────────
bot_running          = False
bot_paused           = False
strategy             = None
last_update_id       = 0
last_recap_hour      = -1
last_diagnostic_hour = -1
last_reset_date      = ""
force_scan           = False   # déclenché par /boost


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_message(text: str, chat_id: str = None):
    cid = chat_id or TELEGRAM_CHAT_ID
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id":      cid,
                "text":         text,
                "parse_mode":   "Markdown",
                "reply_markup": KEYBOARD,
            },
            timeout=10,
        )
    except Exception as e:
        log.error(f"Envoi Telegram échoué : {e}")


def get_updates(offset: int = 0):
    try:
        r = requests.get(
            f"{TELEGRAM_API}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=35,
        )
        return r.json().get("result", [])
    except Exception as e:
        log.error(f"getUpdates échoué : {e}")
        return []


# ── Statut (/statut) — avec métriques ────────────────────────────────────────
def build_status() -> str:
    if not strategy:
        return "🔴 Bot arrêté."
    stats     = strategy.get_stats()
    metrics   = strategy.get_metrics()
    positions = strategy.get_positions()
    state     = "⏸ En pause" if bot_paused else "🟢 Actif"

    lines = [
        f"{state} — Mode PAPER",
        f"💼 Capital : `{stats['capital']:.2f}` USDC | G/P jour : `{stats['pnl_today']:+.2f}` USDC | G/P total : `{stats['pnl']:+.2f}` USDC",
        f"🔢 Trades : {stats['total_trades']} (✅ {stats['wins']} / ❌ {stats['losses']})",
    ]

    # Métriques (affichées uniquement si on a au moins 3 trades)
    if stats["total_trades"] >= 3:
        pf_str = (f"{metrics['profit_factor']:.2f}"
                  if metrics["profit_factor"] != float("inf") else "∞")
        lines.append(
            f"\n📐 *Métriques :*\n"
            f"  Winrate : `{metrics['winrate']:.1f}%` | Profit Factor : `{pf_str}`\n"
            f"  Expectancy : `{metrics['expectancy']:+.4f}` USDC/trade\n"
            f"  Max Drawdown : `{metrics['max_drawdown']:.2f}` USDC | Sharpe : `{metrics['sharpe']:.2f}`"
        )

    if positions:
        lines.append(f"\n📌 *Positions ouvertes :*")
        for p in positions:
            secured    = p.get("secured_pnl_usdc", 0.0)
            flottant   = p["pnl_usdc"]
            total_usdc = secured + flottant
            size_init  = p.get("size_usdc_initial", p["size_usdc"])
            total_pct  = total_usdc / size_init * 100 if size_init > 0 else 0.0
            gp_emoji   = "📈" if total_pct >= 0 else "📉"
            # Décomposition TP1 + TP2 + flottant
            tp1_str = f"TP1:{p['tp1_done'] and secured > 0 and '+' or ''}" if p["tp1_done"] else ""
            parts   = []
            if p["tp1_done"]:
                parts.append("TP1✅")
            if p["tp2_done"]:
                parts.append("TP2✅")
            parts.append(f"flottant:`{flottant:+.2f}`")
            detail = " + ".join(parts)
            lines.append(
                f"{gp_emoji} `{p['symbol']}` | "
                f"G/P : `{total_usdc:+.2f}` USDC ({detail}) → `{total_pct:+.2f}%`"
            )
    else:
        lines.append("\n📭 Aucune position ouverte")

    return "\n".join(lines)


def build_stats() -> str:
    """Statistiques détaillées : winrate, drawdown, sharpe, expectancy."""
    if not strategy:
        return "🔴 Bot arrêté."
    stats   = strategy.get_stats()
    metrics = strategy.get_metrics()
    state   = "⏸ En pause" if bot_paused else "🟢 Actif"

    lines = [
        f"📈 *Statistiques* — {state}",
        f"💼 Capital : `{stats['capital']:.2f}` USDC",
        f"📊 G/P total : `{stats['pnl']:+.2f}` USDC | Aujourd'hui : `{stats['pnl_today']:+.2f}` USDC",
        f"🔢 Trades : {stats['total_trades']} (✅ {stats['wins']} / ❌ {stats['losses']})",
    ]
    if stats["total_trades"] >= 3:
        pf_str = (f"{metrics['profit_factor']:.2f}"
                  if metrics["profit_factor"] != float("inf") else "∞")
        lines += [
            f"\n📐 *Métriques :*",
            f"  Winrate : `{metrics['winrate']:.1f}%`",
            f"  Profit Factor : `{pf_str}`",
            f"  Expectancy : `{metrics['expectancy']:+.4f}` USDC/trade",
            f"  Max Drawdown : `{metrics['max_drawdown']:.2f}` USDC",
            f"  Sharpe : `{metrics['sharpe']:.2f}`",
        ]
    else:
        lines.append(f"\n_Métriques disponibles à partir de 3 trades fermés._")

    # Cooldowns actifs
    if strategy:
        cd_status = strategy.get_cooldowns_status()
        lines.append(f"\n🔄 *Cooldowns :*\n{cd_status}")

    return "\n".join(lines)


# ── Récap horaire ─────────────────────────────────────────────────────────────
def build_recap() -> str:
    if not strategy:
        return ""
    stats     = strategy.get_stats()
    positions = strategy.get_positions()
    now       = datetime.now(PARIS_TZ).strftime("%H:%M")
    state     = "⏸ En pause" if bot_paused else "🟢 Actif"

    lines = [
        f"🕐 *Récap {now}* — {state}",
        f"💼 `{stats['capital']:.2f}` USDC | G/P jour : `{stats['pnl_today']:+.2f}` | Total : `{stats['pnl']:+.2f}` USDC",
        f"🔢 {stats['total_trades']} trades (✅ {stats['wins']} / ❌ {stats['losses']})",
    ]

    if positions:
        lines.append(f"\n📌 *{len(positions)} position(s) :*")
        for p in positions:
            secured    = p.get("secured_pnl_usdc", 0.0)
            flottant   = p["pnl_usdc"]
            total_usdc = secured + flottant
            size_init  = p.get("size_usdc_initial", p["size_usdc"])
            total_pct  = total_usdc / size_init * 100 if size_init > 0 else 0.0
            gp_emoji   = "📈" if total_pct >= 0 else "📉"
            parts      = []
            if p["tp1_done"]:
                parts.append("TP1✅")
            if p["tp2_done"]:
                parts.append("TP2✅")
            parts.append(f"flottant:`{flottant:+.2f}`")
            detail = " + ".join(parts)
            lines.append(
                f"{gp_emoji} `{p['symbol']}` | "
                f"G/P : `{total_usdc:+.2f}` USDC ({detail}) → `{total_pct:+.2f}%`"
            )
    else:
        lines.append("\n📭 Aucune position ouverte")

    return "\n".join(lines)


# ── Trades détaillés (/trades) ────────────────────────────────────────────────
def build_trades() -> str:
    if not strategy:
        return "ℹ️ Aucune stratégie active."
    positions = strategy.get_positions()
    if not positions:
        return "📭 Aucune position ouverte."

    lines = ["📋 *Positions ouvertes :*\n"]
    for p in positions:
        secured    = p.get("secured_pnl_usdc", 0.0)
        flottant   = p["pnl_usdc"]
        total_usdc = secured + flottant
        size_init  = p.get("size_usdc_initial", p["size_usdc"])
        total_pct  = total_usdc / size_init * 100 if size_init > 0 else 0.0
        gp_emoji   = "📈" if total_pct >= 0 else "📉"
        tp1_tag    = "✅" if p["tp1_done"] else "⏳"
        tp2_tag    = "✅" if p["tp2_done"] else "⏳"
        parts      = []
        if p["tp1_done"]:
            parts.append("TP1✅")
        if p["tp2_done"]:
            parts.append("TP2✅")
        parts.append(f"flottant:`{flottant:+.2f}`")
        detail = " + ".join(parts)
        lines.append(
            f"{gp_emoji} `{p['symbol']}` | Ouvert le {p['opened_at']}\n"
            f"  Investi : `{size_init:.2f}` USDC (restant : `{p['size_usdc']:.2f}` USDC)\n"
            f"  G/P : `{total_usdc:+.2f}` USDC ({detail}) → `{total_pct:+.2f}%`\n"
            f"  Entrée : `{p['entry']:.6f}` → Actuel : `{p['current']:.6f}`\n"
            f"  TP1 {tp1_tag} `{p['tp1_price']:.6f}` | TP2 {tp2_tag} `{p['tp2_price']:.6f}`\n"
            f"  TS : `{p['ts_price']:.6f}` | 💵 Min si TS : `{p['ts_pnl']:+.2f}` USDC (`{p['ts_pnl']/size_init*100 if size_init > 0 else 0:+.2f}%`)\n"
        )
    return "\n".join(lines)




def should_send_recap() -> bool:
    global last_recap_hour
    now = datetime.now(PARIS_TZ)
    h   = now.hour
    if RECAP_START_HOUR <= h < RECAP_END_HOUR and h != last_recap_hour:
        last_recap_hour = h
        return True
    return False


def should_send_diagnostic() -> bool:
    global last_diagnostic_hour
    now = datetime.now(PARIS_TZ)
    h   = now.hour
    if h in DIAGNOSTIC_HOURS and h != last_diagnostic_hour:
        last_diagnostic_hour = h
        return True
    return False


# ── Reset minuit ──────────────────────────────────────────────────────────────
def check_midnight_reset():
    global last_reset_date
    today = datetime.now(PARIS_TZ).date().isoformat()
    if today != last_reset_date:
        last_reset_date = today
        if strategy:
            strategy.reset_daily_pnl()


# ── Clôture manuelle avec confirmation ────────────────────────────────────────
def handle_close(text: str, chat_id: str):
    if not strategy:
        send_message("ℹ️ Aucune stratégie active.", chat_id)
        return

    if text.strip().upper() == "/CLOSEALL":
        positions = strategy.get_positions()
        if not positions:
            send_message("📭 Aucune position ouverte.", chat_id)
            return
        symbols = ", ".join([f"`{p['symbol']}`" for p in positions])
        pending_confirmations[chat_id] = {"action": "closeall", "symbol": None}
        send_message(
            f"⚠️ *Confirmation requise*\n\n"
            f"Fermer *toutes les positions* : {symbols} ?\n\n"
            f"Réponds *OUI* pour confirmer ou *NON* pour annuler.",
            chat_id,
        )
        return

    parts = text.strip().split()
    if len(parts) < 2:
        send_message("Usage : `/close SYMBOL`\nEx : `/close GMX` ou `/close GMX/USDC`", chat_id)
        return

    symbol = parts[1].upper()
    if "/" not in symbol:
        symbol = symbol + "/USDC"

    if symbol not in strategy.positions:
        send_message(f"❓ Aucune position ouverte sur `{symbol}`.", chat_id)
        return

    pos = strategy.positions[symbol]
    try:
        price = float(strategy.exchange.fetch_ticker(symbol)["last"])
        pnl_u, pnl_p = strategy._calc_pnl(pos["size_usdc"], pos["entry"], price)
        prix_str = (f"Prix actuel : `{price:.6f}` | "
                    f"G/P estimé : `{pnl_u:+.2f}` USDC (`{pnl_p:+.2f}%`)")
    except Exception:
        prix_str = "Prix actuel indisponible"

    pending_confirmations[chat_id] = {"action": "close", "symbol": symbol}
    send_message(
        f"⚠️ *Confirmation requise*\n\nFermer `{symbol}` ?\n{prix_str}\n\n"
        f"Réponds *OUI* pour confirmer ou *NON* pour annuler.",
        chat_id,
    )


def handle_confirmation(text: str, chat_id: str):
    pending  = pending_confirmations.get(chat_id)
    if not pending:
        return
    response = text.strip().upper()
    if response == "NON":
        del pending_confirmations[chat_id]
        send_message("❎ Annulé.", chat_id)
        return
    if response == "OUI":
        del pending_confirmations[chat_id]
        if not strategy:
            send_message("ℹ️ Aucune stratégie active.", chat_id)
            return
        if pending["action"] == "close":
            send_message(strategy.close_position_manual(pending["symbol"]), chat_id)
        elif pending["action"] == "closeall":
            msgs = strategy.close_all_manual()
            for msg in msgs:
                send_message(msg, chat_id)
            if not msgs:
                send_message("📭 Aucune position à fermer.", chat_id)
        elif pending["action"] == "buy":
            msg = strategy.open_position_manual(pending["symbol"])
            send_message(msg if msg else "⚠️ Entrée impossible.", chat_id)
        return
    send_message("Réponds *OUI* pour confirmer ou *NON* pour annuler.", chat_id)


# ── Réponse aux demandes de symbole ──────────────────────────────────────────
def handle_waiting_input(text: str, chat_id: str):
    state = waiting_input.pop(chat_id, None)
    if not state:
        return
    symbol = text.strip().upper()
    if "/" not in symbol:
        symbol = symbol + "/USDC"

    if state["waiting"] == "debug":
        if not strategy:
            send_message("ℹ️ Aucune stratégie active.", chat_id)
            return
        send_message(strategy.debug_position(symbol), chat_id)

    elif state["waiting"] == "close":
        handle_close(f"/close {symbol}", chat_id)


def handle_buy(text: str, chat_id: str):
    """Force l'achat d'une paire avec confirmation."""
    if not strategy:
        send_message("ℹ️ Aucune stratégie active.", chat_id)
        return
    parts = text.strip().split()
    if len(parts) < 2:
        send_message(
            "Usage : `/buy SYMBOL`\nEx : `/buy SOL` ou `/buy SOL/USDC`\n"
            "⚠️ Tous les filtres seront ignorés.", chat_id)
        return
    symbol = parts[1].upper()
    if "/" not in symbol:
        symbol = symbol + "/USDC"
    pending_confirmations[chat_id] = {"action": "buy", "symbol": symbol}
    send_message(
        f"⚠️ *Confirmation achat forcé*\n\n"
        f"Entrée manuelle sur `{symbol}` ?\n"
        f"_Tous les filtres seront ignorés. SL/TS/TP normaux appliqués._\n\n"
        f"Réponds *OUI* pour confirmer ou *NON* pour annuler.",
        chat_id,
    )


def handle_skip(text: str, chat_id: str):
    """Blacklist manuelle d'une paire 24h."""
    if not strategy:
        send_message("ℹ️ Aucune stratégie active.", chat_id)
        return
    parts = text.strip().split()
    if len(parts) < 2:
        send_message("Usage : `/skip SYMBOL`\nEx : `/skip SOL` ou `/skip SOL/USDC`", chat_id)
        return
    symbol = parts[1].upper()
    if "/" not in symbol:
        symbol = symbol + "/USDC"
    send_message(strategy.skip_symbol(symbol), chat_id)


# ── Actions boutons / commandes ───────────────────────────────────────────────
def handle_action(action: str, chat_id: str, raw_text: str = ""):
    global bot_running, bot_paused, strategy

    if action == "debug":
        if not strategy:
            send_message("ℹ️ Aucune stratégie active.", chat_id)
            return
        parts = raw_text.strip().split()
        if len(parts) < 2:
            send_message("Usage : `/debug SYMBOL`\nEx : `/debug ETH/USDC`", chat_id)
            return
        symbol = parts[1].upper()
        if "/" not in symbol:
            symbol = symbol + "/USDC"
        send_message(strategy.debug_position(symbol), chat_id)
        return

    if action == "debug_prompt":
        if not strategy:
            send_message("ℹ️ Aucune stratégie active.", chat_id)
            return
        positions = strategy.get_positions()
        if not positions:
            send_message("📭 Aucune position ouverte à diagnostiquer.", chat_id)
            return
        symbols = "\n".join([f"• `{p['symbol']}`" for p in positions])
        waiting_input[chat_id] = {"waiting": "debug"}
        send_message(
            f"🔍 *Debug — quelle paire ?*\n\nPositions ouvertes :\n{symbols}\n\n"
            f"Envoie le symbole (ex: `ETH` ou `ETH/USDC`)",
            chat_id,
        )
        return

    if action == "close_prompt":
        if not strategy:
            send_message("ℹ️ Aucune stratégie active.", chat_id)
            return
        positions = strategy.get_positions()
        if not positions:
            send_message("📭 Aucune position ouverte à fermer.", chat_id)
            return
        symbols = "\n".join([
            f"• `{p['symbol']}` | G/P : `{p['pnl_pct']:+.2f}%`"
            for p in positions
        ])
        waiting_input[chat_id] = {"waiting": "close"}
        send_message(
            f"❌ *Fermer — quelle paire ?*\n\nPositions ouvertes :\n{symbols}\n\n"
            f"Envoie le symbole (ex: `ETH` ou `ETH/USDC`)",
            chat_id,
        )
        return

    if action == "closeall_prompt":
        handle_close("/closeall", chat_id)
        return

    if action == "start":
        if bot_running and not bot_paused:
            send_message("⚠️ Le bot tourne déjà.", chat_id)
            return
        if bot_paused:
            bot_paused = False
            send_message("▶️ *Bot repris.*", chat_id)
            return
        bot_running = True
        bot_paused  = False
        strategy    = TradingStrategy(binance_key=BINANCE_KEY, binance_secret=BINANCE_SECRET)
        nb_pos      = len(strategy.positions)
        recap       = f"\n📂 {nb_pos} position(s) rechargée(s)." if nb_pos > 0 else ""
        threading.Thread(target=trading_loop, daemon=True).start()
        send_message(
            f"✅ *Bot démarré* — Mode PAPER\n"
            f"💰 Capital : 100 USDC | 5 positions max\n"
            f"📐 ATR dynamique | SL ×2.5 ATR | TS ×3.0 ATR\n"
            f"🎯 TP1 +3×ATR (25%) | TP2 +5×ATR (25%) | Reste 50% TS\n"
            f"🔍 Filtres : liquidité 10M | spread | BTC régime | RSI 45–85 | pente EMA\n"
            f"🕐 Récaps : {RECAP_START_HOUR}h–{RECAP_END_HOUR}h | Analyse matin : {MORNING_HOUR}h{recap}",
            chat_id,
        )

    elif action == "pause":
        if not bot_running:
            send_message("ℹ️ Le bot n'est pas démarré.", chat_id)
            return
        if bot_paused:
            send_message("ℹ️ Déjà en pause.", chat_id)
            return
        bot_paused = True
        send_message("⏸ *Pause* — stops et TP toujours actifs.", chat_id)

    elif action == "stop":
        if not bot_running:
            send_message("ℹ️ Le bot n'est pas en cours d'exécution.", chat_id)
            return
        bot_running = False
        bot_paused  = False
        send_message("⏹ *Bot arrêté.* Positions sauvegardées.", chat_id)

    elif action == "status":
        send_message(build_status(), chat_id)

    elif action == "positions":
        send_message(build_trades(), chat_id)

    elif action == "stats":
        send_message(build_stats(), chat_id)

    elif action == "boost":
        global force_scan
        if not bot_running:
            send_message("ℹ️ Le bot n'est pas démarré.", chat_id)
            return
        force_scan = True
        send_message("⚡ *Scan forcé* — le bot scanne dans les prochaines secondes.", chat_id)

    elif action == "help":
        send_message(
            "⚙️ *Aide*\n\n"
            "▶️ Démarrer — Lancer / reprendre\n"
            "⏸ Pause — Suspendre _(stops et TP actifs)_\n"
            "⏹ Arrêter — Arrêt complet\n"
            "📊 Statut — Capital, positions, métriques\n"
            "📋 Trades — Détail des positions ouvertes\n"
            "📈 Stats — Winrate, drawdown, sharpe, cooldowns\n"
            "⚡ Boost — Forcer un scan immédiat\n"
            "⚙️ Aide — Cette aide\n\n"
            "`/debug SYMBOL` — Diagnostiquer une position\n"
            "`/close SYMBOL` — Fermer une position\n"
            "`/closeall` — Fermer toutes les positions\n"
            "`/buy SYMBOL` — Forcer entrée manuelle ⚠️\n"
            "`/skip SYMBOL` — Blacklister une paire 24h\n"
            "`/stats` — Statistiques détaillées\n"
            "`/boost` — Scan immédiat\n\n"
            "_Signal : EMA20>50 (1h+4h) | Pente EMA | Volume ×2 | RSI 45–85_\n"
            "_Bear mode : Volume ×3 | RSI 55–85 (filtres resserrés)_\n"
            "_Filtres : liquidité 10M | spread 0.15% | BTC > EMA200 4h_\n"
            "_Stops ATR : SL ×2.5 | TS adaptatif ×2.0–×3.5 | TP1 +3×ATR (25%) | TP2 +5×ATR (25%)_",
            chat_id,
        )


# ── Boucle de trading ─────────────────────────────────────────────────────────
def trading_loop():
    global force_scan
    log.info("Boucle démarrée")
    while bot_running:
        try:
            check_midnight_reset()

            now_paris = datetime.now(PARIS_TZ)
            if now_paris.hour == MORNING_HOUR and strategy:
                for msg in strategy.morning_analysis():
                    send_message(msg)

            # Scan toujours actif (stops et TP vérifiés même en pause)
            # Force scan si /boost demandé
            if force_scan:
                force_scan = False
                log.info("Scan forcé via /boost")

            alerts = strategy.scan()
            for alert in alerts:
                send_message(alert)

            positions = strategy.get_positions() if strategy else []
            if positions:
                if should_send_recap():
                    recap = build_recap()
                    if recap:
                        send_message(recap)
            else:
                if should_send_diagnostic() and strategy:
                    send_message(strategy.scan_market_summary())

        except Exception as e:
            log.error(f"Erreur boucle : {e}")
            send_message(f"⚠️ Erreur : `{e}`")

        # Attendre 60s mais interruptible par force_scan
        for _ in range(60):
            if force_scan or not bot_running:
                break
            time.sleep(1)

    log.info("Boucle arrêtée")


# ── Boucle Telegram (long polling) ───────────────────────────────────────────
def telegram_loop():
    global last_update_id
    log.info("Écoute Telegram démarrée")
    send_message("🤖 *Bot en ligne* — utilise les boutons ou ⚙️ Aide.")
    while True:
        updates = get_updates(offset=last_update_id + 1)
        for update in updates:
            last_update_id = update["update_id"]
            msg     = update.get("message", {})
            text    = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if not text:
                continue

            if text.lower().startswith("/debug"):
                handle_action("debug", chat_id, raw_text=text)
                continue

            if text.lower().startswith("/close"):
                handle_close(text, chat_id)
                continue

            if text.lower().startswith("/buy"):
                handle_buy(text, chat_id)
                continue

            if text.lower().startswith("/skip"):
                handle_skip(text, chat_id)
                continue

            if chat_id in pending_confirmations:
                handle_confirmation(text, chat_id)
                continue

            # Réponse à une demande de symbole (debug ou close)
            if chat_id in waiting_input:
                handle_waiting_input(text, chat_id)
                continue

            action = BUTTON_MAP.get(text.lower())
            if action:
                handle_action(action, chat_id)
            else:
                send_message(f"❓ Non reconnu : `{text}`\nUtilise les boutons ou ⚙️ Aide.", chat_id)

        time.sleep(1)


# ── Point d'entrée ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Démarrage")
    telegram_loop()
