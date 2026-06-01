"""
Couche d'ALERTE independante. Le SEUL endroit qui parle a Telegram.
Un unique formateur consomme n'importe quel Signal -> message coherent.
Le prefixe vient du Signal.tag : ajouter un moteur = 0 ligne touchee ici.

Taxonomie :
  - evenementiel : entree / sortie (avec multiple de R) ; changement de regime
  - programme    : recap consolide 2x/jour (avec scan marche)
  - a la demande : statut / positions / perf
  - systeme      : demarrage (apres purge de la file), info, erreur (anti-spam)

Sans token Telegram -> tout part dans la console (logs Railway).
"""
import json
import time
import urllib.request
import urllib.parse

import config as C
from base_strategy import Signal, SignalType

# --- clavier (UI) -----------------------------------------------------------
KEYBOARD = {
    "keyboard": [
        ["▶️ Démarrer", "⏸ Pause"],
        ["📊 Statut", "📋 Positions"],
        ["📈 Perf", "🔄 Régime"],
        ["❌ Fermer", "💣 Tout fermer"],
        ["⚡ Boost", "⚙️ Aide"],
    ],
    "resize_keyboard": True,
    "persistent": True,
}

# label/commande (minuscule) -> action interne
BUTTON_MAP = {
    "▶️ démarrer": "start", "⏸ pause": "pause",
    "📊 statut": "status", "📋 positions": "positions",
    "📈 perf": "perf", "🔄 régime": "regime",
    "❌ fermer": "close", "💣 tout fermer": "closeall",
    "⚡ boost": "boost", "⚙️ aide": "help",
    "/start": "start", "/pause": "pause", "/status": "status",
    "/positions": "positions", "/perf": "perf", "/regime": "regime",
    "/closeall": "closeall", "/boost": "boost", "/help": "help",
}

_EMOJI = {SignalType.ENTRY: "🟢", SignalType.EXIT: "✅", SignalType.STOP: "🔴"}


class AlertManager:
    def __init__(self):
        self.token = C.TELEGRAM_TOKEN
        self.chat_id = C.TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)
        self._last_err = (None, 0.0)     # anti-spam erreurs

    # --- demarrage : purge la file AVANT d'annoncer (corrige les doublons) ---
    def flush_pending_updates(self):
        if not self.enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/getUpdates?offset=-1"
            with urllib.request.urlopen(url, timeout=10) as r:
                results = json.loads(r.read()).get("result", [])
            if results:
                last = results[-1]["update_id"]
                urllib.request.urlopen(
                    f"https://api.telegram.org/bot{self.token}/getUpdates?offset={last + 1}",
                    timeout=10)
        except Exception as e:
            print(f"[alerts] flush echoue: {e}")

    def startup(self, labels, n_pairs):
        self.flush_pending_updates()
        strat = ", ".join(labels) or "aucune"
        self._raw(f"🤖 Bot en ligne · PAPER\nStratégies : {strat} · {n_pairs} paires · file purgée",
                  keyboard=True)

    # --- evenementiel : entree / sortie -----------------------------------
    def emit(self, sig: Signal):
        emoji = _EMOJI.get(sig.type, "")
        if sig.type == SignalType.ENTRY:
            invest = sig.price * sig.size
            risk = (sig.price - sig.stop) * sig.size
            lines = [
                f"{emoji} ENTRÉE · {sig.tag} · {sig.symbol}",
                f"Prix {sig.price:.4f} · Taille {invest:.2f} USDC",
                f"Stop {sig.stop:.4f}  (risque {risk:.2f} USDC = 1R)",
                f"Motif : {sig.reason}",
            ]
        else:
            head = "SORTIE" if sig.type == SignalType.EXIT else "STOP"
            r = f" · {sig.r_multiple:+.1f}R" if sig.r_multiple is not None else ""
            lines = [
                f"{emoji} {head} · {sig.tag} · {sig.symbol}",
                f"Prix {sig.price:.4f} · {sig.pnl_pct:+.2f} % · {sig.pnl_usdc:+.2f} USDC{r}",
                f"Motif : {sig.reason}",
            ]
        self._raw("\n".join(lines))

    def regime_change(self, text: str):
        self._raw(f"🔄 RÉGIME · {text}")

    # --- programme / a la demande : vue consolidee -------------------------
    def recap(self, snap: dict, on_demand: bool = False):
        head = "📊 STATUT" if on_demand else f"🕗 RÉCAP · {snap['time']}"
        lines = [f"{head} · PAPER", ""]
        for s in snap["strategies"]:
            lines.append(f"{s['tag']:<4} {s['capital']:>7.2f} USDC   "
                         f"jour {s['pnl_today']:+.2f}   total {s['pnl_total']:+.2f}")
        lines.append("──────────────")
        lines.append(f"Total {snap['total_capital']:.2f} USDC · jour {snap['total_pnl_today']:+.2f}")

        pos = snap["positions"]
        lines.append(f"\nPositions ({len(pos)}/{snap['max_positions']})")
        if pos:
            for p in pos:
                dot = "🟢" if p["pnl_usdc"] >= 0 else "🔴"
                lines.append(f"{dot} {p['tag']} {p['symbol']}  {p['pnl_pct']:+.1f} %  {p['pnl_usdc']:+.2f} USDC")
        else:
            lines.append("aucune")

        lines.append(f"\nRégime : BTC {'haussier' if snap['btc_bullish'] else 'baissier'} (EMA200 4h)")
        if snap.get("scan"):
            lines.append("\nScan marché")
            for sc in snap["scan"]:
                lines.append(f"· {sc['symbol']} {sc['regime']} — {sc['note']}")
        self._raw("\n".join(lines), keyboard=on_demand)

    def perf(self, metrics: list):
        lines = ["📈 PERF · PAPER"]
        for m in metrics:
            lines.append(f"\n{m['tag']} · {m['label']}")
            if m["trades"] == 0:
                lines.append("  aucun trade clôturé")
                continue
            lines += [
                f"  {m['trades']} trades · winrate {m['winrate']:.0f} %",
                f"  R moyen {m['expectancy_r']:+.2f}R/trade  (= espérance)",
                f"  gagnant moyen {m['avg_win_r']:+.1f}R · perdant moyen {m['avg_loss_r']:+.1f}R",
                f"  payoff {m['payoff']:.1f}:1 · PF {m['pf']:.2f} · DD {m['max_dd']:+.2f} USDC",
            ]
        self._raw("\n".join(lines), keyboard=True)

    def positions_detail(self, positions: list):
        if not positions:
            self._raw("📋 Aucune position ouverte.", keyboard=True)
            return
        lines = ["📋 POSITIONS"]
        for p in positions:
            lines += [
                f"\n{p['tag']} · {p['symbol']}",
                f"  Entrée {p['entry']:.4f} → actuel {p['current']:.4f}",
                f"  Stop {p['stop']:.4f}" + (f" · {p['stage']}" if p.get("stage") else "")
                + (f" · cible {p['target']:.4f}" if p.get("target") else ""),
                f"  Flottant {p['pnl_usdc']:+.2f} USDC ({p['pnl_pct']:+.1f} %) · {p['r_now']:+.1f}R",
            ]
        self._raw("\n".join(lines), keyboard=True)

    def info(self, text: str):
        self._raw(f"ℹ️ {text}", keyboard=True)

    def error(self, text: str):
        # anti-spam : meme erreur ignoree pendant 5 min
        now = time.time()
        if self._last_err[0] == text and now - self._last_err[1] < 300:
            return
        self._last_err = (text, now)
        self._raw(f"⚠️ {text}")

    # --- envoi bas niveau --------------------------------------------------
    def _raw(self, text: str, keyboard: bool = False):
        if not self.enabled:
            print(f"[ALERT] {text}")
            return
        try:
            data = {"chat_id": self.chat_id, "text": text}
            if keyboard:
                data["reply_markup"] = json.dumps(KEYBOARD)
            payload = urllib.parse.urlencode(data).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                   data=payload, timeout=10)
        except Exception as e:
            print(f"[alerts] envoi echoue: {e} | {text[:60]}")
