"""Programarea automatizărilor OH, per magazin, cu DEFAULT-uri.

Fiecare automatizare are un MOD ales de merchant + MINUTE:
  • on_order     — LA COMANDĂ (webhook la ingest), cu delay opțional de `minutes`
  • cron         — periodic, la fiecare `minutes` (bucla din services.cron_parity.loop)
  • on_delivered — la LIVRARE (doar cod_capture; din status_sync când coletul devine delivered)
  • off          — dezactivat

Stocare: `stores.automation_schedule` (JSONB), suprapus peste DEFAULT_SCHEDULE. Merchantul le
setează în Settings; aici sunt default-urile + helperele de citire. Nivel: dup + risc se evaluează
la nivel de ORGANIZAȚIE (vezi order_shadow); restul per magazin.
"""
from __future__ import annotations

from typing import Any, Dict

# mod + minute default per automatizare (cererea owner: detectoarele la comandă imediat, AWB la 5 min)
DEFAULT_SCHEDULE: Dict[str, Dict[str, Any]] = {
    "duplicates":  {"mode": "on_order",     "minutes": 0},
    "parcels":     {"mode": "on_order",     "minutes": 0},
    "surprise":    {"mode": "on_order",     "minutes": 0},
    "blocklist":   {"mode": "on_order",     "minutes": 0},
    "cod_capture": {"mode": "on_delivered", "minutes": 0},
    "awb":         {"mode": "on_order",     "minutes": 5},
}

AUTOMATIONS = list(DEFAULT_SCHEDULE.keys())
# detectoare care rulează per-comandă la ingest (order_shadow) când modul e on_order.
# „Risc de comandă" = contopit în BLOCKLIST (serial-refuser): ≥N refuzuri în grup → cancel pe intl / hold-CS pe RO.
ON_ORDER_DETECTORS = ["duplicates", "parcels", "surprise", "blocklist"]
VALID_MODES = {"on_order", "cron", "on_delivered", "off"}
VALID_ACTIONS = {"none", "hold", "cancel"}


def _sched(store) -> Dict[str, Any]:
    return getattr(store, "automation_schedule", None) or {}


def mode_of(store, key: str) -> str:
    d = DEFAULT_SCHEDULE.get(key, {})
    m = ((_sched(store).get(key) or {}).get("mode")) or d.get("mode") or "off"
    return m if m in VALID_MODES else (d.get("mode") or "off")


def minutes_of(store, key: str) -> int:
    d = DEFAULT_SCHEDULE.get(key, {})
    v = (_sched(store).get(key) or {}).get("minutes", d.get("minutes", 0))
    try:
        return max(0, int(v))
    except Exception:
        return int(d.get("minutes", 0) or 0)


def close_instead_of_hold(store) -> bool:
    """REGULA „fără hold-uri": magazinele fără CS care lucrează coada (internaționale / NO_CS) NU lasă
    comenzi pe HOLD — n-are cine să le rezolve → orice „hold" devine „cancel" (close). Setare per magazin
    `automation_schedule.no_hold`; default = magazin fără coadă CS (services.utils.no_cs)."""
    v = _sched(store).get("no_hold")
    if isinstance(v, bool):
        return v
    try:
        from services.utils import no_cs
        return bool(no_cs(store))
    except Exception:
        return False


def effective_action(store, action: str) -> str:
    """REGULA pe magazinele fără CS (internaționale): NU lăsăm HOLD-uri — încercăm să TRIMITEM tot.
    `hold` → `ship` (lasă comanda să meargă la AWB); `cancel` rămâne `cancel` (ce NU putem trimite:
    dubluri adevărate, risc mare, clienți blocați, adresă imposibilă)."""
    if action == "hold" and close_instead_of_hold(store):
        return "ship"
    return action


def merged_schedule(store) -> Dict[str, Any]:
    """Programarea EFECTIVĂ (defaults + override magazin) — pentru API/UI."""
    out: Dict[str, Any] = {k: dict(v) for k, v in DEFAULT_SCHEDULE.items()}
    for k, v in _sched(store).items():
        if k in out and isinstance(v, dict):
            if v.get("mode") in VALID_MODES:
                out[k]["mode"] = v["mode"]
            if "minutes" in v:
                try:
                    out[k]["minutes"] = max(0, int(v["minutes"]))
                except Exception:
                    pass
    out["no_hold"] = close_instead_of_hold(store)
    return out


def sanitize(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Curăță un payload de la UI înainte de salvare (doar chei/valori valide)."""
    clean: Dict[str, Any] = {}
    for k in AUTOMATIONS:
        v = payload.get(k)
        if not isinstance(v, dict):
            continue
        entry = {}
        if v.get("mode") in VALID_MODES:
            entry["mode"] = v["mode"]
        if "minutes" in v:
            try:
                entry["minutes"] = max(0, int(v["minutes"]))
            except Exception:
                pass
        if entry:
            clean[k] = entry
    if isinstance(payload.get("no_hold"), bool):
        clean["no_hold"] = payload["no_hold"]
    return clean
