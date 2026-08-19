"""Rutarea comenzilor de SCHIMB (swap) pe connectorul de swap al curierului.

Un swap nu e o livrare obișnuită: curierul livrează produsul nou ȘI ridică produsul vechi, pe un
contract separat („DPD SWAP" la noi). Pe connectorul normal, coletul pleacă fără ridicare — clientul
rămâne cu produsul vechi în mână și cineva reface manual toată operațiunea.

Azi alegerea o face omul din CS, în xConnector, când plasează retrimiterea — și se pierde des:
măsurat pe 60 de zile, din comenzile marcate `swap`, 40 au plecat pe DPD SWAP și 51 pe DPD Romania.
Regula asta o face determinist.

CAPCANĂ ocolită deliberat: `duplicata` NU înseamnă swap, deși apare pe multe retrimiteri. Pe 30 de
zile, comenzile cu `duplicata` au plecat 4410 pe DPD Romania față de 66 pe DPD SWAP — e un marcaj
de cu totul altceva (factură duplicat). Dacă l-am trata ca semnal de swap, am muta ~4400 de comenzi
normale pe un contract de schimb. Semnalul e DOAR cuvântul `swap`.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# `\bswap\b`: eticheta e „swap", nu o subșiră întâmplătoare. („inlocuire" NU e semnal — cele 8 comenzi
# marcate așa au plecat toate pe connectorul normal, deci nu e contractul de schimb.)
_SWAP_RE = re.compile(r"\bswap\b", re.I)


def is_swap_order(order) -> bool:
    blob = "%s %s" % (getattr(order, "tags", "") or "", getattr(order, "note", "") or "")
    return bool(_SWAP_RE.search(blob))


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
