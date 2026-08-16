"""
intl.py — validare adrese INTERNAȚIONALE (CZ/PL/BG/HU/SK) pe nomenclatoarele din DB-ul OH.

RO merge pe validatorul bogat (address_nomenclator). Aici: rutare pe țară + validare localitate+cod-poștal+stradă
pe tabelele intl (CZ/PL=OpenAddresses, BG/HU/SK=OSM+GeoNames). Sync (cursor psycopg2, ca RO), apelat din runner prin
executor. Întoarce {status, address, source, note} cu 4 stări (valid/corrected/needs_geocoder/cs), la fel ca RO.

Reguli (calibrate pe comenzi reale AWBprint, 2026-08-16):
 • fold aliniat cu *_norm din tabele: lowercase, fără diacritice, ł→l (NFKD NU descompune ł!), separatori→spațiu;
 • candidați de oraș (tiparul universal din nomenclatoarele xconnector): strip prefixe gr/с./кв/obec…, strip
   tokeni numerici/romani ("Praha 8", "Kostomlaty n.L. 28921", "Kolín V"), split pe virgulă/paranteză + n-grame
   din față ("Trявна, кв. X"→Трявна; "Sosnowiec Milowice"→Sosnowiec);
 • BG se potrivește și pe name_lat (translit: "Dobrich"→Добрич);
 • ZIP lipsă / format greșit / inexistent → derivăm din localitate (unic sau îngustat pe stradă) = corrected —
   lecția CZ/WPO PR #530: curierul validează STRICT codul, un cod inexistent blochează o adresă livrabilă;
 • strada = verificare SOFT (nepotrivirea doar notează, nu blochează). Necunoscut → needs_geocoder (HERE).
"""
import re
import unicodedata

_TRANSLIT = str.maketrans({"ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ø": "o", "Ø": "o",
                           "ß": "ss", "æ": "ae", "œ": "oe", "þ": "th", "ð": "d"})


def _fold(s):
    s = unicodedata.normalize("NFKD", (s or "").strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c)).translate(_TRANSLIT)
    s = re.sub(r"[^0-9a-zа-яё]+", " ", s)   # litere latine + chirilice; restul → spațiu
    return re.sub(r"\s+", " ", s).strip()


def _digits(s):
    return re.sub(r"\D", "", s or "")


# nume/țară → ISO2
_CC = {
    "romania": "RO", "ro": "RO",
    "bulgaria": "BG", "bg": "BG", "balgariya": "BG", "българия": "BG",
    "hungary": "HU", "hu": "HU", "magyarorszag": "HU",
    "czechia": "CZ", "czech republic": "CZ", "cesko": "CZ", "ceska republika": "CZ", "cz": "CZ",
    "poland": "PL", "polska": "PL", "pl": "PL",
    "slovakia": "SK", "slovensko": "SK", "sk": "SK",
    "moldova": "MD", "republica moldova": "MD", "moldova republic of": "MD", "md": "MD",
}


def country_code(s):
    f = _fold(s)
    return _CC.get(f, (s or "").strip().upper()[:2])


# per-țară: lungime cod poștal, tabelul stradal + coloane, tabelul de localități (owner cod) dacă e separat,
# lat = coloana de transliterare Latină (BG), fmt = formatul canonic de scriere al codului,
# pc_complete = nomenclatorul de coduri e COMPLET (CZ=RÚIAN, HU/SK=listă poștală oficială) → un cod negăsit e
#   sigur greșit și îl derivăm (lecția WPO #530); BG/PL au coduri INCOMPLETE/zgomotoase (OSM / broom-effect PRG)
#   → un cod bine-format dar necunoscut se PĂSTREAZĂ dacă localitatea e reală (decizia PL FREE-FIRST),
# loc_cnt = coloana de frecvență în tabelul de localități (None = fără; folosită la alegerea codului dominant)
CFG = {
    "CZ": dict(pclen=5, tbl="cz_addresses", city="obec", city_norm="obec_norm", street_norm="ulice_norm", pc="psc",
               fmt=lambda d: d[:3] + " " + d[3:], pc_complete=True, loc_cnt="cnt", part="cast_obce"),
    "PL": dict(pclen=5, tbl="pl_addresses", city="city", city_norm="city_norm", street_norm="street_norm", pc="postcode",
               fmt=lambda d: d[:2] + "-" + d[2:], pc_complete=False, loc_cnt="cnt"),
    "BG": dict(pclen=4, tbl="bg_streets_osm", city="city", city_norm="city_norm", street_norm="street_norm", pc="postcode",
               loc="bg_localities", loc_city="name", loc_norm="name_norm", loc_pc="postcode", lat="name_lat",
               fmt=lambda d: d, pc_complete=False, loc_cnt="cnt"),
    "HU": dict(pclen=4, tbl="hu_streets", city="city", city_norm="city_norm", street_norm="street_norm", pc="postcode",
               loc="hu_localities", loc_city="name", loc_norm="name_norm", loc_pc="postcode",
               fmt=lambda d: d, pc_complete=True, loc_cnt=None),
    "SK": dict(pclen=5, tbl="sk_streets", city="city", city_norm="city_norm", street_norm="street_norm", pc="postcode",
               loc="sk_localities", loc_city="name", loc_norm="name_norm", loc_pc="postcode",
               fmt=lambda d: d[:3] + " " + d[3:], pc_complete=True, loc_cnt=None),
    # MD: GeoNames (localitate+cod «MD-####») + OSM (străzi). Clienții scriu codul în orice format
    # (2071 / MD2071 / MD-2071) — comparațiile sunt pe cifre; scrierea canonică rămâne pe cifre
    # (paritate cu ce curge azi prin Frisbo). Cod DUBLAT des în address1 («... MD-2092») — inofensiv.
    # pc_complete=False: GeoNames MD are DOAR codurile principale (Chișinău = doar MD-2000, fără
    # micro-districtele 2059/2068/2069 — toate REALE) → un cod bine-format necunoscut se PĂSTREAZĂ.
    "MD": dict(pclen=4, tbl="md_streets", city="city", city_norm="city_norm", street_norm="street_norm", pc="postcode",
               loc="md_localities", loc_city="name", loc_norm="name_norm", loc_pc="postcode",
               fmt=lambda d: d, pc_complete=False, loc_cnt=None),
}

# cuvinte tip-arteră intl de scos ca să rămână miezul străzii
_STREETWORDS = re.compile(
    r"\b(ul|ulica|ulice|ulici|str|street|namesti|namestie|nam|trida|tr|utca|ut|krt|korut|bul|bulevard|blvd|"
    r"pl|ploshtad|aleja|al|osiedle|os|ул|улица|бул|"
    r"булевард|площад)\.?\b", re.I)

# ridicare de la OFICIU de curier (BG dominant — Econt/Speedy) → adresa stradală e irelevantă, valid direct.
# Lever-ul #1 BG (memoria intl-address-nomenclators): HERE le-ar respinge (negeocodabile) → CS degeaba.
_OFFICE_RE = re.compile(r"офис|еконт|спиди|автогара|куриер|econt|speedy|do ofis|офиса на", re.I)

# adresă GOALĂ/gunoi: fără nicio literă în a1+a2, sau markerul „nu am" — nimeni nu poate livra → CS
_NO_ADDR = {"няма", "nyama", "nu am", "n a", "na", "nemam"}

# prefixe de localitate (gr./с./кв./obec/miasto/or./mun./com.…), deja prin fold (fără diacritice, lowercase)
_CITY_PREFIX = {"gr", "s", "selo", "grad", "kv", "zh", "jk", "obec", "mesto", "miasto", "wies", "oras", "obl",
                "or", "mun", "municipiul", "com", "comuna", "sat", "satul", "raionul", "raion",
                "гр", "с", "село", "кв", "ж",
                "град", "обл"}
_ROMAN = re.compile(r"^[ivxlcdm]{1,4}$")


def _street_core(a1):
    s = _fold(a1)
    s = _STREETWORDS.sub(" ", s)
    s = re.sub(r"\d.*$", "", s)            # scot numărul casei + tot ce urmează
    return re.sub(r"\s+", " ", s).strip()


# MD: numele RUSEȘTI ale localităților (folded) → forma latină din GeoNames. Rusa e a doua limbă
# a comenzilor duppo.md; numele rusești NU-s transliterări simple (Кишинёв≠«Chișinău» transliterat).
_MD_RU = {
    "кишинев": "chisinau", "кишинэу": "chisinau", "бельцы": "balti", "бэлць": "balti",
    "тирасполь": "tiraspol", "бендеры": "bender", "тигина": "tighina", "комрат": "comrat",
    "кагул": "cahul", "оргеев": "orhei", "сороки": "soroca", "унгены": "ungheni",
    "дурлешты": "durlesti", "ставчены": "stauceni", "яловены": "ialoveni", "хынчешты": "hincesti",
    "страшены": "straseni", "стрэшены": "straseni", "криково": "cricova", "кодру": "codru",
    "дубоссары": "dubasari", "рыбница": "ribnita", "единцы": "edinet", "чадыр лунга": "ceadir lunga",
    "тараклия": "taraclia", "флорешты": "floresti", "дрокия": "drochia", "каушаны": "causeni",
    "кэушень": "causeni", "вулканешты": "vulcanesti", "отачь": "otaci", "атаки": "otaci",
    "резина": "rezina", "глодяны": "glodeni", "ниспорены": "nisporeni", "теленешты": "telenesti",
    "шолданешты": "soldanesti", "бричаны": "briceni", "окница": "ocnita", "сынджера": "singera",
    "криуляны": "criuleni", "анений ной": "anenii noi", "калараш": "calarasi", "леова": "leova",
    "кантемир": "cantemir", "басарабяска": "basarabeasca", "фалешты": "falesti", "чимишлия": "cimislia",
}


def city_candidates(city_raw):
    """Variante plauzibile de localitate, în ordinea încrederii (întâi întregul curățat, apoi n-grame din față)."""
    out, seen = [], set()

    def add(s):
        s = re.sub(r"\s+", " ", s).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    base = _fold(city_raw)
    # split pe segmente (virgula/parantezele erau separatori → deja spații; păstrăm întregul + prima jumătate)
    for seg in (base, _fold((city_raw or "").split(",")[0]), _fold(re.split(r"[(/]", city_raw or "")[0])):
        toks = [t for t in seg.split() if not t.isdigit() and not _ROMAN.match(t)]  # scot "8", "28921", "V"
        while toks and toks[0] in _CITY_PREFIX:
            toks = toks[1:]
        if not toks:
            continue
        add(" ".join(toks))
        for n in range(min(3, len(toks) - 1), 0, -1):   # n-grame din față, de la lung la scurt
            add(" ".join(toks[:n]))
    return out


def supported(country):
    return country_code(country) in CFG


def _owners_of_pc(cur, cfg, pc):
    """[(nume_afisabil, norm, alt_norm|None)] pentru localitățile care dețin codul poștal.
    Normele pentru COMPARAȚIE se calculează cu _fold pe numele afișabil (convențiile *_norm stocate diferă
    între loadere — ex. sk_localities păstrează cratimele: 'kosice-barca'). `alt` = a doua identitate a
    localității: BG = transliterarea latină (name_lat), CZ = partea-comună (cast_obce — 'Třebčín' aparține
    obcei Slatinice; clientul scrie partea, AWB-ul vrea obcea)."""
    lt = cfg.get("loc", cfg["tbl"])
    lc, lp = cfg.get("loc_city", cfg["city"]), cfg.get("loc_pc", cfg["pc"])
    alt = cfg.get("lat") or cfg.get("part")
    cols = lc + (", " + alt if alt else "")
    cur.execute("select distinct %s from %s where regexp_replace(%s,'\\D','','g')=%%s" % (cols, lt, lp), (pc,))
    return [(r[0], _fold(r[0]), _fold(r[1]) if alt and len(r) > 1 and r[1] else None) for r in cur.fetchall()]


def _locality_pcs(cur, cfg, cand):
    """[(nume_afisabil, pc_digits, cnt)] pentru o localitate căutată după norm (și translit dacă există),
    ordonat descrescător după frecvență (cnt agregat per cod)."""
    lt = cfg.get("loc", cfg["tbl"])
    lc, ln, lp = cfg.get("loc_city", cfg["city"]), cfg.get("loc_norm", cfg["city_norm"]), cfg.get("loc_pc", cfg["pc"])
    lat, cnt = cfg.get("lat"), cfg.get("loc_cnt")
    cntcol = "sum(coalesce(%s,1))" % cnt if cnt else "count(*)"
    # match și pe varianta cu cratime→spații (convențiile *_norm diferă între loadere) + pe oficiile
    # poștale NUMEROTATE din GeoNames ('Cahul' → 'Cahul 1'…'Cahul 9')
    where = ("%s=%%s or replace(%s,'-',' ')=%%s or %s ~ ('^' || %%s || ' [0-9]+$')" % (ln, ln, ln)
             + (" or lower(%s)=%%s" % lat if lat else ""))
    args = (cand, cand, cand, cand) if lat else (cand, cand, cand)
    cur.execute("select max(%s), regexp_replace(%s,'\\D','','g') as pcd, %s from %s where (%s) and %s is not null "
                "group by pcd order by 3 desc" % (lc, lp, cntcol, lt, where, lp), args)
    return [(r[0], r[1], int(r[2])) for r in cur.fetchall() if r[1]]


def _pick_pc(locs, client_pc):
    """Alege codul localității: unic → el; ≤5 coduri → cel cu cel mai lung prefix comun cu codul clientului,
    la egalitate cel mai frecvent (câștig STRICT, altfel None). >5 coduri (oraș mare) → None (nu ghicim)."""
    agg = {}
    for disp, pcd, cnt in locs:
        d, c = agg.get(pcd, (disp, 0))
        agg[pcd] = (d, c + cnt)
    if len(agg) == 1:
        pcd = next(iter(agg))
        return agg[pcd][0], pcd
    if len(agg) > 5:
        return None

    def common_prefix(a, b):
        n = 0
        for x, y in zip(a, b):
            if x != y:
                break
            n += 1
        return n

    ranked = sorted(((common_prefix(client_pc or "", pcd), cnt, pcd, disp) for pcd, (disp, cnt) in agg.items()),
                    reverse=True)
    if ranked[0][:2] == ranked[1][:2]:
        return None                       # egalitate perfectă prefix+frecvență → nu ghicim
    return ranked[0][3], ranked[0][2]


def _lev1(a, b):
    """Distanța de editare ≤1 (verificare rapidă, fără matrice)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = j = diff = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1; j += 1
        else:
            diff += 1
            if diff > 1:
                return False
            j += 1
    return True


def _fuzzy_locality(cur, cfg, cand):
    """Typo de localitate ('durlesto'→'durlesti'): ≥6 litere, aceeași primă literă, 1 editare,
    match UNIC în tabelul de localități — altfel None (nu ghicim între mai multe)."""
    lt = cfg.get("loc")
    if not lt or len(cand) < 6:
        return None
    ln = cfg.get("loc_norm", cfg["city_norm"])
    cur.execute("select distinct %s from %s where left(%s,1)=%%s and abs(length(%s)-%%s)<=1"
                % (ln, lt, ln, ln), (cand[0], len(cand)))
    best = None
    for (nm,) in cur.fetchall():
        if nm and nm != cand and _lev1(cand, nm):
            if best is not None:
                return None          # ambiguu → nu ghicesc
            best = nm
    return best


def _street_pcs(cur, cfg, city_norm_val, street):
    cur.execute("select distinct regexp_replace(%s,'\\D','','g') from %s "
                "where (%s=%%s or replace(%s,'-',' ')=%%s) and %s like %%s"
                % (cfg["pc"], cfg["tbl"], cfg["city_norm"], cfg["city_norm"], cfg["street_norm"]),
                (city_norm_val, city_norm_val, "%" + street + "%"))
    return [r[0] for r in cur.fetchall() if r[0]]


def _street_note(cur, cfg, city_norm_val, street, note):
    if street and len(street) >= 3:
        cur.execute("select 1 from %s where (%s=%%s or replace(%s,'-',' ')=%%s) and %s like %%s limit 1"
                    % (cfg["tbl"], cfg["city_norm"], cfg["city_norm"], cfg["street_norm"]),
                    (city_norm_val, city_norm_val, "%" + street + "%"))
        if not cur.fetchone():
            note += " · stradă nepotrivită în nomenclator (verifică)"
    return note


def validate(cur, country, fields, policy=None):
    cc = country_code(country)
    cfg = CFG.get(cc)
    if not cfg:
        return {"status": "needs_geocoder", "address": None, "source": "intl",
                "note": "țară fără nomenclator (%s) → geocoder/HERE" % cc}

    city_raw = fields.get("city") or ""
    pc = _digits(fields.get("zip"))
    street = _street_core(fields.get("address1"))
    prov = fields.get("province") or ""
    a1_raw = fields.get("address1") or ""
    a2_raw = fields.get("address2") or ""
    cands = city_candidates(city_raw)
    if cc == "MD":
        # extinde candidații cu formele latine ale numelor rusești (Кишинев→chisinau)
        for c in list(cands):
            al = _MD_RU.get(c)
            if al and al not in cands:
                cands.append(al)

    # 0) BG: ridicare de la OFICIU de curier → valid direct (paritate producție; HERE le-ar respinge degeaba)
    if cc == "BG" and _OFFICE_RE.search(" ".join([city_raw, a1_raw, a2_raw])):
        return {"status": "valid", "address": None, "source": "intl",
                "note": "ridicare de la oficiu curier (%s) — adresa stradală e irelevantă" % cc}
    # 0b) BG: adresă GOALĂ (nicio literă în a1+a2 sau doar „няма") → CS, nimeni nu poate livra.
    #     DOAR BG — în CZ satele livrează legitim pe număr-de-casă pur (a1='181').
    if cc == "BG":
        fa12 = _fold(a1_raw + " " + a2_raw)
        if not re.search(r"[a-zа-я]", fa12) or (fa12.split() and set(fa12.split()) <= _NO_ADDR):
            return {"status": "cs", "address": None, "source": "intl",
                    "note": "adresă goală ('%s') → contact client (%s)" % (a1_raw[:30], cc)}

    def corrected(city_out, zip_out, note):
        return {"status": "corrected", "source": "intl",
                "address": {"province": prov, "city": city_out, "zip": zip_out, "address1": a1_raw},
                "note": _street_note(cur, cfg, _fold(city_out), street, note)}

    owners = _owners_of_pc(cur, cfg, pc) if len(pc) == cfg["pclen"] else []

    if owners:
        owner_norms = {o[1] for o in owners} | {o[2] for o in owners if o[2]}
        # localitățile PRIMARE distincte (CZ întoarce un rând per parte-comună → Praha×4 e TOT o localitate)
        prim = {}
        for disp, onorm, _alt in owners:
            prim.setdefault(onorm, disp)
        # 1) o variantă a orașului clientului e chiar proprietarul codului → valid, nu schimb nimic;
        #    excepție CZ: match pe PARTEA-comună (cast_obce) → corectez orașul la OBEC (forma livrabilă)
        for cand in cands:
            for disp, onorm, oalt in owners:
                if cand == onorm:
                    return {"status": "valid", "address": None, "source": "intl",
                            "note": _street_note(cur, cfg, onorm, street, "valid (%s)" % cc)}
            for disp, onorm, oalt in owners:
                if oalt and cand == oalt:
                    if cfg.get("part"):
                        return corrected(disp, fields.get("zip"),
                                         "parte-comună '%s' → obec '%s' (%s)" % (city_raw, disp, cc))
                    return {"status": "valid", "address": None, "source": "intl",
                            "note": _street_note(cur, cfg, onorm, street, "valid (%s, translit)" % cc)}
        # 1b) proprietarul e un sub-district al orașului clientului ("Košice" ⊂ "Košice-Barca") → valid
        for cand in cands:
            if len(cand) >= 4 and any(o[1].startswith(cand + " ") for o in owners):
                return {"status": "valid", "address": None, "source": "intl",
                        "note": _street_note(cur, cfg, cand, street, "valid (%s, cod de sub-district)" % cc)}
        # 2) un proprietar apare ca prefix/cuvinte în ce a scris clientul ("sosnowiec milowice" ⊃ "sosnowiec")
        for cand in cands:
            for disp, onorm, olat in owners:
                m = onorm if onorm and (cand.startswith(onorm + " ") or (" " + onorm + " ") in (" " + cand + " ")) \
                    else (olat if olat and cand.startswith(olat + " ") else None)
                if m:
                    return corrected(disp, fields.get("zip"),
                                     "oraș normalizat din cod poștal (%s): '%s'→'%s'" % (cc, city_raw, disp))
        # 3) tiebreaker pe address1 (lecția CZ #530): a1 conține numele proprietarului codului → clientul e ACOLO
        fa1 = " " + _fold(a1_raw) + " "
        for disp, onorm, olat in owners:
            if onorm and len(onorm) >= 4 and (" " + onorm + " ") in fa1:
                return corrected(disp, fields.get("zip"),
                                 "oraș corectat din address1+cod (%s): '%s'→'%s'" % (cc, city_raw, disp))
        # 3b) address1 conține un SAT/localitate REALĂ ale cărei coduri includ ZIP-ul clientului →
        #     adresa e COERENTĂ (câmpul oraș = raionul/orașul-mamă, satul e în a1) → valid as-is.
        #     (ex MD: city='Cahul' zip=3925 a1='Satul Pașcani Raionul Cahul' — 3925 e al Pașcani-ului.)
        fa1_toks = [t for t in _fold(a1_raw).split() if len(t) >= 5]
        for tok in fa1_toks[:8]:
            if any(tok == c for c in cands):
                continue                          # e chiar orașul din câmp, nu un sat distinct
            locs = _locality_pcs(cur, cfg, tok)
            if locs and pc in {p for _, p, _ in locs}:
                return {"status": "valid", "address": None, "source": "intl",
                        "note": "valid (%s): codul %s e al localității '%s' din address1 (câmpul oraș = raion)"
                                % (cc, pc, locs[0][0])}
        # 4) orașul clientului e localitate REALĂ care NU deține codul → păstrez ORAȘUL, corectez CODUL
        #    (lecția RO #559 / CZ city-safe: nu remuta clientul în alt sat pe baza unui ZIP typo).
        #    Localitate reală dar cod nederivabil SIGUR → geocoder direct (nu cad pe redenumirea din 5).
        for cand in cands:
            locs = _locality_pcs(cur, cfg, cand)
            if not locs:
                continue
            disp = locs[0][0]
            if street and len(street) >= 3:
                spcs = sorted(set(_street_pcs(cur, cfg, _fold(disp), street)))
                if len(spcs) == 1 and spcs[0] != pc:
                    return corrected(city_raw, cfg["fmt"](spcs[0]),
                                     "cod poștal corectat din strada localității '%s' (%s): %s→%s"
                                     % (disp, cc, pc, spcs[0]))
            picked = _pick_pc(locs, pc)
            # poarta TYPO: rescriu codul DOAR dacă e aproape-identic (același district poștal,
            # prefix comun ≥ pclen-1: 2231→2230 da; 2071→2000 NU — poate fi gaură de date, nu typo)
            if picked and picked[1] != pc:
                npfx = 0
                for a, b in zip(pc, picked[1]):
                    if a != b:
                        break
                    npfx += 1
                if npfx >= cfg["pclen"] - 1:
                    return corrected(city_raw, cfg["fmt"](picked[1]),
                                     "cod poștal corectat din localitatea reală '%s' (%s): %s→%s"
                                     % (picked[0], cc, pc, picked[1]))
            if not cfg["pc_complete"]:
                # date INCOMPLETE (BG/PL/MD): cod REAL + oraș REAL + mismatch = cel mai probabil gaură
                # de date (Chișinău are 2059/2068/2071… dar GeoNames doar 2000) → PĂSTREZ (FREE-FIRST)
                return {"status": "valid", "address": None, "source": "intl",
                        "note": _street_note(cur, cfg, _fold(disp), street,
                                             "oraș real '%s', cod %s real dar nemapat la el în nomenclatorul %s (incomplet) → păstrat"
                                             % (disp, pc, cc))}
            return {"status": "needs_geocoder", "address": None, "source": "intl",
                    "note": "oraș real '%s' dar codul %s e al altei localități și nu-l pot deriva sigur (%s) → geocoder"
                            % (disp, pc, cc)}
        # 5) cod cu O SINGURĂ localitate primară, iar orașul clientului NU e o localitate reală → corectez din cod
        if len(prim) == 1:
            disp = next(iter(prim.values()))
            return corrected(disp, fields.get("zip"),
                             "oraș corectat din cod poștal (%s): '%s'→'%s'" % (cc, city_raw, disp))
        # 6) ambiguu: mai multe localități primare și orașul clientului nu seamănă cu niciuna
        if not city_raw.strip():
            return {"status": "cs", "address": None, "source": "intl",
                    "note": "fără oraș, cod %s cu %d localități (%s) → CS" % (pc, len(prim), cc)}
        return {"status": "needs_geocoder", "address": None, "source": "intl",
                "note": "oraș '%s' ≠ cod %s în %s (ambiguu, %d localități) → geocoder" % (city_raw, pc, cc, len(prim))}

    # — cod poștal lipsă / format greșit / inexistent —
    wellformed = len(pc) == cfg["pclen"]      # format OK dar negăsit în nomenclator (owners a fost gol)
    zip_desc = "lipsă" if not pc else "invalid ('%s')" % (fields.get("zip") or "").strip()
    for cand in cands:
        locs = _locality_pcs(cur, cfg, cand)
        if not locs:
            continue
        disp = locs[0][0]
        # cod BINE-FORMAT dar necunoscut nouă + localitate reală: pe date INCOMPLETE (BG/PL) codul clientului
        # se PĂSTREAZĂ (decizia PL FREE-FIRST — clientul își știe codul, nomenclatorul nostru are găuri);
        # pe date COMPLETE (CZ/HU/SK) codul e sigur greșit → îl derivăm (lecția WPO #530: curierul îl respinge).
        if wellformed and not cfg["pc_complete"]:
            return {"status": "valid", "address": None, "source": "intl",
                    "note": _street_note(cur, cfg, _fold(disp), street,
                                         "localitate reală '%s', cod %s necunoscut nomenclatorului %s (incomplet) → păstrat"
                                         % (disp, pc, cc))}
        if street and len(street) >= 3:
            spcs = sorted(set(_street_pcs(cur, cfg, _fold(disp), street)))
            if len(spcs) == 1:
                return corrected(disp, cfg["fmt"](spcs[0]),
                                 "cod poștal %s → derivat din stradă în '%s' (%s)" % (zip_desc, disp, cc))
        picked = _pick_pc(locs, pc)
        if picked:
            return corrected(disp, cfg["fmt"](picked[1]),
                             "cod poștal %s → derivat din localitatea '%s' (%s)" % (zip_desc, picked[0], cc))
        return {"status": "needs_geocoder", "address": None, "source": "intl",
                "note": "cod poștal %s, localitate '%s' cu multe coduri, strada nu discriminează (%s) → geocoder"
                        % (zip_desc, disp, cc)}
    # — orașul din câmp nu s-a găsit: localitatea poate fi în ADDRESS1 ('Durleşti, mun. Chişinău' cu
    #   city='Durleșto') sau orașul e un TYPO la 1 literă ('durlesto'→'durlesti') —
    resolved, via = None, None
    for tok in [t for t in _fold(a1_raw).split() if len(t) >= 5][:8]:
        locs = _locality_pcs(cur, cfg, tok)
        if locs:
            resolved, via = locs, "address1"
            break
    if not resolved:
        for cand in cands:
            fz = _fuzzy_locality(cur, cfg, cand)
            if fz:
                locs = _locality_pcs(cur, cfg, fz)
                if locs:
                    resolved, via = locs, "typo '%s'" % cand
                    break
    if resolved:
        disp = re.sub(r"\s+\d+$", "", resolved[0][0]) or resolved[0][0]   # 'Cahul 1' → 'Cahul'
        if len(pc) == cfg["pclen"] and not cfg["pc_complete"]:
            return corrected(disp, fields.get("zip"),
                             "oraș corectat din %s: '%s'→'%s'; cod %s păstrat (%s, nomenclator incomplet)"
                             % (via, city_raw, disp, pc, cc))
        if street and len(street) >= 3:
            spcs = sorted(set(_street_pcs(cur, cfg, _fold(disp), street)))
            if len(spcs) == 1:
                return corrected(disp, cfg["fmt"](spcs[0]),
                                 "oraș corectat din %s: '%s'→'%s'; cod %s → derivat din stradă (%s)"
                                 % (via, city_raw, disp, zip_desc, cc))
        picked = _pick_pc(resolved, pc)
        if picked:
            return corrected(disp, cfg["fmt"](picked[1]),
                             "oraș corectat din %s: '%s'→'%s'; cod %s → derivat din localitate (%s)"
                             % (via, city_raw, disp, zip_desc, cc))
        return {"status": "needs_geocoder", "address": None, "source": "intl",
                "note": "localitate '%s' (din %s) cu multe coduri, cod %s nederivabil (%s) → geocoder"
                        % (disp, via, zip_desc, cc)}
    if not pc:
        return {"status": "cs", "address": None, "source": "intl",
                "note": "fără cod poștal și localitate negăsită în nomenclator: '%s' (%s)" % (city_raw, cc)}
    return {"status": "needs_geocoder", "address": None, "source": "intl",
            "note": "cod poștal %s inexistent și localitate negăsită: '%s' (%s) → geocoder" % (pc, city_raw, cc)}
