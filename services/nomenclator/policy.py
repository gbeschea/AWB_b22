"""
policy.py — POLITICILE alegibile ale validării de adrese (toggle-urile de business), SEPARATE de regulile de
corectitudine mereu-ON (care rămân cod în address_nomenclator.py). Defaults = SETUP-UL NOSTRU actual.

Per-magazin se pot suprascrie din tabelul `validation_policy` (models.ValidationPolicy); cheile lipsă cad pe
aceste defaults. Vezi memoria `orderhub-address-consolidation` pt catalogul complet (10 politici).
"""
from __future__ import annotations
from typing import Any, Dict

# Defaults = ce facem AZI (confirmat cu owner-ul, 2026-08-15).
POLICY_DEFAULTS: Dict[str, Any] = {
    # 1. Rural fără număr — trimit adrese din sate/localități mici fără nr. casă (curierul livrează pe localitate+ZIP)
    "rural_no_number": True,
    "rural_small_loc_max_zips": 3,          # „localitate mică" = ≤ N zip-uri distincte
    # 2. Respectă ZIP-ul clientului în localități mici/rurale (omonime) — nu-l suprascriu dacă e valid pt localitatea lui
    "respect_customer_zip_rural": True,
    # 3. Completează ZIP lipsă din nomenclator: off | deterministic (unic+paritate) | best_guess
    "derive_zip_from_nomenclator": "deterministic",
    # 4. Completează/validează ZIP de la curier (DPD): off | fallback (nomenclator întâi) | courier_first
    "zip_from_courier": "fallback",
    # 5. HERE geocoder = a doua opinie pt adrese pe care validatorul RO le respinge
    "here_geocoder": True,
    "here_min_score": 0.9,
    # 6. Internațional AS-IS — non-RO nu trece prin validatorul RO
    "intl_as_is": True,
    # 7. Suprascrie oraș/județ din ZIP owner (excepție = politica 2)
    "overwrite_city_county_from_zip": True,
    # 8. Prag auto-corecție → CS: aggressive | balanced | conservative
    "cs_routing": "balanced",
    # 9. Vârsta minimă a comenzii înainte de procesare (minute)
    "min_age_minutes": 5,
    # 10. Guard omonimie + stradă generică: unique_in_county | require_zip
    #     unique_in_county = rezolv o localitate omonimă doar dacă e UNICĂ în județul dat SAU coroborată de ZIP client;
    #     NICIODATĂ pe stradă (strada „Principală" apare în 2.467 localități = zero semnal).
    "homonym_guard": "unique_in_county",
}


def merge_policy(overrides: Dict[str, Any] | None) -> Dict[str, Any]:
    """Politica efectivă = defaults + override-uri per-magazin (cheile lipsă cad pe default)."""
    p = dict(POLICY_DEFAULTS)
    if overrides:
        p.update({k: v for k, v in overrides.items() if v is not None})
    return p


# ——— metadata pt UI (pagina Reguli): tip + etichetă + opțiuni pt fiecare politică ———
POLICY_META = [
    {"key": "rural_no_number", "type": "bool", "label": "Rural fără număr de casă",
     "help": "Expediază adrese din localități mici fără nr. casă — curierul livrează pe localitate+ZIP."},
    {"key": "rural_small_loc_max_zips", "type": "int", "label": "Prag «localitate mică» (nr. ZIP-uri distincte)",
     "help": "O localitate cu cel mult atâtea coduri poștale e considerată mică/rurală."},
    {"key": "respect_customer_zip_rural", "type": "bool", "label": "Respectă ZIP-ul clientului în rural",
     "help": "Nu suprascrie un ZIP valid al clientului în localități mici (protecție la omonime)."},
    {"key": "derive_zip_from_nomenclator", "type": "enum", "options": ["off", "deterministic", "best_guess"],
     "label": "Completează ZIP lipsă din nomenclator",
     "help": "«deterministic» = doar când derivarea e unică (stradă+număr+paritate); fără ghicit."},
    {"key": "zip_from_courier", "type": "enum", "options": ["off", "fallback", "courier_first"],
     "label": "ZIP de la curier (DPD)", "help": "«fallback» = nomenclatorul întâi, curierul doar dacă nu iese."},
    {"key": "here_geocoder", "type": "bool", "label": "HERE geocoder ca a doua opinie",
     "help": "Adresele respinse de nomenclator merg la HERE înainte de CS."},
    {"key": "here_min_score", "type": "float", "label": "Scor minim HERE",
     "help": "Sub acest scor rezultatul HERE nu e de încredere → CS."},
    {"key": "intl_as_is", "type": "bool", "label": "Internațional pe nomenclatoare proprii",
     "help": "Non-RO trece prin nomenclatoarele CZ/PL/BG/HU/SK, nu prin validatorul RO."},
    {"key": "overwrite_city_county_from_zip", "type": "bool", "label": "Suprascrie oraș/județ din ZIP",
     "help": "Când ZIP-ul e autoritativ, orașul/județul se corectează după el (excepție: regula rural de mai sus)."},
    {"key": "cs_routing", "type": "enum", "options": ["aggressive", "balanced", "conservative"],
     "label": "Prag auto-corecție → CS", "help": "Cât de repede renunțăm la auto-corecție în favoarea CS."},
    {"key": "min_age_minutes", "type": "int", "label": "Vârsta minimă a comenzii (minute)",
     "help": "Nu procesa comenzi mai noi de atât (clientul încă poate edita)."},
    {"key": "homonym_guard", "type": "enum", "options": ["unique_in_county", "require_zip"],
     "label": "Guard omonimie localități",
     "help": "Localitate cu nume în ≥2 județe: rezolv doar dacă e unică în județ SAU coroborată de ZIP; niciodată pe stradă."},
]

# ——— registrul regulilor de CORECTITUDINE (mereu ON, cod — read-only în UI) ———
# Sursa: address_nomenclator.py (portul A) + intl.py. Vezi memoriile ro-address-data-and-rules-research /
# orderhub-address-consolidation pt istoric și PR-urile de origine.
RULES_REGISTRY = [
    {"id": "strip_leading_locality", "scope": "RO", "name": "Scoate orașul/județul din fața străzii",
     "desc": "«Craiova, str Cantacuzino» → strada reală se potrivește. Cea mai mare recuperare (~1.155/an)."},
    {"id": "expand_city_abbrev", "scope": "RO", "name": "Extinde abrevieri de oraș",
     "desc": "Tg/Tîrgu→Târgu, Sf, Rm, Drobeta, nume maghiare — 60+ abrevieri."},
    {"id": "sector_re", "scope": "RO", "name": "Detectează sectorul București",
     "desc": "«Sect 4», «Sector.3», «Sector 2Bucuresti» (lipit)."},
    {"id": "denoise_city", "scope": "RO", "name": "Curăță câmpul oraș",
     "desc": "«Bacău Bacău»→Bacău, «Orașul/Mun. X»→X, sufix «jud Y», cifre la coadă, stradă lipită în oraș."},
    {"id": "split_city_street", "scope": "RO", "name": "Desparte orașul de stradă",
     "desc": "«București str Măriuca nr4» în câmpul oraș → localitate + stradă."},
    {"id": "street_date_names", "scope": "RO", "name": "Străzi-dată",
     "desc": "«22 Decembrie», «1 Mai» — numărul din nume nu e număr de casă (și PL: «3 Maja»)."},
    {"id": "ranks_strip", "scope": "RO", "name": "Ranguri antroponimice",
     "desc": "Gen/Sgt/Cpt/Prof/Sf scoase la matching — «Sergent C. Popescu» ≡ «Popescu Constantin, sergent»."},
    {"id": "street_type_split", "scope": "RO", "name": "Tip-arteră lipit",
     "desc": "«SosBucuresti» → «Sos București»; scope corect pe majusculă (bug-ul «Strada»→«ada» reparat)."},
    {"id": "landmark_strip", "scope": "RO", "name": "Șterge reperele",
     "desc": "«vis-a-vis de școala nr 5» — cifra reperului nu devine număr de casă."},
    {"id": "road_km", "scope": "RO", "name": "Drumuri DN/DJ/DC + km",
     "desc": "Rutare rurală pe localitate+ZIP pentru adrese pe șosele."},
    {"id": "rural_no_number", "scope": "RO", "name": "Rural fără număr",
     "desc": "Localitate mică (≤3 ZIP-uri) → numărul de casă e opțional; curierul livrează pe localitate."},
    {"id": "small_city_zip", "scope": "RO", "name": "Oraș mic pe ZIP dominant",
     "desc": "Regulile C/D: oraș cu ≤3 ZIP-uri + stradă nematchată → corectat pe zip+localitate."},
    {"id": "word_numbers", "scope": "RO", "name": "Numere scrise în cuvinte",
     "desc": "«Nr șase»→6 (doar cu «nr» explicit); «2bis/27ter» = număr valid."},
    {"id": "hoist_street_over_block", "scope": "RO", "name": "Strada înaintea blocului",
     "desc": "«Bl D sc J, Strada Republicii» → strada trece în față, blocul rămâne metadata."},
    {"id": "nfkc_norm", "scope": "RO", "name": "Unicode exotic normalizat",
     "desc": "Litere matematice/fullwidth (𝚂𝚕𝚊𝚝𝚒𝚗𝚊, Ａｂｒｕｄ) → text normal."},
    {"id": "siruta_check", "scope": "RO", "name": "Coroborare SIRUTA",
     "desc": "Localitatea și ZIP-owner-ul verificate în nomenclatorul SIRUTA."},
    {"id": "zip_parity", "scope": "RO", "name": "Derivare ZIP cu paritate",
     "desc": "București + orașe >50k: codul depinde de stradă + interval + par/impar."},
    {"id": "homonym_guard", "scope": "RO", "name": "Guard omonimie + stradă generică",
     "desc": "«Popești» există în 19 județe; strada «Principală» în 2.467 localități = zero semnal. Fără coroborare → CS."},
    {"id": "fold_translit", "scope": "INTL", "name": "Fold aliniat pe loadere",
     "desc": "ł→l (NFKD nu-l descompune), cratime→spațiu, match hyphen-agnostic — convențiile diferă între tabele."},
    {"id": "city_candidates", "scope": "INTL", "name": "Candidați de localitate",
     "desc": "Strip prefixe (гр./с./кв./obec), tokeni numerici/romani («Praha 8», «Kolín V»), n-grame din față."},
    {"id": "parent_city", "scope": "INTL", "name": "Cod de sub-district",
     "desc": "«Košice» cu codul lui «Košice-Barca» = valid (orașul-părinte e corect)."},
    {"id": "a1_tiebreaker", "scope": "INTL", "name": "Tiebreaker pe address1",
     "desc": "Orașul nu se potrivește codului, dar address1 conține localitatea codului → clientul e acolo (lecția CZ)."},
    {"id": "city_keep", "scope": "INTL", "name": "Orașul real bate ZIP-ul",
     "desc": "Oraș REAL care nu deține codul → corectăm CODUL din localitate, nu remutăm clientul (lecția #559)."},
    {"id": "dominant_pc", "scope": "INTL", "name": "Cod dominant pe localități mici",
     "desc": "≤5 coduri/localitate: alege pe prefix comun cu codul clientului + frecvență; câștig strict, altfel geocoder."},
    {"id": "pc_complete_policy", "scope": "INTL", "name": "Politica per țară la cod necunoscut",
     "desc": "CZ/HU/SK (date complete): cod negăsit → derivat (WPO strict). BG/PL (incomplete): cod bine-format → păstrat."},
    {"id": "bg_translit", "scope": "INTL", "name": "BG chirilic + latin",
     "desc": "Match pe name_norm (chirilic) și name_lat (translit): «Dobrich» → Добрич."},
]
