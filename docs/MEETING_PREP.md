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

## 3. Bridging from IQR to the Full System

Since he understands IQR, build from there:

```
What he knows:                    What's new:
+------------------+              +----------------------------------+
|                  |              |                                  |
|  IQR             |              |  Isolation Forest                |
|  "Is this value  |              |  "Is this combination of values  |
|   outside its    |  --------->  |   unusual, even if each value    |
|   normal range?" |              |   individually looks fine?"      |
|                  |              |                                  |
|  Per-feature,    |              |  + LOF (sparse neighborhood?)    |
|  independent     |              |  + Mahalanobis (breaks expected  |
|                  |              |    correlations?)                |
+------------------+              +----------------------------------+

IQR catches:                      ML catches:
  beacon_interval = 1200            beacon = 35000 (normal alone)
  (obviously wrong)                 + sleep = 5000 (normal alone)
                                    = combination never seen before
```

> "IQR catches the obvious stuff — a value way outside its range. But some mistakes don't show up on any single feature. Like applying a template from the wrong campaign: every value looks normal, but the combination is wrong. That's what the ML models add."

---

## 4. How the Voting Works

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

  Why 2+ votes?
  Single-detector alerts are 93% noise (measured).
  Requiring agreement drops false positives dramatically.
```

---

## 5. Key Numbers (One-Page Cheat Sheet)

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

## 6. Anticipated Questions + Answers

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

---

## 7. What You Want Out of the Meeting

Decide before going in which of these you're asking for:

- [ ] **Approval to start Phase 1** (connect real telemetry)
- [ ] **Resources** (how many people, what infrastructure)
- [ ] **Stakeholder intro** (who else needs to see this)
- [ ] **Feedback** (what would make this more convincing)

---

## 8. Things NOT to Say

- Don't say "machine learning" without immediately following with what it does in plain language
- Don't say "Isolation Forest" without saying "it checks if a data point is easy to isolate from the crowd"
- Don't defend the 60% self_destruct_flood rate — acknowledge it, say "that's the hardest type, and we're at 90% overall"
- Don't promise specific numbers on real data — say "the architecture is proven, the numbers need validation"
- Don't get pulled into algorithm details — redirect to "I can walk you through the technical doc after, but the key point is..."
