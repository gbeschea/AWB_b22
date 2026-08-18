-- awb_parity_migrate.sql — coloane noi pentru paritatea auto-AWB cu cronul xconnector. Idempotent.
-- stores: fereastra BLACKOUT (minute-of-day) în care NU se fac AWB-uri, independentă de fereastra permisă (ore).
ALTER TABLE stores ADD COLUMN IF NOT EXISTS awb_blackout_start integer;
ALTER TABLE stores ADD COLUMN IF NOT EXISTS awb_blackout_end   integer;
-- orders: nr. colete memorat în OH (preluat din metafield-ul Shopify, editabil, cu write-back). NULL = default profil.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS parcel_count integer;
