-- Order Hub — schema patches applied DIRECTLY to production.
--
-- ⚠️ WHY THIS FILE EXISTS: this database is NOT managed by alembic. There is no `alembic_version`
-- table in production (verified 2026-07-21), the deploy runs only `uvicorn` (see Dockerfile CMD, no
-- `alembic upgrade head`), and no migration in alembic/versions/ actually creates the core tables
-- (orders, stores, shipments, line_items, …) — the chain's root only ALTERs `stores`. So a migration
-- added the normal way would never run.
--
-- Until a real baseline migration exists, any schema change must be:
--   1. idempotent (IF NOT EXISTS),
--   2. recorded here, and
--   3. applied to prod explicitly:
--        docker exec orderhub-web python -c "...text('<statement>')..."
--      (or psql against DATABASE_URL).
--
-- FOLLOW-UP still open: build a real baseline migration + fold scripts/migrate_pii_encryption.py
-- into it, so a fresh deploy can create the schema (and apply the PII encryption) unattended.
-- APP-STORE-CHECKLIST.md currently claims migration f1a2b3c4d5e6 "creates all tables fresh" — it
-- does not; fix that claim when the baseline lands.

-- ---------------------------------------------------------------------------
-- 2026-07-21 — print_logs.store_id (multi-tenancy)
-- GET /api/print/logs returned EVERY merchant's print history (order names, AWBs, per-SKU shipped
-- volumes) to every other merchant, because PrintLog had no owner column at all. Nullable on
-- purpose: rows created before this have no known owner and are shown to nobody.
ALTER TABLE print_logs ADD COLUMN IF NOT EXISTS store_id INTEGER;
CREATE INDEX IF NOT EXISTS ix_print_logs_store_id ON print_logs (store_id);

-- COMP (2026-08-18): magazinele NOASTRE primesc entitlementul Pro (etichete nelimitate) FĂRĂ abonament Shopify
-- — e aplicația noastră pe magazinele noastre; comisionul Shopify pe bani circulari n-are sens. entitled_plan_key()
-- îl respectă; instalările EXTERNE rămân default false (se abonează sau stau pe Free).
ALTER TABLE stores ADD COLUMN IF NOT EXISTS comp boolean NOT NULL DEFAULT false;

-- AUTOMATION SCHEDULE (2026-08-18): programarea per-magazin a automatizărilor (mod on_order|cron|on_delivered|off
-- + minute) + acțiuni de risc, aleasă de merchant peste default-uri (services.automation_config). NULL = default-uri.
ALTER TABLE stores ADD COLUMN IF NOT EXISTS automation_schedule jsonb;

-- AWB GIVEUP / failcount (2026-08-19): contorul de eșecuri AWB per comandă (services/awb_giveup.py), port
-- din cronul xConnector (AWB_GIVEUP_AFTER + _awb_failcount_bump). Un AWB care pică e reîncercat la fiecare
-- tură; când eroarea „tranzitorie" nu se mai rezolvă niciodată (adresă moartă / stradă absentă din
-- nomenclatorul curierului) comanda bucla la infinit — măsurat în cron: comenzi picate 7 ture la rând, la
-- care nu se uita nimeni. După N eșecuri comanda e predată la CS (HOLD) în loc să fie reîncercată.
--
-- De ce tabelă separată și nu o coloană pe `orders`: cronul ținea contorul într-un fișier JSON; aici trebuie
-- în DB, dar baza NU e pe alembic (vezi antetul) — un deploy făcut înaintea acestui patch, cu coloana și în
-- models.py, ar rupe ORICE citire de comenzi. Cu tabelă separată, dacă patch-ul lipsește, degradează doar
-- funcția de giveup (awb_giveup.py prinde eroarea, o logează și se comportă ca înainte).
-- `order_id` e cheie primară (un rând per comandă) + ON DELETE CASCADE (moare cu comanda).
CREATE TABLE IF NOT EXISTS awb_fail_counts (
    order_id      integer PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,
    store_id      integer,                        -- denormalizat: rapoarte per magazin fără join
    fails         integer NOT NULL DEFAULT 0,     -- eșecuri cumulate de la ultimul reset
    last_class    varchar(16),                    -- transient | permanent | config
    last_error    text,                           -- ultimul mesaj de la curier (pentru omul de la CS)
    first_fail_at timestamptz NOT NULL DEFAULT now(),
    last_fail_at  timestamptz NOT NULL DEFAULT now(),
    held_at       timestamptz                     -- când am predat-o la CS (NULL = încă în retry)
);
CREATE INDEX IF NOT EXISTS ix_awb_fail_counts_store ON awb_fail_counts (store_id);

-- POLLING STARVATION (2026-08-19): bucla de status-sync alegea shipmenturile după `last_status_at ASC NULLS
-- FIRST`, dar un poll EȘUAT (curier nerezolvabil, eroare de API) NU seta niciun timestamp → aceleași
-- shipmenturi rămâneau veșnic în capul cozii și blocau restul. Măsurat: 53.583 din 53.746 eligibile NU
-- fuseseră pollate NICIODATĂ (99,7%) — practic doar un magazin avea statusuri de curier, deci tab-urile
-- „Livrate/În tranzit/Refuzate" erau goale pe 25 din 27 magazine.
-- `last_poll_at` = când am ÎNCERCAT ultima dată (indiferent de rezultat), separat de `last_status_at` =
-- când a raportat curierul. Coada se ordonează după ÎNCERCARE, deci nimic nu mai poate flămânzi.
ALTER TABLE shipments ADD COLUMN IF NOT EXISTS last_poll_at timestamptz;
CREATE INDEX IF NOT EXISTS ix_shipments_last_poll_at ON shipments (last_poll_at);

-- ⚠️ OWNERSHIP: tabelele create rulând psql CA `postgres` rămân ale lui postgres, iar aplicația (user
-- `order_hub`) primește „permission denied" — capcană deja plătită o dată cu hub_settings/blocklist.
-- Orice tabelă nouă are nevoie de linia asta.
ALTER TABLE awb_fail_counts OWNER TO order_hub;

-- Garda de stoc: o linie per SKU aflat sub prag. `cleared_at` marchează re-armarea (stocul a urcat
-- înapoi peste prag + marjă), deci nu trimitem al doilea mail pentru aceeași cădere.
CREATE TABLE IF NOT EXISTS inventory_alerts (
    sku         text PRIMARY KEY,
    name        text,
    qty         integer,
    threshold   integer,
    alerted_at  timestamptz NOT NULL DEFAULT now(),
    cleared_at  timestamptz
);
CREATE INDEX IF NOT EXISTS ix_inventory_alerts_open ON inventory_alerts (cleared_at) WHERE cleared_at IS NULL;
