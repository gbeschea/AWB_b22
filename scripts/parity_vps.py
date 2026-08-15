"""Paritate Faza 3 — partea VPS: rulează validatorul de PRODUCȚIE A (address_nomenclator.py din clona
team-intelligence, pe metrics) pe eșantionul comun. Output: parity_vps_out.json (listă {status, zip, city, note}).
Usage: /root/Scripturi/.venv/bin/python parity_vps.py parity_sample.json
"""
import json, os, re, sys, time

sys.path.insert(0, "/root/Scripturi/team-intelligence/plugins/gigi/skills/xconnector")
import address_nomenclator as N

import psycopg2

def _env(key):
    for line in open("/root/Scripturi/.env"):
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise KeyError(key)

url = _env("DATABASE_URL_METRICS")
import urllib.parse as up
p = up.urlparse(re.sub(r"\+\w+", "", url))
conn = psycopg2.connect(host=p.hostname, port=p.port or 5432, user=up.unquote(p.username or ""),
                        password=up.unquote(p.password or ""), dbname=p.path.lstrip("/"),
                        connect_timeout=15)
cur = conn.cursor()
cur.execute("set statement_timeout = 20000")
cur.execute("select count(*) from romania_addresses")
n_addr = cur.fetchone()[0]

rows = json.load(open(sys.argv[1]))
out = []
t0 = time.time()
for i, r in enumerate(rows):
    try:
        res = N.validate_and_correct(cur, r.get("province") or "", r.get("city") or "", r.get("zip") or "",
                                     r.get("address1") or "", r.get("address2") or "")
        addr = res.get("address") or {}
        out.append({"status": res.get("status"), "zip": addr.get("zip"), "city": addr.get("city"),
                    "note": (res.get("note") or "")[:150]})
    except Exception as e:
        conn.rollback()
        out.append({"status": "ERR", "note": repr(e)[:150]})
    if i % 250 == 249:
        print("%d/%d %.0fs" % (i + 1, len(rows), time.time() - t0), flush=True)

json.dump({"n_addr": n_addr, "results": out}, open("parity_vps_out.json", "w"), ensure_ascii=False)
from collections import Counter
print("DONE", dict(Counter(o["status"] for o in out)), "romania_addresses=%d" % n_addr)
