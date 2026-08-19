"""Automated AWB creation (opt-in, safe by construction).

For a store that turned it on AND picked a courier, this makes AWBs for eligible orders on a
schedule. Eligible = valid address + not cancelled + no AWB yet + at least `auto_awb_delay_minutes`
old (a cancellation / address-fix / COD-confirmation buffer). It only runs inside the merchant's
window (or all day / continuously if no window is set) and NEVER guesses a courier — it does
nothing until `auto_awb_account_key` is set. Bounded per store per pass.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, text
from sqlalchemy.orm import selectinload

import models
from database import AsyncSessionLocal

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("Europe/Bucharest")
_PER_STORE_CAP = 25            # bound dispatch per store per pass
_VALID_ADDR = ("valid", "validat")


def _in_span_min(now_min: int, s, e) -> bool:
    """now_min inside [s,e) minutes-of-day, wrapping past midnight when e<s. s/e None or equal → False."""
    if s is None or e is None or s == e:
        return False
    return (s <= now_min < e) if s < e else (now_min >= s or now_min < e)


def window_ok(store: models.Store) -> bool:
    """True if auto-AWB may run now (Bucharest local): inside the ALLOWED window (whole hours; NULL = all day)
    AND outside the BLACKOUT interval (minutes-of-day; NULL = none)."""
    now = datetime.now(_TZ)
    s, e = store.awb_window_start, store.awb_window_end
    if s is not None and e is not None and s != e:
        allowed = (s <= now.hour < e) if s < e else (now.hour >= s or now.hour < e)  # e<s wraps past midnight
        if not allowed:
            return False
    bs, be = getattr(store, "awb_blackout_start", None), getattr(store, "awb_blackout_end", None)
    if _in_span_min(now.hour * 60 + now.minute, bs, be):
        return False
    return True


async def run_store(db, store: models.Store) -> Dict[str, Any]:
    if not getattr(store, "auto_awb_enabled", False):
        return {"skipped": "off"}
    if not window_ok(store):
        return {"skipped": "outside-window"}

    # Lazy imports avoid a module-load cycle (courier_actions imports services.*).
    from routes.courier_actions import _profile_base, _create_one, _request_pickup
    from services import shipment_rules

    # Conditional routing: enabled rules (this store or shared), evaluated in priority order.
    rules = (await db.execute(
        select(models.ShipmentRule).where(
            models.ShipmentRule.enabled.is_(True),
            (models.ShipmentRule.store_id == store.id) | (models.ShipmentRule.store_id.is_(None)),
        ).order_by(models.ShipmentRule.priority.asc(), models.ShipmentRule.id.asc())
    )).scalars().all()

    default_profile_id = getattr(store, "auto_awb_profile_id", None)
    default_account = getattr(store, "auto_awb_account_key", None)
    if not (rules or default_profile_id or default_account):
        return {"skipped": "off"}  # nothing to route with — never guess a courier

    # Rollback-ul din bucla EXPIRĂ toate instanțele sesiunii, inclusiv `store`. Orice citire de atribut de
    # pe el DUPĂ un eșec aruncă MissingGreenlet (chiar și în linia de log de la final). Capturăm acum.
    store_id, store_domain = store.id, store.domain
    delay = int(getattr(store, "auto_awb_delay_minutes", 0) or 0)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=delay)

    has_awb = select(models.Shipment.id).where(
        models.Shipment.order_id == models.Order.id, models.Shipment.awb.isnot(None))
    cs_open = select(models.CSQueueItem.id).where(
        models.CSQueueItem.order_id == models.Order.id, models.CSQueueItem.status != "solved")
    stmt = (
        select(models.Order)
        .options(selectinload(models.Order.line_items), selectinload(models.Order.store),
                 selectinload(models.Order.shipments))
        .where(
            models.Order.store_id == store_id,
            models.Order.cancelled_at.is_(None),
            models.Order.address_status.in_(_VALID_ADDR),
            models.Order.created_at <= cutoff,
            ~has_awb.exists(),
            # Ghost-AWB / already-shipped guard: never auto-AWB an order Shopify already reports as
            # fulfilled (shipped by another system / a manual label) — that would double-ship.
            models.Order.fulfilled_at.is_(None),
            # HOLD-URILE SUNT OBLIGATORII, nu decorative: fără ele, tot ce opresc detectoarele (dublură,
            # client blocat, adresă proastă, regulă specială „influencer") ar primi AWB automat oricum —
            # exact ce încearcă să prevină. Două surse, ambele necesare: coada CS a OH și hold-ul pus în
            # Shopify (pe care îl poate pune și un om, sau cronul). (Blocantul #1 din auditul cronului.)
            ~cs_open.exists(),
            or_(models.Order.is_on_hold_shopify.is_(None),
                models.Order.is_on_hold_shopify.is_(False)),
        )
        .order_by(models.Order.created_at.asc())
        .limit(_PER_STORE_CAP)
    )
    orders = (await db.execute(stmt)).scalars().all()
    if not orders:
        return {"eligible": 0}

    # Comenzile pe care le-am ABANDONAT (prea multe eșecuri) sau le-am predat deja la CS nu se mai
    # reîncearcă: altfel ocupă permanent cota de 25/magazin (ordonată crescător pe dată) și blochează
    # comenzile noi — starvation tăcut, aceeași clasă ca la polling-ul de status.
    try:
        from services import awb_giveup
        _gave_up = await awb_giveup.gave_up_ids(db, [o.id for o in orders])
        if _gave_up:
            orders = [o for o in orders if o.id not in _gave_up]
            logger.info("auto-awb %s: sar %d comenzi abandonate/la CS", store_domain, len(_gave_up))
    except Exception as e:
        logger.info("auto-awb: gave_up_ids indisponibil (%s) — continui fără filtru", e)
    if not orders:
        return {"eligible": 0, "skipped_gave_up": True}

    # Resolve a profile id → (account_key, options) once per pass.
    resolved: Dict[int, Any] = {}
    async def _resolve(pid: int):
        if pid not in resolved:
            resolved[pid] = await _profile_base(db, store, pid)
        return resolved[pid]

    created, errors, no_route, waiting_multi, blocked = [], [], 0, 0, 0
    by_account: Dict[str, list] = {}
    # Lucrăm pe ID-uri, nu pe instanțele din listă: la primul eșec facem `db.rollback()`, iar rollback-ul
    # EXPIRĂ toate instanțele sesiunii. Următoarea comandă ar arunca MissingGreenlet la simpla citire a unui
    # atribut (refresh sincron în sesiune async). Reîncărcăm comanda curată la fiecare iterație.
    order_ids = [o.id for o in orders]
    for _oid in order_ids:
        o = (await db.execute(
            select(models.Order)
            .options(selectinload(models.Order.line_items), selectinload(models.Order.store),
                     selectinload(models.Order.shipments))
            .where(models.Order.id == _oid)
        )).scalar_one_or_none()
        if o is None:
            continue
        # Precedence: a manually-assigned profile > a matching rule > the store default profile.
        pid = getattr(o, "assigned_profile_id", None) or shipment_rules.pick_profile_id(o, rules) or default_profile_id
        try:
            if pid:
                account_key, base_opts = await _resolve(pid)
                opts = dict(base_opts)
            elif default_account:
                account_key, opts = default_account, {}
            else:
                no_route += 1
                continue  # no rule matched and no default courier — leave it for a human
            # Per-order parcel count (remembered in OH, synced from the Shopify metafield) overrides the
            # profile's default_parcels — so a 3-box order ships as 3 parcels even on a 1-parcel profile.
            # Marcat EXPLICIT: altfel packing.apply_to_options îl SUPRASCRIE cu regula de packing a
            # magazinului, iar numărul per-comandă (metafield-ul depozitului / harta SKU→cutii) se pierde
            # tăcut. Ordinea din cron e: metafield per-comandă > cutii-per-SKU > 1.
            if getattr(o, "parcel_count", None):
                opts["parcels_count"] = int(o.parcel_count)
                opts["_explicit"] = list(set(list(opts.get("_explicit") or []) + ["parcels_count"]))
            # SAFETY: never auto-ship an order that spans multiple fulfillment LOCATIONS from a
            # single AWB (that would ship everything from one location). Flag it to CS and wait
            # for a human to split it / add a location rule. Fail-soft: a check error still ships.
            gate = await _shopify_gate(o.store or store, o)
            if not gate["ok"]:
                if gate["multi"]:
                    from routes.cs_queue import enqueue_order
                    await enqueue_order(db, store, o, reason="manual",
                                        detail="Multiple locations — needs a split or a location rule",
                                        created_by="auto")
                    await db.commit()
                    waiting_multi += 1
                else:
                    # ON_HOLD / CLOSED / CANCELLED în Shopify: nu e treaba noastră s-o expediem, și nu e
                    # nici anomalie de raportat la CS — cineva a decis asta deliberat. Reflectăm hold-ul
                    # local ca UI-ul să nu mai mintă; restul se așază la reconcilierea de fantome.
                    blocked += 1
                    if "ON_HOLD" in gate["reason"] and not getattr(o, "is_on_hold_shopify", False):
                        o.is_on_hold_shopify = True
                        await db.commit()
                    logger.info("auto-awb: sar %s — Shopify zice %s", o.name, gate["reason"])
                continue
            r = await _create_one(db, store, o, account_key, opts)
            await db.commit()
            created.append(r["awb"])
            by_account.setdefault(account_key, []).append(r["awb"])
            # Contorul de eșecuri se ZEROIZEAZĂ la succes — altfel mecanismul e o capcană cu sens unic:
            # eșecurile de acum două săptămâni s-ar aduna peste cele de azi și comanda ar fi abandonată
            # deși de fapt merge. Cablat în ACEEAȘI schimbare cu on_failure (avertismentul review-ului).
            try:
                from services import awb_giveup
                await awb_giveup.reset(db, o)
            except Exception:
                pass
        except Exception as e:
            await db.rollback()
            errors.append(str(e))
            # Decizia completă după un eșec: clasifică (tranzitoriu/permanent/config), incrementează
            # contorul și, la prag, predă comanda la CS în loc s-o reîncerce la infinit.
            try:
                from services import awb_giveup
                await awb_giveup.on_failure(db, store, o, e)
                await db.commit()
            except Exception as ge:
                await db.rollback()
                logger.info("auto-awb: giveup a picat pt %s: %s", getattr(o, "name", "?"), ge)

    # One pickup request per courier account (an order routed to DPD and another to FAN each get theirs).
    _store = await db.get(models.Store, store_id) or store     # instanță curată după eventuale rollback-uri
    for acct_key, awbs in by_account.items():
        try:
            await _request_pickup(db, _store, acct_key, awbs, {})
        except Exception:
            pass
    logger.info("auto-awb %s: created=%d errors=%d no-route=%d waiting-multi=%d blocate-de-shopify=%d",
                store_domain, len(created), len(errors), no_route, waiting_multi, blocked)
    return {"created": len(created), "errors": len(errors), "no_route": no_route,
            "waiting_multi_location": waiting_multi, "blocked_by_shopify": blocked}


async def _shopify_gate(store, order) -> Dict[str, Any]:
    """ADEVĂRUL DESPRE COMANDĂ, CERUT LUI SHOPIFY ÎN MOMENTUL EXPEDIERII — nu din starea locală.

    Starea locală se învechește tăcut: OH află de hold-uri și închideri din webhook, iar un webhook
    pierdut nu se recuperează singur. Verificat pe MagDeal înainte de a-l porni: din 8 comenzi pe care
    OH le credea expediabile, 4 aveau fulfillment-ul ÎNCHIS și 2 erau pe HOLD în Shopify — iar OH le
    avea pe toate cu `is_on_hold_shopify=false`. Adică poarta de hold, care există tocmai ca să
    OPREASCĂ expedierea, era oarbă exact la comenzile pe care cineva le pusese deliberat pe hold.

    Un singur apel GraphQL, făcut doar pentru comenzile pe care chiar urmează să le expediem (1-2 pe
    tură), răspunde la toate trei întrebările: mai e ceva de expediat, e pe hold, sunt mai multe
    locații. Fail-soft peste tot: o eroare de verificare nu blochează expedierea (altfel o pană la
    Shopify ar opri tot depozitul).
    """
    out: Dict[str, Any] = {"ok": True, "reason": "", "multi": False}
    if not getattr(order, "shopify_order_id", None):
        return out
    try:
        from services import shopify_service
        groups = await shopify_service.get_fulfillment_order_groups(store, order.shopify_order_id)
    except Exception as e:
        logger.info("poarta Shopify a picat pt %s: %s — las comanda să meargă", getattr(order, "name", "?"), e)
        return out
    if not groups:
        return out                              # fără date ≠ dovadă că e închisă
    open_groups = [g for g in groups if g.get("open")]
    if not open_groups:
        st = ",".join(sorted({(g.get("status") or "?") for g in groups}))
        return {"ok": False, "reason": st, "multi": False}
    if len({(g.get("location") or "") for g in open_groups}) > 1:
        return {"ok": False, "reason": "multi-location", "multi": True}
    return out


async def run_all() -> Dict[str, Any]:
    """One pass over every store that has auto-AWB enabled. Own DB session."""
    total = {"stores": 0, "created": 0, "errors": 0}
    async with AsyncSessionLocal() as db:
        stores = (await db.execute(
            select(models.Store).where(models.Store.is_active.is_(True),
                                       models.Store.auto_awb_enabled.is_(True))
        )).scalars().all()
        for s in stores:
            res = await run_store(db, s)
            total["stores"] += 1
            total["created"] += res.get("created", 0)
            total["errors"] += res.get("errors", 0)
    return total


# ── bucla proprie ──────────────────────────────────────────────────────────────────────────────
# Auto-AWB avea nevoie de RITMUL ei, nu de al pollingului de status. În bucla comună (900s) o comandă
# aștepta delay-ul ei de 5 min PLUS până la 15 min până la următoarea tură — până la 20 de minute,
# mai lent decât cronul pe care OH îl înlocuiește. Măsurat în producție: GEN17603, 22 de minute
# neexpediată. Lock propriu, deci cele două bucle nu se mai blochează una pe alta.
_AWB_LOCK_KEY = 0x4157424C  # "AWBL"


async def run_forever(interval_sec: int = 300) -> None:
    import asyncio
    from database import engine

    logger.info("auto-awb loop pornit (interval=%ss)", interval_sec)
    await asyncio.sleep(30)     # lasă pornirea (webhook reconcile, view-uri) să se așeze
    while True:
        try:
            async with engine.connect() as conn:
                got = (await conn.execute(
                    text("SELECT pg_try_advisory_lock(:k)"), {"k": _AWB_LOCK_KEY})).scalar()
                if got:
                    try:
                        res = await run_all()
                        if res.get("created") or res.get("errors"):
                            logger.info("auto-awb pass: %s", res)
                    finally:
                        await conn.execute(
                            text("SELECT pg_advisory_unlock(:k)"), {"k": _AWB_LOCK_KEY})
        except Exception:
            logger.exception("auto-awb loop cycle failed")
        await asyncio.sleep(interval_sec)
