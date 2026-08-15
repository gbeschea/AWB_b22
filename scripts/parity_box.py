"""Paritate Faza 3 — partea BOX: rulează runner-ul OH (_validate_sync: A verbatim + guard omonimie,
pe DB-ul OH) pe eșantionul comun. Rulează ÎN CONTAINER: docker exec -i orderhub-web python - < parity_box.py
cu sample-ul la /tmp/parity_sample.json montat... nu — containerul n-are mount: citim de pe stdin marcat.
Simplu: scriptul primește calea ca argv[1] (fișierul e copiat în container cu docker cp).
"""
import json, sys, time
from collections import Counter

sys.path.insert(0, "/app")   # docker exec pune dir-ul scriptului (/tmp) pe sys.path, nu WORKDIR-ul
from services.nomenclator.runner import _validate_sync
from services.nomenclator.policy import merge_policy

rows = json.load(open(sys.argv[1]))
policy = merge_policy(None)
out = []
t0 = time.time()
for i, r in enumerate(rows):
    fields = {"country": "RO", "province": r.get("province") or "", "city": r.get("city") or "",
              "zip": r.get("zip") or "", "address1": r.get("address1") or "", "address2": r.get("address2") or ""}
    try:
        res = _validate_sync(fields, policy)
        addr = res.get("address") or {}
        out.append({"status": res.get("status"), "zip": addr.get("zip"), "city": addr.get("city"),
                    "source": res.get("source"), "note": (res.get("note") or "")[:150]})
    except Exception as e:
        out.append({"status": "ERR", "note": repr(e)[:150]})
    if i % 250 == 249:
        print("%d/%d %.0fs" % (i + 1, len(rows), time.time() - t0), flush=True)

json.dump(out, open("/tmp/parity_box_out.json", "w"), ensure_ascii=False)
print("DONE", dict(Counter(o["status"] for o in out)))
