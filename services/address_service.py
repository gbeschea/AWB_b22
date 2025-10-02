# -*- coding: utf-8 -*-
"""
Address validator — v8.3 (AWB Hub)

Noutăți vs v8.2
- Suport pentru adrese de forma: "Nr. 5 Strada Lalelelor" / "5 Strada Lalelelor" / "5 Lalelelor 10".
  -> Numărul din față este tratat ca număr stradal (nu parte din numele străzii),
     dar denumirea străzii se extrage corect ("Lalelelor").
- Păstrează numerele care fac parte din numele străzii (ex. "Calea 6 Vânători", "Strada 1 Decembrie 1918").
- Restul logicii rămâne ca în v8.2: 2 din 3 (J/L/ZIP), sector București (opțional), sat/comună,
  parser de intervale, aliasuri, fuzzy & TIP-aware pe denumiri, explicații clare când ZIP aparține altei zone.

Integrare:
- Apelul principal rămâne: `validate_address_for_order(db: AsyncSession, order)`.
- `order` trebuie să aibă câmpurile: shipping_province, shipping_city, shipping_zip, shipping_address1, shipping_address2.
- Pune fișierul ca `address_service.py` sau importă funcția din acest modul în runner-ul tău.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple, Optional
from collections import Counter
from difflib import SequenceMatcher

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from schemas import ValidationResult
import models



__VALIDATOR_VERSION__ = "v8.3.1"

# ================== Helpers de normalizare ==================

def strip_diacritics(s: str) -> str:
    if not s: return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return (s.replace("ș","s").replace("ş","s")
             .replace("ț","t").replace("ţ","t")
             .replace("ă","a").replace("â","a").replace("î","i"))

def norm_text(s: Optional[str]) -> str:
    s = strip_diacritics(s or "").lower()
    s = re.sub(r"[',’`\"“”]", " ", s)
    s = re.sub(r"[,.;:()_/\\\-]+", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def same_locality(a: Optional[str], b: Optional[str]) -> bool:
    na, nb = norm_text(a), norm_text(b)
    return bool(na and nb and (na == nb or na in nb or nb in na))

# ================== Alias & expanders ==================

ALIASES = {
    "mendelev": "mendeleev",
    "dr taberei": "drumul taberei",
    "drumultaberei": "drumul taberei",
}

def apply_aliases(s: str) -> str:
    p = norm_text(s)
    for k,v in ALIASES.items():
        if k in p: p = p.replace(k,v)
    return p

# ================== Patterns ==================

LOCKER = re.compile(r"(easybox|locker|sameday|fanbox|collect\s*point|pick[\s\-]*up)", re.I)
HAS_PREFIX_NUM = re.compile(r'(?i)\b(?:nr|no|numar|număr)\.?\s*(\d+[a-zA-Z]?|\d+/\d+)\b')
TRAILING_NUM   = re.compile(r'(?i)(\d+[a-zA-Z]?|\d+/\d+)\s*($|,|\s+bl|bloc|sc|scara|ap|et)')
SECTOR_RE = re.compile(r"\b(?:sector(?:ul)?|sec\.?|sect\.)\s*([1-6])\b", re.I)
SETTLEMENT_ALL_RE = re.compile(r"\b(?:(sat|com(?:\.|una)?)\s+([a-z0-9 \-ăâîșşțţ]+))", re.I)

MONTHS = {"ianuarie","februarie","martie","aprilie","mai","iunie","iulie","august",
          "septembrie","octombrie","noiembrie","decembrie"}

# ================== Număr stradal & denumire stradă ==================

def _strip_leading_number_patterns(s: str) -> str:
    """Scoate din față 'Nr 5 ' / '5 ' când urmează tip de arteră sau un cuvânt,
       pentru a extrage corect denumirea străzii. Detecția numărului real rămâne separată.
    """
    if not s: return s
    # 1) nr./no. 5 + tip arteră
    s = re.sub(r'^\s*(?:nr|no|numar|număr)\.?\s*(\d+[a-z]?|\d+/\d+)\s*[,/ \-]*'
               r'(?=(str(?:\.|ada)?|bd\.?|blvd\.?|bulevardul|calea|drumul?|dr|soseaua|sos\.?|aleea)\b)',
               '', s, flags=re.I)
    # 2) 5 + tip arteră
    s = re.sub(r'^\s*(\d+[a-z]?|\d+/\d+)\s*[,/ \-]*'
               r'(?=(str(?:\.|ada)?|bd\.?|blvd\.?|bulevardul|calea|drumul?|dr|soseaua|sos\.?|aleea)\b)',
               '', s, flags=re.I)
    # 3) opțional: 5 + Nume (fără tip) -> păstrează doar numele (dacă urmează un cuvânt "normal")
    m = re.match(r'^\s*(?:nr|no|numar|număr)?\.?\s*(\d+[a-z]?|\d+/\d+)\s+([A-Za-zĂÂÎȘŞȚŢăâîșşțţ].+)$', s)
    if m:
        nxt = norm_text(m.group(2)).split()[:1]
        if nxt and nxt[0] not in {"bl","bloc","sc","scara","ap","et","etaj","lot"}:
            s = m.group(2)
    return s

def _has_real_house_number(text: str) -> Optional[str]:
    t = text or ""
    t = re.sub(r"([A-Za-zÀ-ÿ])(\d)", r"\1 \2", t)
    m = HAS_PREFIX_NUM.search(t)
    if m: return m.group(1).replace(" ", "")
    m = TRAILING_NUM.search(t)
    if m: return m.group(1).replace(" ", "")
    toks_norm = norm_text(t).split()
    toks_orig = re.split(r"\s+", t.strip())
    for i, tok in enumerate(toks_norm):
        if re.fullmatch(r'\d+[a-z]?|\d+/\d+', tok):
            prev = toks_norm[i-1] if i>0 else ""
            # dacă numărul e imediat după tipul arterei și urmează un cuvânt/lună → parte din numele străzii
            if prev in {"calea","strada","bulevardul","bd","bd.","aleea","soseaua","sos","drum"}:
                nxt_norm = toks_norm[i+1] if i+1 < len(toks_norm) else ""
                if nxt_norm in MONTHS or (nxt_norm and nxt_norm.isalpha()):
                    continue
            return tok.replace(" ", "")
    return None

def _truncate_after_real_number(text: str) -> str:
    """Taie la primul număr care arată ca număr stradal.
       EXCEPȚIE 1: Dacă numărul vine imediat după tipul arterei și urmează un cuvânt/lună,
                   îl considerăm parte din denumirea străzii ("Calea 6 Vânători", "Strada 13 Septembrie").
       EXCEPȚIE 2: Dacă numărul e LA ÎNCEPUT (Nr. 5 / 5) și după el vine tip de arteră sau un cuvânt,
                   îl eliminăm înainte de extracția denumirii străzii (numărul rămâne detectat separat).
    """
    t = text or ""
    t = _strip_leading_number_patterns(t)  # NEW
    t = re.sub(r"([A-Za-zÀ-ÿ])(\d)", r"\1 \2", t)
    m = HAS_PREFIX_NUM.search(t)
    if m: return t[:m.start()].strip()
    m = TRAILING_NUM.search(t)
    if m: return t[:m.start()].strip()
    toks_norm = norm_text(t).split()
    toks_orig = re.split(r"\s+", t.strip())
    for i, tok in enumerate(toks_norm):
        if re.fullmatch(r'\d+[a-z]?|\d+/\d+', tok):
            prev = toks_norm[i-1] if i>0 else ""
            nxt_norm = toks_norm[i+1] if i+1 < len(toks_norm) else ""
            if prev in {"calea","strada","bulevardul","bd","bd.","aleea","soseaua","sos","drum"} and (nxt_norm in MONTHS or (nxt_norm and nxt_norm.isalpha())):
                continue
            return " ".join(toks_orig[:i]).strip()
    return text

def street_core(s: Optional[str]) -> str:
    s = apply_aliases(s or "")
    s = re.sub(r"\b(str\.)\b","strada", s, flags=re.I)
    s = re.sub(r"\b(str)\b","strada", s, flags=re.I)
    s = re.sub(r"\b(bd\.?|blvd\.?)\b","bulevardul", s, flags=re.I)
    s = re.sub(r"\b(sos\.?|soseaua)\b","soseaua", s, flags=re.I)
    s = re.sub(r"\b(alee|aleea)\b","aleea", s, flags=re.I)
    s = re.sub(r"\b(cal\.?)\b","calea", s, flags=re.I)
    s = re.sub(r"\b(drumul?|dr)\b","drum", s, flags=re.I)
    s = norm_text(_truncate_after_real_number(s))
    s = re.sub(r"^\b(strada|bulevardul|calea|aleea|soseaua|sos|drum)\b","", s).strip()
    s = re.sub(r"\b(bloc|bl|scara|sc|ap|ap\.|et|etaj|sector|jud|cartier|lot|sc\.)\b.*","", s).strip()
    return re.sub(r"\s+"," ", s)

def same_street(a: Optional[str], b: Optional[str]) -> bool:
    ca, cb = street_core(a), street_core(b)
    if not ca or not cb: return False
    if ca == cb: return True
    if SequenceMatcher(None, ca, cb).ratio() >= 0.86: return True
    ta, tb = set(ca.split()), set(cb.split())
    if ta and tb:
        inter = len(ta & tb); bigger = max(len(ta),len(tb))
        if bigger and inter / bigger >= 0.75: return True
        if min(len(ta),len(tb))==1 and inter==1: return True
    return False

def detect_tip_from_raw(raw: str) -> Optional[str]:
    t = (raw or "").lower()
    if re.search(r"\bdrumul?\b", t): return "drum"
    if re.search(r"\bstr(?:\.|ada)?\b|\bstrada\b", t): return "strada"
    if re.search(r"\bbd\.?|blvd\.?|bulevardul\b", t): return "bulevardul"
    if re.search(r"\bsoseaua|sos\.?\b", t): return "soseaua"
    if re.search(r"\baleea\b", t): return "aleea"
    if re.search(r"\bcalea\b|\bcal\.?\b", t): return "calea"
    return None

# ================== Interval numere ==================

@dataclass
class NumInterval:
    start: Optional[int]
    end: Optional[int]
    start_suf: Optional[str]
    end_suf: Optional[str]

def parse_house_number(raw: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    if not raw: return None, None
    m = re.search(r'(\d+)([A-Za-z]?)', raw)
    if not m: return None, None
    return int(m.group(1)), (m.group(2).upper() or None)

def parse_numar_spec(spec: Optional[str]) -> Optional[NumInterval]:
    if not isinstance(spec,str) or not spec: return None
    s = spec.strip().lower()
    s = re.sub(r"[^\w\s\-/]+"," ", s)  # păstrează hyphen/slash
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^(nr|no|numar|număr)\.?\s+", "", s)
    m = re.match(r'(\d+)([a-z]?)\s*-\s*t\b', s)
    if m: return NumInterval(int(m.group(1)), None, (m.group(2).upper() or None), None)
    m = re.match(r'(\d+)([a-z]?)\s*-\s*(\d+)([a-z]?)', s)
    if m: return NumInterval(int(m.group(1)), int(m.group(3)),
                             (m.group(2).upper() or None), (m.group(4).upper() or None))
    m = re.fullmatch(r'(\d+)([a-z]?)', s)
    if m: return NumInterval(int(m.group(1)), int(m.group(1)), (m.group(2).upper() or None), (m.group(2).upper() or None))
    return None

def interval_contains(iv: Optional[NumInterval], num: Optional[int], suf: Optional[str]) -> bool:
    if num is None: return False
    if iv is None: return True
    if iv.start is not None and num < iv.start: return False
    if iv.end is not None and num > iv.end: return False
    return True

# ================== DB helpers ==================

try:
    from . import models
except Exception:
    import models  # type: ignore

_MAX_ROWS_PER_LOCALITY = int(os.environ.get("ADDR_VALIDATOR_MAX_ROWS", "25000"))

def _zip_col():
    if hasattr(models.RomaniaAddress, "cod_postal"): return models.RomaniaAddress.cod_postal
    if hasattr(models.RomaniaAddress, "codpostal"):  return models.RomaniaAddress.codpostal
    return None

def _has_norm_cols() -> bool:
    return all(hasattr(models.RomaniaAddress, c) for c in ("judet_norm","localitate_norm","tip_norm","street_full_norm"))

def _street_name_fields(r) -> Tuple[str, str]:
    tip = (getattr(r, "tip_artera", None) or "").strip()
    nume = (getattr(r, "nume_strada", None) or getattr(r, "denumire_artera", None) or "").strip()
    return tip, nume

def _candidate_street_name(r) -> str:
    tip, nume = _street_name_fields(r)
    return " ".join(x for x in [tip, nume] if x).strip()

async def _load_candidates_for_locality(db: AsyncSession, judet_input: str, loc: str):
    if not loc: return []
    jud_norm = norm_text(judet_input); loc_norm = norm_text(loc)

    if _has_norm_cols():
        stmt = (select(models.RomaniaAddress)
                .where(models.RomaniaAddress.judet_norm == jud_norm)
                .where(models.RomaniaAddress.localitate_norm == loc_norm)
                .limit(_MAX_ROWS_PER_LOCALITY))
        rows = (await db.execute(stmt)).scalars().all()
        return rows or []

    def _norm_db_value(col):
        try:
            return func.regexp_replace(func.lower(func.unaccent(col)), r'[^a-z0-9]+', ' ', 'g')
        except Exception:
            return func.regexp_replace(func.lower(col), r'[^a-z0-9]+', ' ', 'g')

    stmt1 = (select(models.RomaniaAddress)
             .where(_norm_db_value(models.RomaniaAddress.judet) == jud_norm)
             .where(_norm_db_value(models.RomaniaAddress.localitate) == loc_norm)
             .limit(_MAX_ROWS_PER_LOCALITY))
    try:
        rows = (await db.execute(stmt1)).scalars().all()
        return rows or []
    except Exception:
        return []


async def _load_by_zip(db: AsyncSession, zip_code: str):
    """Load address rows by ZIP in a DB-agnostic way.
    - Accepts columns stored as TEXT/VARCHAR or as INTEGER.
    - Ignores surrounding spaces and pads with leading zeros to 6 digits.
    """
    col = _zip_col()
    if not col or not zip_code: 
        return []
    z_raw = str(zip_code).strip()
    z6 = re.sub(r'\D', '', z_raw).zfill(6)
    try:
        from sqlalchemy import cast, String, Integer, or_
        stmt = (select(models.RomaniaAddress)
                .where(or_(func.trim(cast(col, String)) == z6,
                           cast(col, Integer) == int(z6)))
                .limit(_MAX_ROWS_PER_LOCALITY))
    except Exception:
        # Fallback to simple trim equality
        stmt = (select(models.RomaniaAddress)
                .where(func.trim(col) == z6)
                .limit(_MAX_ROWS_PER_LOCALITY))
    rows = (await db.execute(stmt)).scalars().all()
    return rows or []

def _zip_owner_stats(rows: Iterable['models.RomaniaAddress']) -> Tuple[Optional[str], Optional[str]]:
    pairs = [ (getattr(r,"judet",""), getattr(r,"localitate","")) for r in rows ]
    cnt = Counter((norm_text(j), norm_text(l)) for j,l in pairs if j and l)
    if not cnt: return None, None
    (jn, ln), _ = cnt.most_common(1)[0]
    for j,l in pairs:
        if norm_text(j)==jn and norm_text(l)==ln:
            return j, l
    return None, None

def _rows_for_street(rows: Iterable['models.RomaniaAddress'], street_raw: Optional[str]) -> List['models.RomaniaAddress']:
    if not street_raw: return []
    tip_pref = detect_tip_from_raw(street_raw or "")
    out = []
    for r in rows:
        tip, nume = _street_name_fields(r)
        full = " ".join(x for x in [tip, nume] if x).strip()
        if not full: continue
        if same_street(street_raw, full):
            if tip_pref and norm_text(tip) != tip_pref:
                continue
            if norm_text(tip) == "intrare" and "intrare" not in norm_text(street_raw or ""):
                continue
            out.append(r)
    return out

def _zip_best_match_detail(zip_rows: List['models.RomaniaAddress'], street_raw: Optional[str], number: Optional[str]) -> Optional[str]:
    rows = _rows_for_street(zip_rows, street_raw) or zip_rows
    if not rows: return None
    num, suf = parse_house_number(number)
    # preferă intervalul cel mai apropiat ca start
    best = None; best_dist = 10**9
    for r in rows:
        iv = parse_numar_spec(getattr(r, "numar", None) or "")
        if interval_contains(iv, num, suf):
            st = _candidate_street_name(r)
            j = getattr(r,"judet",""); l = getattr(r,"localitate","")
            interval = getattr(r,"numar","") or ""
            d = abs((iv.start or 0) - (num or 0)) if iv and num is not None else 10**8
            if d < best_dist:
                best_dist = d; best = (j,l,st,interval)
    if best:
        j,l,st,interval = best
        return f"{j}/{l}, {st} ({interval})"
    # fallback
    r = rows[0]
    j = getattr(r,"judet",""); l = getattr(r,"localitate","")
    st = _candidate_street_name(r); interval = getattr(r,"numar","") or ""
    return f"{j}/{l}, {st} ({interval})" if (j or l or st or interval) else None

def _candidate_zip_from_jl(jl_rows: List['models.RomaniaAddress'], street_raw: Optional[str], number: Optional[str]) -> Optional[str]:
    if not jl_rows: return None
    num, suf = parse_house_number(number)
    sub = _rows_for_street(jl_rows, street_raw) or jl_rows
    best = None; best_dist = 10**9
    for r in sub:
        iv = parse_numar_spec(getattr(r,"numar",None) or "")
        cp = str(getattr(r,"cod_postal", getattr(r,"codpostal","")) or "").strip().zfill(6)
        if re.fullmatch(r"\d{6}", cp) and interval_contains(iv, num, suf):
            d = abs((iv.start or 0) - (num or 0)) if iv and num is not None else 10**8
            if d < best_dist:
                best_dist = d; best = cp
    if best: return best
    zips = []
    for r in sub:
        cp = str(getattr(r,"cod_postal", getattr(r,"codpostal","")) or "").strip().zfill(6)
        if re.fullmatch(r"\d{6}", cp): zips.append(cp)
    if zips: return Counter(zips).most_common(1)[0][0]
    return None

# ================== București & SAT/comună ==================

def detect_easybox(*parts: Optional[str]) -> bool:
    return bool(LOCKER.search(" ".join(p or "" for p in parts)))

def detect_sector(*parts: Optional[str]) -> Optional[str]:
    m = SECTOR_RE.search(" ".join(p or "" for p in parts))
    return m.group(1) if m else None

def detect_settlements(*parts: Optional[str]) -> List[Tuple[str,str]]:
    text = " ".join(p or "" for p in parts)
    out: List[Tuple[str,str]] = []
    for kind,name in SETTLEMENT_ALL_RE.findall(text or ""):
        nm = name.strip(" .,;:/\\-")
        nm = re.split(r"[;,]| sat ", nm, maxsplit=1)[0].strip()
        out.append((kind.lower(), nm))
    return out

def bucharest_fix(judet: Optional[str], city: Optional[str], *addr_parts: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    jud = judet or ""; cty = city or ""; sector = None
    m_city = SECTOR_RE.search(cty); m_jud = SECTOR_RE.search(jud)
    if m_city or m_jud:
        nr = (m_city or m_jud).group(1)
        jud, cty, sector = "Bucuresti", "Bucuresti", nr
    else:
        sec_from_addr = detect_sector(*addr_parts)
        if sec_from_addr:
            jud, cty, sector = "Bucuresti", "Bucuresti", sec_from_addr
    if norm_text(jud) in {"bucuresti", "mun bucuresti", "bucuresti municipiu"}:
        jud, cty = "Bucuresti", "Bucuresti"
    return jud, cty, sector

# ================== Rezultat validare ==================

@dataclass
class ValidationResult:
    is_valid: bool
    score: int
    errors: List[str]
    suggestions: List[str]

def _set_order_fields(order: Any, status: str, score: int, errors: List[str], suggestions: List[str]) -> None:
    setattr(order, "address_status", status)
    setattr(order, "address_score", int(max(0, min(100, score))))
    setattr(order, "address_validation_errors", list(errors or []))
    if hasattr(order, "address_suggestions"):
        suggestions = list(suggestions or []) + [f"validator_version={__VALIDATOR_VERSION__}"]
        setattr(order, "address_suggestions", suggestions)

# ================== Main validate ==================

async def validate_address_for_order(db: AsyncSession, order: Any) -> ValidationResult:
    """
    ZIP-first strict validation:
      - ZIP is canonical (5->6 padding, must exist in DB)
      - House number is mandatory (except easybox); FN/F.N./fara nr => invalid
      - County/City must match ZIP owner pair in DB (suggest correction if mismatch)
      - Numeric street names (ex. "1 Mai", "13 Septembrie", "1 Decembrie 1918") are not treated as house numbers
      - Block metadata (Bl/Sc/Ap/Et) not considered as house number
      - Fuzzy street suggestion within ZIP (non-blocking)
    """
    in_judet_raw = getattr(order, "shipping_province", None) or ""
    in_city_raw  = getattr(order, "shipping_city", None) or ""
    in_zip_raw   = (getattr(order, "shipping_zip", None) or "").strip()
    in_addr1     = getattr(order, "shipping_address1", None) or ""
    in_addr2     = getattr(order, "shipping_address2", None) or ""

    errors: List[str] = []
    suggestions: List[str] = []

    # 0) Easybox shortcut: valid if ZIP exists & formatted
    if detect_easybox(in_addr1, in_addr2):
        z6 = re.sub(r"\D", "", in_zip_raw or "").zfill(6)
        if not re.fullmatch(r"\d{6}", z6):
            msg = "Adresă locker detectată, dar codul poștal lipsește sau are format greșit (6 cifre)."
            _set_order_fields(order, "invalid", 40, [msg], [])
            return ValidationResult(False, 40, [msg], [])
        zip_rows = await _load_by_zip(db, z6)
        if not zip_rows:
            msg = f"Adresă locker detectată, dar codul poștal {z6} nu există în nomenclatorul din baza de date."
            _set_order_fields(order, "invalid", 40, [msg], [])
            return ValidationResult(False, 40, [msg], [])
        _set_order_fields(order, "valid", 100, ["Adresă locker (easybox/pick-up) detectată — valid."], [])
        return ValidationResult(True, 100, ["Adresă locker (easybox/pick-up) detectată — valid."], [])

    # 1) București: normalizează sectorul dacă apare textual
    in_judet, in_city, detected_sector = bucharest_fix(in_judet_raw, in_city_raw, in_addr1, in_addr2)
    if detected_sector and (norm_text(in_judet_raw) != "bucuresti" or norm_text(in_city_raw) != "bucuresti"):
        suggestions.append(f"Recomandat: Județ='București' și Localitate='București' (sector {detected_sector}).")

    # 2) Extrage stradă & număr (strict pe număr)
    street1 = street_core(in_addr1)
    street2 = street_core(in_addr2)
    chosen_street = street1 if len(street1) >= len(street2) else street2

    NO_NUM_RE = re.compile(r"\b(f\.?\s*n\.?|fara\s+nr\.?|fara\s+numar|fără\s+număr)\b", re.I)
    if NO_NUM_RE.search(" ".join([in_addr1, in_addr2])):
        chosen_number = None
    else:
        chosen_number = _has_real_house_number(in_addr1) or _has_real_house_number(in_addr2)

    # 3) ZIP obligatoriu (format + existență în DB)
    z6 = re.sub(r"\D", "", in_zip_raw or "").zfill(6)
    if not re.fullmatch(r"\d{6}", z6):
        errors.append("Cod poștal lipsă sau format greșit (trebuie 6 cifre).")

    zip_rows = await _load_by_zip(db, z6) if not errors else []
    if not errors and not zip_rows:
        errors.append(f"Codul poștal {z6} nu există în nomenclatorul din baza de date.")

    # 4) Numărul este obligatoriu (exceptând easybox)
    if not chosen_number:
        errors.append("Numărul stradal lipsește din adresă (obligatoriu).")

    # 5) Validare J/L vs ZIP (ZIP-first)
    if zip_rows:
        j_owner, l_owner = _zip_owner_stats(zip_rows)
        if j_owner and l_owner:
            if not same_locality(j_owner, in_judet) or not same_locality(l_owner, in_city):
                errors.append("Județ/localitate nu corespund codului poștal.")
                suggestions.append(f"Actualizează la: {l_owner}, {j_owner} (conform ZIP).")

    # 6) Sugestie de stradă (fuzzy în perimetrul ZIP) — nu blochează validarea
    if zip_rows and chosen_street:
        if not _rows_for_street(zip_rows, chosen_street):
            suggestions.append("Verifică denumirea străzii (nu găsesc potrivire pentru localitatea din ZIP).")

    # Emitere rezultat
    if errors:
        _set_order_fields(order, "invalid", 40, errors, suggestions)
        return ValidationResult(False, 40, errors, suggestions)

    _set_order_fields(order, "valid", 100, [], suggestions)
    return ValidationResult(True, 100, [], suggestions)
async def validate_unvalidated_orders(
    db: AsyncSession,
    days: Optional[int] = None,
    store_ids: Optional[List[int]] = None,
):
    """
    Selectează toate comenzile cu address_status='nevalidat' și rulează validatorul,
    opțional filtrate după un interval de zile și după magazin(e).
    """
    try:
        stmt = select(models.Order).where(models.Order.address_status == 'nevalidat')

        if days:
            from datetime import datetime, timezone, timedelta
            start_date = datetime.now(timezone.utc) - timedelta(days=days)
            stmt = stmt.where(models.Order.created_at >= start_date)

        if store_ids:
            stmt = stmt.where(models.Order.store_id.in_(store_ids))

        result = await db.execute(stmt)
        orders_to_validate = result.scalars().all()
        if not orders_to_validate:
            return

        for order in orders_to_validate:
            await validate_address_for_order(db, order)

        await db.commit()
    except Exception as e:
        await db.rollback()
        print(f"Eroare la validarea în masă a adreselor nevalidate: {e}")