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
RECAP_START_HOUR = 7
RECAP_END_HOUR   = 22
MORNING_HOUR     = 7

# ── Clavier Telegram ──────────────────────────────────────────────────────────
KEYBOARD = {
    "keyboard": [
        ["▶️ Démarrer", "⏸ Pause"],
        ["⏹ Arrêter",  "📊 Statut"],
        ["📋 Trades",   "⚙️ Aide"],
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
    "⚙️ aide":      "help",
    "/start":      "start",
    "/stop":       "stop",
    "/pause":      "pause",
    "/status":     "status",
    "/positions":  "positions",
    "/help":       "help",
}

# Confirmations en attente : chat_id → {"action": "close"|"closeall", "symbol": str|None}
pending_confirmations: dict = {}

# ── État global ───────────────────────────────────────────────────────────────
bot_running     = False
bot_paused      = False
strategy        = None
last_update_id  = 0
last_recap_hour = -1
last_reset_date = ""


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


# ── Statut (/statut) ──────────────────────────────────────────────────────────
def build_status() -> str:
    if not strategy:
        return "🔴 Bot arrêté."
    stats     = strategy.get_stats()
    positions = strategy.get_positions()
    state     = "⏸ En pause" if bot_paused else "🟢 Actif"

    lines = [
        f"{state} — Mode PAPER",
        f"💼 Capital : `{stats['capital']:.2f}` USDC | G/P jour : `{stats['pnl_today']:+.2f}` USDC | G/P total : `{stats['pnl']:+.2f}` USDC",
        f"🔢 Trades : {stats['total_trades']} (✅ {stats['wins']} / ❌ {stats['losses']})",
    ]

    if positions:
        lines.append(f"\n📌 *Positions ouvertes :*")
        for p in positions:
            gp_emoji = "📈" if p["pnl_pct"] >= 0 else "📉"
            lines.append(
                f"{gp_emoji} `{p['symbol']}` | G/P : `{p['pnl_usdc']:+.2f}` USDC (`{p['pnl_pct']:+.2f}%`)"
            )
    else:
        lines.append("\n📭 Aucune position ouverte")

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
            gp_emoji = "📈" if p["pnl_pct"] >= 0 else "📉"
            lines.append(
                f"{gp_emoji} `{p['symbol']}` | G/P : `{p['pnl_pct']:+.2f}%` | 💵 Gain : `{p['ts_pnl']:+.2f}` USDC"
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
        gp_emoji = "📈" if p["pnl_pct"] >= 0 else "📉"
        lines.append(
            f"{gp_emoji} `{p['symbol']}` | Ouvert le {p['opened_at']}\n"
            f"  Investi : `{p['size_usdc']:.2f}` USDC\n"
            f"  Entrée : `{p['entry']:.6f}` → Actuel : `{p['current']:.6f}`\n"
            f"  TS à : `{p['ts_price']:.6f}` | 💵 Résultat min si TS : `{p['ts_pnl']:+.2f}` USDC (`{p['ts_pct']:+.2f}%`)\n"
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


# ── Reset minuit ──────────────────────────────────────────────────────────────
def check_midnight_reset():
    global last_reset_date
    today = datetime.now(PARIS_TZ).date().isoformat()
    if today != last_reset_date:
        last_reset_date = today
        if strategy:
            strategy.reset_daily_pnl()


# ── Actions boutons / commandes ───────────────────────────────────────────────
def handle_action(action: str, chat_id: str, raw_text: str = ""):
    global bot_running, bot_paused, strategy

    # ── /debug SYMBOL ─────────────────────────────────────────────────────────
    if action == "debug":
        if not strategy:
            send_message("ℹ️ Aucune stratégie active.", chat_id)
            return
        parts = raw_text.strip().split()
        if len(parts) < 2:
            send_message("Usage : `/debug SYMBOL`\nEx : `/debug BTC/USDC`", chat_id)
            return
        symbol = parts[1].upper()
        if "/" not in symbol:
            symbol = symbol + "/USDC"
        send_message(strategy.debug_position(symbol), chat_id)
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
            f"💰 Capital : 100 USDC | SL -2% | TS -2% | RSI < 65 | Volume ×2 | Filtre 4h\n"
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
        send_message("⏸ *Pause* — stops toujours actifs.", chat_id)

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

    elif action == "help":
        send_message(
            "⚙️ *Aide*\n\n"
            "▶️ Démarrer — Lancer / reprendre\n"
            "⏸ Pause — Suspendre _(stops actifs)_\n"
            "⏹ Arrêter — Arrêt complet\n"
            "📊 Statut — Capital et positions\n"
            "📋 Trades — Détail des positions ouvertes\n"
            "⚙️ Aide — Cette aide\n"
            "`/debug SYMBOL` — Diagnostiquer une position\n"
            "`/close SYMBOL` — Fermer une position manuellement\n"
            "`/closeall` — Fermer toutes les positions\n\n"
            "_Signal : EMA20>50 sur 1h ET 4h | Volume ×2 | RSI < 65_\n"
            "_SL : -2% | TS : -2% depuis le plus haut_\n"
            "_Analyse matin 7h : tendance 1h+4h, fermeture auto si invalide_",
            chat_id,
        )


# ── Boucle de trading ─────────────────────────────────────────────────────────
def trading_loop():
    log.info("Boucle démarrée")
    while bot_running:
        try:
            check_midnight_reset()

            now_paris = datetime.now(PARIS_TZ)
            if now_paris.hour == MORNING_HOUR and strategy:
                for msg in strategy.morning_analysis():
                    send_message(msg)

            # Scan toujours actif (stops vérifiés même en pause)
            alerts = strategy.scan()
            for alert in alerts:
                send_message(alert)

            if should_send_recap():
                recap = build_recap()
                if recap:
                    send_message(recap)

        except Exception as e:
            log.error(f"Erreur boucle : {e}")
            send_message(f"⚠️ Erreur : `{e}`")

        time.sleep(60)

    log.info("Boucle arrêtée")


# ── Boucle Telegram ───────────────────────────────────────────────────────────
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

            # Commande /debug (avec argument)
            if text.lower().startswith("/debug"):
                handle_action("debug", chat_id, raw_text=text)
                continue

            # Commandes /close et /closeall
            if text.lower().startswith("/close"):
                handle_close(text, chat_id)
                continue

            # Confirmation en attente (OUI/NON)
            if chat_id in pending_confirmations:
                handle_confirmation(text, chat_id)
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
