"""Sanitizare câmpuri INTL pentru DPD — port din cronul xConnector (`dpd_intl_sanitize`), fără de care
OH NU poate prelua expedierea internațională: adresele sunt VALIDE (nomenclatorul le confirmă), dar DPD
le respinge pe FORMAT. Măsurat în cron: ~85% din eșecurile CZ și ~100% din cele PL erau DOAR format.

Limitele reale ale DPD: `addressLine1/2` max 35, `city` max 35, cod poștal strict pe țară, nume ≥2 cuvinte,
telefon în format internațional, email obligatoriu (dar xConnector nu are câmp email în adresă → se scrie
pe comanda Shopify, de unde xConnector îl citește).

Cei 6 pași (ordinea din cron):
  1. ZIP   — candidați din ce a tastat clientul, confirmați în nomenclatorul național (zero ghicit)
  2. CITY  — numele canonic, scurtat la 35
  3. ADRESĂ— gunoiul de la coadă tăiat, >35 împărțit pe cuvânt în a1/a2 DOAR dacă nu se pierde text
  4. NUME  — sub 2 cuvinte → se DUBLEAZĂ cel existent (nu inventăm un nume străin)
  5. TELEFON — prefix de țară doar dacă e număr național complet (nu inventăm cifre)
  6. EMAIL — real dacă există, altfel placeholder unic pe comandă

⚠️ DIFERENȚĂ FAȚĂ DE CRON (intenționată): cronul acceptă și schimbarea LOCALITĂȚII când nota
nomenclatorului conține „recunoscut" (satul aparține comunei propuse). Nomenclatorul din OH nu emite acel
semnal, așa că aici garda e mai STRICTĂ: codul poștal se scrie doar când localitatea rămâne aceeași
(corecție pură de cod). Când nomenclatorul propune altă localitate, NU ghicim — adresa rămâne cum e și,
dacă DPD o respinge, comanda ajunge la om prin giveup. Vezi TODO: semnal de tip „recunoscut" în intl.py.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DPD_MAX_ADDR = 35
DPD_MAX_CITY = 35

# Cifre exponent/indice → normale: „3734²" e „37342", nu „3734" (tastă cu exponent, frecventă pe layout-urile
# est-europene). Fără asta codul pică validarea de format din start.
_UNI_DIG = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5",
            "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9"}


def ascii_digits(z: Any) -> str:
    out = "".join(_UNI_DIG.get(ch, ch) for ch in str(z or ""))
    return unicodedata.normalize("NFKC", out)


# Separatorul dintre grupe: clientul tastează ce-i vine — spațiu, cratimă, punct sau virgulă
# („98.200Sieradz", „763,361"). Le acceptăm pe toate; forma canonică o impunem noi la scriere.
_ZIP_SEP = r"\s*[-.,]?\s*"
_ZIP_PAT = {
    "CZ": r"(\d{3})" + _ZIP_SEP + r"(\d{2})",
    "SK": r"(\d{3})" + _ZIP_SEP + r"(\d{2})",
    "PL": r"(\d{2})" + _ZIP_SEP + r"(\d{3})",
    "BG": r"(\d{4})",
    "HU": r"(\d{4})",
}


def zip_candidates(cc: str, z: Any) -> List[str]:
    """TOATE codurile plauzibile din ce a tastat clientul („50002 503 11" → ['500 02','503 11']).
    Nu alegem noi care e bun — fiecare se confirmă în nomenclator înainte de a fi scris."""
    pat = _ZIP_PAT.get((cc or "").upper())
    if not pat or not z:
        return []
    out: List[str] = []
    for m in re.finditer(pat, ascii_digits(z)):
        if cc in ("CZ", "SK"):
            c = m.group(1) + " " + m.group(2)     # PSČ canonic DPD = „NNN NN"
        elif cc == "PL":
            c = m.group(1) + "-" + m.group(2)     # „42-700Lubliniec" → „42-700"
        else:
            c = m.group(1)
        if c not in out:
            out.append(c)
    return out


_JUNK_TAIL = re.compile(r"[\s\-_.,;:*/\\|~=+#]{2,}$")
_JUNK_RUN = re.compile(r"[\-_.*~=]{3,}")


def strip_junk(a: Optional[str]) -> Optional[str]:
    """Taie „decorul" de la coada adresei: „Záhumenice 986------------" → „Záhumenice 986". Clienții umplu
    câmpul cu liniuțe (ca să treacă de un `required`), iar DPD respinge apoi pe LUNGIME.
    Prudent: rulaje INTERNE doar de la 3+ caractere („10-12", „1104/16" rămân intacte); dacă după curățare
    n-ar mai rămâne literă/cifră, întorc originalul (nu golesc câmpul)."""
    t = (a or "").strip()
    if not t:
        return a
    out = _JUNK_TAIL.sub("", _JUNK_RUN.sub(" ", t)).strip()
    out = re.sub(r"\s{2,}", " ", out)
    return out if re.search(r"[0-9A-Za-zÀ-ž]", out) else a


def split_addr(a1: Optional[str], a2: Optional[str]) -> Tuple[str, str, bool]:
    """addressLine1 > 35 → restul în addressLine2, tăiat pe graniță de CUVÂNT.
    `lost` = a rămas text pe dinafară (nu încape în 2×35) ⇒ NU rescriem (am pierde numărul casei ⇒ colet
    nelivrabil). Ăla e caz de om, nu de auto-corecție."""
    a1 = (a1 or "").strip()
    a2 = (a2 or "").strip()
    if len(a1) <= DPD_MAX_ADDR:
        return a1, a2[:DPD_MAX_ADDR], len(a2) > DPD_MAX_ADDR
    cut = a1.rfind(" ", 0, DPD_MAX_ADDR + 1)
    if cut <= 0:
        cut = DPD_MAX_ADDR
    head, tail = a1[:cut].strip(), a1[cut:].strip()
    merged = (tail + (" " + a2 if a2 else "")).strip()
    return head, merged[:DPD_MAX_ADDR], len(merged) > DPD_MAX_ADDR


_PHONE_CC = {"CZ": ("+420", 9), "PL": ("+48", 9), "BG": ("+359", 9), "HU": ("+36", 9), "SK": ("+421", 9)}


def phone_norm(cc: str, ph: Optional[str]) -> Optional[str]:
    """Prefix de țară dacă e număr național gol-goluț. NU inventează cifre: dacă nu iese lungimea
    națională → None (lăsăm cum e). Prinde și „420725787905" (prefix fără „+"), care arăta corect dar
    era respins de DPD (`receiver.phone-1.num.match`)."""
    conf = _PHONE_CC.get((cc or "").upper())
    if not conf or not ph:
        return None
    pref, nlen = conf
    raw = str(ph).strip()
    digits = re.sub(r"\D", "", raw)
    bare = pref.lstrip("+")
    if not raw.startswith("+") and digits.startswith(bare) and len(digits) == nlen + len(bare):
        return pref + digits[len(bare):]
    if raw.startswith("+") or digits.startswith(bare):
        return None                                  # are deja prefix corect
    if len(digits) == nlen:
        return pref + digits
    if digits.startswith("0") and len(digits) == nlen + 1:
        return pref + digits[1:]
    return None


def _fold(s: Any) -> str:
    s = unicodedata.normalize("NFD", str(s or "").strip().lower())
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def canonical_zip(cc: str, z: Any) -> Any:
    """CZ/SK: DPD cere PSČ „NNN NN" (cu spațiu). Un „29001" scris peste un „290 01" bun face DPD să
    respingă. Punct UNIC de formatare, la scriere."""
    if (cc or "").upper() in ("CZ", "SK"):
        d5 = re.sub(r"\D", "", str(z or ""))
        if len(d5) == 5:
            return d5[:3] + " " + d5[3:]
    return z


async def build_corrections(cc: str, ad: Dict[str, Any], country_name: str) -> Dict[str, Any]:
    """Calculează corecțiile de FORMAT (pașii 1-5) pentru o adresă xConnector. Nu scrie nimic."""
    corr: Dict[str, Any] = {}
    z0 = (ad.get("zip") or "").strip()
    city = (ad.get("city") or "").strip()

    # 1) ZIP — se scrie DOAR un cod pe care nomenclatorul îl CONFIRMĂ (un zip greșit = colet la altă adresă)
    for cand in zip_candidates(cc, z0):
        try:
            from services.nomenclator import runner as _runner
            res = await _runner.validate_address({
                "country": country_name, "province": ad.get("province") or "", "city": city,
                "zip": cand, "address1": ad.get("address1") or "", "address2": ad.get("address2") or "",
            })
        except Exception:
            continue
        if (res or {}).get("status") not in ("valid", "corrected"):
            continue
        naddr = (res or {}).get("address") or {}
        nz, nc = naddr.get("zip"), naddr.get("city")
        city_changed = bool(nc and _fold(nc) != _fold(city))
        if nz and nz != cand and not city_changed:
            corr["zip"] = nz                      # corecție PURĂ de cod (aceeași localitate) → sigură
        elif nz and nz != cand and city_changed:
            break                                 # propune ALTĂ localitate → NU ghicim orașul clientului
        elif cand != z0:
            corr["zip"] = cand                    # doar curățat de gunoi
        break

    # 2) CITY — peste limita DPD → scurtează
    ccity = corr.get("city", city)
    if len(ccity) > DPD_MAX_CITY:
        corr["city"] = ccity[:DPD_MAX_CITY].strip()

    # 3) ADRESĂ — întâi scot decorul, abia apoi decid dacă mai trebuie împărțită pe 2 linii
    na1, na2, lost = split_addr(strip_junk(ad.get("address1")), strip_junk(ad.get("address2")))
    if not lost:
        if na1 != (ad.get("address1") or "").strip():
            corr["address1"] = na1
        if na2 != (ad.get("address2") or "").strip():
            corr["address2"] = na2

    # 4) NUME — DPD cere ≥2 cuvinte; dacă lipsește unul, DUBLEZ ce există (nu inventez)
    fn, ln = (ad.get("firstName") or "").strip(), (ad.get("lastName") or "").strip()
    if len((fn + " " + ln).split()) < 2:
        if fn and not ln:
            corr["lastName"] = fn
        elif ln and not fn:
            corr["firstName"] = ln

    # 5) TELEFON
    nph = phone_norm(cc, ad.get("phone"))
    if nph:
        corr["phone"] = nph
    return corr
