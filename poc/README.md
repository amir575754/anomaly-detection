# Implant Anomaly Detection Platform — PoC

Autonomous anomaly detection for implant config telemetry. Learns normal behaviour from historical data and alerts when implants deviate — no hand-written rules.

---

## Prerequisites

- Docker + Docker Compose
- Python 3.10+
- `tmux` (the demo runs in a three-pane tmux session)
- `pip install -r requirements.txt` in a virtualenv at `poc/.venv`

---

## Quick Start

### 1. Start infrastructure

```bash
docker compose up -d
```

Wait until all four services are healthy:

```bash
docker compose ps
```

You should see `postgres`, `zookeeper`, `kafka`, and `redis` all `Up (healthy)`.

### 2. Run the demo

```bash
python run.py
```

That's it. `run.py` launches a tmux session with three panes:

- **Ingestor** (top-left) — Kafka consumer, writes parsed telemetry to PostgreSQL and feature vectors to Redis sliding windows.
- **Generator** (bottom-left) — emits a compressed baseline phase of clean synthetic history, waits for the ingestor to finish, bootstraps baselines, then streams the live phase with `ANOMALY_RATE` (~4%) anomalous events injected.
- **Detector** (right) — scores every ingested event, prints `rich`-formatted alert panels with IQR deviations, SHAP contributions, and all four detector votes.

The full run (baseline + live + detection) completes in about 40 seconds.

### Resetting for a fresh demo

```bash
python reset.py          # interactive confirmation
python reset.py --force  # skip confirmation
```

`reset.py` truncates all application tables in PostgreSQL, flushes Redis, and clears the baseline cursor so the next `python run.py` starts from a clean state.

### Offline validation (no Docker required)

```bash
python validate_detection.py
```

Runs the full detection math in-memory with `random.seed(42)` — generates training data, trains all four models, injects anomalies into a 5000-event test set, scores everything, and prints overall detection rate / precision / FP rate plus a per-injector breakdown. Useful for verifying behaviour after a code change without spinning up the Docker stack.

---

## How It Works

### Architecture Overview

```
  generator ──publish──► Kafka ──consume──► ingestor ──► PostgreSQL (raw + features)
                                                   │
                                                   ▼
                                               Redis (sliding windows + cached models)
                                                   │
                                                   ▼
                                              detector (continuous loop)
                                                   │
                                                   ▼
                                             CLI alert panels (rich)
```

No separate alert engine, dashboard, or persistence layer for alerts — detections are printed to the detector pane and summarized at the end of the run.

### Baseline Phase

The generator creates synthetic implants across groups (see `IMPLANT_GROUPS` in `config.py` — default 3 groups × 5 implants). For each implant, it produces `BASELINE_DAYS * SNAPSHOTS_PER_IMPLANT_PER_DAY` clean configuration snapshots with backdated timestamps so the system treats them as historical data.

Each snapshot is published to Kafka, consumed by the ingestor, and written to:
- **PostgreSQL** `telemetry` table — raw JSON + extracted feature vector + ground truth
- **Redis sliding windows** — append-only lists of feature vectors, capped per `(scope, config_type)` at `BASELINE_WINDOW_SIZE`

After ingestion, the generator bootstraps baselines by computing IQR fences and training the four detector models for every window, caching each `TrainedModel` in a Redis hash per `(scope, config_type)`.

### Live Phase

The live phase streams new snapshots until `LIVE_PHASE_MAX_EVENTS` is reached. About `ANOMALY_RATE` (~4%) of events are anomalous. The 10 injectors in `data/anomalies.py`:

| Injector | Config Type | Effect |
|---|---|---|
| `beacon_storm` | Communication | Drops `beacon_interval_ms` from ~30,000 to 500–2,000 |
| `zero_jitter` | Communication | Sets `jitter_percentage` to 0.0 (predictable beaconing) |
| `wrong_comm_profile` | Communication | Applies a cross-group profile that breaks learned correlations |
| `self_destruct_flood` | Dangerous Programs | Every program/driver action set to `self_destruct`, plus extra entries |
| `mismatched_escalation_policy` | Dangerous Programs | Programs and drivers set to contradictory response postures |
| `capability_explosion` | Capability | All capabilities enabled, inflated `max_concurrent_tasks` |
| `forgotten_operation_teardown` | Capability | Surveillance capabilities left enabled with extreme idle parameters |
| `full_evasion` | Evasion | All evasion layers enabled without their prerequisites (dependency chain violation) |
| `persistence_spike` | Persistence | Inflated registry key and scheduled task counts |
| `duplicated_persistence_setup` | Persistence | Method declared as one type but infrastructure counts match a different method |

Each anomalous event carries its injector tag as ground truth for evaluation.

### Detection Pipeline

The detector runs in a continuous loop using cursor-based fetching (last processed telemetry row ID) so it never misses data. When a backlog exists, ticks run back-to-back; when caught up, it sleeps briefly before polling.

Each tick:

**1. Retrain stale baselines.** A monotonic write counter in Redis tracks how many feature vectors have been pushed since last training. When the delta exceeds `RETRAIN_THRESHOLD`, the baseline is retrained for that `(scope, config_type)` pair.

**2. Score unprocessed telemetry.** For each new row:

- **Scope resolution** — per-implant baseline if the implant has enough historical data (after `IMPLANT_BASELINE_MIN_DAYS`), otherwise per-group. The alert records which was used.
- **IQR scoring** — flags features outside the IQR fences; produces per-feature deviation magnitudes for explainability.
- **Isolation Forest scoring** — multivariate anomaly score plus `predict()` (calibrated to `ISOLATION_FOREST_CONTAMINATION`).
- **LOF scoring** — local density outlier score plus `predict()` (`LOF_CONTAMINATION`).
- **Mahalanobis scoring** — distance → p-value.
- **SHAP explanation** — computed only when severity is non-null, so we don't pay the cost on suppressed events.

Config types listed in `DETECTION_EXCLUDED_CONFIG_TYPES` are skipped.

### Severity Scoring

The severity decision lives in `determine_severity()` in `detection/scoring.py`. Let `ml_votes = if_predicts_anomaly + lof_predicts_anomaly + (mahalanobis_p < 0.01)`.

| Severity | When |
|---|---|
| **HIGH** | IQR significant (2+ deviations or a deviation above `IQR_SIGNIFICANT_MULTIPLIER` IQR units) **and** at least one ML vote, **or** 2+ ML families agree. |
| **MEDIUM** | IQR significant alone, **or** any IQR deviation + at least one ML vote. |
| *(suppressed)* | Everything else. A single ML vote without IQR corroboration is intentionally dropped — measured single-detector alerts on clean data were ~93% noise. |

All thresholds are configurable in `config.py`. Note that `ISOLATION_FOREST_REPORTING_THRESHOLD` and `LOF_REPORTING_THRESHOLD` are used only by the summary-reporting layer (`detection/summary.py`) to count "which detector family fired"; severity voting uses the binary `predict()` outputs instead.

### Redis Data Structures

| Key Pattern | Type | Writer | Reader | Purpose |
|---|---|---|---|---|
| `window:{scope}:{id}:{type}` | List | Ingestor | Detector (training) | Sliding window of feature vectors, capped at `BASELINE_WINDOW_SIZE` |
| `model:{scope}:{id}:{type}` | Hash | Detector (training) | Detector (scoring) | Cached `TrainedModel` — IQR fences, IF, LOF, Mahalanobis params, `StandardScaler`, SHAP explainer |
| `writes:{scope}:{id}:{type}` | String | Ingestor | Detector | Monotonic push counter for retrain decisions |
| `detector:baseline_cursor` | String | Generator | Detector | Telemetry ID where live data starts |

### Database Schema (`init_db.sql`)

| Table | Purpose |
|---|---|
| `implants` | Implant registry — ID, group, first/last seen, status |
| `telemetry` | Raw snapshots + features + ground truth (`is_anomaly`, `injector_tag`) |
| `baselines` | IQR stats (mirrors Redis for durability; not read by the detector) |

---

## Configuration

All parameters are in `config.py`. Each constant has a one-line comment. Grouped roughly by concern: infrastructure (DSNs, Kafka, Redis), detection models (IQR multipliers, contamination rates, reporting thresholds, SHAP top-N), baseline management (window sizes, cold-start days, retrain threshold), detection engine (idle sleep, max rows per tick, backoff), data generation (anomaly rate, implant groups, baseline days), and ingestor (batch sizes, poll timings).

---

## Teardown

```bash
docker compose down -v   # removes containers and the postgres volume
```
