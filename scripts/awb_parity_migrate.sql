-- awb_parity_migrate.sql — coloane noi pentru paritatea auto-AWB cu cronul xconnector. Idempotent.
-- stores: fereastra BLACKOUT (minute-of-day) în care NU se fac AWB-uri, independentă de fereastra permisă (ore).
ALTER TABLE stores ADD COLUMN IF NOT EXISTS awb_blackout_start integer;
ALTER TABLE stores ADD COLUMN IF NOT EXISTS awb_blackout_end   integer;
-- orders: nr. colete memorat în OH (preluat din metafield-ul Shopify, editabil, cu write-back). NULL = default profil.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS parcel_count integer;

-- ── Moștenirea setărilor org→magazin (simplificarea UI, 2026-08-18) ──────────────────────────────────────
-- validation_policy: nivel ORGANIZAȚIE pentru cele 12 politici (set o dată, toate magazinele moștenesc).
ALTER TABLE validation_policy ADD COLUMN IF NOT EXISTS organization_id integer REFERENCES organizations(id);
CREATE INDEX IF NOT EXISTS ix_validation_policy_org ON validation_policy(organization_id);
-- un singur rând GLOBAL (store & org NULL) și cel mult unul per organizație
CREATE UNIQUE INDEX IF NOT EXISTS uq_validation_policy_global
  ON validation_policy((1)) WHERE store_id IS NULL AND organization_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_validation_policy_org
  ON validation_policy(organization_id) WHERE store_id IS NULL AND organization_id IS NOT NULL;

-- hub_settings: blob JSONB de capabilități (duplicates, blocklist, …), org sau magazin.
CREATE TABLE IF NOT EXISTS hub_settings (
    id              serial PRIMARY KEY,
    organization_id integer REFERENCES organizations(id),
    store_id        integer REFERENCES stores(id) UNIQUE,
    settings        jsonb NOT NULL DEFAULT '{}',
    updated_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_hub_settings_org ON hub_settings(organization_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_hub_settings_org
  ON hub_settings(organization_id) WHERE store_id IS NULL AND organization_id IS NOT NULL;

-- blocklist: clienți blocați MANUAL (îi punem noi). Serial-refuser NU se stochează — se calculează la rulare
-- din istoricul de shipments (services/cron_parity/blocklist.py). Matching pe blind-index. store_id NULL = global.
CREATE TABLE IF NOT EXISTS blocklist (
    id          serial PRIMARY KEY,
    store_id    integer REFERENCES stores(id),
    match_type  varchar(16) NOT NULL,
    value_bidx  varchar(64) NOT NULL,
    reason      text,
    source      varchar(16) NOT NULL DEFAULT 'manual',
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_blocklist_value_bidx ON blocklist(value_bidx);
CREATE INDEX IF NOT EXISTS ix_blocklist_store_active ON blocklist(store_id, active);
