"""family khata

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-31 14:36:35.744917
"""

from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    existing_tables = set(insp.get_table_names())

    if "families" not in existing_tables:
        op.create_table(
            'families',
            sa.Column('id', sa.Uuid(), nullable=False),
            sa.Column('merchant_id', sa.Uuid(), nullable=False),
            sa.Column('display_name', sa.String(length=200), nullable=False),
            sa.Column('name_normalized', sa.String(length=200), nullable=False),
            sa.Column('head_customer_id', sa.Uuid(), nullable=False),
            sa.Column('area_hint', sa.Text(), nullable=True),
            sa.Column('credit_limit_paise', sa.BigInteger(), nullable=True),
            sa.Column('merged_into_id', sa.Uuid(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(['head_customer_id'], ['customers.id'], name='fk_families_head', use_alter=True),
            sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id']),
            sa.ForeignKeyConstraint(['merged_into_id'], ['families.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        with op.batch_alter_table('families', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_families_merchant_id'), ['merchant_id'], unique=False)
    else:
        existing_indices = {ix["name"] for ix in insp.get_indexes("families")}
        if "ix_families_merchant_id" not in existing_indices:
            with op.batch_alter_table('families', schema=None) as batch_op:
                batch_op.create_index(batch_op.f('ix_families_merchant_id'), ['merchant_id'], unique=False)

    if "family_merges" not in existing_tables:
        op.create_table(
            'family_merges',
            sa.Column('id', sa.Uuid(), nullable=False),
            sa.Column('merchant_id', sa.Uuid(), nullable=False),
            sa.Column('from_family_id', sa.Uuid(), nullable=False),
            sa.Column('into_family_id', sa.Uuid(), nullable=False),
            sa.Column('moved', sa.JSON(), nullable=False),
            sa.Column('merged_by', sa.String(length=60), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(['from_family_id'], ['families.id']),
            sa.ForeignKeyConstraint(['into_family_id'], ['families.id']),
            sa.ForeignKeyConstraint(['merchant_id'], ['merchants.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        with op.batch_alter_table('family_merges', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_family_merges_merchant_id'), ['merchant_id'], unique=False)
    else:
        existing_indices = {ix["name"] for ix in insp.get_indexes("family_merges")}
        if "ix_family_merges_merchant_id" not in existing_indices:
            with op.batch_alter_table('family_merges', schema=None) as batch_op:
                batch_op.create_index(batch_op.f('ix_family_merges_merchant_id'), ['merchant_id'], unique=False)

    existing_customer_cols = {col["name"] for col in insp.get_columns("customers")}
    existing_customer_indices = {ix["name"] for ix in insp.get_indexes("customers")}
    with op.batch_alter_table('customers', schema=None) as batch_op:
        if "family_id" not in existing_customer_cols:
            batch_op.add_column(sa.Column('family_id', sa.Uuid(), nullable=True))
        if "relation_to_head" not in existing_customer_cols:
            batch_op.add_column(sa.Column('relation_to_head', sa.String(length=20), nullable=True))
        if "can_take_credit" not in existing_customer_cols:
            batch_op.add_column(sa.Column('can_take_credit', sa.Boolean(), server_default=sa.true(), nullable=False))
        if "member_limit_paise" not in existing_customer_cols:
            batch_op.add_column(sa.Column('member_limit_paise', sa.BigInteger(), nullable=True))
        if "ix_customers_family_id" not in existing_customer_indices:
            batch_op.create_index(batch_op.f('ix_customers_family_id'), ['family_id'], unique=False)
        batch_op.create_foreign_key('fk_customers_family_id', 'families', ['family_id'], ['id'])

    existing_ledger_cols = {col["name"] for col in insp.get_columns("ledger_entries")}
    with op.batch_alter_table('ledger_entries', schema=None) as batch_op:
        if "family_id" not in existing_ledger_cols:
            batch_op.add_column(sa.Column('family_id', sa.Uuid(), nullable=True))
        if "taken_by_customer_id" not in existing_ledger_cols:
            batch_op.add_column(sa.Column('taken_by_customer_id', sa.Uuid(), nullable=True))
        batch_op.create_foreign_key('fk_ledger_taken_by', 'customers', ['taken_by_customer_id'], ['id'])
        batch_op.create_foreign_key('fk_ledger_family_id', 'families', ['family_id'], ['id'])


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    existing_tables = set(insp.get_table_names())

    if "ledger_entries" in existing_tables:
        existing_cols = {col["name"] for col in insp.get_columns("ledger_entries")}
        with op.batch_alter_table('ledger_entries', schema=None) as batch_op:
            batch_op.drop_constraint('fk_ledger_family_id', type_='foreignkey')
            batch_op.drop_constraint('fk_ledger_taken_by', type_='foreignkey')
            if 'taken_by_customer_id' in existing_cols:
                batch_op.drop_column('taken_by_customer_id')
            if 'family_id' in existing_cols:
                batch_op.drop_column('family_id')

    if "customers" in existing_tables:
        existing_cols = {col["name"] for col in insp.get_columns("customers")}
        existing_indices = {ix["name"] for ix in insp.get_indexes("customers")}
        with op.batch_alter_table('customers', schema=None) as batch_op:
            batch_op.drop_constraint('fk_customers_family_id', type_='foreignkey')
            if 'ix_customers_family_id' in existing_indices:
                batch_op.drop_index(batch_op.f('ix_customers_family_id'))
            if 'member_limit_paise' in existing_cols:
                batch_op.drop_column('member_limit_paise')
            if 'can_take_credit' in existing_cols:
                batch_op.drop_column('can_take_credit')
            if 'relation_to_head' in existing_cols:
                batch_op.drop_column('relation_to_head')
            if 'family_id' in existing_cols:
                batch_op.drop_column('family_id')

    if "family_merges" in existing_tables:
        existing_indices = {ix["name"] for ix in insp.get_indexes("family_merges")}
        with op.batch_alter_table('family_merges', schema=None) as batch_op:
            if 'ix_family_merges_merchant_id' in existing_indices:
                batch_op.drop_index(batch_op.f('ix_family_merges_merchant_id'))
        op.drop_table('family_merges')

    if "families" in existing_tables:
        existing_indices = {ix["name"] for ix in insp.get_indexes("families")}
        with op.batch_alter_table('families', schema=None) as batch_op:
            if 'ix_families_merchant_id' in existing_indices:
                batch_op.drop_index(batch_op.f('ix_families_merchant_id'))
        op.drop_table('families')
