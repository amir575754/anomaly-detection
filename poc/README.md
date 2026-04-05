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

### 3. Run the generator (Terminal 2)

```bash
python data/generator.py
```

Runs baseline phase (10 days of clean data), waits for ingestion, bootstraps baselines, then starts the live phase with ~4% anomalies.

> Wait for `Initial baseline bootstrap complete` before starting the detector.

### 4. Start the detector (Terminal 3)

```bash
python detection/detector.py
```

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
                                               detector (5-min tick)
                                                  |
                                            alert engine ──► PostgreSQL (alerts)
                                                  |
                                            dashboard (poll)
```

### Phase 1: Baseline Generation and Ingestion

The generator creates 50 synthetic implants across 3 groups. For each implant, it produces 10 days of clean configuration snapshots — 96 per day, ~48,000 events total. All timestamps are backdated so the system treats them as historical data.

Each snapshot is published to Kafka, consumed by the ingestor, and written to:
- **PostgreSQL**: raw JSON + extracted feature vector + ground truth
- **Redis sliding windows**: append-only lists of feature vectors, capped at 1000 entries per (scope, config_type) combination

The ingestor uses batch processing — accumulating 500 messages before writing with `execute_values` (PostgreSQL) and a Redis pipeline, reducing round-trip overhead by ~500x.

After ingestion, the generator bootstraps baselines by computing IQR fences and training Isolation Forest models for every window, caching them in Redis.

### Phase 2: Live Streaming with Anomaly Injection

The live phase streams new snapshots with rate limiting (500 events/sec, 50,000 total). ~4% are anomalous:

| Anomaly | Config Type | Effect |
|---|---|---|
| Beacon storm | Communication | Drops `beacon_interval_ms` from ~30,000 to 500-2,000 |
| Zero jitter | Communication | Sets `jitter_percentage` to 0 |
| Self-destruct flood | Dangerous Programs | All actions set to `self_destruct` |
| Capability explosion | Capability | All capabilities enabled, inflated concurrency |
| Full evasion | Evasion | Every evasion technique enabled simultaneously |
| Persistence spike | Persistence | Inflated registry key and scheduled task counts |

Each anomalous event carries its injector tag as ground truth for evaluation.

### Feature Extraction

The extractor converts raw config JSON into a flat `dict[str, float]` — a fixed-length numeric vector per config type. For example, communication config becomes 10 features: `beacon_interval_ms`, `jitter_percentage`, `c2_channel_count`, etc. Extraction happens once at ingestion time.

### Detection Pipeline

The detector runs on a 5-minute tick using cursor-based fetching (last processed row ID) so it never misses data during bursts.

Each tick:

**1. Retrain stale baselines.** A monotonic write counter in Redis tracks how many feature vectors have been pushed since last training. When the delta exceeds 200, the baseline is retrained.

**2. Score unprocessed telemetry.** For each new row:

- **Scope resolution**: per-implant baseline if the implant has 7+ days of data, otherwise per-group. The alert records which was used.

- **IQR scoring**: flags features outside `[Q1 - 2.5*IQR, Q3 + 2.5*IQR]`. Produces per-feature deviation magnitudes — the primary source of explainability.

- **Isolation Forest scoring**: multivariate anomaly score normalised to [0, 1]. Catches patterns that are subtle across many features but not extreme on any single one.

Config types with too few features (e.g. `internet_configuration` with a single boolean) are excluded.

### Severity Scoring

| Severity | IQR Condition | Isolation Forest | Logic |
|---|---|---|---|
| **HIGH** | 2+ features or >5x IQR | Score > 0.7 | Both agree |
| **MEDIUM** | 1+ features | Any | IQR alone sufficient |
| **MEDIUM** | None | Score 0.5-0.7 | Medium IF alone |
| **LOW** | None | Score > 0.7 | Strong IF, no IQR |

### Alert Engine and Fatigue Mitigation

Three filters before an alert is persisted:

1. **Pattern suppression**: hash of (implant, config type, deviating features). Suppressed patterns are skipped. Suppressions expire after 24 hours.
2. **Deduplication**: same pattern hash within 15 minutes is suppressed.
3. **Rate limiting**: max 3 alerts per implant per tick.

Alerts include auto-generated explanations like:
> "Implant implant_ALPHA_003 (group ALPHA): `beacon_interval_ms` is 1200 (expected 21750-37950, median 30100). Severity: HIGH."

### Feedback Loop

Operators classify alerts via the dashboard:
- **True Positive**: confirms detection
- **False Positive**: detection error
- **Expected Deviation**: real but anticipated

Both `false_positive` and `expected_deviation` count toward suppression. Three such labels on the same pattern suppresses it. A "Clear All Suppressions" button is available for demo resets.

### Redis Data Structures

| Key Pattern | Type | Writer | Reader | Purpose |
|---|---|---|---|---|
| `window:{scope}:{id}:{type}` | List | Ingestor | Baseline trainer | Sliding window of feature vectors (max 1000) |
| `model:{scope}:{id}:{type}` | Hash | Baseline trainer | Detector | Cached IQR fences + serialized Isolation Forest |
| `writes:{scope}:{id}:{type}` | String | Ingestor | Detector | Monotonic push counter for retrain decisions |

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

All parameters are in `config.py`. Key settings:

| Constant | Default | What it controls |
|---|---|---|
| `ANOMALY_RATE` | `0.04` | Fraction of live events that are anomalous |
| `IMPLANT_GROUPS` | `{ALPHA:17, BRAVO:17, CHARLIE:16}` | Group names and implant counts |
| `IQR_MULTIPLIER` | `2.5` | Fence width in IQR units |
| `ISOLATION_FOREST_HIGH_THRESHOLD` | `0.7` | IF score threshold for HIGH |
| `DETECTION_WINDOW_MINUTES` | `5` | Detector tick interval |
| `LIVE_PHASE_MAX_EVENTS_PER_SECOND` | `500` | Generator rate limit |
| `LIVE_PHASE_MAX_EVENTS` | `50000` | Generator event cap |
| `SUPPRESSION_DECAY_HOURS` | `24` | Suppression expiry |
| `INGESTOR_BATCH_SIZE` | `500` | Messages per batch write |

---

## Teardown

```bash
docker compose down -v   # removes containers and the postgres volume
```
