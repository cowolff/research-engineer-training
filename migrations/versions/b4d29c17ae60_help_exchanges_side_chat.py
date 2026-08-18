"""help exchanges (the glossary side chat)

Revision ID: b4d29c17ae60
Revises: eee4fa16c809
Create Date: 2026-08-18 09:41:12.884301

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b4d29c17ae60'
down_revision = 'eee4fa16c809'
branch_labels = None
depends_on = None


def upgrade():
    # A new table only — nothing is added to an existing one, so unlike the
    # previous revision there is no NOT NULL/server_default hazard for
    # databases that already hold rows. Keyed on (scenario_id, user_id) rather
    # than attempt_id on purpose: the side chat is open before the student has
    # sent a first message, which is before an attempt row exists (§5.6).
    op.create_table('help_exchanges',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('scenario_id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('answer_md', sa.Text(), nullable=False),
    sa.Column('declined', sa.Boolean(), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=False),
    sa.Column('prompt_version', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['scenario_id'], ['scenarios.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('help_exchanges', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_help_exchanges_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_help_exchanges_scenario_id'), ['scenario_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_help_exchanges_user_id'), ['user_id'], unique=False)


def downgrade():
    with op.batch_alter_table('help_exchanges', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_help_exchanges_user_id'))
        batch_op.drop_index(batch_op.f('ix_help_exchanges_scenario_id'))
        batch_op.drop_index(batch_op.f('ix_help_exchanges_created_at'))

    op.drop_table('help_exchanges')
