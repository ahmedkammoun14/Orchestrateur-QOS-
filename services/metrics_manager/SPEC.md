# Metrics Manager — Specification

> **Document type:** Specification (*what the service must do*).
> For *how it does it*, see [`TECHNICAL_DOC.md`](TECHNICAL_DOC.md).

| Field | Value |
|---|---|
| Service name | `metrics_manager` |
| Default port | `8004` (`8104` for provider-2, via `PORT_OFFSET`) |
| Component version | 2.3.0 |
| Status | Implemented |
| Position in the pipeline | SLO step of the cycle — builds the contract TOPSIS will use |

---

## 1. Context

The orchestrator needs an SLO contract to decide against. Two of its parts come from elsewhere: the **primary** objectives are either a fixed business value from `METRICS_REGISTRY` (autonomous mode) or the LLM's output (enhanced mode). But primaries alone are not enough.

Consider the autonomous mode: the only stated objective is `latency < 28 ms`. Nothing says anything about CPU or RAM. Yet if CPU saturation is what *causes* the latency breaches, a decision engine blind to CPU will keep migrating towards VMs that are about to break for the same reason. The orchestrator needs to discover, from observation alone, **which secondary metrics actually correlate with violations** — and give them a threshold nobody declared.

That discovery is this service's first job, and it is done with **Mutual Information** rather than correlation. MI detects non-linear dependencies, which linear correlation misses, and the version used here is the continuous **Kozachenko-Leonenko k-NN estimator** — replacing an earlier 2×2 contingency table. The gain is concrete: no discretisation, no arbitrary bucket boundaries, and usable results from roughly 15 points per class.

The second job is giving those discovered metrics a **threshold**. There is no business value to use — nobody said what CPU level is acceptable. The service derives one statistically, as a percentile of observed history, with the percentile itself chosen according to how volatile the signal is.

The resulting architecture is two-tier and the distinction is strict:

| Tier | Origin | Threshold | Weight | `is_primary` |
|---|---|---|---|---|
| **Primary** | Business registry, or LLM | **Fixed**, never recomputed | 1.0, or the LLM's | `True` |
| **Secondary** | Discovered by MI | **Adaptive**, percentile of history | The MI score itself | `False` |

That boundary is load-bearing well beyond this service: only a **primary** violation can trigger a migration. A secondary breach informs the ranking but never starts a Contract Net negotiation.

## 2. Objectives

| # | Objective |
|---|---|
| O-1 | Discover, from observation alone, which metrics are statistically linked to violations. |
| O-2 | Give each discovered metric a threshold, without any human declaring one. |
| O-3 | Adapt that threshold to the signal's volatility, so a noisy metric is not permanently in breach. |
| O-4 | Weight each secondary SLO by the strength of its statistical link. |
| O-5 | Never recompute a primary threshold, whatever the observations say. |
| O-6 | Serve both modes — autonomous and enhanced — through the same MI machinery. |
| O-7 | Make the MI computation fully auditable in the terminal, step by step. |

## 3. Functional requirements

### 3.1 MI computation

| # | Requirement | Verification |
|---|---|---|
| **FR-1** | The service SHALL compute, per metric, a normalised MI score against the binary violation signal `is_violation`. | Terminal table |
| **FR-2** | It SHALL use the Kozachenko-Leonenko k-NN differential estimator: `MI(X;Y) = H(X) − H(X|Y)`, normalised by `H(Y)`. | Code review |
| **FR-3** | The score SHALL be clamped to `[0, 1]`. | Code review |
| **FR-4** | `k` SHALL be adapted to the sample size: `k = max(3, min(5, n // 10))`. | Code review |
| **FR-5** | A metric with fewer than 5 valid points, or a history containing only one violation class, SHALL score `0.0`. | Unit test |
| **FR-6** | A class too small for the estimator (`n ≤ k+1`) SHALL fall back to the conservative estimate `H(X|Y=c) ← H(X)`, which contributes nothing to the MI. | Unit test |
| **FR-7** | Primary metrics SHALL be **skipped** in the MI computation — their weight is fixed and MI would serve no purpose. In autonomous mode via `include_primaries=False`, in enhanced mode via `skip_metrics`. | Code review |
| **FR-8** | The five estimation steps SHALL be printed as formatted ASCII tables with their intermediate values. | Terminal observation |

### 3.2 Adaptive thresholds

| # | Requirement | Verification |
|---|---|---|
| **FR-9** | The percentile SHALL be selected from the coefficient of variation `CV = σ/μ`: `CV < 0.15` → P70; `0.15 ≤ CV < 0.30` → P75; `CV ≥ 0.30` → P85. | Unit test |
| **FR-10** | A series of fewer than 5 points SHALL return no threshold, and the SLO SHALL be skipped. | Unit test |
| **FR-11** | A mean of zero SHALL return a threshold of `0.0` rather than divide by zero. | Unit test |
| **FR-12** | An adaptive threshold SHALL be clamped to the metric's registry bounds — **except** for absolute units. | Code review |

### 3.3 Autonomous mode — `POST /compute`

| # | Requirement | Verification |
|---|---|---|
| **FR-13** | The service SHALL reject with `400` a history of fewer than 5 points. | `curl` |
| **FR-14** | Every metric flagged `is_primary_objective` SHALL produce a primary SLO with its fixed `default_threshold`, weight `1.0`, `target = threshold × 0.9`. | Unit test |
| **FR-15** | Every non-primary metric whose MI exceeds `MI_RELATIVE_THRESHOLD` = 0.15 SHALL produce a secondary SLO weighted by its MI score. | Unit test |
| **FR-16** | A metric below the MI threshold SHALL be excluded from the contract entirely. | Unit test |
| **FR-17** | `cpu_usage` and `ram_usage` SHALL be expressed as an **absolute capacity floor** (`>= N cores`, `>= N GB`) rather than a usage percentile. | Code review |
| **FR-18** | The response SHALL return the SLOs, the `active_metrics`, the raw `mi_scores` and the step timings. | `curl` |

### 3.4 Enhanced mode — `POST /validate`

| # | Requirement | Verification |
|---|---|---|
| **FR-19** | The service SHALL reject with `400` a payload missing either `slos` or `history`. | `curl` |
| **FR-20** | Every LLM SLO SHALL become primary, its threshold and operator preserved unchanged. | Unit test |
| **FR-21** | An LLM SLO expressed in an absolute unit (`cores`, `GB`) SHALL **not** be clamped to the registry's percentage bounds. | Unit test |
| **FR-22** | The detection `target` SHALL always trigger before the contract breaks: `× 1.05` for a floor (`>=`), `× 0.95` for a ceiling (`<`). | Unit test |
| **FR-23** | The LLM's weight SHALL be used as-is, falling back to `1.0` only when absent or non-positive. | Code review |
| **FR-24** | Metrics not covered by the LLM but correlated by MI SHALL be added as secondary SLOs. | Unit test |
| **FR-25** | MI SHALL not be computed for metrics the LLM already covers. | Code review |

### 3.5 Common

| # | Requirement | Verification |
|---|---|---|
| **FR-26** | Final weights SHALL be normalised so that `Σ weight = 1`, merging LLM/business weights with MI weights. | Unit test |
| **FR-27** | Each computation SHALL be instrumented into two steps, `mi_compute` and `mi_slos`, returned in `timings`. | Excel export |
| **FR-28** | The final contract SHALL be printed as a table showing metric, tier, operator, threshold and normalised weight. | Terminal |
| **FR-29** | `GET /health` SHALL report service liveness. | `curl` |

## 4. Non-functional requirements

| # | Requirement | Target / rationale |
|---|---|---|
| **NFR-1 — Cycle budget** | One `/compute` or `/validate` SHALL fit the 6 s cycle. | MI is O(n²) per metric on the pairwise distance matrix; at n = 50 this is 2500 operations — negligible. |
| **NFR-2 — Statistical robustness** | The estimator SHALL produce usable results from ~15 points per class. | The k-NN estimator, unlike a contingency table, needs no discretisation. |
| **NFR-3 — Auditability** | Every intermediate quantity — H(X), H(X\|Y=1), H(X\|Y=0), the weighted average, the final score — SHALL be printed. | This is the project's explainability contribution at the statistical level; the terminal is the proof. |
| **NFR-4 — Statelessness** | The service SHALL hold no state between calls. | Every contract is recomputed from the history it is given. |
| **NFR-5 — Registry-driven** | The set of candidate metrics SHALL derive from `METRICS_REGISTRY`. | Adding a metric must require no change here. |
| **NFR-6 — Numerical stability** | The MI computation SHALL never divide by zero nor take the log of zero. | `+1e-12` in H(Y); `r_k > 0` mask in the entropy. |
| **NFR-7 — Traceability** | Every log SHALL carry the cycle number. | Cross-terminal correlation with `decision_intelligence`. |

## 5. Interface contract

### 5.1 Consumed — `POST /compute` (autonomous)

```jsonc
{
  "history": [ {"latency": 23.7, "cpu_usage": 41.2, "ram_usage": 63.0,
                "is_violation": false}, … ],
  "all_vals": { "cpu_usage": [41.2, 43.0, …] },
  "cycle": 42
}
```

`history` is the per-cycle observation series of the **service VM**, each point carrying the metric values and the violation flag of that cycle.

### 5.2 Consumed — `POST /validate` (enhanced)

Same, plus `slos` — the LLM contract, filtered by the Hub to the *original* intent metrics only.

> **Why that filtering matters.** `state.current_slos` also contains the secondary SLOs this service added last cycle. Sending them back would re-enter them through step 1, which forces `is_primary = True` on everything it receives — promoting them to primaries, cycle after cycle. Observed in production: an intention that produced a single latency SLO ended up with three primaries at drifting weights, triggering migrations on metrics the client never asked about. The Hub guards against this with `original_intent_weights`.

### 5.3 Produced — response

```jsonc
{
  "slos": [
    {"metric": "latency", "operator": "<", "threshold": 28.0, "unit": "ms",
     "weight": 0.62, "target": 25.2, "is_primary": true, …},
    {"metric": "cpu_usage", "operator": ">=", "threshold": 1.0, "unit": "cores",
     "weight": 0.38, "target": 1.1, "is_primary": false, …}
  ],
  "active_metrics": ["latency", "cpu_usage"],
  "mi_scores": {"cpu_usage": 0.41, "ram_usage": 0.08},
  "timings": {"mi_compute": 12.4, "mi_slos": 0.8},
  "timestamp": "2026-08-11T09:14:22.031Z"
}
```

### 5.4 Responses

| Status | Condition |
|---|---|
| `200` | Contract built |
| `400` | History < 5 points (`/compute`), or `slos`/`history` missing (`/validate`) |
| `500` | Internal computation error |

## 6. Constraints

| # | Constraint |
|---|---|
| **C-1** | **MI needs both classes present.** With no violation in the history — the nominal case when everything works — `set(y)` has one element and every score is `0.0`. **The orchestrator therefore discovers secondary metrics only once things have started going wrong.** This is inherent to a supervised correlation measure and is not a defect, but it means the contract is richer under stress than at rest. |
| **C-2** | **MI measures association, not causation.** A high score means the metric co-varies with violations; it does not mean it causes them. A metric that merely *accompanies* the breach earns a secondary SLO just the same. |
| **C-3** | **The violation signal comes from elsewhere.** `is_violation` is computed by `decision_intelligence` and carried in the history. The MI's quality is bounded by that flag's quality. |
| **C-4** | **A primary threshold is never recomputed.** Even with overwhelming statistical evidence, `is_primary = True` freezes the threshold. This is the guarantee that a business objective cannot drift. |
| **C-5** | **Two threshold semantics coexist.** `cpu_usage`/`ram_usage` are expressed as an absolute capacity floor (`>= cores/GB`), the other metrics as a percentile ceiling. Whether the registry bounds apply depends on the unit, and this check is duplicated in three places. |
| **C-6** | **The CV thresholds are engineering parameters.** `CV_LOW = 0.15`, `CV_HIGH = 0.30`, P70/P75/P85: chosen, not measured. They are plausible, not derived. |
| **C-7** | **Weight normalisation merges two incomparable scales.** A primary weight (LLM intent, or 1.0) and a secondary weight (an MI score in [0,1]) are summed and divided by the total. A high MI score therefore competes directly with a business priority. |
| **C-8** | **The MI terminal output is verbose by design.** Five tables per metric per cycle. Deliberate — it is the explainability artefact — but it makes this terminal unusable for anything else. |

## 7. Out of scope

- Measuring metrics — `collector` and `latency_manager`.
- Reading history — `history_loader`; the Hub passes it in.
- Computing `is_violation` — `decision_intelligence`.
- Deciding on a migration — `decision_intelligence` and the Hub.
- Persisting SLOs — `database`, called by the Hub.
- Extracting SLOs from natural language — `intent_manager`.
- Interpreting a threshold differently per provider — not implemented on `master`.

## 8. Traceability against the xQoS internship objectives

| xQoS objective | Contribution of this service |
|---|---|
| O1 — Multi-provider orchestrator | Builds the contract each provider evaluates against. Only the `ACTIVE` provider recomputes it — a `STANDBY` adopts the contract received by broadcast, since it holds no exploitable history of the service VM. |
| O2 — Intent–QoS relationship engine | **The closest thing on `master` to a real intent→QoS derivation**: it turns an implicit objective into an explicit, quantified contract nobody declared. What is missing is the *per-provider* dimension — MI produces one contract, identical for the whole federation. |
| O3 — Visualization & explainability | **Its main contribution.** The five-step MI trace and the SLO tables are the statistical explainability layer; the MI scores are forwarded to the dashboard's audit trail. |
| O4 — Experimental validation | The MI scores and their evolution across cycles are directly usable as an experimental result: they show the orchestrator *discovering* the metrics that matter, rather than being told. |
