#!/usr/bin/env bash
# Sincronizează map-ul CENTRAL SKU→nr_cutii de pe VPS-ul Scripturi în tabelul OH `sku_box_map`.
# Repetabil (upsert). Rulează de pe boxul OH: bash sku_box_map_sync.sh
# Cerințe: cheia SSH către VPS + venv /tmp/nomtest (psycopg2) + containerul orderhub-web pornit (pt DATABASE_URL).
set -euo pipefail
VPS=${VPS:-root@84.46.242.181}
MAP=/root/Scripturi/data/sku_box_map.json
TMP=$(mktemp /tmp/sku_box_map.XXXX.json)
scp -q "$VPS:$MAP" "$TMP"
DBURL=$(docker inspect orderhub-web --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^DATABASE_URL=' | cut -d= -f2-)
export DBURL TMP
/tmp/nomtest/bin/python - <<'PY'
import json, os, re, urllib.parse as up
import psycopg2
raw = re.sub(r"\+\w+", "", os.environ["DBURL"])
p = up.urlparse(raw)
conn = psycopg2.connect(host=p.hostname, port=p.port or 5432, user=p.username,
                        password=up.unquote(p.password or ""), dbname=p.path.lstrip("/"))
conn.autocommit = True
cur = conn.cursor()
data = json.load(open(os.environ["TMP"]))
n = 0
for sku, v in data.items():
    try:
        boxes = float(v)
    except (TypeError, ValueError):
        continue
    cur.execute("insert into sku_box_map (sku, boxes, updated_at) values (%s, %s, now()) "
                "on conflict (sku) do update set boxes = excluded.boxes, updated_at = now()", (sku, boxes))
    n += 1
cur.execute("select count(*) from sku_box_map")
print("upserted %d / total %d" % (n, cur.fetchone()[0]))
PY
rm -f "$TMP"
