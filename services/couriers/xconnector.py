# services/couriers/xconnector.py
"""
xConnector ca CONNECTOR de curierat + facturare în Order Hub — puntea prin care OH preia FULLY
cronul: OH decide (validare/dedup/colete/surpriză/COD), xConnector execută (AWB + facturi), exact
ca în producția de azi. Paritate prin construcție: aceleași endpoint-uri ca xconnector.py (cron):

  GET  /api/orders/by-id?orderId=<shopify_numeric_id>      → comanda xConnector (orderId, documents)
  GET  /api/orders/by-tracking-number?trackingNumber=<awb>  → comanda după AWB (pt void/label)
  GET  /api/merchant/connectors                             → curieri + facturare (SMART_BILL)
  POST /api/actions/create-shipping-label  {orderId, connectorId, parcelCount, parcelType, notifyCustomer}
  POST /api/actions/cancel-shipping-label  {orderId, connectorId?}
  POST /api/actions/create-invoice | cancel-invoice | revert-invoice  {orderId, connectorId, refundId?, languageCode?}

Auth = cheia API xConnector a MAGAZINULUI (Bearer), ținută în CourierAccount.credentials (criptat):
  {"api_key": "...", "shop_domain": "...", "connector_id": opțional (default: singurul activ / DPD),
   "billing_connector_id": opțional (default: singurul SMART_BILL activ)}
Un cont per magazin: account_key = "xconnector-<slug-domeniu>". Gărzile din cron sunt păstrate:
nu creez peste un AWB existent (awb-regen e anulare+refacere explicită), erorile xConnector se
întorc lizibil (errorMessage/errorDescription).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

import models
from .base import BaseCourier, TrackingResponse, VoidResponse

logger = logging.getLogger(__name__)

XBASE = "https://xconnector.app"


class XConnectorCourier(BaseCourier):
    name = "xconnector"
    display_name = "xConnector (punte AWB + facturi)"
    # xConnector fulfill-uiește SINGUR comanda în Shopify după ce face eticheta — OH nu mai împinge.
    owns_shopify_fulfillment = True

    # ── HTTP primitives ──
    def _headers(self, creds: Dict[str, Any]) -> Dict[str, str]:
        return {"Authorization": "Bearer " + (creds.get("api_key") or ""), "Content-Type": "application/json"}

    async def _get(self, creds: Dict[str, Any], path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        r = await self.http.get(XBASE + path, params=params, headers=self._headers(creds))
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text

    async def _post(self, creds: Dict[str, Any], path: str, body: Dict[str, Any]) -> Any:
        r = await self.http.post(XBASE + path, json=body, headers=self._headers(creds))
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text

    @staticmethod
    def _err(d: Any) -> str:
        if isinstance(d, dict):
            return d.get("errorDescription") or d.get("errorMessage") or d.get("errorCode") or str(d)[:200]
        return str(d)[:200]

    # ── comenzi & connectori ──
    async def xc_order_by_shopify_id(self, creds: Dict[str, Any], shopify_order_id: str) -> Dict[str, Any]:
        s, d = await self._get(creds, "/api/orders/by-id", {"orderId": str(shopify_order_id)})
        return d if s == 200 and isinstance(d, dict) else {}

    async def xc_order_by_tracking(self, creds: Dict[str, Any], awb: str) -> Dict[str, Any]:
        s, d = await self._get(creds, "/api/orders/by-tracking-number", {"trackingNumber": awb})
        return d if s == 200 and isinstance(d, dict) else {}

    # Lista de connectori e IDENTICĂ pentru toate comenzile aceluiași magazin și se schimbă foarte rar,
    # dar era cerută de 2 ori PER COMANDĂ (o dată pt rutarea de locker, o dată pt alegerea default).
    # La o tură de 18 comenzi = 36 de apeluri în rafală → xConnector limitează și întoarce gol, iar OH
    # raporta „niciun connector activ" pe TOATE comenzile (măsurat la primul canary). Cache scurt per cheie.
    _CONN_CACHE: Dict[str, tuple] = {}
    _CONN_TTL = 300.0

    async def connectors(self, creds: Dict[str, Any]) -> List[Dict[str, Any]]:
        import time as _t
        key = creds.get("api_key") or ""
        hit = self._CONN_CACHE.get(key)
        if hit and hit[1] > _t.time():
            return hit[0]
        s, d = await self._get(creds, "/api/merchant/connectors")
        if s == 200 and isinstance(d, list) and d:
            self._CONN_CACHE[key] = (d, _t.time() + self._CONN_TTL)
            return d
        if hit:                       # API supărat (429/5xx) → mai bine lista veche decât „niciun connector"
            logger.info("connectors: raspuns gol/eroare (%s) — folosesc lista din cache", s)
            return hit[0]
        return []

    @staticmethod
    def _doc(o: Dict[str, Any], doc_type: str) -> Optional[Dict[str, Any]]:
        for d in (o.get("documents") or []):
            if isinstance(d, dict) and d.get("documentType") == doc_type:
                return d
        return None

    # ── sanitizare INTL pentru DPD (paritate cu cronul; vezi services/couriers/dpd_intl.py) ──
    async def ai_correct_address(self, creds: Dict[str, Any], o: Dict[str, Any],
                                 corr: Dict[str, Any], cc: str) -> bool:
        """Scrie corecțiile de format în comanda xConnector via ai-correct-address. True dacă 200."""
        from . import dpd_intl
        oid = o.get("orderId")
        ad = dict(o.get("shippingAddress") or {})
        if not oid or not ad or not corr:
            return False
        # `province`/`country` sunt în listă pentru corecțiile RO (address_repair): judeţul greşit e una
        # dintre cele mai frecvente cauze de addressStatus=WRONG, iar dacă nu-l scriem aici corecţia
        # pleacă mutilată (zip nou + judeţ vechi = adresă inconsistentă, tot respinsă). Pentru INTL
        # (dpd_intl) cheile astea nu apar niciodată în `corr`, deci comportamentul rămâne neschimbat.
        for k in ("city", "zip", "address1", "address2", "province", "country",
                  "firstName", "lastName", "phone"):
            if corr.get(k) is not None:
                ad[k] = corr[k]
        ad["zip"] = dpd_intl.canonical_zip(cc, ad.get("zip"))   # CZ/SK: PSČ „NNN NN" (punct unic)
        import hashlib
        digest = hashlib.sha1(repr(sorted(ad.items())).encode("utf-8", "replace")).hexdigest()[:12]
        body = {
            "orderId": oid,
            "idempotencyKey": "oh-intl-%s-%s" % (oid, digest),
            "appliedShippingAddress": ad,
            "expectedAddressHash": o.get("addressHash"),
            "expectedStatusHash": o.get("statusHash"),
            "expectedEvidenceHash": o.get("evidenceHash"),
            "agentClaimedConfidence": 0.95,
            "agentRationale": "National address nomenclature reconciliation + DPD field-format limits.",
            "modelName": "orderhub-intl-nomen", "mcpClientId": "orderhub",
        }
        s, d = await self._post(creds, "/api/orders/ai-correct-address", body)
        if s != 200:
            logger.info("xc ai-correct-address a picat (%s): %s", s, self._err(d))
        return s == 200

    async def sanitize_intl(self, creds: Dict[str, Any], o: Dict[str, Any], country_name: str) -> bool:
        """Pașii 1-5 din dpd_intl (ZIP/city/adresă/nume/telefon) + scrierea lor. True dacă a schimbat ceva."""
        from . import dpd_intl
        from services.nomenclator.intl import country_code
        cc = (country_code(country_name) or "").upper()
        if cc not in ("CZ", "PL", "BG", "HU", "SK"):
            return False
        ad = o.get("shippingAddress") or {}
        if not ad:
            return False
        corr = await dpd_intl.build_corrections(cc, ad, country_name)
        if not corr:
            return False
        ok = await self.ai_correct_address(creds, o, corr, cc)
        logger.info("INTL-sanitize order=%s cc=%s corr=%s -> %s",
                    o.get("orderName") or o.get("orderId"), cc, list(corr), "OK" if ok else "FAIL")
        return ok

    async def _pick_shipping_connector(self, creds: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Regula cronului: connectorul explicit din config, altfel singurul activ de curierat,
        altfel preferă DPD (default-ul producției — livrează și cross-border CZ/PL/BG/HU/SK)."""
        cid = creds.get("connector_id")
        cons = [c for c in await self.connectors(creds)
                if c.get("active") and (c.get("type") or "").upper() not in ("SMART_BILL",)]
        if cid:
            m = [c for c in cons if c.get("id") == cid]
            return m[0] if m else {"id": cid, "name": "config"}
        if len(cons) == 1:
            return cons[0]
        dpd = [c for c in cons if "dpd" in (c.get("name") or "").lower()]
        return dpd[0] if dpd else (cons[0] if cons else None)

    async def _pick_billing_connector(self, creds: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cid = creds.get("billing_connector_id")
        bills = [c for c in await self.connectors(creds)
                 if c.get("active") and (c.get("type") or "").upper() == "SMART_BILL"]
        if cid:
            m = [c for c in bills if c.get("id") == cid]
            return m[0] if m else {"id": cid, "name": "config"}
        return bills[0] if len(bills) == 1 else None

    # ── contractul BaseCourier ──
    async def create_awb(self, db: AsyncSession, order: models.Order, account_key: str, *,
                         options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        opts = options or {}
        creds = await self.get_credentials(db, account_key)
        if not creds or not creds.get("api_key"):
            raise RuntimeError("xConnector: contul '%s' nu are api_key." % account_key)
        if not order.shopify_order_id:
            return {"success": False, "message": "comanda nu are shopify_order_id"}
        o = await self.xc_order_by_shopify_id(creds, order.shopify_order_id)
        if not o.get("orderId"):
            return {"success": False, "message": "comanda nu există (încă) în xConnector"}
        if self._doc(o, "SHIPPING_LABEL"):
            return {"success": False, "message": "are DEJA AWB în xConnector — folosește void + create (regen)"}
        # Adresa marcată WRONG/UNKNOWN de xConnector = eticheta NU se poate face deloc (verificat live pe
        # NUBRA13391). Încercăm o reparație CONSERVATOARE înainte să cerem eticheta. Nu e „proactiv": aici
        # adresa e deja respinsă, deci n-avem ce strica — spre deosebire de sanitizarea intl, care se face
        # doar DUPĂ ce curierul refuză.
        if str(o.get("addressStatus") or "").upper() in ("WRONG", "UNKNOWN"):
            try:
                from . import address_repair
                rep = await address_repair.repair_address(self, creds, o, order, apply=True)
                if rep.get("changed"):
                    logger.info("repair-address order=%s -> %s", getattr(order, "name", "?"), rep.get("note"))
                    o = await self.xc_order_by_shopify_id(creds, order.shopify_order_id) or o
            except Exception as e:
                logger.info("repair-address a picat pt %s: %s", getattr(order, "name", "?"), e)
        # RUTARE: dacă clientul a ales pe storefront un punct de ridicare (easybox/locker), coletul TREBUIE
        # să plece pe connectorul acela — un AWB de livrare la domiciliu peste o comandă de locker înseamnă
        # colet rutat greșit. Cade pe default-ul magazinului când nu există alegere.
        con = None
        # NU rutăm comenzile de SCHIMB pe connectorul „DPD SWAP": un schimb nu se poate face prin
        # xConnector. Eticheta care ar ieși e o livrare simplă — coletul pleacă, produsul vechi rămâne
        # la client, iar operațiunea trebuie refăcută manual. Comenzile cu tag `swap` sunt oprite mai
        # devreme, de regula specială din automation_config, și merg la om.
        try:
            from . import locker_routing
            con = await locker_routing.pick_connector(self, creds, order, await self.connectors(creds))
        except Exception as e:
            logger.info("locker-routing a picat pt %s: %s", getattr(order, "name", "?"), e)
        if not con:
            con = await self._pick_shipping_connector(creds)
        if not con:
            return {"success": False, "message": "niciun connector de curierat activ pe cheia xConnector"}
        # `parcels_count` e cheia CANONICĂ în OH (o setează packing.apply_to_options și auto_awb_service din
        # order.parcel_count; toți ceilalți curieri o citesc — gls/sameday/fancourier/dpd). Conectorul ăsta citea
        # doar `parcels`/`parcelCount` → numărul memorat de detectorul de colete NU ajungea niciodată la
        # xConnector și ORICE comandă pleca cu 1 colet, tăcut. Acceptăm toate trei, canonica prima.
        body = {"orderId": o["orderId"], "connectorId": con["id"],
                "parcelCount": max(int(opts.get("parcels_count") or opts.get("parcels")
                                       or opts.get("parcelCount") or 1), 1),
                "parcelType": opts.get("parcel_type") or "BOX",
                "notifyCustomer": bool(opts.get("notify", False))}
        s, d = await self._post(creds, "/api/actions/create-shipping-label", body)
        ok = s == 200 and isinstance(d, dict) and d.get("accepted")
        labels = (d.get("shippingLabels") or []) if isinstance(d, dict) else []
        good = [L for L in labels if L.get("success")]

        # INTL: sanitizare REACTIVĂ — DOAR după ce curierul a respins eticheta, apoi o singură reîncercare.
        # NU proactiv: o corecție AI scrisă pe o comandă care oricum ar fi plecat schimbă adresa clientului
        # degeaba și (lecția cronului) marchează comanda ca AI_CORRECTION, ceea ce face WPO/DPD să ceară
        # emailul OBLIGATORIU — fără pasul de email (neportat încă) ar bloca permanent comenzi CZ/PL care
        # mergeau. Cronul cheamă sanitizerul exact aici, pe eroarea primită, cu marker pe (comandă|motiv).
        country = (getattr(order, "shipping_country", None) or "")
        if not (ok and good) and country and not opts.get("_intl_retried"):
            try:
                if await self.sanitize_intl(creds, o, country):
                    o2 = await self.xc_order_by_shopify_id(creds, order.shopify_order_id) or o
                    body["orderId"] = o2.get("orderId", body["orderId"])
                    s, d = await self._post(creds, "/api/actions/create-shipping-label", body)
                    ok = s == 200 and isinstance(d, dict) and d.get("accepted")
                    labels = (d.get("shippingLabels") or []) if isinstance(d, dict) else []
                    good = [L for L in labels if L.get("success")]
                    logger.info("INTL-retry order=%s -> %s", getattr(order, "name", "?"),
                                "OK" if (ok and good) else "tot respins")
            except Exception as e:
                logger.info("INTL-sanitize a picat pt %s: %s", getattr(order, "name", "?"), e)

        if not (ok and good):
            msg = self._err(d) or (good and good[0].get("errorMessage")) or "respins"
            return {"success": False, "message": msg, "raw": d}
        L = good[0]
        return {"success": True, "awb": L.get("trackingNumber"), "tracking_number": L.get("trackingNumber"),
                "carrier": L.get("carrierName"), "label_url": L.get("shippingLabelUrl"),
                "price": L.get("price"), "connector_id": con["id"], "raw": d}

    async def void_awb(self, db: AsyncSession, awb: str, account_key: Optional[str] = None) -> VoidResponse:
        creds = await self.get_credentials(db, account_key)
        if not creds or not creds.get("api_key"):
            return VoidResponse(success=False, message="xConnector: cont fără api_key")
        o = await self.xc_order_by_tracking(creds, awb)
        if not o.get("orderId"):
            return VoidResponse(success=False, message="AWB negăsit în xConnector")
        body: Dict[str, Any] = {"orderId": o["orderId"]}
        doc = self._doc(o, "SHIPPING_LABEL")
        if doc and doc.get("connectorId"):
            body["connectorId"] = doc["connectorId"]
        s, d = await self._post(creds, "/api/actions/cancel-shipping-label", body)
        ok = s == 200 and isinstance(d, dict) and d.get("accepted")
        return VoidResponse(success=bool(ok), message=None if ok else self._err(d), raw=d)

    async def get_label(self, awb: str, creds: dict, paper_size: str = "A6") -> bytes:
        o = await self.xc_order_by_tracking(creds, awb)
        doc = self._doc(o, "SHIPPING_LABEL") or {}
        # Câmpul REAL întors de xConnector e `url` (verificat pe primul AWB creat de OH, GEN17599);
        # `shippingLabelUrl`/`fileUrl` apar doar în răspunsul de la create-shipping-label.
        url = doc.get("url") or doc.get("shippingLabelUrl") or doc.get("fileUrl")
        if not url:
            raise RuntimeError("xConnector: eticheta AWB %s nu are URL de PDF" % awb)
        r = await self.http.get(url, headers=self._headers(creds))
        r.raise_for_status()
        return r.content

    async def _invoice_pdf_url(self, creds: Dict[str, Any], shopify_order_id: str) -> Optional[str]:
        """URL-ul PDF al facturii (doc INVOICE) din xConnector pt o comandă (best-effort pe numele câmpului)."""
        o = await self.xc_order_by_shopify_id(creds, shopify_order_id)
        doc = self._doc(o, "INVOICE") or {}
        return (doc.get("fileUrl") or doc.get("url") or doc.get("invoiceUrl")
                or doc.get("documentUrl") or doc.get("downloadUrl"))

    async def get_invoice(self, db: AsyncSession, order: models.Order, account_key: str) -> bytes:
        """Descarcă PDF-ul facturii (doc INVOICE) din xConnector — oglindă la get_label."""
        creds = await self.get_credentials(db, account_key)
        if not creds or not creds.get("api_key"):
            raise RuntimeError("xConnector: cont fără api_key")
        url = await self._invoice_pdf_url(creds, order.shopify_order_id or "")
        if not url:
            raise RuntimeError("xConnector: factura nu are URL de PDF (e creată?)")
        r = await self.http.get(url, headers=self._headers(creds))
        r.raise_for_status()
        return r.content

    async def track_awb(self, db: AsyncSession, awb: str, account_key: Optional[str] = None) -> TrackingResponse:
        """xConnector nu e sursă de tracking live — statusul vine de la curierul real (adapterul
        DPD/etc. al OH). Întoarcem ce știe xConnector despre comandă (best-effort)."""
        creds = await self.get_credentials(db, account_key)
        if not creds or not creds.get("api_key"):
            return TrackingResponse(success=False, status_raw="cont fără api_key")
        o = await self.xc_order_by_tracking(creds, awb)
        if not o:
            return TrackingResponse(success=False, status_raw="AWB negăsit în xConnector")
        return TrackingResponse(success=True, code=awb, status_raw=o.get("status") or o.get("fulfillmentStatus"),
                                raw_data=o)

    # ── FACTURI (dincolo de BaseCourier — puntea de facturare SMART_BILL prin xConnector) ──
    async def _invoice_action(self, db: AsyncSession, order: models.Order, account_key: str, endpoint: str,
                              refund_id: Optional[int] = None, lang: Optional[str] = None) -> Dict[str, Any]:
        creds = await self.get_credentials(db, account_key)
        if not creds or not creds.get("api_key"):
            return {"success": False, "message": "xConnector: cont fără api_key"}
        o = await self.xc_order_by_shopify_id(creds, order.shopify_order_id or "")
        if not o.get("orderId"):
            return {"success": False, "message": "comanda nu există în xConnector"}
        con = await self._pick_billing_connector(creds)
        if not con:
            return {"success": False, "message": "connector de facturare (SMART_BILL) ambiguu/absent"}
        body: Dict[str, Any] = {"orderId": o["orderId"], "connectorId": con["id"]}
        if refund_id is not None:
            body["refundId"] = int(refund_id)
        if lang:
            body["languageCode"] = lang
        s, d = await self._post(creds, endpoint, body)
        ok = s == 200 and isinstance(d, dict) and d.get("accepted")
        invs = (d.get("invoices") or []) if isinstance(d, dict) else []
        good = [i for i in invs if i.get("success")]
        if ok and (good or not invs):
            first = good[0] if good else {}
            return {"success": True, "serie": first.get("invoiceSerie"), "numar": first.get("invoiceNumber"),
                    "storno": bool(first.get("storno")), "raw": d}
        return {"success": False, "message": self._err(d), "raw": d}

    async def create_invoice(self, db: AsyncSession, order: models.Order, account_key: str,
                             lang: Optional[str] = None) -> Dict[str, Any]:
        creds = await self.get_credentials(db, account_key)
        o = await self.xc_order_by_shopify_id(creds or {}, order.shopify_order_id or "") if creds else {}
        if o and self._doc(o, "INVOICE"):
            return {"success": False, "message": "are DEJA factură — folosește cancel + create (regen)"}
        res = await self._invoice_action(db, order, account_key, "/api/actions/create-invoice", lang=lang)
        if res.get("success") and creds:
            res["url"] = await self._invoice_pdf_url(creds, order.shopify_order_id or "")
        return res

    async def cancel_invoice(self, db: AsyncSession, order: models.Order, account_key: str) -> Dict[str, Any]:
        return await self._invoice_action(db, order, account_key, "/api/actions/cancel-invoice")

    async def storno_invoice(self, db: AsyncSession, order: models.Order, account_key: str,
                             refund_id: Optional[int] = None) -> Dict[str, Any]:
        return await self._invoice_action(db, order, account_key, "/api/actions/revert-invoice", refund_id=refund_id)
