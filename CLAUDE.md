# Implant Anomaly Detection Platform — PoC

Autonomous anomaly detection platform for implant telemetry streams. Detects **operator mistakes** — misuse of capabilities, exposure risks, behavioral deviations — without hand-written rules. Uses statistical and ML-based baselines learned from historical data.

This is a **proof-of-concept** targeting a live demo. The architecture should resemble the final product in structure and data flow, but not in operational hardening, scalability, or test coverage. The goal is a working, presentable demo — not production code.

---

## Project Overview

- **Purpose:** Catch operator errors and operational risks across implants in near real-time
- **Users:** NOC operators who receive alerts, classify them, and use feedback to improve detection
- **Demo timing:** The complete demo (baseline + live + detection) must finish in under 3 minutes so it can be presented live to stakeholders
- **Alert latency:** Detector must keep up with incoming telemetry — no multi-minute backlogs
- **Detection approach:** Fully autonomous (no manual rules/signatures); baseline learning at per-implant and per-group levels
- **Scope:** Config snapshot telemetry only. Logs and non-config products are out of scope for the PoC.

---

## Demo Flow

The demo runs in two phases:

1. **Baseline phase (compressed time):** A dataset generator pre-populates the system with synthetic historical data representing normal implant behaviour across all groups. This data is ingested in bulk at maximum speed before the live phase starts. Per-implant baselines become active after `IMPLANT_BASELINE_MIN_DAYS` of data per implant; group baselines are used until then.

2. **Live phase:** The generator streams a new wave of telemetry — ~4% of events are anomalous (injected using `anomalies.py`) — as fast as the pipeline can process. The Streamlit UI shows alerts appearing in real time as the detector processes each batch.

There is no time throttling. Everything runs as fast as possible.

---

## Repository Layout

```
poc/
├── docker-compose.yml
├── config.py                  # all tunable constants in one place
├── CLAUDE.md
├── README.md                  # user-facing demo documentation
│
├── data/
│   ├── profiles.py            # config snapshot generators (provided)
│   ├── anomalies.py           # anomaly injectors (provided)
│   └── generator.py           # orchestrates bulk baseline + live stream generation
│
├── ingestion/
│   └── ingestor.py            # Kafka consumer → PostgreSQL + Redis writer
│
├── extraction/
│   └── extractor.py           # feature extraction per config type (provided, extend here)
│
├── detection/
│   ├── baseline.py            # IQR and Isolation Forest baseline management
│   └── detector.py            # runs detection against each incoming feature vector
│
├── alerts/
│   ├── engine.py              # deduplication, severity scoring, fatigue mitigation
│   └── models.py              # Alert dataclass and label enums
│
└── ui/
    └── dashboard.py           # Streamlit NOC dashboard
```

**Provided files** (`profiles.py`, `anomalies.py`, `extractor.py`) should be copied into the repo and used as-is. Do not refactor them unless there is a concrete reason.

---

## Tech Stack

### Infrastructure (docker-compose)

| Service | Role |
|---------|------|
| **PostgreSQL** | Persistent storage for raw telemetry, extracted features, baselines, alerts, and feedback |
| **Kafka + Zookeeper** | Message stream between generator and ingestor. Single broker. |
| **Redis** | Two roles: (1) sliding-window lists of raw feature vectors as training material for baselines; (2) cached computed baseline artefacts (IQR fences + serialised Isolation Forest models) so the detector doesn't retrain on every batch tick |

### Application

| Component | Technology |
|-----------|------------|
| All application code | Python |
| Detection models | `scikit-learn` (Isolation Forest + IQR) |
| UI | Streamlit |
| Kafka client | `confluent-kafka` |
| PostgreSQL client | `psycopg2` |
| Redis client | `redis-py` |

---

## Data Model

### Telemetry Envelope (all messages)

```json
{
  "metadata": {
    "implant_id": "0x1337",
    "timestamp": "30-03-2026 17:25:36.02"
  }
}
```

### Configuration Types (PoC scope)

| Config Type | Key Fields |
|-------------|------------|
| `dangerous_program_configuration` | `dangerous_programs[]`, `dangerous_drivers[]` — each entry has `name` and `action` |
| `communication_configuration` | `beacon_interval_ms`, `jitter_percentage`, `c2_channels[]`, `encryption{}` |
| `persistence_configuration` | `active_methods[]`, `registry_key_count`, `scheduled_task_count` |
| `capability_configuration` | `capabilities[]` with `enabled` flags, `max_concurrent_tasks` |
| `evasion_configuration` | boolean flags for each evasion technique |

### Configuration Snapshot Examples

**`dangerous_program_configuration`:**
```json
{
  "metadata": {
    "implant_id": 1337,
    "received_at": "25-03-2026 22:36:02",
    "type": "dangerous_program_configuration"
  },
  "configuration": {
    "dangerous_programs": [
      { "name": "wireshark.exe", "action": "audit" },
      { "name": "dumpcap.exe", "action": "self_destruct" },
      { "name": "CSFalconService.exe", "action": "self_destruct" }
    ],
    "dangerous_drivers": [
      { "name": "WdFilter.sys", "action": "do_nothing" },
      { "name": "csagent.sys", "action": "audit" },
      { "name": "csfalcondrv.sys", "action": "self_destruct" }
    ]
  }
}
```

### Organizational Hierarchy

Implants belong to **implant groups**. Groups are a first-class concept — they define the scope of shared behavioral baselines. Every detection and alert must be group-aware. Group names are arbitrary strings (e.g. ALPHA, BRAVO, CHARLIE in the synthetic data).

---

## Dataset Generation

### Implants and Groups

The generator creates a configurable number of implants spread across groups. Must be large enough for stable group baselines but small enough that the full demo finishes within the timing budget.

### Baseline Phase

- Generate enough days of synthetic snapshots per implant to exceed `IMPLANT_BASELINE_MIN_DAYS`
- Config type distribution follows weights in `profiles.py` (`_TYPE_WEIGHTS`)
- All events are clean (no anomalies injected)
- Publish to Kafka and ingest at maximum speed before starting the live phase
- After ingestion, compute and cache initial baselines in Redis

### Live Phase

- Stream new snapshots continuously at maximum speed
- **~4% of events** are anomalous — injected via `anomalies.py`. Controlled by `ANOMALY_RATE` in `config.py`
- Each anomalous event carries its injector description as ground truth so detection quality can be evaluated

---

## Feature Extraction

Feature extraction is implemented in `extractor.py` (provided). The `extract_features(config, config_type)` function returns a flat `dict[str, float]` for any config type. Use this as-is.

### Feature Catalogue (per config type)

**`dangerous_program_configuration`:**
`program_count`, `driver_count`, `total_entries`, `prog_audit_count`, `prog_self_destruct_count`, `prog_do_nothing_count`, `prog_self_destruct_ratio`, `drv_audit_count`, `drv_self_destruct_count`, `drv_do_nothing_count`, `drv_do_nothing_ratio`

**`communication_configuration`:**
`beacon_interval_ms`, `jitter_percentage`, `max_retries`, `sleep_on_failure_ms`, `c2_channel_count`, `c2_enabled_count`, `c2_unique_protocols`, `c2_non_standard_ports`, `encryption_enabled`, `key_rotation_hours`

**`persistence_configuration`:**
`active_method_count`, `registry_key_count`, `scheduled_task_count`, `watchdog_enabled`, `reinstall_on_removal`, `total_persistence_footprint`

**`capability_configuration`:**
`capability_count`, `enabled_count`, `enabled_ratio`, `max_concurrent_tasks`, `task_timeout_ms`, `active_surveillance_count`

**`evasion_configuration`:**
`obfuscate_strings`, `amsi_bypass_enabled`, `etw_patch_enabled`, `unhook_ntdll`, `sleep_obfuscation`, `stack_spoof`, `evasion_enabled_count`, `evasion_enabled_ratio`

---

## Detection

### Baseline Scoping and Cold Start

Every feature vector is scored against two baselines:

- **Per-implant baseline** — built from this implant's own history. Active after `IMPLANT_BASELINE_MIN_DAYS` of per-implant data.
- **Per-group baseline** — built from all implants in the group. Always active.

During the cold-start period for a given implant, only the group baseline is used. Once enough per-implant data exists, the per-implant result takes precedence. Alerts always indicate which baseline was used.

### Detection Models

Two models run in parallel on each feature vector.

**IQR detector (statistical, per-feature):**
- For each feature, compute Q1, Q3, and IQR from the baseline window
- Flag the feature if the observed value falls outside `[Q1 - k*IQR, Q3 + k*IQR]` where `k` is configurable via `IQR_MULTIPLIER` in `config.py`
- IQR is robust to outliers and makes no assumptions about the underlying distribution
- Produces per-feature deviation scores — the primary source of alert explainability

**Isolation Forest (multivariate):**
- Trained on the full feature vector for each (baseline scope, config type) combination
- Catches anomalies that are subtle across multiple features but not extreme on any single one
- One model instance per (scope, config type) pair, serialised and cached in Redis
- Retrained whenever the baseline window is refreshed

Both detectors must independently signal anomalous for a HIGH alert. Either alone produces MEDIUM or LOW (see Severity Scoring).

### Severity Scoring

| Level | Criteria |
|-------|----------|
| **HIGH** | Both detectors agree: IQR shows significant deviation (multiple features or extreme single-feature breach) AND Isolation Forest score exceeds the high threshold. |
| **MEDIUM** | Either detector alone: any IQR deviation, or a moderate IF score. |
| **LOW** | IF signal only, no IQR breach. |

All thresholds (`IQR_SIGNIFICANT_FEATURE_COUNT`, `IQR_SIGNIFICANT_MULTIPLIER`, `ISOLATION_FOREST_HIGH_THRESHOLD`, `ISOLATION_FOREST_MEDIUM_THRESHOLD`) are constants in `config.py`. Tune them to keep false positive noise low enough that the demo dashboard is readable.

### Detection Loop

The detector runs in a continuous loop. When there is a backlog of unscored telemetry, it processes ticks back-to-back. When caught up, it sleeps briefly (`DETECTION_IDLE_SLEEP_SECONDS`) before polling again. This keeps alert latency low during the demo.

---

## Alert Engine

### Alert and Supporting Types

```python
@dataclass
class FeatureDeviation:
    feature_name: str
    observed_value: float
    expected_median: float
    lower_fence: float
    upper_fence: float
    iqr_multiplier: float       # how many IQR units outside the fence

@dataclass
class Alert:
    alert_id: str               # UUID
    implant_id: str
    group_id: str
    config_type: str
    timestamp: datetime
    window_start: datetime
    window_end: datetime
    severity: Severity          # HIGH / MEDIUM / LOW
    baseline_used: str          # "implant" or "group"
    deviating_features: list[FeatureDeviation]
    isolation_forest_score: float
    explanation: str            # human-readable, auto-generated
    label: Label | None         # set by operator
```

### Explanation Generation

Auto-generate `explanation` from `deviating_features`. Format:

> "Implant 0x1337 (group ALPHA): `beacon_interval_ms` is 1200 (expected 28000–32000, median 30100). `jitter_percentage` is 0.0 (expected 0.10–0.22). Severity: HIGH."

### Fatigue Mitigation

- **Deduplication:** suppress duplicate alerts for the same (implant, config type, deviating feature set) within `DEDUP_WINDOW_MINUTES`
- **Rate limiting:** max `RATE_LIMIT_PER_WINDOW` alerts per implant per detection tick
- **False positive suppression:** if an alert pattern accumulates enough `false_positive` labels (threshold: `SUPPRESSION_LABEL_THRESHOLD`), suppress it and log the suppression

---

## Feedback Loop

### Operator Label Set

| Label | Value |
|-------|-------|
| True Positive | `true_positive` |
| False Positive | `false_positive` |
| Expected Deviation | `expected_deviation` |

### Feedback Behaviour (PoC)

- Labels are stored on the alert record in PostgreSQL
- `false_positive` labels count toward pattern suppression (threshold: `SUPPRESSION_LABEL_THRESHOLD`)
- No automatic threshold adjustment in the PoC — suppression is the only mechanical effect
- Policy questions (scope, decay, per-operator weighting) remain open for post-PoC experimentation

---

## Database Schema

**`implants`**
```sql
implant_id    TEXT PRIMARY KEY,
group_id      TEXT NOT NULL,
first_seen    TIMESTAMPTZ NOT NULL,
last_seen     TIMESTAMPTZ NOT NULL,
status        TEXT NOT NULL DEFAULT 'active'
```

**`telemetry`**
```sql
id            BIGSERIAL PRIMARY KEY,
implant_id    TEXT NOT NULL,
config_type   TEXT NOT NULL,
received_at   TIMESTAMPTZ NOT NULL,
raw           JSONB NOT NULL,
features      JSONB NOT NULL,
is_anomaly    BOOLEAN NOT NULL DEFAULT FALSE,
injector_tag  TEXT
```

**`baselines`**
```sql
scope         TEXT NOT NULL,    -- 'implant' or 'group'
scope_id      TEXT NOT NULL,    -- implant_id or group_id
config_type   TEXT NOT NULL,
updated_at    TIMESTAMPTZ NOT NULL,
stats         JSONB NOT NULL,   -- per-feature Q1/Q3/IQR/median
PRIMARY KEY (scope, scope_id, config_type)
```

**`alerts`**
```sql
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
```

**`suppressed_patterns`**
```sql
pattern_hash  TEXT PRIMARY KEY,
suppressed_at TIMESTAMPTZ NOT NULL,
label_count   INT NOT NULL DEFAULT 3
```

---

## NOC Dashboard (Streamlit)

### Layout

- **Left panel:** live alert feed, newest first. Each card shows implant ID, group, config type, severity badge, explanation, and three classification buttons (True Positive / False Positive / Expected Deviation). Cards collapse after labelling.
- **Right panel:** summary metrics — total alerts, label breakdown, false positive rate, detection rate (alerts where `is_anomaly=TRUE` that were flagged).
- **Bottom:** scrollable raw telemetry view for the selected alert, linked from the alert card.

### Behaviour

- Polls for new alerts every 5 seconds
- Alert cards animate in as new alerts arrive
- Labelling an alert immediately updates the card and writes to PostgreSQL
- Suppressed patterns shown in a collapsed section with a count

---

## Component Interfaces

### Generator → Kafka

Each Kafka message on the `telemetry` topic:
```json
{
  "metadata": { "implant_id": "...", "group_id": "...", "received_at": "...", "type": "..." },
  "configuration": { "..." : "..." },
  "ground_truth": { "is_anomaly": true, "injector_tag": "beacon_storm:30000->1200" }
}
```

### Kafka → Ingestor

The ingestor consumes from the `telemetry` topic, calls `extract_features()`, and writes to:
- PostgreSQL `telemetry` table (raw + features + ground truth)
- Redis: appends the serialised feature vector to the sliding window list for each applicable scope (implant and group), trimmed to `BASELINE_WINDOW_SIZE` entries

Redis key format for sliding window lists: `window:{scope}:{scope_id}:{config_type}`

Example: `window:group:ALPHA:communication_configuration`

Each list entry is a JSON-serialised `dict[str, float]` — exactly what `extract_features()` returns. No timestamps or metadata; the key carries all context. The list is ordered oldest (index 0) → newest (index -1). `rpush` appends to the right; `ltrim(-BASELINE_WINDOW_SIZE, -1)` drops the oldest entry from the left on every push.

### Ingestor → Detector (via Redis)

Redis holds two distinct artefact types per (scope, config type) combination:

**Sliding window list** — raw feature vectors, maintained by the ingestor. Training material.
```
window:group:ALPHA:communication_configuration  →  List[JSON feature vector]
window:implant:0x1337:communication_configuration  →  List[JSON feature vector]
```

**Computed baseline** — IQR fences and serialised Isolation Forest model, maintained by the detector. Used for scoring.
```
model:group:ALPHA:communication_configuration  →  Hash {
    "iqr_fences":            JSON  (per-feature Q1, Q3, IQR, median, lower fence, upper fence),
    "isolation_forest":      Pickle (trained sklearn IsolationForest),
    "trained_at":            ISO timestamp,
    "training_sample_size":  int
}
```

The detector runs in a continuous loop and does two things per tick:

1. **Retrain** (if the window list has grown by more than `RETRAIN_THRESHOLD` new entries since `trained_at`): read the full window list, recompute IQR fences, refit Isolation Forest, write back to the model hash.
2. **Score**: read the model hash, score unprocessed feature vectors from PostgreSQL against the cached fences and model, pass results to the alert engine.

### Detector → Alert Engine

```python
@dataclass
class ScoredEvent:
    telemetry_row: dict
    deviating_features: list[FeatureDeviation]
    isolation_forest_score: float
    baseline_used: str          # "implant" or "group"
```

### Alert Engine → Dashboard

Dashboard reads directly from PostgreSQL `alerts` on each poll. No additional interface.

---

## Coding Conventions

- **Language:** Python throughout
- **Function length:** short and single-purpose. If a function needs a comment to explain what it does, consider splitting or renaming it.
- **Naming:** no abbreviations or shortcuts. `program` not `prog`, `feature_name` not `feat`. Names should read like prose.
- **Comments:** only for non-obvious decisions (e.g. why `k=2.5`). Do not comment what the code does; write code that says what it does.
- **Configuration:** all tunable constants live in `config.py` at the repo root with descriptive names and a one-line comment explaining what each controls.
- **Error handling:** fail loudly. Raise exceptions rather than swallowing errors. Log the exception and enough context to reproduce it.

---

## Open Questions (Post-PoC)

- [ ] **Feedback policy** — scope (implant vs. group vs. global), decay window, minimum labels before suppression
- [ ] **Seasonality modeling** — time-of-day and day-of-week patterns in implant behaviour
- [ ] **Drift detection** — when do baselines go stale and need forced retraining?
- [ ] **Multi-model strategy** — specialized models per config type vs. unified model
- [ ] **Access control** — role separation between NOC operators and model administrators
- [ ] **Scale** — migrate from PostgreSQL to ClickHouse, single Kafka broker to cluster

---

## Success Criteria (PoC)

1. Baseline phase completes and baselines are populated before the live phase starts
2. Anomalous events in the live phase trigger alerts at a meaningful detection rate
3. False positive rate on clean events is low enough that the demo is not noisy
4. Operator labelling in the UI writes back to the database and suppression works
5. A non-technical observer watching the dashboard understands what is happening