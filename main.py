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

# ── Config depuis variables d'environnement ───────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
BINANCE_KEY      = os.environ.get("BINANCE_KEY")
BINANCE_SECRET   = os.environ.get("BINANCE_SECRET")

if not TELEGRAM_TOKEN:
    raise EnvironmentError("Variable TELEGRAM_TOKEN manquante")
if not TELEGRAM_CHAT_ID:
    raise EnvironmentError("Variable TELEGRAM_CHAT_ID manquante")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ── Fuseau horaire Paris ──────────────────────────────────────────────────────
PARIS_TZ        = timezone(timedelta(hours=2))  # UTC+2 été / passer à hours=1 en hiver
RECAP_START_HOUR = 7
RECAP_END_HOUR   = 22

# ── Clavier Telegram persistant ──────────────────────────────────────────────
KEYBOARD = {
    "keyboard": [
        ["▶️ Démarrer", "⏸ Pause"],
        ["⏹ Arrêter",  "📊 Statut"],
        ["📋 Trades",   "⚙️ Aide"],
    ],
    "resize_keyboard": True,
    "persistent": True,
}

# Mapping boutons → actions
BUTTON_MAP = {
    "▶️ démarrer": "start",
    "⏸ pause":     "pause",
    "⏹ arrêter":   "stop",
    "📊 statut":    "status",
    "📋 trades":    "positions",
    "⚙️ aide":      "help",
    # commandes slash (compatibilité)
    "/start":     "start",
    "/stop":      "stop",
    "/pause":     "pause",
    "/status":    "status",
    "/positions": "positions",
    "/help":      "help",
}

# ── État global ───────────────────────────────────────────────────────────────
bot_running    = False
bot_paused     = False
strategy       = None
last_update_id = 0
last_recap_hour = -1


# ── Telegram helpers ──────────────────────────────────────────────────────────
def send_message(text: str, chat_id: str = None):
    cid = chat_id or TELEGRAM_CHAT_ID
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id":    cid,
                "text":       text,
                "parse_mode": "Markdown",
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


# ── Récap horaire ─────────────────────────────────────────────────────────────
def build_recap() -> str:
    if not strategy:
        return ""
    stats     = strategy.get_stats()
    positions = strategy.get_positions()
    now       = datetime.now(PARIS_TZ).strftime("%H:%M")
    state     = "⏸ En pause" if bot_paused else "🟢 Actif"

    lines = [
        f"🕐 *Récap {now}* — Mode PAPER | {state}",
        f"💼 Capital disponible : `{stats['capital']:.2f}` USDC",
        f"📈 PnL total : `{stats['pnl']:+.2f}` USDC",
        f"🔢 Trades : {stats['total_trades']} (✅ {stats['wins']} / ❌ {stats['losses']})",
    ]

    if positions:
        lines.append(f"\n📊 *{len(positions)} position(s) ouverte(s) :*")
        for p in positions:
            lines.append(
                f"• `{p['symbol']}` | Entrée : {p['entry']:.4f} | PnL : {p['pnl_pct']:+.2f}%"
            )
    else:
        lines.append("\n📭 Aucune position ouverte")

    return "\n".join(lines)


def should_send_recap() -> bool:
    global last_recap_hour
    now          = datetime.now(PARIS_TZ)
    current_hour = now.hour
    if RECAP_START_HOUR <= current_hour < RECAP_END_HOUR:
        if current_hour != last_recap_hour:
            last_recap_hour = current_hour
            return True
    return False


# ── Gestionnaire d'actions ────────────────────────────────────────────────────
def handle_action(action: str, chat_id: str):
    global bot_running, bot_paused, strategy

    # ── START ─────────────────────────────────────────────────────────────────
    if action == "start":
        if bot_running and not bot_paused:
            send_message("⚠️ Le bot tourne déjà.", chat_id)
            return
        if bot_paused:
            bot_paused = False
            send_message("▶️ *Bot repris* — scan du marché actif.", chat_id)
            return
        bot_running = True
        bot_paused  = False
        strategy    = TradingStrategy(
            binance_key=BINANCE_KEY,
            binance_secret=BINANCE_SECRET,
        )
        nb_pos = len(strategy.positions)
        recap  = f"\n\n📂 {nb_pos} position(s) rechargée(s) depuis la sauvegarde." if nb_pos > 0 else ""
        threading.Thread(target=trading_loop, daemon=True).start()
        send_message(
            f"✅ *Bot démarré* — Mode PAPER actif\n"
            f"💰 Capital total : 100 USDC | 5 positions × 20 USDC\n"
            f"🕐 Récaps automatiques : 7h → 22h (heure Paris){recap}",
            chat_id,
        )

    # ── PAUSE ─────────────────────────────────────────────────────────────────
    elif action == "pause":
        if not bot_running:
            send_message("ℹ️ Le bot n'est pas démarré.", chat_id)
            return
        if bot_paused:
            send_message("ℹ️ Le bot est déjà en pause. Tape ▶️ Démarrer pour reprendre.", chat_id)
            return
        bot_paused = True
        send_message(
            "⏸ *Bot en pause* — positions conservées, aucune nouvelle entrée.\n"
            "Tape ▶️ Démarrer pour reprendre.",
            chat_id,
        )

    # ── STOP ──────────────────────────────────────────────────────────────────
    elif action == "stop":
        if not bot_running:
            send_message("ℹ️ Le bot n'est pas en cours d'exécution.", chat_id)
            return
        bot_running = False
        bot_paused  = False
        send_message("⏹ *Bot arrêté.* Les positions sont sauvegardées.", chat_id)

    # ── STATUS ────────────────────────────────────────────────────────────────
    elif action == "status":
        if not bot_running:
            send_message("🔴 Bot *arrêté*.", chat_id)
        else:
            send_message(build_recap(), chat_id)

    # ── POSITIONS ─────────────────────────────────────────────────────────────
    elif action == "positions":
        if not strategy:
            send_message("ℹ️ Aucune stratégie active.", chat_id)
            return
        positions = strategy.get_positions()
        if not positions:
            send_message("📭 Aucune position ouverte.", chat_id)
            return
        lines = ["📊 *Positions ouvertes :*"]
        for p in positions:
            lines.append(
                f"• `{p['symbol']}` | Entrée : {p['entry']:.4f} | PnL : {p['pnl_pct']:+.2f}%"
            )
        send_message("\n".join(lines), chat_id)

    # ── HELP ──────────────────────────────────────────────────────────────────
    elif action == "help":
        msg = (
            "⚙️ *Aide — Commandes disponibles :*\n\n"
            "▶️ Démarrer — Lancer / reprendre le bot\n"
            "⏸ Pause — Suspendre sans fermer les positions\n"
            "⏹ Arrêter — Arrêter complètement le bot\n"
            "📊 Statut — PnL et état général\n"
            "📋 Trades — Positions ouvertes\n"
            "⚙️ Aide — Cette aide\n\n"
            "_Les commandes /start /stop /status /positions /help fonctionnent aussi._"
        )
        send_message(msg, chat_id)


# ── Boucle de trading ─────────────────────────────────────────────────────────
def trading_loop():
    global bot_running, bot_paused
    log.info("Boucle de trading démarrée")
    while bot_running:
        try:
            if not bot_paused:
                alerts = strategy.scan()
                for alert in alerts:
                    send_message(alert)

            if should_send_recap():
                recap = build_recap()
                if recap:
                    send_message(recap)

        except Exception as e:
            log.error(f"Erreur boucle trading : {e}")
            send_message(f"⚠️ Erreur interne : `{e}`")

        time.sleep(60)

    log.info("Boucle de trading arrêtée")


# ── Boucle Telegram (long polling) ───────────────────────────────────────────
def telegram_loop():
    global last_update_id
    log.info("Écoute Telegram démarrée")
    send_message("🤖 *Bot en ligne* — utilise les boutons ou tape /help.")
    while True:
        updates = get_updates(offset=last_update_id + 1)
        for update in updates:
            last_update_id = update["update_id"]
            msg    = update.get("message", {})
            text   = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            if not text:
                continue

            key    = text.lower()
            action = BUTTON_MAP.get(key)

            if action:
                log.info(f"Action : {action} (chat {chat_id})")
                handle_action(action, chat_id)
            else:
                send_message(
                    f"❓ Non reconnu : `{text}`\nUtilise les boutons ou tape ⚙️ Aide.",
                    chat_id,
                )

        time.sleep(1)


# ── Point d'entrée ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Démarrage du trading bot")
    telegram_loop()
