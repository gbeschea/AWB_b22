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
    "risk":        {"mode": "on_order",     "minutes": 0},
    "cod_capture": {"mode": "on_delivered", "minutes": 0},
    "awb":         {"mode": "on_order",     "minutes": 5},
}
# ce se întâmplă la fiecare nivel de risc de comandă — ales de merchant (mediu/mare)
RISK_ACTION_DEFAULTS = {"medium": "hold", "high": "cancel"}

AUTOMATIONS = list(DEFAULT_SCHEDULE.keys())
# detectoare care rulează per-comandă la ingest (order_shadow) când modul e on_order
ON_ORDER_DETECTORS = ["duplicates", "parcels", "surprise", "blocklist", "risk"]
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


def risk_actions(store) -> Dict[str, str]:
    ra = _sched(store).get("risk_actions") or {}
    out = {}
    for lvl in ("medium", "high"):
        a = ra.get(lvl)
        out[lvl] = a if a in VALID_ACTIONS else RISK_ACTION_DEFAULTS[lvl]
    return out


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
    out["risk_actions"] = risk_actions(store)
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
    ra = payload.get("risk_actions")
    if isinstance(ra, dict):
        rc = {lvl: ra[lvl] for lvl in ("medium", "high") if ra.get(lvl) in VALID_ACTIONS}
        if rc:
            clean["risk_actions"] = rc
    return clean
