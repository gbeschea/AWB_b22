-- sku_box_map — map-ul CENTRAL SKU → nr. cutii/colete per unitate (paritate cu
-- /root/Scripturi/data/sku_box_map.json de pe VPS, construit zilnic din metafield-urile
-- `custom.nr_cutii`/`nr_produse` de pe orice magazin deals). Sincronizat cu scripts/sku_box_map_sync.sh.
-- Idempotent (DB-ul OH nu are alembic).
CREATE TABLE IF NOT EXISTS sku_box_map (
    sku         VARCHAR(128) PRIMARY KEY,
    boxes       REAL NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT now()
);
