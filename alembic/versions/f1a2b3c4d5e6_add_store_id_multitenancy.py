"""add store_id to courier tables (multi-tenancy)

⚠️ DO NOT RUN ON PROD BLIND. Additive + idempotent. Test on a DB **copy** first:
   restore/clone prod → `alembic upgrade head` → boot the app against the copy → verify
   couriers/profiles/mappings still load → only then run on prod (after a fresh backup).

Adds a nullable `store_id` FK to courier_accounts / courier_mappings / shipment_profiles
and backfills it to the single existing shop (if exactly one exists). Nullable = legacy
rows keep working; the embedded app's scoped reads treat NULL as "shared".

Revision ID: f1a2b3c4d5e6
Revises: e7b3f9a1c2d5
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e7b3f9a1c2d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("courier_accounts", "courier_mappings", "shipment_profiles")


def _has_column(bind, table: str, col: str) -> bool:
    return any(c["name"] == col for c in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    for t in _TABLES:
        if not _has_column(bind, t, "store_id"):
            op.add_column(t, sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=True))
            op.create_index(f"ix_{t}_store_id", t, ["store_id"])

    # Backfill only when there is exactly ONE shop — the safe single-tenant → multi-tenant seed.
    store_ids = [r[0] for r in bind.execute(sa.text("SELECT id FROM stores")).fetchall()]
    if len(store_ids) == 1:
        sid = store_ids[0]
        for t in _TABLES:
            bind.execute(sa.text(f"UPDATE {t} SET store_id = :sid WHERE store_id IS NULL"), {"sid": sid})

    # Billing columns on stores (cached plan; Shopify is source of truth).
    if not _has_column(bind, "stores", "plan"):
        op.add_column("stores", sa.Column("plan", sa.String(length=32), nullable=False, server_default="free"))
    if not _has_column(bind, "stores", "subscription_gid"):
        op.add_column("stores", sa.Column("subscription_gid", sa.String(length=255), nullable=True))
    if not _has_column(bind, "stores", "subscription_status"):
        op.add_column("stores", sa.Column("subscription_status", sa.String(length=32), nullable=True))

    # Reinstall-proof per-shop ledger (free-trial guard). No FK to stores by design.
    if "app_ledger" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "app_ledger",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("domain_hash", sa.String(length=64), nullable=False),
            sa.Column("trial_used", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("first_seen", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_app_ledger_domain_hash", "app_ledger", ["domain_hash"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    if "app_ledger" in sa.inspect(bind).get_table_names():
        op.drop_index("ix_app_ledger_domain_hash", table_name="app_ledger")
        op.drop_table("app_ledger")
    for col in ("subscription_status", "subscription_gid", "plan"):
        if _has_column(bind, "stores", col):
            op.drop_column("stores", col)
    for t in _TABLES:
        if _has_column(bind, t, "store_id"):
            op.drop_index(f"ix_{t}_store_id", table_name=t)
            op.drop_column(t, "store_id")
