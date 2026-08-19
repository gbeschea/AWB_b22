"""Rutarea comenzilor de SCHIMB (swap) pe connectorul de swap al curierului.

Un swap nu e o livrare obișnuită: curierul livrează produsul nou ȘI ridică produsul vechi, pe un
contract separat („DPD SWAP" la noi). Pe connectorul normal, coletul pleacă fără ridicare — clientul
rămâne cu produsul vechi în mână și cineva reface manual toată operațiunea.

SEMNALUL e TAG-ul exact `swap`, nimic altceva. Pe 90 de zile, toate cele 6 comenzi cu tag-ul `swap`
au plecat pe DPD SWAP — 6 din 6, fără excepție. Deci regula asta nu schimbă practica, o face
deterministă (azi o ține un om minte, când plasează retrimiterea).

DOUĂ capcane, ambele la un caracter distanță de adevăr:
  • `swap_request_bi` NU e swap. 60 de comenzi în 90 de zile, din care 56 au plecat pe connectorul
    normal — e un flag de BI, nu o instrucțiune de expediere. Potrivirea pe subșir („conține swap")
    l-ar prinde și ar muta 56 de comenzi pe contractul de schimb. Comparăm tag-uri ÎNTREGI.
  • `duplicata` NU e swap, deși apare pe multe retrimiteri (inclusiv pe comenzile de la care a
    pornit discuția). Peste 4400 de comenzi cu `duplicata` în 30 de zile pleacă normal.

Nu ne uităm în NOTĂ: e text liber scris de oameni, unde „swap" apare și în „clientul a cerut swap
dar nu se poate". Tag-ul e pus deliberat; nota e conversație.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SWAP_TAG = "swap"


def is_swap_order(order) -> bool:
    """Tag-ul `swap` EXACT, comparat pe tag întreg. `swap_request_bi` și `swapped-out` nu se potrivesc."""
    tags = (getattr(order, "tags", "") or "")
    return any(t.strip().lower() == _SWAP_TAG for t in tags.split(","))


def pick_connector(order, connectors: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Connectorul de swap al merchantului, dacă comanda e swap ȘI el există. Altfel None (cheamă
    default-ul). Doar 14 din 23 de magazine au unul — pe restul, un swap pleacă pe connectorul normal,
    exact ca azi; nu blocăm expedierea pentru o rutare pe care merchantul n-a configurat-o."""
    if not is_swap_order(order):
        return None
    for c in connectors or []:
        if not c.get("active"):
            continue
        if (c.get("type") or "").upper() == "SMART_BILL":
            continue
        if "swap" in (c.get("name") or "").lower():
            return c
    logger.info("swap-routing: %s e swap dar merchantul n-are connector de swap — merge pe default",
                getattr(order, "name", "?"))
    return None
