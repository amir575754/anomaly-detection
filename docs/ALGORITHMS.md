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

This is why we use four different algorithms instead of one — they each see different types of "unusual."

---

## 3. Our Feature Engineering Philosophy

Before any algorithm runs, raw configuration snapshots are transformed into **feature vectors** — flat lists of numbers that capture the operational semantics of a configuration. This transformation is critical because algorithms work on numbers, not nested JSON.

### Design Principle: Capture Operational Meaning, Not Anomaly Patterns

Our features are designed to measure general operational properties:
- **Ratios:** What fraction of programs are set to self-destruct? (Not: "is this the self_destruct_flood anomaly?")
- **Consistency scores:** Do programs and drivers share the same response posture? (Not: "is programs=audit and drivers=do_nothing?")
- **Dependency coherence:** Are enabled evasion techniques supported by their prerequisites?
- **Cross-feature interactions:** What's the ratio of beacon interval to sleep-on-failure time?

If our features started encoding specific anomaly signatures (like "is_all_self_destruct"), we'd be doing rule-based detection disguised as ML — defeating the purpose of unsupervised learning.

### Feature Categories Per Configuration Type

| Configuration Type | Feature Count | Key Features |
|---|---|---|
| **Communication** | 12 | beacon_interval_ms, jitter_percentage, beacon_to_sleep_ratio, jitter_retries_product, c2_channel_count |
| **Dangerous Program** | 15 | program_count, per-action counts and ratios, posture_consistency, action_diversity, dominant_action_ratio |
| **Persistence** | 7 | active_method_count, registry_key_count, safety_to_depth_ratio, total_persistence_footprint |
| **Capability** | 9 | enabled_count, surveillance_ratio, resource_per_capability, concurrency_timeout_product |
| **Evasion** | 11 | evasion_enabled_count, dependency_coherence, depth_per_enabled, individual boolean flags |

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

3. **Clamp fences** to [P1, P99] — the 1st and 99th percentiles of training data. This prevents fences from extending into impossible ranges (like negative counts).

4. **Score each feature independently:** If the observed value is outside the fences, compute how far (in IQR units).

### Example

```
Feature: beacon_interval_ms
Training: Q1=27000, Q3=33000, IQR=6000
Fences: [27000 - 2×6000, 33000 + 2×6000] = [15000, 45000]

Observed value: 1200ms
→ Outside lower fence by (15000-1200)/6000 = 2.3 IQR units → FLAGGED
```

### Handling Zero-IQR Features

Some features are boolean (0 or 1) or near-constant. For these, IQR=0 and the standard formula breaks down. We use a cascade of fallbacks:

1. **MAD (Median Absolute Deviation):** If the data has some spread but IQR is 0, use MAD × 1.4826 as a pseudo-IQR.
2. **Training range:** If MAD is also 0 but values vary (e.g., 80% are 0, 20% are 1), use the min/max of training data as the fence boundaries.
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

3. **Normalize and calibrate:** We convert the raw "anomaly score" to a percentile using the training data's empirical CDF (Cumulative Distribution Function). A score of 0.95 means "more anomalous than 95% of training data."

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
2. **Standard-scaled:** Each feature is centered to mean=0 and scaled to std=1. This prevents features with large ranges (like beacon_interval_ms in the thousands) from dominating features with small ranges (like jitter_percentage around 0.15).

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

1. **Find k-nearest neighbors:** For each point, find the k=20 closest points in the feature space (using Euclidean distance on scaled features).

2. **Compute local reachability density:** How far away are this point's neighbors? If they're far, the density is low.

3. **Compare with neighbors' density:** The LOF score is the ratio of neighbors' average density to this point's density. If LOF > 1, the point is in a sparser region than its neighbors — it's an outlier.

4. **Novelty mode:** We train LOF on clean data and score new data against the learned density structure. This is called `novelty=True` in sklearn.

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

Mahalanobis distance asks: **"How far is this point from the center of the training distribution, accounting for correlations between features?"**

Standard Euclidean distance treats all directions equally. But in real data, features are correlated. If beacon_interval and sleep_on_failure are highly correlated (high beacon → high sleep), then a point with high beacon but low sleep is unusual — even if each value individually is within range. Euclidean distance might miss this, but Mahalanobis "stretches" the space according to the correlation structure.

### How It Works

1. **Compute the mean vector** (μ) — the center of the training distribution.
2. **Compute the covariance matrix** (Σ) — captures how features vary together.
3. **For a new point x, compute:** D² = (x - μ)ᵀ Σ⁻¹ (x - μ)
4. **Convert to p-value:** Under a multivariate normal assumption, D² follows a chi-squared distribution with degrees of freedom equal to the number of features. A p-value < 0.001 means "there's a 0.1% chance of seeing this configuration if the data were normal."

### Strengths

- **Principled thresholds:** p-values give statistically grounded false positive guarantees.
- **Correlation-aware:** Catches anomalies that are normal per-feature but unusual in combination.

### Limitations

- **Assumes multivariate normality:** Real data (especially boolean features) isn't normally distributed. This weakens the p-value interpretation.
- **Sensitive to covariance estimation:** With more features than samples, the covariance matrix becomes ill-conditioned. We add regularization (small diagonal term) to prevent this.
- **Supplementary role in our system:** Due to the normality limitation, Mahalanobis serves as supporting evidence rather than a primary detector.

---

## 8. How We Combine Them — Ensemble Voting

No single algorithm is perfect. IQR catches single-feature outliers but misses correlations. IF catches complex patterns but can produce noisy scores. LOF captures local structure. Mahalanobis provides principled statistics. By combining them, we get the strengths of all four while mitigating individual weaknesses.

### The Corroboration Principle

The key insight behind our ensemble: **real anomalies trigger multiple detectors independently; random noise triggers only one.**

A beacon_storm anomaly (beacon_interval dropped from 30000 to 1200) will:
- Trigger IQR (massive deviation on beacon_interval)
- Trigger IF (unusual point in the feature space)
- Trigger LOF (sparse neighborhood)
- Maybe trigger Mahalanobis (far from centroid)

But a normal event that happens to have a slightly unusual jitter_percentage might:
- Trigger IF (borderline score due to noise)
- Not trigger IQR, LOF, or Mahalanobis

By **requiring at least two detectors to agree**, we dramatically reduce false positives while preserving true positive detection.

### How predict() Reduces False Positives

A critical innovation: instead of using continuous anomaly scores with thresholds (where 10% of clean data scores above the 90th percentile by definition), we use sklearn's `predict()` method which produces a binary yes/no decision calibrated to the contamination rate.

With `contamination=0.02`:
- IF's `predict()` flags ~2% of training data as anomalous
- LOF's `predict()` flags ~2% of training data as anomalous
- The probability of BOTH independently flagging the same clean data point is ~0.04% (assuming independence)

This gives us a structural FP guarantee: **requiring both ML models to agree limits ML-driven false positives to <0.1% of clean events.**

### Severity Voting Rules

```
HIGH:   IQR significant + any ML predict
        OR both IF and LOF predict anomaly

MEDIUM: IQR significant alone
        OR IQR any + one ML predict

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

Every detected anomaly is assigned one of three severity levels:

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

### Retraining

Baselines are not static. As new telemetry arrives:
- Feature vectors are appended to a sliding window in Redis (max 1000 entries per scope).
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
