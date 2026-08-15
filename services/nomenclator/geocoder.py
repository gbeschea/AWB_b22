"""
geocoder.py — poarta HERE (a doua opinie pt adresele pe care nomenclatoarele nu le decid), PORT VERBATIM
din xconnector.py de producție (funcțiile here_* + helper-ele lor, commit 10cdd69). NU rescrie logica —
gărzile au fost calibrate pe incidente reale:
 • street-match: fără el, fallback-ul fără-tip accepta o stradă GREȘITĂ cu scor mare ('Str Tineretului'→
   HERE '21 Decembrie' 0.99) — un token distinctiv (≥5 litere, non-rang) comun, cu declinare RO tolerată;
 • garda oraș la zip-fill: fără ea ~1/60 completări veneau din ALT oraș (misroute); cu ea 0;
 • _street_core taie bloc/scara/ap din query (detaliile îneacă scorul: 'Magheru 9 bl Eva sc 3'→0.00, fără→1.00);
 • i↔â iexact: 'Birsei'≡'Bârsei' → prag coborât 0.75 (HERE știe forma corectă, clientul a scris 'i').
Adaptări FĂRĂ schimbare de comportament: (1) refinarea zip-ului autoritativ primește cursorul OH (aceeași
schemă romania_addresses) în loc de metrics_cursor_live(); (2) country_iso3() pt maparea ISO2→ISO3 HERE.
Apelat din runner DOAR când runner-ul e autoritar (nu în shadow — ar dubla quota HERE cu cronul).
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

HERE_MIN_SCORE = 0.9

ISO3 = {"RO": "ROU", "CZ": "CZE", "PL": "POL", "BG": "BGR", "HU": "HUN", "SK": "SVK",
        "MD": "MDA", "DE": "DEU", "AT": "AUT", "IT": "ITA", "ES": "ESP", "FR": "FRA", "GR": "GRC"}


def country_iso3(cc2):
    return ISO3.get((cc2 or "").strip().upper()[:2])


def http(method, url, headers, body=None, timeout=45):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:
        return "ERR", str(e)[:160]


# ——— helper-e de matching (verbatim din xconnector.py) ———

def _fold(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", (s or "").strip().lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _foldi(s):
    """Fold pt comparație STRADĂ care unifică â/î cu i — românii scriu 'i' unde oficialul are 'â/î'
    ('Birsei'≡'Bârsei', 'Tirgului'≡'Târgului'). Mapează â/î→i ÎNAINTE de strip diacritice (care ar da â→a)."""
    return _fold((s or "").replace("â", "i").replace("Â", "i").replace("î", "i").replace("Î", "i"))


def _lev(a, b):
    """Distanta de editare (Levenshtein). Fuzzy în Python, fără extensii SQL."""
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


_DECL_SUF = ["ului", "ilor", "elor", "lui", "lor", "ei", "ea", "ul", "le", "a", "i", "e"]


def _stem(w):
    """Taie terminatia de declinare/articol RO (cea mai lunga) daca ramane o tulpina >=4 litere."""
    for suf in _DECL_SUF:
        if len(w) - len(suf) >= 4 and w.endswith(suf):
            return w[:-len(suf)]
    return w


def _same_street_token(a, b):
    """Acelasi cuvant de strada, tolerand DECLINAREA RO (Grivitei=Grivita, Viteazu=Viteazul) + max 1 typo
    in TULPINA. Crinului≠Cornului (tulpini crin/corn diferite) → False. Conservator la singular/plural."""
    if a == b:
        return True
    sa, sb = _stem(a), _stem(b)
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    return (sa[0] == sb[0] and abs(len(sa) - len(sb)) <= 1
            and _lev(sa, sb) <= 1 and min(len(sa), len(sb)) >= 5)


def _typo_ok(t, o, strict=False):
    """`o` (scris de client) = acelasi cuvant ca `t` (canonic), cu o greseala mica?
    Garzi ca sa NU inlocuim o strada cu ALTA: prima litera identica, lungimi apropiate, cuvinte lungi.
    strict=True (ruta NOMENCLATOR, fara context de adresa) = maxim O litera diferenta."""
    if t == o:
        return True
    if not t or not o or t[0] != o[0] or abs(len(t) - len(o)) > 2:
        return False
    d = _lev(t, o)
    if strict:
        return d <= 1 and len(t) >= 6
    return (d <= 1 and len(t) >= 6) or (d <= 2 and len(t) >= 9)


_ST_RANK = {"general", "gen", "doctor", "dr", "profesor", "prof", "inginer", "ing", "maior", "capitan",
            "colonel", "locotenent", "aviator", "pictor", "poet", "scriitor", "academician", "acad",
            "sfantul", "sf", "sfanta", "parintele", "preot", "episcop", "marele", "cel", "si", "de", "la"}

_ST_TYPE_STRIP_RE = re.compile(r"(?i)^\s*(strada|str|stra|bulevardul|bdul|b-dul|bd|blvd|calea|cal|[sș]oseaua|sos|aleea|alee|"
                               r"intrarea|intr|drumul|splaiul|prelungirea|fundatura|pia[țt]a)\.?\s+")


def _strip_street_type(s):
    """Scoate tipul de arteră din față ('Str/Bd/Aleea/Calea X'→'X') — pt fallback-ul HERE pe NUME când clientul
    a scris tipul GREȘIT (client 'Str Tineretului' dar e 'Aleea Tineretului'; 'STR Brătianu' dar e Bulevard)."""
    return _ST_TYPE_STRIP_RE.sub("", s or "").strip()


def _street_core(a1):
    """Miezul străzii pt QUERY-ul HERE: stradă + număr, TĂIAT la primul bloc/scară/etaj/ap/interfon.
    Detaliile de apartament ÎNEACĂ scorul geocoderului. NU taie strada reală ('Blanari'/'Scarlat'/'Apahida'
    — bl/sc/ap urmat de LITERĂ, nu cifră, rămân întregi). Adresa STOCATĂ nu se schimbă — doar interogarea."""
    if not a1:
        return a1
    m = re.search(r"(?i)\b(bloc|scara|etaj|apartament|interfon|corp|tronson|demisol|mansarda)\b"
                  r"|\b(bl|sc|et|ap|apt)\.?\s*[a-z]?\d", a1)
    if not m:
        return a1
    core = a1[:m.start()].strip(" ,.-")
    return core if len(core) >= 4 else a1


# ——— HERE Geocoding (verbatim) ———

def _here_geocode_q(q, country, key):
    if not q:
        return None
    url = ("https://geocode.search.hereapi.com/v1/geocode?q=%s&in=countryCode:%s&apiKey=%s"
           % (urllib.parse.quote(q), country, urllib.parse.quote(key)))
    s, b = http("GET", url, {})
    try:
        items = json.loads(b).get("items") or []
    except Exception:
        return None
    if not items:
        return None
    it = items[0]; a = it.get("address") or {}
    return {"score": float((it.get("scoring") or {}).get("queryScore", 0)), "rt": it.get("resultType"),
            "zip": a.get("postalCode"), "street": a.get("street") or "", "city": a.get("city") or ""}


def here_geocode(addr, country, key):
    """Geocodare HERE completa: {score, rt, zip, street, city}. None la eroare. country = ISO3 (ROU/CZE/…)."""
    if not key or not country:
        return None
    _z = str(addr.get("zip") or "").strip()
    _z = _z if re.fullmatch(r"\d{6}", _z) else ""   # zip gunoi (ex "-") strica scorul HERE -> il scot din query
    core = _street_core(addr.get("address1"))
    g = _here_geocode_q(", ".join([x for x in [core, addr.get("address2"), addr.get("city"), _z] if x]), country, key)
    # FALLBACK: scor mic = poate tip-ul de arteră e GREȘIT (client 'Str' dar e Bulevard/Aleea) → caută pe NUME.
    # Garda street-match (aval) verifică că e ACEEAȘI stradă → un match greșit pe nume e respins.
    if not g or g["score"] < HERE_MIN_SCORE:
        core2 = _strip_street_type(core)
        if core2 and core2 != core:
            g2 = _here_geocode_q(", ".join([x for x in [core2, addr.get("address2"), addr.get("city"), _z] if x]), country, key)
            if g2 and (not g or g2["score"] > g["score"]):
                return g2
    return g


def _here_street_match(client_a1, here_street):
    """Strada HERE = aceeasi cu ce a scris clientul? Un token distinctiv (>=5 litere, non-rang) comun
    (exact sau 1 typo). Fara asta nu completam zip-ul (poate fi alta strada la alt cod postal)."""
    ct = [t for t in re.findall(r"[a-z]+", _fold(client_a1)) if len(t) >= 5 and t not in _ST_RANK]
    ht = [t for t in re.findall(r"[a-z]+", _fold(here_street)) if len(t) >= 5 and t not in _ST_RANK]
    if not ct or not ht:
        return False
    return any(c == h or _same_street_token(c, h) or _typo_ok(h, c) or _typo_ok(c, h) for c in ct for h in ht)


def _here_street_iexact(client_a1, here_street):
    """Strada clientului = strada HERE EXACT modulo â/î↔i ('Birsei'≡'Bârsei'). Semnal PUTERNIC (identitate,
    nu doar overlap) → permite accept sub 0.9. Toți tokenii distinctivi au corespondent EXACT (după _foldi)."""
    ct = [t for t in re.findall(r"[a-z]+", _foldi(client_a1)) if len(t) >= 5 and t not in _ST_RANK]
    ht = {t for t in re.findall(r"[a-z]+", _foldi(here_street)) if len(t) >= 5}
    return bool(ct) and all(c in ht for c in ct)


def here_street_ok(ad, key, country="ROU", min_score=HERE_MIN_SCORE):
    """HERE confirmă STRADA+ORAȘUL (scor≥prag, casă/stradă, street-match, oraș-match) — chiar dacă n-are ZIP
    unic de completat → acceptăm adresa AS-IS."""
    g = here_geocode(ad, country, key)
    if not g or g["score"] < min_score or g["rt"] not in ("houseNumber", "street"):
        return False
    if not _here_street_match(ad.get("address1") or "", g["street"]):
        return False
    _cc, _hc = _fold(ad.get("city") or ""), _fold(g["city"] or "")
    if _cc and _hc and not (_cc == _hc or _typo_ok(_cc, _hc) or _typo_ok(_hc, _cc) or _cc in _hc or _hc in _cc):
        return False
    return True


def here_zip_fill(ad, key, cur=None, min_score=HERE_MIN_SCORE):
    """Completeaza zip-ul RO LIPSA din HERE (>=prag, casa/strada, strada confirmata, garda oras). Prefera
    zip-ul autoritativ din nomenclator (strada HERE in orasul HERE, daca iese UNIC — `cur` = cursorul OH,
    aceeași schemă romania_addresses). NU suprascrie un zip valid; NU atinge strada. → {'zip':..} sau None."""
    cur_zip = str(ad.get("zip") or "").strip().strip("-").strip()
    if re.fullmatch(r"\d{6}", cur_zip):
        return None
    g = here_geocode(ad, "ROU", key)
    if not g or g["rt"] not in ("houseNumber", "street"):
        return None
    _iexact = _here_street_iexact(ad.get("address1") or "", g["street"])
    if g["score"] < (0.75 if _iexact else min_score):
        return None
    if not (g["zip"] and re.fullmatch(r"\d{6}", g["zip"])):
        return None
    if not _here_street_match(ad.get("address1") or "", g["street"]):
        return None
    # GARDA ORAS: zip-ul HERE sa fie din ACELASI oras cu ce a scris clientul (fara ea ~1/60 = misroute).
    _cc, _hc = _fold(ad.get("city") or ""), _fold(g["city"] or "")
    if _cc and _hc and not (_cc == _hc or _typo_ok(_cc, _hc) or _typo_ok(_hc, _cc) or _cc in _hc or _hc in _cc):
        return None
    zc = g["zip"]
    # rafinare: zip autoritativ din nomenclator pt strada+oras HERE (daca e unic)
    try:
        if cur is not None and g["street"] and g["city"]:
            stoks = [t for t in re.split(r"[^a-z0-9]+", _fold(g["street"])) if len(t) >= 4 and t not in _ST_RANK]
            if stoks:
                cur.execute("SELECT DISTINCT cod_postal, nume_strada FROM public.romania_addresses "
                            "WHERE localitate_norm=%s AND cod_postal ~ '^[0-9]{6}$'", (_fold(g["city"]),))
                zz = {z for z, nm in cur.fetchall()
                      if all(any(st == t or _typo_ok(t, st, strict=True) for t in _fold(nm).split()) for st in stoks)}
                if len(zz) == 1:
                    zc = list(zz)[0]
    except Exception:
        pass
    return {"zip": zc}
