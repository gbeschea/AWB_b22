# services/couriers/address_repair.py
"""Reparare CONSERVATOARE a adreselor RO marcate WRONG/UNKNOWN de xConnector — port din cronul
`cron_xconnector.correct_address` (+ helperii lui). Fără ea, o comandă cu `addressStatus=WRONG` NU
primește etichetă: xConnector refuză create-shipping-label (verificat live pe NUBRA13391).

Lanțul (ordinea din cron — prima variantă care REUȘEȘTE câștigă):
  0. gărzi de oprire: non-RO · are deja etichetă · adresă-gunoi · ZIP-ul clientului confirmat
  1. CANDIDAT TARE  — validatorul xConnector (`/match-address`): UN singur candidat cu zip/oraș/județ
                      ≥0.95 și stradă ≥0.90, ZIP re-confirmat la `/zip-code`, numărul casei PĂSTRAT
  2. VALIDAREA STOCATĂ — `latestAddressValidation` de pe comandă: completează zip/oraș greșit (scor 1.0)
                      și curăță zgomotul din stradă, DOAR pe aceeași stradă
  3. NOMENCLATOR    — `services.nomenclator.runner.validate_address` (RO + garda de omonimie #10 + a
                      doua opinie HERE, deja înăuntru) → corecție deterministă

DE CE atâtea gărzi (lecții plătite scump în cron — NU le slăbi):
  · o corecție greșită = colet la adresa greșită. Mai bine „manual/CS" decât o adresă VALIDĂ-dar-ALTA.
  · UN singur candidat: doi candidați tari = validatorul nu știe care e, deci nici noi.
  · numărul casei se PĂSTREAZĂ mereu: „Ap. 49" devenea „Nr 49" la primele versiuni.
  · strada NU se schimbă, se curăță: „Nicolina str prof. Ion Inculeț" devenea „Strada Nicolina".
  · ZIP-ul dat de client care se potrivește localității lui NU se suprascrie (over-corecție pe omonime
    rurale — 2× Popești/Vâlcea).
  · RO-ONLY: tot ce urmează (nomenclator RO, country="Romania") ar transforma o adresă CZ/PL/BG într-una
    „românească". INTL are calea lui: `dpd_intl.py` + `XConnectorCourier.sanitize_intl`.

DIFERENȚE FAȚĂ DE CRON (intenționate, documentate):
  · nomenclatorul se citește prin `runner.validate_address` (async, pool-ul OH), NU prin cursor psycopg2
    sincron pe metrics. Consecință: garda „ZIP client pe localitate mică (≤3 coduri)" devine „nomenclatorul
    declară adresa VALIDĂ ca-i" — un verdict cel puțin la fel de tare (include garda de omonimie #10).
  · pasul HERE zip-fill nu mai e separat: runner-ul îl face singur pe verdictul `needs_geocoder`.
  · pasul „istoricul clientului" (strada/numărul dintr-o comandă LIVRATĂ anterior, din AWBprint) NU e
    portat — cere istoricul AWBprint, pe care OH nu-l are async. Vezi rezumatul din PR.
"""
from __future__ import annotations

import itertools
import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

XBASE = "https://xconnector.app"
VBASE = "https://address-validator.xconnector.app"      # validatorul de adrese (token separat!)

# statusurile xConnector care blochează eticheta
BAD_STATUSES = ("WRONG", "UNKNOWN")

# Cache token validator, per api_key: {api_key: (token, expira_la)}. TTL OBLIGATORIU — în cron procesul
# trăia câteva minute, dar OH e un proces FastAPI de lungă durată: un token memorat pe veci expiră și
# reparația începe să pice tăcut cu 401. Se invalidează și explicit la primul 401.
_VTOKEN: Dict[str, tuple] = {}
_VTOKEN_TTL = 1800.0                                    # 30 min


def _vtoken_get(key: str) -> Optional[str]:
    import time as _t
    v = _VTOKEN.get(key)
    if not v:
        return None
    tok, exp = v
    if exp <= _t.time():
        _VTOKEN.pop(key, None)
        return None
    return tok


def _vtoken_set(key: str, token: str) -> None:
    import time as _t
    _VTOKEN[key] = (token, _t.time() + _VTOKEN_TTL)


def _vtoken_drop(key: str) -> None:
    _VTOKEN.pop(key, None)


# ─────────────────────────── text: folding, tokeni, typo ───────────────────────────
def _fold(s: Any) -> str:
    t = "".join(c for c in unicodedata.normalize("NFKD", str(s or "")) if not unicodedata.combining(c))
    return t.lower().strip().replace("ı", "i")          # „i" fără punct (encoding stricat) → i


# Caractere care fac validatorul ORB: „Prıncıpala" (i turcesc), „Lāpușneanu" (macron = ă mis-encodat),
# ligaturi, zero-width. Diacriticele RO corecte (ă â î ș ț) NU se ating.
_FOREIGN_DIA = {"ı": "i", "ﬁ": "fi", "ﬂ": "fl", "​": "",
                "ā": "a", "ē": "e", "ī": "i", "ō": "o", "ū": "u",
                "à": "a", "è": "e", "ì": "i", "ò": "o", "ù": "u",
                "ä": "a", "ë": "e", "ï": "i", "ö": "o", "ü": "u"}


def clean_chars(s: Optional[str]) -> str:
    out = s or ""
    for k, v in _FOREIGN_DIA.items():
        if k in out:
            out = out.replace(k, v)
    return out


_ST_TYPE = (r"strada|stradela|str|soseaua|sos|bulevardul|bulevard|bdul|bd|blvd|aleea|alee|al|"
            r"calea|cale|intrarea|intrare|intr|drumul|drum|piata|pta|piateta|prelungirea|prelungire|"
            r"splaiul|splai|fundacul|fundac|pasajul|pasaj|cartierul|cartier|cart")
_ST_TYPE_RE = "|".join(sorted(_ST_TYPE.split("|"), key=len, reverse=True))
# ranguri/particule care NU disting o stradă de alta („General Dascălescu" ≡ „Dascălescu")
_ST_RANK = {"general", "gen", "doctor", "dr", "profesor", "prof", "inginer", "ing", "maior", "capitan",
            "colonel", "locotenent", "aviator", "pictor", "poet", "scriitor", "academician", "acad",
            "sfantul", "sf", "sfanta", "parintele", "preot", "episcop", "marele", "cel", "si", "de", "la"}


def _lev(a: str, b: str) -> int:
    """Distanța de editare (Levenshtein) — fuzzy în Python, nu în SQL (nomenclatorul n-are pg_trgm)."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if ca == cb else 1)))
        prev = cur
    return prev[-1]


def _tok_words(s: Any) -> List[str]:
    """Cuvintele unui text, cu cifrele lipite de litere separate („1mai" → ['1','mai']) și tipul de
    arteră lipit de nume desprins („bulevardulstefan" → ['bulevardul','stefan'])."""
    out: List[str] = []
    for t in re.findall(r"[a-z]+|\d+", _fold(s)):
        m = re.match(r"^(" + _ST_TYPE_RE + r")(.{3,})$", t)
        if m:
            out.extend([m.group(1), m.group(2)])
        else:
            out.append(t)
    return out


def _glued_ok(otok: List[str], sttok: List[str]) -> bool:
    """Clientul a scris cuvintele străzii LIPITE („Ionmihalache" = „Mihalache Ion").
    Cerem concatenare EXACTĂ a TUTUROR cuvintelor sugerate — nu substring, care acceptă „rosu" în
    „drosu" și SCHIMBĂ strada (măsurat: „Aviatorului Nicolae Drosu" → „Rosu Nicolae")."""
    if not (2 <= len(sttok) <= 3):
        return False
    joins = {"".join(p) for p in itertools.permutations(sttok)}
    return any(o in joins for o in otok)


_DECL_SUF = ["ului", "ilor", "elor", "lui", "lor", "ei", "ea", "ul", "le", "a", "i", "e"]


def _stem(w: str) -> str:
    """Taie terminația de declinare/articol RO (cea mai lungă) dacă rămâne o tulpină ≥4 litere."""
    for suf in _DECL_SUF:
        if len(w) - len(suf) >= 4 and w.endswith(suf):
            return w[:-len(suf)]
    return w


def _same_street_token(a: str, b: str) -> bool:
    """Același cuvânt de stradă, tolerând DECLINAREA RO (Grivitei=Grivita, Viteazu=Viteazul) + max 1 typo
    în TULPINĂ. Crinului ≠ Cornului (tulpini crin/corn diferite) → False."""
    if a == b:
        return True
    sa, sb = _stem(a), _stem(b)
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    return (sa[0] == sb[0] and abs(len(sa) - len(sb)) <= 1
            and _lev(sa, sb) <= 1 and min(len(sa), len(sb)) >= 5)


def _typo_ok(t: str, o: str, strict: bool = False) -> bool:
    """`o` (scris de client) = același cuvânt ca `t` (canonic), cu o greșeală mică?
    Gărzi ca să NU înlocuim o stradă cu ALTA: prima literă identică, lungimi apropiate, cuvinte lungi.
    Măsurat pe 356 străzi reale din 10 orașe: 88% recuperate, 0 străzi greșite."""
    if t == o:
        return True
    if not t or not o or t[0] != o[0] or abs(len(t) - len(o)) > 2:
        return False
    d = _lev(t, o)
    if strict:
        return d <= 1 and len(t) >= 6
    return (d <= 1 and len(t) >= 6) or (d <= 2 and len(t) >= 9)


# ─────────────────────────── adresă: gunoi, număr casă ───────────────────────────
def is_junk_address(a1: Optional[str]) -> bool:
    """Adresă fără conținut plauzibil de stradă: fără cifre, fără tip de arteră și fără niciun cuvânt
    de ≥5 litere („Nici eu nu", „Sc B", „Casa", „Jud. Olt"). NU o corectăm — am face-o doar să PARĂ
    validă (validatorul dă streetName 1.0 și pe text aiurea) și am trimite un colet în gol. Rămâne la CS."""
    f = _fold(a1)
    if re.search(r"\d", f):
        return False
    if re.search(r"\b(" + _ST_TYPE + r")\b", f):
        return False
    return not any(len(w) >= 5 for w in re.findall(r"[a-z]+", f))


_TRAIL_NUM = re.compile(r"\b(bl|bloc|sc|scara|ap|apt|apartament|et|etaj|interf|interfon)\b", re.I)


def _house_number(tok: Dict[str, Any], a1: str, street_digits: set) -> str:
    """Numărul casei, cu grijă să NU punem apartamentul/blocul/scara drept număr și să nu repetăm cifra
    din numele străzii („1 Decembrie 1918" → „…1"). Dacă nu-l știm sigur, mai bine FĂRĂ număr (curierul
    sună oricum) decât cu unul greșit — măsurat: „Ap. 49" ajungea „Nr 49", „Ogorului B1, Ap. 15" → „15"."""
    num = str(tok.get("streetNumber") or "").strip()
    bad = {str(tok.get(k) or "").strip().lower() for k in ("apartment", "building", "staircase", "floor")}
    bad.discard("")
    if num and num.lower() not in bad and num not in street_digits:
        return num
    m = re.search(r"\b(?:nr|numarul|nrul|no)\.?\s*(\d+\s*(?:[A-Za-z](?![A-Za-z]))?(?:\s*-\s*\d+)?)", a1, re.I)
    if m:
        c = re.sub(r"\s+", "", m.group(1))
        if c.lower() not in bad and c not in street_digits:
            return c
    # număr la FINAL („Str. Oprescu Dumitru 1") — doar dacă adresa n-are bloc/scară/apartament, altfel
    # finalul e apartamentul („…,AP.49,INTERF.49").
    if not _TRAIL_NUM.search(a1):
        m = re.search(r"(?<![\w])(\d+[A-Za-z]?)\s*$", a1.strip())
        if m:
            c = m.group(1)
            if c.lower() not in bad and c not in street_digits:
                return c
    return ""


def _street_part(a1: Optional[str]) -> str:
    """Partea de STRADĂ (până la primul bl/bloc/sc/ap/et), folded — acolo stă numărul casei."""
    return re.split(r"\b(bl|bloc|sc|scara|ap|apt|et|etaj|interfon)\b", _fold(a1))[0]


def _street_house_num(a1: Optional[str]) -> Optional[str]:
    """Numărul casei din partea de stradă (nr X / X, ÎNAINTE de bloc/scară/ap). None dacă lipsește."""
    sp = _street_part(a1)
    m = re.search(r"\bnr\.?\s*(\d+[a-z]?)\b", sp) or re.search(r"\b(\d+[a-z]?)\b", sp)
    return m.group(1) if m else None


def keeps_house_number(old_a1: Optional[str], new_a1: Optional[str]) -> bool:
    """GARDA CENTRALĂ: o rescriere de stradă are voie să curețe orice, dar NU să piardă sau să schimbe
    numărul casei. Dacă originalul n-avea număr, nu avem ce păzi (True)."""
    old = _street_house_num(old_a1)
    if not old:
        return True
    return _street_house_num(new_a1) == old


# ─────────────────────────── HTTP: validatorul xConnector ───────────────────────────
def _auth_headers(courier: Any, creds: Dict[str, Any]) -> Dict[str, str]:
    try:
        return courier._headers(creds)
    except Exception:
        return {"Authorization": "Bearer " + (creds.get("api_key") or ""), "Content-Type": "application/json"}


async def _validator_token(courier: Any, creds: Dict[str, Any]) -> Optional[str]:
    """Token-ul validatorului de adrese — ALT token decât cheia magazinului (POST /api/token).
    Cache-uit per cheie: validatorul e apelat de 2 ori per comandă (match + zip-code)."""
    key = creds.get("api_key") or ""
    if not key:
        return None
    cached = _vtoken_get(key)
    if cached:
        return cached
    try:
        r = await courier.http.post(XBASE + "/api/token", headers=_auth_headers(courier, creds))
        tok = (r.json() or {}).get("accessToken") if r.status_code == 200 else None
    except Exception as e:
        logger.info("xc /api/token a picat: %s", e)
        tok = None
    if tok:
        _vtoken_set(key, tok)
    return tok


async def match_address(courier: Any, creds: Dict[str, Any], ad: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Candidații validatorului xConnector pentru o adresă RO. [] la orice eroare (fail-safe)."""
    h = {"Content-Type": "application/json"}
    tok = await _validator_token(courier, creds)
    if tok:
        h["Authorization"] = "Bearer " + tok
    body = {"country": "Romania", "zipCode": ad.get("zip") or "", "county": ad.get("province") or "",
            "city": ad.get("city") or "", "address1": ad.get("address1") or "",
            "address2": ad.get("address2") or ""}
    try:
        r = await courier.http.post(VBASE + "/match-address", json=body, headers=h)
        if r.status_code == 401:                      # token expirat → îl aruncăm, tura viitoare ia altul
            _vtoken_drop(creds.get("api_key") or "")
            logger.info("xc /match-address 401 — token de validator invalidat")
            return []
        d = r.json()
    except Exception as e:
        logger.info("xc /match-address a picat: %s", e)
        return []
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        return d.get("matchers") or d.get("matches") or []
    return []


async def zip_confirm(courier: Any, creds: Dict[str, Any], zipc: Any) -> bool:
    """ZIP-ul propus EXISTĂ în nomenclatorul validatorului? Ultimul filtru înainte de scriere — un zip
    inventat rutează coletul în alt județ. Fail-safe: eroare/necunoscut = False (nu scriem)."""
    if not zipc:
        return False
    h: Dict[str, str] = {}
    tok = await _validator_token(courier, creds)
    if tok:
        h["Authorization"] = "Bearer " + tok
    try:
        r = await courier.http.get(VBASE + "/zip-code", params={"countryId": 1, "zipCode": str(zipc)}, headers=h)
        return bool(r.status_code == 200 and r.json())
    except Exception as e:
        logger.info("xc /zip-code a picat pt %s: %s", zipc, e)
        return False


def fscore(m: Dict[str, Any], k: str) -> Tuple[Any, float]:
    """(valoare, scor) dintr-un câmp de matcher xConnector."""
    v = m.get(k) or {}
    if isinstance(v, dict):
        return v.get("value"), float(v.get("score") or 0)
    return v, 0.0


# ─────────────────────────── pasul 2: validarea STOCATĂ pe comandă ───────────────────────────
def _zip_looks_real(cur_zip: str, nom: Dict[str, Any]) -> bool:
    """Codul poștal dat de client pare REAL? (În cron era un SELECT în nomenclator; aici deducem din
    verdictul runner-ului.) CONSERVATOR: True implicit — un zip pe care nu-l putem infirma NU se
    suprascrie. False doar când nomenclatorul propune ALT zip pentru ACEEAȘI localitate = zip greșit."""
    if not re.fullmatch(r"\d{6}", cur_zip or ""):
        return False
    addr = (nom or {}).get("address") or {}
    nz = str(addr.get("zip") or "").strip()
    nc, ic = _fold(addr.get("city")), _fold((nom or {}).get("_input_city"))
    if nz and nz != cur_zip and nc and ic and nc == ic:
        return False
    return True


def stored_validation_fix(d: Dict[str, Any], ad: Dict[str, Any], nom: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Corecție MINIMALĂ din validarea STOCATĂ pe comandă (`latestAddressValidation`), cu gărzi:
      · completează ZIP-ul gol/greșit cu cel găsit de validator (DOAR scor 1.0);
      · completează CITY/COUNTY greșit (scor 1.0, sau ancorat de un zip 1.0);
      · CURĂȚĂ strada cu zgomot (scor <0.99) DOAR dacă e ACEEAȘI stradă în textul original.
    Întoarce `applied` (adresa completă) doar dacă s-a schimbat ceva; altfel None. Gate: overall ≥0.85."""
    ms = ((d.get("latestAddressValidation") or {}).get("addressMatchers")) or []
    if not ms or is_junk_address(ad.get("address1") or ""):
        return None
    m = ms[0]
    st_v, st_s = fscore(m, "streetName")
    city_v, city_s = fscore(m, "city")
    zip_v, zip_s = fscore(m, "zipCode")
    county_v, county_s = fscore(m, "county")
    ov = float(m.get("score") or 0)
    tok = m.get("tokenizedAddress") or {}
    if ov < 0.85:
        return None

    applied = dict(ad)
    applied["country"] = "Romania"
    changed = False
    for _k in ("address1", "address2", "city"):
        _cv = clean_chars(ad.get(_k))
        if _cv != (ad.get(_k) or "") and _cv.strip():
            applied[_k] = _cv
            changed = True                       # „Prıncıpala" (i fără punct) orbea validatorul

    cur_zip = str(ad.get("zip") or "").strip().strip("-").strip()
    if zip_v and zip_s >= 0.99 and cur_zip != str(zip_v):
        # Un cod poștal VALID dat de client NU se mută în altă localitate (validatorul caută strada în tot
        # județul când orașul e scris greșit și întoarce alt sat cu scor 1.0). Completăm liber doar codul
        # lipsă/nereal; rafinăm segmentul doar în ACEEAȘI localitate.
        _cv, _cc = _fold(city_v or ""), _fold(ad.get("city") or "")
        _same_loc = bool(_cv) and (_cv == _cc or _typo_ok(_cv, _cc) or _typo_ok(_cc, _cv))
        if (not _zip_looks_real(cur_zip, nom)) or _same_loc:
            applied["zip"] = str(zip_v)
            changed = True
        else:
            zip_v = None                          # zip refuzat → nu ancorăm nici orașul/județul pe el

    # ZIP-ul confirmat (scor 1.0) ANCOREAZĂ locația → orașul/județul corect = ce zice validatorul, chiar
    # dacă scorul lor per-câmp e mic (prefix „Com", typo „Buvuresti", oraș duplicat în câmp).
    zip_anchored = bool(zip_v and zip_s >= 0.99)
    if city_v and _fold(city_v) != _fold(ad.get("city") or "") \
            and (city_s >= 0.99 or (zip_anchored and ov >= 0.95 and city_s >= 0.6)):
        applied["city"] = str(city_v).title()
        changed = True
    if county_v and _fold(county_v) != _fold(ad.get("province") or "") \
            and (county_s >= 0.99 or (zip_anchored and ov >= 0.95)):
        applied["province"] = str(county_v).title()
        changed = True

    if st_v and 0.5 <= st_s < 0.99:
        # scoate lămuririle din paranteze ale CLIENTULUI („Mihai Viteazu(Caminele I.M.R.)") — adaugă tokeni
        # de zgomot care sparg acoperirea; strada e în afara parantezei.
        orig_a1 = re.sub(r"\([^)]*\)", " ", _fold(ad.get("address1") or ""))
        _stv = re.sub(r"\([^)]*\)", " ", _fold(st_v))       # și lămuririle din nomenclator
        _sall = [t for t in re.split(r"[^a-z0-9]+", _stv) if t]
        _sttok = [t for t in _sall if not t.isdigit() and len(t) >= 3]
        # Comparăm DOAR cu ce a parsat validatorul ca fiind STRADA (tokenizedAddress.streetName), nu cu tot
        # textul adresei: altfel sugestia se „potrivea" pe alt cuvânt și SCHIMBA strada — măsurat pe comenzi
        # reale: „Nicolina str prof. Ion Inculeț" → „Strada Nicolina" (cartier).
        _base = _fold(tok.get("streetName") or "") or orig_a1
        _otok = _tok_words(_base)
        # CIFRELE fac parte din numele străzii: „Orizont 9" NU e „Orizont 1", „1 Decembrie 1918" nu e
        # „22 Decembrie". Sugestie cu o cifră pe care clientul n-a scris-o → ALTĂ stradă.
        _sdig = {t for t in _sall if t.isdigit()}
        _odig = {t for t in _otok if t.isdigit()}
        # fuzzy (typo/declinare/inițială) DOAR când validatorul e sigur pe stradă (≥0.85); sub asta o
        # diferență de 1 literă poate fi altă stradă reală (Cringului=Crangului ≠ Crinului).
        _fuzzy_ok = st_s >= 0.85

        def _hit(t: str) -> bool:
            if t in _otok:
                return True                                   # potrivire EXACTĂ → mereu ok
            if not _fuzzy_ok:
                return False
            if any(_typo_ok(t, o) for o in _otok):
                return True
            if any(_same_street_token(t, o) for o in _otok):  # declinare RO (Grivitei=Grivita)
                return True
            return len(t) > 1 and any(len(o) == 1 and o == t[0] for o in _otok)   # „M.Basarab"

        if not (_sttok and _sdig <= _odig and (_glued_ok(_otok, list(_sttok)) or all(_hit(t) for t in _sttok))):
            _sttok = []
        if _sttok:
            num = _house_number(tok, ad.get("address1") or "", _sdig)
            stype = (str(tok.get("streetType") or "Strada")).strip().title() or "Strada"
            _stname = re.sub(r"\s*\([^)]*\)", "", str(st_v)).strip()
            new_a1 = ("%s %s%s" % (stype, _stname.title(), ((" " + num) if num else ""))).strip()
            if _fold(new_a1) != orig_a1 and keeps_house_number(ad.get("address1"), new_a1):
                applied["address1"] = new_a1
                changed = True
    return applied if changed else None


# ─────────────────────────── pasul 3: nomenclatorul OH ───────────────────────────
async def nomenclator_verdict(ad: Dict[str, Any]) -> Dict[str, Any]:
    """`runner.validate_address` pe adresa xConnector. {} la orice eroare (fail-safe — nomenclatorul
    indisponibil NU trebuie să oprească restul lanțului)."""
    try:
        from services.nomenclator import runner as _runner
        res = await _runner.validate_address({
            "country": "Romania", "province": ad.get("province") or "", "city": ad.get("city") or "",
            "zip": ad.get("zip") or "", "address1": ad.get("address1") or "",
            "address2": ad.get("address2") or "",
        }) or {}
    except Exception as e:
        logger.info("nomenclator (runner) a picat: %s", e)
        return {}
    res["_input_city"] = ad.get("city") or ""
    return res


def nomenclator_fix(ad: Dict[str, Any], nom: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Adresa corectată de nomenclator, cu ACELEAȘI gărzi ca restul: nu golim câmpuri, nu pierdem numărul
    casei. Doar verdictul `corrected` produce o scriere (valid = n-are ce corecta; cs/needs_geocoder = om)."""
    if (nom or {}).get("status") != "corrected":
        return None
    out = (nom or {}).get("address") or {}
    if not out:
        return None
    applied = dict(ad)
    applied["country"] = "Romania"
    changed = False
    for k in ("province", "city", "zip"):
        v = out.get(k)
        if v and _fold(v) != _fold(ad.get(k) or ""):
            applied[k] = str(v)
            changed = True
    na1 = out.get("address1")
    if na1 and _fold(na1) != _fold(ad.get("address1") or ""):
        if keeps_house_number(ad.get("address1"), na1):
            applied["address1"] = str(na1)
            changed = True
        else:
            logger.info("nomenclator: refuz address1 '%s' (pierde numărul casei din '%s')",
                        na1, ad.get("address1"))
    return applied if changed else None


# ─────────────────────────── orchestrarea ───────────────────────────
def needs_repair(xc_order: Dict[str, Any]) -> bool:
    """Comanda are adresa marcată de xConnector ca blocantă (WRONG/UNKNOWN) și n-are încă etichetă."""
    if (xc_order or {}).get("addressStatus") not in BAD_STATUSES:
        return False
    for d in (xc_order.get("documents") or []):
        if isinstance(d, dict) and d.get("documentType") == "SHIPPING_LABEL":
            return False
    return True


def _diff(ad: Dict[str, Any], applied: Dict[str, Any]) -> Dict[str, Any]:
    """Doar câmpurile CHIAR schimbate — asta se trimite la ai-correct-address."""
    out: Dict[str, Any] = {}
    for k in ("address1", "address2", "city", "zip", "province", "country"):
        v = applied.get(k)
        if v not in (None, "") and _fold(v) != _fold(ad.get(k) or ""):
            out[k] = v
    return out


def _detail(applied: Dict[str, Any]) -> str:
    return "%s, %s %s (%s)" % (applied.get("address1"), applied.get("city"),
                               applied.get("zip"), applied.get("province"))


def _res(changed: bool, corr: Optional[Dict[str, Any]], note: str,
         status: str = "manual", source: str = "") -> Dict[str, Any]:
    return {"changed": bool(changed), "corr": dict(corr or {}), "note": note,
            "status": status, "source": source}


async def repair_address(courier: Any, creds: Dict[str, Any], xc_order: Dict[str, Any], order: Any,
                         *, apply: bool = False, store: Any = None) -> Dict[str, Any]:
    """Repară CONSERVATOR adresa RO a unei comenzi xConnector marcate WRONG/UNKNOWN.

    courier   = XConnectorCourier (pt HTTP + `ai_correct_address`)
    creds     = credențialele contului xConnector al magazinului
    xc_order  = comanda xConnector (`xc_order_by_shopify_id`) — trebuie să aibă shippingAddress + hash-urile
    order     = models.Order (doar pt log + oglindirea în Shopify)
    apply     = False (implicit) → DRY-RUN: calculează, nu scrie nimic
    store     = models.Store opțional; dat + apply → oglindește corecția și în Shopify (best-effort)

    Întoarce {changed, corr, note, status, source}:
      changed = s-a schimbat ceva (la apply=False = „AR schimba")
      corr    = doar câmpurile modificate (formatul cerut de `ai_correct_address`)
      note    = explicația (mesajul pt CS când nu s-a putut repara)
      status  = would-correct | corrected | manual | error
    NU aruncă NICIODATĂ: orice excepție devine {changed: False, status: 'error'}."""
    name = getattr(order, "name", None) or (xc_order or {}).get("orderName") or "?"
    try:
        ad = dict((xc_order or {}).get("shippingAddress") or {})
        if not (xc_order or {}).get("orderId") or not ad:
            return _res(False, None, "comanda xConnector n-are adresă de livrare")

        # ── GARDA 0a: RO-ONLY. Nomenclatorul RO + country='Romania' ar transforma o adresă CZ/PL/BG
        # într-una „românească". INTL are calea lui (dpd_intl + sanitize_intl).
        cc = _country_code(ad.get("country") or getattr(order, "shipping_country", None) or "Romania")
        if cc != "RO":
            return _res(False, None, "non-RO (%s) → INTL are cale proprie (dpd_intl/sanitize_intl)" % cc)

        # ── GARDA 0b: are DEJA etichetă → NU corecta (adresa e deja tipărită; corecția ar oscila
        # zip-ul la fiecare rulare). Sursa de adevăr devine eticheta.
        for doc in (xc_order.get("documents") or []):
            if isinstance(doc, dict) and doc.get("documentType") == "SHIPPING_LABEL":
                return _res(False, None, "are deja AWB/etichetă → nu corectez (evit oscilația)")

        # ── GARDA 0c: adresă-GUNOI („Nici eu nu", „Casa", „Sc B") → CS, nu auto-corecție. Validatorul dă
        # streetName 1.0 și pe text aiurea: am face adresa să PARĂ validă și am trimite coletul în gol.
        if is_junk_address(ad.get("address1") or ""):
            return _res(False, None, "adresă fără conținut de stradă → CS (sună clientul)")

        # Nomenclatorul OH — o SINGURĂ dată; verdictul lui servește și garda de zip, și pasul 3.
        nom = await nomenclator_verdict(ad)

        # ── GARDA 0d: ZIP-ul CLIENTULUI e confirmat de nomenclator ca parte dintr-o adresă VALIDĂ →
        # NU-l suprascriem. (În cron: „zip client pe localitatea lui, rural ≤3 coduri". A rezolvat
        # over-corecția pe omonime rurale — 2× Popești/Vâlcea. Aici verdictul `valid` al runner-ului e
        # cel puțin la fel de tare: include garda de omonimie #10.)
        cz = re.sub(r"\D", "", ad.get("zip") or "")
        # Garda protejează ZIP-ul, NU oprește reparația: dacă nomenclatorul confirmă adresa, nu-i mai
        # atingem codul poștal, dar restul (strada canonică, județ/oraș, format) se poate repara în
        # continuare — altfel o adresă respinsă de curier pentru numele străzii rămânea nereparată doar
        # pentru că zip-ul era bun. (Cronul bloca doar suprascrierea zip-ului.)
        zip_locked = bool(len(cz) == 6 and cz != "000000" and nom.get("status") == "valid")
        if zip_locked:
            logger.info("repair: zip %s confirmat de nomenclator — îl păstrez, repar restul", cz)

        applied: Optional[Dict[str, Any]] = None
        source = ""
        why = ""

        # ── PASUL 1: CANDIDAT TARE la validatorul xConnector ──
        msl = await match_address(courier, creds, ad)
        # zip/oraș/județ ≥0.95 + stradă ≥0.90 (relaxat — există plasa de siguranță DPD/client la preluare).
        # UN singur candidat (fără competitor) = nu riscăm o adresă validă-dar-greșită.
        strong = [m for m in msl
                  if all(fscore(m, f)[1] >= 0.95 for f in ("zipCode", "county", "city"))
                  and fscore(m, "streetName")[1] >= 0.90]
        if len(strong) != 1:
            why = "%d candidați (zip/oraș/județ≥0.95, stradă≥0.90)" % len(strong)
        else:
            m = strong[0]
            czip = str(fscore(m, "zipCode")[0] or "")
            ccity = fscore(m, "city")[0] or ad.get("city") or ""
            ccounty = fscore(m, "county")[0] or ad.get("province") or ""
            tok = m.get("tokenizedAddress") or {}
            orig_nums = re.findall(r"\b(\d+[A-Za-z]?)\b", ad.get("address1") or "")
            num = (tok.get("streetNumber") or "").strip()
            if not num and len(orig_nums) == 1:
                num = orig_nums[0]
            if not num or (orig_nums and num not in orig_nums):
                # numărul casei trebuie să vină din ce a scris CLIENTUL — altfel mutăm coletul pe stradă
                why = "număr casă nesigur"
            elif not await zip_confirm(courier, creds, czip):
                why = "zip neconfirmat"
            else:
                # construiește adresa: păstrează TOT, înlocuiește doar core-ul; strada canonică doar dacă
                # diferă după folding (numele CANONIC al matcher-ului, nu forma tokenizată a clientului)
                stype = (tok.get("streetType") or "").strip()
                sname = (fscore(m, "streetName")[0] or tok.get("streetName") or "").strip()
                new_a1 = ad.get("address1")
                if _fold(stype + " " + sname) != _fold(ad.get("address1") or ""):
                    new_a1 = ("%s %s Nr. %s" % (stype.title(), str(sname).title(), num)).strip()
                cand = dict(ad)
                cand["country"] = "Romania"
                if _fold(ccounty) != _fold(ad.get("province") or ""):
                    cand["province"] = str(ccounty).title()
                if _fold(ccity) != _fold(ad.get("city") or ""):
                    cand["city"] = str(ccity).title()
                cand["zip"] = czip
                cand["address1"] = new_a1
                if not keeps_house_number(ad.get("address1"), new_a1):
                    why = "rescrierea ar pierde numărul casei"
                else:
                    applied, source = cand, "match-address"

        # ── LANȚUL DE REZERVĂ. Se intră din TOATE ieșirile „manual", nu doar când lipsește candidatul
        # tare: un candidat tare care pică pe „număr casă nesigur" nu înseamnă că adresa nu se poate
        # repara altfel (lecție din cron).
        if applied is None:
            applied = stored_validation_fix(xc_order, ad, nom)
            source = "validare-stocată" if applied else ""
        if applied is None:
            applied = nomenclator_fix(ad, nom)
            source = "nomenclator" if applied else ""

        if applied is None:
            note = why or (nom.get("note") or "nicio corecție sigură")
            return _res(False, None, "%s → CS" % note)

        corr = _diff(ad, applied)
        if not corr:
            return _res(False, None, "corecția e identică cu adresa curentă → nimic de scris")
        detail = "[%s] %s" % (source, _detail(applied))
        if not apply:
            return _res(True, corr, detail, status="would-correct", source=source)

        ok = await courier.ai_correct_address(creds, xc_order, corr, "RO")
        if not ok:
            return _res(False, corr, "ai-correct-address a refuzat scrierea: %s" % detail,
                        status="error", source=source)
        logger.info("ADDR-REPAIR order=%s [%s] %s", name, source, detail)
        if store is not None:
            await _mirror_to_shopify(store, order, applied)     # best-effort, nu rupe nimic
        return _res(True, corr, detail, status="corrected", source=source)
    except Exception as e:                                       # FAIL-SAFE: nu rupem crearea de AWB
        logger.warning("repair_address a picat pt %s: %s", name, e, exc_info=True)
        return _res(False, None, "eroare internă: %s" % e, status="error")


async def _mirror_to_shopify(store: Any, order: Any, applied: Dict[str, Any]) -> bool:
    """Oglindește adresa corectată și în Shopify (sursa din care xConnector/Frisbo/SmartBill re-sincronizează).
    Best-effort — un eșec aici NU invalidează corecția din xConnector.

    ⚠️ `orderUpdate` ÎNLOCUIEȘTE shippingAddress, nu face merge → trimitem adresa COMPLETĂ, cu numele și
    telefonul luate din OH (care le are neredactate). Fără nume în OH NU scriem — am goli destinatarul.
    GARDA „ACEEAȘI ADRESĂ": scriem doar dacă e ACEEAȘI adresă curățată, nu una DIFERITĂ — dacă între timp
    clientul/CS a mutat comanda în alt oraș sau pe altă stradă, NU suprascriem."""
    try:
        from services import shopify_service
        if not getattr(order, "shopify_order_id", None):
            return False
        oh_name = getattr(order, "shipping_name", None)
        if not oh_name:
            return False                                        # fără PII → risc să golim destinatarul
        oc, ac = _fold(getattr(order, "shipping_city", None)), _fold(applied.get("city"))
        if oc and ac and not (oc == ac or _typo_ok(oc, ac) or _typo_ok(ac, oc) or oc in ac or ac in oc):
            logger.info("shopify-mirror skip %s: alt oraș în OH (%s) vs corecție (%s)",
                        getattr(order, "name", "?"), oc, ac)
            return False
        # GARDA STRADĂ: corecția trebuie să fie o CURĂȚARE a aceleiași străzi (token distinctiv ≥5 comun).
        at = {t for t in re.findall(r"[a-z]+", _fold(applied.get("address1"))) if len(t) >= 5 and t not in _ST_RANK}
        ot = {t for t in re.findall(r"[a-z]+", _fold(getattr(order, "shipping_address1", None))) if len(t) >= 5 and t not in _ST_RANK}
        if at and ot and not any(a == o or _typo_ok(a, o) or _same_street_token(a, o) for a in at for o in ot):
            logger.info("shopify-mirror skip %s: altă stradă în OH decât în corecție", getattr(order, "name", "?"))
            return False
        addr = {"name": oh_name,
                "address1": applied.get("address1") or getattr(order, "shipping_address1", None),
                "address2": applied.get("address2") or getattr(order, "shipping_address2", None) or "",
                "city": applied.get("city") or getattr(order, "shipping_city", None),
                "zip": applied.get("zip") or getattr(order, "shipping_zip", None),
                "province": applied.get("province") or getattr(order, "shipping_province", None),
                "country": getattr(order, "shipping_country", None) or "Romania",
                "phone": getattr(order, "shipping_phone", None)}
        addr = {k: v for k, v in addr.items() if v not in (None, "")}
        await shopify_service.update_order_shipping_address(store, order.shopify_order_id, addr)
        return True
    except Exception as e:
        logger.info("shopify-mirror a picat pt %s: %s", getattr(order, "name", "?"), e)
        return False


def _country_code(country: Any) -> str:
    """Codul ISO2 al țării (prin nomenclatorul intl al OH). Gol/necunoscut → RO (default-ul producției)."""
    try:
        from services.nomenclator.intl import country_code
        return (country_code(country or "RO") or "RO").upper()
    except Exception:
        f = _fold(country)
        return "RO" if f in ("", "romania", "ro", "rou", "românia") else "??"


__all__ = ["repair_address", "needs_repair", "match_address", "zip_confirm",
           "nomenclator_verdict", "is_junk_address", "keeps_house_number", "BAD_STATUSES"]
