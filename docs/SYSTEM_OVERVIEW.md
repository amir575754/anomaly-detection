# Implant Anomaly Detection Platform — System Overview

> **Audience:** Engineering managers, technical leads, and stakeholders who need a comprehensive understanding of the system's architecture, data flow, design decisions, tradeoffs, and validated capabilities.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Architecture Overview](#3-architecture-overview)
4. [End-to-End Data Flow](#4-end-to-end-data-flow)
5. [Infrastructure Stack](#5-infrastructure-stack)
6. [Data Model and Schema](#6-data-model-and-schema)
7. [Feature Engineering](#7-feature-engineering)
8. [Detection Engine — How Anomalies Are Found](#8-detection-engine--how-anomalies-are-found)
9. [Baseline Management and Cold Start](#9-baseline-management-and-cold-start)
10. [Severity Classification and Alert Design](#10-severity-classification-and-alert-design)
11. [Synthetic Data and Evaluation Methodology](#11-synthetic-data-and-evaluation-methodology)
12. [Validated Performance](#12-validated-performance)
13. [Design Decisions and Their Tradeoffs](#13-design-decisions-and-their-tradeoffs)
14. [What This PoC Proves](#14-what-this-poc-proves)
15. [Known Limitations and Gaps](#15-known-limitations-and-gaps)
16. [Path to Production](#16-path-to-production)
17. [Appendix A: Configuration Reference](#appendix-a-configuration-reference)
18. [Appendix B: Repository Layout](#appendix-b-repository-layout)

---

## 1. Executive Summary

This proof-of-concept demonstrates an **autonomous anomaly detection platform** for implant telemetry. It detects operator mistakes — misconfigurations, forgotten teardowns, wrong templates, policy contradictions — **without hand-written rules or prior knowledge of specific failure modes.** The system learns what "normal" looks like from historical data, then flags deviations in real time.

### Key Results

| Metric | Achieved | Target |
|---|---|---|
| Detection rate (true anomalies caught) | **82.1%** | >70% |
| False positive rate (false alerts / total alerts) | **28.9%** | <40% |
| Precision (true alerts / total alerts) | **71.1%** | — |
| Per-injector detection (9 of 10 types) | **80–100%** | — |

### What the Demo Shows

Running `python run.py` launches a tmux session with three panes:

1. **Ingestor** — consumes telemetry from Kafka, writes to PostgreSQL + Redis
2. **Generator** — produces synthetic baseline data, then streams live events with ~4% anomaly injection
3. **Detector** — scores every event in real time, prints color-coded alert panels with feature-level explanations

The entire demo — from baseline generation through model training to live anomaly detection — runs from a single command in under 3 minutes.

---

## 2. Problem Statement

### The Operational Risk

Implants are configured through templates and manual adjustments. Operators make mistakes:
- Applying a template from the wrong campaign
- Forgetting to disable surveillance mode after an operation
- Panic-loading a blanket self-destruct policy
- Loading contradictory response policies for programs vs. drivers
- Enabling advanced evasion techniques without their prerequisites

These mistakes create **exposure risks** — detectable beacon patterns, inconsistent security postures, unnecessary persistence footprints. Catching them quickly is operationally critical.

### Why Rules Don't Scale

Traditional approaches use hand-written rules: "alert if beacon_interval < 5000." This fails because:
- The rule set grows linearly with the number of known failure modes
- New failure modes require new rules — there's always a blind spot
- Rules can't account for per-group or per-implant behavioral differences
- Maintaining a rule set across hundreds of implant types is unsustainable

### Our Approach

Learn what "normal" looks like for each implant group and individual implant, then flag anything that deviates statistically. The system has **zero hard-coded rules about what anomalies look like** — it discovers them autonomously from the data.

---

## 3. Architecture Overview

```
                    ┌──────────────┐
                    │  Generator   │
                    │ (synthetic   │
                    │   data +     │
                    │  anomaly     │
                    │  injection)  │
                    └──────┬───────┘
                           │ Kafka messages
                           ▼
                    ┌──────────────┐
                    │    Kafka     │
                    │   (topic:    │
                    │  telemetry)  │
                    └──────┬───────┘
                           │
                           ▼
                    ┌──────────────┐         ┌──────────────┐
                    │   Ingestor   │────────▶│  PostgreSQL   │
                    │  (consumer + │         │ (telemetry,   │
                    │   parser +   │         │  implants,    │
                    │   writer)    │         │  baselines)   │
                    └──────┬───────┘         └──────▲───────┘
                           │                        │
                           │ feature vectors        │ unscored rows
                           ▼                        │
                    ┌──────────────┐         ┌──────┴───────┐
                    │    Redis     │◀────────│   Detector    │
                    │ (sliding     │         │  (scoring     │
                    │  windows +   │────────▶│   pipeline +  │
                    │  cached      │ models  │   CLI output) │
                    │  models)     │         └──────────────┘
                    └──────────────┘
```

### Component Responsibilities

| Component | Responsibility | Runs As |
|---|---|---|
| **Generator** | Produces synthetic telemetry (baseline + live with injection) | Python process |
| **Ingestor** | Kafka consumer → feature extraction → PostgreSQL + Redis writes | Python process |
| **Detector** | Baseline training, real-time scoring, alert output | Python process |
| **PostgreSQL** | Persistent storage for raw telemetry, features, baselines | Docker container |
| **Kafka** | Message bus between generator and ingestor | Docker container |
| **Redis** | Two roles: sliding window storage for training data + cached model artefacts | Docker container |

---

## 4. End-to-End Data Flow

### Phase 1: Baseline (Compressed Historical Data)

```
Generator                  Kafka          Ingestor              Redis              PostgreSQL
   │                         │               │                    │                     │
   │  10 days × 24           │               │                    │                     │
   │  snapshots/implant/day  │               │                    │                     │
   │  × 15 implants          │               │                    │                     │
   │  = 3,600 events         │               │                    │                     │
   │────publish──────────────▶│               │                    │                     │
   │                         │──consume──────▶│                    │                     │
   │                         │               │──extract features──▶│ rpush to windows    │
   │                         │               │                    │                     │
   │                         │               │──────────────────────────insert rows──────▶│
   │                         │               │                    │                     │
   │  (wait for ingestor)    │               │                    │                     │
   │                         │               │                    │                     │
   │  bootstrap baselines ───────────────────────────────────────▶│ train IF+LOF+IQR    │
   │                         │               │                    │ per (scope, config)  │
   │  save cursor ───────────────────────────────────────────────▶│ detector:baseline_   │
   │                         │               │                    │ cursor = max(id)     │
```

**Key detail:** The baseline phase generates **clean data only** — no anomalies. This ensures the models learn what "normal" looks like without contamination. The ingestor writes each feature vector to **two** Redis sliding windows: one per-implant and one per-group. After ingestion, the generator bootstraps baselines (IQR fences + Isolation Forest + LOF + Mahalanobis) for every (scope, config_type) combination.

### Phase 2: Live Detection

```
Generator                  Kafka          Ingestor          Detector                  CLI
   │                         │               │                 │                       │
   │  ~4% anomalous events   │               │                 │                       │
   │────publish──────────────▶│               │                 │                       │
   │                         │──consume──────▶│                 │                       │
   │                         │               │──write to PG+Redis──▶                   │
   │                         │               │                 │                       │
   │                         │               │                 │──fetch unscored rows──▶│
   │                         │               │                 │  from PostgreSQL       │
   │                         │               │                 │                       │
   │                         │               │                 │──load cached models────│
   │                         │               │                 │  from Redis            │
   │                         │               │                 │                       │
   │                         │               │                 │──score each event:     │
   │                         │               │                 │  IQR fences            │
   │                         │               │                 │  IF predict()          │
   │                         │               │                 │  LOF predict()         │
   │                         │               │                 │  Mahalanobis p-value   │
   │                         │               │                 │  SHAP explanation      │
   │                         │               │                 │                       │
   │                         │               │                 │──determine severity────▶│
   │                         │               │                 │  print alert panel     │
   │                         │               │                 │                       │
   │                         │               │                 │──retrain if stale──────│
   │                         │               │                 │  (100+ new entries)    │
```

**Key detail:** The detector runs in a continuous loop. On each tick, it:
1. Checks if any baselines need retraining (based on write counters)
2. Fetches unscored telemetry rows from PostgreSQL (cursor-based, never misses events)
3. Resolves the appropriate baseline scope for each event (per-implant vs per-group)
4. Loads cached models from Redis
5. Scores each event with all four detectors
6. Applies severity classification
7. Prints alert panels for detected anomalies
8. Prints a summary table with detection rates, precision, and per-injector breakdowns

---

## 5. Infrastructure Stack

| Service | Technology | Role | Why This Choice |
|---|---|---|---|
| **Message Bus** | Apache Kafka (Confluent 7.6) | Decouples generation from ingestion | Handles backpressure, supports replay, industry standard for streaming telemetry |
| **Persistent Storage** | PostgreSQL 16 | Raw telemetry, features, baselines | Mature, JSONB support for flexible schemas, strong indexing |
| **Model Cache** | Redis 7 | Sliding windows + serialized models | Sub-millisecond reads, native list operations for windows, pipeline batching |
| **ML Models** | scikit-learn (IF, LOF, StandardScaler) | Training and scoring | Gold standard for classical ML, well-tested, serializable |
| **Explainability** | SHAP (TreeExplainer) | Per-feature attribution | Exact Shapley values for tree models, no approximation error |
| **Statistics** | NumPy, SciPy | IQR fences, Mahalanobis distance | Standard numerical stack, optimized C implementations |
| **CLI Output** | Rich | Color-coded panels, tables, progress | Produces polished terminal output suitable for live demos |
| **Orchestration** | tmux | Multi-pane demo session | Lightweight, no external dependencies, works over SSH |

### Docker Compose

All infrastructure services are defined in `docker-compose.yml` with health checks. The application code runs locally (not containerized) to simplify development and debugging.

---

## 6. Data Model and Schema

### PostgreSQL Tables

**`implants`** — Registry of known implants.
```sql
implant_id  TEXT PRIMARY KEY        -- e.g., "implant_ALPHA_003"
group_id    TEXT NOT NULL            -- e.g., "ALPHA"
first_seen  TIMESTAMPTZ NOT NULL    -- earliest telemetry from this implant
last_seen   TIMESTAMPTZ NOT NULL    -- most recent telemetry
status      TEXT DEFAULT 'active'   -- lifecycle tracking
```

**`telemetry`** — Every configuration snapshot received.
```sql
id           BIGSERIAL PRIMARY KEY  -- monotonic, used as detection cursor
implant_id   TEXT NOT NULL
config_type  TEXT NOT NULL           -- e.g., "communication_configuration"
received_at  TIMESTAMPTZ NOT NULL    -- when the implant sent it
ingested_at  TIMESTAMPTZ DEFAULT NOW()
raw          JSONB NOT NULL          -- full original message
features     JSONB NOT NULL          -- extracted feature vector
is_anomaly   BOOLEAN DEFAULT FALSE   -- ground truth (from generator)
injector_tag TEXT                     -- which injector created it (ground truth)
```

**`baselines`** — Persisted IQR fence statistics.
```sql
scope        TEXT NOT NULL           -- "implant" or "group"
scope_id     TEXT NOT NULL           -- implant_id or group_id
config_type  TEXT NOT NULL
updated_at   TIMESTAMPTZ NOT NULL
stats        JSONB NOT NULL          -- per-feature Q1, Q3, IQR, median, fences
PRIMARY KEY (scope, scope_id, config_type)
```

### Redis Key Structure

**Sliding windows** (maintained by ingestor):
```
window:group:ALPHA:communication_configuration      → List[JSON feature vector]
window:implant:implant_ALPHA_003:evasion_config...  → List[JSON feature vector]
```

**Cached models** (maintained by detector):
```
model:group:ALPHA:communication_configuration       → Hash {
    iqr_fences:           JSON,
    isolation_forest:     serialized TrainedModel,
    trained_at:           ISO timestamp,
    training_sample_size: int,
    trained_at_writes:    int
}
```

**Write counters** (maintained by ingestor, read by detector):
```
writes:group:ALPHA:communication_configuration      → int (monotonic counter)
```

**Detector cursor**:
```
detector:baseline_cursor                            → int (max telemetry.id at baseline end)
```

---

## 7. Feature Engineering

### Philosophy

Features capture **operational semantics** — the meaning of a configuration in terms of posture, consistency, resource allocation, and dependency coherence. They do NOT encode knowledge of specific anomaly patterns. This ensures the system detects novel mistakes, not just the ones we anticipated.

### Feature Catalogue

#### Communication Configuration (12 features)

| Feature | Type | Captures |
|---|---|---|
| `beacon_interval_ms` | Raw | Primary timing parameter |
| `jitter_percentage` | Raw | Timing randomization |
| `max_retries` | Raw | Failure resilience |
| `sleep_on_failure_ms` | Raw | Failure backoff duration |
| `key_rotation_hours` | Raw | Cryptographic freshness |
| `c2_channel_count` | Count | Infrastructure breadth |
| `c2_enabled_count` | Count | Active infrastructure |
| `c2_unique_protocols` | Count | Protocol diversity |
| `c2_non_standard_ports` | Count | Network stealth |
| `beacon_to_sleep_ratio` | Interaction | Beacon vs recovery timing correlation |
| `beacon_to_rotation_ratio` | Interaction | Beacon vs crypto rotation correlation |
| `jitter_retries_product` | Interaction | Randomization x resilience relationship |

**Correlated generation:** The synthetic generator ties jitter, retries, sleep, and key rotation to a normalized beacon interval. This creates a manifold of valid configurations. Anomalies that draw features independently (like `wrong_comm_profile`) land off this manifold.

#### Dangerous Program Configuration (15 features)

| Feature | Type | Captures |
|---|---|---|
| `program_count`, `driver_count`, `total_entries` | Count | Configuration size |
| `prog_audit_count`, `prog_self_destruct_count`, `prog_do_nothing_count` | Count | Per-category action distribution (programs) |
| `drv_audit_count`, `drv_self_destruct_count`, `drv_do_nothing_count` | Count | Per-category action distribution (drivers) |
| `overall_audit_ratio`, `overall_self_destruct_ratio`, `overall_do_nothing_ratio` | Ratio | Global action balance |
| `posture_consistency` | Derived | L1 similarity between program and driver action distributions |
| `action_diversity` | Count | Number of distinct actions present |
| `dominant_action_ratio` | Ratio | Fraction of the most common action |

#### Persistence Configuration (7 features)

| Feature | Type | Captures |
|---|---|---|
| `active_method_count` | Count | Persistence depth |
| `registry_key_count`, `scheduled_task_count` | Count | Per-mechanism footprint |
| `watchdog_enabled`, `reinstall_on_removal` | Binary | Safety net presence |
| `total_persistence_footprint` | Sum | Overall persistence weight |
| `safety_to_depth_ratio` | Ratio | Safety infrastructure per method |

**Correlated generation:** Method count drives registry/task counts and safety net probabilities via depth-dependent lookup tables. `duplicated_persistence_setup` violates this by pairing depth-1 methods with depth-3 safety infrastructure.

#### Capability Configuration (9 features)

| Feature | Type | Captures |
|---|---|---|
| `enabled_count`, `enabled_ratio` | Count/Ratio | How much capability is active |
| `max_concurrent_tasks`, `task_timeout_ms` | Raw | Resource allocation |
| `active_surveillance_count`, `non_surveillance_enabled_count` | Count | Capability type breakdown |
| `resource_per_capability` | Ratio | Resources per enabled capability |
| `surveillance_ratio` | Ratio | Surveillance proportion of total |
| `concurrency_timeout_product` | Interaction | Resource x timeout relationship |

**Two-cluster structure:** The generator creates surveillance posture (high surveillance, high concurrency, low timeout) and passive posture (low surveillance, low concurrency, high timeout). `forgotten_operation_teardown` mixes them.

#### Evasion Configuration (11 features)

| Feature | Type | Captures |
|---|---|---|
| `evasion_enabled_count`, `evasion_enabled_ratio` | Count/Ratio | How many techniques are active |
| `evasion_layer_depth` | Derived | Highest dependency layer with active techniques |
| `dependency_coherence` | Derived | Fraction of techniques whose prerequisites are met |
| `depth_per_enabled` | Ratio | Layer depth per active technique |
| `obfuscate_strings` through `stack_spoof` | Binary | Individual technique states |

**Layered dependency chain:** The generator maintains a dependency structure (layer 1, 2, 3). Higher-layer techniques are rarely enabled without lower-layer prerequisites. `full_evasion` breaks this chain.

---

## 8. Detection Engine — How Anomalies Are Found

### Four-Detector Ensemble

Each telemetry event is scored by four independent algorithms:

| Detector | Type | Catches | FP Characteristics |
|---|---|---|---|
| **IQR Fences** | Statistical, per-feature | Single-feature outliers, extreme deviations | Low FP (data-driven fences), interpretable |
| **Isolation Forest** | Tree-based, multivariate | Complex multi-feature patterns, correlation violations | Moderate FP without corroboration, calibrated via contamination |
| **Local Outlier Factor** | Density-based, multivariate | Local manifold deviations, cluster-edge anomalies | Moderate FP without corroboration |
| **Mahalanobis Distance** | Parametric, covariance-aware | Deviations from multivariate normal center | Low FP (chi-squared p-values), but assumes normality |

### The Corroboration Requirement

**The single most important design decision for FP control.** We require at least two detector families to independently flag an event before issuing an alert. This exploits the fact that:
- True anomalies produce signals across multiple independent algorithms
- False positives are typically noise from a single algorithm
- Requiring agreement cuts the effective FP rate from ~5-10% (single detector) to <1% (joint agreement)

### How predict() Reduces False Positives

A critical innovation: instead of using continuous anomaly scores with thresholds (where 10% of clean data scores above the 90th percentile by definition), we use sklearn's `predict()` method which produces a binary yes/no decision calibrated to the contamination rate.

With `contamination=0.02`:
- IF's `predict()` flags ~2% of training data as anomalous
- LOF's `predict()` flags ~2% of training data as anomalous
- The probability of BOTH independently flagging the same clean data point is approximately 0.04% (assuming independence)

This gives us a structural FP guarantee: **requiring both ML models to agree limits ML-driven false positives to under 0.1% of clean events.**

### Scoring Pipeline (Per Event)

```python
# 1. Statistical: per-feature fence check
hard_deviations, soft_count = score_with_iqr(features, fences)

# 2. ML: calibrated hard decisions
if_predicts_anomaly = IF.predict(scaled_vector) == -1
lof_predicts_anomaly = LOF.predict(scaled_vector) == -1

# 3. Parametric: covariance-aware distance
mahalanobis_p_value = chi2_pvalue(mahal_distance_squared, df=n_features)

# 4. Explainability: which features drove the IF score
shap_contributions = TreeExplainer.shap_values(scaled_vector)

# 5. Severity classification via corroboration voting
severity = determine_severity(deviations, if_pred, lof_pred, ...)
```

---

## 9. Baseline Management and Cold Start

### Two Baseline Scopes

| Scope | Source | When Used |
|---|---|---|
| **Group** | All implants in the group | Always available (cold-start fallback) |
| **Implant** | This specific implant's history | After 7+ days of per-implant data |

The per-implant baseline captures individual behavioral patterns. The group baseline provides a safety net when per-implant data is insufficient.

### Sliding Windows

Training data is maintained as **sliding windows** in Redis — capped lists of the most recent feature vectors:
- Group windows: up to 1000 vectors
- Implant windows: up to 300 vectors

New vectors are appended on every ingestion. Old vectors are trimmed automatically.

### Automatic Retraining

The detector monitors a **write counter** per window. When 100+ new vectors have arrived since the last training, it retrains:
1. Reads the full sliding window from Redis
2. Recomputes IQR fences
3. Refits Isolation Forest (500 trees)
4. Refits LOF (20 neighbors)
5. Recomputes Mahalanobis parameters (mean + inverse covariance)
6. Caches everything back to Redis

This means baselines adapt to gradual behavioral drift without manual intervention.

---

## 10. Severity Classification and Alert Design

### Alert Panel Format (CLI)

```
+-- HIGH implant_ALPHA_003 (ALPHA) communication_configuration --+
| IQR Deviations:                                                 |
|   beacon_interval_ms    1200    (expected 15000-45000, median   |
|                                  30100)                         |
|   jitter_percentage     0.00    (expected 0.10-0.25, median     |
|                                  0.18)                          |
| SHAP Contributions:                                             |
|   beacon_interval_ms   +0.928                                   |
|   jitter_percentage    +0.340                                   |
| IF: 0.987  LOF: 0.943  Mahal p: 2.1e-08                       |
+-- TP: beacon_storm:30000->1200 --------------------------------+
```

Each panel shows:
- **Severity** (color-coded: red=HIGH, yellow=MEDIUM)
- **Implant ID, group, and config type**
- **IQR deviations:** Which features are outside fences, with expected range and median
- **SHAP contributions:** Which features drove the ML score (positive = toward anomalous)
- **All detector scores:** IF percentile, LOF percentile, Mahalanobis p-value
- **Ground truth** (green subtitle if this was an injected anomaly)

### Summary Table

After each scoring batch, a summary table shows:
- **Per-injector detection breakdown** with color-coded rates (green at or above 70%, yellow at or above 40%, red below 40%)
- **Severity distribution** with visual bar charts
- **Overall performance:** detection rate, precision, FP rate

---

## 11. Synthetic Data and Evaluation Methodology

### Data Generation

The PoC uses synthetic data with controlled anomaly injection:

**Baseline phase (clean):**
- 15 implants across 3 groups (ALPHA, BRAVO, CHARLIE)
- 10 days of history at 24 snapshots/implant/day
- 5 configuration types with weighted sampling
- All data is clean — no anomalies

**Live phase (with injection):**
- 10,000 events streamed at maximum speed
- ~4% of events have anomalies injected (controlled by `ANOMALY_RATE`)
- Each anomalous event is tagged with ground truth (injector name + parameters)

### Anomaly Injectors

10 injectors organized by detection difficulty:

| Injector | Config Type | Mechanism | Difficulty |
|---|---|---|---|
| `beacon_storm` | Communication | Beacon interval 30000 to 1200ms | Easy (extreme IQR) |
| `zero_jitter` | Communication | Jitter set to 0 | Easy (extreme IQR) |
| `persistence_spike` | Persistence | Registry 50-200, tasks 20-50 | Easy (extreme IQR) |
| `capability_explosion` | Capability | All caps enabled + high concurrency | Easy (multi-feature IQR) |
| `wrong_comm_profile` | Communication | All features independently drawn | Medium (correlation violation) |
| `duplicated_persistence_setup` | Persistence | Depth-1 methods + depth-3 safety | Medium (correlation violation) |
| `mismatched_escalation_policy` | Dangerous Program | Programs=audit, drivers=do_nothing + extra drivers | Medium (posture violation) |
| `self_destruct_flood` | Dangerous Program | All actions to self_destruct + extra programs | Medium (ratio saturation) |
| `forgotten_operation_teardown` | Capability | All caps enabled + concurrency=1, timeout=200s | Medium (extreme mismatch) |
| `full_evasion` | Evasion | Top-layer techniques without prerequisites | Hard (dependency chain violation) |

### Evaluation Metrics

- **Detection rate** (recall) = true positives / total injected anomalies
- **FP rate** = false positives / total alerts
- **Precision** = true positives / total alerts

---

## 12. Validated Performance

### Overall Results

| Metric | Value |
|---|---|
| **Detection rate** | 82.1% |
| **False positive rate** | 28.9% |
| **Precision** | 71.1% |
| Events scored | 5,000 |
| Anomalies injected | ~201 |
| True positives | 165 |
| False positives | 67 |

### Per-Injector Breakdown

| Injector | Injected | Detected | Rate |
|---|---|---|---|
| beacon_storm | 24 | 24 | **100%** |
| zero_jitter | 15 | 15 | **100%** |
| capability_explosion | 22 | 22 | **100%** |
| persistence_spike | 21 | 21 | **100%** |
| duplicated_persistence_setup | 14 | 14 | **100%** |
| forgotten_operation_teardown | 16 | 16 | **100%** |
| wrong_comm_profile | 13 | 13 | **100%** |
| mismatched_escalation_policy | 22 | 19 | **86%** |
| self_destruct_flood | 26 | 21 | **81%** |
| full_evasion | 28 | 0 | **0%** |

### Interpretation

- **9 of 10 anomaly types** are detected at 81-100%
- **`full_evasion` (0%)** represents a fundamental limitation of unsupervised detection on binary features — see Section 15
- Excluding `full_evasion`, detection rate is **95.4%** (165/173)

---

## 13. Design Decisions and Their Tradeoffs

### Decision 1: Unsupervised Over Supervised

**Chosen:** Learn normal behavior, flag deviations.
**Alternative:** Train a classifier on labeled anomaly examples.

| | Unsupervised | Supervised |
|---|---|---|
| Detects novel anomalies | Yes | Only if similar to training examples |
| Requires labeled data | No | Yes (expensive to obtain) |
| Precision on known types | Lower | Higher |
| Maintenance burden | Baselines adapt automatically | Requires relabeling when patterns change |

**Why:** Operator mistakes are diverse and unpredictable. New failure modes appear regularly. The system must generalize beyond known examples.

### Decision 2: Four-Detector Ensemble Over Single Model

**Chosen:** IQR + IF + LOF + Mahalanobis with corroboration voting.
**Alternative:** Single powerful model (e.g., deep autoencoder).

| | Ensemble | Single Model |
|---|---|---|
| Interpretability | High (per-detector explanations) | Low (latent space is opaque) |
| FP control | Structural (corroboration requirement) | Threshold-dependent |
| Different anomaly types | Each detector has strengths | Single model may have blind spots |
| Complexity | Higher (4 models to train/cache) | Lower |
| Training time | ~2s per baseline (acceptable) | Could be much longer for deep models |

**Why:** The corroboration requirement provides a *structural* guarantee against false positives — it doesn't depend on getting thresholds exactly right. Each detector also provides a different type of explanation.

### Decision 3: predict() Over Percentile Scoring for ML

**Chosen:** Use sklearn's `predict()` (calibrated to contamination rate) for severity decisions.
**Alternative:** Use percentile-based continuous scores with thresholds.

| | predict() | Percentile scoring |
|---|---|---|
| FP control | Calibrated to contamination rate | X% of clean data always scores above (100-X)% by construction |
| Sensitivity tuning | Via contamination parameter | Via threshold (fragile) |
| Score meaning | Binary: anomalous or not | Continuous: relative ranking |

**Why:** Percentile scoring has a structural FP floor — with threshold at P90, 10% of clean data triggers by definition. predict() with contamination=0.02 produces a threshold calibrated to flag ~2% of training-like data, giving a much lower effective FP rate.

### Decision 4: Group + Implant Baselines Over Global

**Chosen:** Two-level hierarchy (per-group, per-implant).
**Alternative:** Single global baseline for all implants.

| | Two-Level | Global |
|---|---|---|
| Sensitivity | Higher (captures group/individual norms) | Lower (diluted by cross-group variation) |
| Cold-start handling | Group baseline as fallback | Always available |
| Model count | ~(groups + implants) x config_types | config_types |
| Storage | More (separate windows per scope) | Less |

**Why:** Different groups may have fundamentally different configurations. A global baseline would have wide fences that miss group-specific anomalies. The two-level hierarchy provides sensitivity without requiring full per-implant history.

### Decision 5: Redis for Model Caching Over PostgreSQL

**Chosen:** Serialize trained models to Redis hashes.
**Alternative:** Store models in PostgreSQL as bytea columns.

| | Redis | PostgreSQL |
|---|---|---|
| Read latency | Sub-millisecond | ~1-5ms per query |
| Atomic hash operations | Native | Requires row-level transactions |
| Sliding window operations | Native list with ltrim | Requires window function queries |
| Persistence | Configurable (not needed for PoC) | Always persisted |

**Why:** The detector reads models on every tick. Sub-millisecond access matters for keeping detection latency low. Redis also naturally supports the sliding-window pattern with rpush/ltrim.

### Decision 6: P1/P99 Fence Clamping

**Chosen:** Clamp IQR fences at the 1st and 99th percentiles of training data.
**Alternative:** Allow fences to extend beyond the data range.

**Why:** For features with narrow distributions (like integer counts), the IQR-based fence can extend into impossible ranges (negative counts). Clamping at P1/P99 keeps fences within the observed data range, which is tighter and more sensitive to deviations at the distribution edges.

**Tradeoff:** For bounded features (ratios in [0,1]), P99 may equal 1.0, meaning the upper fence can't catch values at 1.0 even if they're rare. This is why we complement IQR with ML detectors.

---

## 14. What This PoC Proves

### 1. Autonomous Detection Is Viable

The system detects 9 out of 10 anomaly types at 81-100% without any hand-written rules. It learned what "normal" looks like from 10 days of synthetic history and immediately began flagging deviations.

### 2. False Positive Rate Is Controllable

At 28.9% FP rate (71.1% precision), the alert stream is readable. The corroboration requirement between detector families is the key mechanism — it provides structural FP control independent of threshold tuning.

### 3. Multi-Algorithm Ensemble Provides Robustness

No single algorithm achieves acceptable performance alone:
- IQR alone: catches single-feature anomalies but misses all correlation violations
- IF alone: catches complex patterns but has ~5-10% FP rate on clean data
- LOF alone: similar to IF but with different sensitivity characteristics
- Ensemble with corroboration: catches both types with under 2% clean data FP rate

### 4. Feature Engineering Matters More Than Algorithm Choice

The biggest detection improvements came from better features (interaction ratios, action diversity, depth_per_enabled) — not from algorithm tuning. This validates the principle that domain-aware feature extraction is the most impactful investment.

### 5. Explainability Is Achievable

Every alert includes human-readable explanations: which features deviated from IQR fences, which features drove the ML score (via SHAP), and the exact expected ranges. This makes the system auditable and trustworthy.

### 6. The Architecture Scales Conceptually

The component separation (generator to Kafka to ingestor to PostgreSQL/Redis to detector) mirrors a production architecture. Replacing synthetic data with real telemetry requires changes only in the generator/ingestor layer — the detection engine is data-source agnostic.

---

## 15. Known Limitations and Gaps

### Detection Gaps

1. **Binary feature combinations:** Anomalies that only change boolean flags (like enabling all evasion techniques) are hard to detect because each individual flag value appears in normal data. Isolation Forest and LOF struggle with high-dimensional binary spaces. The `full_evasion` injector achieves 0% detection for this reason.

2. **Subtle correlation violations:** When anomaly feature values overlap heavily with normal data distributions (individual features within range, only the combination is unusual), detection requires very high sample counts (above 1000 per config type) for reliable ML discrimination.

3. **Stochastic variation:** Detection rates vary slightly across runs due to random seed differences in data generation and model training. The reported 82.1% is representative but not exact.

### Operational Gaps

4. **No alert deduplication:** If the same misconfiguration persists across multiple snapshots, each snapshot generates a separate alert. A production system needs temporal deduplication and alert grouping.

5. **No feedback loop:** Operators cannot label alerts as true/false positives. This feedback is essential for refining thresholds and retraining.

6. **No dashboard:** Detection output is CLI-only. A production system needs a web-based triage interface.

7. **No seasonality modeling:** Implant behavior may vary by time-of-day or day-of-week. The current baselines don't account for temporal patterns.

### Scale Gaps

8. **Single-broker Kafka:** The PoC uses a single Kafka broker. Production requires a cluster.

9. **PostgreSQL for telemetry:** At high event volumes, PostgreSQL may become a bottleneck. ClickHouse or TimescaleDB would be more appropriate for time-series telemetry.

10. **Model serialization:** Model caching currently uses Python-native serialization. For cross-service communication, a language-agnostic format (e.g., ONNX) would be more appropriate.

---

## 16. Path to Production

### Phase 1: Replace Synthetic Data (Weeks 1-4)

- Connect the ingestor to real implant telemetry streams
- Validate that the feature extractor handles real configuration schemas
- Run in shadow mode (detect but don't alert) to measure FP rate on real data

### Phase 2: Feedback and Tuning (Weeks 5-8)

- Add operator feedback mechanism (label alerts as TP/FP)
- Use feedback to tune contamination, IQR multiplier, and severity thresholds per config type
- Implement alert deduplication and temporal grouping

### Phase 3: Scale and Harden (Weeks 9-12)

- Migrate to Kafka cluster with partitioned topics
- Evaluate ClickHouse for telemetry storage at scale
- Add monitoring/alerting for the detection system itself (meta-monitoring)
- Implement model versioning and rollback

### Phase 4: Dashboard and Integration (Weeks 13-16)

- Build web-based NOC dashboard for alert triage
- Integrate with existing incident management workflows
- Add seasonality modeling and drift detection
- Implement per-operator alert preferences and fatigue mitigation

---

## Appendix A: Configuration Reference

All tunable parameters are centralized in `config.py`:

### Detection Models

| Parameter | Value | Description |
|---|---|---|
| `IQR_MULTIPLIER` | 2.0 | Fence width in IQR units |
| `IQR_SIGNIFICANT_MULTIPLIER` | 3.0 | Deviation threshold for "significant" |
| `IQR_SIGNIFICANT_FEATURE_COUNT` | 2 | Feature count threshold for "significant" |
| `MINIMUM_IQR_DEVIATION` | 0.1 | Minimum deviation to count as "hard" |
| `ISOLATION_FOREST_ESTIMATORS` | 500 | Number of IF trees |
| `ISOLATION_FOREST_CONTAMINATION` | 0.02 | Expected anomaly fraction in training |
| `ISOLATION_FOREST_MAX_FEATURES` | 0.8 | Feature subsampling per tree |
| `LOF_N_NEIGHBORS` | 20 | LOF density estimation neighbors |
| `SHAP_TOP_N_FEATURES` | 5 | Max SHAP features shown per alert |

### Baseline Management

| Parameter | Value | Description |
|---|---|---|
| `IMPLANT_BASELINE_MIN_DAYS` | 7 | Days before per-implant baseline activates |
| `BASELINE_WINDOW_SIZE` | 1000 | Max vectors in Redis sliding window |
| `BASELINE_WINDOW_SIZE_GROUP` | 1000 | Effective group training window |
| `BASELINE_WINDOW_SIZE_IMPLANT` | 300 | Effective implant training window |
| `MINIMUM_TRAINING_SAMPLES` | 10 | Min vectors to train a model |
| `RETRAIN_THRESHOLD` | 100 | New vectors before retraining triggers |

### Data Generation

| Parameter | Value | Description |
|---|---|---|
| `ANOMALY_RATE` | 0.04 | Fraction of live events with injected anomalies |
| `IMPLANT_GROUPS` | 3 groups x 5 implants | Organizational structure |
| `BASELINE_DAYS` | 10 | Days of synthetic history |
| `SNAPSHOTS_PER_IMPLANT_PER_DAY` | 24 | Telemetry frequency |
| `LIVE_PHASE_MAX_EVENTS` | 10,000 | Total live events before stop |

---

## Appendix B: Repository Layout

```
poc/
├── docker-compose.yml              # Infrastructure services
├── init_db.sql                     # PostgreSQL schema initialization
├── config.py                       # All tunable constants
├── run.py                          # Single-command demo launcher (tmux)
├── reset.py                        # Full state reset
├── validate_detection.py           # Offline detection accuracy measurement
│
├── data/
│   ├── profiles.py                 # Correlated synthetic snapshot generators
│   ├── anomalies.py                # 10 anomaly injectors with ground truth
│   ├── generator.py                # Two-phase orchestrator
│   ├── baseline_phase.py           # Baseline data generation + bootstrap
│   ├── live_phase.py               # Live streaming with injection
│   └── kafka_utils.py              # Kafka publish helper
│
├── ingestion/
│   ├── ingestor.py                 # Kafka consumer → batch writer
│   ├── parser.py                   # Message parsing + feature extraction
│   └── writers.py                  # PostgreSQL + Redis bulk writers
│
├── extraction/
│   └── extractor.py                # Feature extraction per config type
│
└── detection/
    ├── models.py                   # Domain types (ScoredEvent, Severity, etc.)
    ├── trained_model.py            # TrainedModel container (IF + LOF + Mahalanobis)
    ├── fences.py                   # IQR fence computation with MAD fallback
    ├── training.py                 # IF + LOF + Mahalanobis fitting
    ├── scoring.py                  # All scoring functions + severity classification
    ├── scoring_pipeline.py         # Batch scoring orchestration
    ├── baseline.py                 # Baseline refresh orchestration
    ├── scoping.py                  # Per-implant vs per-group scope resolution
    ├── model_cache.py              # Redis model serialization/deserialization
    ├── queries.py                  # PostgreSQL + Redis query helpers
    ├── keys.py                     # Redis key format parsing
    ├── detector.py                 # Detection loop entry point
    ├── output.py                   # Rich CLI alert panel formatting
    └── summary.py                  # Detection statistics and summary tables
```
