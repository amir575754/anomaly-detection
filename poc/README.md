# Implant Anomaly Detection Platform — PoC

Autonomous anomaly detection for implant config telemetry. Learns normal behaviour from historical data and alerts when implants deviate — no hand-written rules.

---

## Prerequisites

- Docker + Docker Compose
- Python 3.10+
- `pip install -r requirements.txt`

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

### 2. Start the ingestor (Terminal 1)

```bash
python ingestion/ingestor.py
```

### 3. Start the detector (Terminal 2)

```bash
python detection/detector.py
```

The detector will wait for baseline data and then begin scoring live telemetry as it arrives.

### 4. Run the generator (Terminal 3)

```bash
python data/generator.py
```

Runs the baseline phase (clean synthetic history), waits for ingestion, bootstraps models, then starts the live phase with anomaly injection. The complete run finishes in under 3 minutes.

### 5. Open the dashboard (Terminal 4)

```bash
streamlit run ui/dashboard.py
```

Opens at `http://localhost:8501`.

### Resetting for a fresh demo

```bash
python reset.py          # interactive confirmation
python reset.py --force  # skip confirmation
```

---

## How It Works

### Architecture Overview

```
  generator ──publish──► Kafka ──consume──► ingestor ──► PostgreSQL + Redis
                                                              |
                                                   detector (continuous loop)
                                                      |
                                                alert engine ──► PostgreSQL (alerts)
                                                      |
                                                dashboard (poll)
```

### Baseline Phase

The generator creates synthetic implants across groups. For each implant, it produces clean configuration snapshots spanning multiple days — all timestamps are backdated so the system treats them as historical data.

Each snapshot is published to Kafka, consumed by the ingestor, and written to:
- **PostgreSQL**: raw JSON + extracted feature vector + ground truth
- **Redis sliding windows**: append-only lists of feature vectors, capped per (scope, config_type)

After ingestion, the generator bootstraps baselines by computing IQR fences and training Isolation Forest models for every window, caching them in Redis.

### Live Phase

The live phase streams new snapshots. ~4% are anomalous:

| Anomaly | Config Type | Effect |
|---|---|---|
| Beacon storm | Communication | Drops `beacon_interval_ms` from ~30,000 to 500-2,000 |
| Zero jitter | Communication | Sets `jitter_percentage` to 0 |
| Self-destruct flood | Dangerous Programs | All actions set to `self_destruct` |
| Capability explosion | Capability | All capabilities enabled, inflated concurrency |
| Full evasion | Evasion | Every evasion technique enabled simultaneously |
| Persistence spike | Persistence | Inflated registry key and scheduled task counts |

Each anomalous event carries its injector tag as ground truth for evaluation.

### Detection Pipeline

The detector runs in a continuous loop using cursor-based fetching (last processed row ID) so it never misses data. When a backlog exists, ticks run back-to-back; when caught up, it sleeps briefly before polling.

Each tick:

**1. Retrain stale baselines.** A monotonic write counter in Redis tracks how many feature vectors have been pushed since last training. When the delta exceeds `RETRAIN_THRESHOLD`, the baseline is retrained.

**2. Score unprocessed telemetry.** For each new row:

- **Scope resolution**: per-implant baseline if the implant has enough historical data, otherwise per-group. The alert records which was used.

- **IQR scoring**: flags features outside the IQR fences. Produces per-feature deviation magnitudes — the primary source of explainability.

- **Isolation Forest scoring**: multivariate anomaly score normalised to [0, 1]. Catches patterns that are subtle across many features but not extreme on any single one.

Config types listed in `DETECTION_EXCLUDED_CONFIG_TYPES` are skipped.

### Severity Scoring

| Severity | IQR Condition | Isolation Forest | Logic |
|---|---|---|---|
| **HIGH** | Significant deviation | Score above high threshold | Both agree |
| **MEDIUM** | Any deviation | Any | IQR alone sufficient |
| **MEDIUM** | None | Moderate score | IF alone |
| **LOW** | None | High score | Strong IF, no IQR |

All thresholds are configurable in `config.py`.

### Alert Engine and Fatigue Mitigation

Three filters before an alert is persisted:

1. **Pattern suppression**: hash of (implant, config type, deviating features). Suppressed patterns are skipped. Suppressions expire after `SUPPRESSION_DECAY_HOURS`.
2. **Deduplication**: same pattern hash within `DEDUP_WINDOW_MINUTES` is suppressed.
3. **Rate limiting**: max `RATE_LIMIT_PER_WINDOW` alerts per implant per tick.

Alerts include auto-generated explanations like:
> "Implant implant_ALPHA_003 (group ALPHA): `beacon_interval_ms` is 1200 (expected 21750-37950, median 30100). Severity: HIGH."

### Feedback Loop

Operators classify alerts via the dashboard:
- **True Positive**: confirms detection
- **False Positive**: detection error
- **Expected Deviation**: real but anticipated

Both `false_positive` and `expected_deviation` count toward suppression. Once enough labels accumulate on the same pattern, it is suppressed. A "Clear All Suppressions" button is available for demo resets.

### Redis Data Structures

| Key Pattern | Type | Writer | Reader | Purpose |
|---|---|---|---|---|
| `window:{scope}:{id}:{type}` | List | Ingestor | Baseline trainer | Sliding window of feature vectors |
| `model:{scope}:{id}:{type}` | Hash | Baseline trainer | Detector | Cached IQR fences + serialized Isolation Forest |
| `writes:{scope}:{id}:{type}` | String | Ingestor | Detector | Monotonic push counter for retrain decisions |
| `detector:baseline_cursor` | String | Generator | Detector | Telemetry ID where live data starts |

### Database Schema

| Table | Purpose |
|---|---|
| `implants` | Implant registry — ID, group, first/last seen |
| `telemetry` | Raw snapshots + features + ground truth |
| `baselines` | IQR stats (mirrors Redis for durability) |
| `alerts` | Detections with severity, explanation, operator labels |
| `suppressed_patterns` | Patterns silenced by feedback |

---

## Configuration

All parameters are in `config.py`. See comments there for what each controls. Key settings include anomaly rate, implant groups, IQR/IF thresholds, detection loop timing, and alert fatigue parameters.

---

## Teardown

```bash
docker compose down -v   # removes containers and the postgres volume
```
