import os
import time
import threading
import logging
from datetime import datetime, timezone, timedelta
import requests
from strategy import TradingStrategy
from strategy_b import StrategyB

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
PARIS_TZ           = timezone(timedelta(hours=2))
RECAP_START_HOUR   = 7
RECAP_END_HOUR     = 22
MORNING_HOUR       = 7
DIAGNOSTIC_HOURS   = {8, 12, 18}

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
    "▶️ démarrer":    "start",
    "⏸ pause":        "pause",
    "⏹ arrêter":      "stop",
    "📊 statut":       "status",
    "📋 trades":       "positions",
    "🔍 debug":        "debug_prompt",
    "❌ fermer":       "close_prompt",
    "💣 tout fermer":  "closeall_prompt",
    "📈 stats":        "stats",
    "⚡ boost":        "boost",
    "⚙️ aide":         "help",
    "/start":         "start",
    "/stop":          "stop",
    "/pause":         "pause",
    "/status":        "status",
    "/positions":     "positions",
    "/help":          "help",
    "/stats":         "stats",
    "/boost":         "boost",
    "/statsb":        "stats_b",
    "/boostb":        "boost_b",
}

# États conversationnels : chat_id → {"waiting": "debug"|"close", "strategy": "a"|"b"}
waiting_input: dict = {}

# Confirmations : chat_id → {"action": str, "symbol": str|None, "strategy": "a"|"b"}
pending_confirmations: dict = {}

# ── État global ───────────────────────────────────────────────────────────────
bot_running           = False
bot_paused            = False
strategy              = None    # Stratégie A
strategy_b            = None    # Stratégie B
last_update_id        = 0
last_recap_hour       = -1
last_diagnostic_hour  = -1
last_morning_hour     = -1
last_recap_hour_b     = -1
last_diagnostic_hour_b = -1
last_morning_hour_b   = -1
last_reset_date       = ""
force_scan            = False
force_scan_b          = False
_trading_thread       = None
_trading_thread_b     = None

# ── Locks threading ───────────────────────────────────────────────────────────
_diagnostic_lock    = threading.Lock()
_recap_lock         = threading.Lock()
_diagnostic_lock_b  = threading.Lock()
_recap_lock_b       = threading.Lock()


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


# ── Statut combiné A + B ──────────────────────────────────────────────────────
def build_status() -> str:
    if not strategy:
        return "🔴 Bot arrêté."

    state = "⏸ En pause" if bot_paused else "🟢 Actif"
    lines = [f"*{state} — Mode PAPER*\n"]

    # ── Stratégie A ───────────────────────────────────────────────────────────
    stats_a   = strategy.get_stats()
    metrics_a = strategy.get_metrics()
    pos_a     = strategy.get_positions()

    lines.append("━━━ *Stratégie A — Trend Following* ━━━")
    lines.append(
        f"💼 Capital : `{stats_a['capital']:.2f}` USDC | "
        f"G/P jour : `{stats_a['pnl_today']:+.2f}` | "
        f"Total : `{stats_a['pnl']:+.2f}` USDC"
    )
    lines.append(f"🔢 {stats_a['total_trades']} trades (✅ {stats_a['wins']} / ❌ {stats_a['losses']})")

    if stats_a["total_trades"] >= 3:
        pf_str = (f"{metrics_a['profit_factor']:.2f}"
                  if metrics_a["profit_factor"] != float("inf") else "∞")
        lines.append(
            f"📐 Winrate `{metrics_a['winrate']:.1f}%` | PF `{pf_str}` | "
            f"Sharpe `{metrics_a['sharpe']:.2f}` | DD `{metrics_a['max_drawdown']:.2f}` USDC"
        )

    if pos_a:
        lines.append(f"📌 *{len(pos_a)} position(s) :*")
        for p in pos_a:
            secured    = p.get("secured_pnl_usdc", 0.0)
            total_usdc = secured + p["pnl_usdc"]
            size_init  = p.get("size_usdc_initial", p["size_usdc"])
            total_pct  = total_usdc / size_init * 100 if size_init > 0 else 0.0
            gp_emoji   = "📈" if total_pct >= 0 else "📉"
            parts      = []
            if p["tp1_done"]:
                parts.append("TP1✅")
            if p["tp2_done"]:
                parts.append("TP2✅")
            parts.append(f"flottant:`{p['pnl_usdc']:+.2f}`")
            lines.append(
                f"{gp_emoji} `{p['symbol']}` | "
                f"`{total_usdc:+.2f}` USDC ({' + '.join(parts)}) → `{total_pct:+.2f}%`"
            )
    else:
        lines.append("📭 Aucune position A")

    # ── Stratégie B ───────────────────────────────────────────────────────────
    lines.append("")
    if strategy_b:
        stats_b   = strategy_b.get_stats()
        metrics_b = strategy_b.get_metrics()
        pos_b     = strategy_b.get_positions()

        lines.append("━━━ *Stratégie B — Momentum Breakout* ━━━")
        lines.append(
            f"💼 Capital : `{stats_b['capital']:.2f}` USDC | "
            f"G/P jour : `{stats_b['pnl_today']:+.2f}` | "
            f"Total : `{stats_b['pnl']:+.2f}` USDC"
        )
        lines.append(f"🔢 {stats_b['total_trades']} trades (✅ {stats_b['wins']} / ❌ {stats_b['losses']})")

        if stats_b["total_trades"] >= 3:
            pf_str = (f"{metrics_b['profit_factor']:.2f}"
                      if metrics_b["profit_factor"] != float("inf") else "∞")
            lines.append(
                f"📐 Winrate `{metrics_b['winrate']:.1f}%` | PF `{pf_str}` | "
                f"Sharpe `{metrics_b['sharpe']:.2f}` | DD `{metrics_b['max_drawdown']:.2f}` USDC"
            )

        if pos_b:
            lines.append(f"📌 *{len(pos_b)} position(s) :*")
            for p in pos_b:
                secured    = p.get("secured_pnl_usdc", 0.0)
                total_usdc = secured + p["pnl_usdc"]
                size_init  = p.get("size_usdc_initial", p["size_usdc"])
                total_pct  = total_usdc / size_init * 100 if size_init > 0 else 0.0
                gp_emoji   = "📈" if total_pct >= 0 else "📉"
                parts      = []
                if p["tp1_done"]:
                    parts.append("TP1✅")
                if p["tp2_done"]:
                    parts.append("TP2✅")
                parts.append(f"flottant:`{p['pnl_usdc']:+.2f}`")
                lines.append(
                    f"{gp_emoji} `{p['symbol']}` | "
                    f"`{total_usdc:+.2f}` USDC ({' + '.join(parts)}) → `{total_pct:+.2f}%`"
                )
        else:
            lines.append("📭 Aucune position B")
    else:
        lines.append("━━━ *Stratégie B* — non démarrée ━━━")

    return "\n".join(lines)


# ── Stats détaillées A ────────────────────────────────────────────────────────
def build_stats() -> str:
    if not strategy:
        return "🔴 Bot arrêté."
    stats   = strategy.get_stats()
    metrics = strategy.get_metrics()
    state   = "⏸ En pause" if bot_paused else "🟢 Actif"
    lines   = [
        f"📈 *Stats A — Trend Following* — {state}",
        f"💼 Capital : `{stats['capital']:.2f}` USDC",
        f"📊 G/P total : `{stats['pnl']:+.2f}` USDC | Aujourd'hui : `{stats['pnl_today']:+.2f}` USDC",
        f"🔢 {stats['total_trades']} trades (✅ {stats['wins']} / ❌ {stats['losses']})",
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
        lines.append("_Métriques disponibles à partir de 3 trades fermés._")
    if strategy:
        lines.append(f"\n🔄 *Cooldowns A :*\n{strategy.get_cooldowns_status()}")
    return "\n".join(lines)


# ── Stats détaillées B ────────────────────────────────────────────────────────
def build_stats_b() -> str:
    if not strategy_b:
        return "ℹ️ Stratégie B non démarrée."
    stats   = strategy_b.get_stats()
    metrics = strategy_b.get_metrics()
    state   = "⏸ En pause" if bot_paused else "🟢 Actif"
    lines   = [
        f"📈 *Stats B — Momentum Breakout* — {state}",
        f"💼 Capital : `{stats['capital']:.2f}` USDC",
        f"📊 G/P total : `{stats['pnl']:+.2f}` USDC | Aujourd'hui : `{stats['pnl_today']:+.2f}` USDC",
        f"🔢 {stats['total_trades']} trades (✅ {stats['wins']} / ❌ {stats['losses']})",
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
        lines.append("_Métriques disponibles à partir de 3 trades fermés._")
    lines.append(f"\n🔄 *Cooldowns B :*\n{strategy_b.get_cooldowns_status()}")
    return "\n".join(lines)


# ── Récap horaire A ───────────────────────────────────────────────────────────
def build_recap() -> str:
    if not strategy:
        return ""
    stats     = strategy.get_stats()
    positions = strategy.get_positions()
    now       = datetime.now(PARIS_TZ).strftime("%H:%M")
    state     = "⏸ En pause" if bot_paused else "🟢 Actif"
    lines     = [
        f"🕐 *Récap A {now}* — {state}",
        f"💼 `{stats['capital']:.2f}` USDC | G/P jour : `{stats['pnl_today']:+.2f}` | Total : `{stats['pnl']:+.2f}` USDC",
        f"🔢 {stats['total_trades']} trades (✅ {stats['wins']} / ❌ {stats['losses']})",
    ]
    if positions:
        lines.append(f"\n📌 *{len(positions)} position(s) :*")
        for p in positions:
            secured    = p.get("secured_pnl_usdc", 0.0)
            total_usdc = secured + p["pnl_usdc"]
            size_init  = p.get("size_usdc_initial", p["size_usdc"])
            total_pct  = total_usdc / size_init * 100 if size_init > 0 else 0.0
            gp_emoji   = "📈" if total_pct >= 0 else "📉"
            parts      = []
            if p["tp1_done"]:
                parts.append("TP1✅")
            if p["tp2_done"]:
                parts.append("TP2✅")
            parts.append(f"flottant:`{p['pnl_usdc']:+.2f}`")
            lines.append(
                f"{gp_emoji} `{p['symbol']}` | "
                f"`{total_usdc:+.2f}` USDC ({' + '.join(parts)}) → `{total_pct:+.2f}%`"
            )
    return "\n".join(lines)


# ── Récap horaire B ───────────────────────────────────────────────────────────
def build_recap_b() -> str:
    if not strategy_b:
        return ""
    stats     = strategy_b.get_stats()
    positions = strategy_b.get_positions()
    now       = datetime.now(PARIS_TZ).strftime("%H:%M")
    state     = "⏸ En pause" if bot_paused else "🟢 Actif"
    lines     = [
        f"🕐 *Récap B {now}* — {state}",
        f"💼 `{stats['capital']:.2f}` USDC | G/P jour : `{stats['pnl_today']:+.2f}` | Total : `{stats['pnl']:+.2f}` USDC",
        f"🔢 {stats['total_trades']} trades (✅ {stats['wins']} / ❌ {stats['losses']})",
    ]
    if positions:
        lines.append(f"\n📌 *{len(positions)} position(s) B :*")
        for p in positions:
            secured    = p.get("secured_pnl_usdc", 0.0)
            total_usdc = secured + p["pnl_usdc"]
            size_init  = p.get("size_usdc_initial", p["size_usdc"])
            total_pct  = total_usdc / size_init * 100 if size_init > 0 else 0.0
            gp_emoji   = "📈" if total_pct >= 0 else "📉"
            parts      = []
            if p["tp1_done"]:
                parts.append("TP1✅")
            if p["tp2_done"]:
                parts.append("TP2✅")
            parts.append(f"flottant:`{p['pnl_usdc']:+.2f}`")
            lines.append(
                f"{gp_emoji} `{p['symbol']}` | "
                f"`{total_usdc:+.2f}` USDC ({' + '.join(parts)}) → `{total_pct:+.2f}%`"
            )
    return "\n".join(lines)


# ── Trades détaillés A ────────────────────────────────────────────────────────
def build_trades() -> str:
    if not strategy:
        return "ℹ️ Aucune stratégie active."
    positions = strategy.get_positions()
    if not positions:
        return "📭 [A] Aucune position ouverte."
    lines = ["📋 *Positions A ouvertes :*\n"]
    for p in positions:
        secured    = p.get("secured_pnl_usdc", 0.0)
        total_usdc = secured + p["pnl_usdc"]
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
        parts.append(f"flottant:`{p['pnl_usdc']:+.2f}`")
        lines.append(
            f"{gp_emoji} `{p['symbol']}` | Ouvert le {p['opened_at']}\n"
            f"  Investi : `{size_init:.2f}` USDC (restant : `{p['size_usdc']:.2f}` USDC)\n"
            f"  G/P : `{total_usdc:+.2f}` USDC ({' + '.join(parts)}) → `{total_pct:+.2f}%`\n"
            f"  Entrée : `{p['entry']:.6f}` → Actuel : `{p['current']:.6f}`\n"
            f"  TP1 {tp1_tag} `{p['tp1_price']:.6f}` | TP2 {tp2_tag} `{p['tp2_price']:.6f}`\n"
            f"  TS : `{p['ts_price']:.6f}` | 💵 Min si TS : `{p['ts_pnl']:+.2f}` USDC\n"
        )

    # Ajouter les positions B si présentes
    if strategy_b:
        positions_b = strategy_b.get_positions()
        if positions_b:
            lines.append("\n📋 *Positions B ouvertes :*\n")
            for p in positions_b:
                secured    = p.get("secured_pnl_usdc", 0.0)
                total_usdc = secured + p["pnl_usdc"]
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
                parts.append(f"flottant:`{p['pnl_usdc']:+.2f}`")
                lines.append(
                    f"{gp_emoji} `{p['symbol']}` | Ouvert le {p['opened_at']}\n"
                    f"  Investi : `{size_init:.2f}` USDC (restant : `{p['size_usdc']:.2f}` USDC)\n"
                    f"  G/P : `{total_usdc:+.2f}` USDC ({' + '.join(parts)}) → `{total_pct:+.2f}%`\n"
                    f"  Entrée : `{p['entry']:.6f}` → Actuel : `{p['current']:.6f}`\n"
                    f"  TP1 {tp1_tag} `{p['tp1_price']:.6f}` (+6%) | "
                    f"TP2 {tp2_tag} `{p['tp2_price']:.6f}` (+12%)\n"
                    f"  TS : `{p['ts_price']:.6f}` | 💵 Min si TS : `{p['ts_pnl']:+.2f}` USDC\n"
                )
    return "\n".join(lines)


# ── Guards temporels ──────────────────────────────────────────────────────────
def should_send_recap() -> bool:
    global last_recap_hour
    now = datetime.now(PARIS_TZ)
    h, m = now.hour, now.minute
    with _recap_lock:
        if RECAP_START_HOUR <= h < RECAP_END_HOUR and h != last_recap_hour and m <= 3:
            last_recap_hour = h
            return True
    return False


def should_send_recap_b() -> bool:
    global last_recap_hour_b
    now = datetime.now(PARIS_TZ)
    h, m = now.hour, now.minute
    with _recap_lock_b:
        if RECAP_START_HOUR <= h < RECAP_END_HOUR and h != last_recap_hour_b and m <= 3:
            last_recap_hour_b = h
            return True
    return False


def should_send_diagnostic() -> bool:
    global last_diagnostic_hour
    now = datetime.now(PARIS_TZ)
    h, m = now.hour, now.minute
    with _diagnostic_lock:
        if h in DIAGNOSTIC_HOURS and h != last_diagnostic_hour and m <= 3:
            last_diagnostic_hour = h
            return True
    return False


def should_send_diagnostic_b() -> bool:
    global last_diagnostic_hour_b
    now = datetime.now(PARIS_TZ)
    h, m = now.hour, now.minute
    with _diagnostic_lock_b:
        if h in DIAGNOSTIC_HOURS and h != last_diagnostic_hour_b and m <= 3:
            last_diagnostic_hour_b = h
            return True
    return False


# ── Reset minuit ──────────────────────────────────────────────────────────────
def check_midnight_reset():
    global last_reset_date, last_morning_hour, last_recap_hour, last_diagnostic_hour
    global last_morning_hour_b, last_recap_hour_b, last_diagnostic_hour_b
    today = datetime.now(PARIS_TZ).date().isoformat()
    if today != last_reset_date:
        last_reset_date        = today
        last_morning_hour      = -1
        last_recap_hour        = -1
        last_diagnostic_hour   = -1
        last_morning_hour_b    = -1
        last_recap_hour_b      = -1
        last_diagnostic_hour_b = -1
        if strategy:
            strategy.reset_daily_pnl()
        if strategy_b:
            strategy_b.reset_daily_pnl()
        log.info("Reset journalier — tous les compteurs réinitialisés")


# ── Clôture manuelle avec confirmation ────────────────────────────────────────
def handle_close(text: str, chat_id: str, strat: str = "a"):
    strat_obj = strategy if strat == "a" else strategy_b
    prefix    = "" if strat == "a" else "b"
    label     = "A" if strat == "a" else "B"

    if not strat_obj:
        send_message(f"ℹ️ Stratégie {label} non active.", chat_id)
        return

    cmd = text.strip().upper()
    if cmd in ["/CLOSEALL", "/CLOSEBALL"]:
        positions = strat_obj.get_positions()
        if not positions:
            send_message(f"📭 [{ label}] Aucune position ouverte.", chat_id)
            return
        symbols = ", ".join([f"`{p['symbol']}`" for p in positions])
        pending_confirmations[chat_id] = {
            "action": "closeall", "symbol": None, "strategy": strat
        }
        send_message(
            f"⚠️ *Confirmation requise*\n\n"
            f"Fermer *toutes les positions {label}* : {symbols} ?\n\n"
            f"Réponds *OUI* pour confirmer ou *NON* pour annuler.",
            chat_id,
        )
        return

    parts = text.strip().split()
    if len(parts) < 2:
        send_message(f"Usage : `/close{'b' if strat == 'b' else ''} SYMBOL`", chat_id)
        return

    symbol = parts[1].upper()
    if "/" not in symbol:
        symbol = symbol + "/USDC"

    if symbol not in strat_obj.positions:
        send_message(f"❓ [{label}] Aucune position ouverte sur `{symbol}`.", chat_id)
        return

    pos = strat_obj.positions[symbol]
    try:
        price = float(strat_obj.exchange.fetch_ticker(symbol)["last"])
        pnl_u, pnl_p = strat_obj._calc_pnl(pos["size_usdc"], pos["entry"], price)
        prix_str = (f"Prix actuel : `{price:.6f}` | "
                    f"G/P estimé : `{pnl_u:+.2f}` USDC (`{pnl_p:+.2f}%`)")
    except Exception:
        prix_str = "Prix actuel indisponible"

    pending_confirmations[chat_id] = {
        "action": "close", "symbol": symbol, "strategy": strat
    }
    send_message(
        f"⚠️ *Confirmation requise*\n\n[{label}] Fermer `{symbol}` ?\n{prix_str}\n\n"
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
        strat    = pending.get("strategy", "a")
        strat_obj = strategy if strat == "a" else strategy_b
        if not strat_obj:
            send_message("ℹ️ Stratégie non active.", chat_id)
            return
        if pending["action"] == "close":
            send_message(strat_obj.close_position_manual(pending["symbol"]), chat_id)
        elif pending["action"] == "closeall":
            msgs = strat_obj.close_all_manual()
            for msg in msgs:
                send_message(msg, chat_id)
            if not msgs:
                send_message("📭 Aucune position à fermer.", chat_id)
        elif pending["action"] == "buy":
            msg = strat_obj.open_position_manual(pending["symbol"])
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
    strat     = state.get("strategy", "a")
    strat_obj = strategy if strat == "a" else strategy_b

    if state["waiting"] == "debug":
        if not strat_obj:
            send_message("ℹ️ Stratégie non active.", chat_id)
            return
        send_message(strat_obj.debug_position(symbol), chat_id)
    elif state["waiting"] == "close":
        cmd = "/closeb" if strat == "b" else "/close"
        handle_close(f"{cmd} {symbol}", chat_id, strat)


# ── Achat forcé (/buy / /buyb) ────────────────────────────────────────────────
def handle_buy(text: str, chat_id: str, strat: str = "a"):
    strat_obj = strategy if strat == "a" else strategy_b
    label     = "A" if strat == "a" else "B"
    if not strat_obj:
        send_message(f"ℹ️ Stratégie {label} non active.", chat_id)
        return
    parts = text.strip().split()
    if len(parts) < 2:
        cmd = "/buyb" if strat == "b" else "/buy"
        send_message(f"Usage : `{cmd} SYMBOL`\n⚠️ Tous les filtres seront ignorés.", chat_id)
        return
    symbol = parts[1].upper()
    if "/" not in symbol:
        symbol = symbol + "/USDC"
    pending_confirmations[chat_id] = {
        "action": "buy", "symbol": symbol, "strategy": strat
    }
    send_message(
        f"⚠️ *Confirmation achat forcé [{label}]*\n\n"
        f"Entrée manuelle sur `{symbol}` ?\n"
        f"_Tous les filtres seront ignorés. SL/TS/TP normaux appliqués._\n\n"
        f"Réponds *OUI* pour confirmer ou *NON* pour annuler.",
        chat_id,
    )


# ── Skip (/skip / /skipb) ─────────────────────────────────────────────────────
def handle_skip(text: str, chat_id: str, strat: str = "a"):
    strat_obj = strategy if strat == "a" else strategy_b
    label     = "A" if strat == "a" else "B"
    if not strat_obj:
        send_message(f"ℹ️ Stratégie {label} non active.", chat_id)
        return
    parts = text.strip().split()
    if len(parts) < 2:
        cmd = "/skipb" if strat == "b" else "/skip"
        send_message(f"Usage : `{cmd} SYMBOL`", chat_id)
        return
    symbol = parts[1].upper()
    if "/" not in symbol:
        symbol = symbol + "/USDC"
    send_message(strat_obj.skip_symbol(symbol), chat_id)


# ── Actions boutons / commandes ───────────────────────────────────────────────
def handle_action(action: str, chat_id: str, raw_text: str = ""):
    global bot_running, bot_paused, strategy, strategy_b
    global _trading_thread, _trading_thread_b

    # ── Debug ─────────────────────────────────────────────────────────────────
    if action in ("debug", "debug_b"):
        strat     = "b" if action == "debug_b" else "a"
        strat_obj = strategy if strat == "a" else strategy_b
        if not strat_obj:
            send_message("ℹ️ Stratégie non active.", chat_id)
            return
        parts = raw_text.strip().split()
        if len(parts) < 2:
            send_message(f"Usage : `/debug{'b' if strat == 'b' else ''} SYMBOL`", chat_id)
            return
        symbol = parts[1].upper()
        if "/" not in symbol:
            symbol = symbol + "/USDC"
        send_message(strat_obj.debug_position(symbol), chat_id)
        return

    if action == "debug_prompt":
        if not strategy:
            send_message("ℹ️ Aucune stratégie active.", chat_id)
            return
        positions = strategy.get_positions()
        if not positions:
            send_message("📭 Aucune position A à diagnostiquer.", chat_id)
            return
        symbols = "\n".join([f"• `{p['symbol']}`" for p in positions])
        waiting_input[chat_id] = {"waiting": "debug", "strategy": "a"}
        send_message(
            f"🔍 *Debug A — quelle paire ?*\n\nPositions :\n{symbols}\n\n"
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
            send_message("📭 Aucune position A à fermer.", chat_id)
            return
        symbols = "\n".join([
            f"• `{p['symbol']}` | G/P : `{p['pnl_pct']:+.2f}%`"
            for p in positions
        ])
        waiting_input[chat_id] = {"waiting": "close", "strategy": "a"}
        send_message(
            f"❌ *Fermer A — quelle paire ?*\n\nPositions :\n{symbols}\n\n"
            f"Envoie le symbole (ex: `ETH` ou `ETH/USDC`)",
            chat_id,
        )
        return

    if action == "closeall_prompt":
        handle_close("/closeall", chat_id, "a")
        return

    # ── Start ─────────────────────────────────────────────────────────────────
    if action == "start":
        if bot_running and not bot_paused:
            send_message("⚠️ Le bot tourne déjà.", chat_id)
            return
        if bot_paused:
            bot_paused = False
            send_message("▶️ *Bot repris.*", chat_id)
            return
        # Vérification thread A
        if _trading_thread and _trading_thread.is_alive():
            _trading_thread.join(timeout=5)
            if _trading_thread.is_alive():
                send_message("⚠️ Thread A précédent encore actif. Attends 5s.", chat_id)
                return
        # Vérification thread B
        if _trading_thread_b and _trading_thread_b.is_alive():
            _trading_thread_b.join(timeout=5)

        bot_running = True
        bot_paused  = False
        strategy    = TradingStrategy(binance_key=BINANCE_KEY, binance_secret=BINANCE_SECRET)
        strategy_b  = StrategyB(binance_key=BINANCE_KEY, binance_secret=BINANCE_SECRET)
        nb_a        = len(strategy.positions)
        nb_b        = len(strategy_b.positions)

        _trading_thread   = threading.Thread(target=trading_loop,   daemon=True)
        _trading_thread_b = threading.Thread(target=trading_loop_b, daemon=True)
        _trading_thread.start()
        _trading_thread_b.start()

        recap_a = f"\n📂 {nb_a} position(s) A rechargée(s)." if nb_a > 0 else ""
        recap_b = f"\n📂 {nb_b} position(s) B rechargée(s)." if nb_b > 0 else ""
        send_message(
            f"✅ *Bot démarré* — Mode PAPER — 2 stratégies actives\n\n"
            f"*Stratégie A — Trend Following*\n"
            f"💰 100 USDC | 5 positions max\n"
            f"📐 SL ×2.5 ATR | TS adaptatif ×2.0–×3.5\n"
            f"🎯 TP1 +3×ATR (25%) | TP2 +5×ATR (25%) | Reste 50% TS\n"
            f"🔍 Liquidité 10M | EMA | RSI 45–85 | BTC bear mode{recap_a}\n\n"
            f"*Stratégie B — Momentum Breakout*\n"
            f"💰 100 USDC | 5 positions max\n"
            f"📐 SL ×1.5 ATR | TS ×1.5 ATR (serré)\n"
            f"🎯 TP1 +6% (40%) | TP2 +12% (40%) | Reste 20% TS\n"
            f"🔍 Liquidité 1M | Volume ×3 | RSI 60–75 | Momentum +5%/1h{recap_b}",
            chat_id,
        )
        return

    # ── Pause ─────────────────────────────────────────────────────────────────
    elif action == "pause":
        if not bot_running:
            send_message("ℹ️ Le bot n'est pas démarré.", chat_id)
            return
        if bot_paused:
            send_message("ℹ️ Déjà en pause.", chat_id)
            return
        bot_paused = True
        send_message("⏸ *Pause* — stops et TP toujours actifs (A et B).", chat_id)

    # ── Stop ──────────────────────────────────────────────────────────────────
    elif action == "stop":
        if not bot_running:
            send_message("ℹ️ Le bot n'est pas en cours d'exécution.", chat_id)
            return
        bot_running = False
        bot_paused  = False
        if _trading_thread and _trading_thread.is_alive():
            _trading_thread.join(timeout=10)
        if _trading_thread_b and _trading_thread_b.is_alive():
            _trading_thread_b.join(timeout=10)
        send_message("⏹ *Bot arrêté.* Positions A et B sauvegardées.", chat_id)

    elif action == "status":
        send_message(build_status(), chat_id)

    elif action == "positions":
        send_message(build_trades(), chat_id)

    elif action == "stats":
        send_message(build_stats(), chat_id)

    elif action == "stats_b":
        send_message(build_stats_b(), chat_id)

    # ── Boost A ───────────────────────────────────────────────────────────────
    elif action == "boost":
        global force_scan
        if not bot_running:
            send_message("ℹ️ Le bot n'est pas démarré.", chat_id)
            return
        force_scan = True
        send_message("⚡ *[A] Scan forcé déclenché.*", chat_id)
        if strategy:
            summary = strategy.scan_market_summary(force=True)
            if summary:
                send_message(summary, chat_id)

    # ── Boost B ───────────────────────────────────────────────────────────────
    elif action == "boost_b":
        global force_scan_b
        if not bot_running:
            send_message("ℹ️ Le bot n'est pas démarré.", chat_id)
            return
        force_scan_b = True
        send_message("⚡ *[B] Scan forcé déclenché.*", chat_id)
        if strategy_b:
            summary = strategy_b.scan_market_summary(force=True)
            if summary:
                send_message(summary, chat_id)

    elif action == "help":
        send_message(
            "⚙️ *Aide — Bot 2 Stratégies*\n\n"
            "▶️ Démarrer — Lance A et B simultanément\n"
            "⏸ Pause — Suspend les deux _(stops et TP actifs)_\n"
            "⏹ Arrêter — Arrêt complet\n"
            "📊 Statut — Vue combinée A + B\n"
            "📋 Trades — Toutes les positions ouvertes\n"
            "📈 Stats — Statistiques détaillées A\n"
            "⚡ Boost — Scan immédiat A\n\n"
            "*Stratégie A — Trend Following :*\n"
            "`/debug SYMBOL` — Diagnostiquer une position A\n"
            "`/close SYMBOL` — Fermer une position A\n"
            "`/closeall` — Fermer toutes les positions A\n"
            "`/buy SYMBOL` — Entrée forcée A ⚠️\n"
            "`/skip SYMBOL` — Blacklister 24h (A)\n"
            "`/stats` — Stats A détaillées\n"
            "`/boost` — Scan immédiat A\n\n"
            "*Stratégie B — Momentum Breakout :*\n"
            "`/debugb SYMBOL` — Diagnostiquer une position B\n"
            "`/closeb SYMBOL` — Fermer une position B\n"
            "`/closeallb` — Fermer toutes les positions B\n"
            "`/buyb SYMBOL` — Entrée forcée B ⚠️\n"
            "`/skipb SYMBOL` — Blacklister 24h (B)\n"
            "`/statsb` — Stats B détaillées\n"
            "`/boostb` — Scan immédiat B\n\n"
            "_A : EMA20>50 | Volume ×2 | RSI 45–85 | 10M liquidité_\n"
            "_B : Momentum +5%/1h | Volume ×3 spike | RSI 60–75 | 1M liquidité_",
            chat_id,
        )


# ── Boucle de trading A ───────────────────────────────────────────────────────
def trading_loop():
    global force_scan, last_morning_hour
    log.info("[A] Boucle démarrée")
    while bot_running:
        try:
            check_midnight_reset()

            now_paris = datetime.now(PARIS_TZ)
            if now_paris.hour == MORNING_HOUR and now_paris.hour != last_morning_hour and strategy:
                last_morning_hour = now_paris.hour
                for msg in strategy.morning_analysis():
                    send_message(msg)

            if force_scan:
                force_scan = False
                log.info("[A] Scan forcé via /boost")

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
                    msg = strategy.scan_market_summary()
                    if msg:
                        send_message(msg)

        except Exception as e:
            log.error(f"[A] Erreur boucle : {e}")
            send_message(f"⚠️ [A] Erreur : `{e}`")

        for _ in range(60):
            if force_scan or not bot_running:
                break
            time.sleep(1)

    log.info("[A] Boucle arrêtée")


# ── Boucle de trading B ───────────────────────────────────────────────────────
def trading_loop_b():
    global force_scan_b, last_morning_hour_b
    log.info("[B] Boucle démarrée")
    # Décalage de 30s pour éviter que A et B appellent l'API exactement en même temps
    time.sleep(30)
    while bot_running:
        try:
            now_paris = datetime.now(PARIS_TZ)
            if now_paris.hour == MORNING_HOUR and now_paris.hour != last_morning_hour_b and strategy_b:
                last_morning_hour_b = now_paris.hour
                for msg in strategy_b.morning_analysis():
                    send_message(msg)

            if force_scan_b:
                force_scan_b = False
                log.info("[B] Scan forcé via /boostb")

            alerts = strategy_b.scan()
            for alert in alerts:
                send_message(alert)

            positions_b = strategy_b.get_positions() if strategy_b else []
            if positions_b:
                if should_send_recap_b():
                    recap = build_recap_b()
                    if recap:
                        send_message(recap)
            else:
                if should_send_diagnostic_b() and strategy_b:
                    msg = strategy_b.scan_market_summary()
                    if msg:
                        send_message(msg)

        except Exception as e:
            log.error(f"[B] Erreur boucle : {e}")
            send_message(f"⚠️ [B] Erreur : `{e}`")

        for _ in range(60):
            if force_scan_b or not bot_running:
                break
            time.sleep(1)

    log.info("[B] Boucle arrêtée")


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

            tl = text.lower()

            # ── Commandes B ───────────────────────────────────────────────────
            if tl.startswith("/debugb"):
                handle_action("debug_b", chat_id, raw_text=text)
                continue
            if tl.startswith("/closeball"):
                handle_close("/closeball", chat_id, "b")
                continue
            if tl.startswith("/closeb"):
                handle_close(text, chat_id, "b")
                continue
            if tl.startswith("/buyb"):
                handle_buy(text, chat_id, "b")
                continue
            if tl.startswith("/skipb"):
                handle_skip(text, chat_id, "b")
                continue

            # ── Commandes A ───────────────────────────────────────────────────
            if tl.startswith("/debug"):
                handle_action("debug", chat_id, raw_text=text)
                continue
            if tl.startswith("/closeall"):
                handle_close("/closeall", chat_id, "a")
                continue
            if tl.startswith("/close"):
                handle_close(text, chat_id, "a")
                continue
            if tl.startswith("/buy"):
                handle_buy(text, chat_id, "a")
                continue
            if tl.startswith("/skip"):
                handle_skip(text, chat_id, "a")
                continue

            if chat_id in pending_confirmations:
                handle_confirmation(text, chat_id)
                continue

            if chat_id in waiting_input:
                handle_waiting_input(text, chat_id)
                continue

            action = BUTTON_MAP.get(tl)
            if action:
                handle_action(action, chat_id)
            else:
                send_message(
                    f"❓ Non reconnu : `{text}`\nUtilise les boutons ou ⚙️ Aide.", chat_id
                )

        time.sleep(1)


# ── Point d'entrée ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Démarrage")
    telegram_loop()
