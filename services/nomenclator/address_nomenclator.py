# -*- coding: utf-8 -*-
"""address_nomenclator.py — validator + auto-corector RO pe nomenclator (portat sync din AWB Hub v8.3.1).

Sursa nomenclatorului: metrics.public.romania_addresses (judet/localitate/tip_artera/nume_strada/numar/
cod_postal/sector + judet_norm/localitate_norm). Funcțiile pure (parsing stradă/număr, București, fuzzy)
sunt copiate 1:1 din services/address_service.py (AWB Hub, github gbeschea/AWB_b22). Query-urile DB rescrise
sync (psycopg2), interogând tabelul din metrics — NU async SQLAlchemy.

API: `validate_and_correct(cur, province, city, zip, address1, address2) -> dict`
  {status: valid|corrected|needs_geocoder|cs, address: {province,city,zip,address1}, source, note}
  - status='corrected' → `address` are câmpurile corectate (de scris în comandă); 'valid' → e bună ca-atare;
    'needs_geocoder' → nomenclatorul n-o rezolvă (candidat pt HERE); 'cs' → fără număr / la CS.
Pipeline: număr obligatoriu → ZIP→stradă (fwd, 84% unic) → invers localitate+stradă→ZIP (ZIP gunoi) → rural-valid.
"""
import address_rules as _AR
import re, unicodedata
from collections import Counter
from difflib import SequenceMatcher

# marker de arteră în a1 → e STRADĂ (nu nume de sat) → nu rural-iza pe token bare (logica prefixului Bd/Str)
ART_MARK_RE = re.compile(r"(?i)\b(str|strada|bd|b-dul|bdul|bulevardul|blvd|calea|cal|aleea|soseaua|sos|splaiul|intrarea|drumul|piata|prelungirea|fundatura)\b")

# ===== normalizare (copiat) =====
def strip_diacritics(s):
    if not s: return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return (s.replace("ș","s").replace("ş","s").replace("ț","t").replace("ţ","t")
             .replace("ă","a").replace("â","a").replace("î","i"))
def norm_text(s):
    # NFKC: pliază literele Unicode „fancy" (matematice/monospace '𝚂𝚕𝚊𝚝𝚒𝚗𝚊'→'Slatina', fullwidth 'Ａｂｒｕｄ'→'Abrud',
    # ligaturi) la ASCII — altfel `[^a-z0-9]` le ștergea la gunoi → localitate/stradă „negăsită".
    s = unicodedata.normalize("NFKC", s or "")
    s = strip_diacritics(s).lower()
    s = re.sub(r"[',’`\"“”]", " ", s)
    s = re.sub(r"[,.;:()_/\\\-]+", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()
def same_locality(a, b):
    na, nb = norm_text(a), norm_text(b)
    return bool(na and nb and (na == nb or na in nb or nb in na))

# cele 41 județe (formă normalizată) — pt strip „județ lipit la coada orașului" ('Abrud Alba'→'Abrud')
COUNTIES = {"alba","arad","arges","bacau","bihor","bistrita nasaud","botosani","braila","brasov","buzau","calarasi",
            "caras severin","cluj","constanta","covasna","dambovita","dolj","galati","giurgiu","gorj","harghita",
            "hunedoara","ialomita","iasi","ilfov","maramures","mehedinti","mures","neamt","olt","prahova","salaj",
            "satu mare","sibiu","suceava","teleorman","timis","tulcea","valcea","vaslui","vrancea","bucuresti"}

def _lev1(a, b):
    """True dacă distanța de editare între a și b e ≤1 (1 substituție/inserție/ștergere). Rapid, fără matrice."""
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if a == b:
        return True
    i = 0
    while i < min(la, lb) and a[i] == b[i]:
        i += 1
    if la == lb:
        return a[i + 1:] == b[i + 1:]          # substituție
    if la < lb:
        return a[i:] == b[i + 1:]              # b are un caracter în plus
    return a[i + 1:] == b[i:]                  # a are un caracter în plus

def _foldloc(s):
    """Normalizare ortografică RO pt matching LOCALITATE (owner: 'clientul scapă un i / ie↔e'): ie→e + colaps
    litere dublate. 'Plăești'≡'Plăieșii', 'Brejoaele'≡'Brezoaele'(+d≤1). Peste norm_text (care face deja â→a/î→i)."""
    t = norm_text(s)
    t = re.sub(r"ie", "e", t)
    t = re.sub(r"(.)\1+", r"\1", t)            # ii→i, ss→s, ee→e...
    return t

def _foldai(s):
    """Pliază â/î/a/i → 'i' (owner: 'Rimnicu Vilcea'≡'Râmnicu Vâlcea'; clientul scrie i unde nomenclatorul are â→a).
    norm_text face deja â→a/î→i INCONSISTENT → aici unific {a,i}→i. Peste _foldloc (ie→e + colaps)."""
    return re.sub(r"[ai]", "i", _foldloc(s))


def _locmatch(cand_norm, cli_norm):
    """Localitate = aceeași, tolerând typo d≤1 SAU variație ortografică RO (ie↔e, i dublat/scăpat, â/î↔i). Ambele
    forme sunt deja norm_text. Folosit DOAR în fuzzy-ul pe JUDEȚ + potrivire UNICĂ → colizii improbabile (garda de
    unicitate prinde eventualele coliziuni din pliajul a/i)."""
    return (_lev1(cand_norm, cli_norm) or _lev1(_foldloc(cand_norm), _foldloc(cli_norm))
            or _lev1(_foldai(cand_norm), _foldai(cli_norm)))

ALIASES = {"mendelev":"mendeleev","dr taberei":"drumul taberei","drumultaberei":"drumul taberei"}
def apply_aliases(s):
    p = norm_text(s)
    for k,v in ALIASES.items():
        if k in p: p = p.replace(k,v)
    return p

def expand_city_abbrev(city):
    """Abrevieri de ORAȘ → nume oficial ('Tg Mureș'→Târgu Mureș, 'Sf Gheorghe'→Sfântu Gheorghe, 'Rm Vâlcea'→Râmnicu
    Vâlcea). ⚠️ CITY_ABBREV (rulebook) NU era aplicat pe câmpul oraș → abrevierile cădeau pe 'localitate negăsită'
    (~949/an). Exact-match pe dict (acoperă cazurile speciale: Drobeta/Miercurea/nume maghiare), apoi PREFIX generic
    de tip-oraș (Tg/Sf/Rm) pt cele neacoperite de dict."""
    if not city:
        return city
    f = norm_text(city)
    if f in _AR.CITY_ABBREV:
        return _AR.CITY_ABBREV[f]
    # prefix generic de tip-oraș. „Tirgu/Tîrgu" (client scrie i unde oficialul are â) → „Târgu" (altfel norm 'tirgu'≠DB 'targu').
    m = re.match(r"(?i)^\s*(tg|t[îi]rgu|sf|rm)\.?\s+(.+)$", city)
    if m:
        g = norm_text(m.group(1))
        full = "Târgu" if g in ("tg", "tirgu") else ("Sfântu" if g == "sf" else "Râmnicu")
        return "%s %s" % (full, m.group(2).strip())
    return city

def denoise_city(city):
    """FALLBACK pt 'localitate negăsită': curăță zgomotul din câmpul ORAȘ ca să iasă localitatea reală.
      - prefix administrativ: 'Orașul/Municipiul/Comuna/Satul/Loc(alitatea)/Mun/Com/Sat X' → X
      - sufix 'jud(ețul) Y' → scos
      - stradă LIPITĂ în oraș ('Neajlov Strada Intrarea…' / 'Bacau Str Ion Luca…') → păstrează doar localitatea din față
      - cifră lipită de literă la început ('2Mai' → '2 Mai', localitate reală)
    Aplicat DOAR când orașul brut nu se găsește (zero regresie pe orașe valide — alea se rezolvă înainte)."""
    c = city or ""
    c = re.sub(r"(?i)^\s*(ora[șs]ul|municipiul|mun|comuna|com|satul|sat|localitatea|loc)\b\.?\s+", "", c)
    c = re.sub(r"(?i)[,\s]*\bjud(?:e[țt]?)?(?:ul)?\b\.?\s*[A-Za-zăâîșțĂÂÎȘȚ\-]+\s*$", "", c)  # „jud/jude/judet/județ(ul) X"
    m = ART_MARK_RE.search(c)
    if m and m.start() > 0:
        c = c[:m.start()]
    c = re.sub(r"(?i)^(\d+)([a-zăâîșț])", r"\1 \2", c)
    c = re.sub(r"[,\s]+\d{3,}\s*$", "", c)   # cifre-gunoi/zip lipite la COADA orașului ('Cluj Napoca 123456' → 'Cluj Napoca')
    c = re.sub(r"(?i)^([a-zăâîșțĂÂÎȘȚ][\w-]+(?:\s+[a-zăâîșțĂÂÎȘȚ][\w-]+)?)\s+\1\b", r"\1", c)  # nume DUBLAT ('Bacău Bacău'→'Bacău', 'Vaslui Vaslui'→'Vaslui')
    return c.strip(" ,.-")

def strip_leading_locality(a1, city, prov):
    """Client a lipit ORAȘUL (uneori + JUDEȚUL) în fața străzii în address1 ('Craiova, str Dr Ioan Cantacuzino',
    'Ploiesti Prahova Bihorului 20', 'Vaslui,strada castanilor') → poluează matching-ul de stradă (52% din bucket-ul
    'stradă gunoi + ZIP valid'). Scoate prefixul REDUNDANT de oraș/județ din FAȚA lui a1 (fire DOAR dacă primele cuvinte
    din a1 sunt exact numele din câmpuri = redundant sigur; păstrează restul = strada reală). Nu atinge a1 dacă n-ar
    rămâne nicio stradă după strip."""
    if not a1:
        return a1
    parts = re.split(r"([\s,]+)", a1.strip())            # [word, sep, word, sep, …]
    widx = [i for i in range(0, len(parts), 2) if i < len(parts) and parts[i]]
    folded = [norm_text(parts[i]) for i in widx]
    cw = norm_text(city).split(); pw = norm_text(prov).split()
    _ADMIN = {"mun", "municipiul", "municipiu", "oras", "orasul", "orasu", "loc", "localitatea", "localitate"}
    _JUD = {"jud", "jud.", "judet", "judetul", "judetului"}
    # ITERATIV: scoate din față oraș / județ / 'jud X' / marker administrativ (Mun/Oraș) — repetat, în ORICE ordine,
    # până dăm de stradă. NU atinge Comuna/Sat (le citește resolver-ul rural din a1 mai jos). Ex: 'Iasi jud Iasi strada
    # Calea Chisinaului' / 'Prahova, Ploiesti, Str X' / 'Mun. Hunedoara,str.BRAZILOR'.
    i = 0; guard = 0
    while i < len(folded) and guard < 12:
        guard += 1
        t = folded[i]
        if t in _JUD and i + 1 < len(folded):             # 'jud X' → scoate marker + numele județului (mereu 1 token)
            i += 2; continue
        if t in _ADMIN:
            i += 1; continue
        if cw and folded[i:i + len(cw)] == cw:            # numele orașului (multi-cuvânt)
            i += len(cw); continue
        if pw and folded[i:i + len(pw)] == pw:            # numele județului
            i += len(pw); continue
        break
    if 0 < i < len(folded):
        rest = "".join(parts[widx[i]:]).strip(" ,.-")
        if rest and re.search(r"[A-Za-zăâîșțĂÂÎȘȚ]", rest):   # rămâne o STRADĂ reală (nu doar un număr) → altfel păstrez a1
            return rest
    return a1

LOCKER = re.compile(r"(easybox|locker|sameday|fanbox|collect\s*point|pick[\s\-]*up)", re.I)
HAS_PREFIX_NUM = re.compile(r'(?i)\b(?:nr|no|numar|număr)\.?\s*(\d+[a-zA-Z]?|\d+/\d+)\b')
TRAILING_NUM   = re.compile(r'(?i)(\d+[a-zA-Z]?|\d+/\d+)\s*($|,|\s+bl|bloc|sc|scara|ap|et)')
SECTOR_RE = re.compile(r"\bsec(?:tor(?:ul)?|t)?\.?\s*([1-6])(?![0-9])", re.I)  # „Sect 4"/„Sector.3"/„Sector 2Bucuresti"(lipit) — nu doar „Sector. N"
MONTHS = {"ianuarie","februarie","martie","aprilie","mai","iunie","iulie","august","septembrie","octombrie","noiembrie","decembrie"}
NO_NUM_RE = re.compile(r"\b(f\.?\s*n\.?|fara\s+nr\.?|fara\s+numar|fără\s+număr)\b", re.I)

# REPERE (landmark) — „vis-a-vis de Kaufland", „lângă școală", „în spatele bisericii" — NU fac parte din adresa
# livrabilă și cel mai periculos: conțin CIFRE (ex „vis-a-vis de scoala nr 5") care ar fi luate greșit drept nr casă.
# Tăiem de la trigger până la coadă, DAR doar dacă rămâne un nume de stradă (altfel landmark-ul e tot ce avem → CS).
LANDMARK_RE = re.compile(
    r"(?i)[,;]?\s*\b(vis[\s\-]?a[\s\-]?vis|viza?vi|l[aâ]ng[aă]|langa|"
    r"[iî]n\s+spatele|[iî]n\s+fa[țt]a|[iî]n\s+incinta|[iî]n\s+curtea|"
    r"peste\s+drum(?:\s+de)?|fost(?:ul|a|ei)|aproape\s+de|deasupra\s+la|colt\s+cu|col[țt]\s+cu)\b.*$")
def strip_landmark(a1):
    if not a1: return a1
    m = LANDMARK_RE.search(a1)
    if not m: return a1
    head = a1[:m.start()].strip(" ,;.-")
    if re.search(r"[A-Za-zĂÂÎȘŞȚŢăâîșşțţ]{3}", head):   # rămâne un nume de stradă → landmark-ul e pur zgomot
        return head
    return a1

# STRADĂ după BLOC/CARTIER — clientul scrie întâi blocul/cartierul, apoi strada reală cu marcaj („Bl D sc J ap 156,
# Strada Republicii nr 8" / „Cartier Valea Rosie, Strada Revolutiei"). Extractorul de stradă ia zona blocului și pică.
# → mutăm partea cu marcaj de stradă în FAȚĂ (blocul rămâne la coadă → block_meta îl găsește tot). ~460 comenzi/an.
_STREET_MARK_RE = re.compile(r"(?i)\b(str(?:\.|ada)?|b[-\.]?dul|bd\.?|blvd\.?|bulevard(?:ul)?|calea|soseaua|sos\.?|"
                             r"aleea|intrarea|intr\.?|drumul|prelungirea|prel\.?|splaiul|spl\.?|fundatura|fdc\.?|piata|p-?ta)\b")
def split_city_street(city, a1):
    """Câmpul ORAȘ conține ȘI strada ('București str Măriuca nr4' / 'Pitești Str Alunului 23') → localitatea =
    partea din FAȚA marcajului de stradă. a1 câștigă DOAR dacă are propriul marcaj de stradă; altfel (a1 = bloc /
    localitate / gunoi) ia strada din câmpul oraș. Un oraș legit N-ARE marcaj de stradă → nu se atinge. → (city, a1)."""
    if not city: return city, a1
    m = _STREET_MARK_RE.search(city)
    if not m or m.start() == 0:
        return city, a1
    loc = city[:m.start()].strip(" ,.-")
    if not re.search(r"[A-Za-zăâîșțĂÂÎȘŞȚŢ]{3}", loc):
        return city, a1
    street = city[m.start():].strip(" ,.-")
    return loc, (a1 if _STREET_MARK_RE.search(a1 or "") else street)

def hoist_street_over_block(a1):
    if not a1: return a1
    m = _STREET_MARK_RE.search(a1)
    if not m or m.start() == 0:
        return a1                                  # strada e deja în față (sau lipsește marcajul)
    head = a1[:m.start()]
    if re.search(r"(?i)\b(bl|bloc|sc|scara|ap|apartament|et|etaj|parter|cartier|cart|zona)\b", head):
        return (a1[m.start():].rstrip(" ,.-") + ", " + head.strip(" ,.-")).strip(" ,.-")
    return a1

# DRUMURI naționale/județene/comunale/europene cu bornă km — „DN 1 km 5", „DJ106 km12+300", „DE 70".
# În aceste adrese punctul livrabil = borna km (nu există număr de casă clasic) → tratează ca RURAL (localitate+zip).
ROAD_KM_RE = re.compile(r"(?i)\b(dn|dj|dc|de|dn\.?)\s*\.?\s*\d+[a-z]?\b.*\bkm\b|\bkm\s*\.?\s*\d")

def _strip_leading_number_patterns(s):
    if not s: return s
    s = re.sub(r'^\s*(?:nr|no|numar|număr)\.?\s*(\d+[a-z]?|\d+/\d+)\s*[,/ \-]*'
               r'(?=(str(?:\.|ada)?|bd\.?|blvd\.?|bulevardul|calea|drumul?|dr|soseaua|sos\.?|aleea)\b)','',s,flags=re.I)
    s = re.sub(r'^\s*(\d+[a-z]?|\d+/\d+)\s*[,/ \-]*'
               r'(?=(str(?:\.|ada)?|bd\.?|blvd\.?|bulevardul|calea|drumul?|dr|soseaua|sos\.?|aleea)\b)','',s,flags=re.I)
    m = re.match(r'^\s*(?:nr|no|numar|număr)?\.?\s*(\d+[a-z]?|\d+/\d+)\s+([A-Za-zĂÂÎȘŞȚŢăâîșşțţ].+)$', s)
    if m:
        nxt = norm_text(m.group(2)).split()[:1]
        # NU tăia numărul din față dacă urmează o LUNĂ → e stradă-DATĂ ('22 Decembrie', '1 Mai', '8 Martie',
        # '1 Decembrie 1918'), nu 'nr + stradă'. Altfel '22 decembrie'→'decembrie' și nu mai matchează DB.
        if nxt and nxt[0] not in {"bl","bloc","sc","scara","ap","et","etaj","lot"} and nxt[0] not in MONTHS: s = m.group(2)
    return s

_NUM_WORD = {"unu":"1","una":"1","doi":"2","doua":"2","trei":"3","patru":"4","cinci":"5","sase":"6","sapte":"7",
             "opt":"8","noua":"9","zece":"10","unsprezece":"11","doisprezece":"12","treisprezece":"13",
             "paisprezece":"14","cincisprezece":"15","saisprezece":"16","saptesprezece":"17","optsprezece":"18",
             "nouasprezece":"19","douazeci":"20"}
_NUM_WORD_RE = re.compile(r"(?:nr|numar|no)\.?\s+(" + "|".join(_NUM_WORD) + r")\b")
def has_real_house_number(text):
    t = text or ""
    t = re.sub(r"([A-Za-zÀ-ÿ])(\d)", r"\1 \2", t)
    # 'bis'/'ter' după număr = număr de casă valid RO ('2bis', '27 bis', '102 ter') — nu 'fără număr'.
    mb = re.search(r'(?i)(?<![\d/])(\d{1,4})\s*(bis|ter)\b', t)
    if mb: return (mb.group(1) + mb.group(2)).lower()
    # număr scris ca CUVÂNT după 'nr' ('Nr sase'→6, 'nr cinci'→5) — nu 'fără număr'. Doar cu 'nr' explicit (neambiguu).
    mw = _NUM_WORD_RE.search(norm_text(t))
    if mw: return _NUM_WORD[mw.group(1)]
    m = HAS_PREFIX_NUM.search(t)
    if m: return m.group(1).replace(" ","")
    m = TRAILING_NUM.search(t)
    if m: return m.group(1).replace(" ","")
    toks = norm_text(t).split()
    for i,tok in enumerate(toks):
        if re.fullmatch(r'\d+[a-z]?|\d+/\d+', tok):
            prev = toks[i-1] if i>0 else ""
            if prev in {"calea","strada","bulevardul","bd","bd.","aleea","soseaua","sos","drum"}:
                nx = toks[i+1] if i+1<len(toks) else ""
                if nx in MONTHS or (nx and nx.isalpha()): continue
            return tok.replace(" ","")
    return None

def _truncate_after_real_number(text):
    t = text or ""
    t = _strip_leading_number_patterns(t)
    t = re.sub(r"([A-Za-zÀ-ÿ])(\d)", r"\1 \2", t)
    m = HAS_PREFIX_NUM.search(t)
    if m: return t[:m.start()].strip()
    m = TRAILING_NUM.search(t)
    if m: return t[:m.start()].strip()
    toks = norm_text(t).split(); orig = re.split(r"\s+", t.strip())
    for i,tok in enumerate(toks):
        if re.fullmatch(r'\d+[a-z]?|\d+/\d+', tok):
            prev = toks[i-1] if i>0 else ""; nx = toks[i+1] if i+1<len(toks) else ""
            if prev in {"calea","strada","bulevardul","bd","bd.","aleea","soseaua","sos","drum"} and (nx in MONTHS or (nx and nx.isalpha())): continue
            return " ".join(orig[:i]).strip()
    return text

_MONTH_ABBR = {"ian": "ianuarie", "feb": "februarie", "febr": "februarie", "mart": "martie", "apr": "aprilie",
               "aug": "august", "sep": "septembrie", "sept": "septembrie", "oct": "octombrie",
               "noi": "noiembrie", "nov": "noiembrie", "dec": "decembrie"}
_MONTH_ABBR_RE = re.compile(r"(?i)(\b\d+)\s+(ian|febr?|mart|apr|aug|sept?|oct|noi|nov|dec)\.?(?=\s|$)")
def _expand_month_abbr(s):
    """Străzi-DATĂ cu lună ABREVIATĂ: '1 dec 1918'→'1 decembrie 1918', '22 dec'→'22 decembrie'. DOAR când e
    precedată de o cifră (context dată) → fără fals-pozitive. Abrevieri neambigue (fără mar/mai/iun/iul, prea scurte)."""
    return _MONTH_ABBR_RE.sub(lambda m: "%s %s" % (m.group(1), _MONTH_ABBR[m.group(2).lower()]), s or "")

# RANGURI/TITLURI antroponimice — nomenclatorul le OMITE des sau le pune la coadă cu numele INVERSAT
# ('Str. Gen. Eremia Grigorescu' ↔ DB 'Eremia Grigorescu'; 'Sergent Constantin Popescu' ↔ 'Popescu Constantin, sergent').
# Le scoatem din AMBELE părți (aplicat pe customer ȘI pe rândul DB în same_street) → matching pe NUME. Fără 'dr'
# (=Drumul, deja mapat), fără tokeni prea scurți ambigui.
_RANK_RE = re.compile(r"(?i)\b(gen|general|generalul|gral|cpt|cap|capitan|capitanul|locotenent|col|colonel|colonelul|"
                      r"maior|plt|plutonier|sgt|serg|sergent|sergentul|cdor|comandor|amiral|mares|maresal|"
                      r"sf|sfant|sfantu|sfantul|sfanta|prof|profesor|profesorul|inv|invatator|acad|academician|"
                      r"ing|inginer|mitropolit|mitropolitul|patriarh|patriarhul|episcop|preot)\b")
def street_core(s):
    # split tip-arteră LIPIT de nume DOAR când numele începe cu MAJUSCULĂ ('SosBucuresti'→'Sos Bucuresti',
    # 'StrEroilor'→'Str Eroilor'). BUG istoric: `(?i)` făcea `[A-Z]` să prindă și litere mici → 'Strada'→'Str ada',
    # 'Calea'→'Cal ea', 'Aleea'→'Ale ea' → cust corupt → no-zip nu deriva. Scope (?i) DOAR pe tip; litera 2 case-sensitive.
    s = re.sub(r"^((?i:sos|str|bd|bdul|blvd|calea|cal|aleea|ale|intr|prel|spl|drumul|drum))([A-ZĂÂÎȘȚ])", r"\1 \2", s or "")
    s = _expand_month_abbr(s)
    s = apply_aliases(s)
    # abrevieri tip-arteră EXTINSE → formă completă (Prel/Intr/Fdc/Spl/P-ța) — apoi se scot ca tip la strip-ul de jos
    s = re.sub(r"(?i)\b(prel|prelung)\.?\b", "prelungirea", s); s = re.sub(r"(?i)\bintr\.?\b", "intrarea", s)
    s = re.sub(r"(?i)\b(fdc|fdt|fnd)\.?\b", "fundatura", s); s = re.sub(r"(?i)\bspl\.?\b", "splaiul", s)
    s = re.sub(r"(?i)\bp[-\s]?[tț]a\b", "piata", s)
    s = re.sub(r"\b(str\.)\b","strada",s,flags=re.I); s = re.sub(r"\b(str)\b","strada",s,flags=re.I)
    s = re.sub(r"\b(bd\.?|blvd\.?)\b","bulevardul",s,flags=re.I); s = re.sub(r"\b(sos\.?|soseaua)\b","soseaua",s,flags=re.I)
    s = re.sub(r"\b(alee|aleea)\b","aleea",s,flags=re.I); s = re.sub(r"\b(cal\.?)\b","calea",s,flags=re.I)
    s = re.sub(r"\b(drumul?|dr)\b","drum",s,flags=re.I)
    s = norm_text(_truncate_after_real_number(s))
    s = re.sub(r"^\b(strada|bulevardul|calea|aleea|soseaua|sos|drum|prelungirea|intrarea|fundatura|splaiul|piata)\b","",s).strip()
    s = re.sub(r"\b(bloc|bl|scara|sc|ap|ap\.|et|etaj|sector|jud|cartier|lot|sc\.)\b.*","",s).strip()
    _r = _RANK_RE.sub(" ", s).strip()          # scoate ranguri/titluri (dacă rămâne un nume real)
    if _r:
        s = _r
    return re.sub(r"\s+"," ",s)

_DECL_SUF = _AR.DECL_SUF   # → address_rules.py (RULEBOOK)
def _destem(w):
    """Taie terminația de declinare/articol RO (cea mai lungă) dacă rămâne tulpină ≥3 litere. Owner: declinarea
    poate fi -ului/-ul/-ei/-ii/-lor + i-spurios: 'Sebesi'≡'Sebeșului', 'Gara'≡'Gării', 'Viteazu'≡'Viteazul'."""
    for suf in _DECL_SUF:
        if len(w) - len(suf) >= 3 and w.endswith(suf):
            return w[:-len(suf)]
    return w

def same_street(a, b):
    ca, cb = street_core(a), street_core(b)
    if not ca or not cb: return False
    if ca == cb: return True
    if SequenceMatcher(None, ca, cb).ratio() >= 0.86: return True
    ta, tb = set(ca.split()), set(cb.split())
    if ta and tb:
        inter = len(ta & tb); bigger = max(len(ta),len(tb))
        if bigger and inter/bigger >= 0.75: return True
        if min(len(ta),len(tb))==1 and inter==1: return True
    # DECLINARE RO pe TULPINĂ: toți tokenii distinctivi (≥4 litere) ai unuia au corespondent pe tulpină în celălalt
    # ('Sebesi'≡'Sebeșului', 'Gara'≡'Gării'). Subset în ambele sensuri; tulpini identice, nu doar apropiate.
    da = {_destem(t) for t in ta if len(t) >= 4}
    db = {_destem(t) for t in tb if len(t) >= 4}
    if da and db and (da <= db or db <= da):
        return True
    return False

_TIP_MAP = {"cale":"calea","calea":"calea","cal":"calea",
            "alee":"aleea","aleea":"aleea",
            "bulevard":"bulevardul","bulevardul":"bulevardul","bd":"bulevardul","blvd":"bulevardul",
            "strada":"strada","str":"strada",
            "sosea":"soseaua","soseaua":"soseaua","sos":"soseaua",
            "drum":"drum","drumul":"drum","dr":"drum"}
def _tip_canon(s):
    """Normalizează tipul arterei la o formă canonică comună — nomenclatorul ține forme SCURTE ('Cale','Alee',
    'Bulevard'), iar `detect_tip_from_raw` întoarce forme lungi ('calea','aleea','bulevardul'). Fără asta, filtrul
    de tip din `rows_for_street` respinge greșit toate rândurile Cale/Alee/Bulevard."""
    t = norm_text(s)
    return _TIP_MAP.get(t, t)
def detect_tip_from_raw(raw):
    t = (raw or "").lower()
    if re.search(r"\bdrumul?\b",t): return "drum"
    if re.search(r"\bstr(?:\.|ada)?\b|\bstrada\b",t): return "strada"
    if re.search(r"\bbd\.?|blvd\.?|bulevardul\b",t): return "bulevardul"
    if re.search(r"\bsoseaua|sos\.?\b",t): return "soseaua"
    if re.search(r"\baleea\b",t): return "aleea"
    if re.search(r"\bcalea\b|\bcal\.?\b",t): return "calea"
    return None

def block_meta(a1):
    """coada bl/sc/ap/et din adresa clientului — de PĂSTRAT când rescriu strada (altfel pierd apartamentul)."""
    m = re.search(r'\b(bl|bloc|sc|scara|ap|apartament|et|etaj)\b.*$', a1 or '', re.I)
    return (" " + re.sub(r"\s+"," ", m.group(0).strip())) if m else ""
def street_is_garbage(cust, city):
    """True dacă „strada" clientului e de fapt gunoi (goală sau = numele orașului, ex „cluj").
    DOAR atunci am voie să completez strada din ZIP; o stradă REALĂ care nu se potrivește = conflict (nu o suprascriu)."""
    if not cust: return True
    nc = norm_text(cust); ncity = norm_text(city)
    if not nc: return True
    if nc == ncity: return True                     # strada = EXACT numele orașului ('Constanta'/'Cluj Napoca')
    if nc in set(ncity.split()): return True         # strada = UN cuvânt din numele orașului ('Cluj' în 'Cluj Napoca')
    # NU mai folosesc substring (`nc in ncity`) — clasa greșit str reale prefix/conținând orașul: 'Timiș'⊂'Timișoara',
    # 'Calea București' conține 'bucuresti' = străzi REALE, NU gunoi (mergeau greșit la geocoder în loc de valid).
    return len(nc) <= 2
def detect_easybox(*parts): return bool(LOCKER.search(" ".join(p or "" for p in parts)))
def detect_sector(*parts):
    m = SECTOR_RE.search(" ".join(p or "" for p in parts)); return m.group(1) if m else None
def bucharest_fix(judet, city, *addr):
    jud = judet or ""; cty = city or ""; sector=None
    mc = SECTOR_RE.search(cty); mj = SECTOR_RE.search(jud)
    if mc or mj:
        jud, cty, sector = "Bucuresti","Bucuresti",(mc or mj).group(1)
    else:
        sa = detect_sector(*addr)
        if sa: jud, cty, sector = "Bucuresti","Bucuresti",sa
    if norm_text(jud) in {"bucuresti","mun bucuresti","bucuresti municipiu"}: jud, cty = "Bucuresti","Bucuresti"
    return jud, cty, sector

# ===== DB sync (metrics.public.romania_addresses) =====
_COLS = "judet, localitate, tip_artera, nume_strada, numar, cod_postal, sector"
def _dictify(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
def _siruta_row(judet, denumire, cod_postal):
    """Rând sintetic locality-level din SIRUTA (fără stradă) — ca validatorul să recunoască
    localitatea/ZIP-ul oficial chiar dacă n-avem străzile lui în romania_addresses."""
    return {"judet": judet or "", "localitate": denumire, "tip_artera": None,
            "nume_strada": None, "numar": None, "cod_postal": cod_postal, "sector": None}
def load_by_zip(cur, z6):
    if not z6: return []
    cur.execute(f"SELECT {_COLS} FROM public.romania_addresses WHERE cod_postal=%s LIMIT 25000", (z6,))
    rows = _dictify(cur)
    if rows: return rows
    # FALLBACK SIRUTA: ZIP oficial care aparține unei localități reale (nu supra-respinge)
    cur.execute("SELECT judet_norm, denumire, cod_postal FROM public.romania_siruta WHERE cod_postal=%s AND niv IN (2,3) LIMIT 1", (str(z6).strip(),))
    r = cur.fetchone()
    return [_siruta_row(r[0], r[1], r[2])] if r else []
def load_by_locality(cur, judet, loc):
    ln = norm_text(loc)
    if not ln: return []
    jn = norm_text(judet)
    if jn:
        cur.execute(f"SELECT {_COLS} FROM public.romania_addresses WHERE judet_norm=%s AND localitate_norm=%s LIMIT 25000", (jn, ln))
        rows = _dictify(cur)
        if rows: return rows
        # SIRUTA județ-specific ÎNAINTEA fallback-ului cross-județ: (județ,localitate) validă oficial
        # dar fără străzi la noi → păstrează JUDEȚUL CORECT (nu „corecta" spre alt județ cu același nume).
        cur.execute("SELECT denumire, cod_postal FROM public.romania_siruta WHERE niv IN (2,3) AND judet_norm=%s AND localitate_norm=%s ORDER BY niv DESC LIMIT 1", (jn, ln))
        r = cur.fetchone()
        if r: return [_siruta_row(judet, r[0], r[1])]
        # FUZZY localitate (typo d≤1, UNIC în județ): românii scriu 'i' unde nomenclatorul are 'â/î' și greșesc o literă
        # — 'Pantilimon'→'Pantelimon', 'Gîldău'→'Gâldău', 'Covăsânț'→'Covăsinț'. Județul dezambiguizează; o SINGURĂ
        # potrivire (altfel ambiguu → nu ghicesc). Doar la ≥5 litere (typo-urile scurte sunt prea riscante).
        if len(ln) >= 5:
            cur.execute("SELECT DISTINCT localitate_norm FROM public.romania_addresses WHERE judet_norm=%s", (jn,))
            near = {x[0] for x in cur.fetchall() if x[0] and _locmatch(x[0], ln)}
            if len(near) == 1:
                cur.execute(f"SELECT {_COLS} FROM public.romania_addresses WHERE judet_norm=%s AND localitate_norm=%s LIMIT 25000", (jn, next(iter(near))))
                rows = _dictify(cur)
                if rows: return rows
            cur.execute("SELECT DISTINCT localitate_norm, denumire, cod_postal FROM public.romania_siruta WHERE niv IN (2,3) AND judet_norm=%s", (jn,))
            sr = [x for x in cur.fetchall() if x[0] and _locmatch(x[0], ln)]
            if len({x[0] for x in sr}) == 1:
                return [_siruta_row(judet, sr[0][1], sr[0][2])]
            # PREFIX: localitatea din nomenclator are SUFIX (județ/Nou/Vechi) pe care clientul nu-l scrie —
            # 'Daraști' ⊂ 'Dărăști-Ilfov', 'Cluj' ⊂ 'Cluj-Napoca'. Client-name (pliat, ≥4) = PREFIX al numelui din
            # nomenclator, la graniță de cuvânt/'-'. UNIC în județ → rezolv; MULTIPLU ('Târgșoru'→Nou+Vechi) → ambiguu, skip.
            lf = _foldloc(ln)
            if len(lf) >= 4:
                cur.execute("SELECT DISTINCT localitate_norm FROM public.romania_addresses WHERE judet_norm=%s", (jn,))
                pre = {x[0] for x in cur.fetchall()
                       if x[0] and _foldloc(x[0]) != lf and re.match(r"^" + re.escape(lf) + r"([ \-]|$)", _foldloc(x[0]))}
                if len(pre) == 1:
                    cur.execute(f"SELECT {_COLS} FROM public.romania_addresses WHERE judet_norm=%s AND localitate_norm=%s LIMIT 25000", (jn, next(iter(pre))))
                    rows = _dictify(cur)
                    if rows: return rows
            # RUN-TOGETHER: client a LIPIT numele localității ('preotesavalcaudejos'). Găsesc localitatea din județ
            # al cărei nume (fără spații, _foldloc) e SUBSTRING al string-ului clientului: 'valcaudejos' ⊂
            # 'preotesavalcaudejos' → 'Vâlcău de Jos' (deglue prin nomenclator-ca-dicționar). Cel mai LUNG match, unic.
            lnc = re.sub(r"\s", "", _foldloc(ln))
            if len(lnc) >= 8:
                cur.execute("SELECT DISTINCT localitate_norm FROM public.romania_addresses WHERE judet_norm=%s", (jn,))
                hits = [(c, re.sub(r"\s", "", _foldloc(c))) for c in (x[0] for x in cur.fetchall()) if c]
                hits = [(c, f) for c, f in hits if len(f) >= 6 and f in lnc]
                if hits:
                    mx = max(len(f) for _, f in hits)
                    top = {c for c, f in hits if len(f) == mx}
                    if len(top) == 1:
                        cur.execute(f"SELECT {_COLS} FROM public.romania_addresses WHERE judet_norm=%s AND localitate_norm=%s LIMIT 25000", (jn, next(iter(top))))
                        rows = _dictify(cur)
                        if rows: return rows
    # județ greșit/gol (ex. ZIP suspect) → cad pe localitate-only; candidate_zip alege dominant
    cur.execute(f"SELECT {_COLS} FROM public.romania_addresses WHERE localitate_norm=%s LIMIT 25000", (ln,))
    rows = _dictify(cur)
    if rows: return rows
    # FALLBACK SIRUTA: localitatea e reală chiar dacă n-avem străzile ei (recuperează satele mici)
    s = locality_in_siruta(cur, judet, loc)
    return [_siruta_row(judet, s["denumire"], s["cod_postal"])] if s else []
# ===== SIRUTA (registru oficial complet INS — îmbogățire nomenclator; vezi siruta_sync.py) =====
def locality_in_siruta(cur, judet, loc):
    """Localitatea EXISTĂ în registrul oficial SIRUTA? Prinde satele reale care lipsesc din tabelul
    postal parțial (romania_addresses) → validatorul NU mai supra-respinge o adresă doar fiindcă
    n-avem străzile acelui sat. Întoarce {cod_siruta, cod_postal, judet_norm, denumire} sau None."""
    ln = norm_text(loc)
    if not ln: return None
    jn = norm_text(judet)
    if jn:
        cur.execute("SELECT cod_siruta,cod_postal,judet_norm,denumire FROM public.romania_siruta "
                    "WHERE niv IN (2,3) AND judet_norm=%s AND localitate_norm=%s ORDER BY niv DESC LIMIT 1", (jn, ln))
        r = cur.fetchone()
        if r: return {"cod_siruta": r[0], "cod_postal": r[1], "judet_norm": r[2], "denumire": r[3]}
    cur.execute("SELECT cod_siruta,cod_postal,judet_norm,denumire FROM public.romania_siruta "
                "WHERE niv IN (2,3) AND localitate_norm=%s ORDER BY niv DESC LIMIT 1", (ln,))
    r = cur.fetchone()
    return {"cod_siruta": r[0], "cod_postal": r[1], "judet_norm": r[2], "denumire": r[3]} if r else None
def zip_owner_siruta(cur, z):
    """județ+localitate care DEȚINE un cod poștal, din SIRUTA (fallback când romania_addresses n-are ZIP-ul)."""
    if not z: return (None, None)
    cur.execute("SELECT judet_norm,denumire FROM public.romania_siruta WHERE cod_postal=%s AND niv=3 LIMIT 1", (str(z).strip(),))
    r = cur.fetchone()
    return (r[0], r[1]) if r else (None, None)


def _cand_street(r): return " ".join(x for x in [(r.get("tip_artera") or "").strip(), (r.get("nume_strada") or "").strip()] if x).strip()
def zip_owner(rows):
    pairs = [(r.get("judet") or "", r.get("localitate") or "") for r in rows]
    cnt = Counter((norm_text(j),norm_text(l)) for j,l in pairs if j and l)
    if not cnt: return None,None
    (jn,ln),_ = cnt.most_common(1)[0]
    for j,l in pairs:
        if norm_text(j)==jn and norm_text(l)==ln: return j,l
    return None,None
def zip_owner_of(cur, z):
    """judeţ+localitate care DEŢINE ZIP-ul z (din nomenclator) — ca să corectez și jud/loc, nu doar ZIP-ul, la INVERS."""
    rows = load_by_zip(cur, z)
    return zip_owner(rows) if rows else (None, None)
def rows_for_street(rows, street_raw):
    if not street_raw: return []
    tip_pref = detect_tip_from_raw(street_raw); out=[]; out_notip=[]
    for r in rows:
        full = _cand_street(r)
        if full and same_street(street_raw, full):
            out_notip.append(r)
            if tip_pref and _tip_canon(r.get("tip_artera")) != tip_pref: continue
            out.append(r)
    # TIP strict n-a dat nimic dar NUMELE se potrivește sub ALT tip (client a scris „Calea" unde e „Șosea"/„Stradă") →
    # folosește name-only; downstream (candidate_zip) verifică oricum unicitatea ZIP-ului → fără misroute.
    return out if out else out_notip
_INF_NUM = 10**9
def _parse_numar_ranges(numar):
    """Parsează coloana `numar` a nomenclatorului → listă de (lo, hi, paritate) cu paritate ∈ {0 par, 1 impar, None ambele}.
    Format RO: 'nr. 29-37'→(29,37,1) · 'nr. 32-34'→(32,34,0) · 'nr. 155'→(155,155,1) · 'nr. 157-T'→(157,∞,1 impar până la capăt)
    · 'nr. 2-T'→(2,∞,0) · 'nr. 1-T; 2-T'→[(1,∞,1),(2,∞,0)] (toată strada). T = terminus (până la capătul străzii)."""
    out = []
    for part in re.split(r"[;,]", (numar or "").lower()):
        part = part.replace("nr.", " ")
        nums = re.findall(r"\d+", part)
        has_t = bool(re.search(r"(?:^|[\s\-])t\b", part))
        if not nums:
            continue
        lo = int(nums[0])
        hi = int(nums[1]) if len(nums) >= 2 else (_INF_NUM if has_t else lo)
        if hi < lo:
            lo, hi = hi, lo
        par = (lo % 2) if (hi == _INF_NUM or lo % 2 == hi % 2) else None
        out.append((lo, hi, par))
    return out

def _num_in_ranges(n, ranges):
    for lo, hi, par in ranges:
        if lo <= n <= hi and (par is None or n % 2 == par):
            return True
    return False

def candidate_zip_from_locality(rows, street_raw, number=None):
    """Derivă ZIP din localitate+stradă:
      · 1 singur ZIP pe stradă (oraș mic) → îl întorc (neambiguu).
      · MULTE ZIP-uri (oraș mare — „zip-urile sunt inclusiv pe numere", ex. Calea Victoriei = 35 ZIP-uri, câte unul pe
        interval de numere) → dezambiguez pe NUMĂRUL casei (paritate inclusă: par/impar = laturi diferite de stradă).
        Dacă numărul cade fix pe un ZIP → îl întorc; altfel None (→ HERE, NU ghicesc)."""
    sub = rows_for_street(rows, street_raw)
    if not sub:
        return None
    def _z6(r):
        z = str(r.get("cod_postal") or "").strip()
        return z if re.fullmatch(r"\d{6}", z) else None
    zips = {_z6(r) for r in sub if _z6(r)}
    if len(zips) == 1:
        return next(iter(zips))
    if not zips:
        return None
    n = None
    if number is not None:
        m = re.search(r"\d+", str(number))
        if m:
            n = int(m.group(0))
    if n is None:
        return None
    hit = {_z6(r) for r in sub if _z6(r) and _num_in_ranges(n, _parse_numar_ranges(r.get("numar")))}
    return next(iter(hit)) if len(hit) == 1 else None

# ===== validare + corecție =====
def _rural_principala(a1, num):
    """RURAL fără nume de stradă (a1 = doar număr/gunoi) → 'Principală Nr. X' (drumul principal de sat, livrabil).
    Dacă a1 are deja un NUME (sat/stradă) → îl păstrez (ăla e drumul principal denumit)."""
    core = street_core(a1)
    if core and re.search(r"[a-zăâîșț]{3,}", core, re.I):
        return a1
    return ("Principală Nr. %s%s" % (num, block_meta(a1))).strip()


_SMALL_LOC_MAX_ZIPS = 3   # localitate cu ≤3 coduri poștale distincte = sat/oraș mic → nr. casă OPȚIONAL (curierul livrează pe localitate+zip)

def _distinct_zips(rows):
    return {str(r.get("cod_postal")) for r in (rows or []) if re.fullmatch(r"\d{6}", str(r.get("cod_postal") or ""))}

def _rural_no_number(cur, prov, cty, a1, a2, zip_):
    """RURAL / ORAȘ MIC fără număr → livrabil pe localitate+zip. Istoric: 77% livrate. Gate = localitatea are
    ≤3 coduri poștale distincte (sat/comună/oraș mic sub 50k, UN cod pe localitate) — atunci zip-ul e destul de
    specific, nr. casei nu blochează. Orașele mari (multe zip-uri street-level) → None (număr necesar). Păstrează
    strada denumită dacă există (drumul principal are nume). Returnează result-dict sau None."""
    street = street_core(a1) or street_core(a2)
    has_name = bool(street and re.search(r"[a-zăâîșț]{3,}", street, re.I))
    def _ship(jo, lo, z, note):
        return {"status": "corrected",
                "address": {"province": jo, "city": lo, "zip": z, "address1": street if has_name else "Principală"},
                "source": "nomenclator", "note": note}
    # 1) ZIP SCRIS valid → dacă localitatea lui e MICĂ (≤3 zip-uri) SAU sat-pur → livrabil
    z6 = re.sub(r"\D", "", zip_ or "").zfill(6)
    if re.fullmatch(r"\d{6}", z6) and z6 != "000000":
        zr = load_by_zip(cur, z6)
        if zr:
            jo, lo = zip_owner(zr)
            if jo and lo:
                sat_pur = not any(_cand_street(r) for r in zr)
                small = len(_distinct_zips(load_by_locality(cur, jo, lo))) <= _SMALL_LOC_MAX_ZIPS
                if sat_pur or small:
                    return _ship(jo, lo, z6, "rural/oraș mic fără număr (zip scris → localitate, livrabil)")
    # 2) FĂRĂ zip scris: derivă din localitate (city SAU comuna/sat din a1/a2), dacă e localitate MICĂ cu ZIP UNIC
    locs = [cty] if cty else []
    for mk in re.finditer(r"(?i)\b(comuna|com|satul|sat)(?:\.\s*|\s+)([A-Za-zăâîșțĂÂÎȘȚ][\w-]*(?:\s+[A-Za-zăâîșțĂÂÎȘȚ][\w-]*){0,2})",
                          " ".join([a1 or "", a2 or ""])):
        locs.append(mk.group(2))
    for loc in locs:
        cands = load_by_locality(cur, prov, loc)
        if not cands:
            continue
        zips = _distinct_zips(cands)
        # UN singur zip pe localitate → destul de specific chiar dacă are străzi denumite (oraș mic sub-50k)
        if len(zips) == 1:
            z = next(iter(zips)); jo, lo = zip_owner_of(cur, z)
            return _ship(jo or prov, lo or loc, z, "rural/oraș mic fără număr (zip derivat din localitate)")
    return None


def validate_and_correct(cur, province, city, zip_, address1, address2="", loc_hint=""):
    a1 = address1 or ""; a2 = address2 or ""
    prov = province or ""; cty = city or ""
    # 0) easybox
    if detect_easybox(a1, a2):
        z6 = re.sub(r"\D","", zip_ or "").zfill(6)
        if re.fullmatch(r"\d{6}", z6) and load_by_zip(cur, z6):
            return {"status":"valid","address":None,"source":"easybox","note":"locker"}
        return {"status":"cs","address":None,"source":"easybox","note":"locker fără ZIP valid"}
    # 1) București sector
    prov, cty, sector = bucharest_fix(prov, cty, a1, a2)
    # 1a) abrevieri de oraș (Tg/Sf/Rm/Drobeta… — CITY_ABBREV) → nume oficial, ÎNAINTE de orice lookup pe localitate
    cty = expand_city_abbrev(cty)
    # 1a1b) câmpul ORAȘ conține ȘI strada ('București str Măriuca nr4', a1='Bucuresti') → separă localitatea + mută strada în a1 dacă a1 e slab
    cty, a1 = split_city_street(cty, a1)
    # 1a2) oraș/județ lipit în FAȚA străzii în a1 ('Craiova, str X' / 'Ploiesti Prahova Y') → scos (poluează matching-ul de stradă)
    a1 = strip_leading_locality(a1, cty, prov)
    # 1a3) REPER lipit în COADĂ ('...vis-a-vis de Kaufland', '...langa scoala nr 5') → scos ÎNAINTE de detecția
    #      numărului (altfel cifra reperului = nr casă fals). Doar dacă rămâne un nume de stradă.
    a1 = strip_landmark(a1)
    # 1a4) STRADĂ după BLOC/CARTIER ('Bl D sc J ap 156, Strada Republicii' / 'Cartier X, Strada Y') → mut strada în față
    a1 = hoist_street_over_block(a1)
    # 1b) câmpul ORAȘ e de fapt un COD POȘTAL (client a pus zip-ul în oraș: city='305600') → mută-l în ZIP,
    #     localitatea vine din load_by_zip. Doar dacă zip-ul propriu-zis lipsește.
    if not re.sub(r"\D", "", zip_ or "") and re.fullmatch(r"\d{6}", (cty or "").strip()):
        zip_ = cty.strip(); cty = ""
    # 1c) COD POȘTAL pus în ADDRESS1 ('905800, 17' = zip 905800 (Negru Vodă) + nr 17; client a pus zip-ul ca stradă)
    #     → mută-l în ZIP, restul rămâne. Doar dacă zip-ul lipsește și a1 are un 6-cifre standalone (load_by_zip validează).
    if not re.sub(r"\D", "", zip_ or ""):
        mz = re.search(r"(?<!\d)(\d{6})(?!\d)", a1)
        if mz:
            zip_ = mz.group(1); a1 = (a1[:mz.start()] + " " + a1[mz.end():]).strip(" ,.-")
    # 2) număr obligatoriu
    if NO_NUM_RE.search(a1+" "+a2): num = None
    else: num = has_real_house_number(a1) or has_real_house_number(a2)
    if not num and ROAD_KM_RE.search(a1+" "+a2):
        # drum DN/DJ/DC/DE cu bornă km → punctul livrabil e borna, nu un nr clasic → validare rural (localitate+zip)
        rr = _rural_no_number(cur, prov, cty, a1, a2, zip_)
        if rr:
            rr["note"] = "drum+km (livrabil pe localitate)"; return rr
    if not num:
        rr = _rural_no_number(cur, prov, cty, a1, a2, zip_)   # rural sat-pur → livrabil fără număr (zip scris sau derivat din localitate+județ)
        if rr:
            return rr
        return {"status":"cs","address":None,"source":"nomenclator","note":"fără număr de casă"}
    s1, s2 = street_core(a1), street_core(a2)
    cust = s1 if len(s1) >= len(s2) else s2   # ca v8.3.1: iau strada din câmpul mai substanțial (a1 SAU a2)
    z6 = re.sub(r"\D","", zip_ or "").zfill(6)
    zip_rows = load_by_zip(cur, z6) if (re.fullmatch(r"\d{6}", z6) and z6 != "000000") else []

    if zip_rows:
        jo, lo = zip_owner(zip_rows)
        out_prov = jo or prov; out_city = lo or cty
        jl_fixed = bool(jo and lo and (not same_locality(jo,prov) or not same_locality(lo,cty)))
        streets = sorted({_cand_street(r) for r in zip_rows if _cand_street(r)})
        garbage = street_is_garbage(cust, cty) or street_is_garbage(cust, out_city)

        if not garbage:
            # clientul a dat o stradă REALĂ → NU o schimb NICIODATĂ (nici dacă ZIP-ul are mai multe străzi).
            in_zip = bool(rows_for_street(zip_rows, cust)) or any(same_street(cust, st) for st in streets)
            if in_zip or not streets:
                # strada e la acest ZIP (orice tip) SAU ZIP rural fără străzi → PĂSTREZ adresa clientului, fix doar jud/loc
                return {"status":"corrected" if jl_fixed else "valid",
                        "address":{"province":out_prov,"city":out_city,"zip":z6,"address1":a1} if jl_fixed else None,
                        "source":"nomenclator",
                        "note":("stradă păstrată (client)" if streets else "rural") + (" +fix jud/loc" if jl_fixed else "")}
            # stradă reală care NU e la acest ZIP → ZIP suspect. INVERS: păstrez strada, caut ZIP-ul ei din localitate.
            cands = load_by_locality(cur, prov, cty)
            dz = candidate_zip_from_locality(cands, cust, num) if cands else None
            if dz and dz != z6:
                jo2, lo2 = zip_owner_of(cur, dz)   # fix și jud/loc din ZIP-ul derivat (altfel ex. Ilfov+Buc → rămâne UNKNOWN)
                return {"status":"corrected","address":{"province":jo2 or prov,"city":lo2 or cty,"zip":dz,"address1":a1},
                        "source":"nomenclator-invers","note":"stradă reală ≠ ZIP → ZIP+jud/loc corectat din localitate+stradă"}
            # owner (25-iul): STRADA corectează ZIP-ul ÎNTÂI (sus: `candidate_zip_from_locality` derivă ZIP-ul REAL al
            # străzii → „dacă e strada bună, corectezi zip-ul"). DOAR dacă strada nu se poate localiza determinist →
            # ZIP-ul valid rămâne ancoră: iau JUDEȚ+localitate din el (fiabil), PĂSTREZ strada (curierul rutează pe ZIP+loc).
            return {"status":"corrected" if jl_fixed else "valid",
                    "address":{"province":out_prov,"city":out_city,"zip":z6,"address1":a1} if jl_fixed else None,
                    "source":"nomenclator","note":"jud/loc din ZIP valid (stradă nederivabilă), stradă păstrată"}

        # strada clientului e GUNOI (goală / = numele orașului) → o pot completa DOAR determinist:
        if not streets:
            # rural: ZIP fără străzi → valid pe localitate+număr. Dacă a1 n-are nume de stradă (doar număr) → 'Principală'.
            ra1 = _rural_principala(a1, num); changed = jl_fixed or ra1 != a1
            return {"status":"corrected" if changed else "valid",
                    "address":{"province":out_prov,"city":out_city,"zip":z6,"address1":ra1} if changed else None,
                    "source":"nomenclator","note":"rural (ZIP+localitate+număr, fără stradă)" + (" +Principală" if ra1 != a1 else "") + (" +fix jud/loc" if jl_fixed else "")}
        if len(streets) == 1:
            # ZIP are EXACT o stradă → completez fără ambiguitate (84% din ZIP-uri RO)
            new_a1 = f"{streets[0]} Nr. {num}{block_meta(a1)}".strip()
            changed = jl_fixed or (norm_text(new_a1) != norm_text(a1))
            return {"status":"corrected" if changed else "valid",
                    "address":{"province":out_prov,"city":out_city,"zip":z6,"address1":new_a1} if changed else None,
                    "source":"nomenclator","note":"stradă completată din ZIP (unic)" + (" +fix jud/loc" if jl_fixed else "")}
        # ORAȘ MIC (≤3 coduri poștale pe TOATĂ localitatea, ex Făgăraș/Târnăveni/Petrila = 1 zip, deși are zeci de
        # străzi) → zip-ul e destul de specific pt tot orașul; strada negăsibilă NU blochează (curierul rutează pe
        # zip+localitate). Discriminator = nr. ZIP-uri pe localitate, NU nr. străzi pe zip. Orașe mari (multe zip-uri
        # street-level) → cad mai jos la HERE. Rezolvă clasa mare „multi-stradă (N)" pt sub-50k.
        if len(_distinct_zips(load_by_locality(cur, out_prov, out_city))) <= _SMALL_LOC_MAX_ZIPS:
            return {"status":"corrected","address":{"province":out_prov,"city":out_city,"zip":z6,"address1":a1},
                    "source":"nomenclator","note":"oraș mic (≤3 zip/localitate) — livrabil pe zip+localitate, stradă păstrată"}
        # gunoi + ZIP cu MAI MULTE străzi → NU ghicesc care e → HERE (nu pun altă stradă)
        return {"status":"needs_geocoder","address":None,"source":"nomenclator",
                "note":f"stradă gunoi + ZIP multi-stradă ({len(streets)})"}

    # 3) ZIP gunoi/lipsă → INVERS: derivă ZIP din localitate(+stradă)
    def _z6r(r):
        z = str(r.get("cod_postal") or "").strip()
        return z if re.fullmatch(r"\d{6}", z) else None
    # loc_hint = localitatea scrisă ÎNAINTE de stradă ('Ipatele, str principala, Nr 275') scoasă de strip_loc_prefix —
    # câmpul oraș e orașul VECIN. Dacă e sat-pur cu ZIP unic (dezambiguat pe județ) → e localitatea reală.
    if loc_hint:
        hc = load_by_locality(cur, prov, loc_hint)
        if hc and not [r for r in hc if _cand_street(r)]:
            hz = {_z6r(r) for r in hc if _z6r(r) and not _cand_street(r)}
            if len(hz) == 1:
                z = next(iter(hz)); jo2, lo2 = zip_owner_of(cur, z)
                return {"status":"corrected","address":{"province":jo2 or prov,"city":lo2 or loc_hint,"zip":z,"address1":a1},
                        "source":"nomenclator","note":"rural din loc-hint (localitate înainte de stradă, ZIP unic)"}
    cands = load_by_locality(cur, prov, cty) or []
    if not cands:                                  # FALLBACK: denoise oraș (Orașul/Comuna/jud/stradă-lipită/'2Mai'/dublat) — zero regresie (fire doar când brutul nu iese)
        dc = expand_city_abbrev(denoise_city(cty))
        if dc and norm_text(dc) != norm_text(cty):
            alt = load_by_locality(cur, prov, dc)
            if alt:
                cty = dc; cands = alt
    if not cands:                                  # oraș prefixat cu junk scurt+punct ('Rd.Vaslui'→'Vaslui') — testează cu DB
        mp = re.match(r"(?i)^[a-zăâîșț]{2,4}[.\-]\s*([A-Za-zăâîșțĂÂÎȘȚ].+)$", cty or "")
        if mp:
            alt = load_by_locality(cur, prov, mp.group(1))
            if alt: cty = mp.group(1); cands = alt
    if not cands:                                  # JUDEȚ lipit la coada orașului ('Abrud Alba'→'Abrud', 'Sansimion Harghita'→'Sansimion') — testat cu DB
        ctoks = (cty or "").split()
        for k in (2, 1):                           # ultimele 2 apoi 1 cuvinte = județ (Satu Mare/Caraș-Severin = 2 cuvinte)
            if len(ctoks) > k and norm_text(" ".join(ctoks[-k:])) in COUNTIES:
                head = " ".join(ctoks[:-k]).strip(" ,.-")
                alt = load_by_locality(cur, prov, head) if head else None
                if alt: cty = head; cands = alt; break
    if not cands:                                  # localitatea e în address1 ca 'Oraș/Municipiul/Comuna X' iar câmpul oraș e gunoi ('IrasSibiu' + a1 'Oras Sibiu str…')
        ma = re.search(r"(?i)\b(?:ora[șs](?:ul)?|municipiul|mun|comuna|com|satul|sat|loc)\.?\s+([A-Za-zăâîșțĂÂÎȘȚ][\w-]+(?:\s+[A-Za-zăâîșțĂÂÎȘȚ][\w-]+)?)", address1 or "")
        if ma:
            ph = ma.group(1).split()
            for k in (len(ph), 1):                 # 2 cuvinte apoi 1 ('Baia Mare' vs 'Sibiu str' → 'Sibiu')
                alt = load_by_locality(cur, prov, " ".join(ph[:k]))
                if alt: cty = " ".join(ph[:k]); cands = alt; break
    # city GUNOI / județ / placeholder ('Selectează') / număr → NU renunțăm aici: cădem prin rezolvarea
    # Comuna/Sat/bare din ORICE câmp (a1/a2/city/județ, jos). Doar dacă nici acolo nu iese o localitate → final.
    dz = candidate_zip_from_locality(cands, cust, num)
    if dz:
        # completez ZIP-ul + jud/loc din ZIP-ul derivat; PĂSTREZ strada clientului (doar ZIP-ul lipsea)
        jo2, lo2 = zip_owner_of(cur, dz)
        return {"status":"corrected","address":{"province":jo2 or prov,"city":lo2 or cty,"zip":dz,"address1":a1},
                "source":"nomenclator-invers","note":"ZIP+jud/loc completat din localitate+stradă"}
    # RURAL fără ZIP: localitatea NU are străzi denumite (sat) și are UN SINGUR cod poștal pe localitate →
    # valid pe localitate+număr, indiferent ce „stradă" a scris clientul (Principala/numele satului = drum
    # principal de sat, livrabil). Cheia rurală = cod poștal DOAR pe localitate (tip_artera+nume_strada NULL).
    named = [r for r in cands if _cand_street(r)]
    rural_zips = {_z6r(r) for r in cands if _z6r(r) and not _cand_street(r)}
    if not named and len(rural_zips) == 1:
        z = next(iter(rural_zips)); jo2, lo2 = zip_owner_of(cur, z)
        return {"status":"corrected","address":{"province":jo2 or prov,"city":lo2 or cty,"zip":z,"address1":a1},
                "source":"nomenclator","note":"rural (localitate cu ZIP unic pe localitate, fără stradă denumită)"}
    # a1/a2 conține EXPLICIT 'Comuna X'/'Sat Y' iar câmpul oraș e alt oraș (vecin) → încearcă localitatea din a1.
    # DOAR cu marker 'Sat/Comuna' (marker de arteră Bd/Str/Calea în față = STRADĂ, nu se atinge — de aceea NU
    # folosim token liber: 'Magheru'/'Dacia' bare = străzi ce coincid cu sate → misroute). Dezambiguare pe județ.
    # când clientul scrie ȘI comuna ȘI satul ('Comuna Satulung sat Finteușu Mic') → ia SATUL (unitatea de livrare;
    # sate diferite din aceeași comună au coduri poștale DIFERITE). Deci sortez markerii SAT înaintea COMUNEI.
    _allf = " ".join([a1 or "", a2 or "", cty or "", prov or ""])   # Comuna/Sat pot fi în ORICE câmp (client haotic)
    _mk = [(0 if mk.group(1).lower().startswith("sat") else 1, mk.group(2))
           for mk in re.finditer(r"(?i)\b(comuna|com|satul|sat)(?:\.\s*|\s+)([A-Za-zăâîșțĂÂÎȘȚ][\w-]*(?:\s+[A-Za-zăâîșțĂÂÎȘȚ][\w-]*){0,2})", _allf)]
    for _pri, phrase in sorted(_mk, key=lambda x: x[0]):
        words = phrase.split()
        for k in range(len(words), 0, -1):          # 'Sat Finteușu Mic pricipala' → încearcă 3→1 cuvinte
            vloc = " ".join(words[:k])
            vc = load_by_locality(cur, prov, vloc)
            if not vc:
                continue
            if [r for r in vc if _cand_street(r)]:
                break                                # localitatea are străzi denumite → nu e sat-pur → HERE/CS
            vz = {_z6r(r) for r in vc if _z6r(r) and not _cand_street(r)}
            if len(vz) == 1:
                z = next(iter(vz)); jo2, lo2 = zip_owner_of(cur, z)
                return {"status":"corrected","address":{"province":jo2 or prov,"city":lo2 or vloc,"zip":z,"address1":a1},
                        "source":"nomenclator","note":"rural din a1 (Sat/Comuna explicit — SAT prioritar, ZIP unic)"}
            break
    # BARE (fără marker Sat/Comuna): a1 e DOAR numele localității (client scrie satul ca „stradă": 'Arioneștii Noi
    # nr 36'). SIGUR doar dacă: (1) a1 NU are marker de arteră (Bd/Str/Calea = stradă, nu sat — logica prefixului);
    # (2) numele NU e stradă în orașul-câmp (Oradea ARE 'Magheru' → e strada, nu satul → HERE/CS, nu rural);
    # (3) e sat-pur cu ZIP unic, dezambiguat pe județ. Fără (2), 'Magheru/Dacia' (străzi = și sate) ar face misroute.
    if not ART_MARK_RE.search(a1):
        bare = re.split(r"(?i)\b(?:nr\.?|num[ăa]r|no\.?)\b", a1)[0]
        bare = re.sub(r"\d.*$", "", bare).strip(" ,.-")
        # drum GENERIC de sat lipit de numele satului ('Tîmburești principala'→'Tîmburești', 'Principala iacobeni'
        # →'iacobeni') — strip din AMBELE capete DOAR cuvinte-drum generice (NU 'Baia Mare'→'Baia' = alt sat).
        _gen = r"(?:principal[ăa]|drumul|g[ăa]rii|bisericii|satului|izlazului)"
        bare = re.sub(r"(?i)^(?:%s\s+)+" % _gen, "", bare)
        bare = re.sub(r"(?i)(?:\s+%s)+\s*$" % _gen, "", bare).strip(" ,.-")
        if bare and 1 <= len(bare.split()) <= 3:
            city_rows = load_by_locality(cur, prov, cty)
            if not (city_rows and rows_for_street(city_rows, bare)):     # (2) nu e stradă în orașul dat
                vc = load_by_locality(cur, prov, bare)
                if vc and not [r for r in vc if _cand_street(r)]:
                    vz = {_z6r(r) for r in vc if _z6r(r) and not _cand_street(r)}
                    if len(vz) == 1:
                        z = next(iter(vz)); jo2, lo2 = zip_owner_of(cur, z)
                        return {"status":"corrected","address":{"province":jo2 or prov,"city":lo2 or bare,"zip":z,"address1":a1},
                                "source":"nomenclator","note":"rural din a1 (nume localitate bare — nu e stradă în oraș, ZIP unic pe județ)"}
    # RULE D (last-resort): oraș MIC (≤3 coduri poștale/localitate) găsit din câmpul oraș, dar zip invalid/lipsă și
    # strada nederivabilă → livrabil pe zip-ul DOMINANT al localității (curierul rutează pe localitate+zip). Analog
    # regulii C pt calea fără-zip. Firește DUPĂ resolver-ele Sat/Comuna/bare (alea sunt mai precise). Orașe mari (multe
    # zip-uri) → rămân geocoder. Strada clientului păstrată.
    if cands:
        zc = {}
        for r in cands:
            z = str(r.get("cod_postal") or "")
            if re.fullmatch(r"\d{6}", z): zc[z] = zc.get(z, 0) + 1
        if 0 < len(zc) <= _SMALL_LOC_MAX_ZIPS:
            z = max(zc, key=zc.get); jo2, lo2 = zip_owner_of(cur, z)
            return {"status":"corrected","address":{"province":jo2 or prov,"city":lo2 or cty,"zip":z,"address1":a1},
                    "source":"nomenclator","note":"oraș mic (≤3 zip) fără zip valid → zip localității (livrabil)"}
    return {"status":"needs_geocoder","address":None,"source":"nomenclator",
            "note":("localitate OK dar ZIP nederivabil" if cands else "localitate negăsită în nomenclator")}
