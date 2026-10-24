-- NYRR 9+1 Volunteer Monitor — initial schema
-- Target: PostgreSQL 15+ (Supabase). All timestamps are TIMESTAMPTZ and stored in UTC.
--
-- Everything lives in a dedicated schema that Supabase does NOT expose through its
-- auto-generated REST API (only `public` and `graphql_public` are exposed by default).
-- Row Level Security is also enabled as defence in depth.

BEGIN;

CREATE SCHEMA IF NOT EXISTS nyrr_monitor;
SET LOCAL search_path TO nyrr_monitor;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------- events
CREATE TABLE events (
    event_id        TEXT PRIMARY KEY,              -- "<year>/<slug>" from the event URL
    event_name      TEXT NOT NULL,
    event_date      DATE NOT NULL,                 -- first day, America/New_York
    location        TEXT,
    event_url       TEXT NOT NULL,
    -- extensions
    event_end_date  DATE,                          -- last day for multi-day events
    event_status    TEXT,                          -- calendar-level status slug (informational only)
    event_tags      TEXT[] NOT NULL DEFAULT '{}',
    last_seen_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------- shifts
CREATE TABLE shifts (
    shift_id            TEXT PRIMARY KEY,          -- data-registration-option-id
    event_id            TEXT NOT NULL REFERENCES events(event_id),
    role_name           TEXT NOT NULL,
    start_time          TIMESTAMPTZ,
    end_time            TIMESTAMPTZ,
    is_medical          BOOLEAN,
    plus_one_2026       BOOLEAN,
    availability_status TEXT NOT NULL,             -- raw shift status slug, e.g. "sold-out"
    classification      TEXT NOT NULL,
    registration_url    TEXT,
    source_url          TEXT NOT NULL,
    verified_at         TIMESTAMPTZ,
    -- extensions
    tags                TEXT[] NOT NULL DEFAULT '{}',
    is_restricted       BOOLEAN,
    reason_codes        TEXT[] NOT NULL DEFAULT '{}',
    state_version       INTEGER NOT NULL DEFAULT 0,      -- compare-and-swap guard
    alert_armed         BOOLEAN NOT NULL DEFAULT TRUE,   -- FALSE after an alert until a confirmed UNAVAILABLE
    last_observed_run_id UUID,
    last_alerted_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT shifts_classification_chk
        CHECK (classification IN ('AVAILABLE_VERIFIED', 'UNAVAILABLE', 'UNVERIFIED')),
    CONSTRAINT shifts_available_needs_url_chk
        CHECK (classification <> 'AVAILABLE_VERIFIED'
               OR (registration_url IS NOT NULL AND verified_at IS NOT NULL
                   AND is_medical = FALSE AND plus_one_2026 = TRUE))
);

-- ---------------------------------------------------------------- monitoring_runs
CREATE TABLE monitoring_runs (
    run_id              UUID PRIMARY KEY,
    started_at          TIMESTAMPTZ NOT NULL,
    completed_at        TIMESTAMPTZ,
    status              TEXT NOT NULL,
    events_checked      INTEGER NOT NULL DEFAULT 0,
    shifts_checked      INTEGER NOT NULL DEFAULT 0,
    verified_openings   INTEGER NOT NULL DEFAULT 0,
    unverified_shifts   INTEGER NOT NULL DEFAULT 0,
    error_count         INTEGER NOT NULL DEFAULT 0,
    error_summary       TEXT,
    -- extensions
    trigger             TEXT,                     -- schedule | workflow_dispatch | local
    scheduled_for       TIMESTAMPTZ,              -- the quarter-hour slot this run serves
    full_sweep          BOOLEAN NOT NULL DEFAULT FALSE,
    requests_made       INTEGER NOT NULL DEFAULT 0,
    source_failures     INTEGER NOT NULL DEFAULT 0,
    parser_errors       INTEGER NOT NULL DEFAULT 0,
    transitions_created INTEGER NOT NULL DEFAULT 0,
    notifications_created INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT monitoring_runs_status_chk CHECK (status IN (
        'RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED',
        'SKIPPED_LOCKED', 'NOT_AUTHORIZED', 'EXPIRED'))
);

-- ---------------------------------------------------------------- availability_transitions
CREATE TABLE availability_transitions (
    transition_id           UUID PRIMARY KEY,
    shift_id                TEXT NOT NULL REFERENCES shifts(shift_id),
    previous_classification TEXT,
    new_classification      TEXT NOT NULL,
    detected_at             TIMESTAMPTZ NOT NULL,
    run_id                  UUID NOT NULL REFERENCES monitoring_runs(run_id),
    -- extensions
    from_version            INTEGER NOT NULL,     -- shifts.state_version before the change
    reason_codes            TEXT[] NOT NULL DEFAULT '{}',
    alert_generated         BOOLEAN NOT NULL DEFAULT FALSE,
    alert_snapshot          JSONB,                -- frozen e-mail content; retries resend exactly this
    CONSTRAINT transitions_new_chk
        CHECK (new_classification IN ('AVAILABLE_VERIFIED', 'UNAVAILABLE', 'UNVERIFIED')),
    CONSTRAINT transitions_prev_chk
        CHECK (previous_classification IS NULL
               OR previous_classification IN ('AVAILABLE_VERIFIED', 'UNAVAILABLE', 'UNVERIFIED')),
    -- one transition per (shift, version): concurrent runs cannot record the same change twice
    CONSTRAINT transitions_shift_version_uq UNIQUE (shift_id, from_version)
);

-- ---------------------------------------------------------------- notifications (outbox)
CREATE TABLE notifications (
    notification_id     UUID PRIMARY KEY,
    transition_id       UUID NOT NULL REFERENCES availability_transitions(transition_id),
    recipient_email     TEXT NOT NULL,
    delivery_status     TEXT NOT NULL,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    last_attempt_at     TIMESTAMPTZ,
    provider_message_id TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- extensions
    next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    idempotency_key     TEXT NOT NULL,            -- sent as Resend Idempotency-Key
    accepted_at         TIMESTAMPTZ,
    last_error          TEXT,
    needs_review        BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (transition_id, recipient_email),
    CONSTRAINT notifications_idem_uq UNIQUE (idempotency_key),
    CONSTRAINT notifications_status_chk CHECK (delivery_status IN (
        'PENDING', 'SENDING', 'ACCEPTED', 'FAILED_RETRYABLE', 'FAILED_PERMANENT', 'SUPPRESSED'))
);

-- ---------------------------------------------------------------- operational tables
CREATE TABLE app_state (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Lease-based lock: works through any connection pooler (session or transaction mode)
-- and is released automatically when the lease expires if a process crashes.
CREATE TABLE locks (
    lock_name   TEXT PRIMARY KEY,
    holder      TEXT NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE health_incidents (
    incident_id   UUID PRIMARY KEY,
    incident_type TEXT NOT NULL,
    severity      TEXT NOT NULL,
    opened_at     TIMESTAMPTZ NOT NULL,
    last_seen_at  TIMESTAMPTZ NOT NULL,
    resolved_at   TIMESTAMPTZ,
    alert_sent_at TIMESTAMPTZ,
    details       TEXT,
    CONSTRAINT health_severity_chk CHECK (severity IN ('DEGRADED', 'CRITICAL', 'WARNING'))
);
-- at most one open incident per type -> one alert per unresolved incident
CREATE UNIQUE INDEX health_incidents_open_uq
    ON health_incidents (incident_type) WHERE resolved_at IS NULL;

-- ---------------------------------------------------------------- 60-minute email delivery test
CREATE TABLE email_test_runs (
    test_id        UUID PRIMARY KEY,
    activated_at   TIMESTAMPTZ NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL,
    completed_at   TIMESTAMPTZ,
    status         TEXT NOT NULL,
    authorized_by  TEXT NOT NULL,
    total_cycles   INTEGER NOT NULL DEFAULT 4,
    CONSTRAINT email_test_status_chk CHECK (status IN ('ACTIVE', 'COMPLETED', 'EXPIRED', 'ABORTED'))
);
CREATE UNIQUE INDEX email_test_one_active_uq ON email_test_runs ((TRUE)) WHERE status = 'ACTIVE';

CREATE TABLE email_test_cycles (
    cycle_id      UUID PRIMARY KEY,
    test_id       UUID NOT NULL REFERENCES email_test_runs(test_id),
    cycle_number  INTEGER NOT NULL CHECK (cycle_number BETWEEN 1 AND 4),
    expected_at   TIMESTAMPTZ NOT NULL,
    executed_at   TIMESTAMPTZ NOT NULL,
    trigger       TEXT NOT NULL,
    UNIQUE (test_id, cycle_number)
);

CREATE TABLE email_test_deliveries (
    delivery_id         UUID PRIMARY KEY,
    cycle_id            UUID NOT NULL REFERENCES email_test_cycles(cycle_id),
    recipient_email     TEXT NOT NULL,
    delivery_status     TEXT NOT NULL,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    requested_at        TIMESTAMPTZ,
    responded_at        TIMESTAMPTZ,
    provider_message_id TEXT,
    last_error          TEXT,
    UNIQUE (cycle_id, recipient_email),
    CONSTRAINT email_test_deliveries_status_chk CHECK (delivery_status IN (
        'PENDING', 'SENDING', 'ACCEPTED', 'FAILED_RETRYABLE', 'FAILED_PERMANENT'))
);

-- ---------------------------------------------------------------- indexes (spec §9)
CREATE INDEX events_event_date_idx           ON events (event_date);
CREATE INDEX shifts_event_id_idx             ON shifts (event_id);
CREATE INDEX shifts_classification_idx       ON shifts (classification);
CREATE INDEX shifts_verified_at_idx          ON shifts (verified_at);
CREATE INDEX monitoring_runs_started_at_idx  ON monitoring_runs (started_at);
CREATE INDEX notifications_delivery_status_idx ON notifications (delivery_status);
-- supporting indexes
CREATE INDEX notifications_due_idx ON notifications (next_attempt_at)
    WHERE delivery_status IN ('PENDING', 'FAILED_RETRYABLE');
CREATE INDEX transitions_shift_idx ON availability_transitions (shift_id, detected_at);

-- ---------------------------------------------------------------- defence in depth
ALTER TABLE events                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE shifts                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE monitoring_runs          ENABLE ROW LEVEL SECURITY;
ALTER TABLE availability_transitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications            ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_state                ENABLE ROW LEVEL SECURITY;
ALTER TABLE locks                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE health_incidents         ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_test_runs          ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_test_cycles        ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_test_deliveries    ENABLE ROW LEVEL SECURITY;

INSERT INTO schema_migrations (version) VALUES ('001_initial_schema');

COMMIT;
