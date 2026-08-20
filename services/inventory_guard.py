"""Gardă de stoc: alertă pe email când un produs scade sub prag.

DE CE nu citim stocul dintr-un singur magazin: aceeași marfă e listată pe mai multe magazine, iar
fiecare arată o BUCATĂ din ea. HA-1193-1 arată 294 pe MagDeal, 92 pe CasaOfertelor și câte 91 pe
încă două — niciuna nu e adevărul, iar o gardă pe stocul per magazin ar alerta pentru marfă care
există și ar rata marfă care chiar se termină.

Stocul real = SUMA pe magazinele Shopify: 294+92+91+91 = 568, exact cât raportează și stock-sync ca
`shopify_total`.

DE CE nu luăm `globalAvailable` de la masterul stock-sync, deși pare mai autoritar: el include ȘI
Trendyol, unde 196 din 200 de listinguri sunt INACTIVE (draft). Pentru HA-1193-1 masterul zice 572
față de 568 real — 4 bucăți fantomă. Diferența e mică aici, dar merge în direcția periculoasă:
umflă stocul, deci ASCUNDE o lipsă. Însumând magazinele Shopify, Trendyol iese din calcul prin
construcție, fără să depindem de starea listingurilor lui.

Excludem și produsele Shopify DRAFT/ARCHIVED: nu se vând, deci n-au ce alerta.

PRAG: al organizației (implicit 50).

ANTI-SPAM cu HISTEREZĂ: alertăm o singură dată la TRECEREA sub prag. Alerta se re-armează abia când
stocul urcă înapoi peste prag + marjă (implicit +20%). Fără marjă, un produs care oscilează în jurul
pragului (49 → 51 → 49) ar trimite mail la fiecare tură.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy import text

from database import AsyncSessionLocal

logger = logging.getLogger(__name__)

DEFAULTS = {"enabled": False, "threshold": 50, "recipients": [], "hysteresis_pct": 20}
_VARIANTS_Q = """
query($cursor: String) {
  productVariants(first: 250, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { sku inventoryQuantity product { title status } }
  }
}
"""
_MAX_PAGES = 40          # 10.000 variante/magazin; plasă împotriva unui cursor care nu se termină


async def _settings(db) -> Dict[str, Any]:
    row = (await db.execute(text(
        "select settings from hub_settings where store_id is null and organization_id is null limit 1"
    ))).scalar()
    cfg = dict(DEFAULTS)
    cfg.update(((row or {}).get("inventory_guard") or {}) if isinstance(row, dict) else {})
    return cfg


async def _stock_by_sku() -> Dict[str, Dict[str, Any]]:
    """{sku: {qty, name}} — suma stocului pe TOATE magazinele Shopify active. Produsele draft/archived
    și variantele fără SKU sunt sărite."""
    from sqlalchemy import select as _sel
    from services import shopify_service
    import models as _m

    out: Dict[str, Dict[str, Any]] = {}
    async with AsyncSessionLocal() as db:
        stores = (await db.execute(_sel(_m.Store).where(
            _m.Store.is_active.is_(True),
            ~_m.Store.domain.like("%orderhub-test.invalid"),
            ~_m.Store.domain.like("test-%")))).scalars().all()
    for st in stores:
        cursor = None
        for _ in range(_MAX_PAGES):
            try:
                d = await shopify_service._gql(st, _VARIANTS_Q, {"cursor": cursor})
            except Exception as e:
                logger.info("garda-stoc: %s a picat (%s)", st.domain, e)
                break
            pv = d.get("productVariants") or {}
            for n in (pv.get("nodes") or []):
                sku = (n.get("sku") or "").strip().lower()
                qty = n.get("inventoryQuantity")
                prod = n.get("product") or {}
                if not sku or qty is None:
                    continue
                if str(prod.get("status") or "").upper() in ("DRAFT", "ARCHIVED"):
                    continue
                e = out.setdefault(sku, {"qty": 0, "name": prod.get("title") or sku})
                e["qty"] += int(qty)
            info = pv.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            cursor = info.get("endCursor")
    return out


async def run_once() -> Dict[str, Any]:
    out = {"checked": 0, "low": 0, "new_alerts": 0, "cleared": 0, "emailed": False}
    async with AsyncSessionLocal() as db:
        cfg = await _settings(db)
        if not cfg.get("enabled"):
            return {**out, "skipped": "off"}
        stock = await _stock_by_sku()
        if not stock:
            return {**out, "skipped": "n-am putut citi stocul din Shopify"}

        # LINIE DE BAZĂ la prima pornire. Pragul de 50 prinde ACUM peste jumătate din catalog (mare
        # parte produse la zero, scoase din vânzare). Un prim mail cu o mie de rânduri n-ar fi o
        # alertă, ar fi un inventar pe care nimeni nu-l citește, iar alertele reale de a doua zi
        # s-ar pierde în el. Deci la prima rulare doar ÎNREGISTRĂM starea, fără email.
        seeding = not (await db.execute(text("select 1 from inventory_alerts limit 1"))).first()
        hyst = 1 + (float(cfg.get("hysteresis_pct") or 0) / 100.0)
        thr = int(cfg.get("threshold") or 50)
        known = {r[0] for r in (await db.execute(text(
            "select sku from inventory_alerts where cleared_at is null"))).all()}
        fresh: List[Dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        for sku, info in stock.items():
            out["checked"] += 1
            qty = int(info["qty"])
            if qty < thr:
                out["low"] += 1
                if sku in known:
                    continue                     # deja alertat, încă sub prag → tăcere
                await db.execute(text("""
                    insert into inventory_alerts (sku, name, qty, threshold, alerted_at)
                    values (:s, :n, :q, :t, :now)
                    on conflict (sku) do update set qty = :q, threshold = :t,
                        alerted_at = :now, cleared_at = null"""),
                    {"s": sku, "n": (info.get("name") or "")[:300], "q": qty, "t": thr, "now": now})
                fresh.append({"name": info.get("name") or sku, "sku": sku, "qty": qty, "threshold": thr})
                out["new_alerts"] += 1
            elif sku in known and qty >= thr * hyst:
                # re-armare: a urcat peste prag CU MARJĂ, deci nu mai oscilează în jurul lui
                await db.execute(text("update inventory_alerts set cleared_at = :now where sku = :s"),
                                 {"now": now, "s": sku})
                out["cleared"] += 1
        await db.commit()

    if seeding:
        out["baseline"] = out["new_alerts"]
        out["new_alerts"] = 0
        logger.info("garda-stoc: linie de bază — %d produse deja sub prag, înregistrate FĂRĂ email. "
                    "De acum alertăm doar la trecerile noi.", out["baseline"])
    elif fresh:
        out["emailed"] = _send_digest(fresh, cfg)
    if out["new_alerts"] or out["cleared"]:
        logger.info("garda-stoc: %s", out)
    return out


def _send_digest(rows: List[Dict[str, Any]], cfg: Dict[str, Any]) -> bool:
    from services import mailer
    rows = sorted(rows, key=lambda r: r["qty"])
    trs = "".join(
        "<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'>%s</td>"
        "<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:right;font-weight:600;color:%s'>%d</td>"
        "<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:right;color:#666'>%d</td>"
        "<td style='padding:6px 10px;border-bottom:1px solid #eee;color:#666'>%s</td></tr>"
        % (r["name"], "#b42318" if r["qty"] == 0 else "#b54708", r["qty"], r["threshold"], r["sku"].upper())
        for r in rows)
    html = (
        "<div style='font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#111'>"
        "<h2 style='margin:0 0 4px'>Stoc sub prag: %d produse</h2>"
        "<p style='margin:0 0 16px;color:#555'>Cantitatea e stocul TOTAL din depozit (suma pe toate magazinele Shopify), "
        "nu ce arată un singur magazin. Trendyol nu e inclus — listingurile lui sunt draft.</p>"
        "<table style='border-collapse:collapse;font-size:14px'>"
        "<tr><th style='text-align:left;padding:6px 10px;border-bottom:2px solid #ddd'>Produs</th>"
        "<th style='text-align:right;padding:6px 10px;border-bottom:2px solid #ddd'>Stoc</th>"
        "<th style='text-align:right;padding:6px 10px;border-bottom:2px solid #ddd'>Prag</th>"
        "<th style='text-align:left;padding:6px 10px;border-bottom:2px solid #ddd'>SKU</th></tr>"
        "%s</table>"
        "<p style='margin:16px 0 0;color:#777;font-size:12px'>Primești un singur mail per produs, la "
        "trecerea sub prag. Alertă nouă doar după ce stocul urcă înapoi peste prag.</p></div>"
        % (len(rows), trs))
    txt = "\n".join("%s — %d buc (prag %d)" % (r["name"], r["qty"], r["threshold"]) for r in rows)
    return mailer.send("Stoc sub prag: %d produse" % len(rows), html,
                       cfg.get("recipients") or [], body_text=txt)
