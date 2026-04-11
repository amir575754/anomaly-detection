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
| Detection rate (true anomalies caught) | **90.0%** | >70% |
| False positive rate (false alerts / total alerts) | **24.2%** | <40% |
| Precision (true alerts / total alerts) | **75.8%** | — |
| Per-injector detection (10 of 10 types) | **60–100%** | — |

> Numbers measured via `poc/validate_detection.py` (`random.seed(42)`, 5000 events, 209 injected anomalies). Rerun the script to reproduce.

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

| Feature | Type | Description |
|---|---|---|
| `beacon_interval_ms` | Raw | Milliseconds between implant beacons; higher = stealthier |
| `jitter_percentage` | Raw | Randomization on beacon timing (0.0 = predictable, 1.0 = fully random) |
| `max_retries` | Raw | Connection retry count before sleeping on failure |
| `sleep_on_failure_ms` | Raw | Backoff duration in ms after retries exhausted |
| `key_rotation_hours` | Raw | Encryption key rotation frequency in hours |
| `c2_channel_count` | Count | Total C2 channels configured (active + inactive) |
| `c2_enabled_count` | Count | Currently active C2 channels |
| `c2_unique_protocols` | Count | Distinct protocols across channels (https, dns, smb, etc.) |
| `c2_non_standard_ports` | Count | Channels on ports other than 80/443/53 — more likely flagged by network monitoring |
| `beacon_to_sleep_ratio` | Interaction | `beacon_interval_ms / sleep_on_failure_ms` — stable in baseline because both are driven by the beacon parameter |
| `beacon_to_rotation_ratio` | Interaction | `beacon_interval_ms / key_rotation_hours` — ties beacon timing to crypto freshness |
| `jitter_retries_product` | Interaction | `jitter_percentage * max_retries` — captures the randomization-resilience interaction |

**Correlated generation (synthetic):** The generator ties jitter, retries, sleep, and key rotation to a normalized beacon interval using hand-picked linear relationships. This creates a manifold of valid configurations that the ML models learn. Whether these correlations exist in real telemetry is unverified — they should be checked during the shadow-mode evaluation phase.

#### Dangerous Program Configuration (15 features)

| Feature | Type | Description |
|---|---|---|
| `program_count` | Count | Number of dangerous programs in the watchlist |
| `driver_count` | Count | Number of dangerous drivers in the watchlist |
| `total_entries` | Count | `program_count + driver_count` — overall watchlist size |
| `prog_audit_count` | Count | Programs set to "audit" (monitor only) |
| `prog_self_destruct_count` | Count | Programs set to "self_destruct" (wipe implant if detected) |
| `prog_do_nothing_count` | Count | Programs set to "do_nothing" (ignore if detected) |
| `drv_audit_count` | Count | Drivers set to "audit" |
| `drv_self_destruct_count` | Count | Drivers set to "self_destruct" |
| `drv_do_nothing_count` | Count | Drivers set to "do_nothing" |
| `overall_audit_ratio` | Ratio | Fraction of all entries set to "audit" |
| `overall_self_destruct_ratio` | Ratio | Fraction of all entries set to "self_destruct" |
| `overall_do_nothing_ratio` | Ratio | Fraction of all entries set to "do_nothing" |
| `posture_consistency` | Derived | L1 similarity between program and driver action distributions (1.0 = identical, 0.0 = opposite) |
| `action_diversity` | Count | Distinct actions present (1 = all same action, 3 = all three used) |
| `dominant_action_ratio` | Ratio | Fraction of entries assigned to the most common action |

#### Persistence Configuration (11 features)

Operational implants use a single persistence method. Features capture the method type (one-hot) and its supporting infrastructure.

| Feature | Type | Description |
|---|---|---|
| `registry_key_count` | Count | Registry keys used; method-type dependent in baseline |
| `scheduled_task_count` | Count | Scheduled tasks created; method-type dependent in baseline |
| `watchdog_enabled` | Binary | 1.0 if a watchdog monitors persistence health |
| `reinstall_on_removal` | Binary | 1.0 if auto-reinstall on removal |
| `total_persistence_footprint` | Sum | `registry_keys + scheduled_tasks` — overall infrastructure weight |
| `safety_net_count` | Sum | `watchdog + reinstall` — count of active safety mechanisms (0–2) |
| `method_registry_run` | Binary | 1.0 if using registry_run persistence |
| `method_scheduled_task` | Binary | 1.0 if using scheduled_task persistence |
| `method_service_install` | Binary | 1.0 if using service_install persistence |
| `method_startup_folder` | Binary | 1.0 if using startup_folder persistence |
| `method_wmi_subscription` | Binary | 1.0 if using wmi_subscription persistence |

**Correlated generation (synthetic):** Each method type has hand-picked per-method infrastructure ranges (e.g., `registry_run`: 2–4 keys, 0 tasks, 30% watchdog; `scheduled_task`: 0–1 keys, 1–3 tasks, 25% watchdog). `duplicated_persistence_setup` violates this by using a `registry_run` method with scheduled_task infrastructure. These per-method mappings are authorial assumptions.

#### Capability Configuration (9 features)

| Feature | Type | Description |
|---|---|---|
| `enabled_count` | Count | Number of enabled capabilities (out of 8 total) |
| `enabled_ratio` | Ratio | `enabled_count / 8` — fraction of total capabilities active |
| `max_concurrent_tasks` | Raw | Maximum simultaneous capability executions — higher = more aggressive posture |
| `task_timeout_ms` | Raw | Milliseconds before a task is killed — short = aggressive, long = passive |
| `active_surveillance_count` | Count | Enabled surveillance capabilities (keylogging, screenshot) |
| `non_surveillance_enabled_count` | Count | `enabled_count - surveillance_count` — non-surveillance capabilities |
| `resource_per_capability` | Ratio | `max_concurrent_tasks / enabled_count` — execution slots per capability |
| `surveillance_ratio` | Ratio | `surveillance_count / enabled_count` — what fraction of activity is surveillance |
| `concurrency_timeout_product` | Interaction | `max_tasks * timeout_ms / 1000` — total resource commitment; stays in a narrow band because high concurrency pairs with short timeouts and vice versa |

**Two-cluster structure (synthetic):** The generator splits configs into surveillance posture (35%, hand-picked) and passive posture (65%) with different resource profiles. This two-cluster assumption gives IF/LOF a learnable structure. Real capability configurations may have more nuanced or continuous posture variations.

#### Evasion Configuration (11 features)

| Feature | Type | Description |
|---|---|---|
| `evasion_enabled_count` | Count | Number of evasion techniques enabled (out of 6) |
| `evasion_enabled_ratio` | Ratio | `enabled_count / 6` — fraction of techniques active |
| `evasion_layer_depth` | Derived | Highest active behavioral layer (0–3); layer 3 = advanced techniques |
| `dependency_coherence` | Derived | Fraction of enabled techniques whose behavioral prerequisites are also enabled (1.0 = normal) |
| `depth_per_enabled` | Ratio | `layer_depth / enabled_count` — exceeds 1.0 only when advanced techniques are on without foundations |
| `obfuscate_strings` | Binary | Layer 1 — string obfuscation; 55% baseline rate |
| `amsi_bypass_enabled` | Binary | Layer 2 — AMSI bypass; 65% if obfuscate is on, 8% if off |
| `etw_patch_enabled` | Binary | Layer 2 — ETW patching; 50% independent rate |
| `unhook_ntdll` | Binary | Layer 2 — ntdll unhooking; 65% if etw is on, 12% if off |
| `sleep_obfuscation` | Binary | Layer 3 — sleep hiding; 65% if 2+ layer-2 on, 8% otherwise |
| `stack_spoof` | Binary | Layer 3 — call stack spoofing; 55% if sleep_obfuscation on, 5% otherwise; rarest technique (~15%) |

**Behavioral dependency chain (synthetic):** The "dependencies" are not hard technical requirements — they are conditional probabilities (hand-picked, e.g., 65% / 8%) in the data generator that model how operators *might* tend to enable techniques in layers. Whether real operators follow this layered pattern is unverified. The `dependency_coherence` feature measures whether a configuration follows these expected behavioral patterns. The `full_evasion` anomaly enables advanced techniques while skipping the basics, producing low coherence values that almost never occur in the synthetic baseline data.

---

## 8. Detection Engine — How Anomalies Are Found

### Detection Ensemble

Each telemetry event is scored by four voting detectors:

| Detector | Type | Catches | FP Characteristics |
|---|---|---|---|
| **IQR Fences** | Statistical, per-feature | Single-feature outliers, extreme deviations | Low FP (data-driven fences), interpretable |
| **Isolation Forest** | Tree-based, multivariate | Complex multi-feature patterns, correlation violations | Moderate FP without corroboration, calibrated via contamination |
| **Local Outlier Factor** | Density-based, multivariate | Local manifold deviations, cluster-edge anomalies | Moderate FP without corroboration |
| **Mahalanobis Distance** | Parametric, covariance-aware | Deviations from multivariate center (p < 0.01 = one vote) | Principled p-values; despite normality assumption, 77.8% of anomalies have p < 0.001 vs 0.6% of clean data |

### The Corroboration Requirement

**The single most important design decision for FP control.** For most alerts, we require at least two detector families to independently flag an event. The one exception: IQR "significant" (2+ deviating features or extreme single-feature deviation) can trigger MEDIUM alone because the evidence is already multi-dimensional and directly interpretable. This exploits the fact that:
- True anomalies produce signals across multiple independent algorithms
- False positives are typically noise from a single algorithm
- Requiring agreement cuts the effective FP rate from ~5-10% (single detector) to <1% (joint agreement)

### How predict() Reduces False Positives

A critical design choice: instead of using continuous anomaly scores with thresholds (where 10% of clean data scores above the 90th percentile by definition), we use sklearn's `predict()` method for the IF and LOF votes — a binary yes/no decision calibrated to the contamination rate. The Mahalanobis vote uses a p-value threshold (`p < 0.01`) which has the same "fraction of clean data flagged" interpretation.

With `contamination=0.02`:
- IF's `predict()` flags ~2% of training data as anomalous
- LOF's `predict()` flags ~2% of training data as anomalous
- Mahalanobis `p < 0.01` flags ~1% of normal data by construction
- The probability of any two of the three independently flagging the same clean data point is approximately 0.04% (assuming independence)

This gives us a structural FP guarantee: **requiring 2-of-3 ML families to agree limits ML-driven false positives to a fraction of a percent of clean events, before IQR corroboration further tightens it.**

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
severity = determine_severity(
    deviations, if_pred, lof_pred, mahalanobis_p_value,
)
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
3. Refits Isolation Forest (100 trees, see `ISOLATION_FOREST_ESTIMATORS`)
4. Refits LOF (20 neighbors)
5. Recomputes Mahalanobis parameters (mean + inverse covariance)
6. Caches everything back to Redis

This means baselines adapt to gradual behavioral drift without manual intervention.

### Measured Performance by Scope

| Metric | Group Baseline | Per-Implant Baseline |
|---|---|---|
| **Detection rate** | 82.3% | **86.0%** |
| **FP rate** | **21.9%** | 44.3% |
| **Precision** | **78.1%** | 55.7% |

Per-implant baselines catch more anomalies (+4%) because they learn tighter, individual-specific norms. The biggest gain is `duplicated_persistence_setup` (44% → 94%) — when the model knows *this implant* always uses `scheduled_task`, seeing `registry_run` with wrong infrastructure is much more obvious than when the group model has seen all method types.

However, per-implant baselines have much higher FP rates because with fewer training samples the models are noisier. This is directly tied to sample count:

### Effect of Training Sample Size (Per-Implant)

| Samples per implant | Detection | FP rate | Precision |
|---|---|---|---|
| 70 | 86.7% | 43.8% | 56.2% |
| 150 | 80.1% | 40.5% | 59.5% |
| **300** | **82.2%** | **22.5%** | **77.5%** |
| 500 | 78.0% | 21.2% | 78.8% |

**300 samples is the sweet spot** — FP rate drops from 44% to 22% while detection stays at 82%. Going to 500 gives diminishing returns and detection actually decreases as the models overfit to a very stable baseline. This is why `BASELINE_WINDOW_SIZE_IMPLANT` is set to 300.

**Practical implication:** With 24 snapshots/implant/day and ~20% being any given config type, it takes approximately 60 days to fill a 300-sample window for one config type. The `IMPLANT_BASELINE_MIN_DAYS = 7` threshold activates per-implant baselines with ~34 samples — well below the optimal 300. Early per-implant baselines will be noisier (closer to the 70-sample FP rate) and will improve over time as the window fills.

---

## 10. Severity Classification and Alert Design

### Alert Panel Format (CLI)

```
+-- HIGH implant_ALPHA_003 (ALPHA) communication_configuration --+
| IQR Deviations [FLAGGED]:                                       |
|   beacon_interval_ms    1200   (expected 17000-43000, median    |
|                                 29900)                          |
|   jitter_percentage     0.00   (expected 0.10-0.25, median      |
|                                 0.18)                           |
| SHAP Contributions:                                             |
|   beacon_interval_ms   -0.623  anomalous                        |
|   sleep_on_failure_ms  -0.367  anomalous                        |
| IF: 0.987 [FLAGGED]  LOF: 0.943 [FLAGGED]  Mahal p: 2.1e-08 [F] |
+-- TP: beacon_storm:30000->1200 --------------------------------+
```

Each panel shows:
- **Severity** (color-coded: red=HIGH, yellow=MEDIUM)
- **Implant ID, group, and config type**
- **IQR deviations:** which features are outside fences, with expected range and median
- **SHAP contributions:** which features drove the IF decision. Sign convention follows IF `decision_function`: **negative** values push toward anomalous, positive values push toward normal. A more-negative SHAP value means a bigger contribution to the anomaly.
- **Detector votes:** IF percentile + `[FLAGGED]` indicator from `predict()`, LOF percentile + `[FLAGGED]`, Mahalanobis p-value + `[FLAGGED]` when `p < 0.01`
- **Ground truth** (green subtitle if this was an injected anomaly — the injector tag is shown as proof of a true positive)

### Summary Table

After each scoring batch, a summary table shows:
- **Per-injector detection breakdown** with color-coded rates (green at or above 70%, yellow at or above 40%, red below 40%)
- **Severity distribution** with visual bar charts
- **Overall performance:** detection rate, precision, FP rate

---

## 11. Synthetic Data and Evaluation Methodology

> **Important: All Data Is Synthetic**
>
> The entire PoC — baseline data, live telemetry, anomaly injection, and performance numbers — runs on **synthetic data** produced by hand-authored generators (`profiles.py`, `anomalies.py`). No real implant telemetry was used. This means:
>
> - **Correlation structures** (beacon drives jitter/retries/sleep; method count drives registry/task counts; evasion flags follow a layered probability chain) are **design choices**, not empirical observations. The probability values (e.g., "55% enable obfuscate_strings," "35% surveillance posture," "70% dominant action") were hand-picked by the PoC authors to produce realistic-looking data.
> - **Feature ranges** (beacon 25–35k ms, jitter 0.10–0.25, etc.) reflect the generator's clamped random distributions, not observed real-world ranges.
> - **Anomaly injectors** represent the authors' best guesses at what operator mistakes look like. Real mistakes may be subtler, more diverse, or structured differently.
> - **Detection rates and FP rates** are validated against this synthetic data only. Performance on real telemetry is unknown and will require re-evaluation.
> - **Algorithm parameters** (IQR multiplier, contamination rate, tree count, etc.) were tuned to work well on this synthetic data. They should be treated as starting points, not production-ready values.
>
> The PoC proves the **architecture and detection approach** work. It does not prove the specific numbers will transfer to production.

### Data Generation

The PoC uses synthetic data with controlled anomaly injection:

**Baseline phase (clean):**
- 15 implants across 3 groups (ALPHA, BRAVO, CHARLIE)
- 10 days of history at 24 snapshots/implant/day
- 5 configuration types with weighted sampling
- All data is clean — no anomalies

**Live phase (with injection):**
- Up to `LIVE_PHASE_MAX_EVENTS` (default 3,000) events streamed at maximum speed
- ~4% of events have anomalies injected (controlled by `ANOMALY_RATE`)
- Each anomalous event is tagged with ground truth (injector name + parameters)

### Anomaly Injectors

10 injectors organized by detection difficulty:

| Injector | Config Type | Mechanism | Difficulty |
|---|---|---|---|
| `beacon_storm` | Communication | Beacon interval reduced to 500-2000ms (from ~30,000ms baseline) | Easy (extreme IQR) |
| `zero_jitter` | Communication | Jitter set to 0 | Easy (extreme IQR) |
| `persistence_spike` | Persistence | Registry 50-200, tasks 20-50 | Easy (extreme IQR) |
| `capability_explosion` | Capability | All caps enabled + high concurrency | Easy (multi-feature IQR) |
| `wrong_comm_profile` | Communication | All features independently drawn | Medium (correlation violation) |
| `duplicated_persistence_setup` | Persistence | Depth-1 methods + depth-3 safety | Medium (correlation violation) |
| `mismatched_escalation_policy` | Dangerous Program | Programs=audit, drivers=do_nothing + extra drivers | Medium (posture violation) |
| `self_destruct_flood` | Dangerous Program | All actions to self_destruct + extra programs | Medium (ratio saturation) |
| `forgotten_operation_teardown` | Capability | All caps enabled + concurrency=1, timeout=150-200s | Medium (extreme mismatch) |
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
| **Detection rate** | 90.0% |
| **False positive rate** | 24.2% |
| **Precision** | 75.8% |
| Events scored | 5,000 |
| Anomalies injected | 209 |
| True positives | 188 |
| False positives | 60 |
| Total alerts fired | 248 |

> Reproducible via `poc/validate_detection.py` — deterministic `random.seed(42)`, no Kafka/Postgres/Redis required.

### Per-Injector Breakdown

| Injector | Injected | Detected | Rate |
|---|---|---|---|
| beacon_storm | 16 | 16 | **100%** |
| zero_jitter | 16 | 16 | **100%** |
| capability_explosion | 18 | 18 | **100%** |
| persistence_spike | 25 | 25 | **100%** |
| forgotten_operation_teardown | 15 | 15 | **100%** |
| wrong_comm_profile | 22 | 22 | **100%** |
| duplicated_persistence_setup | 23 | 23 | **100%** |
| mismatched_escalation_policy | 21 | 19 | **90%** |
| full_evasion | 33 | 22 | **67%** |
| self_destruct_flood | 20 | 12 | **60%** |

### Interpretation

- **All 10 anomaly types** are now detected at 60-100%
- **`full_evasion` improved from 0% to 67%** after redesigning persistence features with one-hot method encoding, which gave the ML models richer signal across all config types
- The two weakest injectors (`self_destruct_flood` at 60%, `full_evasion` at 67%) represent correlation-only anomalies where individual features stay within normal ranges

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

### Decision 6: No Fence Clamping — Pure IQR Fences

**Chosen:** Let IQR fences extend freely based on Q1 - k*IQR and Q3 + k*IQR.
**Alternative considered and rejected:** Clamp fences at P1/P99 of training data.

**Why not clamp:** Clamping at P1/P99 makes the fence effectively equal to [P1, P99] for most features, rendering the IQR multiplier irrelevant. With k=2.0 and compact distributions, Q1-k*IQR is typically well below P1, so `max(Q1-k*IQR, P1)` always equals P1. This defeats the purpose of having a tunable multiplier.

**Tradeoff:** Fences can extend into theoretically impossible ranges (e.g., negative lower fence for a count feature). This is harmless — impossible values never occur in real telemetry, so the fence boundary is never tested there. The fences define a statistical boundary, not a physical one.

**Note on boolean/constant features:** When IQR is zero (common for boolean flags), the standard formula breaks down. The system falls back to MAD-based fences (if the data has some spread) or a tight epsilon band (if the feature is truly constant in training).

---

## 14. What This PoC Proves

> **Caveat:** All claims below are validated against synthetic data with hand-authored correlation structures and anomaly injectors. They demonstrate that the architecture and algorithms *can* achieve these results when the data has the expected structure. Performance on real telemetry is an open question to be answered during the shadow-mode evaluation phase (see Section 16).

### 1. Autonomous Detection Is Viable

The system detects all 10 anomaly types at 60-100% (90% overall) without any hand-written rules. It learned what "normal" looks like from 10 days of synthetic history and immediately began flagging deviations.

### 2. False Positive Rate Is Controllable

At 24.2% FP rate (75.8% precision), the alert stream is readable. The corroboration requirement between detector families is the key mechanism — it provides structural FP control independent of threshold tuning.

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

1. **Binary feature combinations:** Anomalies that only change boolean flags (like enabling all evasion techniques) are hard to detect because each individual flag value appears in normal data. Isolation Forest and LOF struggle with high-dimensional binary spaces. The `full_evasion` injector currently sits at 66.7%, the second-weakest detector rate after `self_destruct_flood` (60.0%); both are correlation-only anomalies where individual features stay within normal ranges.

2. **Subtle correlation violations:** When anomaly feature values overlap heavily with normal data distributions (individual features within range, only the combination is unusual), detection requires very high sample counts (above 1000 per config type) for reliable ML discrimination.

3. **Stochastic variation:** The reported 90.0% detection rate is exactly reproducible via `validate_detection.py` (which seeds `random.seed(42)`), but is sensitive to algorithm parameters and training window sizes — change `ISOLATION_FOREST_CONTAMINATION` or `BASELINE_WINDOW_SIZE_IMPLANT` and the numbers shift.

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
| `ZERO_IQR_EPSILON` | 0.01 | Epsilon band for truly constant features |
| `ISOLATION_FOREST_ESTIMATORS` | 100 | Number of IF trees |
| `ISOLATION_FOREST_CONTAMINATION` | 0.02 | Expected anomaly fraction — controls `predict()` threshold used for severity voting |
| `ISOLATION_FOREST_MAX_FEATURES` | 0.8 | Feature subsampling per tree |
| `ISOLATION_FOREST_REPORTING_THRESHOLD` | 0.90 | IF percentile threshold used only by the summary layer to count "detected by IF" — does NOT drive severity voting |
| `LOF_N_NEIGHBORS` | 20 | LOF density estimation neighbors |
| `LOF_CONTAMINATION` | 0.02 | Expected anomaly fraction — controls `predict()` threshold used for severity voting |
| `LOF_REPORTING_THRESHOLD` | 0.90 | LOF percentile threshold used only by the summary layer — does NOT drive severity voting |
| `MAHALANOBIS_P_VALUE_HIGH` | 0.001 | p-value used for red colouring in alert panels |
| `MAHALANOBIS_P_VALUE_MEDIUM` | 0.01 | p-value threshold for the Mahalanobis severity vote — `p < 0.01` counts as one ML vote |
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
| `LIVE_PHASE_MAX_EVENTS` | 3,000 | Total live events before stop |

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
