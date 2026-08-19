"""Rutare EASYBOX / locker — port din cronul xConnector (`route_connector`, `order_connector_id`,
`_store_has_alt_courier`), fără de care OH ar trimite coletele de locker pe curierul greșit.

CE FACE CRONUL (verificat în sursă, nu presupus)
------------------------------------------------
Widget-ul xConnector de pe storefront scrie pe comanda Shopify, ca *note attributes*, alegerea
clientului atunci când acesta selectează un punct de ridicare: `ConnectorId`, `ConnectorType`,
`LocationId`. Cronul citește DOAR `ConnectorId` și, dacă acel connector e încă ACTIV pe cheia
xConnector a magazinului, îl folosește în locul default-ului (DPD). Exemplu real din producție:
ROSSI, care are Sameday activ TOCMAI pentru easybox — o comandă de locker plecată pe DPD standard
ajunge livrare la domiciliu pe o adresă care e de fapt un dulap.

⚠️ LECȚIA CARE COSTĂ (păstrată din cron): `LocationId` **NU** se trimite în body-ul de
`create-shipping-label`. xConnector reține lockerul ales server-side, legat de comandă; noi trimitem
doar `connectorId`. De aceea `extra_label_options()` întoarce `{}` — vezi comentariul de acolo.

⚠️ A DOUA LECȚIE: lookup-ul de note attributes costă un apel Shopify pe comandă. Cronul îl face
DOAR dacă magazinul are un curier ALTERNATIV activ (altul decât DPD/Dragon Star/geocoding/billing).
Un magazin care are doar DPD n-are ce ruta → mergem direct pe default, gratis. Păstrăm garda.

⚠️ A TREIA LECȚIE: dacă storefront-ul a pus un `ConnectorId` care între timp NU mai e activ (ex.
Econt scos din cont), cronul **nu** face AWB home-delivery peste un locker — lasă `create-label` pe
default să pice, iar comanda intră după 3 ture în coada de „awb-eșec-repetat" la CS. Reproducem
comportamentul (întoarcem None = fără override) DAR logăm zgomotos, ca omul să vadă cauza.

CE NU E AICI (intenționat)
--------------------------
Rutarea Grandia „produs voluminos → Dragon Star" din același `route_connector` al cronului NU e
rutare de locker; ține de tipul produsului și de un lookup de `productType` în Shopify. E o
capabilitate separată, nu o strecor aici ca să nu se piardă la review.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Tipuri de connector care NU sunt „curier alternativ": facturare, geocoding, și cei doi curieri
# care sunt oricum default-ul nostru de livrare la domiciliu. Set identic cu cel din cron
# (BILLING_TYPES + HERE_GEOCODING/POSTIS/DPD/DRAGON_STAR).
BILLING_TYPES = {"SMART_BILL", "SMARTBILL", "SMART_BILL_RO", "FACTURIS", "OBLIO", "FGO"}
_NOT_ALTERNATIVE = BILLING_TYPES | {"HERE_GEOCODING", "POSTIS", "DPD", "DRAGON_STAR"}

# Cheile scrise de widget-ul xConnector. Prima e cea reală din producție; restul sunt variante de
# capitalizare pe care le-am văzut în checkout-uri custom — le acceptăm, nu costă nimic.
_CONNECTOR_KEYS = ("ConnectorId", "connectorId", "connector_id", "xconnector_connector_id")
_LOCATION_KEYS = ("LocationId", "locationId", "location_id", "lockerId", "locker_id")
_CONNECTOR_TYPE_KEYS = ("ConnectorType", "connectorType", "connector_type")

# Fallback de detecție dacă `address_service` nu se poate importa (nu ar trebui, dar modulul ăsta
# rulează pe calea de creare AWB — nu are voie să arunce).
_LOCKER_RE = re.compile(
    r"(easybox|locker|pachetomat|fanbox|paczkomat|box\s*now|pudo|packeta|zasilkovna|"
    r"ship\s*&?\s*go|collect\s*point|pick[\s\-]*up)", re.I)

# Cache scurt pentru note attributes: aceeași comandă e interogată de mai multe ori într-o tură
# (create → eșec → retry). Cheia = (store_id, shopify_order_id).
_ATTR_TTL = 300.0
_ATTR_CACHE: Dict[tuple, Dict[str, Any]] = {}
_ATTR_CACHE_MAX = 2000


# ── 1. detecție ────────────────────────────────────────────────────────────────────────────────
def is_locker_order(order: Any) -> bool:
    """Comanda e livrare la locker/punct de ridicare?

    Sursa primară e `Order.is_locker`, precalculat la ingest de evenimentul de mapper din models.py
    (adresa e criptată la rest, deci flag-ul e singurul lucru pe care se poate filtra în SQL).
    Când e NULL (rând vechi, dintainte de coloană) recalculăm din adresă cu `detect_easybox`.
    """
    try:
        flag = getattr(order, "is_locker", None)
        if flag is not None:
            return bool(flag)
    except Exception:                                     # pragma: no cover — ORM detașat
        pass
    a1 = a2 = ""
    try:
        a1 = getattr(order, "shipping_address1", None) or ""
        a2 = getattr(order, "shipping_address2", None) or ""
    except Exception:
        return False
    try:
        from services.address_service import detect_easybox
        return bool(detect_easybox(a1, a2))
    except Exception as e:
        logger.debug("locker: address_service indisponibil (%s) — folosesc regexul local", e)
        return bool(_LOCKER_RE.search(f"{a1} {a2}"))


# ── 2. alegerea făcută de client pe storefront ──────────────────────────────────────────────────
async def _note_attributes(order: Any) -> Dict[str, str]:
    """`customAttributes` (note attributes) ale comenzii din Shopify, ca dict {key: value}.

    Citirea trece prin `shopify_service._gql` (singura poartă către Shopify în OH). Fail-safe:
    orice eroare → {} și un log, niciodată o excepție pe calea de creare AWB.
    """
    store = None
    try:
        store = getattr(order, "store", None)             # eager-loaded în _load_order / auto_awb
    except Exception as e:                                # lazy-load într-o sesiune async = MissingGreenlet
        logger.info("locker: nu pot citi order.store (%s) — sar peste rutare", e)
        return {}
    soid = getattr(order, "shopify_order_id", None)
    if not store or not soid:
        return {}

    key = (getattr(store, "id", None), str(soid))
    hit = _ATTR_CACHE.get(key)
    if hit and (time.time() - hit["ts"]) < _ATTR_TTL:
        return hit["attrs"]

    query = """
    query($id: ID!) {
      order(id: $id) { customAttributes { key value } }
    }
    """
    try:
        from services import shopify_service
        data = await shopify_service._gql(
            store, query, {"id": f"gid://shopify/Order/{str(soid).split('/')[-1]}"})
    except Exception as e:
        logger.info("locker: citirea note attributes a picat pt %s: %s",
                    getattr(order, "name", soid), e)
        return {}

    attrs: Dict[str, str] = {}
    for a in (((data.get("order") or {}).get("customAttributes")) or []):
        k, v = (a or {}).get("key"), (a or {}).get("value")
        if k and v is not None and str(v).strip():
            attrs[str(k)] = str(v).strip()

    if len(_ATTR_CACHE) > _ATTR_CACHE_MAX:
        _ATTR_CACHE.clear()
    _ATTR_CACHE[key] = {"attrs": attrs, "ts": time.time()}
    return attrs


def _pick(attrs: Dict[str, str], keys) -> Optional[str]:
    for k in keys:
        if attrs.get(k):
            return attrs[k]
    return None


async def storefront_connector_id(order: Any) -> Optional[str]:
    """`ConnectorId` ales de client pe storefront (widget-ul xConnector), sau None."""
    return _pick(await _note_attributes(order), _CONNECTOR_KEYS)


async def storefront_location_id(order: Any) -> Optional[str]:
    """`LocationId` = ID-ul lockerului ales de client.

    NU se trimite către xConnector (îl are deja server-side, vezi `extra_label_options`). E util
    doar pentru adaptoarele DIRECTE de curier ale OH — `services/couriers/sameday.py` îl consumă
    ca `options["address_id"]` când face AWB de easybox direct la Sameday, fără xConnector.
    """
    return _pick(await _note_attributes(order), _LOCATION_KEYS)


# ── 3. rutarea ─────────────────────────────────────────────────────────────────────────────────
def store_has_alt_courier(connectors: Optional[List[Dict[str, Any]]]) -> bool:
    """Magazinul are un curier ALTERNATIV activ (ex. ROSSI cu Sameday pentru easybox)?

    Doar atunci merită un lookup de note attributes per comandă; altfel (doar DPD) rutăm direct pe
    default, fără să cheltuim un apel Shopify. Garda e din cron — a fost pusă exact ca lookup-ul
    să nu ruleze pe toate magazinele.
    """
    for c in (connectors or []):
        if not isinstance(c, dict) or not c.get("active"):
            continue
        if (c.get("type") or "").upper() in _NOT_ALTERNATIVE:
            continue
        return True
    return False


async def route_decision(courier: Any, creds: Dict[str, Any], order: Any,
                         connectors: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Varianta explicativă a lui `pick_connector` — întoarce {connector, reason, locker, ...}.

    `reason` e gândit să ajungă în log/nota de comandă, ca omul de la CS să vadă DE CE a plecat
    coletul pe curierul ăla. `pick_connector` e învelișul subțire peste ea.
    """
    out: Dict[str, Any] = {"connector": None, "reason": "no-override",
                           "locker": False, "connector_id": None}
    try:
        out["locker"] = is_locker_order(order)
    except Exception:
        pass

    try:
        cons = [c for c in (connectors or []) if isinstance(c, dict)]
        if not store_has_alt_courier(cons):
            # Magazin fără curier alternativ. Dacă adresa MIROASE a locker, e un semnal real de
            # mis-rutare (colet de dulap pe curier de domiciliu) — îl logăm ca să apară în triaj,
            # dar nu blocăm AWB-ul: cronul nu-l bloca, iar KPI-ul e AWB făcut.
            if out["locker"]:
                logger.warning(
                    "locker: %s pare livrare la locker, dar magazinul n-are curier alternativ activ "
                    "— pleacă pe default (risc de mis-rutare).", getattr(order, "name", "?"))
                out["reason"] = "locker-fara-curier-alternativ"
            else:
                out["reason"] = "fara-curier-alternativ"
            return out

        cid = await storefront_connector_id(order)
        if not cid:
            out["reason"] = "locker-fara-connector-pe-comanda" if out["locker"] else "fara-alegere-storefront"
            return out
        out["connector_id"] = cid

        # Filtru DEFENSIV pe conectorii de FACTURARE: modulul primește lista de la apelant și n-are
        # voie să presupună că a fost deja curățată. Dacă un id de SmartBill ar ajunge din greșeală
        # în note-attributes, am cere eticheta de la un connector de facturi.
        cons = [c for c in (cons or [])
                if isinstance(c, dict) and (c.get("type") or "").upper() not in BILLING_TYPES]
        match = [c for c in cons if str(c.get("id")) == str(cid) and c.get("active")]
        if match:
            out["connector"] = match[0]
            out["reason"] = "storefront-connector"
            logger.info("locker: %s → connector %s [%s] ales de client pe storefront (locker=%s)",
                        getattr(order, "name", "?"), match[0].get("name"), match[0].get("id"),
                        out["locker"])
            return out

        # Connectorul ales nu mai e activ (ex. Econt scos din cont). NU facem AWB home-delivery
        # peste un locker: lăsăm create-label pe default să pice, comanda intră la CS prin coada de
        # eșec repetat. Paritate exactă cu cronul — dar cu un log care numește cauza.
        logger.warning(
            "locker: %s are ConnectorId=%s pe comandă, dar connectorul NU mai e activ pe cheia "
            "xConnector — nu forțez home-delivery peste un locker; create-label va pica → CS.",
            getattr(order, "name", "?"), cid)
        out["reason"] = "connector-inactiv"
        return out
    except Exception as e:                                # nimic de aici nu are voie să rupă AWB-ul
        logger.exception("locker: rutare eșuată pt %s (%s) — merg pe default",
                         getattr(order, "name", "?"), e)
        out["reason"] = "eroare"
        return out


async def pick_connector(courier: Any, creds: Dict[str, Any], order: Any,
                         connectors: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """Connectorul xConnector pe care trebuie să plece comanda, sau None.

    **None = fără override**: apelantul își păstrează default-ul (DPD). Nu întoarcem niciodată un
    connector „ghicit" — dacă nu există o alegere explicită și activă pe comandă, decizia rămâne a
    apelantului, exact ca în cron.

    `courier` (XConnectorCourier) și `creds` nu sunt folosite azi — lista de connectori vine deja
    rezolvată din apelant, ca să nu mai lovim `/api/merchant/connectors` a doua oară pe comandă. Le
    păstrez în semnătură pentru când rutarea va avea nevoie de un lookup propriu.
    """
    return (await route_decision(courier, creds, order, connectors)).get("connector")


# ── 4. câmpuri suplimentare pe eticheta xConnector ─────────────────────────────────────────────
def extra_label_options(order: Any) -> Dict[str, Any]:
    """Ce se ADAUGĂ la body-ul de `POST /api/actions/create-shipping-label` pentru o comandă de locker.

    **Nimic — și asta e concluzia verificată, nu o scăpare.** În cron, body-ul e identic pentru
    locker și pentru domiciliu:

        {orderId, connectorId, parcelCount, parcelType, notifyCustomer}

    Lockerul ales de client (`LocationId`) e reținut de xConnector server-side, legat de comandă, și
    aplicat de el la generarea AWB-ului. Dacă l-am trimite noi în body am dubla informația și am
    risca un conflict. Toată rutarea de locker se face, deci, prin `connectorId` — vezi
    `pick_connector`.

    Funcția rămâne în API ca punct unic de extindere: dacă xConnector adaugă mâine un câmp de
    locker în contractul de create-shipping-label, aici se pune, și nicăieri altundeva.

    Pentru adaptoarele DIRECTE de curier (fără xConnector) NU folosi funcția asta — Sameday vrea
    `options["address_id"]`, care se ia cu `await storefront_location_id(order)` sau din pickerul
    din `services/lockers.py`.
    """
    return {}
