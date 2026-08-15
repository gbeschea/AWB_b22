-- intl_nomenclator_migrate.sql — nomenclatoarele INTERNAȚIONALE (CZ/PL/BG) în DB-ul Order Hub, ca app-ul să poată
-- valida adrese intl (Bonhaus CZ/BG + Polonia) când preia cronul. HU/SK n-au nomenclator → HERE geocoding (separat).
-- Schema 1:1 cu metrics.public.{cz_addresses,pl_addresses,bg_localities,bg_streets}. IDEMPOTENT (IF NOT EXISTS).
-- Popularea = sync separat (export metrics → import atomic TRUNCATE+COPY, fiind tabele de referință full-refresh).

BEGIN;

-- Cehia
CREATE TABLE IF NOT EXISTS cz_addresses (
    obec text, district text, cast_obce text, ulice text, psc text,
    num_min integer, num_max integer, cnt integer, obec_norm text, ulice_norm text
);
CREATE INDEX IF NOT EXISTS ix_cz_obec_norm  ON cz_addresses (obec_norm);
CREATE INDEX IF NOT EXISTS ix_cz_ulice_norm ON cz_addresses (ulice_norm);
CREATE INDEX IF NOT EXISTS ix_cz_psc        ON cz_addresses (psc);

-- Polonia
CREATE TABLE IF NOT EXISTS pl_addresses (
    region text, powiat text, city text, street text, postcode text,
    num_min integer, num_max integer, has_odd boolean, has_even boolean, cnt integer,
    city_norm text, street_norm text
);
CREATE INDEX IF NOT EXISTS ix_pl_city_norm   ON pl_addresses (city_norm);
CREATE INDEX IF NOT EXISTS ix_pl_street_norm ON pl_addresses (street_norm);
CREATE INDEX IF NOT EXISTS ix_pl_postcode    ON pl_addresses (postcode);

-- Bulgaria — localități
CREATE TABLE IF NOT EXISTS bg_localities (
    name text, name_norm text, name_lat text, place_type text, postcode text, cnt integer
);
CREATE INDEX IF NOT EXISTS ix_bgl_name_norm ON bg_localities (name_norm);
CREATE INDEX IF NOT EXISTS ix_bgl_postcode  ON bg_localities (postcode);

-- Bulgaria — străzi
CREATE TABLE IF NOT EXISTS bg_streets (
    city text, city_norm text, street text, street_norm text, street_lat text,
    postcode text, num_min integer, num_max integer, cnt integer
);
CREATE INDEX IF NOT EXISTS ix_bgs_city_norm   ON bg_streets (city_norm);
CREATE INDEX IF NOT EXISTS ix_bgs_street_norm ON bg_streets (street_norm);
CREATE INDEX IF NOT EXISTS ix_bgs_postcode    ON bg_streets (postcode);

COMMIT;
