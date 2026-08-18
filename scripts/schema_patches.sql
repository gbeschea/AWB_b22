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
