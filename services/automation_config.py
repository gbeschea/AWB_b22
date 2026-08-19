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
    "special":     {"mode": "on_order",     "minutes": 0},
    "cod_capture": {"mode": "on_delivered", "minutes": 0},
    "awb":         {"mode": "on_order",     "minutes": 5},
}

# CÂND apare comanda ca EXPEDIATĂ în Shopify, la curierii DIRECȚI (DPD/Sameday/GLS/…), unde OH e cel
# care împinge fulfillment-ul. La xConnector/Frisbo nu se aplică: platforma lor fulfill-uiește singură.
#   on_pickup (implicit) = la prima scanare a curierului. Clientul primește „expediat" când chiar a plecat;
#                          o etichetă anulată înainte de ridicare nu lasă o comandă fals-expediată.
#   on_label             = imediat ce se face eticheta. Shopify arată expediat pe loc, dar clientul poate
#                          fi notificat înainte ca pachetul să plece efectiv.
FULFILL_WHEN_DEFAULT = "on_pickup"
VALID_FULFILL_WHEN = {"on_pickup", "on_label"}


def fulfill_when(store) -> str:
    v = (_sched(store).get("fulfill_when") or "").strip().lower()
    return v if v in VALID_FULFILL_WHEN else FULFILL_WHEN_DEFAULT


# REGULI SPECIALE (merchant): keyword în tags/note → acțiune (hold|cancel|ship), PESTE politica implicită
# (ex. „influencer" → hold, chiar și pe internațional unde altfel s-ar trimite).
#
# `whole=True` = potrivire pe TAG ÎNTREG, nu pe subșir. Există pentru că cele două reguli implicite au
# nevoi opuse și amândouă sunt corecte:
#   • „influencer" trebuie să prindă și tag-ul „influenceri" → subșir (implicit).
#   • „swap" NU are voie să prindă „swap_request_bi", un flag de BI pus pe ~60 de comenzi în 90 de zile
#     care n-au nicio legătură cu un schimb. Pe subșir, regula ar pune pe hold toate acele comenzi.
#
# swap → HOLD, nu expediere: un schimb (livrezi noul produs, ridici pe cel vechi) NU se poate face prin
# xConnector, deci orice AWB automat pe o comandă de swap ar fi o livrare simplă — coletul pleacă,
# produsul vechi rămâne la client, iar operațiunea trebuie refăcută manual. Îl oprim și îl dăm la om.
SPECIAL_RULE_DEFAULTS = [
    {"contains": "influencer", "action": "hold"},
    {"contains": "swap", "action": "hold", "whole": True},
]
VALID_SPECIAL_ACTIONS = {"hold", "cancel", "ship"}

AUTOMATIONS = list(DEFAULT_SCHEDULE.keys())
# detectoare care rulează per-comandă la ingest (order_shadow) când modul e on_order.
# „Risc de comandă" = contopit în BLOCKLIST (serial-refuser): ≥N refuzuri în grup → cancel pe intl / hold-CS pe RO.
ON_ORDER_DETECTORS = ["special", "duplicates", "parcels", "surprise", "blocklist"]
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


def special_rules(store) -> list:
    """Regulile speciale ale magazinului: listă de {contains, action}. Match pe tags+note (numele e criptat).
    Default = influencer→hold. Aplicate PESTE politica implicită (ex. hold chiar și pe internațional)."""
    v = _sched(store).get("special_rules")
    if isinstance(v, list):
        out = []
        for r in v:
            kw = str((r or {}).get("contains") or "").strip().lower()
            if kw and (r or {}).get("action") in VALID_SPECIAL_ACTIONS:
                out.append({"contains": kw, "action": r["action"],
                            "whole": bool((r or {}).get("whole"))})
        return out
    return [dict(r) for r in SPECIAL_RULE_DEFAULTS]


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
    out["fulfill_when"] = fulfill_when(store)
    out["special_rules"] = special_rules(store)
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
    if payload.get("fulfill_when") in VALID_FULFILL_WHEN:
        clean["fulfill_when"] = payload["fulfill_when"]
    sr = payload.get("special_rules")
    if isinstance(sr, list):
        rules = []
        for r in sr:
            kw = str((r or {}).get("contains") or "").strip()
            if kw and (r or {}).get("action") in VALID_SPECIAL_ACTIONS:
                rules.append({"contains": kw, "action": r["action"],
                              "whole": bool((r or {}).get("whole"))})
        clean["special_rules"] = rules      # listă goală = fără reguli speciale (înlocuiește default-ul)
    return clean


def special_rule_matches(rule: Dict[str, Any], order) -> bool:
    """True dacă regula se potrivește comenzii. `whole=True` compară TAG-URI ÎNTREGI (separate pe
    virgulă) — singurul mod în care „swap" nu înghite „swap_request_bi". Altfel, subșir în tags+note,
    ca să prindă și formele flexionate („influencer" în „influenceri")."""
    kw = (rule.get("contains") or "").strip().lower()
    if not kw:
        return False
    tags = (getattr(order, "tags", "") or "")
    if rule.get("whole"):
        return any(t.strip().lower() == kw for t in tags.split(","))
    return kw in (tags + " " + (getattr(order, "note", "") or "")).lower()
