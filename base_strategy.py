"""
Le CONTRAT. Une strategie produit des SIGNAUX ; elle n'envoie JAMAIS de message.
Tout ce qui parle a Telegram vit dans alerts.py.

Chaque strategie :
  - expose un `tag` unique (prefixe d'alerte, jamais code en dur ailleurs)
  - scan(market)                   -> signaux d'entree
  - check_exits(positions, market) -> signaux de sortie (met a jour le trailing en place)
  - encapsule son etat (force_scan = attribut d'instance, plus de `global`)
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Dict


class SignalType(Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"    # sortie normale (cible / trailing)
    STOP = "STOP"    # stop touche (perte ou nul)


@dataclass
class Signal:
    type: SignalType
    tag: str
    symbol: str
    price: float
    reason: str
    # entree :
    stop: Optional[float] = None        # stop initial (= 1R)
    size: Optional[float] = None        # taille en unites de base
    target: Optional[float] = None      # cible fixe (MR) ; None = trailing (BRK)
    # sortie :
    pnl_pct: Optional[float] = None
    pnl_usdc: Optional[float] = None
    r_multiple: Optional[float] = None  # gain/perte exprime en multiples de R


class BaseStrategy:
    tag: str = "BASE"
    label: str = "Base"

    def __init__(self):
        self.force_scan: bool = False

    def scan(self, market: Dict[str, "pd.DataFrame"]) -> List[Signal]:  # noqa: F821
        raise NotImplementedError

    def check_exits(self, positions: List[dict], market: Dict[str, "pd.DataFrame"]) -> List[Signal]:  # noqa: F821
        raise NotImplementedError

    # --- dimensionnement par le risque ------------------------------------
    @staticmethod
    def position_size(capital: float, risk_pct: float, entry: float, stop: float) -> float:
        risk_per_unit = entry - stop
        if risk_per_unit <= 0:
            return 0.0
        return (capital * risk_pct) / risk_per_unit

    # --- construction d'un signal de sortie (commun aux moteurs) ----------
    @classmethod
    def build_exit(cls, pos: dict, price: float, reason: str) -> Signal:
        entry = pos["entry_price"]
        init_stop = pos.get("init_stop", pos["stop"])
        r_unit = entry - init_stop
        pnl_pct = (price / entry - 1) * 100
        pnl_usdc = (price - entry) * pos["size"]
        r_mult = (price - entry) / r_unit if r_unit > 0 else 0.0
        stype = SignalType.STOP if price <= entry else SignalType.EXIT
        return Signal(type=stype, tag=cls.tag, symbol=pos["symbol"], price=price,
                      reason=reason, pnl_pct=pnl_pct, pnl_usdc=pnl_usdc, r_multiple=r_mult)
