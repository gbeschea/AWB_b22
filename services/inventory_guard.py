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
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from database import AsyncSessionLocal

logger = logging.getLogger(__name__)

DEFAULTS = {"enabled": False, "threshold": 50, "recipients": [], "hysteresis_pct": 20, "rules": []}
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
    """{sku: {name, total, stores:{nume_magazin: qty}}} — stocul pe magazinele Shopify active.

    Păstrăm ȘI defalcarea pe magazine, nu doar totalul, fiindcă o regulă „per magazin" nu se poate
    măsura pe total: stocul e împărțit în felii, iar felia magazinului e singurul lucru care are
    sens acolo (dacă MagDeal a rămas cu 5 bucăți, contează, chiar dacă în grup mai sunt 500).
    """
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
        label = st.name or st.domain
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
                e = out.setdefault(sku, {"total": 0, "name": prod.get("title") or sku, "stores": {}})
                e["total"] += int(qty)
                e["stores"][label] = e["stores"].get(label, 0) + int(qty)
            info = pv.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            cursor = info.get("endCursor")
    return out


def sanitize_rules(raw: Any) -> List[Dict[str, Any]]:
    """Curăță regulile venite din UI: {store?, sku?, threshold}. `store`/`sku` goale = „oricare"."""
    out: List[Dict[str, Any]] = []
    for r in (raw if isinstance(raw, list) else []):
        if not isinstance(r, dict):
            continue
        try:
            thr = int(r.get("threshold"))
        except Exception:
            continue
        if thr < 0:
            continue
        store = (str(r.get("store") or "")).strip()
        sku = (str(r.get("sku") or "")).strip().lower()
        if not store and not sku:
            continue          # o regulă fără țintă e pragul implicit, care se setează separat
        out.append({"store": store, "sku": sku, "threshold": thr})
    return out


def _store_rule(rules: List[Dict[str, Any]], sku: str, store: str) -> Optional[int]:
    """Pragul pentru FELIA unui magazin — DOAR din reguli care numesc explicit magazinul.

    Fără regulă explicită nu alertăm per magazin. Altfel, un produs împărțit în 8 felii mici ar
    declanșa 8 alerte pentru marfă care, în grup, e suficientă — exact zgomotul pe care garda
    trebuie să-l evite. Între regulile care numesc magazinul, cea cu produs bate cea generală.
    """
    best, best_rank = None, -1
    for r in rules:
        r_store, r_sku = (r.get("store") or ""), (r.get("sku") or "")
        if not r_store or r_store.lower() != (store or "").lower():
            continue
        if r_sku and r_sku != sku:
            continue
        rank = 1 if r_sku else 0
        if rank > best_rank:
            best, best_rank = r["threshold"], rank
    return best


def _rule_for(rules: List[Dict[str, Any]], sku: str, store: Optional[str]) -> Optional[int]:
    """Pragul pe TOTALUL din grup: doar reguli care vizează produsul, fără magazin.
    O regulă cu magazin nu se aplică pe total — acolo felia magazinului e unitatea de măsură."""
    for r in rules:
        if (r.get("store") or ""):
            continue
        if (r.get("sku") or "") == sku:
            return r["threshold"]
    return None


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
        default_thr = int(cfg.get("threshold") or 50)
        known = {r[0] for r in (await db.execute(text(
            "select key from inventory_alerts where cleared_at is null"))).all()}
        fresh: List[Dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        rules = sanitize_rules(cfg.get("rules"))

        def _record(key, store_label, sku, name, qty, thr):
            """Alertă nouă doar dacă nu e deja deschisă pe ACEEAȘI cheie. Cheia include magazinul,
            ca o regulă pe MagDeal să nu tacă din cauza unei alerte deschise pe total (și invers)."""
            if key in known:
                return False
            fresh.append({"name": name, "sku": sku, "qty": qty, "threshold": thr,
                          "scope": store_label or "total"})
            return True

        for sku, info in stock.items():
            out["checked"] += 1
            name = info.get("name") or sku

            # 1. pe TOTALUL din grup — pragul produsului dacă are regulă, altfel cel implicit
            thr_total = _rule_for(rules, sku, None)
            thr_total = default_thr if thr_total is None else thr_total
            checks = [("total", None, int(info["total"]), thr_total)]

            # 2. pe FELIA fiecărui magazin — DOAR dacă merchantul a scris o regulă pentru magazinul
            #    ăla. Fără regulă explicită nu alertăm per magazin: altfel un produs împărțit în 8
            #    felii mici ar declanșa 8 alerte pentru marfă care în grup e suficientă.
            for store_label, qty in (info.get("stores") or {}).items():
                thr_s = _store_rule(rules, sku, store_label)
                if thr_s is not None:
                    checks.append((store_label, store_label, int(qty), thr_s))

            for key_scope, store_label, qty, thr in checks:
                key = "%s|%s" % (key_scope, sku)
                if qty < thr:
                    out["low"] += 1
                    if key in known:
                        continue
                    await db.execute(text("""
                        insert into inventory_alerts (key, sku, store, name, qty, threshold, alerted_at)
                        values (:k, :s, :st, :n, :q, :t, :now)
                        on conflict (key) do update set qty = :q, threshold = :t,
                            alerted_at = :now, cleared_at = null"""),
                        {"k": key, "s": sku, "st": store_label, "n": name[:300],
                         "q": qty, "t": thr, "now": now})
                    _record(key, store_label, sku, name, qty, thr)
                    out["new_alerts"] += 1
                elif key in known and qty >= thr * hyst:
                    await db.execute(text(
                        "update inventory_alerts set cleared_at = :now where key = :k"),
                        {"now": now, "k": key})
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
        % (r["name"] + ("" if r.get("scope") in (None, "total") else
                         " <span style='color:#888'>(%s)</span>" % r["scope"]),
           "#b42318" if r["qty"] == 0 else "#b54708", r["qty"], r["threshold"], r["sku"].upper())
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
