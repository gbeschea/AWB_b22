-- validation_policy — politicile ALEGIBILE ale validării de adrese (override-uri peste
-- services/nomenclator/policy.py::POLICY_DEFAULTS). Rând cu store_id NULL = global; altfel per magazin.
-- Idempotent (DB-ul OH nu are alembic — schema se evoluează cu DDL direct, convenția proiectului).
CREATE TABLE IF NOT EXISTS validation_policy (
    id          SERIAL PRIMARY KEY,
    store_id    INTEGER NULL UNIQUE REFERENCES stores(id),
    policies    JSONB NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_validation_policy_store_id ON validation_policy (store_id);
