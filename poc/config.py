from types import MappingProxyType

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
IQR_MULTIPLIER: float = 2.0

# IQR multiplier threshold for "significant" deviation (used in HIGH severity check)
IQR_SIGNIFICANT_MULTIPLIER: float = 3.0

# Minimum number of IQR-deviating features for "significant" deviation
IQR_SIGNIFICANT_FEATURE_COUNT: int = 2

# Isolation Forest anomaly score threshold for HIGH severity
ISOLATION_FOREST_HIGH_THRESHOLD: float = 0.95

# Isolation Forest anomaly score threshold for MEDIUM severity
ISOLATION_FOREST_MEDIUM_THRESHOLD: float = 0.90

# Isolation Forest: number of trees in the ensemble
ISOLATION_FOREST_ESTIMATORS: int = 500

# Isolation Forest: expected proportion of anomalies in training data
ISOLATION_FOREST_CONTAMINATION: float | str = 0.02

# Isolation Forest: random seed for reproducible training
ISOLATION_FOREST_RANDOM_STATE: int = 42

# Isolation Forest: fraction of features to draw per tree (lower = more diverse ensemble)
ISOLATION_FOREST_MAX_FEATURES: float = 0.8

# Local Outlier Factor: number of neighbors for density estimation
LOF_N_NEIGHBORS: int = 20

# LOF: expected proportion of anomalies in training data (controls predict() threshold)
LOF_CONTAMINATION: float = 0.02

# LOF anomaly score threshold (percentile-based, like IF)
LOF_HIGH_THRESHOLD: float = 0.90

# Mahalanobis p-value thresholds (lower = more anomalous)
MAHALANOBIS_P_VALUE_HIGH: float = 0.001   # 0.1% chance under normal distribution
MAHALANOBIS_P_VALUE_MEDIUM: float = 0.01  # 1% chance under normal distribution

# Minimum feature variance to include a feature in IF training (drops constant/near-constant features)
FEATURE_VARIANCE_THRESHOLD: float = 0.001

# Per-config-type IQR multiplier overrides (types not listed use IQR_MULTIPLIER)
CONFIG_TYPE_IQR_OVERRIDES: MappingProxyType[str, float] = MappingProxyType({})

# Maximum number of SHAP feature contributions included in alert explanations
SHAP_TOP_N_FEATURES: int = 5

# Config types excluded from anomaly detection entirely
DETECTION_EXCLUDED_CONFIG_TYPES: tuple[str, ...] = ()

# Sentinel value for IQR deviation when IQR is zero (all baseline values identical)
ZERO_IQR_DEVIATION_SENTINEL: float = 2.0

# Minimum IQR deviation (in IQR units) to count as a "hard" deviation
MINIMUM_IQR_DEVIATION: float = 0.1

# Epsilon band around constant features — any value outside median ± epsilon is flagged
ZERO_IQR_EPSILON: float = 0.01


# ---------------------------------------------------------------------------
# Baseline management
# ---------------------------------------------------------------------------

# Minimum days of per-implant data before the implant baseline supersedes the group baseline
IMPLANT_BASELINE_MIN_DAYS: int = 7

# Maximum number of feature vectors kept per sliding window in Redis (used for ltrim)
BASELINE_WINDOW_SIZE: int = 1000

# Effective training window size per scope — vectors are sliced before training
BASELINE_WINDOW_SIZE_GROUP: int = 1000
BASELINE_WINDOW_SIZE_IMPLANT: int = 300

# Minimum feature vectors required before a model can be trained
MINIMUM_TRAINING_SAMPLES: int = 10

# New entries added to a window since last training that trigger a baseline retrain
RETRAIN_THRESHOLD: int = 100


# ---------------------------------------------------------------------------
# Detection engine
# ---------------------------------------------------------------------------

# Seconds the detector sleeps when no backlog remains
DETECTION_IDLE_SLEEP_SECONDS: int = 3

# Maximum telemetry rows scored per tick
MAX_ROWS_PER_DETECTION_TICK: int = 5000


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
INGESTOR_WAIT_TIMEOUT_SECONDS: int = 300


# ---------------------------------------------------------------------------
# Ingestor
# ---------------------------------------------------------------------------

# Number of Kafka messages the ingestor batches before committing to PostgreSQL and Redis
INGESTOR_BATCH_SIZE: int = 500

# Kafka consumer poll timeout in seconds — how long to wait for a message before checking batch
KAFKA_POLL_TIMEOUT_SECONDS: float = 0.05

# Number of processed messages between progress log lines
INGESTOR_LOG_INTERVAL_MESSAGES: int = 2000
