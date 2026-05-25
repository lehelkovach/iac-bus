-- ACP v2 Postgres Schema (Draft)
-- Purpose: durable agent identity + message routing history for IAC-Bus.
-- Notes:
-- - Append-only intent for bus_messages content.
-- - Designed for incremental adoption from current in-memory behavior.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- Agents and endpoints
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agents (
    agent_uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    brand TEXT NOT NULL,
    repo_locale TEXT NOT NULL,
    ordinal_path TEXT NOT NULL CHECK (ordinal_path ~ '^([0-9]+)(-[0-9]+)*$'),
    logical_handle TEXT NOT NULL UNIQUE,
    parent_agent_uuid UUID REFERENCES agents(agent_uuid) ON DELETE SET NULL,
    role TEXT NOT NULL DEFAULT 'worker',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'idle', 'blocked', 'terminated')),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agents_parent ON agents(parent_agent_uuid);
CREATE INDEX IF NOT EXISTS idx_agents_brand_repo ON agents(brand, repo_locale);

CREATE TABLE IF NOT EXISTS agent_endpoints (
    endpoint_uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_uuid UUID NOT NULL REFERENCES agents(agent_uuid) ON DELETE CASCADE,
    medium TEXT NOT NULL CHECK (medium IN ('web', 'slack', 'ide', 'api', 'automation', 'other')),
    endpoint_handle TEXT NOT NULL UNIQUE,
    session_id TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_seen_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (agent_uuid, medium, session_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_endpoints_agent ON agent_endpoints(agent_uuid);
CREATE INDEX IF NOT EXISTS idx_agent_endpoints_medium_active ON agent_endpoints(medium, is_active);

-- ---------------------------------------------------------------------------
-- Message ledger and route history
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bus_messages (
    message_id BIGSERIAL PRIMARY KEY,
    message_uuid UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    channel TEXT NOT NULL,
    agent_uuid UUID NOT NULL REFERENCES agents(agent_uuid),
    endpoint_uuid UUID REFERENCES agent_endpoints(endpoint_uuid) ON DELETE SET NULL,
    type TEXT NOT NULL CHECK (
        type IN (
            'info',
            'progress',
            'request',
            'response',
            'blocker',
            'handoff',
            'done',
            'decision',
            'heartbeat',
            'error'
        )
    ),
    message TEXT NOT NULL,
    ref TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlation_id UUID,
    parent_message_uuid UUID REFERENCES bus_messages(message_uuid) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bus_messages_channel_id ON bus_messages(channel, message_id);
CREATE INDEX IF NOT EXISTS idx_bus_messages_agent_time ON bus_messages(agent_uuid, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bus_messages_corr ON bus_messages(correlation_id);
CREATE INDEX IF NOT EXISTS idx_bus_messages_type_time ON bus_messages(type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bus_messages_metadata_gin ON bus_messages USING GIN (metadata);

CREATE TABLE IF NOT EXISTS bus_routes (
    route_id BIGSERIAL PRIMARY KEY,
    message_id BIGINT NOT NULL REFERENCES bus_messages(message_id) ON DELETE CASCADE,
    route_kind TEXT NOT NULL CHECK (route_kind IN ('channel', 'agent', 'queue', 'webhook', 'slack', 'other')),
    route_target TEXT NOT NULL,
    route_status TEXT NOT NULL CHECK (route_status IN ('queued', 'delivered', 'filtered', 'failed', 'skipped')),
    error_text TEXT,
    delivered_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bus_routes_message ON bus_routes(message_id);
CREATE INDEX IF NOT EXISTS idx_bus_routes_target_status ON bus_routes(route_target, route_status);

-- ---------------------------------------------------------------------------
-- Consumer progress for polling workers
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS channel_offsets (
    consumer_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    last_message_id BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer_id, channel)
);

-- ---------------------------------------------------------------------------
-- Task coordination state (minimal IPC/delegation model)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS task_assignments (
    task_uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel TEXT NOT NULL,
    title TEXT NOT NULL,
    assigned_by_agent_uuid UUID NOT NULL REFERENCES agents(agent_uuid),
    owner_agent_uuid UUID NOT NULL REFERENCES agents(agent_uuid),
    parent_task_uuid UUID REFERENCES task_assignments(task_uuid) ON DELETE SET NULL,
    wait_for_task_uuid UUID REFERENCES task_assignments(task_uuid) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (
        status IN ('assigned', 'accepted', 'running', 'blocked', 'handoff', 'done', 'failed', 'cancelled')
    ),
    blocker_reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_task_assignments_owner_status ON task_assignments(owner_agent_uuid, status);
CREATE INDEX IF NOT EXISTS idx_task_assignments_wait_for ON task_assignments(wait_for_task_uuid);

-- ---------------------------------------------------------------------------
-- Resource locking for codebase critical sections
-- ---------------------------------------------------------------------------

CREATE SEQUENCE IF NOT EXISTS lock_fencing_token_seq;

CREATE TABLE IF NOT EXISTS resource_locks (
    lock_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_key TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL CHECK (mode IN ('exclusive', 'shared')),
    holder_agent_uuid UUID NOT NULL REFERENCES agents(agent_uuid),
    task_uuid UUID REFERENCES task_assignments(task_uuid) ON DELETE SET NULL,
    fencing_token BIGINT NOT NULL DEFAULT nextval('lock_fencing_token_seq'),
    lease_expires_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_resource_locks_holder ON resource_locks(holder_agent_uuid);
CREATE INDEX IF NOT EXISTS idx_resource_locks_lease ON resource_locks(lease_expires_at);

