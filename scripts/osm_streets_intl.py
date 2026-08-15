#!/usr/bin/env python3
"""
osm_streets_intl.py — nomenclator STRADAL pentru BG/HU/SK din OpenStreetMap (Geofabrik), în DB-ul Order Hub.

BG/HU/SK n-au acoperire stradală bună în OpenAddresses (CZ/PL au: 102k/342k). OSM are addr nodes
(addr:street/city/postcode/housenumber) → străzi comprehensive. Descarcă extractul Geofabrik per țară,
parsează cu pyosmium, agregă pe (city, street, postcode) → num_min/num_max/cnt, încarcă în
bg_streets_osm / hu_streets / sk_streets (schema ca bg_streets). Idempotent (TRUNCATE+reload).

Rezultat măsurat 2026-08-15: BG 16.019 (era 7.7k), HU 47.492 (era 0), SK 31.642 (era 0).

Cerințe: `pip install osmium asyncpg` (rulează pe boxul OH — are internet + DB; NU în containerul app, n-are osmium).
  DATABASE_URL=... python osm_streets_intl.py
"""
import os, re, io, asyncio, unicodedata, urllib.request

import osmium
import asyncpg

COUNTRIES = [("bulgaria", "bg_streets_osm"), ("hungary", "hu_streets"), ("slovakia", "sk_streets")]
_num = re.compile(r"\d+")


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").strip())
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


class AddrHandler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.agg = {}

    def _add(self, tags):
        st = tags.get("addr:street")
        if not st:
            return
        city = tags.get("addr:city") or tags.get("addr:place") or ""
        pc = tags.get("addr:postcode") or ""
        m = _num.search(tags.get("addr:housenumber") or "")
        num = int(m.group()) if m else None
        k = (city, st, pc)
        r = self.agg.get(k)
        if r is None:
            r = [num, num, 0]
            self.agg[k] = r
        r[2] += 1
        if num is not None:
            r[0] = num if r[0] is None else min(r[0], num)
            r[1] = num if r[1] is None else max(r[1], num)

    def node(self, n):
        self._add(n.tags)

    def way(self, w):
        self._add(w.tags)


def download(country: str) -> str:
    path = f"/tmp/osm/{country}.osm.pbf"
    os.makedirs("/tmp/osm", exist_ok=True)
    if not os.path.exists(path) or os.path.getsize(path) < 1_000_000:
        url = f"https://download.geofabrik.de/europe/{country}-latest.osm.pbf"
        urllib.request.urlretrieve(url, path)  # urlretrieve follows redirects
    return path


async def load(dsn: str, tbl: str, agg: dict) -> int:
    DDL = (f"CREATE TABLE IF NOT EXISTS {tbl} (city text, city_norm text, street text, street_norm text, "
           f"street_lat text, postcode text, num_min integer, num_max integer, cnt integer);"
           f"CREATE INDEX IF NOT EXISTS ix_{tbl}_city_norm ON {tbl}(city_norm);"
           f"CREATE INDEX IF NOT EXISTS ix_{tbl}_street_norm ON {tbl}(street_norm);"
           f"CREATE INDEX IF NOT EXISTS ix_{tbl}_postcode ON {tbl}(postcode);")
    rows = [(c, fold(c), s, fold(s), s, pc, r[0], r[1], r[2]) for (c, s, pc), r in agg.items()]
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(DDL)
        async with conn.transaction():
            await conn.execute(f"TRUNCATE {tbl}")
            await conn.copy_records_to_table(
                tbl, records=rows,
                columns=["city", "city_norm", "street", "street_norm", "street_lat", "postcode", "num_min", "num_max", "cnt"])
        return await conn.fetchval(f"select count(*) from {tbl}")
    finally:
        await conn.close()


async def main():
    dsn = re.sub(r"^postgresql(\+\w+)?://", "postgresql://", os.environ["DATABASE_URL"])
    for country, tbl in COUNTRIES:
        path = download(country)
        print(f"parsing {path} ...", flush=True)
        h = AddrHandler()
        h.apply_file(path)
        n = await load(dsn, tbl, h.agg)
        print(f"DONE {tbl}: {n} distinct streets (city+street+postcode)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
