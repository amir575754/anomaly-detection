# Manager Meeting Prep — Anomaly Detection PoC

> **Context:** Manager understands IQR and basic feature extraction. No ML background. Goal: show the PoC works, explain what it proves, get buy-in for next phase.

---

## 1. Opening (30 seconds)

> "We built a system that catches operator mistakes in implant configurations — automatically, without writing rules for each type of mistake. It learns what 'normal' looks like from historical data and flags anything that deviates. I'll show you a live demo, then we'll talk about what it proves and what's next."

---

## 2. Demo Script (~2 minutes)

Run `python run.py` from the `poc/` directory. While it loads:

### Step 1: Explain the layout

> "Three panes: Generator creates telemetry, Ingestor writes it to the database, Detector scores every event in real time."

```
+---------------------------+---------------------------+
|        INGESTOR           |                           |
|   (Kafka -> DB + Redis)   |        DETECTOR           |
+---------------------------+   (scoring + alerts)      |
|        GENERATOR          |                           |
|   (baseline + live data)  |   <-- detections appear   |
+---------------------------+---------------------------+
```

**KEY ARCHITECTURE POINT:** This layout mirrors a production system. Kafka decouples the data source from processing — swap the Generator for real telemetry feeds and the rest stays the same. PostgreSQL stores everything durably, Redis provides sub-millisecond model access for real-time scoring.

### Step 2: Walk through a detection panel

When the first HIGH panel appears, pause and explain each section:

```
+---- HIGH implant_ALPHA_003 (ALPHA) communication_configuration ----+
|                                                                     |
|  IQR Deviations [FLAGGED]              <-- "You know this part"     |
|    beacon_interval_ms  1597                                         |
|    (expected 17000-43000, median 29900)                             |
|                                                                     |
|  SHAP Contributions:                   <-- "Which features drove    |
|    beacon_interval_ms  -0.623 anomalous     the ML decision"        |
|    sleep_on_failure_ms -0.367 anomalous                             |
|                                                                     |
|  IF: 0.995 [FLAGGED]                  \                             |
|  LOF: 1.000 [FLAGGED]                  |-- "3 algorithms agree"     |
|  Mahal p: 0.0e+00 [FLAGGED]           /                             |
|                                                                     |
+----------------------- TP: beacon_storm ----------------------------+
                         ^-- "Ground truth confirms: real anomaly"
```

Talking points:
- "IQR is what you already know — single-feature outlier detection."
- "The `[FLAGGED]` tags show which algorithms voted. We need at least 2 to agree."
- "SHAP tells us *why* the ML models flagged it — not a black box."
- "TP means True Positive — our injected anomaly was correctly caught."

### Step 3: Walk through the summary table

> "Here's the scorecard: 90% detection rate, 76% precision, every type of mistake detected."

---

## 3. Feature Engineering — The Most Important Part

> **KEY POINT TO HAMMER HOME:** The quality of detection depends more on choosing the right features than on which algorithm we use. We spent significant effort here.

### What makes a good feature

Features capture **operational meaning** — not raw data and not anomaly signatures:

```
  RAW DATA                 FEATURES WE EXTRACT           WHY IT MATTERS
  (what the config says)   (what it means operationally)

  beacon: 30000ms    -->   beacon_to_sleep_ratio: 3.0    "When beacon is high,
  sleep:  10000ms                                          sleep is always high
                                                           too. If they diverge,
                                                           something is wrong."

  programs: 5 entries -->  posture_consistency: 0.9       "Programs and drivers
  drivers: 3 entries       action_diversity: 3              normally share the
  (with actions)           dominant_action_ratio: 0.5       same response policy.
                                                           Contradiction = mistake."

  evasion flags:     -->   dependency_coherence: 1.0      "Advanced techniques are
  6 boolean fields         depth_per_enabled: 0.8           normally enabled WITH
                                                           their prerequisites.
                                                           Skipping basics = mistake."
```

### What we DON'T do (and why it matters)

> **This is critical for staying true to unsupervised anomaly detection:**

```
  +---------------------------------------------------------------+
  |                                                               |
  |  WE DO NOT encode anomaly knowledge into features.            |
  |                                                               |
  |  WRONG:  "is_all_self_destruct" (encodes the exact anomaly)   |
  |  RIGHT:  "dominant_action_ratio" (measures general property)  |
  |                                                               |
  |  WRONG:  "beacon_below_5000" (a rule disguised as a feature)  |
  |  RIGHT:  "beacon_to_sleep_ratio" (captures the relationship)  |
  |                                                               |
  |  If features resemble the anomalies we're trying to detect,   |
  |  we've drifted from unsupervised detection into rule-writing  |
  |  with extra steps.                                            |
  |                                                               |
  +---------------------------------------------------------------+
```

### One-hot encoding for categorical features

> "For persistence, the implant uses exactly one method (registry_run, scheduled_task, etc.). Each method type has characteristic infrastructure — registry_run uses registry keys, scheduled_task uses tasks. We encode the method as 5 binary flags so the ML models can learn per-method expectations. Then if a registry_run method shows up with scheduled_task infrastructure, the models flag it."

```
  method = "registry_run"

  Extracted features:
    method_registry_run     = 1    <-- one-hot encoding
    method_scheduled_task   = 0
    method_service_install  = 0
    method_startup_folder   = 0
    method_wmi_subscription = 0
    registry_key_count      = 3    <-- expected for this method
    scheduled_task_count    = 0    <-- expected for this method

  ANOMALY (duplicated_persistence_setup):
    method_registry_run     = 1    <-- says "registry_run"
    registry_key_count      = 0    <-- but NO registry keys??
    scheduled_task_count    = 4    <-- and lots of tasks??
    --> method-to-infrastructure mismatch, caught by ML
```

---

## 4. How Each Algorithm Works (Plain English)

### IQR — "Is this value outside its normal range?"

> He knows this. Use as the anchor point.

```
  Training data for beacon_interval:
  [25000, 27000, 28000, 29000, 30000, 31000, 32000, 33000, 35000]
       |          Q1=27500                Q3=32500          |
       |          |<------  IQR = 5000  ------>|            |
       |          |                             |            |
       Lower fence = Q1 - 2*IQR = 17500        Upper = 42500

  New event: beacon = 1597
  --> WAY below lower fence --> FLAGGED (2.3 IQR units outside)
```

**Strength:** Interpretable — "expected 17000-43000, got 1597" makes instant sense.
**Limitation:** Checks features independently. Can't see that beacon=35000 + sleep=5000 is unusual as a *combination*.

### Isolation Forest — "Is this point easy to isolate from the crowd?"

> "Imagine a room full of people. Most cluster in groups. If you can separate someone from everyone else with just one or two questions ('are you taller than 6ft?' 'are you wearing red?'), they're the outlier. Normal people need many questions to distinguish."

```
  Normal configs cluster together:         Anomaly is isolated quickly:

       . . . .                                 . . . .
     . . . . . .                             . . . . . .
    . . . . . . .      vs                  . . . . . . .
     . . . . . .                             . . . . . .
       . . . .                                 . . . .

                                                                X  <-- 1 split
                                                                       isolates it
```

**Strength:** Catches multi-feature anomalies that IQR misses.
**Limitation:** Doesn't explain *why* — that's what SHAP adds.

### LOF — "Is this point in a sparse neighborhood?"

> "Look at the 20 nearest configs to this one. Are they close together (dense neighborhood = normal) or far apart (sparse = unusual)? LOF measures local density vs neighbor density."

```
  Dense neighborhood (normal):     Sparse neighborhood (anomalous):

    . . .                            .
    . X .    <-- X has many               .
    . . .        close neighbors              X    <-- X's neighbors
                                           .       are far away
                                      .
```

**Strength:** Catches "local" outliers — a config that's unusual *for its cluster* even if it's not globally extreme.
**Limitation:** Needs scaled features (we use StandardScaler) so that all features contribute equally to distance.

### Mahalanobis — "Does this break expected correlations?"

> "Imagine plotting beacon_interval vs sleep_on_failure for 1000 configs. They form an oval because when beacon goes up, sleep goes up. Mahalanobis measures distance from the center using the oval's shape as the ruler."

```
  sleep_on_failure
       |         . . .
       |       . . . . .
       |     . . X . . . .       X = center
       |       . . . . .
       |         . . .
       +---+---+---+---+---
           beacon_interval

  Point A: high beacon + high sleep   = follows the oval    = p=0.15 (normal)
  Point B: high beacon + LOW sleep    = cuts across the oval = p=0.001 (anomalous)

  Same distance from center, but Mahalanobis knows B is suspicious.
```

**Strength:** Catches correlation violations — each feature normal individually, combination is unusual.
**Measured performance:** 77.8% of anomalies have p < 0.001 vs only 0.6% of clean data.

---

## 5. The Voting Tradeoff — Why We Require 2+ Algorithms to Agree

> **KEY DESIGN DECISION. Be ready to explain this clearly.**

```
                    +-------+
                    | Event |
                    +---+---+
                        |
          +-------------+-------------+-------------+
          |             |             |             |
      +---v---+   +----v----+   +----v---+   +----v--------+
      |  IQR  |   |   IF    |   |  LOF   |   | Mahalanobis |
      | fence |   | predict |   | predict|   |  p-value    |
      | check |   |         |   |        |   |  < 0.01?    |
      +---+---+   +----+----+   +----+---+   +----+--------+
          |             |             |             |
          v             v             v             v
        vote?         vote?         vote?         vote?
          |             |             |             |
          +-------------+-------------+-------------+
                        |
                   2+ votes?
                   /        \
                 YES          NO
                  |            |
              +---v---+   +---v------+
              | ALERT |   | suppress |
              +-------+   +----------+
```

### Why not let each algorithm alert independently?

> "We measured this. Single-algorithm alerts are **93% noise:**"

```
  If we let every single-algorithm alert through:
  +---------------------------------------------------+
  |  Single-detector events:                           |
  |    157 IF-only on clean data     (false positives) |
  |    167 LOF-only on clean data    (false positives) |
  |    209 IQR-only on clean data    (false positives) |
  |    ---                                             |
  |    533 false alerts                                |
  |                                                    |
  |  vs only 23 real anomalies caught by 1 detector    |
  |                                                    |
  |  That's a 23:1 noise ratio. Unacceptable.          |
  +---------------------------------------------------+

  With 2+ vote requirement:
  +---------------------------------------------------+
  |  Only 58 false positives total                     |
  |  188 true detections                               |
  |  76% of alerts are real                            |
  +---------------------------------------------------+
```

### The tradeoff we accepted

> "We lose ~6% of anomalies (23 out of 377 in testing) that only one detector catches. In exchange, we cut false positives from 533 to 58. That's the right call when alert fatigue is a bigger risk than missing a marginal detection."

### One exception: IQR significant

> "IQR gets special treatment. If it finds 2+ features with extreme deviations, that alone triggers MEDIUM — no ML confirmation needed. Because at that point the evidence is directly interpretable and multi-dimensional."

---

## 6. Key Architectural Choices

### Why Kafka? (not direct DB writes)

```
  WITHOUT Kafka:                    WITH Kafka:
  Generator --> PostgreSQL          Generator --> Kafka --> Ingestor --> PG
  (tight coupling)                  (decoupled)

  Problems:                         Benefits:
  - Generator blocks on slow DB     - Generator never blocks
  - Can't replay data               - Can replay from any offset
  - Can't add consumers             - Add new consumers without
                                      touching the generator
```

### Why Redis? (not just PostgreSQL)

```
  For the detector, speed matters:

  PostgreSQL (models in DB):        Redis (models in cache):
  ~1-5ms per model load             ~0.1ms per model load
  75 models = 75-375ms              75 models = 7.5ms

  Redis also gives us sliding windows natively:
    RPUSH window:group:ALPHA:comm  '{"beacon": 30000, ...}'
    LTRIM window:group:ALPHA:comm  -1000 -1       <-- auto-cap at 1000
```

### Why two baseline scopes? (group + per-implant)

```
  GROUP baseline:                   PER-IMPLANT baseline:
  +---------------------------+     +---------------------------+
  | All implants in ALPHA     |     | Just implant_ALPHA_003    |
  | pooled together           |     | individual behavior       |
  | 1000 training samples     |     | 300 training samples      |
  +---------------------------+     +---------------------------+
  | Detection:  82%           |     | Detection:  86%           |
  | FP rate:    22%           |     | FP rate:    22% (at 300   |
  | Always available          |     |   samples, noisy before)  |
  +---------------------------+     +---------------------------+

  We use group during cold start (first 7 days), then switch
  to per-implant once enough data accumulates.
```

### Why synthetic data? (honest caveat)

```
  +---------------------------------------------------------------+
  |                                                               |
  |  ALL data in this PoC is synthetic.                           |
  |                                                               |
  |  What's hand-picked:                                          |
  |    - Probability distributions (55% enable obfuscate, etc.)   |
  |    - Feature correlations (beacon drives jitter/sleep/etc.)   |
  |    - Anomaly injectors (our guess at operator mistakes)       |
  |                                                               |
  |  What this PROVES:                                            |
  |    - The architecture works end-to-end                        |
  |    - The algorithms CAN achieve 90% detection / 24% FP       |
  |    - The approach is viable                                   |
  |                                                               |
  |  What this DOESN'T prove:                                     |
  |    - That these specific numbers transfer to real data        |
  |    - That our assumed correlations exist in real telemetry    |
  |    - That real operator mistakes look like our injectors      |
  |                                                               |
  |  Phase 1 answers these questions with shadow-mode on          |
  |  real telemetry.                                              |
  |                                                               |
  +---------------------------------------------------------------+
```

---

## 7. Key Numbers (One-Page Cheat Sheet)

```
+================================================================+
|            ANOMALY DETECTION PoC - KEY NUMBERS                  |
+================================================================+
|                                                                 |
|  DETECTION RATE        90.0%     188 / 209 anomalies caught    |
|  FALSE POSITIVE RATE   23.6%     58 false / 246 total alerts   |
|  PRECISION             76.4%     ~3 out of 4 alerts are real   |
|  ANOMALY TYPES         10        all detected at 60-100%       |
|  DEMO TIME             ~70s      from start to full results    |
|                                                                 |
+-----------------------------------------------------------------+
|                                                                 |
|  PER-INJECTOR DETECTION                                         |
|                                                                 |
|  beacon_storm              100%  ======================== EASY  |
|  zero_jitter               100%  ========================       |
|  capability_explosion      100%  ========================       |
|  persistence_spike         100%  ========================       |
|  forgotten_op_teardown     100%  ========================       |
|  wrong_comm_profile        100%  ========================       |
|  duplicated_persistence    100%  ======================== MED   |
|  mismatched_escalation      90%  ======================         |
|  full_evasion               67%  ================         HARD  |
|  self_destruct_flood        60%  ===============                |
|                                                                 |
+-----------------------------------------------------------------+
|                                                                 |
|  4 DETECTION ALGORITHMS (need 2+ to agree)                      |
|                                                                 |
|   IQR            "value outside normal range?"     (he knows)   |
|   Iso Forest     "easy to isolate from the crowd?"              |
|   LOF            "sparse neighborhood?"                         |
|   Mahalanobis    "breaks expected correlations?"                |
|                                                                 |
+-----------------------------------------------------------------+
|                                                                 |
|  WHAT'S SYNTHETIC (needs real-data validation)                  |
|                                                                 |
|   - All probability distributions (hand-picked)                 |
|   - Feature correlations (designed, not observed)               |
|   - Anomaly injectors (our guess at operator mistakes)          |
|   - Detection rates WILL change on real telemetry               |
|                                                                 |
+================================================================+
```

---

## 8. Anticipated Questions + Answers

### "How do you know it works on real data?"

> "Honestly, we don't yet. Everything was validated on synthetic data with hand-picked distributions. The architecture and algorithms are proven, but the specific numbers will change on real telemetry. Phase 1 is connecting to real data and running in shadow mode."

### "What's the false positive rate?"

> "24% of alerts are false positives — so about 3 out of 4 alerts are real. We got there by requiring multiple algorithms to agree. We measured that single-algorithm alerts are 93% noise, so suppressing those was the key design decision."

### "Can't we just write rules?"

> "Rules work for known mistakes. But we'd need a new rule for every failure mode, and they don't adapt when behavior changes. This system catches mistakes we never defined."

```
  Rules:                          This system:
  "if beacon < 5000 -> alert"     "this combination of features
  "if all self_destruct -> alert"  has never been seen in
  "if jitter = 0 -> alert"        10 days of baseline data"
  "if ..."                         (no rules to maintain)
  "if ..."
  (grows forever)
```

### "What does it miss?"

> "The hardest cases: every feature individually within range, only the combination is unusual. Our weakest detection is 60% (self_destruct_flood). But 8 out of 10 types are caught at 90-100%."

### "How fast is it?"

> "70 seconds from cold start to full detection results in the demo. In production, the detector scores events continuously as they arrive — sub-second per event."

### "What's the timeline to production?"

```
  Phase 1 (weeks 1-4):   Connect real telemetry, shadow mode
  Phase 2 (weeks 5-8):   Operator feedback loop, tune thresholds
  Phase 3 (weeks 9-12):  Scale (Kafka cluster, ClickHouse)
  Phase 4 (weeks 13-16): NOC dashboard, alert deduplication
```

### "Why four algorithms? Isn't that overkill?"

> "Each one catches different things. IQR catches obvious single-feature outliers. Isolation Forest catches complex multi-feature patterns. LOF catches things that are unusual relative to their local neighborhood. Mahalanobis catches correlation violations. No single algorithm covers all cases — we measured this. But requiring 2+ to agree gives us low false positives."

---

## 9. What You Want Out of the Meeting

Decide before going in which of these you're asking for:

- [ ] **Approval to start Phase 1** (connect real telemetry)
- [ ] **Resources** (how many people, what infrastructure)
- [ ] **Stakeholder intro** (who else needs to see this)
- [ ] **Feedback** (what would make this more convincing)

---

## 10. Things NOT to Say

- Don't say "machine learning" without immediately following with what it does in plain language
- Don't say "Isolation Forest" without saying "it checks if a data point is easy to isolate from the crowd"
- Don't defend the 60% self_destruct_flood rate — acknowledge it, say "that's the hardest type, and we're at 90% overall"
- Don't promise specific numbers on real data — say "the architecture is proven, the numbers need validation"
- Don't get pulled into algorithm details — redirect to "I can walk you through the technical doc after, but the key point is..."
- Don't oversell features as "intelligent" — say "the features measure operational properties, the algorithms do the math"
