# Decision Intelligence — Specification

> **Document type:** Specification (*what the service must do*).
> For *how it does it*, see [`TECHNICAL_DOC.md`](TECHNICAL_DOC.md).

| Field | Value |
|---|---|
| Service name | `decision_intelligence` |
| Default port | `8008` (`8108` for provider-2, via `PORT_OFFSET`) |
| Component version | 2.0.0 |
| Status | Implemented |
| Position in the pipeline | Step 8 — the decision itself |

---

## 1. Context

Everything before this service **observes**: the collector measures, `history_loader` retrieves, `ml_predictor` forecasts, `metrics_manager` builds the contract. This service is where observation becomes **action**: it answers the only question the orchestrator actually exists to answer — *should the service move, and if so, where?*

That question decomposes into three sub-questions, and the service answers them in a fixed order, each acting as a gate on the next:

1. **Is the SLO contract breached on the current VM?** — and specifically, is a **primary** objective breached? A secondary breach is a symptom, not a reason to move.
2. **If so, which VM is the best target?** — a multi-criteria problem with conflicting objectives (a VM with the lowest latency may have the least free CPU), solved with **TOPSIS**.
3. **Is the best target good enough to justify moving?** — migration has a cost, and a marginally better VM is not worth a migration. A **hysteresis margin** guards against ping-pong.

Two design decisions distinguish this service from a naive threshold monitor.

**Decisions are made on predictions, not measurements.** If an ML forecast exists, it decides — a transient measured spike is ignored. The reactive path survives only as a safety net for when the models are down. This is what makes the orchestrator *proactive* rather than merely reactive, and it is why the whole ML chain upstream exists.

**The active VM competes against the challengers.** It is included in the TOPSIS candidate pool rather than excluded as "the one to escape from". If TOPSIS still ranks it first despite the violation, the answer is STAY — it remains the least-bad option, and moving would make things worse. Only comparing the incumbent to the alternatives makes that judgement possible.

## 2. Objectives

| # | Objective |
|---|---|
| O-1 | Detect SLO violations on the service VM, based on predictions when available. |
| O-2 | Trigger a migration **only** on a primary-objective breach. |
| O-3 | Rank candidate VMs by a multi-criteria method that handles conflicting objectives. |
| O-4 | Compare VMs of heterogeneous capacity on their real margin, not on relative percentages. |
| O-5 | Prevent oscillation between two near-equivalent VMs. |
| O-6 | Return the complete ranking, not just the winner, so the decision can be justified. |
| O-7 | Remain stateless and side-effect-free: decide, do not act. |

## 3. Functional requirements

### 3.1 Violation detection

| # | Requirement | Verification |
|---|---|---|
| **FR-1** | For each SLO, the service SHALL compare the current value and the predicted horizon against the **contractual threshold**, with no safety factor applied. | Code review |
| **FR-2** | Violation direction SHALL follow the operator: a ceiling (`<`) is breached when exceeded, a floor (`>=`) when fallen below. | Unit test |
| **FR-3** | **When predictions exist, the decision SHALL be based on them alone.** A breach anywhere in the horizon → `proactive`; otherwise → `none`, whatever the current measurement says. | Unit test |
| **FR-4** | The `reactive` mode SHALL apply **only** in the absence of any prediction. | Unit test |
| **FR-5** | An SLO expressed in an absolute unit (`cores`, `GB`) SHALL have both its current value and its predictions converted to that VM's real availability before comparison. | Unit test |
| **FR-6** | The service SHALL compute a `severity` combining the relative overshoot and the prediction slope (`excess + 0.3 × slope_bonus`). | Unit test |
| **FR-7** | It SHALL compute a `time_to_breach` — the index of the first breaching prediction. | Unit test |
| **FR-8** | Severity SHALL be measured on the worst predicted case: the peak for a ceiling, the trough for a floor. | Code review |

### 3.2 The primary gate

| # | Requirement | Verification |
|---|---|---|
| **FR-9** | A migration SHALL be triggered **only** by a violation on a metric marked `is_primary`. | Unit test |
| **FR-10** | Secondary-only violations SHALL yield STAY with the reason `Secondary-only violation`. | Unit test |
| **FR-11** | The reported `breach_type` SHALL be that of the **primary** SLO, not the worst across all metrics. | Unit test |
| **FR-12** | No severity threshold SHALL be applied: any detected primary violation triggers a TOPSIS evaluation. | Code review |

### 3.3 Candidate filtering

| # | Requirement | Verification |
|---|---|---|
| **FR-13** | The pool SHALL contain **all** candidates supplied, including the active VM. | Code review |
| **FR-14** | The service SHALL prefer candidates satisfying **all** SLOs on their weighted-mean prediction. | Unit test |
| **FR-15** | A candidate without predictions for a required metric SHALL be considered non-compliant. | Unit test |
| **FR-16** | If **no** candidate satisfies every SLO, the full pool SHALL be kept (fail-open) rather than returning nothing. | Unit test |

### 3.4 TOPSIS

| # | Requirement | Verification |
|---|---|---|
| **FR-17** | Criteria SHALL be the SLO metrics, valued by a **weighted mean of the predictions**, decreasing with the horizon. | Unit test |
| **FR-18** | `cpu_usage` and `ram_usage` SHALL be converted to absolute availability: `capacity × (1 − usage/100)`, and treated as **benefit** criteria. Other metrics remain **cost** criteria. | Unit test |
| **FR-19** | Capacity SHALL be read from the candidate itself (`total_cores`, `total_ram_gb`), falling back to the raw percentage when absent. | Unit test |
| **FR-20** | Normalisation SHALL be min-max over the candidate pool. | Unit test |
| **FR-21** | A criterion whose relative spread is below `_TIE_THRESHOLD` = 1 % SHALL be neutralised to 0.5 for all candidates instead of being polarised to 0/1. | Unit test |
| **FR-22** | Weighting SHALL use the SLO weights as supplied by `metrics_manager`. | Code review |
| **FR-23** | The ideal solutions SHALL respect criterion polarity: `A⁺` = min for a cost, max for a benefit; `A⁻` the converse. | Unit test |
| **FR-24** | The score SHALL be the relative closeness `d⁻ / (d⁺ + d⁻)`, in `[0, 1]`, higher is better. | Unit test |
| **FR-25** | A single candidate SHALL score `1.0` by convention, without computation. | Unit test |
| **FR-26** | The four TOPSIS phases SHALL be printed as tables: predictions, normalisation, weighting, distances and scores. | Terminal |
| **FR-27** | The full ranking `vm_scores` SHALL be returned, not just the winner. | `curl` |

### 3.5 Hysteresis and output

| # | Requirement | Verification |
|---|---|---|
| **FR-28** | Migration SHALL occur only if `best_score > active_score × (1 + 0.05)`. | Unit test |
| **FR-29** | If TOPSIS elects the active VM, the answer SHALL be STAY with the reason `still best candidate`. | Unit test |
| **FR-30** | An active VM absent from the pool SHALL score `0.0`, freeing migration towards any compliant VM. | Unit test |
| **FR-31** | An active `cooldown_active` SHALL return STAY immediately, before any computation (fast path). | Unit test |
| **FR-32** | Every response SHALL carry `decision`, `from_vm`, `to_vm`, `reason`, `topsis_score`, `breach_type`, `violated_metrics`, `timestamp`. | `curl` |
| **FR-33** | `violated_metrics` SHALL list **every** active SLO with its normalised weight, breached or not. | Code review |
| **FR-34** | The sub-steps SHALL be profiled and returned under `timings`. | Excel export |
| **FR-35** | `POST /decide` SHALL reject with `400` a payload missing `current_data`, `slos` or `service_vm`. | `curl` |

## 4. Non-functional requirements

| # | Requirement | Target / rationale |
|---|---|---|
| **NFR-1 — Purity** | The service SHALL perform no I/O: no Redis, no HTTP, no file. It decides; the Hub acts. | Makes it fully testable without mocks, and makes the decision reproducible from its payload alone. |
| **NFR-2 — Statelessness** | No memory between calls. Cooldown and hysteresis state are supplied by the caller. | Two providers can run the same code with no interference. |
| **NFR-3 — Cycle budget** | One `/decide` SHALL fit the 6 s cycle. | TOPSIS is O(n_vm × n_criteria); at 4×3 the cost is negligible. |
| **NFR-4 — Determinism** | The same payload SHALL always produce the same decision. | Required for the demonstration to be reproducible and for the audit trail to mean anything. |
| **NFR-5 — Numerical robustness** | No division by zero in normalisation, distances or the score. | `1e-9` guard, `span <= 0` branch, `(d⁺+d⁻) > 0` test. |
| **NFR-6 — Explainability** | Every phase SHALL be traceable in the terminal, and the full ranking returned. | The dashboard must be able to answer "why this VM rather than that one". |
| **NFR-7 — Anti-thrashing** | Two mechanisms SHALL guard against oscillation: the temporal cooldown (caller-supplied) and the score hysteresis (here). | Ping-pong edge↔cloud was observed without the 5 % margin. |

## 5. Interface contract

### 5.1 Consumed — `POST /decide`

Caller: the Hub, at step 8, once per cycle.

```jsonc
{
  "service_vm":   "edge1",
  "current_data": [ {"vm_id": "edge1", "cpu_usage": 41.2, "ram_usage": 63.0,
                     "total_cores": 2, "total_ram_gb": 4.0, "rtt_ms": 23.7}, … ],
  "predictions_map": { "edge1": { "latency": {"predictions": [24.1, …],
                                              "uncertainty": 0.12} } },
  "slos": [ {"metric": "latency", "operator": "<", "threshold": 28.0,
             "unit": "ms", "weight": 0.62, "is_primary": true}, … ],
  "cooldown_active": false,
  "reliability_scores": {"edge1": 0.98},
  "mi_scores": {"cpu_usage": 0.41},
  "cycle": 42
}
```

`reliability_scores` and `mi_scores` are accepted; `mi_scores` is used for display only, and `reliability_scores` is **not used at all** — see C-5.

### 5.2 Produced — response

```jsonc
{
  "decision":     "migrate",              // or "stay"
  "from_vm":      "edge1",
  "to_vm":        "edge1b",
  "reason":       "proactive violation on latency — TOPSIS selected 'edge1b' (score=0.8712)",
  "topsis_score": 0.8712,
  "breach_type":  "proactive",            // proactive | reactive | null
  "violated_metrics": [ {"metric": "latency", "weight": 0.62},
                        {"metric": "cpu_usage", "weight": 0.38} ],
  "vm_scores":    {"edge1": 0.31, "edge1b": 0.8712, "cloud1": 0.44},
  "timings":      {"violation_detection": 0.4, "candidate_filter": 0.2,
                   "topsis_total": 1.8, …},
  "timestamp":    "2026-08-11T09:14:22.031Z"
}
```

`vm_scores` is **absent** — not null — from a STAY produced before TOPSIS ran (cooldown, no violation, no candidate). Its presence is itself the signal that TOPSIS was evaluated.

### 5.3 Responses

| Status | Condition |
|---|---|
| `200` | Decision taken (`migrate` or `stay`) |
| `400` | `current_data`, `slos` or `service_vm` missing |
| `500` | Internal engine error |

## 6. Constraints

| # | Constraint |
|---|---|
| **C-1** | **A TOPSIS score is meaningful only within its pool.** Min-max normalises against the candidates present, so 0.87 means "best *here*", not an absolute quality. This is precisely why cross-provider comparison uses the Gap Grade instead, and why `candidates_for_provider()` keeps each provider's TOPSIS confined to its own VMs. |
| **C-2** | **Adding or removing a candidate changes every score.** Normalisation is relative to the pool, so scores from two different cycles are not comparable if the pool changed. |
| **C-3** | **Trusting the prediction means ignoring the measurement.** FR-3 is a deliberate trade-off: it removes the flapping caused by adaptive secondary thresholds tracking the measured value, at the cost of missing a real spike the model did not see. |
| **C-4** | **The dead-band is an engineering parameter.** `_MIGRATION_MARGIN = 0.05` is chosen, not measured. It is also a **hardcoded module constant**, not an environment variable — unlike the arbiter's dead-band, which is configurable. |
| **C-5** | **Three declared criteria, one implemented.** The class docstring announces "SLO metrics, compliance budget, reliability". Only the SLO metrics are used: `_budget_score` is never called, and `reliability_scores` is passed from the Hub through two layers and never read. Reliability influences no decision today. |
| **C-6** | **"7-step TOPSIS" is a presentation, not a code structure.** The implementation has four profiled phases; the seven steps of the classical method are grouped within them. |
| **C-7** | **Two config values are displayed but unused.** `PROACTIVE_FACTOR` and `HORIZON_ALERT` appear in the startup banner. Neither reaches the logic: no safety factor is applied (FR-1), and the breach is searched across the **whole** horizon, not up to `HORIZON_ALERT`. |
| **C-8** | **Migration cost is not a criterion.** Deliberately removed: it was never instrumented and no real measurement exists. Documented in the class docstring. |

## 7. Out of scope

- Executing the migration — the Hub, via `openstack_client`.
- Enforcing the temporal cooldown — the Hub computes it; this service only honours the flag.
- Comparing across providers — `hub/provider_arbitration.py` (Gap Grade) and `placement_arbiter`.
- Producing predictions — `ml_predictor`.
- Building the SLO contract — `metrics_manager`.
- Persisting the decision — `database`, called by the Hub.
- Any I/O whatsoever (NFR-1).

## 8. Traceability against the xQoS internship objectives

| xQoS objective | Contribution of this service |
|---|---|
| O1 — Multi-provider orchestrator | Provides the **intra-provider** decision. Its structural limit (C-1) is what motivated the Gap Grade for the inter-provider layer — the two are complementary by design, not redundant. |
| O2 — Intent–QoS relationship engine | Consumes the interpreted contract; produces no interpretation of its own. |
| O3 — Visualization & explainability | **Its strongest contribution.** Four printed tables per cycle, a textual `reason`, the complete `vm_scores` ranking, and the breach type — everything needed to answer "why this VM, and why now". |
| O4 — Experimental validation | Its `timings` measure the decision cost; `vm_scores` across cycles is the raw material for analysing decision stability and the dead-band's effect. |
