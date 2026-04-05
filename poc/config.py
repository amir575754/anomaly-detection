# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

# PostgreSQL connection string
DATABASE_DSN: str = "postgresql://anomaly:anomaly@localhost:5432/anomaly"

# Kafka broker address
KAFKA_BOOTSTRAP: str = "localhost:9092"

# Kafka topic that carries all telemetry messages
KAFKA_TOPIC: str = "telemetry"

# Redis hostname
REDIS_HOST: str = "localhost"

# Redis port
REDIS_PORT: int = 6379


# ---------------------------------------------------------------------------
# Detection models
# ---------------------------------------------------------------------------

# IQR fence multiplier — how many IQR units outside Q1/Q3 before a feature is flagged
IQR_MULTIPLIER: float = 2.5

# IQR multiplier threshold for "significant" deviation (used in HIGH severity check)
IQR_SIGNIFICANT_MULTIPLIER: float = 5.0

# Minimum number of IQR-deviating features for "significant" deviation
IQR_SIGNIFICANT_FEATURE_COUNT: int = 2

# Isolation Forest anomaly score threshold for HIGH severity
ISOLATION_FOREST_HIGH_THRESHOLD: float = 0.7

# Isolation Forest anomaly score threshold for MEDIUM severity
ISOLATION_FOREST_MEDIUM_THRESHOLD: float = 0.5

# Isolation Forest: number of trees in the ensemble
ISOLATION_FOREST_ESTIMATORS: int = 50

# Isolation Forest: expected proportion of anomalies in training data ("auto" for novelty detection)
ISOLATION_FOREST_CONTAMINATION: str = "auto"

# Isolation Forest: random seed for reproducible training
ISOLATION_FOREST_RANDOM_STATE: int = 42

# Minimum feature variance to include a feature in IF training (drops constant/near-constant features)
FEATURE_VARIANCE_THRESHOLD: float = 0.01

# Per-config-type IQR multiplier overrides (types not listed use IQR_MULTIPLIER)
CONFIG_TYPE_IQR_OVERRIDES: dict[str, float] = {
    "dangerous_program_configuration": 1.5,
}

# Sentinel value for IQR deviation when IQR is zero (all baseline values identical)
ZERO_IQR_DEVIATION_SENTINEL: float = 999.0


# ---------------------------------------------------------------------------
# Baseline management
# ---------------------------------------------------------------------------

# Minimum days of per-implant data before the implant baseline supersedes the group baseline
IMPLANT_BASELINE_MIN_DAYS: int = 7

# Seconds in one day — used to convert timedelta to days
SECONDS_PER_DAY: int = 86400

# Maximum number of feature vectors kept per sliding window in Redis (used for ltrim)
BASELINE_WINDOW_SIZE: int = 500

# Effective training window size per scope — vectors are sliced before training
BASELINE_WINDOW_SIZE_GROUP: int = 500
BASELINE_WINDOW_SIZE_IMPLANT: int = 200

# Minimum feature vectors required before a model can be trained
MINIMUM_TRAINING_SAMPLES: int = 10

# New entries added to a window since last training that trigger a baseline retrain
RETRAIN_THRESHOLD: int = 100


# ---------------------------------------------------------------------------
# Detection engine
# ---------------------------------------------------------------------------

# Config types excluded from anomaly detection
DETECTION_EXCLUDED_CONFIG_TYPES: list[str] = []

# Nominal detection batch window in minutes (used for alert window labeling)
DETECTION_WINDOW_MINUTES: int = 5

# Seconds the detector sleeps when no backlog remains
DETECTION_IDLE_SLEEP_SECONDS: int = 3

# Maximum telemetry rows scored per tick
MAX_ROWS_PER_DETECTION_TICK: int = 5000


# ---------------------------------------------------------------------------
# Alert engine
# ---------------------------------------------------------------------------

# Window within which identical alert patterns are considered duplicates and suppressed
DEDUP_WINDOW_MINUTES: int = 15

# Maximum alerts raised per implant within one detection window
RATE_LIMIT_PER_WINDOW: int = 3

# Number of false_positive labels on the same pattern before it is permanently suppressed
SUPPRESSION_LABEL_THRESHOLD: int = 3

# Length of the truncated SHA-256 hash used to identify alert patterns
PATTERN_HASH_LENGTH: int = 16

# Hours after which a suppressed pattern expires and can trigger alerts again (0 = never expire)
SUPPRESSION_DECAY_HOURS: int = 24


# ---------------------------------------------------------------------------
# Data generation
# ---------------------------------------------------------------------------

# Fraction of live-phase events that will have an anomaly injected
ANOMALY_RATE: float = 0.04

# Groups and the number of implants in each
IMPLANT_GROUPS: dict[str, int] = {"ALPHA": 5, "BRAVO": 5, "CHARLIE": 5}

# Number of days of clean synthetic history generated in the baseline phase
BASELINE_DAYS: int = 10

# Synthetic snapshots generated per implant per day during baseline phase
SNAPSHOTS_PER_IMPLANT_PER_DAY: int = 24

# Maximum events per second during the live phase (0 = unlimited)
LIVE_PHASE_MAX_EVENTS_PER_SECOND: int = 0

# Maximum total events to generate during the live phase (0 = unlimited)
LIVE_PHASE_MAX_EVENTS: int = 10_000

# Fraction of expected baseline events before the generator considers ingestion complete
INGESTOR_COMPLETION_THRESHOLD: float = 0.99

# Seconds between ingestor progress checks during wait_for_ingestor
INGESTOR_POLL_INTERVAL_SECONDS: int = 1

# Seconds between progress log lines during wait_for_ingestor
INGESTOR_LOG_INTERVAL_SECONDS: int = 3

# Maximum seconds to wait for the ingestor to finish baseline ingestion
INGESTOR_WAIT_TIMEOUT_SECONDS: int = 120


# ---------------------------------------------------------------------------
# Ingestor
# ---------------------------------------------------------------------------

# Number of Kafka messages the ingestor batches before committing to PostgreSQL and Redis
INGESTOR_BATCH_SIZE: int = 500

# Kafka consumer poll timeout in seconds — how long to wait for a message before checking batch
KAFKA_POLL_TIMEOUT_SECONDS: float = 0.5

# Number of processed messages between progress log lines
INGESTOR_LOG_INTERVAL_MESSAGES: int = 2000


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

# Maximum number of alerts fetched per dashboard refresh
DASHBOARD_ALERT_LIMIT: int = 200

# Seconds between dashboard auto-refreshes
DASHBOARD_REFRESH_INTERVAL_SECONDS: int = 5
