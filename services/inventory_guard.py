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

DEFAULTS = {"enabled": False, "threshold": 50, "recipients": [], "hysteresis_pct": 20,
            "rules": [], "categories": []}
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


def _emails(raw: Any) -> List[str]:
    lst = raw if isinstance(raw, list) else str(raw or "").replace(";", ",").split(",")
    return [a.strip() for a in lst if a and "@" in str(a)]


def sanitize_rules(raw: Any) -> List[Dict[str, Any]]:
    """Curăță regulile din UI: {store?, category?, sku?, threshold, recipients?}.

    Ținta poate fi un magazin, o CATEGORIE de magazine (ex. „parfumuri") sau un produs. `recipients`
    = oameni care primesc ÎN PLUS față de destinatarii generali, doar pentru ce prinde regula asta.
    """
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
        cat = (str(r.get("category") or "")).strip()
        # O regulă acoperă o LISTĂ de produse, nu unul singur: „magazinul X, aceste 20 de SKU-uri,
        # pragul N" e o singură regulă, nu douăzeci. Acceptăm și forma veche cu un `sku`.
        raw_skus = r.get("skus")
        if raw_skus is None:
            raw_skus = [r.get("sku")] if r.get("sku") else []
        if isinstance(raw_skus, str):
            raw_skus = raw_skus.replace(";", ",").replace("\n", ",").split(",")
        skus = sorted({str(x).strip().lower() for x in (raw_skus or []) if str(x).strip()})
        if not store and not cat and not skus:
            continue          # o regulă fără țintă e pragul implicit, care se setează separat
        out.append({"store": store, "category": cat, "skus": skus, "threshold": thr,
                    "recipients": _emails(r.get("recipients"))})
    return out


def sanitize_categories(raw: Any) -> List[Dict[str, Any]]:
    """O categorie = CE MAGAZINE intră la socoteală × CE PRODUSE acoperă.

    Ambele liste sunt opționale, și fiecare combinație are sens:
      stores=[Esteban, GT, Lab Noir, Nubra], skus=[]   → „parfumuri" ca grup de magazine
      stores=[], skus=[ZN-78, GT-35, …]                → parfumuri ANUME, pe tot grupul
      ambele                                           → acele produse, doar pe acele magazine
    Fără niciuna, categoria n-ar însemna nimic, deci o sărim.
    """
    out: List[Dict[str, Any]] = []
    for c in (raw if isinstance(raw, list) else []):
        if not isinstance(c, dict):
            continue
        nm = (str(c.get("name") or "")).strip()
        stores = [str(x).strip() for x in (c.get("stores") or []) if str(x).strip()]
        skus = [str(x).strip().lower() for x in (c.get("skus") or []) if str(x).strip()]
        if nm and (stores or skus):
            out.append({"name": nm, "stores": stores, "skus": skus})
    return out


def _category_map(cfg: Dict[str, Any]) -> Dict[str, Dict[str, List[str]]]:
    return {c["name"].lower(): {"stores": c["stores"], "skus": c["skus"]}
            for c in sanitize_categories(cfg.get("categories"))}


def _pick(rules: List[Dict[str, Any]], sku: str, *, store: Optional[str],
          category: Optional[str]) -> Optional[Dict[str, Any]]:
    """Regula care se aplică pe un ANUME nivel de măsurare, cea mai specifică dintre cele potrivite.

    Nivelurile nu se amestecă, fiindcă măsoară lucruri diferite: „total" e marfa din tot grupul,
    „categorie" e suma unui subset de magazine, „magazin" e o singură felie. O regulă scrisă pentru
    un magazin n-are ce căuta pe total — acolo ar însemna altceva decât a cerut merchantul.
    La egalitate de nivel, regula cu produs bate regula fără produs.
    """
    best, best_rank = None, -1
    for r in rules:
        r_store, r_cat = (r.get("store") or ""), (r.get("category") or "")
        r_skus = r.get("skus") or []
        if store is not None:
            if not r_store or r_store.lower() != store.lower():
                continue
        elif category is not None:
            if not r_cat or r_cat.lower() != category.lower():
                continue
        else:
            if r_store or r_cat:
                continue
        if r_skus and sku not in r_skus:
            continue
        rank = 1 if r_skus else 0
        if rank > best_rank:
            best, best_rank = r, rank
    return best


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
        cats = _category_map(cfg)
        base_rcpt = _emails(cfg.get("recipients"))

        for sku, info in stock.items():
            out["checked"] += 1
            name = info.get("name") or sku
            per_store = info.get("stores") or {}

            # Ce verificăm pentru produsul ăsta: (cheie, etichetă, cantitate, prag, destinatari-extra)
            checks = []

            # 1. TOTALUL din grup — pragul produsului dacă are regulă proprie, altfel cel implicit
            r_tot = _pick(rules, sku, store=None, category=None)
            checks.append(("total", None, int(info["total"]),
                           default_thr if r_tot is None else r_tot["threshold"],
                           (r_tot or {}).get("recipients") or []))

            # 2. CATEGORII — suma feliilor magazinelor din categorie. O categorie se măsoară ca GRUP,
            #    nu magazin cu magazin: altfel „parfumuri sub 50" ar da 4 alerte pentru același produs.
            for cname, cdef in cats.items():
                if cdef["skus"] and sku not in cdef["skus"]:
                    continue                      # categoria acoperă produse anume, ăsta nu e printre ele
                r_cat = _pick(rules, sku, store=None, category=cname)
                if r_cat is None:
                    continue                      # categoria există, dar n-are regulă → nu verificăm
                members = [s for s in cdef["stores"]] or list(per_store.keys())
                qty = sum(per_store.get(m, 0) for m in members)
                if not any(m in per_store for m in members):
                    continue                      # produsul nu se vinde pe magazinele categoriei
                checks.append(("cat:" + cname, cname, int(qty), r_cat["threshold"],
                               r_cat.get("recipients") or []))

            # 3. MAGAZINE — doar unde merchantul a scris o regulă anume pe magazinul ăla.
            for store_label, qty in per_store.items():
                r_st = _pick(rules, sku, store=store_label, category=None)
                if r_st is None:
                    continue
                checks.append((store_label, store_label, int(qty), r_st["threshold"],
                               r_st.get("recipients") or []))

            for key_scope, label, qty, thr, extra in checks:
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
                        {"k": key, "s": sku, "st": label, "n": name[:300],
                         "q": qty, "t": thr, "now": now})
                    fresh.append({"name": name, "sku": sku, "qty": qty, "threshold": thr,
                                  "scope": label or "total",
                                  "to": sorted(set(base_rcpt) | set(extra))})
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
        # Un email per SET DE DESTINATARI, nu unul singur către toți: regula pe „parfumuri" poate
        # avea alt om decât cea pe Grandia, iar oamenii ăia n-au de ce să vadă restul catalogului.
        groups: Dict[tuple, List[Dict[str, Any]]] = {}
        for r in fresh:
            groups.setdefault(tuple(r.get("to") or []), []).append(r)
        sent = 0
        for to, rows in groups.items():
            if to and _send_digest(rows, cfg, list(to)):
                sent += 1
        out["emailed"] = sent > 0
        out["emails"] = sent
    if out["new_alerts"] or out["cleared"]:
        logger.info("garda-stoc: %s", out)
    return out


def _send_digest(rows: List[Dict[str, Any]], cfg: Dict[str, Any],
                 to: Optional[List[str]] = None) -> bool:
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
                       to if to is not None else (cfg.get("recipients") or []), body_text=txt)
