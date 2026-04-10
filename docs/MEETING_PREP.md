# Implant Engineers Onboarding — Anomaly Detection PoC

> **Audience:** Implant engineers. Deep domain knowledge (they built the configs we're scoring). **Zero** machine learning background. Goal: teach them enough ML + anomaly detection intuition that they can sit next to us and co-design features and review detection results.
>
> **Tone:** Concrete. Every abstract concept pinned to a real implant/group/config. No algorithm math beyond what's needed to reason about tradeoffs.
>
> **Every example below is tagged with scope** — `[GROUP: ALPHA]` means the baseline was computed across all implants in ALPHA; `[IMPLANT: ALPHA_003]` means the baseline was computed from one implant's own history.

---

## 1. The One-Sentence Pitch (30 seconds)

> "We built a system that looks at every new config snapshot an implant reports and answers the question **'does this look like anything we've seen before from this implant, or from its group?'** If the answer is no — alert. No rules, no signatures, no hardcoded thresholds. Just learned behavior."

Then pause, and say:

> "I'm showing this to you specifically because the quality of the system depends on two things you know better than anyone: (1) which numbers we extract from a config to represent it, and (2) whether the things it flags are actually mistakes. I need your help on both."

---

## 2. The Mental Model — What Is Anomaly Detection, Really?

Start here. This is the single concept everything else hangs off.

### 2.1 Classifier vs. anomaly detector

```
  CLASSIFIER (what most people picture when you say "ML"):

     "Here are 10,000 examples of cat pictures, 10,000 of dogs.
      Learn the difference. Now classify this new picture."

     Needs:  LABELS for everything
     Learns: the boundary between cat and dog
     Fails:  on something it's never seen (giraffe -> "dog, I guess")


  ANOMALY DETECTOR (what we actually built):

     "Here are 10,000 normal configs. No labels. No examples of
      mistakes. Learn what normal looks like. Flag anything that
      doesn't fit."

     Needs:  only normal data (we call this the 'baseline')
     Learns: the shape of 'normal'
     Flags:  anything that falls outside that shape
```

**Why we chose this flavor:** We don't have labeled mistakes. Nobody sat down and tagged 500 configs as "operator forgot the watchdog" or "wrong comm profile pushed to the wrong group." We have **a lot of normal** (every config every implant has ever reported) and **no labels**. Anomaly detection is the only approach that works with that data shape.

> **Key phrase to plant in their heads:** *"We are not teaching the system what's wrong. We are teaching it what's normal, and letting it flag the rest."*

### 2.2 What "normal" means — and why scope matters

"Normal" isn't a global constant. It depends on **who** you're comparing against.

```
  [GROUP: ALPHA]  (5 implants pooled)
    normal beacon_interval_ms  ≈  25000–35000
    normal jitter              ≈  0.10–0.25
    normal watchdog_enabled    ≈  always on

  [IMPLANT: ALPHA_003]  (this one implant's own history)
    normal beacon_interval_ms  ≈  29800–30200   <-- much tighter
    normal jitter              ≈  0.17–0.19     <-- much tighter
```

Same feature, two baselines, two different definitions of normal. A beacon of 26000 is:
- normal for `[GROUP: ALPHA]`
- **anomalous** for `[IMPLANT: ALPHA_003]` (its own history says it always sits near 30000)

This is why we maintain **two baselines per implant**:

| Baseline | Built from | When it's used |
|---|---|---|
| **Group** (e.g. `[GROUP: ALPHA]`) | all 5 implants in ALPHA, pooled | always available, used during cold start |
| **Per-implant** (e.g. `[IMPLANT: ALPHA_003]`) | only that implant's own history | kicks in after 7 days of data for that implant |

**Why both:**
- **Group** is robust (1000+ samples, stable) but loose — it tolerates anything any ALPHA implant does.
- **Per-implant** is tight (300 samples, sensitive to subtle drift) but needs time to accumulate.

During cold start we rely on the group. Once the implant has earned its own baseline, we prefer it because it catches drift the group would smooth over.

---

## 3. Feature Extraction — Where YOU Come In

> **This is the section to slow down on. This is what you're going to be helping us with.**

### 3.1 The problem: ML models don't understand JSON

An implant reports this:

```json
{
  "beacon_interval_ms": 30000,
  "jitter_percentage": 0.18,
  "c2_channels": [
    { "protocol": "https", "port": 443, "enabled": true },
    { "protocol": "dns",   "port": 53,  "enabled": false }
  ],
  "encryption": { "enabled": true, "key_rotation_hours": 24 }
}
```

No ML algorithm can score that. They eat **flat vectors of numbers**. So we write a function, `extract_features(config) -> dict[str, float]`, that turns each config into something like:

```
[GROUP: ALPHA]  communication_configuration features:
  beacon_interval_ms        30000.0
  jitter_percentage             0.18
  c2_channel_count              2.0
  c2_enabled_count              1.0
  c2_non_standard_ports         0.0
  beacon_to_sleep_ratio         3.0     <-- derived, not in the JSON
  beacon_to_rotation_ratio   1250.0     <-- derived
  jitter_retries_product        0.54    <-- derived
```

**The features we invent are the vocabulary the ML models get to think in.** If we don't extract a feature, no algorithm — no matter how sophisticated — can use it.

### 3.2 The three flavors of feature we extract

Feature engineering is an art, not a checklist. But here's the taxonomy we've been using:

**(a) Raw pass-through** — the number from the config, unchanged.
`beacon_interval_ms`, `jitter_percentage`, `max_concurrent_tasks`.

**(b) Counts and ratios** — summarizing lists or proportions.
`c2_channel_count`, `enabled_ratio`, `overall_self_destruct_ratio`.

**(c) Relationships between fields** — this is the interesting one.
`beacon_to_sleep_ratio = beacon_interval_ms / sleep_on_failure_ms`
`dependency_coherence = fraction of evasion techniques whose prerequisites are enabled`

The relationship features are where your domain knowledge becomes detection capability. Example:

```
[IMPLANT: BRAVO_002]  communication_configuration

  You (the implant engineer) know:
    "When we crank beacon up, we always crank sleep_on_failure up
     proportionally. They're tuned together. An implant with
     beacon=30000 and sleep=1000 is misconfigured — it'll hammer
     the C2 on every transient network blip."

  That domain rule becomes a feature:
    beacon_to_sleep_ratio  (normally ~3.0 across baseline)

  Now when beacon=30000, sleep=1000 shows up:
    beacon_to_sleep_ratio = 30.0   <-- 10x normal
    IQR flags it immediately.
```

Without that derived feature, IQR would see:
- `beacon_interval_ms = 30000` — **normal**
- `sleep_on_failure_ms = 1000` — **normal individually**
- And miss the anomaly entirely.

> **What I need from you:** every time you read a config and think *"huh, that's weird because X and Y should match"* — that's a feature we should be extracting. Tell us. We'll add it.

### 3.3 The trap: don't encode the answer into the feature

This is subtle but critical. Features should describe **general properties**, not **specific anomalies**.

```
  WRONG — this is a rule disguised as a feature:
    is_all_self_destruct = 1 if every entry's action is self_destruct else 0

  RIGHT — this is a general property that happens to catch it:
    dominant_action_ratio = (count of most-common action) / total
    # is_all_self_destruct would be a special case where this hits 1.0,
    # but it also catches all-audit, all-do_nothing, and partial skews
```

If your feature name contains the word "anomalous," "bad," "suspicious," "wrong," or a specific threshold — you're writing a rule. The whole point of this approach is that we don't have to enumerate every mistake in advance. Features should describe **shape**, not **verdict**.

> **Why this matters practically:** the first time an operator invents a new category of mistake we've never imagined, a rule-encoded feature will sail right past it. A shape-encoded feature has a chance of catching it because the shape will look wrong.

### 3.4 One-hot encoding — turning a category into numbers

ML algorithms can't eat strings. If a field is categorical (`method = "registry_run"`), we expand it into multiple binary features:

```
[IMPLANT: CHARLIE_001]  persistence_configuration

  Raw config:  active_methods = ["registry_run"]
               registry_key_count = 3
               scheduled_task_count = 0

  Extracted features:
    method_registry_run      = 1.0    \
    method_scheduled_task    = 0.0     |
    method_service_install   = 0.0     |-- one-hot encoding
    method_startup_folder    = 0.0     |
    method_wmi_subscription  = 0.0    /
    registry_key_count       = 3.0
    scheduled_task_count     = 0.0
```

**Why this matters:** the ML models can now *learn that when `method_registry_run=1`, then `registry_key_count > 0` and `scheduled_task_count == 0`*. If a new config shows up with `method_registry_run=1` but `scheduled_task_count=5`, the models notice the combination is impossible in the baseline and flag it.

That's a real anomaly type we inject (`duplicated_persistence_setup`) and it's caught entirely because of one-hot encoding — no hand-written rule required.

---

## 4. The Algorithms — Plain English

We run **four** detectors on every feature vector. Each one catches different kinds of weirdness. None of them individually is good enough.

### 4.1 IQR — "Is this number outside its normal range, feature by feature?"

The simplest one. For every feature, we compute the middle 50% range of the baseline (Q1 to Q3), multiply its width by 2, and draw fences.

```
[GROUP: BRAVO]  feature: beacon_interval_ms

  baseline values (sorted):
    [25000, 27000, 28000, 29000, 30000, 31000, 32000, 33000, 35000]
                      Q1=27500               Q3=32500
                      |<------  IQR  ------>|
                      |<---  5000 wide  --->|

  lower fence = 27500 - 2*5000 = 17500
  upper fence = 32500 + 2*5000 = 42500

  new event: beacon = 1597   --> WAY below 17500 --> FLAGGED
```

- **Strength:** crystal clear to explain. "Expected 17500–42500, got 1597" is something anyone can reason about.
- **Weakness:** looks at features **one at a time**. Cannot see that `beacon=35000` + `sleep=1000` is a weird combination if both are individually within their own fences.

### 4.2 Isolation Forest — "Is this point easy to pluck out of the crowd?"

Imagine plotting every baseline feature vector as a dot in high-dimensional space. Normal points cluster. Anomalies sit alone.

Isolation Forest builds random binary trees that split the space on random features at random thresholds. Each split asks one yes/no question like *"is `max_concurrent_tasks > 15`?"* and cuts the remaining points in two. The algorithm's score is **how few splits it takes to isolate the point by itself, averaged over 100 random trees**. Few splits → anomaly.

Concrete walkthrough on `[GROUP: CHARLIE] capability_configuration`, using just two features for illustration (the real model uses all ~9):

```
  max_concurrent_tasks
       |
    40 |                                                  X   <-- new event
       |                                                      from CHARLIE_004
       |
    20 |
       |                                                      split 1:
       |    . . . .                                           max_concurrent_tasks > 15?
    10 |  . . . . . .                                         --> isolates X
       |  . . . . . .      <-- baseline: 2-4 enabled,             from 95% of
     5 |  . . . . . .          max_tasks ≈ 5                      the cluster
       |    . . . .
       +---+---+---+---+---+---+---+---+---+---+---+---+
           2   4   6   8  10  12                     enabled_count

  X's path down one tree:
    split 1: max_concurrent_tasks > 15?   YES  --> isolated from 95% of cluster
    split 2: enabled_count > 10?          YES  --> alone in the leaf

  X needed 2 splits to isolate.
  A normal point in the dense cluster would need ~10 splits
  (you have to slice the crowd away from it one layer at a time).

  Averaged over 100 random trees:
    IF score for X = 0.987  --> FLAGGED

  Critically: neither feature alone was extreme enough for IQR
  to flag (max_concurrent_tasks=40 might be within the upper
  fence, enabled_count=12 might be within its upper fence). It's
  the COMBINATION that lands X in an empty region of the plot,
  and IF is the first algorithm in our stack that can see that.
```

- **Strength:** catches **multi-feature** weirdness that IQR misses.
- **Weakness:** it's a black box by default. "Why did you flag this?" — the model just says "it was easy to isolate." Not satisfying. That's why we add **SHAP** on top (§4.5).

### 4.3 LOF (Local Outlier Factor) — "Are your neighbors as close to you as theirs are to them?"

Same high-dimensional picture, but a different question. LOF takes each new point, finds its 20 nearest baseline neighbors, and asks: *"How dense is my neighborhood compared to my neighbors' neighborhoods?"* If you're in a tight cluster, normal. If you're floating in empty space while everyone else is packed tight, anomalous.

Concrete walkthrough on `[IMPLANT: ALPHA_003] communication_configuration`, plotting `jitter_percentage` against `beacon_interval_ms`:

```
  jitter_percentage
         |
    0.20 |
         |         . . .         <-- ALPHA_003's 300 historical
    0.19 |       . . . . .            vectors form a VERY tight
         |       . . A . .            cluster: beacon ≈ 30000,
    0.18 |       . . . . .            jitter ≈ 0.18. Neighbors
         |         . . .              are all within 0.01 of
    0.17 |                            each other.
         |
    ...  |
         |
    0.05 |                                          B   <-- new event from
         |                                              ALPHA_003:
         |                                              beacon=29500, jitter=0.05
         +---+---+---+---+---+---+---+---+---+---+---+---
             28000     29000     30000     31000     32000
                         beacon_interval_ms

  What LOF sees for point B:
    - B's 20 nearest neighbors are all the cluster points near A
    - Average distance from B to those neighbors:  ≈ 0.13
    - Average distance among those neighbors:       ≈ 0.01
    - B is ~13x sparser than its neighbors are to each other
    --> LOF score ≈ 13  --> FLAGGED

  Meanwhile:
    IQR (using the [GROUP: ALPHA] baseline, which spans jitter 0.10-0.25):
      jitter = 0.05   --> outside the group fence, MIGHT flag
      beacon = 29500  --> well within group fence, does NOT flag
    But using the [IMPLANT: ALPHA_003] baseline, jitter=0.05 is
    miles from this implant's historical 0.17-0.19 range —
    which is exactly the situation LOF on the per-implant
    baseline is built to catch.
```

This is why per-implant baselines matter: LOF on `[IMPLANT: ALPHA_003]` notices drift the `[GROUP: ALPHA]` baseline would tolerate, because "normal" for ALPHA_003 is far tighter than "normal" for the group.

- **Strength:** catches **local** outliers — things that are unusual *for this specific cluster* even if they'd be normal globally.
- **Weakness:** sensitive to feature scaling (we handle this with `StandardScaler`, so `beacon_interval_ms=30000` and `jitter_percentage=0.18` are put on the same numeric scale before LOF measures distances).

### 4.4 Mahalanobis — "Do you break the expected correlations?"

This is the correlation-aware detector. Instead of looking at features independently, it learns the **joint shape** of the baseline cloud.

Imagine plotting `beacon_interval` against `sleep_on_failure`. In normal data they're correlated — the cloud is an **oval**, not a circle:

```
  sleep_on_failure
       |           . . .
       |         . . . . .
       |       . . .C. . . .      C = center of the cloud
       |         . . . . .
       |           . . .
       +---+---+---+---+---
           beacon_interval

  Point A: high beacon + high sleep   = follows the oval    = normal (p ≈ 0.15)
  Point B: high beacon + LOW sleep    = cuts across the oval = anomalous (p ≈ 0.001)

  Same Euclidean distance from C. But B is in a direction the baseline
  has never populated. Mahalanobis measures distance using the OVAL'S
  SHAPE as its ruler — so it knows B is suspicious.
```

Mahalanobis reports a **p-value**: "probability of seeing something this weird under the learned distribution." `p < 0.001` → very anomalous.

```
[GROUP: BRAVO]  communication_configuration
  Baseline correlation: beacon and sleep scale together (ratio ~3x)
  New event:
    beacon_interval_ms    = 30000   <-- normal alone
    sleep_on_failure_ms   = 1000    <-- normal alone
    beacon_to_sleep_ratio = 30.0    <-- derived
  Mahalanobis p-value = 0.0003  --> FLAGGED
  (the joint direction has never been populated in BRAVO's baseline)
```

- **Strength:** catches **correlation violations** — each feature normal individually, combination impossible.
- **Weakness:** assumes the baseline cloud is roughly elliptical. On weird multi-modal feature distributions it's noisier.

### 4.5 SHAP — the "why" for Isolation Forest

Isolation Forest and LOF don't naturally explain themselves. SHAP is an add-on that, for a given flagged event, reports:

> "Of the anomaly score IF assigned, **feature X contributed -0.62, feature Y contributed -0.34, the rest were negligible.** Those are the features that drove the decision."

```
[IMPLANT: ALPHA_003]  communication_configuration

  IF flagged the event with score 0.995
  SHAP contributions:
    beacon_interval_ms   -0.623   <-- dominant driver
    sleep_on_failure_ms  -0.367   <-- secondary driver
    jitter_percentage    -0.012   <-- negligible
    ...

  Human reading: "IF flagged this mainly because of beacon and sleep.
  Go look at those."
```

Without SHAP, Isolation Forest alerts are "trust me, it's weird." With SHAP, we can hand an operator the **two features to inspect first**. For implant engineers reviewing detections, this is the single most important interpretability tool we have.

---

## 5. The Voting Ensemble — Why Four Instead of One

We run all four detectors on every event and require **at least 2 to agree** before we raise an alert.

```
                        +-------+
                        | Event |
                        +---+---+
                            |
              +-------------+-------------+-------------+
              |             |             |             |
          +---v---+   +----v----+   +----v---+   +----v--------+
          |  IQR  |   |   IF    |   |  LOF   |   | Mahalanobis |
          +---+---+   +----+----+   +----+---+   +----+--------+
              |             |             |             |
              +-------------+-------------+-------------+
                            |
                       2+ voted yes?
                        /        \
                      YES          NO
                       |            |
                    ALERT       suppress
```

### Why multiple detectors?

Because they catch **different kinds of weirdness**:

| Detector | Catches |
|---|---|
| **IQR** | single-feature extremes (`beacon = 500`) |
| **IF** | multi-feature combinations in empty regions of space |
| **LOF** | local drift — unusual *for this cluster* |
| **Mahalanobis** | correlation violations — normal marginals, impossible joint |

No single one covers all cases. Example:

```
[IMPLANT: CHARLIE_002]  evasion_configuration

  sleep_obfuscation = ON  (normally requires amsi_bypass + etw_patch + unhook_ntdll)
  amsi_bypass       = OFF
  etw_patch         = OFF
  unhook_ntdll      = OFF

  IQR:          nothing flagged — all individual features are valid booleans
  Mahalanobis:  FLAGGED — this joint pattern never occurs in baseline
  IF:           FLAGGED — isolated region of feature space
  LOF:          FLAGGED — no dense neighborhood for this combination
  --> 3/4 votes, alert raised
```

### Why **require agreement** — the alert fatigue tradeoff

When we measured single-detector alerts on clean data:

```
  "Let every detector alert independently" scenario:
    157 IF-only alerts on clean data    (false positives)
    167 LOF-only alerts on clean data   (false positives)
    209 IQR-only alerts on clean data   (false positives)
    ------
    533 false alerts
  vs only 23 real anomalies caught by exactly one detector

  That's a 23:1 noise ratio. Nobody would trust the system.

  With the 2+ voting rule:
    60 total false positives
    188 true detections
    76% of alerts are real   (188 / 248 total alerts)
```

> Reproducible from `poc/validate_detection.py` — deterministic (`random.seed(42)`), 5000 scored events, 209 injected anomalies.


**The tradeoff we accepted:** we lose the ~6% of anomalies that only one detector catches. In exchange, false positives drop by nearly 10x. If an operator ignores the system because it cries wolf, catching that extra 6% was worthless anyway. This is the single biggest design call we made.

> **Note for engineers:** if you watch the CLI output and think "this alert is garbage, I'd never have flagged that" — **tell us**. Every false positive you call out is a data point we can use to tune voting thresholds or identify bad features.

---

## 6. System Architecture — One Slide

```
  Implants  ---+
               |
               v                       (swap this for the real implant
        +-------------+                 backchannel in production)
        |  Generator  |  <-- synthetic data for the PoC
        +------+------+
               |
               v
           +-------+             +-------------+
           | Kafka |             |  PostgreSQL |  raw + features + alerts
           +---+---+             +------+------+
               |                        ^
               v                        |
        +-------------+                 |
        |  Ingestor   | ---> writes ----+
        +------+------+                 |
               |                        |
               | also pushes feature    |
               | vectors to sliding     |
               | windows in Redis       |
               v                        |
           +-------+                    |
           | Redis |                    |
           +---+---+                    |
               |                        |
               | baselines cached as    |
               | (fences + trained      |
               |  models) per scope     |
               v                        |
        +-------------+                 |
        |  Detector   | ---- reads -----+
        +------+------+
               |
               v
          CLI output
          (every flagged config with
           scope tag, driving features,
           SHAP explanation)
```

### What each piece does, and why

**Kafka** — decouples the implant backchannel from our processing. In production, the implant backchannel publishes to Kafka; we consume from it. If the detector dies, no data is lost — it's queued. If we want to add a second consumer (e.g. a dashboard), we add one without touching the ingestor.

**PostgreSQL** — durable store of raw telemetry + extracted features + ground truth (for PoC evaluation). Also where the detector reads "unscored" rows from.

**Redis** — two roles:
1. **Sliding windows** of raw feature vectors per scope. Key format: `window:{scope}:{scope_id}:{config_type}`. Example: `window:group:ALPHA:communication_configuration`. These are the training material.
2. **Cached trained models** per scope. IQR fences + serialized IsolationForest + LOF + Mahalanobis + StandardScaler, all packed into one Redis hash. Loading a model from Redis is ~0.1ms; loading from Postgres would be ~5ms. With up to 90 models in play (5 config types × 15 implants = 75 per-implant models, plus 5 config types × 3 groups = 15 per-group models), that matters.

**Detector** — runs in a tight loop. Every tick it:
1. Checks which sliding windows have grown by more than `RETRAIN_THRESHOLD` (100) new vectors and **retrains** those baselines.
2. Reads unscored telemetry rows, loads the cached models for their scope, scores them, prints detections.

The retrain-while-scoring design is why baselines stay fresh without a separate training pipeline.

---

## 7. Where Implant Engineers Fit In — The Ask

Two concrete buckets of work. These are what I need you for.

### 7.1 Feature engineering — the big lever

For every config type, we need to keep asking:

1. **What properties of this config should be consistent across normal operation?** Those are candidate features.
2. **What pairs or triples of fields should move together?** Those become ratio/product features.
3. **What combinations are physically impossible in a well-formed config?** Those are the gold — Mahalanobis and IF will nail them.

Example conversations I want to have with you:

> **Me:** "For `persistence_configuration`, we extract `total_persistence_footprint = registry_key_count + scheduled_task_count`. Good idea?"
>
> **You:** "Not quite. Those aren't fungible — 5 registry keys is cheap, 5 scheduled tasks is loud. You want `weighted_footprint = registry_key_count + 3*scheduled_task_count`, because scheduled tasks are more visible to defenders."
>
> **Me:** "Adding it."

That's the loop. We'll spend a full session per config type.

### 7.2 Detection review — the feedback loop

When we run the detector and it fires an alert, we need someone who can look at the raw config and answer:

- "Yes, this is a real mistake I'd want to know about." → **true positive, feature is working**
- "No, this is a weird but valid config." → **false positive, we need to understand why**
- "This is actually a mistake, but not the kind the alert is describing." → **detection works, explanation is misleading, fix the SHAP output or feature names**

Without that feedback loop, we can't tune. You are the ground truth.

---

## 8. Glossary — Things They'll Hear Today

- **Baseline** — the set of historical feature vectors we learn "normal" from. Scoped per `(implant, config_type)` or per `(group, config_type)`.
- **Feature vector** — the flat `dict[str, float]` we extract from a config, e.g. for `[GROUP: ALPHA]`'s `communication_configuration` there are ~13 features.
- **Scope** — either an implant (`[IMPLANT: ALPHA_003]`) or a group (`[GROUP: ALPHA]`). Every detection is tagged with which scope's baseline flagged it.
- **Cold start** — the period (first 7 days) when an implant has too little data for its own baseline, so we use the group's.
- **Fence** — the lower/upper bound IQR draws around a feature. Outside the fence → flagged.
- **Contamination** — the expected fraction of anomalies in training data (we set 2% — conservative assumption that baselines aren't 100% clean).
- **SHAP value** — per-feature contribution to an ML model's output. We use it to explain which features drove an IF decision.
- **Ground truth** — in the PoC, we know which events are anomalies because we injected them. In production, you are the ground truth.

---

## 9. What I'm NOT Going to Cover Today

- Math of Isolation Forest tree construction, SHAP's Shapley-value derivation, Mahalanobis via precision matrix inversion. If anyone asks, I'll happily nerd out after — but it's not load-bearing for feature engineering.
- Exact config constants (IQR multiplier, LOF neighbor count, etc.). These are in `config.py`, all tunable, none of them magic.
- Production scaling (Kafka cluster, ClickHouse migration). Out of scope for the PoC; we're building to demo, not to deploy.

---

## 10. Demo Walkthrough (if time permits)

Run `python run.py` from `poc/`. Three tmux panes:

1. **Generator** (bottom-left) — emits baseline data fast, then live data with ~4% anomalies injected.
2. **Ingestor** (top-left) — consumes Kafka, extracts features, writes to Postgres + Redis.
3. **Detector** (right) — scores every event, prints alerts.

When the first HIGH alert appears, pause and walk through the panel:

```
+---- HIGH [IMPLANT: ALPHA_003] (ALPHA) communication_configuration ----+
|                                                                        |
|  IQR Deviations [FLAGGED]                                              |
|    beacon_interval_ms  1597  (expected 17000-43000, median 29900)      |
|                                                                        |
|  SHAP Contributions:                                                   |
|    beacon_interval_ms    -0.623  <-- drove the IF decision             |
|    sleep_on_failure_ms   -0.367                                        |
|                                                                        |
|  IF:  0.995 [FLAGGED]                                                  |
|  LOF: 1.000 [FLAGGED]                                                  |
|  Mahal p: 0.0e+00 [FLAGGED]                                            |
|                                                                        |
+----------------------- TP: beacon_storm ------------------------------+
```

Talking points, in order:
1. **Scope tag:** "`[IMPLANT: ALPHA_003]` — this was flagged against the per-implant baseline, not the group. ALPHA_003 has enough history to have its own."
2. **IQR row:** "That's the interpretable part. Expected range, actual value. You could have written this detection rule by hand, but we didn't have to."
3. **SHAP row:** "These are the two features IF thought mattered most. If you were reviewing this alert, these are the fields to look at first."
4. **Three `[FLAGGED]` votes:** "IF, LOF, and Mahalanobis all agreed. Plus IQR. 4/4 votes — this is a high-confidence detection."
5. **`TP: beacon_storm`:** "Ground truth says this was actually anomalous. Injected by our generator. The tag `beacon_storm` is the name of the injector."

Then scroll to a `[LOW]` alert (SHAP-only, no IQR breach) and show how the per-feature story changes.

---

## 11. Closing — The One Thing I Want Them to Remember

> **"The algorithms are mostly commodity. The features are the product. If you want this system to catch the mistakes that matter to you, sit with us and tell us what properties of a normal config you'd check by eye. That's what we turn into features. Everything else follows."**
