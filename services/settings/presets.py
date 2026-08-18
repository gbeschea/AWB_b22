"""
presets.py — catalogul de CAPABILITĂȚI al Order Hub + PRESET-urile lor. Sursa UNICĂ din care pagina de Setări
randează cele ~6 comutatoare (master switch + preset), în loc de ~42 de câmpuri brute.

Filozofie (owner, 2026-08-18): un magazin nou moștenește TOT din defaults/organizație și NU atinge nimic —
setările există pentru EXCEPȚII. Fiecare capabilitate = 1 comutator ON/OFF + 1 preset; knob-urile brute stau
sub „Avansat", pre-completate corect. Un preset se EXPANDEAZĂ în valorile pe care le citesc motoarele.

Storage per capabilitate (cum e persistat overrideul):
 • "column"       — master switch + knob-uri = coloane pe Store (auto_awb, status_sync) — deja cablate, nu le mutăm;
 • "hub_settings" — blob JSONB org/magazin (duplicates, blocklist), rezolvat de resolver.py (defaults→org→store);
 • "validation"   — tabelul validation_policy (12 politici) + preset cs_routing.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional

# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# PRESET-uri → valorile BRUTE efective. Preset ales = un singur rând în UI; owner-ul nu vede knob-urile.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────

# BLOCKLIST — clienți blocați (serial-refuser). Preset = pragul + ce numărăm drept „refuz".
BLOCKLIST_PRESETS: Dict[str, Dict[str, Any]] = {
    "off":         {"enabled": False},
    "conservator": {"enabled": True, "serial_refuser_threshold": 3, "include_failed": False},
    "echilibrat":  {"enabled": True, "serial_refuser_threshold": 2, "include_failed": False},  # default
    "agresiv":     {"enabled": True, "serial_refuser_threshold": 2, "include_failed": True},
}

# DUPLICATE — comenzi dublate ale aceluiași client. Preset = fereastra + pe ce se potrivește.
DUPLICATE_PRESETS: Dict[str, Dict[str, Any]] = {
    "off":        {"enabled": False},
    "strict":     {"enabled": True, "duplicate_window_hours": 12, "duplicate_match": "phone"},
    "echilibrat": {"enabled": True, "duplicate_window_hours": 24, "duplicate_match": "phone"},  # default
    "generos":    {"enabled": True, "duplicate_window_hours": 72, "duplicate_match": "phone"},
}

# VALIDARE ADRESE — preset = pragul cs_routing (cât de repede renunțăm la auto-corecție → CS). Expandează DOAR
# cheia cs_routing; restul celor 12 politici rămân pe defaults și se ating doar din „Avansat".
ADDRESS_PRESETS: Dict[str, Dict[str, Any]] = {
    "conservator": {"cs_routing": "conservative"},
    "echilibrat":  {"cs_routing": "balanced"},   # default
    "agresiv":     {"cs_routing": "aggressive"},
}


def expand(presets: Dict[str, Dict[str, Any]], name: Optional[str], default: str) -> Dict[str, Any]:
    """Preset (nume) → dict de valori brute. Nume necunoscut/None → presetul default."""
    return dict(presets.get(name or "", presets[default]))


def preset_of(presets: Dict[str, Dict[str, Any]], values: Dict[str, Any], default: str) -> str:
    """Invers: dintr-un dict de valori brute deduce ce preset e activ (pt afișare în UI). Fallback = default."""
    for pname, pv in presets.items():
        if all(values.get(k) == v for k, v in pv.items()):
            return pname
    # măcar respectă master-switch-ul: dacă e enabled=False → 'off' dacă există
    if values.get("enabled") is False and "off" in presets:
        return "off"
    return default


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# CATALOGUL pt UI — o intrare per capabilitate. UI randează: master switch + selector preset + „Avansat".
#   storage: unde stă overrideul · switch: cheia master · presets/preset_default: opțiunile · advanced: knob brute
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
CAPABILITIES: List[Dict[str, Any]] = [
    {
        "key": "auto_awb", "label": "Auto-AWB", "storage": "column",
        "switch": "auto_awb_enabled",
        "help": "Face AWB automat pentru comenzile cu adresă validă, în fereastra setată.",
        "presets": None,   # fără preset — doar switch + Avansat
        "advanced": [
            {"key": "awb_window_start", "type": "hour", "label": "Fereastră de la (oră)"},
            {"key": "awb_window_end", "type": "hour", "label": "Fereastră până la (oră)"},
            {"key": "awb_blackout_start", "type": "minutes", "label": "Blackout de la (min din zi)"},
            {"key": "awb_blackout_end", "type": "minutes", "label": "Blackout până la (min din zi)"},
            {"key": "auto_awb_delay_minutes", "type": "int", "label": "Întârziere înainte de AWB (min)"},
        ],
    },
    {
        "key": "address_validation", "label": "Validare adrese", "storage": "validation",
        "switch": None,    # validarea e mereu ON; preset-ul reglează agresivitatea
        "help": "Corectează și validează adresele (RO + internațional) înainte de AWB.",
        "presets": list(ADDRESS_PRESETS.keys()), "preset_default": "echilibrat",
        "advanced_note": "12 politici (rural, ZIP, HERE, homonime…) — pre-setate ca la noi.",
        "advanced_link": "/app/address-lab",   # editorul celor 12 politici (Address Lab, nu-l dublăm aici)
    },
    {
        "key": "duplicates", "label": "Duplicate", "storage": "hub_settings",
        "switch": "enabled",
        "help": "Prinde comenzile dublate ale aceluiași client înainte să plece.",
        "presets": list(DUPLICATE_PRESETS.keys()), "preset_default": "echilibrat",
        "advanced": [
            {"key": "duplicate_window_hours", "type": "int", "label": "Fereastră (ore)"},
            {"key": "duplicate_match", "type": "enum", "options": ["phone", "email"], "label": "Potrivire după"},
        ],
    },
    {
        "key": "blocklist", "label": "Blocklist", "storage": "hub_settings",
        "switch": "enabled",
        "help": "Blochează auto-AWB pentru clienți puși manual pe listă sau serial-refuseri.",
        "presets": list(BLOCKLIST_PRESETS.keys()), "preset_default": "echilibrat",
        "advanced": [
            {"key": "serial_refuser_threshold", "type": "int", "label": "Prag refuzuri (serial-refuser)"},
            {"key": "include_failed", "type": "bool", "label": "Include livrări eșuate (nu doar refuz)"},
        ],
    },
    {
        "key": "status_sync", "label": "Sync status Shopify", "storage": "column",
        "switch": "status_sync_enabled",
        "help": "Împinge fulfillment/tracking/livrare din curier înapoi în Shopify.",
        "presets": None,
        "advanced": [
            {"key": "fulfill_notify_customer", "type": "bool", "label": "Email «expediat» către client"},
            {"key": "auto_cancel_on_refusal", "type": "bool", "label": "Anulează la refuz (doar COD)"},
            {"key": "refusal_notify_customer", "type": "bool", "label": "Email la anulare refuz"},
            {"key": "refusal_restock", "type": "bool", "label": "Restock la refuz"},
        ],
    },
    {
        "key": "invoicing", "label": "Facturare (SmartBill)", "storage": "json_blob",
        "switch": None, "switch_blob": "invoice_settings",
        "help": "Emite facturi automat prin SmartBill.",
        "presets": None,
    },
]

# preset default per capabilitate JSONB (pt resolver când nu există override)
HUB_PRESET_MAP = {
    "duplicates": (DUPLICATE_PRESETS, "echilibrat"),
    "blocklist": (BLOCKLIST_PRESETS, "echilibrat"),
}
