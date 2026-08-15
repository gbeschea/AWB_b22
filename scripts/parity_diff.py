"""Diff-ul de paritate Faza 3: producție A (VPS/metrics) vs OH runner (box/OH DB) pe același eșantion.
Clasifică divergențele: guard-omonimie (intenționat), status-diff, zip-diff, city-diff."""
import json, sys
from collections import Counter

sample = json.load(open(sys.argv[1]))
vps = json.load(open(sys.argv[2]))["results"]
box = json.load(open(sys.argv[3]))

assert len(sample) == len(vps) == len(box), (len(sample), len(vps), len(box))

same_status = 0
diffs = []
for i, (s, a, b) in enumerate(zip(sample, vps, box)):
    if a["status"] == b["status"]:
        same_status += 1
        # la corrected, compară și output-ul
        if a["status"] == "corrected" and (a.get("zip") != b.get("zip") or (a.get("city") or "") != (b.get("city") or "")):
            diffs.append(("output-diff", i, s, a, b))
    else:
        kind = "guard" if b.get("source") == "homonym-guard" else "status-diff"
        diffs.append((kind, i, s, a, b))

print("n=%d · status identic: %d (%.2f%%)" % (len(sample), same_status, 100.0 * same_status / len(sample)))
print("divergențe pe clase:", dict(Counter(d[0] for d in diffs)))
print()
for kind, i, s, a, b in diffs[:40]:
    print("[%s] #%d %s | %s | %s" % (kind, i, (s.get("city") or "")[:25], (s.get("zip") or "")[:8], (s.get("address1") or "")[:45]))
    print("   VPS: %s zip=%s city=%s | %s" % (a["status"], a.get("zip"), (a.get("city") or "")[:20], (a.get("note") or "")[:70]))
    print("   BOX: %s zip=%s city=%s | %s" % (b["status"], b.get("zip"), (b.get("city") or "")[:20], (b.get("note") or "")[:70]))
