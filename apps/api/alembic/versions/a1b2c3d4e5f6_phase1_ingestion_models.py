"""Phase 1: ingestion models + observability event columns

Revision ID: a1b2c3d4e5f6
Revises: 28c26487fe25
Create Date: 2026-09-16 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '28c26487fe25'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- observability_events: add source_id, ingested_at, correlation_id ----
    op.add_column(
        'observability_events',
        sa.Column('source_id', sa.String(length=255), nullable=True),
    )
    op.add_column(
        'observability_events',
        sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.add_column(
        'observability_events',
        sa.Column('correlation_id', sa.String(length=64), nullable=True),
    )
    op.add_column(
        'observability_events',
        sa.Column('fingerprint', sa.String(length=64), nullable=True),
    )
    op.create_index(op.f('ix_observability_events_source_id'), 'observability_events', ['source_id'], unique=False)
    op.create_index(op.f('ix_observability_events_correlation_id'), 'observability_events', ['correlation_id'], unique=False)
    op.create_index(op.f('ix_observability_events_fingerprint'), 'observability_events', ['fingerprint'], unique=False)

    # ---- log_records: add source_id, ingested_at ----
    op.add_column(
        'log_records',
        sa.Column('source_id', sa.String(length=255), nullable=True),
    )
    op.add_column(
        'log_records',
        sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.create_index(op.f('ix_log_records_source_id'), 'log_records', ['source_id'], unique=False)

    # ---- observability_sources (new table) ----
    op.create_table(
        'observability_sources',
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('environment_id', sa.UUID(), nullable=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column(
            'source_type',
            sa.Enum(
                'APPLICATION', 'OTEL', 'PROMETHEUS', 'CLOUD',
                'CUSTOM', 'WEBHOOK', 'FILE', 'MOCK',
                name='observabilitysourcecategory',
            ),
            nullable=False,
        ),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('configuration', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            'status',
            sa.Enum(
                'HEALTHY', 'DEGRADED', 'FAILING', 'DISABLED', 'UNKNOWN',
                name='observabilitysourcestatus',
            ),
            nullable=False,
        ),
        sa.Column('last_event_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('error_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('consecutive_errors', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('event_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['environment_id'], ['environments.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_observability_sources_project_id'), 'observability_sources', ['project_id'], unique=False)
    op.create_index(op.f('ix_observability_sources_environment_id'), 'observability_sources', ['environment_id'], unique=False)

    # ---- configuration_change_events (new table) ----
    op.create_table(
        'configuration_change_events',
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('environment_id', sa.UUID(), nullable=True),
        sa.Column('component_id', sa.UUID(), nullable=True),
        sa.Column('change_id', sa.String(length=255), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source', sa.String(length=255), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('source_id', sa.String(length=255), nullable=True),
        sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['environment_id'], ['environments.id']),
        sa.ForeignKeyConstraint(['component_id'], ['system_components.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_configuration_change_events_project_id'), 'configuration_change_events', ['project_id'], unique=False)
    op.create_index(op.f('ix_configuration_change_events_environment_id'), 'configuration_change_events', ['environment_id'], unique=False)
    op.create_index(op.f('ix_configuration_change_events_component_id'), 'configuration_change_events', ['component_id'], unique=False)
    op.create_index(op.f('ix_configuration_change_events_change_id'), 'configuration_change_events', ['change_id'], unique=False)
    op.create_index(op.f('ix_configuration_change_events_timestamp'), 'configuration_change_events', ['timestamp'], unique=False)
    op.create_index(op.f('ix_configuration_change_events_source_id'), 'configuration_change_events', ['source_id'], unique=False)

    # ---- health_check_events (new table) ----
    op.create_table(
        'health_check_events',
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('environment_id', sa.UUID(), nullable=True),
        sa.Column('component_id', sa.UUID(), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'status',
            sa.Enum('HEALTHY', 'DEGRADED', 'UNHEALTHY', 'UNKNOWN', name='healthstatus'),
            nullable=False,
        ),
        sa.Column('latency_ms', sa.Float(), nullable=True),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('source_id', sa.String(length=255), nullable=True),
        sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['environment_id'], ['environments.id']),
        sa.ForeignKeyConstraint(['component_id'], ['system_components.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_health_check_events_project_id'), 'health_check_events', ['project_id'], unique=False)
    op.create_index(op.f('ix_health_check_events_environment_id'), 'health_check_events', ['environment_id'], unique=False)
    op.create_index(op.f('ix_health_check_events_component_id'), 'health_check_events', ['component_id'], unique=False)
    op.create_index(op.f('ix_health_check_events_timestamp'), 'health_check_events', ['timestamp'], unique=False)
    op.create_index(op.f('ix_health_check_events_status'), 'health_check_events', ['status'], unique=False)
    op.create_index(op.f('ix_health_check_events_source_id'), 'health_check_events', ['source_id'], unique=False)

    # ---- ingestion_failures (new table) ----
    op.create_table(
        'ingestion_failures',
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('source_id', sa.String(length=255), nullable=True),
        sa.Column('source', sa.String(length=255), nullable=True),
        sa.Column('project_id', sa.UUID(), nullable=True),
        sa.Column('event_type', sa.String(length=50), nullable=True),
        sa.Column('error_type', sa.String(length=100), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=False),
        sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('payload_summary', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ingestion_failures_fingerprint'), 'ingestion_failures', ['fingerprint'], unique=False)
    op.create_index(op.f('ix_ingestion_failures_source_id'), 'ingestion_failures', ['source_id'], unique=False)
    op.create_index(op.f('ix_ingestion_failures_project_id'), 'ingestion_failures', ['project_id'], unique=False)


def downgrade() -> None:
    # ---- ingestion_failures ----
    op.drop_index(op.f('ix_ingestion_failures_project_id'), table_name='ingestion_failures')
    op.drop_index(op.f('ix_ingestion_failures_source_id'), table_name='ingestion_failures')
    op.drop_index(op.f('ix_ingestion_failures_fingerprint'), table_name='ingestion_failures')
    op.drop_table('ingestion_failures')

    # ---- health_check_events ----
    op.drop_index(op.f('ix_health_check_events_source_id'), table_name='health_check_events')
    op.drop_index(op.f('ix_health_check_events_status'), table_name='health_check_events')
    op.drop_index(op.f('ix_health_check_events_timestamp'), table_name='health_check_events')
    op.drop_index(op.f('ix_health_check_events_component_id'), table_name='health_check_events')
    op.drop_index(op.f('ix_health_check_events_environment_id'), table_name='health_check_events')
    op.drop_index(op.f('ix_health_check_events_project_id'), table_name='health_check_events')
    op.drop_table('health_check_events')
    op.execute('DROP TYPE IF EXISTS healthstatus')

    # ---- configuration_change_events ----
    op.drop_index(op.f('ix_configuration_change_events_source_id'), table_name='configuration_change_events')
    op.drop_index(op.f('ix_configuration_change_events_timestamp'), table_name='configuration_change_events')
    op.drop_index(op.f('ix_configuration_change_events_change_id'), table_name='configuration_change_events')
    op.drop_index(op.f('ix_configuration_change_events_component_id'), table_name='configuration_change_events')
    op.drop_index(op.f('ix_configuration_change_events_environment_id'), table_name='configuration_change_events')
    op.drop_index(op.f('ix_configuration_change_events_project_id'), table_name='configuration_change_events')
    op.drop_table('configuration_change_events')

    # ---- observability_sources ----
    op.drop_index(op.f('ix_observability_sources_environment_id'), table_name='observability_sources')
    op.drop_index(op.f('ix_observability_sources_project_id'), table_name='observability_sources')
    op.drop_table('observability_sources')
    op.execute('DROP TYPE IF EXISTS observabilitysourcecategory')
    op.execute('DROP TYPE IF EXISTS observabilitysourcestatus')

    # ---- log_records: remove added columns ----
    op.drop_index(op.f('ix_log_records_source_id'), table_name='log_records')
    op.drop_column('log_records', 'ingested_at')
    op.drop_column('log_records', 'source_id')

    # ---- observability_events: remove added columns ----
    op.drop_index(op.f('ix_observability_events_correlation_id'), table_name='observability_events')
    op.drop_index(op.f('ix_observability_events_source_id'), table_name='observability_events')
    op.drop_index(op.f('ix_observability_events_fingerprint'), table_name='observability_events')
    op.drop_column('observability_events', 'fingerprint')
    op.drop_column('observability_events', 'correlation_id')
    op.drop_column('observability_events', 'ingested_at')
    op.drop_column('observability_events', 'source_id')
