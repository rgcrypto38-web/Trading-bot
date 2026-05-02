import os
import time
import threading
import logging
from datetime import datetime
import requests
from strategy import TradingStrategy

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Config depuis variables d'environnement ───────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
BINANCE_KEY = os.environ.get("BINANCE_KEY")
BINANCE_SECRET = os.environ.get("BINANCE_SECRET")

if not TELEGRAM_TOKEN:
    raise EnvironmentError("Variable TELEGRAM_TOKEN manquante")
if not TELEGRAM_CHAT_ID:
    raise EnvironmentError("Variable TELEGRAM_CHAT_ID manquante")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ── État global ───────────────────────────────────────────────────────────────
bot_running = False
strategy = None
last_update_id = 0


# ── Telegram helpers ──────────────────────────────────────────────────────────
def send_message(text: str, chat_id: str = None):
    cid = chat_id or TELEGRAM_CHAT_ID
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": cid, "text": text, "parse_mode": "Markdown"},
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


# ── Commandes Telegram ────────────────────────────────────────────────────────
def handle_command(cmd: str, chat_id: str):
    global bot_running, strategy

    if cmd == "/start":
        if bot_running:
            send_message("⚠️ Le bot tourne déjà.", chat_id)
            return
        bot_running = True
        strategy = TradingStrategy(
            binance_key=BINANCE_KEY,
            binance_secret=BINANCE_SECRET,
        )
        threading.Thread(target=trading_loop, daemon=True).start()
        send_message("✅ *Bot démarré* — Mode PAPER actif\n💰 Capital : 100 USDC | 5 positions × 20 USDC", chat_id)

    elif cmd == "/stop":
        if not bot_running:
            send_message("ℹ️ Le bot n'est pas en cours d'exécution.", chat_id)
            return
        bot_running = False
        send_message("🛑 *Bot arrêté.*", chat_id)

    elif cmd == "/status":
        if not bot_running:
            send_message("🔴 Bot *arrêté*.", chat_id)
        else:
            stats = strategy.get_stats() if strategy else {}
            pnl = stats.get("pnl", 0.0)
            capital = stats.get("capital", 100.0)
            trades = stats.get("total_trades", 0)
            wins = stats.get("wins", 0)
            losses = stats.get("losses", 0)
            msg = (
                f"🟢 Bot *actif* — Mode PAPER\n"
                f"💼 Capital : `{capital:.2f}` USDC\n"
                f"📈 PnL total : `{pnl:+.2f}` USDC\n"
                f"🔢 Trades : {trades} (✅ {wins} / ❌ {losses})"
            )
            send_message(msg, chat_id)

    elif cmd == "/positions":
        if not strategy:
            send_message("ℹ️ Aucune stratégie active.", chat_id)
            return
        positions = strategy.get_positions()
        if not positions:
            send_message("📭 Aucune position ouverte.", chat_id)
            return
        lines = ["📊 *Positions ouvertes :*"]
        for p in positions:
            pnl_pct = p.get("pnl_pct", 0.0)
            lines.append(
                f"• `{p['symbol']}` | Entrée : {p['entry']:.4f} | PnL : {pnl_pct:+.2f}%"
            )
        send_message("\n".join(lines), chat_id)

    elif cmd == "/help":
        msg = (
            "📋 *Commandes disponibles :*\n\n"
            "/start — Démarrer le bot\n"
            "/stop — Arrêter le bot\n"
            "/status — PnL et état général\n"
            "/positions — Positions ouvertes\n"
            "/help — Cette aide"
        )
        send_message(msg, chat_id)

    else:
        send_message(f"❓ Commande inconnue : `{cmd}`\nTape /help pour la liste.", chat_id)


# ── Boucle de trading ─────────────────────────────────────────────────────────
def trading_loop():
    global bot_running
    log.info("Boucle de trading démarrée")
    while bot_running:
        try:
            alerts = strategy.scan()
            for alert in alerts:
                send_message(alert)
        except Exception as e:
            log.error(f"Erreur boucle trading : {e}")
            send_message(f"⚠️ Erreur interne : `{e}`")
        time.sleep(60)
    log.info("Boucle de trading arrêtée")


# ── Boucle Telegram (long polling) ───────────────────────────────────────────
def telegram_loop():
    global last_update_id
    log.info("Écoute Telegram démarrée")
    send_message("🤖 Bot en ligne — tape /help pour commencer.")
    while True:
        updates = get_updates(offset=last_update_id + 1)
        for update in updates:
            last_update_id = update["update_id"]
            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if text.startswith("/"):
                cmd = text.split()[0].lower()
                log.info(f"Commande reçue : {cmd} (chat {chat_id})")
                handle_command(cmd, chat_id)
        time.sleep(1)


# ── Point d'entrée ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Démarrage du trading bot")
    telegram_loop()
    
