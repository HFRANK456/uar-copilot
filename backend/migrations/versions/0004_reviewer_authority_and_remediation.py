"""reviewer authority and remediation tracking

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14 22:36:04.446210
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A later upload used only to confirm remediation. Postgres can't use a new enum value in the
    # same transaction it was added in, but nothing below uses it.
    op.execute("ALTER TYPE source_kind ADD VALUE IF NOT EXISTS 'verification'")

    op.add_column('decision', sa.Column('assigned_reviewer_id', sa.UUID(), nullable=True))
    op.add_column('decision', sa.Column('as_assigned_reviewer', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('decision', sa.Column('override_reason', sa.Text(), nullable=True))
    op.create_foreign_key('fk_decision_assigned_reviewer', 'decision', 'person', ['assigned_reviewer_id'], ['id'],
                          ondelete='SET NULL')

    op.add_column('remediation', sa.Column('campaign_id', sa.UUID(), nullable=True))
    op.add_column('remediation', sa.Column('review_item_id', sa.UUID(), nullable=True))
    op.add_column('remediation', sa.Column('action', sa.String(length=20), nullable=True))
    op.add_column('remediation', sa.Column('note', sa.Text(), nullable=True))
    op.add_column('remediation', sa.Column('resolved_by', sa.String(length=255), nullable=True))
    op.add_column('remediation', sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('remediation', sa.Column('verification_method', sa.String(length=50), nullable=True))
    op.create_index(op.f('ix_remediation_campaign_id'), 'remediation', ['campaign_id'], unique=False)
    op.create_foreign_key('fk_remediation_review_item', 'remediation', 'review_item', ['review_item_id'], ['id'],
                          ondelete='CASCADE')
    op.create_foreign_key('fk_remediation_campaign', 'remediation', 'campaign', ['campaign_id'], ['id'],
                          ondelete='CASCADE')


def downgrade() -> None:
    op.drop_constraint('fk_remediation_campaign', 'remediation', type_='foreignkey')
    op.drop_constraint('fk_remediation_review_item', 'remediation', type_='foreignkey')
    op.drop_index(op.f('ix_remediation_campaign_id'), table_name='remediation')
    for col in ('verification_method', 'resolved_at', 'resolved_by', 'note', 'action', 'review_item_id', 'campaign_id'):
        op.drop_column('remediation', col)
    op.drop_constraint('fk_decision_assigned_reviewer', 'decision', type_='foreignkey')
    for col in ('override_reason', 'as_assigned_reviewer', 'assigned_reviewer_id'):
        op.drop_column('decision', col)
    # Postgres cannot drop a single enum value; 'verification' stays on downgrade (harmless).
