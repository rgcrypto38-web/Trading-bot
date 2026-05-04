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


# ── Récap ─────────────────────────────────────────────────────────────────────
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
        f"📈 PnL du jour : `{stats['pnl_today']:+.2f}` USDC | Total : `{stats['pnl']:+.2f}` USDC",
        f"🔢 Trades : {stats['total_trades']} (✅ {stats['wins']} / ❌ {stats['losses']})",
    ]

    if positions:
        lines.append(f"\n📊 *{len(positions)} position(s) ouverte(s) :*")
        for p in positions:
            trend     = "📈" if p["pnl_pct"] >= 0 else "📉"
            sec_emoji = "🔒" if p["secured_pct"] >= 0 else "⚠️"
            lines.append(
                f"{trend} `{p['symbol']}` | PnL : `{p['pnl_pct']:+.2f}%` | {sec_emoji} Gain sécurisé : `{p['secured_pnl']:+.2f}` USDC"
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


# ── Reset minuit ──────────────────────────────────────────────────────────────
def check_midnight_reset():
    global last_reset_date
    today = datetime.now(PARIS_TZ).date().isoformat()
    if today != last_reset_date:
        last_reset_date = today
        if strategy:
            strategy.reset_daily_pnl()
            log.info("Reset PnL journalier effectué")


# ── Actions ───────────────────────────────────────────────────────────────────
def handle_action(action: str, chat_id: str):
    global bot_running, bot_paused, strategy

    if action == "start":
        if bot_running and not bot_paused:
            send_message("⚠️ Le bot tourne déjà.", chat_id)
            return
        if bot_paused:
            bot_paused = False
            send_message("▶️ *Bot repris* — scan actif.", chat_id)
            return
        bot_running = True
        bot_paused  = False
        strategy    = TradingStrategy(binance_key=BINANCE_KEY, binance_secret=BINANCE_SECRET)
        nb_pos      = len(strategy.positions)
        recap       = f"\n\n📂 {nb_pos} position(s) rechargée(s)." if nb_pos > 0 else ""
        threading.Thread(target=trading_loop, daemon=True).start()
        send_message(
            f"✅ *Bot démarré* — Mode PAPER\n"
            f"💰 100 USDC | 5 × 20 USDC | SL -2% | TS -2%\n"
            f"🕐 Récaps : {RECAP_START_HOUR}h → {RECAP_END_HOUR}h | Analyse matin : {MORNING_HOUR}h{recap}",
            chat_id,
        )

    elif action == "pause":
        if not bot_running:
            send_message("ℹ️ Le bot n'est pas démarré.", chat_id)
            return
        if bot_paused:
            send_message("ℹ️ Déjà en pause. Tape ▶️ Démarrer pour reprendre.", chat_id)
            return
        bot_paused = True
        send_message(
            "⏸ *Pause* — positions conservées, aucune nouvelle entrée.\n"
            "⚠️ Les stops restent actifs en pause.",
            chat_id,
        )

    elif action == "stop":
        if not bot_running:
            send_message("ℹ️ Le bot n'est pas en cours d'exécution.", chat_id)
            return
        bot_running = False
        bot_paused  = False
        send_message("⏹ *Bot arrêté.* Positions sauvegardées.", chat_id)

    elif action == "status":
        if not bot_running:
            send_message("🔴 Bot *arrêté*.", chat_id)
        else:
            send_message(build_recap(), chat_id)

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
            sec_emoji = "🔒" if p["secured_pct"] >= 0 else "⚠️"
            lines.append(
                f"\n`{p['symbol']}`\n"
                f"  Entrée : `{p['entry']:.4f}` → Actuel : `{p['current']:.4f}` | PnL : `{p['pnl_pct']:+.2f}%`\n"
                f"  {sec_emoji} Si TS déclenché → `{p['secured_pct']:+.2f}%` (`{p['secured_pnl']:+.2f}` USDC)"
            )
        send_message("\n".join(lines), chat_id)

    elif action == "help":
        send_message(
            "⚙️ *Aide*\n\n"
            "▶️ Démarrer — Lancer / reprendre\n"
            "⏸ Pause — Suspendre _(stops toujours actifs)_\n"
            "⏹ Arrêter — Arrêt complet\n"
            "📊 Statut — Capital, PnL, gain sécurisé\n"
            "📋 Trades — Positions ouvertes détaillées\n"
            "⚙️ Aide — Cette aide\n\n"
            "_SL : -2% depuis entrée | TS : -2% depuis le plus haut_\n"
            "_Analyse matin automatique à 7h avec verdict Garder/Abandonner_",
            chat_id,
        )


# ── Boucle de trading ─────────────────────────────────────────────────────────
def trading_loop():
    global bot_running, bot_paused
    log.info("Boucle démarrée")
    while bot_running:
        try:
            now_paris = datetime.now(PARIS_TZ)

            # Reset PnL journalier à minuit
            check_midnight_reset()

            # Analyse matin à 7h (une fois par jour)
            if now_paris.hour == MORNING_HOUR and strategy:
                morning_msgs = strategy.morning_analysis()
                for msg in morning_msgs:
                    send_message(msg)

            # Scan marché — stops TOUJOURS vérifiés, même en pause
            # (la garde drawdown est dans scan(), après la vérification des positions)
            alerts = strategy.scan()
            for alert in alerts:
                send_message(alert)

            # Récap horaire
            if should_send_recap():
                recap = build_recap()
                if recap:
                    send_message(recap)

        except Exception as e:
            log.error(f"Erreur boucle : {e}")
            send_message(f"⚠️ Erreur interne : `{e}`")

        time.sleep(60)

    log.info("Boucle arrêtée")


# ── Boucle Telegram ───────────────────────────────────────────────────────────
def telegram_loop():
    global last_update_id
    log.info("Écoute Telegram démarrée")
    send_message("🤖 *Bot en ligne* — utilise les boutons ou tape ⚙️ Aide.")
    while True:
        updates = get_updates(offset=last_update_id + 1)
        for update in updates:
            last_update_id = update["update_id"]
            msg     = update.get("message", {})
            text    = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if not text:
                continue
            action = BUTTON_MAP.get(text.lower())
            if action:
                log.info(f"Action : {action}")
                handle_action(action, chat_id)
            else:
                send_message(f"❓ Non reconnu : `{text}`\nUtilise les boutons ou ⚙️ Aide.", chat_id)
        time.sleep(1)


# ── Point d'entrée ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Démarrage")
    telegram_loop()
        
