"""
AUTOBOT v2 — Momentum pur sur paires /USDC
Stratégie : momentum 1h + trailing stop optimisé
5 positions x 20 USDC | SL 2% | Trailing 1.5%
"""

import os
import time
import threading
import logging
from datetime import datetime, date
import requests
import ccxt
import pandas as pd
from flask import Flask

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN  = "7869233416:AAEWOKJDysyXSFhSTLr4lwipkGL_DXYIRAI"
TELEGRAM_CHAT   = "1922889384"

BINANCE_KEY     = os.environ.get("BINANCE_KEY", "")
BINANCE_SECRET  = os.environ.get("BINANCE_SECRET", "")

PAPER_MODE      = True
CAPITAL_USDC    = 100.0

# ─────────────────────────────────────────────────────────────────────────────
# PARAMÈTRES STRATÉGIE (modifiables via /ajuster)
# ─────────────────────────────────────────────────────────────────────────────

MAX_POSITIONS       = 5
POSITION_SIZE_USDC  = 20.0
MOMENTUM_THRESHOLD  = 0.03
VOLUME_ACCEL        = 1.5
VOLUME_MIN_USDC     = 3_000_000
TIMEFRAME_MOMENTUM  = "1h"
SCAN_INTERVAL       = 60
DAILY_DRAWDOWN_LIMIT = 0.10   # 10% drawdown journalier max

settings = {
    "trailing_stop": 0.015,   # modifiable via /ajuster
    "stop_loss":     0.02,    # modifiable via /ajuster
}

USDC_PAIRS = [
    "BTC/USDC", "ETH/USDC", "BNB/USDC", "SOL/USDC", "XRP/USDC",
    "ADA/USDC", "AVAX/USDC", "DOGE/USDC", "DOT/USDC", "LINK/USDC",
    "MATIC/USDC", "UNI/USDC", "ATOM/USDC", "LTC/USDC", "ETC/USDC",
    "APT/USDC", "ARB/USDC", "OP/USDC", "INJ/USDC", "SUI/USDC",
    "TIA/USDC", "WLD/USDC", "PEPE/USDC", "SHIB/USDC", "WIF/USDC",
    "BONK/USDC", "JUP/USDC", "FIL/USDC", "SEI/USDC", "FLOKI/USDC",
]

# ─────────────────────────────────────────────────────────────────────────────
# ÉTAT GLOBAL
# ─────────────────────────────────────────────────────────────────────────────

state = {
    "status":            "stopped",
    "capital":           CAPITAL_USDC,
    "capital_initial":   CAPITAL_USDC,
    "capital_day_start": CAPITAL_USDC,
    "pnl":               0.0,
    "positions":         {},
    "trades":            [],
    "trades_today":      [],
    "paper_mode":        PAPER_MODE,
    "last_scan":         "—",
    "day":               date.today(),
    "awaiting_ajuster":  False,
    "last_opened":       None,    # last opened position symbol
}

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────────────────────────────────────

TG_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def tg_send(msg: str):
    try:
        requests.post(
            f"{TG_URL}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def tg_send_inline(msg: str, symbol: str = None):
    """Envoie un message avec boutons inline Statut + Fermer position."""
    close_cb = f"close_{symbol}" if symbol else "close_last"
    keyboard = {
        "inline_keyboard": [[
            {"text": "📊 Statut",          "callback_data": "status"},
            {"text": "❌ Fermer position", "callback_data": close_cb},
        ]]
    }
    try:
        requests.post(
            f"{TG_URL}/sendMessage",
            json={
                "chat_id":      TELEGRAM_CHAT,
                "text":         msg,
                "parse_mode":   "Markdown",
                "reply_markup": keyboard,
            },
            timeout=10
        )
    except Exception as e:
        logging.error(f"Telegram inline error: {e}")

def tg_answer_callback(callback_id: str, text: str = ""):
    try:
        requests.post(
            f"{TG_URL}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text},
            timeout=10
        )
    except Exception as e:
        logging.error(f"Telegram callback answer error: {e}")

def tg_send_menu():
    keyboard = {
        "keyboard": [
            [{"text": "▶️ Démarrer"}, {"text": "⏸ Pause"}],
            [{"text": "⏹ Arrêter"},  {"text": "📊 Statut"}],
            [{"text": "📋 Trades"},   {"text": "⚙️ Aide"}],
        ],
        "resize_keyboard": True,
        "persistent": True
    }
    try:
        requests.post(
            f"{TG_URL}/sendMessage",
            json={
                "chat_id":      TELEGRAM_CHAT,
                "text":         "🤖 *AUTOBOT v2* — Momentum USDC\nChoisis une action :",
                "parse_mode":   "Markdown",
                "reply_markup": keyboard,
            },
            timeout=10
        )
    except Exception as e:
        logging.error(f"Telegram menu error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# POLLING TELEGRAM (messages + callbacks)
# ─────────────────────────────────────────────────────────────────────────────

def tg_poll():
    offset = 0
    while True:
        try:
            r = requests.get(
                f"{TG_URL}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35
            )
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1

                # Callback query (boutons inline)
                if "callback_query" in update:
                    cq      = update["callback_query"]
                    cb_id   = cq["id"]
                    cb_data = cq.get("data", "")
                    chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
                    if chat_id == TELEGRAM_CHAT:
                        handle_callback(cb_id, cb_data)
                    continue

                # Message texte
                msg     = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text    = msg.get("text", "").strip()
                if chat_id == TELEGRAM_CHAT:
                    handle_command(text)

        except Exception as e:
            logging.error(f"Polling error: {e}")
            time.sleep(5)

def handle_callback(cb_id: str, data: str):
    """Gère les appuis sur les boutons inline."""
    tg_answer_callback(cb_id)

    if data == "status":
        handle_command("/status")

    elif data.startswith("close_"):
        symbol = data[len("close_"):]
        if symbol == "last":
            symbol = state.get("last_opened")
        if symbol and symbol in state["positions"]:
            # Fermeture manuelle sans exchange (paper mode)
            pos = state["positions"][symbol]
            price = pos["current"]
            pnl_t = pos["size_usdc"] * (price - pos["entry"]) / pos["entry"]
            state["pnl"]     += pnl_t
            state["capital"] += pos["size_usdc"] + pnl_t
            trade = {
                "pair":   symbol,
                "entry":  pos["entry"],
                "exit":   price,
                "pnl":    round(pnl_t, 4),
                "reason": "MANUEL",
            }
            state["trades"].append(trade)
            state["trades_today"].append(trade)
            del state["positions"][symbol]
            sign = "+" if pnl_t >= 0 else ""
            tg_send(
                f"🔴 *Fermeture manuelle* `{symbol}`\n"
                f"P&L : `{sign}{pnl_t:.2f} USDC`\n"
                f"Capital : `{state['capital']:.2f} USDC`"
            )
        else:
            tg_send("ℹ️ Aucune position à fermer.")

# ─────────────────────────────────────────────────────────────────────────────
# COMMANDES TELEGRAM
# ─────────────────────────────────────────────────────────────────────────────

def handle_command(text: str):
    global settings

    # Mode ajuster — attend deux valeurs
    if state["awaiting_ajuster"]:
        try:
            parts = text.strip().split()
            if len(parts) == 2:
                new_trail = float(parts[0]) / 100
                new_sl    = float(parts[1]) / 100
                if 0.001 <= new_trail <= 0.20 and 0.001 <= new_sl <= 0.20:
                    settings["trailing_stop"] = new_trail
                    settings["stop_loss"]     = new_sl
                    state["awaiting_ajuster"] = False
                    tg_send(
                        f"✅ *Paramètres mis à jour*\n"
                        f"Trailing stop : `{new_trail*100:.2f}%`\n"
                        f"Stop loss     : `{new_sl*100:.2f}%`\n\n"
                        f"_Les nouvelles positions utiliseront ces valeurs._"
                    )
                else:
                    tg_send("⚠️ Valeurs hors limites (0.1% – 20%). Réessaie.")
            else:
                tg_send("⚠️ Format invalide. Envoie deux nombres séparés d'un espace.\nEx : `1.5 2.0`")
        except ValueError:
            tg_send("⚠️ Valeurs non reconnues. Ex : `1.5 2.0`")
        return

    t = text.lower()

    if t in ["/start", "▶️ démarrer"]:
        if state["status"] != "running":
            state["status"] = "running"
            threading.Thread(target=bot_loop, daemon=True).start()
            mode = "🟡 PAPER" if state["paper_mode"] else "🔴 RÉEL"
            tg_send(
                f"✅ *Bot démarré*\n"
                f"Mode : {mode}\n"
                f"Capital : `{state['capital']:.2f} USDC`\n"
                f"Trailing : `{settings['trailing_stop']*100:.2f}%` | "
                f"SL : `{settings['stop_loss']*100:.2f}%`\n"
                f"Positions : 5 x 20 USDC"
            )
            tg_send_menu()
        else:
            tg_send("⚠️ Bot déjà actif.")

    elif t in ["/pause", "⏸ pause"]:
        if state["status"] == "running":
            state["status"] = "paused"
            tg_send("⏸ *Pause.* Positions maintenues.")
        else:
            tg_send("ℹ️ Bot non actif.")

    elif t in ["/stop", "⏹ arrêter"]:
        state["status"] = "stopped"
        tg_send("⏹ *Bot arrêté.*")

    elif t in ["/status", "📊 statut"]:
        pnl  = state["pnl"]
        cap  = state["capital"]
        sign = "+" if pnl >= 0 else ""
        pct  = pnl / state["capital_initial"] * 100

        pos_lines = ""
        for sym, p in state["positions"].items():
            cur_pnl = p["size_usdc"] * (p["current"] - p["entry"]) / p["entry"]
            sp = "+" if cur_pnl >= 0 else ""
            pos_lines += f"\n  • `{sym}` → {sp}{cur_pnl:.2f} USDC"
        if not pos_lines:
            pos_lines = "\n  Aucune"

        wins  = len([tr for tr in state["trades"] if tr["pnl"] > 0])
        total = len(state["trades"])
        wr    = round(wins / total * 100) if total > 0 else 0

        dd_pct = (state["capital_day_start"] - cap) / state["capital_day_start"] * 100
        tg_send(
            f"📊 *Statut AUTOBOT v2*\n"
            f"─────────────────\n"
            f"Statut : `{state['status'].upper()}`\n"
            f"Capital : `{cap:.2f} USDC`\n"
            f"P&L : `{sign}{pnl:.2f} USDC ({sign}{pct:.1f}%)`\n"
            f"DD jour : `{dd_pct:.1f}%` / 10% max\n"
            f"Positions : `{len(state['positions'])}/{MAX_POSITIONS}`{pos_lines}\n"
            f"Win rate : `{wr}%` sur {total} trades\n"
            f"Trail : `{settings['trailing_stop']*100:.2f}%` | "
            f"SL : `{settings['stop_loss']*100:.2f}%`\n"
            f"Dernier scan : `{state['last_scan']}`"
        )

    elif t in ["/trades", "📋 trades"]:
        trades = state["trades"]
        if not trades:
            tg_send("📋 Aucun trade pour l'instant.")
            return
        lines = ""
        wins = 0
        for tr in trades[-10:]:
            sign  = "+" if tr["pnl"] >= 0 else ""
            emoji = "✅" if tr["pnl"] >= 0 else "❌"
            lines += f"{emoji} `{tr['pair']}` {sign}{tr['pnl']:.2f} USDC\n"
            if tr["pnl"] > 0:
                wins += 1
        wr = round(wins / len(trades) * 100) if trades else 0
        tg_send(
            f"📋 *Derniers trades*\n─────────────────\n{lines}\n"
            f"Win rate : `{wr}%` | P&L cumulé : `{state['pnl']:+.2f} USDC`"
        )

    elif t in ["/ajuster"]:
        state["awaiting_ajuster"] = True
        tg_send(
            f"⚙️ *Ajuster les paramètres*\n"
            f"─────────────────\n"
            f"Valeurs actuelles :\n"
            f"• Trailing stop : `{settings['trailing_stop']*100:.2f}%`\n"
            f"• Stop loss     : `{settings['stop_loss']*100:.2f}%`\n\n"
            f"Envoie les nouvelles valeurs en pourcentage, séparées d'un espace.\n"
            f"Ex : `1.5 2.0` → trailing 1.5%, SL 2.0%"
        )

    elif t in ["/aide", "⚙️ aide", "/help"]:
        tg_send(
            "⚙️ *AUTOBOT v2 — Aide*\n"
            "─────────────────\n"
            "▶️ Démarrer | ⏸ Pause | ⏹ Arrêter\n"
            "📊 Statut | 📋 Trades | /ajuster\n\n"
            "*Stratégie :*\n"
            "• Momentum 1h > 3% + volume x1.5\n"
            "• 5 positions x 20 USDC\n"
            f"• Trailing stop `{settings['trailing_stop']*100:.2f}%` | "
            f"Stop loss `{settings['stop_loss']*100:.2f}%`\n"
            "• DD journalier max 10% → pause auto\n\n"
            "⚠️ PAPER MODE — aucun ordre réel."
        )
    else:
        tg_send("❓ Commande inconnue. Tape `⚙️ Aide`.")

# ─────────
