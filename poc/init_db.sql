CREATE TABLE IF NOT EXISTS implants (
    implant_id  TEXT PRIMARY KEY,
    group_id    TEXT NOT NULL,
    first_seen  TIMESTAMPTZ NOT NULL,
    last_seen   TIMESTAMPTZ NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS telemetry (
    id           BIGSERIAL PRIMARY KEY,
    implant_id   TEXT NOT NULL,
    config_type  TEXT NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw          JSONB NOT NULL,
    features     JSONB NOT NULL,
    is_anomaly   BOOLEAN NOT NULL DEFAULT FALSE,
    injector_tag TEXT
);

CREATE INDEX IF NOT EXISTS idx_telemetry_received_at ON telemetry (received_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_ingested_at ON telemetry (ingested_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_implant_config ON telemetry (implant_id, config_type);

CREATE TABLE IF NOT EXISTS baselines (
    scope        TEXT NOT NULL,
    scope_id     TEXT NOT NULL,
    config_type  TEXT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL,
    stats        JSONB NOT NULL,
    PRIMARY KEY (scope, scope_id, config_type)
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id      UUID PRIMARY KEY,
    implant_id    TEXT NOT NULL,
    group_id      TEXT NOT NULL,
    config_type   TEXT NOT NULL,
    timestamp     TIMESTAMPTZ NOT NULL,
    window_start  TIMESTAMPTZ NOT NULL,
    window_end    TIMESTAMPTZ NOT NULL,
    severity      TEXT NOT NULL,
    baseline_used TEXT NOT NULL,
    details       JSONB NOT NULL,
    explanation   TEXT NOT NULL,
    label         TEXT,
    labeled_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_implant ON alerts (implant_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_alerts_pattern ON alerts ((details->>'pattern_hash'));

CREATE TABLE IF NOT EXISTS suppressed_patterns (
    pattern_hash  TEXT PRIMARY KEY,
    suppressed_at TIMESTAMPTZ NOT NULL,
    label_count   INT NOT NULL DEFAULT 3
);
