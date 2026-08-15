-- nomenclator_migrate.sql — aduce schema nomenclatorului Order Hub la paritate cu sursa ÎMBOGĂȚITĂ
-- (metrics.public.romania_addresses + metrics.public.romania_siruta), ca validatorul consolidat să aibă
-- aceleași coloane/tabele ca validatorul „bogat" (address_nomenclator.py: norm-cols + SIRUTA + numar).
--
-- IDEMPOTENT: sigur de re-rulat. DB-ul OH NU e gestionat prin alembic (nu există alembic_version), deci
-- schema se evoluează prin acest DDL direct, nu prin replay de migrări. Rulează pe boxul OH (localhost:5432).

BEGIN;

-- 1) romania_addresses: coloanele care există în sursa îmbogățită dar lipsesc în OH
ALTER TABLE romania_addresses ADD COLUMN IF NOT EXISTS numar           text;   -- interval număr (paritate stânga/dreapta)
ALTER TABLE romania_addresses ADD COLUMN IF NOT EXISTS judet_norm      text;   -- județ fără diacritice (viteză + fuzzy)
ALTER TABLE romania_addresses ADD COLUMN IF NOT EXISTS localitate_norm text;   -- localitate fără diacritice
ALTER TABLE romania_addresses ADD COLUMN IF NOT EXISTS cod_siruta      bigint; -- legătură către SIRUTA (rural/reverse-zip)

CREATE INDEX IF NOT EXISTS ix_ra_judet_norm       ON romania_addresses (judet_norm);
CREATE INDEX IF NOT EXISTS ix_ra_localitate_norm  ON romania_addresses (localitate_norm);
CREATE INDEX IF NOT EXISTS ix_ra_cod_siruta       ON romania_addresses (cod_siruta);
CREATE INDEX IF NOT EXISTS ix_ra_locnorm_judnorm  ON romania_addresses (localitate_norm, judet_norm);

-- 2) romania_siruta (NOU) — nomenclatorul SIRUTA, folosit de regulile rural / reverse-zip / sat↔comună.
--    Coloane 1:1 cu metrics.public.romania_siruta.
CREATE TABLE IF NOT EXISTS romania_siruta (
    cod_siruta      bigint PRIMARY KEY,
    denumire        text,
    denumire_norm   text,
    localitate_norm text,
    tip             integer,
    niv             integer,
    med             text,
    cod_postal      text,
    sirsup          bigint,
    jud             integer,
    judet_norm      text,
    nuts            text
);
CREATE INDEX IF NOT EXISTS ix_siruta_denumire_norm    ON romania_siruta (denumire_norm);
CREATE INDEX IF NOT EXISTS ix_siruta_localitate_norm  ON romania_siruta (localitate_norm);
CREATE INDEX IF NOT EXISTS ix_siruta_judet_norm       ON romania_siruta (judet_norm);
CREATE INDEX IF NOT EXISTS ix_siruta_cod_postal       ON romania_siruta (cod_postal);
CREATE INDEX IF NOT EXISTS ix_siruta_sirsup           ON romania_siruta (sirsup);

COMMIT;
