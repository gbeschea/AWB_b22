#!/usr/bin/env bash
# sync_nomenclator.sh — sync REPETABIL + IDEMPOTENT al nomenclatorului îmbogățit în DB-ul Order Hub.
#
# De ce: OH avea baza VECHE (55.407 rânduri, fără SIRUTA, fără coloane norm/numar/cod_siruta). Sursa de adevăr
# îmbogățită = metrics.public.romania_addresses (67k) + romania_siruta. Niciun server nu vede AMBELE DB-uri:
#   1) export — rulează unde e reachable `metrics` (ex VPS Scripturi): dump cele 2 tabele → fișiere .csv.gz
#   2) import — rulează pe boxul OH (localhost:5432): UPSERT idempotent (ON CONFLICT id / cod_siruta), NU wipe.
# Rulează întâi schema: nomenclator_migrate.sql. Re-rularea e sigură (upsert, nu strică validările în curs).
#
#   SRC_DSN="postgresql://…metrics…"  ./sync_nomenclator.sh export /tmp/nomen
#   OH_DSN="postgresql://…orderhub…"  ./sync_nomenclator.sh import /tmp/nomen
set -euo pipefail

MODE="${1:?export|import}"
DIR="${2:-/tmp/nomen}"
mkdir -p "$DIR"

RA_COLS="id,judet,localitate,tip_artera,nume_strada,numar,cod_postal,sector,judet_norm,localitate_norm,cod_siruta"
SIR_COLS="cod_siruta,denumire,denumire_norm,localitate_norm,tip,niv,med,cod_postal,sirsup,jud,judet_norm,nuts"

if [ "$MODE" = "export" ]; then
  : "${SRC_DSN:?setează SRC_DSN = DSN-ul metrics}"
  psql "$SRC_DSN" -q -c "\copy (select $RA_COLS  from romania_addresses) to '$DIR/ra.csv'     csv"
  psql "$SRC_DSN" -q -c "\copy (select $SIR_COLS from romania_siruta)     to '$DIR/siruta.csv' csv"
  gzip -f "$DIR/ra.csv" "$DIR/siruta.csv"
  echo "export OK → $DIR/ra.csv.gz ($(gunzip -c "$DIR/ra.csv.gz" | wc -l) rânduri), $DIR/siruta.csv.gz ($(gunzip -c "$DIR/siruta.csv.gz" | wc -l) rânduri)"

elif [ "$MODE" = "import" ]; then
  : "${OH_DSN:?setează OH_DSN = DSN-ul Order Hub}"
  gunzip -kf "$DIR/ra.csv.gz" "$DIR/siruta.csv.gz"
  psql "$OH_DSN" -v ON_ERROR_STOP=1 <<SQL
BEGIN;
CREATE TEMP TABLE _ra (LIKE romania_addresses INCLUDING DEFAULTS) ON COMMIT DROP;
\copy _ra ($RA_COLS) from '$DIR/ra.csv' csv
INSERT INTO romania_addresses AS t ($RA_COLS)
SELECT $RA_COLS FROM _ra
ON CONFLICT (id) DO UPDATE SET
  judet=excluded.judet, localitate=excluded.localitate, tip_artera=excluded.tip_artera,
  nume_strada=excluded.nume_strada, numar=excluded.numar, cod_postal=excluded.cod_postal,
  sector=excluded.sector, judet_norm=excluded.judet_norm, localitate_norm=excluded.localitate_norm,
  cod_siruta=excluded.cod_siruta;

CREATE TEMP TABLE _sir (LIKE romania_siruta INCLUDING DEFAULTS) ON COMMIT DROP;
\copy _sir ($SIR_COLS) from '$DIR/siruta.csv' csv
INSERT INTO romania_siruta AS t ($SIR_COLS)
SELECT $SIR_COLS FROM _sir
ON CONFLICT (cod_siruta) DO UPDATE SET
  denumire=excluded.denumire, denumire_norm=excluded.denumire_norm, localitate_norm=excluded.localitate_norm,
  tip=excluded.tip, niv=excluded.niv, med=excluded.med, cod_postal=excluded.cod_postal,
  sirsup=excluded.sirsup, jud=excluded.jud, judet_norm=excluded.judet_norm, nuts=excluded.nuts;
COMMIT;
SQL
  echo "import OK → romania_addresses: $(psql "$OH_DSN" -tAc 'select count(*) from romania_addresses'), romania_siruta: $(psql "$OH_DSN" -tAc 'select count(*) from romania_siruta')"

else
  echo "mod necunoscut: $MODE (export|import)"; exit 1
fi
