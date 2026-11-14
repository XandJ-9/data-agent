"""Single source of truth for the schema: one MetaData for every backend.

Conventions (cross-backend invariant):
- timestamps are Text in 'YYYY-MM-DD HH:MM:SS' format (local time, lexicographic sort)
- booleans are 0/1 SmallInteger; JSON documents stay Text
- partial unique indexes declare sqlite_where AND postgresql_where explicitly
- server defaults cover both Core inserts and raw administrative/fixture inserts
"""
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    text,
)

NAMING_CONVENTION = {
    'ix': 'ix_%(column_0_label)s',
    'uq': 'uq_%(table_name)s_%(column_0_name)s',
    'ck': 'ck_%(table_name)s_%(constraint_name)s',
    'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
    'pk': 'pk_%(table_name)s',
}

# Bump on every schema evolution.
SCHEMA_VERSION = 3

metadata = MetaData(naming_convention=NAMING_CONVENTION)

_schema_version = Table('_schema_version', metadata,
    Column('version', Integer, nullable=False))

users = Table('users', metadata,
    Column('id', Text, primary_key=True),
    Column('username', Text, nullable=False, unique=True),
    Column('password_hash', Text, nullable=False))

# Promotion markers: admin-only operations (skill upload) key off membership.
admins = Table('admins', metadata,
    Column('user_id', Text, ForeignKey('users.id'), primary_key=True))

auth_sessions = Table('auth_sessions', metadata,
    Column('token_hash', Text, primary_key=True),
    Column('user_id', Text, ForeignKey('users.id'), nullable=False),
    Column('csrf_token', Text, nullable=False),
    Column('expires_at', Text, nullable=False))

login_attempts = Table('login_attempts', metadata,
    Column('key', Text, primary_key=True),
    Column('attempts', Integer, nullable=False),
    Column('window_start', Text, nullable=False))

sessions = Table('sessions', metadata,
    Column('id', Text, primary_key=True),
    Column('user_id', Text, ForeignKey('users.id'), nullable=False),
    Column('title', Text, nullable=False),
    Column('created_at', Text, nullable=False),
    Column('memory_cursor', Integer, nullable=False, server_default=text('0')),
    Column('memory_dirty', Integer, nullable=False, server_default=text('0')),
    Column('memory_retry_at', Text, nullable=False, server_default=text("'1970-01-01 00:00:00.000000'")),
    Column('memory_failures', Integer, nullable=False, server_default=text('0')),
    Column('memory_error', Text),
    Column('archived_at', Text))

messages = Table('messages', metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('user_id', Text, ForeignKey('users.id'), nullable=False),
    Column('session_id', Text, ForeignKey('sessions.id'), nullable=False),
    Column('run_id', Text, ForeignKey('runs.id', use_alter=True), nullable=False),
    Column('role', Text,
           CheckConstraint("role IN ('user','assistant')", name='role_user_or_assistant'), nullable=False),
    Column('content', Text, nullable=False),
    Column('created_at', Text, nullable=False),
    UniqueConstraint('run_id', 'role', name='uq_messages_run_role'))

# runs is declared after messages so message FKs resolve; its own back-reference
# (input_message_id) uses use_alter to break the circular dependency.
runs = Table('runs', metadata,
    Column('id', Text, primary_key=True),
    Column('user_id', Text, ForeignKey('users.id'), nullable=False),
    Column('session_id', Text, ForeignKey('sessions.id'), nullable=False),
    Column('input_message_id', Integer, ForeignKey('messages.id', use_alter=True)),
    Column('client_message_id', Text, nullable=False),
    Column('status', Text, nullable=False, server_default='queued'),
    Column('error_code', Text),
    Column('created_at', Text, nullable=False),
    Column('owner', Text),
    Column('generation', Integer, nullable=False, server_default=text('0')),
    Column('lease_until', Text),
    Column('definition_hash', Text, nullable=False),
    Column('skills_snapshot', Text, nullable=False),
    Column('config_snapshot', Text, nullable=False),
    Column('memory_revision', Integer),
    Column('source', Text, nullable=False, server_default='web'),
    Column('binding_id', Text, ForeignKey('channel_identities.id')),
    Column('reply_target', Text),
    UniqueConstraint('user_id', 'session_id', 'client_message_id', name='uq_runs_client_message'))

# Per-run execution trace: intermediate assistant replies, tool calls and results
# persisted incrementally by the worker so progress (and interrupts) are observable.
run_steps = Table('run_steps', metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('run_id', Text, ForeignKey('runs.id'), nullable=False),
    Column('seq', Integer, nullable=False),
    Column('kind', Text, nullable=False),
    Column('name', Text),
    Column('content', Text, nullable=False),
    Column('reasoning', Text),
    Column('created_at', Text, nullable=False),
    UniqueConstraint('run_id', 'seq', name='uq_run_steps_run_seq'))

user_memories = Table('user_memories', metadata,
    Column('user_id', Text, ForeignKey('users.id'), primary_key=True),
    Column('revision', Integer, nullable=False, server_default=text('0')),
    Column('entries', Text, nullable=False, server_default='[]'),
    Column('excluded_before', Integer, nullable=False, server_default=text('0')))

memory_exclusions = Table('memory_exclusions', metadata,
    Column('user_id', Text, ForeignKey('users.id'), primary_key=True),
    Column('message_id', Integer, ForeignKey('messages.id'), primary_key=True))

user_skills = Table('user_skills', metadata,
    Column('user_id', Text, ForeignKey('users.id'), primary_key=True),
    Column('name', Text, primary_key=True),
    Column('enabled', Integer, nullable=False))

channel_identities = Table('channel_identities', metadata,
    Column('id', Text, primary_key=True),
    Column('user_id', Text, ForeignKey('users.id'), nullable=False),
    Column('channel', Text, nullable=False, server_default='feishu'),
    Column('app_id', Text, nullable=False),
    Column('tenant_id', Text, nullable=False),
    Column('external_user_id', Text, nullable=False),
    Column('session_id', Text, ForeignKey('sessions.id'), nullable=False),
    Column('active', SmallInteger, nullable=False, server_default=text('1')))

binding_codes = Table('binding_codes', metadata,
    Column('code_hash', Text, primary_key=True),
    Column('user_id', Text, ForeignKey('users.id'), nullable=False),
    Column('expires_at', Text, nullable=False),
    Column('consumed_at', Text))

deliveries = Table('deliveries', metadata,
    Column('id', Text, primary_key=True),
    Column('run_id', Text, ForeignKey('runs.id'), nullable=False),
    Column('binding_id', Text, ForeignKey('channel_identities.id'), nullable=False),
    Column('reply_target', Text, nullable=False),
    Column('content', Text, nullable=False),
    Column('part', Integer, nullable=False, server_default=text('0')),
    Column('status', Text, nullable=False, server_default='pending'),
    Column('receipt', Text),
    Column('error_code', Text),
    Column('attempts', Integer, nullable=False, server_default=text('0')),
    Column('retry_at', Text, nullable=False, server_default=text("'1970-01-01 00:00:00.000000'")),
    Column('owner', Text),
    Column('generation', Integer, nullable=False, server_default=text('0')),
    Column('lease_until', Text),
    UniqueConstraint('run_id', 'part', name='uq_deliveries_run_part'))

feishu_events = Table('feishu_events', metadata,
    Column('app_id', Text, primary_key=True),
    Column('tenant_id', Text, primary_key=True),
    Column('event_id', Text, primary_key=True),
    Column('message_id', Text),
    Column('received_at', Text, nullable=False))

feishu_replies = Table('feishu_replies', metadata,
    Column('id', Text, primary_key=True),
    Column('app_id', Text, nullable=False),
    Column('tenant_id', Text, nullable=False),
    Column('external_user_id', Text, nullable=False),
    Column('reply_target', Text, nullable=False),
    Column('content', Text, nullable=False),
    Column('status', Text, nullable=False, server_default='pending'),
    Column('receipt', Text),
    Column('error_code', Text),
    Column('attempts', Integer, nullable=False, server_default=text('0')),
    Column('retry_at', Text, nullable=False, server_default=text("'1970-01-01 00:00:00.000000'")),
    Column('owner', Text),
    Column('generation', Integer, nullable=False, server_default=text('0')),
    Column('lease_until', Text),
    Column('uuid', Text, nullable=False, unique=True),
    Column('created_at', Text, nullable=False))

feishu_bind_attempts = Table('feishu_bind_attempts', metadata,
    Column('identity_key', Text, primary_key=True),
    Column('window_start', Text, nullable=False),
    Column('attempts', Integer, nullable=False))

# Admin-configured MCP servers: external tool sources for the agent. Credentials
# (env/headers) live here and must never reach logs or model context.
mcp_servers = Table('mcp_servers', metadata,
    Column('id', Text, primary_key=True),
    Column('name', Text, nullable=False, unique=True),
    Column('transport', Text, nullable=False),
    Column('command', Text),
    Column('args_json', Text, nullable=False, server_default='[]'),
    Column('env_json', Text, nullable=False, server_default='{}'),
    Column('url', Text),
    Column('headers_json', Text, nullable=False, server_default='{}'),
    Column('enabled', Integer, nullable=False, server_default=text('1')),
    Column('created_at', Text, nullable=False))

# ---------------------------------------------------------------- indexes

Index('messages_session_order', messages.c.session_id, messages.c.id)
Index('runs_queue', runs.c.status, runs.c.input_message_id)
Index('run_steps_run_order', run_steps.c.run_id, run_steps.c.seq)
Index('feishu_events_message', feishu_events.c.app_id, feishu_events.c.tenant_id, feishu_events.c.message_id)
Index('feishu_replies_due', feishu_replies.c.status, feishu_replies.c.retry_at)
Index('channel_external_active', channel_identities.c.channel, channel_identities.c.app_id,
      channel_identities.c.tenant_id, channel_identities.c.external_user_id, unique=True,
      sqlite_where=text('active = 1'), postgresql_where=text('active = 1'))
Index('channel_user_active', channel_identities.c.user_id, channel_identities.c.channel, unique=True,
      sqlite_where=text('active = 1'), postgresql_where=text('active = 1'))