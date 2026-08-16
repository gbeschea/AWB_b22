#!/usr/bin/env bash
# Sincronizează token-urile Frisbo din KB (secret FRISBO_ORG_TOKENS, JSON [{name, token}]) în
# `courier_accounts` ale Order Hub (courier_type=frisbo, credentials EncryptedJSON — criptare prin
# modelele aplicației, în container). Repetabil (upsert). Rulează de pe Mac (are kb.py + ssh).
# După ce owner-ul adaugă JWT-ul unui org nou (ex. duppo.md) în FRISBO_ORG_TOKENS → rulezi asta →
# contul frisbo-<org> apare/actualizează în OH. Nu printează nicio valoare de token.
set -euo pipefail
BOX=${BOX:-root@161.97.69.226}
KBDIR=${KBDIR:-$HOME/Downloads/Scripturi/team-intelligence/plugins/core/scripts}
TMP=$(mktemp /tmp/.frisbo_sync.XXXX.json)
trap 'rm -f "$TMP"' EXIT
(cd "$KBDIR" && uv run kb.py secret-get FRISBO_ORG_TOKENS) > "$TMP"
python3 -c "import json,sys; d=json.load(open('$TMP')); print('KB:', len(d), 'org-uri')"
chmod 644 "$TMP"   # mktemp dă 0600 — în container appuser trebuie să-l poată citi
scp -q "$TMP" "$BOX:/tmp/.frisbo_sync.json"
ssh "$BOX" 'chmod 644 /tmp/.frisbo_sync.json && docker cp /tmp/.frisbo_sync.json orderhub-web:/tmp/.frisbo_sync.json && docker exec -i orderhub-web python - <<PY
import asyncio, json, sys
sys.path.insert(0, "/app")
from sqlalchemy import select
import models
from database import AsyncSessionLocal

async def main():
    rows = json.load(open("/tmp/.frisbo_sync.json"))
    added = updated = 0
    async with AsyncSessionLocal() as db:
        for r in rows:
            name = (r.get("name") or "").strip()
            tok = (r.get("token") or "").strip()
            if not name or not tok:
                continue
            ak = "frisbo-" + name.replace(".", "-")
            ex = (await db.execute(select(models.CourierAccount).where(models.CourierAccount.account_key == ak))).scalar_one_or_none()
            creds = {"token": tok, "org_name": name}
            if ex:
                ex.credentials = creds; ex.is_active = True; updated += 1
            else:
                db.add(models.CourierAccount(name="Frisbo " + name, account_key=ak,
                                             courier_type="frisbo", credentials=creds, is_active=True))
                added += 1
        await db.commit()
    print("frisbo accounts: added=%d updated=%d" % (added, updated))
asyncio.run(main())
PY
docker exec -u root orderhub-web rm -f /tmp/.frisbo_sync.json; rm -f /tmp/.frisbo_sync.json'
echo "OK — conturile Frisbo sunt sincronizate în Order Hub."
