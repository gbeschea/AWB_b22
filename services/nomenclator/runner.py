"""
runner.py — GLUE async peste validatorul „bogat" (address_nomenclator.py, sync psycopg2). Îl rulează din app-ul
async (FastAPI) prin run_in_executor, pe DB-ul OH, aplicând:
  • politicile alegibile (policy.py) — încă parțial (portul complet al toggle-urilor în validator = pas următor);
  • **guard-ul de omonimie #10** (post-hoc, fără să ating internele validatorului): dacă validatorul REDENUMEȘTE o
    localitate omonimă (nume în ≥2 județe) FĂRĂ coroborare (ZIP client SAU unic-în-județ), NU expediez ghicitul → CS.
    Coroborarea NU se face NICIODATĂ pe stradă („Principală" apare în 2.467 localități = zero semnal).

⚠️ DORMANT: nu e legat încă în address_service.validate_address_for_order — se testează pe box la deploy, apoi se cablează.
Pool mic (1..3 conexiuni) — pe fleet-box partajat, evită pool-storm ([[pm2-env-bleed-shared-box]]).
"""
from __future__ import annotations
import os, re, asyncio, functools, unicodedata, urllib.parse as up
from typing import Any, Dict, Optional

import psycopg2
from psycopg2 import pool as _pgpool

from .policy import merge_policy
from . import address_nomenclator as N

_POOL: Optional[_pgpool.ThreadedConnectionPool] = None


def _pool() -> _pgpool.ThreadedConnectionPool:
    global _POOL
    if _POOL is None:
        raw = re.sub(r"\+\w+", "", os.environ["DATABASE_URL"])  # postgresql+asyncpg -> postgresql (psycopg2)
        p = up.urlparse(raw); q = up.parse_qs(p.query)
        _POOL = _pgpool.ThreadedConnectionPool(
            1, 3,
            host=p.hostname, port=p.port or 5432, user=p.username,
            password=up.unquote(p.password or ""), dbname=p.path.lstrip("/"),
            sslmode=q.get("sslmode", ["disable"])[0],
        )
    return _POOL


def _fold(s: Optional[str]) -> str:
    s = unicodedata.normalize("NFKD", (s or "").strip())
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


# Străzi GENERICE (fără semnal de dezambiguizare) — nu se folosesc NICIODATĂ ca să alegem localitatea.
_GENERIC_STREET = re.compile(
    r"^(str(ada)?\.?\s+)?(principal[aăe]?|sat(ul)?|centr[u]?|comuna|f\.?\s*n\.?|fara\s*numar|"
    r"d[ne]\s*\d+|drum(ul)?\s+(national|european))\b", re.I)


def is_generic_street(a1: Optional[str]) -> bool:
    s = _fold(a1)
    return bool(_GENERIC_STREET.match(s)) or len(s) <= 2


def _name_regex(folded_city: str) -> str:
    # 'popesti' -> prinde 'popesti', 'popesti-leordeni', 'popesti (cocu)' (nume urmat de separator sau final)
    return "^" + re.escape(folded_city) + r"([ (-]|$)"


def _homonym_counties(cur, city: str) -> int:
    cur.execute("select count(distinct judet_norm) from romania_addresses where localitate_norm ~ %s",
                (_name_regex(_fold(city)),))
    return cur.fetchone()[0] or 0


def _matches_in_county(cur, city: str, county: str) -> int:
    cur.execute("select count(distinct localitate_norm) from romania_addresses "
                "where judet_norm = %s and localitate_norm ~ %s",
                (_fold(county), _name_regex(_fold(city))))
    return cur.fetchone()[0] or 0


def _apply_homonym_guard(cur, fields: Dict[str, Any], result: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    """#10: dacă validatorul a redenumit o localitate OMONIMĂ fără coroborare (ZIP client / unic-în-județ), → CS."""
    if policy.get("homonym_guard") not in ("unique_in_county", "require_zip"):
        return result
    if result.get("status") not in ("valid", "corrected"):
        return result  # deja needs_geocoder/cs — nimic de păzit
    in_city = (fields.get("city") or "").strip()
    if not in_city:
        return result
    out = result.get("address") or {}
    out_city = (out.get("city") or in_city).strip()
    has_zip = bool(re.sub(r"\D", "", fields.get("zip") or ""))
    renamed = _fold(out_city) != _fold(in_city)            # incl. expandare 'popesti' -> 'popesti-leordeni'
    derived_zip = (not has_zip) and bool((out.get("zip") or "").strip())  # a inventat un ZIP (client n-avea)
    if not (renamed or derived_zip):
        return result                                       # nici redenumire, nici ZIP derivat → nimic riscant
    if _homonym_counties(cur, in_city) < 2:
        return result                                       # nume unic național → sigur
    county = fields.get("province") or ""
    unique_in_county = bool(county) and _matches_in_county(cur, in_city, county) == 1
    corroborated = has_zip or (policy["homonym_guard"] == "unique_in_county" and unique_in_county)
    if not corroborated:
        return {"status": "cs", "address": None, "source": "homonym-guard",
                "note": "localitate omonimă '%s' fără ZIP client / unic-în-județ → nu ghicesc (redenumire/ZIP derivat pe nume ambiguu)"
                        % in_city}
    return result


def _validate_sync(fields: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    conn = _pool().getconn()
    try:
        cur = conn.cursor()
        r = N.validate_and_correct(
            cur, fields.get("province") or "", fields.get("city") or "", fields.get("zip") or "",
            fields.get("address1") or "", fields.get("address2") or "",
        )
        return _apply_homonym_guard(cur, fields, r, policy)
    finally:
        _pool().putconn(conn)


async def validate_address(fields: Dict[str, Any], overrides: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """fields = {province, city, zip, address1, address2}. Întoarce {status, address, source, note}."""
    policy = merge_policy(overrides)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(_validate_sync, fields, policy))
