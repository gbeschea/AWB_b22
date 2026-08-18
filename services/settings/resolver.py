"""
resolver.py — REZOLVAREA setărilor pe lanțul de MOȘTENIRE: defaults → ORGANIZAȚIE → MAGAZIN.

De ce (owner, 2026-08-18): OH e aplicația NOASTRĂ, instalată pe 15+ magazine care rulează ACELAȘI playbook.
Setările per-magazin te obligau să configurezi de 15 ori identic. Aici pui o dată la nivel de ORGANIZAȚIE și
toate magazinele moștenesc; un magazin override-uiește DOAR cheile în care chiar diferă (cad restul pe org/defaults).

Două fețe:
 • capabilități JSONB (duplicates, blocklist) → `resolve_capability(db, store, key)` = preset default → org → magazin;
 • validare adrese (12 politici) → `effective_overrides(db, store)` = global → org → magazin (peste POLICY_DEFAULTS).
Ambele citesc din `hub_settings` (org/magazin) și `validation_policy`. Un magazin care n-a atins nimic = pur moștenire.
"""
from __future__ import annotations
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select

import models
from . import presets as P


# ─── capabilități JSONB (hub_settings) ────────────────────────────────────────────────────────────────────
async def _hub_rows(db, store: models.Store) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(org_blob, store_blob) — cele două rânduri hub_settings relevante (lipsă → {})."""
    org_blob: Dict[str, Any] = {}
    if getattr(store, "organization_id", None):
        row = (await db.execute(
            select(models.HubSettings.settings).where(
                models.HubSettings.organization_id == store.organization_id,
                models.HubSettings.store_id.is_(None))
        )).scalar_one_or_none()
        org_blob = dict(row or {})
    row = (await db.execute(
        select(models.HubSettings.settings).where(models.HubSettings.store_id == store.id)
    )).scalar_one_or_none()
    return org_blob, dict(row or {})


def _resolve_one(cap_key: str, org_cap: Dict[str, Any], store_cap: Dict[str, Any]) -> Dict[str, Any]:
    """preset default → preset org → preset magazin, apoi override-uri brute (Avansat) org apoi magazin."""
    presets, default = P.HUB_PRESET_MAP[cap_key]
    chosen = store_cap.get("preset") or org_cap.get("preset") or default
    eff = P.expand(presets, chosen, default)          # preset → valori brute
    for blob in (org_cap, store_cap):                 # Avansat: knob brute peste preset (magazin bate org)
        for k, v in blob.items():
            if k != "preset" and v is not None:
                eff[k] = v
    eff["_preset"] = chosen
    return eff


async def resolve_capability(db, store: models.Store, cap_key: str) -> Dict[str, Any]:
    """Config EFECTIV pt o capabilitate JSONB (duplicates|blocklist), moștenit org→magazin."""
    org_blob, store_blob = await _hub_rows(db, store)
    return _resolve_one(cap_key, dict(org_blob.get(cap_key) or {}), dict(store_blob.get(cap_key) or {}))


# ─── validare adrese (validation_policy) ──────────────────────────────────────────────────────────────────
async def effective_overrides(db, store: Optional[models.Store]) -> Dict[str, Any]:
    """Override-urile de politică EFECTIVE = global(store&org NULL) → org → magazin. Se dau lui merge_policy()
    (care le pune peste POLICY_DEFAULTS). store=None → doar globalul."""
    merged: Dict[str, Any] = {}
    glob = (await db.execute(
        select(models.ValidationPolicy.policies).where(
            models.ValidationPolicy.store_id.is_(None),
            models.ValidationPolicy.organization_id.is_(None))
    )).scalar_one_or_none()
    merged.update({k: v for k, v in (glob or {}).items() if v is not None})
    if store is not None and getattr(store, "organization_id", None):
        org = (await db.execute(
            select(models.ValidationPolicy.policies).where(
                models.ValidationPolicy.store_id.is_(None),
                models.ValidationPolicy.organization_id == store.organization_id)
        )).scalar_one_or_none()
        merged.update({k: v for k, v in (org or {}).items() if v is not None})
    if store is not None:
        st = (await db.execute(
            select(models.ValidationPolicy.policies).where(models.ValidationPolicy.store_id == store.id)
        )).scalar_one_or_none()
        merged.update({k: v for k, v in (st or {}).items() if v is not None})
    return merged
