"""
hub_settings.py — pagina „Automatizări" (control tower): UNIFICĂ cele ~6 capabilități ale Order Hub ca
comutatoare cu preset, în loc de ~42 de câmpuri. Deține setările MOȘTENITE org→magazin (duplicates, blocklist,
preset validare); capabilitățile pe coloane (auto-AWB, sync) sunt afișate cu link spre editorul per-magazin.

Scop = ORGANIZAȚIE (pui o dată, toate magazinele moștenesc) SAU un MAGAZIN (override doar unde diferă).
Vezi services/settings/{presets,resolver}.py. Scrierile sunt idempotente (upsert pe rândul org/magazin).
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request, Body, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import AsyncSession

import models
from database import get_db
from dependencies import get_templates
from services.settings import presets as P, resolver
from services.nomenclator.policy import merge_policy, POLICY_DEFAULTS

router = APIRouter(prefix="/settings/hub", tags=["Hub Settings"])

_ADDR_PRESET_BY_ROUTING = {v["cs_routing"]: k for k, v in P.ADDRESS_PRESETS.items()}


# ─── pagina ───────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse, name="get_hub_settings_page")
async def hub_page(request: Request, db: AsyncSession = Depends(get_db),
                   templates: Jinja2Templates = Depends(get_templates)):
    stores = (await db.execute(
        select(models.Store).where(models.Store.is_active.is_(True)).order_by(models.Store.name)
    )).scalars().all()
    orgs = (await db.execute(select(models.Organization).order_by(models.Organization.name))).scalars().all()
    return templates.TemplateResponse("settings_hub.html", {
        "request": request, "capabilities": P.CAPABILITIES,
        "stores": stores, "organizations": orgs,
    })


# ─── citirea stării (per scope) → alimentează UI-ul ───────────────────────────────────────────────────────
async def _hub_state(db, cap_key: str, scope: str, store: Optional[models.Store],
                     org_id: Optional[int]) -> Dict[str, Any]:
    """Pt o capabilitate JSONB: preset activ + valori efective + de la ce NIVEL vine (default|org|magazin)."""
    presets, default = P.HUB_PRESET_MAP[cap_key]
    # nivelurile setate explicit
    org_blob = {}
    if org_id:
        r = (await db.execute(select(models.HubSettings.settings).where(
            models.HubSettings.organization_id == org_id, models.HubSettings.store_id.is_(None)))).scalar_one_or_none()
        org_blob = (r or {}).get(cap_key) or {}
    store_blob = {}
    if store is not None:
        r = (await db.execute(select(models.HubSettings.settings).where(
            models.HubSettings.store_id == store.id))).scalar_one_or_none()
        store_blob = (r or {}).get(cap_key) or {}
    if scope == "store" and store_blob:
        source = "store"
    elif org_blob:
        source = "org"
    else:
        source = "default"
    # valori efective la scope-ul cerut
    if scope == "org":
        eff = resolver._resolve_one(cap_key, org_blob, {})
    else:
        eff = resolver._resolve_one(cap_key, org_blob, store_blob)
    return {"preset": eff.get("_preset", default), "enabled": bool(eff.get("enabled")),
            "source": source, "values": {k: v for k, v in eff.items() if not k.startswith("_")}}


async def _addr_state(db, scope: str, store: Optional[models.Store], org_id: Optional[int]) -> Dict[str, Any]:
    # rândurile BRUTE pe niveluri — ca să deducem DE UNDE vine cs_routing, nu doar valoarea efectivă
    store_pol: Dict[str, Any] = {}
    if scope == "store" and store is not None:
        r = (await db.execute(select(models.ValidationPolicy.policies).where(
            models.ValidationPolicy.store_id == store.id))).scalar_one_or_none()
        store_pol = r or {}
    org_pol: Dict[str, Any] = {}
    if org_id:
        r = (await db.execute(select(models.ValidationPolicy.policies).where(
            models.ValidationPolicy.store_id.is_(None),
            models.ValidationPolicy.organization_id == org_id))).scalar_one_or_none()
        org_pol = r or {}
    # valoarea efectivă la scope-ul cerut
    if scope == "org":
        glob = (await db.execute(select(models.ValidationPolicy.policies).where(
            models.ValidationPolicy.store_id.is_(None),
            models.ValidationPolicy.organization_id.is_(None)))).scalar_one_or_none()
        ov = {k: v for k, v in (glob or {}).items() if v is not None}
        ov.update({k: v for k, v in org_pol.items() if v is not None})
    else:
        ov = await resolver.effective_overrides(db, store)
    routing = merge_policy(ov).get("cs_routing", POLICY_DEFAULTS["cs_routing"])
    preset = _ADDR_PRESET_BY_ROUTING.get(routing, "echilibrat")
    if scope == "store" and store_pol.get("cs_routing") is not None:
        source = "store"
    elif org_pol.get("cs_routing") is not None:
        source = "org"
    else:
        source = "default"
    return {"preset": preset, "enabled": True, "source": source, "values": {"cs_routing": routing}}


@router.get("/state", name="get_hub_state")
async def hub_state(scope: str = "org", id: Optional[int] = None, db: AsyncSession = Depends(get_db)):
    store, org_id = None, None
    if scope == "store":
        store = (await db.execute(select(models.Store).where(models.Store.id == id))).scalar_one_or_none()
        if not store:
            raise HTTPException(404, "store")
        org_id = store.organization_id
    else:
        org_id = id
    out: List[Dict[str, Any]] = []
    for cap in P.CAPABILITIES:
        st: Dict[str, Any]
        if cap["key"] in P.HUB_PRESET_MAP:
            st = await _hub_state(db, cap["key"], scope, store, org_id)
        elif cap["key"] == "address_validation":
            st = await _addr_state(db, scope, store, org_id)
        else:                                            # capabilități pe coloane (auto_awb, status_sync, invoicing)
            if scope == "store" and store is not None:
                enabled = bool(getattr(store, cap.get("switch") or "", False)) if cap.get("switch") else None
                st = {"preset": None, "enabled": enabled, "source": "store", "values": {}, "column": True}
            else:
                st = {"preset": None, "enabled": None, "source": "per_store", "values": {}, "column": True}
        st.update({"key": cap["key"], "label": cap["label"], "help": cap.get("help", ""),
                   "presets": cap.get("presets"), "storage": cap["storage"],
                   "advanced": cap.get("advanced"), "advanced_link": cap.get("advanced_link")})
        out.append(st)
    return {"scope": scope, "id": id, "capabilities": out}


# ─── scriere (upsert idempotent pe nivelul cerut) ─────────────────────────────────────────────────────────
async def _hub_row(db, scope: str, id_: int) -> models.HubSettings:
    if scope == "org":
        row = (await db.execute(select(models.HubSettings).where(
            models.HubSettings.organization_id == id_, models.HubSettings.store_id.is_(None)))).scalar_one_or_none()
        if not row:
            row = models.HubSettings(organization_id=id_, store_id=None, settings={})
            db.add(row)
    else:
        row = (await db.execute(select(models.HubSettings).where(
            models.HubSettings.store_id == id_))).scalar_one_or_none()
        if not row:
            row = models.HubSettings(store_id=id_, settings={})
            db.add(row)
    return row


async def _vpolicy_row(db, scope: str, id_: int) -> models.ValidationPolicy:
    if scope == "org":
        row = (await db.execute(select(models.ValidationPolicy).where(
            models.ValidationPolicy.store_id.is_(None), models.ValidationPolicy.organization_id == id_))).scalar_one_or_none()
        if not row:
            row = models.ValidationPolicy(store_id=None, organization_id=id_, policies={})
            db.add(row)
    else:
        row = (await db.execute(select(models.ValidationPolicy).where(
            models.ValidationPolicy.store_id == id_))).scalar_one_or_none()
        if not row:
            row = models.ValidationPolicy(store_id=id_, policies={})
            db.add(row)
    return row


@router.post("/set", name="set_hub_capability")
async def set_capability(payload: Dict[str, Any] = Body(...), db: AsyncSession = Depends(get_db)):
    """{scope: 'org'|'store', id, capability, preset, advanced?} → upsert pe nivelul cerut. Preset 'inherit'
    (doar la scope=store) șterge overrideul de magazin → cade înapoi pe organizație/default."""
    scope = payload.get("scope")
    id_ = payload.get("id")
    cap = payload.get("capability")
    preset = payload.get("preset")
    advanced = payload.get("advanced") or {}
    if scope not in ("org", "store") or not id_ or not cap:
        raise HTTPException(400, "scope/id/capability required")

    async def _apply():
        if cap in P.HUB_PRESET_MAP:
            presets, default = P.HUB_PRESET_MAP[cap]
            row = await _hub_row(db, scope, id_)
            blob = dict(row.settings or {})
            if preset == "inherit" and scope == "store":
                blob.pop(cap, None)                          # revino la moștenire
            else:
                if preset and preset not in presets:
                    raise HTTPException(400, f"preset necunoscut: {preset}")
                entry: Dict[str, Any]
                if preset:
                    entry = {"preset": preset}               # preset nou ales → resetează (fără override-uri vechi)
                else:
                    entry = dict(blob.get(cap) or {})        # doar Avansat → păstrează presetul, suprapune knob-ul
                entry.update({k: v for k, v in advanced.items() if v is not None})
                blob[cap] = entry
            row.settings = blob
            flag_modified(row, "settings")
        elif cap == "address_validation":
            if preset and preset not in P.ADDRESS_PRESETS:
                raise HTTPException(400, f"preset necunoscut: {preset}")
            row = await _vpolicy_row(db, scope, id_)
            pol = dict(row.policies or {})
            if preset == "inherit" and scope == "store":
                pol.pop("cs_routing", None)
            elif preset:
                pol.update(P.ADDRESS_PRESETS[preset])        # setează cs_routing
            pol.update({k: v for k, v in advanced.items() if v is not None})
            row.policies = pol
            flag_modified(row, "policies")
        else:
            raise HTTPException(400, f"capabilitatea '{cap}' se configurează per magazin (coloane), nu aici")
        await db.commit()

    # get-or-create poate curse pe PRIMUL override (2 request-uri simultane creează 2 rânduri org/magazin →
    # violare unique parțial). Reîncearcă o dată: după rollback, re-SELECT găsește rândul creat de concurent
    # și intră pe calea de UPDATE. HTTPException (ex. preset necunoscut) NU e prinsă — se propagă normal.
    for attempt in (1, 2):
        try:
            await _apply()
            break
        except IntegrityError:
            await db.rollback()
            if attempt == 2:
                raise HTTPException(409, "conflict de scriere concurentă — reîncearcă")
    return {"ok": True}
