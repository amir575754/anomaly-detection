# Anomaly Detection Algorithms — A Newcomer's Guide

> **Audience:** Engineers, data scientists, and analysts who are new to anomaly detection and want to understand the algorithms, intuition, and tradeoffs behind this system.

---

## Table of Contents

1. [What Is Anomaly Detection?](#1-what-is-anomaly-detection)
2. [Supervised vs Unsupervised — And Why We Chose Unsupervised](#2-supervised-vs-unsupervised--and-why-we-chose-unsupervised)
3. [Our Feature Engineering Philosophy](#3-our-feature-engineering-philosophy)
4. [Algorithm 1: IQR Fences (Interquartile Range)](#4-algorithm-1-iqr-fences-interquartile-range)
5. [Algorithm 2: Isolation Forest](#5-algorithm-2-isolation-forest)
6. [Algorithm 3: Local Outlier Factor (LOF)](#6-algorithm-3-local-outlier-factor-lof)
7. [Algorithm 4: Mahalanobis Distance](#7-algorithm-4-mahalanobis-distance)
8. [How We Combine Them — Ensemble Voting](#8-how-we-combine-them--ensemble-voting)
9. [Explainability — SHAP Values](#9-explainability--shap-values)
10. [Severity Classification](#10-severity-classification)
11. [The Cold-Start Problem](#11-the-cold-start-problem)
12. [Glossary](#12-glossary)

---

## 1. What Is Anomaly Detection?

Anomaly detection is the process of identifying data points that don't conform to the expected pattern. Think of it like this: if you've observed someone's daily routine for a month — they wake up at 7am, drive to work, return at 6pm — and one day they're driving at 3am toward a location they've never visited, that's an anomaly.

In our context, we monitor **implant configurations** — structured snapshots describing how an implant is configured at a point in time. Each snapshot contains settings like beacon intervals, evasion technique flags, persistence methods, and more. Normal operations produce predictable patterns in these configurations. When an operator makes a mistake — applying the wrong template, forgetting to disable a surveillance mode, panic-loading a blanket kill list — the configuration deviates from what we've learned to be "normal."

Our job is to catch these deviations **without being told in advance what mistakes look like.** This is the key constraint: we don't have labeled examples of "here's a mistake" and "here's normal." We only have historical data that we assume is predominantly clean.

---

## 2. Supervised vs Unsupervised — And Why We Chose Unsupervised

### Supervised Learning (Not Used Here)

In supervised learning, you give the algorithm labeled examples: "this is normal" and "this is an anomaly." The algorithm learns the boundary between the two classes. Examples include logistic regression, random forests for classification, and neural network classifiers.

**Why we didn't use it:**
- We don't have labeled anomaly data. Real operator mistakes are rare and diverse.
- New types of mistakes will appear that we've never seen before. A supervised model can only recognize patterns it was trained on.
- Labeling data requires domain experts to manually review thousands of events — not scalable.

### Unsupervised Learning (Our Approach)

In unsupervised anomaly detection, the algorithm learns what "normal" looks like from unlabeled historical data, then flags anything that deviates significantly. It doesn't need to know what anomalies look like — only what normal looks like.

**Why this fits our problem:**
- We have plenty of clean historical data (the baseline phase generates it).
- We want to catch **novel** mistakes — things we've never explicitly defined as anomalous.
- The system should be self-tuning: as normal behavior evolves, the baselines update automatically.

### The Tradeoff

Unsupervised detection is inherently harder than supervised. Without knowing what anomalies look like, the algorithm must make judgments based purely on statistical distance from normalcy. This means:
- **Some anomalies are subtle** — they look normal on every individual feature but are anomalous in combination. These are hard to catch.
- **Some normal data looks unusual** — a legitimate but rare configuration might trigger a false alarm.
- **Precision matters** — if you alert on everything slightly unusual, operators will ignore the alerts (alert fatigue).

This is why we use multiple algorithms instead of one — they each see different types of "unusual." Three algorithms actively vote on severity (IQR, Isolation Forest, LOF), while a fourth (Mahalanobis distance) provides diagnostic context displayed in alert panels.

---

## 3. Our Feature Engineering Philosophy

> **PoC Assumption — Synthetic Data**
>
> All "typical ranges," "baseline correlations," and "normal behavior" described below are properties of the **synthetic data generator** (`profiles.py`), not observations from real implant telemetry. The generator uses hand-picked probability distributions and correlation structures that are plausible but unvalidated against real operational data. Specific assumptions to be aware of:
>
> - **Probability values** (e.g., "55% of configs enable obfuscate_strings," "35% surveillance posture") were chosen by the PoC authors to create realistic-looking data, not estimated from real operator behavior.
> - **Correlation structures** (beacon drives jitter/retries/sleep, method count drives registry/task counts, evasion techniques follow a layered dependency chain) are intentional design choices that give the ML models learnable patterns. Real telemetry may have different, weaker, or additional correlations.
> - **Feature ranges** reflect the generator's clamped random distributions. Real telemetry ranges may be wider, narrower, or multimodal.
> - **Algorithm parameters** (IQR multiplier=2.0, contamination=0.02, 500 IF trees, etc.) were tuned to perform well on this synthetic data. They will need re-tuning on real data.
> - **Detection rates** (82.1% detection, 28.9% FP) are measured against synthetic anomalies. Real operator mistakes may be subtler or more diverse.
>
> When moving to production, the synthetic generator should be replaced by real telemetry, and all thresholds and correlations should be re-evaluated.

Before any algorithm runs, raw configuration snapshots are transformed into **feature vectors** — flat lists of numbers that capture the operational semantics of a configuration. This transformation is critical because algorithms work on numbers, not nested JSON.

### Design Principle: Capture Operational Meaning, Not Anomaly Patterns

Our features are designed to measure general operational properties:
- **Ratios:** What fraction of programs are set to self-destruct? (Not: "is this the self_destruct_flood anomaly?")
- **Consistency scores:** Do programs and drivers share the same response posture? (Not: "is programs=audit and drivers=do_nothing?")
- **Dependency coherence:** Are enabled evasion techniques supported by their prerequisites?
- **Cross-feature interactions:** What's the ratio of beacon interval to sleep-on-failure time?

If our features started encoding specific anomaly signatures (like "is_all_self_destruct"), we'd be doing rule-based detection disguised as ML — defeating the purpose of unsupervised learning.

### Feature Catalogue

Below is every feature the system extracts, grouped by configuration type. Each feature is a single floating-point number. All features are passed through `StandardScaler` (mean=0, std=1) before reaching the ML models (IF, LOF, SHAP). IQR operates on the raw values by design.

#### Communication Configuration (12 features)

| Feature | Description | Typical Range |
|---|---|---|
| `beacon_interval_ms` | Milliseconds between beacons sent by the implant. The primary timing parameter — higher values are stealthier. | 25,000 – 35,000 |
| `jitter_percentage` | Randomization applied to the beacon interval (0.0 = perfectly predictable, 1.0 = fully random). Defenders look for predictable intervals. | 0.10 – 0.25 |
| `max_retries` | How many times the implant retries a failed C2 connection before sleeping. | 3 – 7 |
| `sleep_on_failure_ms` | Backoff duration in milliseconds after all retries are exhausted. | 5,000 – 15,000 |
| `key_rotation_hours` | How frequently the encryption key is rotated. Lower = more secure but more traffic. | 12 – 48 |
| `c2_channel_count` | Total number of C2 channels configured (active + inactive). | 1 – 3 |
| `c2_enabled_count` | How many of those C2 channels are currently enabled. | 0 – 3 |
| `c2_unique_protocols` | Number of distinct protocols across all channels (https, dns, smb, etc.). | 1 – 3 |
| `c2_non_standard_ports` | Count of channels using ports other than 80, 443, or 53. Non-standard ports are more likely to be flagged by network monitoring. | 0 – 3 |
| `beacon_to_sleep_ratio` | `beacon_interval_ms / sleep_on_failure_ms`. Captures the operational relationship between normal timing and failure recovery. In the synthetic baseline data this ratio is stable because the generator derives both values from a single normalized beacon parameter. Real telemetry should be checked for whether this correlation holds. | 2.0 – 5.5 |
| `beacon_to_rotation_ratio` | `beacon_interval_ms / key_rotation_hours`. Captures how beacon timing relates to cryptographic freshness. | 700 – 2,300 |
| `jitter_retries_product` | `jitter_percentage * max_retries`. Captures the interaction between randomization and resilience — high jitter with high retries means a very different failure profile than low jitter with low retries. | 0.5 – 1.3 |

#### Dangerous Program Configuration (15 features)

| Feature | Description | Typical Range |
|---|---|---|
| `program_count` | Number of dangerous programs in the watchlist (e.g., wireshark.exe, procmon.exe). | 2 – 5 |
| `driver_count` | Number of dangerous drivers in the watchlist (e.g., WdFilter.sys, csagent.sys). | 1 – 4 |
| `total_entries` | `program_count + driver_count`. Overall watchlist size. | 3 – 9 |
| `prog_audit_count` | Programs with action set to "audit" (monitor but don't react). | 0 – 5 |
| `prog_self_destruct_count` | Programs with action set to "self_destruct" (wipe the implant if detected). | 0 – 5 |
| `prog_do_nothing_count` | Programs with action set to "do_nothing" (ignore even if detected). | 0 – 5 |
| `drv_audit_count` | Drivers with action set to "audit". | 0 – 4 |
| `drv_self_destruct_count` | Drivers with action set to "self_destruct". | 0 – 4 |
| `drv_do_nothing_count` | Drivers with action set to "do_nothing". | 0 – 4 |
| `overall_audit_ratio` | Fraction of all entries (programs + drivers) set to "audit". | 0.0 – 1.0 |
| `overall_self_destruct_ratio` | Fraction of all entries set to "self_destruct". | 0.0 – 1.0 |
| `overall_do_nothing_ratio` | Fraction of all entries set to "do_nothing". | 0.0 – 1.0 |
| `posture_consistency` | L1 similarity between the program action distribution and the driver action distribution. Returns 1.0 when both groups have identical ratios, 0.0 when completely opposite. In normal operations, programs and drivers share the same dominant action (e.g., both mostly "audit"). | 0.0 – 1.0 |
| `action_diversity` | Number of distinct actions present across all entries. Normal configs have 2–3 distinct actions; a value of 1 means every entry has the same action. | 1 – 3 |
| `dominant_action_ratio` | Fraction of all entries assigned to the most common action. Higher values mean a more uniform (less diverse) policy. | 0.33 – 1.0 |

#### Persistence Configuration (11 features)

Operational implants use a single persistence method. The features capture which method is used (one-hot encoded) and the infrastructure that supports it. Each method type has characteristic infrastructure — a `registry_run` method normally has 2–4 registry keys and 0 scheduled tasks, while a `scheduled_task` method has 0–1 registry keys and 1–3 tasks. These per-method ranges are hand-picked in the synthetic generator (`profiles.py`) and should be verified on real data.

| Feature | Description | Typical Range |
|---|---|---|
| `registry_key_count` | Number of registry keys used for persistence. Correlates with method type in the synthetic generator — `registry_run` uses 2–4 keys, other methods 0–1. | 0 – 4 |
| `scheduled_task_count` | Number of scheduled tasks. `scheduled_task` method uses 1–3, `wmi_subscription` uses 1–2, others use 0. | 0 – 3 |
| `watchdog_enabled` | 1.0 if a watchdog monitors persistence health. Probability varies by method type (hand-picked: 60% for `service_install`, 10% for `startup_folder`). | 0 or 1 |
| `reinstall_on_removal` | 1.0 if auto-reinstall on removal. Like watchdog, probability is method-dependent (hand-picked). | 0 or 1 |
| `total_persistence_footprint` | `registry_key_count + scheduled_task_count`. Overall infrastructure weight. | 0 – 7 |
| `safety_net_count` | `watchdog_enabled + reinstall_on_removal`. How many safety mechanisms are active (0, 1, or 2). | 0 – 2 |
| `method_registry_run` | 1.0 if the persistence method is `registry_run`, 0.0 otherwise. | 0 or 1 |
| `method_scheduled_task` | 1.0 if the persistence method is `scheduled_task`. | 0 or 1 |
| `method_service_install` | 1.0 if the persistence method is `service_install`. | 0 or 1 |
| `method_startup_folder` | 1.0 if the persistence method is `startup_folder`. | 0 or 1 |
| `method_wmi_subscription` | 1.0 if the persistence method is `wmi_subscription`. | 0 or 1 |

The one-hot method encoding lets the ML models learn per-method infrastructure expectations. The `duplicated_persistence_setup` anomaly creates a `registry_run` method with 0 registry keys and 3–5 scheduled tasks — infrastructure that belongs to a `scheduled_task` setup, not a registry one.

#### Capability Configuration (9 features)

| Feature | Description | Typical Range |
|---|---|---|
| `enabled_count` | Number of capabilities currently enabled (out of 8 total: file_exfiltration, keylogging, screenshot, process_injection, lateral_movement, credential_harvesting, persistence, network_scan). | 0 – 7 |
| `enabled_ratio` | `enabled_count / 8`. Fraction of total capabilities that are active. | 0.0 – 0.88 |
| `max_concurrent_tasks` | Maximum number of capabilities that can execute simultaneously. Higher values indicate an active operational posture. | 2 – 6 |
| `task_timeout_ms` | Milliseconds before a capability task is killed for taking too long. Short timeouts = active/aggressive posture; long timeouts = passive/patient posture. | 30,000 – 120,000 |
| `active_surveillance_count` | Number of enabled surveillance-specific capabilities (keylogging, screenshot). These are the most operationally sensitive capabilities. | 0 – 2 |
| `non_surveillance_enabled_count` | `enabled_count - active_surveillance_count`. Enabled capabilities that are NOT surveillance. | 0 – 6 |
| `resource_per_capability` | `max_concurrent_tasks / enabled_count`. How many execution slots exist per enabled capability. Low values mean capabilities are competing for resources. | 0.3 – 2.0 |
| `surveillance_ratio` | `active_surveillance_count / enabled_count`. What fraction of enabled capabilities are surveillance. The `forgotten_operation_teardown` anomaly produces a high surveillance ratio paired with passive resource settings — a contradiction. | 0.0 – 1.0 |
| `concurrency_timeout_product` | `max_concurrent_tasks * task_timeout_ms / 1000`. Captures the overall resource commitment — high concurrency with long timeouts means the implant is dedicating significant host resources. In the synthetic generator, 35% of configs are "surveillance" posture (high concurrency + short timeouts) and 65% are "passive" (low concurrency + long timeouts) — hand-picked split — so this product stays in a narrow band. | 60 – 480 |

#### Evasion Configuration (11 features)

**Understanding the dependency chain:** The "dependencies" between evasion techniques are not hard technical requirements — AMSI bypass does not literally require string obfuscation to function. They are **behavioral correlations built into the synthetic data generator** that model how real operators *might* tend to enable techniques in layers. All the conditional probabilities below (55%, 65%, 8%, etc.) were hand-picked by the PoC authors to create plausible correlations, not estimated from observed operator behavior. An operator who enables the basics (string obfuscation) is much more likely to also enable the next tier (AMSI bypass: 65% chance) than an operator who skipped the basics (8% chance). This conditional-probability structure creates learnable correlations in the baseline data. The `dependency_coherence` feature measures whether a configuration follows these expected behavioral patterns — configurations that violate them (advanced techniques without foundations) are statistically unusual and worth flagging.

```
Layer 1 (basic):        obfuscate_strings (55% enabled)
                              ↓ enables with 65% probability
Layer 2 (intermediate): amsi_bypass_enabled    etw_patch_enabled (50%)    unhook_ntdll
                              ↓                       ↓ enables with 65%        ↑
                              ↓                       └──────────────────────────┘
                              ↓ 2+ layer-2 techniques enable with 65%
Layer 3 (advanced):     sleep_obfuscation
                              ↓ enables with 55%
                        stack_spoof (~15% enabled — rarest technique)
```

| Feature | Description | Typical Range |
|---|---|---|
| `evasion_enabled_count` | Number of evasion techniques currently enabled (out of 6). | 0 – 6 |
| `evasion_enabled_ratio` | `evasion_enabled_count / 6`. Fraction of available techniques that are active. | 0.0 – 1.0 |
| `evasion_layer_depth` | The highest dependency layer with any enabled technique (0–3). Layer 1 = basic, Layer 2 = intermediate, Layer 3 = advanced. | 0 – 3 |
| `dependency_coherence` | Fraction of enabled techniques whose behavioral prerequisites are also enabled. Returns 1.0 when all expected chains are satisfied (normal operator behavior). Lower values mean an operator enabled advanced techniques while skipping the basics — a pattern that almost never occurs in the baseline data. | 0.0 – 1.0 |
| `depth_per_enabled` | `evasion_layer_depth / evasion_enabled_count`. In baseline data, reaching layer 3 requires at least 3 enabled techniques (the prerequisites), so this ratio stays at or below 1.0. The `full_evasion` anomaly has depth=3 with count=1, producing a ratio of 3.0. | 0.0 – 3.0 |
| `obfuscate_strings` | 1.0 if string obfuscation is enabled. Layer 1 — the most basic evasion technique; 55% of baseline configs enable it. | 0 or 1 |
| `amsi_bypass_enabled` | 1.0 if AMSI (Antimalware Scan Interface) bypass is enabled. Layer 2 — operators who enable obfuscate_strings have a 65% chance of also enabling this; those who don't have only 8%. | 0 or 1 |
| `etw_patch_enabled` | 1.0 if ETW (Event Tracing for Windows) patching is enabled. Layer 2 — 50% base rate, independent of AMSI. | 0 or 1 |
| `unhook_ntdll` | 1.0 if ntdll unhooking is enabled (removes security product hooks from system DLLs). Layer 2 — follows the same conditional pattern as AMSI: 65% if etw is on, 12% if off. | 0 or 1 |
| `sleep_obfuscation` | 1.0 if sleep obfuscation is enabled (hides the implant during sleep periods). Layer 3 — 65% if 2+ layer-2 techniques are on, 8% otherwise. | 0 or 1 |
| `stack_spoof` | 1.0 if call stack spoofing is enabled (masks the implant's call stack). Layer 3 — 55% if sleep_obfuscation is on, 5% otherwise. The rarest technique in baseline data (~15% enabled overall). | 0 or 1 |

---

## 4. Algorithm 1: IQR Fences (Interquartile Range)

### The Intuition

IQR is the simplest and most interpretable algorithm in our ensemble. It asks: **"Is this feature value unusually far from what we've seen before?"** — one feature at a time.

Imagine you've recorded the beacon interval for 1000 past snapshots. Most values fall between 27,000ms and 33,000ms. Suddenly, a snapshot arrives with beacon_interval=1,200ms. That's clearly unusual. IQR formalizes this intuition.

### How It Works

1. **Sort all training values** for a feature and find:
   - **Q1** (25th percentile): 25% of values are below this
   - **Q3** (75th percentile): 75% of values are below this
   - **IQR** = Q3 - Q1 (the "middle 50%" spread)

2. **Compute fences:**
   - Lower fence = Q1 - k × IQR
   - Upper fence = Q3 + k × IQR
   - Where k is the **multiplier** (we use k=2.0)

3. **Score each feature independently:** If the observed value is outside the fences, compute how far (in IQR units).

Note: fences can extend into theoretically impossible ranges (e.g., a negative lower fence for a count feature). This is fine — a count value of 0 is within the fence, and negative values never occur in practice. The fences define a statistical boundary, not a physical one.

### Example

IQR operates on **raw, unscaled feature values** — not the StandardScaler-normalized values used by the ML models. This is correct by design: IQR computes percentiles per-feature independently, so the absolute scale of a feature doesn't affect other features' fences. There is no cross-feature distance calculation where scale mismatch would cause problems.

```
Feature: beacon_interval_ms (raw milliseconds, not scaled)
Training: Q1=27000, Q3=33000, IQR=6000
Fences: [27000 - 2×6000, 33000 + 2×6000] = [15000, 45000]

Observed value: 1200ms
→ Outside lower fence by (15000-1200)/6000 = 2.3 IQR units → FLAGGED
```

### Handling Zero-IQR Features

Some features are boolean (0 or 1) or near-constant. For these, IQR=0 and the standard formula breaks down. We use a cascade of fallbacks:

1. **MAD (Median Absolute Deviation):** If the data has some spread but IQR is 0, use MAD × 1.4826 as a pseudo-IQR.
2. **Training range:** If MAD is also 0 but values vary (e.g., 80% are 0, 20% are 1), use the min/max of training data plus a tiny epsilon margin as the fence boundaries. Note: the IQR multiplier is NOT applied here — the fences are data-range-based, not spread-based.
3. **Epsilon band:** If the feature is truly constant (all training values identical), use a tiny ±0.01 band — any different value is flagged.

### Strengths

- **Highly interpretable:** "beacon_interval was 1200ms but we expected 15000–45000ms" is immediately understandable.
- **Fast:** O(n) scoring, no model to load.
- **No distributional assumptions:** Works on any shaped distribution (unlike z-scores which assume normality).
- **Robust to outliers:** The quartile-based approach isn't pulled by extreme values.

### Limitations

- **Per-feature only:** If each feature is individually normal but the *combination* is anomalous, IQR won't catch it. This is why we need multivariate algorithms.
- **Bounded features:** For ratios in [0,1], the training range may cover the entire [0,1] interval, making the fences ineffective.

---

## 5. Algorithm 2: Isolation Forest

### The Intuition

Isolation Forest asks: **"How easy is it to isolate this data point from the rest?"**

Normal data points are surrounded by similar points and need many splits to isolate. Anomalous points are sparse and different — they can be isolated with very few splits. Think of it like a game of "20 questions" to identify a specific person in a crowd: identifying someone who looks like everyone else takes many questions, but identifying the person wearing a clown suit takes one.

### How It Works

1. **Build many random trees (500 in our config):** Each tree is built by:
   - Randomly picking a feature
   - Randomly picking a split value between the feature's min and max
   - Recursing on each side until every point is isolated

2. **Measure path length:** For a new data point, feed it through all trees and record how deep it goes before being isolated. Short paths = easy to isolate = anomalous.

3. **Normalize and calibrate:** The raw anomaly score is a number whose scale depends on the training data — it's not directly interpretable. We convert it to a 0–1 percentile using the training data's **empirical CDF (Cumulative Distribution Function)**.

   A CDF answers the question: "what fraction of values fall below X?" A *theoretical* CDF uses a mathematical formula (like the bell curve). An *empirical* CDF skips the formula and just uses the actual observed data — sort all training scores, and the percentile of a new score is its rank in that sorted list divided by the total count. No assumptions about what distribution the scores follow.

   For example, if 950 out of 1000 training samples scored lower than the current observation, the empirical CDF gives percentile = 950/1000 = 0.95. We flip this to an anomaly score: `1.0 - 0.95 = 0.05` would mean "less anomalous than 95% of training" (normal), while `1.0 - 0.05 = 0.95` means "more anomalous than 95% of training" (suspicious).

4. **Hard prediction via predict():** sklearn's `predict()` uses the contamination parameter (0.02 = we expect 2% of training data to be outliers) to set a decision threshold. Points more anomalous than this threshold return -1 (anomaly).

### Key Parameters

| Parameter | Value | Meaning |
|---|---|---|
| `n_estimators` | 500 | Number of trees in the forest. More trees = more stable scores. |
| `contamination` | 0.02 | Expected fraction of anomalies in training data. Controls the predict() threshold. |
| `max_features` | 0.8 | Fraction of features each tree uses. Lower = more diverse ensemble. |
| `random_state` | 42 | Seed for reproducibility. |

### Feature Preprocessing

Before training, features are:
1. **Filtered by variance:** Features with variance below 0.001 are dropped (they carry no useful signal).
2. **Standard-scaled:** Each feature is centered to mean=0 and scaled to std=1. Strictly speaking, **Isolation Forest does not need scaling** — tree-based models split one feature at a time and never compute cross-feature distances, so they are inherently scale-invariant. We scale anyway because LOF (which shares the same training matrix) *does* need scaling, and using one consistent preprocessing path for all models simplifies the code and avoids bugs from maintaining separate scaled/unscaled pipelines.

### Strengths

- **Catches multivariate anomalies:** A configuration where beacon_interval is normal AND jitter is normal, but the *combination* is unusual, can be caught because the random splits interact across features.
- **Efficient:** Training is O(n × t × log(n)) where t is the number of trees. Scoring is O(t × log(n)).
- **No distributional assumptions:** Works on non-normal, non-linear data.

### Limitations

- **Opaque decisions:** Without SHAP (see Section 9), the score is a black box. "It's anomalous" isn't useful without "because of beacon_interval."
- **Weak on binary features:** Random splits on a boolean feature produce only two groups. With many binary features (like evasion flags), the tree can't create fine-grained partitions.
- **Contamination sensitivity:** The predict() threshold depends on contamination. Too low (0.01) misses subtle anomalies; too high (0.10) floods with false positives.

---

## 6. Algorithm 3: Local Outlier Factor (LOF)

### The Intuition

LOF asks: **"Is this data point in a sparse neighborhood compared to its neighbors?"**

Imagine a map of a city. Most people live in neighborhoods with many other nearby residents. But one person lives alone on a hilltop, far from anyone. LOF measures exactly this: the ratio of a point's local density to its neighbors' local density.

### How It Works

1. **Find k-nearest neighbors:** For each point, find the k=20 closest points in the feature space using Euclidean distance — the straight-line distance across all features: `sqrt((f1_a - f1_b)² + (f2_a - f2_b)² + ...)`. This is computed on StandardScaler-normalized features so that all features contribute equally (without scaling, a feature like `beacon_interval_ms` in the tens of thousands would dominate `jitter_percentage` around 0.15).

2. **Compute local reachability density:** How far away are this point's neighbors? If they're far, the density is low.

3. **Compare with neighbors' density:** The LOF score is the ratio of neighbors' average density to this point's density. If LOF > 1, the point is in a sparser region than its neighbors — it's an outlier.

4. **Novelty mode (`novelty=True`):** By default, sklearn's LOF can only label the training data itself — it can't score new, unseen observations. Setting `novelty=True` switches it to novelty detection mode, where the model learns the density structure from clean training data and can then score incoming live telemetry against it. This is essential for our workflow: train once on baseline data, then score thousands of new events.

### Why LOF Complements Isolation Forest

| Scenario | Isolation Forest | LOF |
|---|---|---|
| Global outlier (far from everything) | Catches it | Catches it |
| Local outlier (within a cluster but at its edge) | May miss it | Catches it |
| Anomaly in a dense region | Catches it (via tree splits) | May miss it |

LOF excels at detecting points that are unusual *relative to their neighborhood*, even if they're not globally extreme. For example, a communication configuration that's normal for a high-beacon-interval cluster but unusual for the low-beacon-interval cluster it's near.

### Key Parameters

| Parameter | Value | Meaning |
|---|---|---|
| `n_neighbors` | 20 | Number of neighbors for density estimation. Higher = smoother density estimate. |
| `contamination` | 0.02 | Same as IF — controls the predict() threshold. |
| `novelty` | True | Score new data against training distribution (don't use training data for scoring). |

---

## 7. Algorithm 4: Mahalanobis Distance

### The Intuition

Mahalanobis distance asks: **"How far is this configuration from what's normal, taking into account that some features move together?"**

To understand why this matters, consider a simple example with two features: `beacon_interval_ms` and `sleep_on_failure_ms`. In our baseline data, these are **correlated** — when one goes up, the other tends to go up too (the generator derives both from the same internal parameter). If you plot hundreds of normal configs, they form an elongated oval (not a circle):

```
sleep_on_failure_ms
     15000 |         . . .
           |       . . . . .
     10000 |     . . X . . . .       X = average config
           |       . . . . .
      5000 |         . . .
           +---+---+---+---+---
            25k  28k  30k  33k  35k
                beacon_interval_ms
```

Now consider two incoming configs, both the same straight-line distance from the center:

- **Config A:** beacon=35,000 + sleep=15,000 — high beacon AND high sleep. This **follows the pattern** (upper-right along the oval). It's at the edge of normal but in a direction the data naturally stretches.
- **Config B:** beacon=35,000 + sleep=5,000 — high beacon BUT low sleep. This **breaks the pattern** (it's off the side of the oval, in a direction the data never goes).

A simple distance measurement (like Euclidean — see Section 6) treats both the same. Mahalanobis knows Config B is far more suspicious because it measures distance **relative to the oval's shape** — distances along the oval's natural direction are treated as less unusual, while distances that cut across it (breaking the correlation) are amplified.

### How It Works

1. **Compute the average** of all training feature vectors — this is the center of the oval.
2. **Compute the covariance matrix** — this captures the oval's shape: which features move together, how spread out each feature is, and how tilted the oval is.
3. **For a new config, measure how far it is from the center** using the oval's shape as the ruler. The result is a single number: "how many oval-widths away is this point?"
4. **Convert to a p-value** — a probability that answers: "if this config were normal, what's the chance it would be this far from the center?" A p-value of 0.001 means "only 0.1% of normal data would be this far out."

Going back to our example:
- Config A (follows the pattern): p ≈ 0.15 — 15% of normal data is this far. Not unusual.
- Config B (breaks the pattern): p ≈ 0.001 — only 0.1% of normal data is this far. Very unusual.

Same straight-line distance from center, but Mahalanobis correctly identifies that only Config B is anomalous.

### Strengths

- **Correlation-aware:** The main advantage over IQR (which checks features independently). Catches anomalies where each feature is individually within range but the *combination* breaks the expected pattern.
- **Principled p-values:** The output has a clear statistical meaning ("0.1% chance of being normal") rather than an arbitrary score that needs a hand-tuned threshold.
- **Strong empirical separation:** In our testing, 77.8% of anomalies have p < 0.001 vs only 0.6% of clean data — better discrimination than IF or LOF alone.

### Limitations

- **Assumes the oval is smooth and symmetric:** The math works best when the data forms a nice, smooth elliptical cloud. Real data (especially boolean features like evasion flags) can be lumpy and asymmetric, which weakens the p-value's accuracy.
- **Needs enough training data:** With more features than training samples, the oval shape can't be estimated reliably. We add a small correction (regularization) to prevent this from causing errors.
- **Votes via p-value threshold:** A p-value below 0.01 counts as one vote in the severity corroboration system — same weight as an IF or LOF prediction. This brought detection from 88% to 90%.

---

## 8. How We Combine Them — Ensemble Voting

No single algorithm is perfect. IQR catches single-feature outliers but misses correlations. IF catches complex patterns but can produce noisy scores. LOF captures local structure. Mahalanobis catches covariance-level deviations. By combining their **severity votes**, we get the strengths of all four while mitigating individual weaknesses.

### The Corroboration Principle — And Its Cost

The operating principle: **real anomalies trigger multiple detectors independently; random noise triggers only one.** A beacon_storm will trigger IQR, IF, and LOF simultaneously. A borderline normal event might trigger one ML model but nothing else.

For most severity levels, we require at least two detector families to agree. The one exception: **IQR significant** (2+ deviating features or an extreme single-feature deviation) fires MEDIUM alone because the evidence is already multi-dimensional and directly interpretable.

### Why Not Let Single Detectors Fire Alone?

This is the natural question: if we chose LOF specifically for local outliers that IF misses, why require IF to confirm? Doesn't that defeat the purpose?

**Yes — partially.** Corroboration is a pragmatic tradeoff, not a theoretically optimal strategy. Here's the measured cost:

In validation against 10,000 events (~377 anomalies):
- **23 anomalies (6.1%)** are caught by exactly one ML model but suppressed because the other doesn't agree
- 19 of those are IF-only, 4 are LOF-only
- They're concentrated in `duplicated_persistence_setup` (13), `self_destruct_flood` (6), and `mismatched_escalation_policy` (4)

If we removed the corroboration requirement and let single ML predictions through, we would catch those 23 extra anomalies — but we'd also admit:
- **157 IF-only false positives** on clean data
- **167 LOF-only false positives** on clean data

That's **324 additional FPs to gain 23 TPs** — a 14:1 FP-to-TP ratio on the marginal detections. The FP rate would jump from 13.6% to roughly 75%.

The table below shows every detection pattern observed:

```
ANOMALY PATTERNS (what fires on true anomalies):
  IQR_sig + IF + LOF      75 (19.9%)   → ALERTED (HIGH)
  IQR_any + IF + LOF      63 (16.7%)   → ALERTED (MEDIUM)
  IQR_any + IF             63 (16.7%)   → ALERTED (MEDIUM)
  IQR_sig + LOF            51 (13.5%)   → ALERTED (HIGH)
  IF + LOF                 36  (9.5%)   → ALERTED (HIGH)
  IQR_any + LOF            34  (9.0%)   → ALERTED (MEDIUM)
  none                     31  (8.2%)   → suppressed (no signal at all)
  IF only                  19  (5.0%)   → suppressed (no corroboration)
  LOF only                  4  (1.1%)   → suppressed (no corroboration)

FALSE POSITIVE PATTERNS (what fires on clean data):
  IQR_any only            209           → suppressed
  LOF only                167           → suppressed
  IF only                 157           → suppressed
  IF + LOF                 34           → ALERTED (FP)
  IQR_any + IF             31           → ALERTED (FP)
```

The data shows that single-ML-predict events are **overwhelmingly false positives** (157+167 clean vs 19+4 anomalous = 93% noise). Corroboration filters this noise effectively.

### The Design Choice

We accepted ~6% missed anomalies to keep the FP rate under 15%. This is appropriate for a system where **alert fatigue is a bigger operational risk than missing a subtle anomaly** — an operator who ignores alerts because most are false is worse off than one who occasionally misses a marginal detection.

If a future use case demands higher recall at the cost of precision, the corroboration can be relaxed:
- **Option A:** Let single ML predictions through as LOW severity (separate triage queue)
- **Option B:** Lower the contamination rate (e.g., 0.01) so single predictions are more selective
- **Option C:** Add a confidence threshold — only let single ML predictions through if the percentile score exceeds 0.99

### How predict() Reduces False Positives

Instead of using continuous anomaly scores with thresholds (where 10% of clean data scores above the 90th percentile by definition), we use sklearn's `predict()` method for the IF and LOF severity votes. `predict()` produces a binary yes/no decision calibrated to the contamination rate.

Note: the system also computes continuous percentile scores for both IF and LOF (via the empirical CDF of training scores). These are displayed in alert panels for operator context. But the **severity voting** uses only the binary `predict()` output, not the continuous scores.

With `contamination=0.02`:
- IF's `predict()` flags ~2% of training data as anomalous
- LOF's `predict()` flags ~2% of training data as anomalous
- The probability of BOTH independently flagging the same clean data point is ~0.04% (assuming independence)

This gives us a structural FP guarantee: **requiring both ML models to agree limits ML-driven false positives to <0.1% of clean events.**

### Severity Voting Rules

```
ML vote = IF predict() OR LOF predict() OR Mahalanobis p < 0.01

HIGH:   IQR significant + any ML vote
        OR 2+ ML votes agree

MEDIUM: IQR significant alone (strong per-feature evidence)
        OR IQR any + one ML vote (corroboration)

(none): Everything else — not reported
```

---

## 9. Explainability — SHAP Values

Detecting that something is anomalous is only half the job. Operators need to know **why** the system flagged it. This is where SHAP (SHapley Additive exPlanations) comes in.

### What SHAP Does

SHAP computes the contribution of each feature to the Isolation Forest's anomaly score. It answers: "How much did each feature push the prediction toward anomalous vs. normal?"

For example, for a detected beacon_storm:
```
Feature Contributions:
  beacon_interval_ms    → +0.928 (strong push toward anomalous)
  jitter_percentage     → +0.340 (moderate push)
  c2_channel_count      → -0.050 (slight push toward normal)
```

### How It Works (Simplified)

SHAP is based on Shapley values from cooperative game theory. The idea: try every possible combination of features, see how including/excluding each feature changes the prediction, and average the marginal contributions. For tree-based models like Isolation Forest, the `TreeExplainer` algorithm computes exact Shapley values efficiently by traversing the tree structure.

### Why We Show SHAP

- **Trust:** Operators can verify that the alert makes sense ("yes, beacon_interval IS wrong").
- **Prioritization:** An alert driven by one overwhelming feature needs a different response than one driven by many small deviations.
- **Debugging:** If SHAP highlights a feature that shouldn't matter, it reveals a potential issue with the training data or feature engineering.

---

## 10. Severity Classification

Every detected anomaly is assigned one of two severity levels. Events that don't meet the evidence threshold are suppressed entirely:

### HIGH

**Both statistical and ML evidence agree.** This is the strongest signal. Either:
- IQR found significant deviations (2+ features or extreme single-feature) AND at least one ML model predicts anomaly, OR
- Both IF and LOF independently predict anomaly (multivariate agreement)

**Operator action:** Investigate immediately. This is very likely a real anomaly.

### MEDIUM

**Strong evidence from one family, supported by another.** Either:
- IQR found significant deviations alone (extreme per-feature evidence), OR
- IQR found any deviation AND one ML model corroborates

**Operator action:** Review when possible. Likely real but could be an unusual-but-legitimate configuration.

### Suppressed (no severity)

**Insufficient evidence.** Only one signal from one detector family, without corroboration. Not reported.

---

## 11. The Cold-Start Problem

When a new implant appears, there's no per-implant history to build a baseline from. Our solution uses **two levels of baseline:**

1. **Group baseline:** Computed from all implants in the same group. Always available if the group has enough history (≥10 samples per config type).
2. **Per-implant baseline:** Computed from this specific implant's history. Only activates after 7+ days of data (configurable via `IMPLANT_BASELINE_MIN_DAYS`).

During the cold-start period, the group baseline handles detection. Once enough per-implant data exists, the per-implant baseline takes precedence — it's more tailored to the individual implant's normal behavior.

### Group vs Per-Implant — Measured Tradeoff

| Metric | Group | Per-Implant |
|---|---|---|
| Detection rate | 82.3% | **86.0%** |
| FP rate | **21.9%** | 44.3% |

Per-implant baselines are more sensitive (they catch anomalies the group model misses) but noisier (more false positives). The noise is directly caused by **sample count** — per-implant windows have fewer training samples than group windows:

| Samples per implant | Detection | FP rate |
|---|---|---|
| 70 | 86.7% | 43.8% |
| 150 | 80.1% | 40.5% |
| **300 (configured)** | **82.2%** | **22.5%** |
| 500 | 78.0% | 21.2% |

300 samples is the sweet spot — FP drops from 44% to 22% while detection holds at 82%. The system is configured to use 300-sample implant windows, but at 24 snapshots/day per implant, it takes ~60 days to fill a window for a given config type. Early per-implant baselines will be noisier and will improve over time.

### Retraining

Baselines are not static. As new telemetry arrives:
- Feature vectors are appended to a sliding window in Redis (max 1000 entries for group windows, 300 for per-implant windows).
- When 100+ new entries arrive since the last training, the detector retrains the models automatically.
- Old entries are trimmed from the window, so the baseline tracks the recent distribution.

---

## 12. Glossary

| Term | Definition |
|---|---|
| **IQR** | Interquartile Range — the difference between the 75th and 25th percentiles. Measures the spread of the middle 50% of data. |
| **Fence** | A threshold computed from IQR. Values outside the fence are considered outliers. |
| **Isolation Forest (IF)** | An ensemble of random trees that scores anomalies by how easily they can be isolated. |
| **Local Outlier Factor (LOF)** | A density-based algorithm that flags points in sparser regions than their neighbors. |
| **Mahalanobis Distance** | A distance metric that accounts for correlations between features. |
| **SHAP** | SHapley Additive exPlanations — a method to explain which features contributed to a model's prediction. |
| **Contamination** | The expected fraction of anomalies in training data. Controls how aggressively models flag outliers. |
| **Feature vector** | A flat list of numbers extracted from a raw configuration snapshot, suitable for mathematical algorithms. |
| **Baseline** | A statistical model of "normal" behavior, learned from historical data. Includes IQR fences, IF model, LOF model, and Mahalanobis parameters. |
| **Percentile scoring** | Converting a raw algorithm score to a 0–1 scale based on how it ranks against training data scores. |
| **Corroboration** | The requirement that multiple independent detectors agree before issuing an alert. Reduces false positives. |
| **Cold start** | The period when a new implant has insufficient history for a per-implant baseline. |
| **StandardScaler** | A preprocessing step that centers features to mean=0 and scales to standard deviation=1. |
| **Empirical CDF** | The cumulative distribution function estimated directly from data (ranking observed scores) rather than from a theoretical distribution. |
| **MAD** | Median Absolute Deviation — a robust measure of spread, less sensitive to outliers than standard deviation. |
