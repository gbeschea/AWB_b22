"""encrypt courier credentials + shopify tokens at rest

Revision ID: e7b3f9a1c2d5
Revises: d4dddf066a16
Create Date: 2026-07-18

=============================================================================
!!! DO NOT RUN THIS ON PRODUCTION FIRST — READ THIS HEADER IN FULL !!!
=============================================================================
This migration ENCRYPTS existing PLAINTEXT secrets IN PLACE:

    * stores.access_token            (TEXT      -> enc:v1:... ciphertext)
    * stores.shared_secret           (TEXT      -> enc:v1:... ciphertext)
    * courier_accounts.credentials   (JSONB     -> encrypted JSON string scalar)

Preconditions & safety:
  1. AWB_B2_ENC_KEY must be set to the SAME key the app uses. The migration
     imports `crypto`, which aborts if the key is missing/invalid.
  2. It is IDEMPOTENT: rows already tagged `enc:v1:` are skipped, so re-running
     is safe.
  3. It also WIDENS stores.access_token / stores.shared_secret from VARCHAR(255)
     to TEXT so ciphertext always fits.

REQUIRED PROCEDURE (per the app owner):
  a. Take a fresh backup / restore the prod DB into a COPY.
  b. Set AWB_B2_ENC_KEY, run `alembic upgrade head` against the COPY.
  c. Boot the app against the COPY with the same key; confirm AWB generation,
     courier tracking and Shopify webhooks still work.
  d. Only then schedule it against production (after another backup).

downgrade() reverses the data (decrypts back to plaintext) and narrows the
columns. NOTE: downgrade re-exposes secrets in plaintext — intended only for a
deliberate rollback to the pre-encryption code.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7b3f9a1c2d5'
down_revision: Union[str, Sequence[str], None] = 'd4dddf066a16'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ENC_PREFIX = "enc:v1:"


def upgrade() -> None:
    import json
    import crypto  # imported lazily so `alembic history` doesn't require the key

    conn = op.get_bind()

    # 1) Widen the token columns so ciphertext always fits.
    op.alter_column('stores', 'access_token', type_=sa.Text(), existing_nullable=True)
    op.alter_column('stores', 'shared_secret', type_=sa.Text(), existing_nullable=True)

    # 2) Encrypt store tokens (skip already-encrypted / empty values).
    rows = conn.execute(sa.text("SELECT id, access_token, shared_secret FROM stores")).fetchall()
    for r in rows:
        updates = {}
        if r.access_token and not r.access_token.startswith(ENC_PREFIX):
            updates['access_token'] = crypto.encrypt(r.access_token)
        if r.shared_secret and not r.shared_secret.startswith(ENC_PREFIX):
            updates['shared_secret'] = crypto.encrypt(r.shared_secret)
        if updates:
            set_clause = ", ".join(f"{k} = :{k}" for k in updates)
            conn.execute(sa.text(f"UPDATE stores SET {set_clause} WHERE id = :id"),
                         {**updates, "id": r.id})

    # 3) Encrypt courier credentials (JSONB). Read as text to be codec-agnostic.
    rows = conn.execute(
        sa.text("SELECT id, credentials::text AS credentials_text FROM courier_accounts")
    ).fetchall()
    for r in rows:
        if not r.credentials_text:
            continue
        val = json.loads(r.credentials_text)  # dict/list (legacy) OR str (already enc)
        if isinstance(val, str) and val.startswith(ENC_PREFIX):
            continue  # already encrypted
        enc = crypto.encrypt(json.dumps(val, ensure_ascii=False))
        conn.execute(
            sa.text("UPDATE courier_accounts SET credentials = CAST(:v AS jsonb) WHERE id = :id"),
            {"v": json.dumps(enc), "id": r.id},  # json.dumps(enc) => quoted JSON string scalar
        )


def downgrade() -> None:
    import json
    import crypto

    conn = op.get_bind()

    # Reverse store tokens.
    rows = conn.execute(sa.text("SELECT id, access_token, shared_secret FROM stores")).fetchall()
    for r in rows:
        updates = {}
        if r.access_token and r.access_token.startswith(ENC_PREFIX):
            updates['access_token'] = crypto.decrypt(r.access_token)
        if r.shared_secret and r.shared_secret.startswith(ENC_PREFIX):
            updates['shared_secret'] = crypto.decrypt(r.shared_secret)
        if updates:
            set_clause = ", ".join(f"{k} = :{k}" for k in updates)
            conn.execute(sa.text(f"UPDATE stores SET {set_clause} WHERE id = :id"),
                         {**updates, "id": r.id})

    # Reverse courier credentials back to a JSONB object.
    rows = conn.execute(
        sa.text("SELECT id, credentials::text AS credentials_text FROM courier_accounts")
    ).fetchall()
    for r in rows:
        if not r.credentials_text:
            continue
        val = json.loads(r.credentials_text)
        if isinstance(val, str) and val.startswith(ENC_PREFIX):
            plain = crypto.decrypt(val)  # JSON text of the original object
            conn.execute(
                sa.text("UPDATE courier_accounts SET credentials = CAST(:v AS jsonb) WHERE id = :id"),
                {"v": plain, "id": r.id},
            )

    op.alter_column('stores', 'access_token', type_=sa.String(length=255), existing_nullable=True)
    op.alter_column('stores', 'shared_secret', type_=sa.String(length=255), existing_nullable=True)
