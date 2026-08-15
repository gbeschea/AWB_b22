#!/usr/bin/env python3
"""
geonames_hu_sk.py — nomenclator localitate + cod poștal pentru HU/SK din GeoNames, în DB-ul Order Hub.

HU (Ungaria) și SK (Slovacia) NU au date stradale OpenAddresses ca CZ/PL/BG, deci sursa de referință e
GeoNames (download.geonames.org/export/zip, licență CC-BY): localitate + cod poștal + admin. Suficient pt
validarea „localitate+cod poștal e reală?" (stradal rămâne HERE geocoding). Rulează pe boxul OH (are internet
+ DB pe localhost, în containerul orderhub-web care are asyncpg). Idempotent (TRUNCATE + reload).

  docker exec orderhub-web python /opt/orderhub/scripts/geonames_hu_sk.py
"""
import os, re, io, zipfile, asyncio, unicodedata, urllib.request

import asyncpg


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").strip())
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


DDL = ("CREATE TABLE IF NOT EXISTS {t} (postcode text, name text, name_norm text, admin1 text, admin2 text);"
       "CREATE INDEX IF NOT EXISTS ix_{t}_name_norm ON {t}(name_norm);"
       "CREATE INDEX IF NOT EXISTS ix_{t}_postcode ON {t}(postcode);")


def fetch(country: str):
    # GeoNames zip TSV: country postcode place admin1 a1code admin2 a2code admin3 a3code lat lon accuracy
    url = f"https://download.geonames.org/export/zip/{country}.zip"
    with urllib.request.urlopen(url, timeout=60) as r:
        z = zipfile.ZipFile(io.BytesIO(r.read()))
    rows = []
    for line in z.read(f"{country}.txt").decode("utf-8").splitlines():
        f = line.split("\t")
        if len(f) < 4:
            continue
        rows.append((f[1], f[2], fold(f[2]), f[3], f[5] if len(f) > 5 else ""))
    return rows


async def main():
    u = re.sub(r"^postgresql(\+\w+)?://", "postgresql://", os.environ["DATABASE_URL"])
    c = await asyncpg.connect(u)
    try:
        for country, tbl in (("HU", "hu_localities"), ("SK", "sk_localities")):
            rows = fetch(country)
            await c.execute(DDL.format(t=tbl))
            async with c.transaction():
                await c.execute(f"TRUNCATE {tbl}")
                await c.copy_records_to_table(tbl, records=rows,
                                              columns=["postcode", "name", "name_norm", "admin1", "admin2"])
            print(f"{tbl}: {await c.fetchval(f'select count(*) from {tbl}')}")
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
