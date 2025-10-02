from alembic import op
import sqlalchemy as sa

# Alea două valori trebuie setate corect:
revision = 'd4dddf066a16'
down_revision = '0ac0fcf02e1a'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column(
        'shipment_profiles',
        sa.Column('default_packing', sa.String(length=20), nullable=True)
    )

def downgrade():
    op.drop_column('shipment_profiles', 'default_packing')
