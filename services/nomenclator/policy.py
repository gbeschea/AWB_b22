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
