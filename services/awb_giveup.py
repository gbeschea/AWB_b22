"""awb_giveup.py — GIVEUP / failcount pentru AWB. Port din cronul xConnector
(`AWB_GIVEUP_AFTER`, `_awb_failcount_load/_awb_failcount_bump` + felul în care `_do_awb` le folosește).

DE CE (lecție plătită scump în cron, nu o rescrie „mai simplu"):
un AWB care pică e reîncercat la fiecare tură. Când eroarea e TRANZITORIE (tipic 422 „was not created"
= latența de sincronizare xConnector ↔ curier) reîncercarea e corectă — DAR dacă latența nu se mai
rezolvă NICIODATĂ (adresă chiar-moartă, stradă absentă din nomenclatorul curierului), comanda intră în
BUCLĂ INFINITĂ: măsurat în cron, comenzi picate 7 ture la rând, ne-hold-uite, la care nu se uita nimeni
și care nu plecau niciodată. Regula owner-ului: **dacă nu merge de N ori, comanda rămâne la CS, fără
buclă la infinit**.

Cele două clase de eșec (identic cu cronul):
  • TRANZITORIU → mai încercăm, dar CONTORIZAT; la `GIVEUP_AFTER` eșecuri → HOLD la CS („awb-esec-repetat").
  • PERMANENT   → curierul a respins adresa; reîncercarea nu are ce să schimbe → HOLD la CS imediat
                  („bad-address"). Contorul nu contează.
Regula de clasificare din cron era literal:
    transient = status in (429, 500, 502, 503, 504) or (status == 422 and "was not created" in msg)
    # orice altceva = PERMANENT
⚠️ În OH statusul HTTP al CURIERULUI se pierde: `routes/courier_actions._create_one` împachetează orice
eroare în `HTTPException(400, f"{account_key}: {e}")`, iar `XConnectorCourier.create_awb` întoarce
`{"success": False, "message": ...}` fără status. De aceea aici clasificarea merge ÎN PRINCIPAL pe TEXT
(cuvintele-cheie de mai jos), iar statusul curierului se poate da explicit prin `status=` când apelantul
îl are. Textul „was not created" rămâne semnalul-ancoră al tranzitoriului, ca în cron.

A TREIA clasă, adăugată în OH (nu există în cron, dar cronul nici n-avea abonamente/conturi de curier):
  • CONFIG — eroarea nu e a comenzii, ci a instalării: cheie API lipsă, niciun connector activ, curier
    nesuportat, cotă de plan atinsă (402). Astea NU consumă bugetul de retry și NU se rutează la CS ca
    „adresă greșită" — ar bloca comenzi bune ale clientului pentru o problemă de configurare.

STOCARE — tabelă mică `awb_fail_counts`, NU coloană pe `orders`, și citită cu SQL text():
baza asta NU e pe alembic (vezi antetul lui scripts/schema_patches.sql — patch-urile se aplică de mână
în producție). Dacă aș fi pus coloana pe `orders` ȘI în models.py, un deploy făcut ÎNAINTE de aplicarea
patch-ului ar fi rupt ORICE citire de comenzi (SQLAlchemy selectează coloanele explicit) — adică toată
aplicația, pentru o funcție de bookkeeping. Cu tabelă separată + text(), raza de explozie e exact acest
fișier: dacă tabela lipsește, funcțiile prind excepția, o logează și se comportă ca înainte („nu renunțăm
niciodată"). Când patch-ul e aplicat peste tot, tabela poate primi un model ORM normal (pentru UI-ul CS).

Alte diferențe intenționate față de cron:
  • cronul ținea contorul într-un JSON cheiat pe NUMELE comenzii, global pe toate magazinele (două
    magazine cu același prefix de comandă își stricau reciproc contorul). Aici cheia e `orders.id`.
  • cronul nu resetează niciodată contorul; aici `reset()` există și TREBUIE apelat când AWB-ul reușește
    și când CS rezolvă comanda — altfel o comandă reparată de om rămâne pe veci fără buget de retry.
  • cronul făcea `here_ok_del(name)` la hold (invalida cache-ul „HERE a zis valid"). În OH nu există acel
    cache; echivalentul (re-validarea adresei / scoaterea din `address_status='validat'`) e treaba
    validatorului, nu a acestui modul — vezi „NEPORTAT" în rezumat.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Iterable, Optional, Set

from sqlalchemy import bindparam, inspect as sa_inspect, select, text
from sqlalchemy.orm import selectinload

import models

logger = logging.getLogger("awb_giveup")

__all__ = [
    "TRANSIENT", "PERMANENT", "CONFIG", "GIVEUP_AFTER", "CORRECT_AT",
    "classify", "bump", "reset", "should_give_up", "gave_up_ids", "hold_for_cs", "on_failure",
]

TRANSIENT = "transient"
PERMANENT = "permanent"
CONFIG = "config"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name) or default))
    except Exception:
        return default


# 4 = valoarea calibrată în cron (AWB_GIVEUP_AFTER). NU e un număr rotund la întâmplare: la al 2-lea eșec
# cronul rulează corecția agentică de adresă, apoi mai lasă 2 ture ca adresa corectată să se propage prin
# xConnector înainte de a renunța. Dacă scazi pragul sub 3, renunți înainte ca o corecție reușită să apuce
# să se sincronizeze; dacă îl urci mult, te întorci la bucla infinită.
GIVEUP_AFTER = _env_int("AWB_GIVEUP_AFTER", 4)
CORRECT_AT = 2          # la al câtelea eșec merită rulată corecția agentică de adresă (vezi `on_failure`)
PERMANENT_AFTER = _env_int("AWB_PERMANENT_AFTER", 1)   # respins permanent → CS din primul eșec (ca în cron)

_MAX_ERR = 500          # cât din mesajul de eroare păstrăm (e pentru omul de la CS, nu pentru arhivă)

# Eroare de INSTALARE/abonament, nu a comenzii → nu consumă buget, nu ajunge la CS ca adresă greșită.
_CONFIG_MARKS = (
    "nu are api_key", "api_key", "niciun connector", "no connector", "unsupported courier",
    "isn't supported", "nu are shopify_order_id", "plan limit reached", "upgrade to pro",
    "quota", "credentials", "cont inexistent", "account not found",
)

# TRANZITORIU. „was not created" e ancora din cron: xConnector acceptă cererea, dar eticheta încă nu
# există la curier — la tura următoare de obicei există. Restul sunt eșecuri de rețea/infrastructură.
_TRANSIENT_MARKS = (
    "was not created", "nu există (încă)", "nu exista (inca)", "not yet", "încă nu",
    "timeout", "timed out", "time-out", "temporar", "temporarily", "try again", "reîncear",
    "rate limit", "too many requests", "throttl",
    "service unavailable", "bad gateway", "gateway timeout", "internal server error",
    "reset by peer", "unreachable", "eof occurred",
)

# Cuvintele astea trebuie potrivite pe GRANIȚĂ DE CUVÂNT, nu ca substring: `_create_one` împachetează
# ORICE eroare într-un mesaj care conține „xconnector-<slug>", iar un substring „connect" s-ar potrivi
# în „xConnector" → absolut orice eșec ar fi clasificat TRANZITORIU și comanda ar bucla la infinit,
# fix invers față de intenție. (Prins de review-ul adversarial, 19-aug.)
_TRANSIENT_RE = re.compile(
    r"\b(connection|connect|network|dns|socket|broken pipe|conn reset)\b", re.I)

# Statusuri HTTP ale CURIERULUI (când apelantul le are). 408/425 adăugate față de cron — sunt tranzitorii
# prin definiția lor HTTP și nu strică regula.
_TRANSIENT_STATUS = (408, 425, 429, 500, 502, 503, 504)

# Statusul apare uneori doar în textul erorii („HTTP 502 …", „status 429"). Nu căutăm cifre goale:
# un nume de comandă („GT502") sau o sumă ar da fals-pozitive.
# `cod` scos din alternanță: la ARONA „cod" înseamnă cod poștal sau COD=ramburs, nu status HTTP.
_STATUS_RE = re.compile(r"(?:http|https|status|statuscode|status_code|code)\D{0,4}(\d{3})", re.I)


def _text_of(err: Any) -> str:
    """Mesajul erorii, indiferent de forma în care vine (HTTPException / Exception / dict / str)."""
    if err is None:
        return ""
    try:
        detail = getattr(err, "detail", None)          # fastapi.HTTPException
        if isinstance(err, dict):
            body = str(err.get("message") or err.get("error") or err)
        else:
            body = str(detail) if detail else str(err)
        # TIPUL contează: excepțiile de timeout din httpx au str(e) == "" (ReadTimeout/ConnectTimeout/
        # PoolTimeout), deci pe text gol ar fi clasificate PERMANENT — exact invers, timeout-ul e cazul
        # tranzitoriu prin excelență. Numele clasei aduce cuvântul care le identifică.
        if isinstance(err, BaseException):
            return "%s: %s" % (type(err).__name__, body)
        return body
    except Exception:
        return ""


def classify(err: Any, status: Optional[int] = None) -> str:
    """TRANSIENT | PERMANENT | CONFIG pentru un eșec de creare AWB.

    `status` = statusul HTTP al CURIERULUI, dacă apelantul îl are (adaptorul xConnector îl are în
    `_post`; `_create_one` nu îl mai propagă). Fără el, decidem pe text.
    IMPLICIT = PERMANENT, exact ca în cron: dacă nu recunoaștem eroarea drept tranzitorie, presupunem că
    respingerea e reală și trimitem comanda la om. Invers (implicit tranzitoriu) = bucla infinită înapoi.
    """
    try:
        msg = _text_of(err).lower()
        st = status
        if st is None:
            st = getattr(err, "status_code", None) or getattr(err, "status", None)
            # 400 e wrapperul lui OH peste ORICE eroare de curier (`_create_one`) — nu spune nimic despre
            # curier, deci nu-l folosim ca semnal. 402 = cota de plan, o tratăm ca CONFIG mai jos.
            if st == 400:
                st = None
        if st is None:
            m = _STATUS_RE.search(msg)
            if m:
                st = int(m.group(1))
        try:
            st = int(st) if st is not None else None
        except Exception:
            st = None

        if st == 402 or any(k in msg for k in _CONFIG_MARKS):
            return CONFIG
        if st in _TRANSIENT_STATUS:
            return TRANSIENT
        if any(k in msg for k in _TRANSIENT_MARKS) or _TRANSIENT_RE.search(msg):
            return TRANSIENT
        return PERMANENT
    except Exception as e:                                   # clasificarea nu are voie să rupă nimic
        logger.warning("classify a picat (%s) — tratez ca PERMANENT", e)
        return PERMANENT


def _order_id(order: Any) -> Optional[int]:
    """id-ul comenzii FĂRĂ să atingem atributele ORM.

    De ce atât de defensiv: apelantul ajunge aici pe ramura de eroare, imediat după `await db.rollback()`
    (vezi services/auto_awb_service.run_store). Rollback-ul EXPIRĂ instanțele, iar orice acces la un
    atribut expirat declanșează un refresh sincron care, în SQLAlchemy async, aruncă MissingGreenlet.
    `inspect(obj).identity` și `__dict__` nu fac IO.
    """
    try:
        ident = sa_inspect(order).identity
        if ident:
            return int(ident[0])
    except Exception:
        pass
    try:
        raw = order.__dict__.get("id")
        if raw is not None:
            return int(raw)
    except Exception:
        pass
    try:
        return int(getattr(order, "id", None))
    except Exception:
        return None


async def _fails(db, oid: int) -> int:
    row = (await db.execute(
        text("SELECT fails FROM awb_fail_counts WHERE order_id = :oid"), {"oid": oid})).first()
    return int(row[0]) if row and row[0] is not None else 0


async def bump(db, order, *, error: Any = None, kind: Optional[str] = None) -> int:
    """Incrementează ATOMIC contorul de eșecuri al comenzii și întoarce noua valoare (0 = n-am putut).

    ⚠️ Se apelează DUPĂ `db.rollback()`-ul apelantului (contorul trebuie să supraviețuiască rollback-ului
    care anulează AWB-ul eșuat) și COMMITEAZĂ singur. Upsert cu `ON CONFLICT` = fără citește-modifică-scrie,
    deci două pase concurente nu-și pierd incrementele.
    """
    oid = _order_id(order)
    if oid is None:
        logger.warning("bump: comandă fără id (%r) — sar", order)
        return 0
    try:
        row = (await db.execute(text("""
            INSERT INTO awb_fail_counts (order_id, store_id, fails, last_class, last_error,
                                         first_fail_at, last_fail_at)
            SELECT o.id, o.store_id, 1, :cls, :err, now(), now() FROM orders o WHERE o.id = :oid
            ON CONFLICT (order_id) DO UPDATE
               SET fails        = awb_fail_counts.fails + 1,
                   last_class   = EXCLUDED.last_class,
                   last_error   = EXCLUDED.last_error,
                   last_fail_at = now()
            RETURNING fails
        """), {"oid": oid, "cls": (kind or "")[:16] or None,
               "err": (_text_of(error)[:_MAX_ERR] or None)})).first()
        await db.commit()
        n = int(row[0]) if row else 0
        logger.info("awb-fail order_id=%s fails=%d/%d class=%s", oid, n, GIVEUP_AFTER, kind or "?")
        return n
    except Exception as e:
        # Tabela lipsă (patch neaplicat) sau orice altceva → 0 = „nu renunțăm", adică EXACT comportamentul
        # de dinaintea acestui modul. Nimic nu se rupe, dar se vede în loguri că mecanismul nu e activ.
        logger.error("bump a picat pt order_id=%s: %s (patch-ul awb_fail_counts e aplicat?)", oid, e)
        try:
            await db.rollback()
        except Exception:
            pass
        return 0


async def reset(db, order) -> None:
    """Șterge contorul comenzii (a reușit AWB-ul / CS a rezolvat-o). Comanda primește buget curat de retry.

    Cronul NU avea reset (fișierul JSON creștea la infinit). În OH e obligatoriu: fără el, o comandă
    reparată de om rămâne pe veci peste prag și n-ar mai primi niciodată AWB automat.
    """
    oid = _order_id(order)
    if oid is None:
        return
    try:
        res = await db.execute(text("DELETE FROM awb_fail_counts WHERE order_id = :oid"), {"oid": oid})
        await db.commit()
        if getattr(res, "rowcount", 0):
            logger.info("awb-fail reset order_id=%s", oid)
    except Exception as e:
        logger.error("reset a picat pt order_id=%s: %s", oid, e)
        try:
            await db.rollback()
        except Exception:
            pass


async def should_give_up(db, order) -> bool:
    """True dacă această comandă și-a consumat bugetul de reîncercări → NU o mai încerca.

    Se cheamă ca POARTĂ, înainte de a apela curierul: hold-ul la CS nu scoate singur comanda din bucla de
    auto-AWB (filtrul din `auto_awb_service.run_store` nu se uită la hold), deci fără poarta asta am
    continua să ardem apeluri la curier pe o comandă deja predată omului.
    Fail-safe: orice eroare → False (încearcă), niciodată blocare din greșeală.
    """
    oid = _order_id(order)
    if oid is None:
        return False
    try:
        row = (await db.execute(text(
            "SELECT fails, held_at FROM awb_fail_counts WHERE order_id = :o"), {"o": oid})).first()
        if not row:
            return False
        return bool(row[0] is not None and row[0] >= GIVEUP_AFTER) or row[1] is not None
    except Exception as e:
        logger.error("should_give_up a picat pt order_id=%s: %s", oid, e)
        return False


async def gave_up_ids(db, order_ids: Iterable[int]) -> Set[int]:
    """Varianta pe lot a lui `should_give_up` — un singur SELECT pentru toată pasa (bucla auto-AWB ia
    până la 25 de comenzi/magazin; n-are rost un query per comandă)."""
    ids = [int(i) for i in (order_ids or []) if i is not None]
    if not ids:
        return set()
    try:
        # bindparam expanding, nu `= ANY(:ids)`: pe asyncpg tipul array-ului nu se poate deduce din
        # parametru și query-ul crapă cu „could not determine data type of parameter".
        # `held_at IS NOT NULL` e la fel de important ca pragul: ramura PERMANENT predă comanda la CS din PRIMUL
        # eșec (PERMANENT_AFTER=1), deci `fails` rămâne 1 < GIVEUP_AFTER=4 — fără condiția asta o comandă
        # deja dată omului ar fi reîncercată la fiecare tură.
        stmt = text("SELECT order_id FROM awb_fail_counts WHERE order_id IN :ids "
                    "AND (fails >= :n OR held_at IS NOT NULL)")
        stmt = stmt.bindparams(bindparam("ids", expanding=True))
        rows = (await db.execute(stmt, {"ids": ids, "n": GIVEUP_AFTER})).all()
        return {int(r[0]) for r in rows}
    except Exception as e:
        logger.error("gave_up_ids a picat: %s", e)
        return set()


# motivul din cron → categoria de coadă CS din OH (routes/cs_queue._REASONS)
_CS_REASON = {
    "bad-address": "wrong_address",        # curierul a respins adresa → CS cere adresa corectă clientului
    "awb-esec-repetat": "manual",          # adresa pare bună, AWB-ul tot pică → se uită un om
}

# ⚠️ `wrong_address` NU e o simplă etichetă: routes/cs_queue.enqueue_order declanșează pe ea `_auto_email`,
# adică un EMAIL REAL către client. O clasificare greșită = clientul primește „corectează-ți adresa" pentru
# o eroare care n-avea legătură cu adresa. Deci cerem dovadă în mesaj, altfel mergem pe `manual` (coadă CS,
# fără email). (Prins de review-ul adversarial, 19-aug.)
_ADDRESS_WORDS = re.compile(
    r"address|adres|localit|street|strad|zip|postal|city|ora[sș]|jude[tț]|recipient|receiver", re.I)


def cs_reason_for(kind: str, err: Any = None) -> str:
    """Categoria de coadă CS pentru un eșec — conservatoare la trimiterea de emailuri."""
    mapped = _CS_REASON.get(kind, "manual")
    if mapped == "wrong_address" and not _ADDRESS_WORDS.search(_text_of(err)):
        return "manual"
    return mapped


async def hold_for_cs(db, store, order, reason: str, err: Any = None) -> None:
    """Predă comanda la CS (coadă + HOLD în Shopify) și oprește bucla. `reason` = motivul din cron
    ('bad-address' | 'awb-esec-repetat' | text liber).

    Reîncarcă instanța comenzii din DB înainte de a o da mai departe: apelantul vine de pe ramura de
    eroare, după rollback, cu obiecte expirate — iar `enqueue_order` citește `order.shipments`,
    `order.store`, `order.shopify_order_id` (lazy-load pe obiect expirat = MissingGreenlet).
    """
    oid = _order_id(order)
    if oid is None:
        return
    try:
        # deja predată la CS pentru acest ciclu de eșecuri → nu mai lovim Shopify încă o dată
        row = (await db.execute(
            text("SELECT held_at, fails, last_error FROM awb_fail_counts WHERE order_id = :oid"),
            {"oid": oid})).first()
        if row and row[0] is not None:
            return
        fails = int(row[1]) if row and row[1] is not None else 0
        last_err = (row[2] if row else None) or ""

        o = (await db.execute(
            select(models.Order)
            .options(selectinload(models.Order.shipments), selectinload(models.Order.store))
            .where(models.Order.id == oid)
        )).scalar_one_or_none()
        if o is None:
            return
        # PROTECȚIE LIVRARE (aceeași ca în cron_parity/blocklist): ce are deja AWB sau e anulat nu se atinge.
        if o.cancelled_at is not None or any(s.awb for s in (o.shipments or [])):
            return

        owner = o.store or store
        detail = "AWB: %s (%d eșecuri). Ultima eroare: %s" % (reason, fails, last_err[:200])
        cs_reason = cs_reason_for(reason, err)   # nu trimite email decât dacă eroarea vorbește de adresă
        item = None
        try:
            # Calea reală din OH: pune HOLD în Shopify, aplică tag-ul de flag și trimite o dată emailul
            # automat potrivit motivului. Import lazy = același motiv ca în auto_awb_service (ciclu de module).
            from routes.cs_queue import enqueue_order
            item = await enqueue_order(db, owner, o, reason=cs_reason, detail=detail, created_by="auto")
        except Exception as e:
            logger.error("enqueue_order a picat pt %s: %s — cad pe intrare directă în coadă", o.name, e)
        if item is None:
            # Ori magazinul e NO_CS (piață fără coadă lucrată — `enqueue_order` întoarce None și NU rutăm
            # la CS: hold-ul acolo = comandă moartă), ori enqueue-ul a crăpat. În ambele cazuri comanda tot
            # nu se mai reîncearcă, fiindcă poarta `should_give_up` o scoate din buclă.
            exists = (await db.execute(
                select(models.CSQueueItem.id).where(models.CSQueueItem.order_id == o.id))).first()
            if not exists:
                from services.utils import no_cs
                if not no_cs(owner):
                    db.add(models.CSQueueItem(store_id=o.store_id, order_id=o.id, reason=cs_reason,
                                              status="open", reason_detail=detail, created_by="auto"))
        await db.execute(text("UPDATE awb_fail_counts SET held_at = now() WHERE order_id = :oid"),
                         {"oid": oid})
        await db.commit()
        logger.info("GIVEUP order=%s store=%s reason=%s fails=%d -> CS(%s)",
                    o.name, getattr(owner, "domain", "?"), reason, fails, cs_reason)
    except Exception as e:
        logger.error("hold_for_cs a picat pt order_id=%s: %s", oid, e)
        try:
            await db.rollback()
        except Exception:
            pass


async def on_failure(db, store, order, err: Any, *, status: Optional[int] = None) -> Dict[str, Any]:
    """Decizia completă după UN eșec de creare AWB — punctul unic de cablare (echivalentul blocului din
    `_do_awb`, cron ~5991-6020). Se apelează DUPĂ `db.rollback()`-ul apelantului.

    Întoarce {"class", "fails", "held", "suggest_correction"}:
      • class = transient|permanent|config
      • held  = am predat comanda la CS (nu o mai încerca)
      • suggest_correction = suntem exact la eșecul la care cronul rulează corecția agentică de adresă
        (xc match-address + ai-correct-address, apoi nomenclator). Corecția NU e portată aici; când
        ajunge în OH, se declanșează pe flag-ul ăsta — iar dacă adresa se corectează, se cheamă `reset()`
        și comanda se reîncearcă la tura următoare, FĂRĂ hold (exact ca în cron).
    """
    kind = classify(err, status)
    oid = _order_id(order)
    # FAIL-SAFE pe TOT corpul: rulăm pe ramura de eroare a creării de AWB, imediat după un rollback. Dacă
    # patch-ul de schemă (`awb_fail_counts`) lipsește pe un mediu, SQL-ul de aici aruncă — iar o excepție
    # ridicată din handlerul de eroare ar masca eroarea REALĂ a curierului și ar rupe bucla de AWB.
    # Degradăm în „nu știm, nu ținem nimic pe loc". (Prins de review-ul adversarial, 19-aug.)
    try:
        if kind == CONFIG:
            # Instalare/abonament, nu comanda. Nu ardem bugetul comenzii și n-o trimitem la CS: s-ar bloca
            # comenzi perfect bune pentru o cheie API lipsă sau o cotă de plan atinsă.
            logger.warning("AWB config-error order_id=%s: %s", oid, _text_of(err)[:200])
            return {"class": kind, "fails": (await _fails(db, oid) if oid else 0),
                    "held": False, "suggest_correction": False}

        n = await bump(db, order, error=err, kind=kind)
        held = False
        if kind == PERMANENT and n >= PERMANENT_AFTER:
            # Curierul a respins-o PERMANENT → reîncercarea nu schimbă nimic; e treabă de om.
            await hold_for_cs(db, store, order, "bad-address", err)
            held = True
        elif kind == TRANSIENT and n >= GIVEUP_AFTER:
            # „Tranzitoriu", dar buclează de prea multe ture = latență care nu se mai rezolvă / adresă moartă.
            await hold_for_cs(db, store, order, "awb-esec-repetat", err)
            held = True
        return {"class": kind, "fails": n, "held": held,
                "suggest_correction": (kind == TRANSIENT and n == CORRECT_AT)}
    except Exception as e:
        logger.error("on_failure a picat (order_id=%s): %s — degradez la neutru", oid, e)
        return {"class": kind, "fails": 0, "held": False, "suggest_correction": False}
