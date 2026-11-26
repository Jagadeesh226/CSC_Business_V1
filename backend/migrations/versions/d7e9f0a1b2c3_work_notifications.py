"""work notifications (Phase 5.1)

Notifications for more than vehicle expiries: a job assigned to someone,
OCR that failed for good, a lookup waiting for a CAPTCHA, background tasks
failing (admins). Each event notifies a person once (event_key); what it
is about is linked by id.

Revision ID: d7e9f0a1b2c3
Revises: c5d8e1f2a3b4
Create Date: 2026-11-26 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd7e9f0a1b2c3'
down_revision: Union[str, Sequence[str], None] = 'c5d8e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KINDS = (
    "'vehicle_expiry', 'job_assigned', 'ocr_failed', 'lookup_captcha', "
    "'task_failed'"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('notifications', sa.Column('event_key', sa.String(length=200), nullable=True))
    op.add_column('notifications', sa.Column('job_id', sa.Integer(), nullable=True))
    op.add_column('notifications', sa.Column('document_id', sa.Integer(), nullable=True))
    op.add_column('notifications', sa.Column('vehicle_lookup_id', sa.Integer(), nullable=True))
    op.add_column('notifications', sa.Column('task_type', sa.String(length=50), nullable=True))
    op.add_column('notifications', sa.Column('occurrences', sa.Integer(), server_default='1', nullable=False))
    op.create_foreign_key('fk_notifications_job', 'notifications', 'jobs', ['job_id'], ['id'])
    op.create_foreign_key('fk_notifications_document', 'notifications', 'documents', ['document_id'], ['id'])
    op.create_foreign_key('fk_notifications_vehicle_lookup', 'notifications', 'vehicle_lookups', ['vehicle_lookup_id'], ['id'], ondelete='SET NULL')
    op.create_index(op.f('ix_notifications_job_id'), 'notifications', ['job_id'], unique=False)
    op.create_index(op.f('ix_notifications_document_id'), 'notifications', ['document_id'], unique=False)
    op.create_index(op.f('ix_notifications_vehicle_lookup_id'), 'notifications', ['vehicle_lookup_id'], unique=False)
    op.create_unique_constraint('uq_notifications_event', 'notifications', ['user_id', 'event_key'])
    op.drop_constraint('ck_notifications_kind', 'notifications', type_='check')
    op.create_check_constraint('ck_notifications_kind', 'notifications', f"kind IN ({KINDS})")
    op.create_check_constraint('ck_notifications_occurrences', 'notifications', 'occurrences >= 1')


def downgrade() -> None:
    """Downgrade schema. Notifications of the new kinds are removed."""
    op.execute("DELETE FROM notifications WHERE kind <> 'vehicle_expiry'")
    op.drop_constraint('ck_notifications_occurrences', 'notifications', type_='check')
    op.drop_constraint('ck_notifications_kind', 'notifications', type_='check')
    op.create_check_constraint('ck_notifications_kind', 'notifications', "kind IN ('vehicle_expiry')")
    op.drop_constraint('uq_notifications_event', 'notifications', type_='unique')
    op.drop_index(op.f('ix_notifications_vehicle_lookup_id'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_document_id'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_job_id'), table_name='notifications')
    op.drop_constraint('fk_notifications_vehicle_lookup', 'notifications', type_='foreignkey')
    op.drop_constraint('fk_notifications_document', 'notifications', type_='foreignkey')
    op.drop_constraint('fk_notifications_job', 'notifications', type_='foreignkey')
    op.drop_column('notifications', 'occurrences')
    op.drop_column('notifications', 'task_type')
    op.drop_column('notifications', 'vehicle_lookup_id')
    op.drop_column('notifications', 'document_id')
    op.drop_column('notifications', 'job_id')
    op.drop_column('notifications', 'event_key')
