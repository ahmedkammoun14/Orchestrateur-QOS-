# Decision Intelligence — Technical Documentation

> **Document type:** Technical documentation (*how the service is built and how it works*).
> For *what it must do*, see [`SPEC.md`](SPEC.md).

| Field | Value |
|---|---|
| Package | `services.decision_intelligence` |
| Entry point | `services/decision_intelligence/app.py` |
| Framework | FastAPI + Uvicorn |
| Port | `config.DECISION_INTELLIGENCE_PORT` = `8008 + PORT_OFFSET` |
| I/O | **None** — pure computation |
| Lines of code | 917 (125 + 312 + 306 + 174) |

---

## 1. Role in the architecture

```text
   Hub — step 8
     │  POST /decide {service_vm, current_data, predictions_map, slos, cooldown, cycle}
     ▼
┌──────────────────────────────────────────────────────────────────┐
│                 decision_intelligence :8008                      │
│                                                                  │
│  ① cooldown_active? ──────────────────────────────► STAY (fast)  │
│                                                                  │
│  ② ViolationDetector.detect(service_vm)                          │
│       predictions exist → decide on them (proactive|none)        │
│       no predictions    → decide on measurement (reactive|none)  │
│                                                                  │
│  ③ GATE: any violation on a PRIMARY metric? ──no──► STAY         │
│                                                                  │
│  ④ _filter_candidates: prefer VMs satisfying ALL SLOs            │
│       none compliant → keep the full pool (fail-open)            │
│                                                                  │
│  ⑤ TopsisSelector.select  (4 phases, 4 printed tables)           │
│                                                                  │
│  ⑥ hysteresis: best > active × 1.05 ? ──no──────► STAY           │
│                                                                  │
│  ⑦ ─────────────────────────────────────────────► MIGRATE        │
└──────────────────────────────────────────────────────────────────┘
     │  {decision, from_vm, to_vm, reason, topsis_score, vm_scores, timings}
     ▼
   Hub → openstack_client (migration) + database + observability
```

Seven stages, four of which can short-circuit to STAY. The service **decides but never acts** — it opens no socket and touches no store.

## 2. Folder structure

```text
services/decision_intelligence/
├── app.py                  # HTTP layer, cooldown fast path
├── decision.py             # DecisionHandler — orchestrates the 7 stages
├── violation_detector.py   # ViolationDetector — stages ②
├── topsis.py               # TopsisSelector — stage ⑤ + the SLO predicate
├── requirements.txt
├── SPEC.md
└── TECHNICAL_DOC.md
```

Three business classes, each stateless and independently testable. `topsis.py` also exports the free function `vm_satisfies_slo`, used by both the filter and the budget scorer.

Note the logger convention: `app.py` configures the parent `DecisionIntelligence`, and the two modules use the child `DecisionIntelligence.handler`, inheriting by propagation. This is the only service using hierarchical loggers rather than configuring each one explicitly.

## 3. Internal design

### 3.1 Stage ① — the cooldown fast path, twice

The cooldown is checked in **two** places: in `app.py` before calling the handler, and again at the top of `DecisionHandler.decide`. The first is the live path; the second is described in the code as *défensif* and protects any caller reaching the handler directly.

The two responses differ subtly: `app.py` returns a dict without `violated_metrics`, the handler's returns it as an empty list. Harmless, but they are not interchangeable — see L-1.

### 3.2 Stage ② — `ViolationDetector.detect`

For each SLO of the contract, on the **service VM only**:

```text
current_val = vm_data[payload_key]
preds       = predictions_map[service_vm][metric]["predictions"]

if unit in ("cores","GB"):
    current_val, preds → converted to absolute availability
                          (same _to_criterion_value as TOPSIS)

threshold  = slo["threshold"]          ← contractual, NO safety factor
is_floor   = operator in (">", ">=")

_analyze(preds, threshold, current_val, operator)
```

**The unit conversion is the trap this guards against.** An LLM SLO of `>= 0.5 cores` compared against a raw `cpu_usage` of `41.2` %, would be a comparison between a core count and a percentage — meaningless, and it would silently report a violation on every cycle. The conversion uses `TopsisSelector._to_criterion_value`, deliberately the *same* function TOPSIS uses, so the filter, the scorer and the detector cannot drift apart.

**No safety factor.** The comment is explicit: predictions are compared directly to the contractual threshold. If the model says the future breaches the contract, that is a real proactive violation — no `α` needed. This is why `PROACTIVE_FACTOR` survives in the config and the banner but reaches no code.

### 3.3 `_analyze` — the prediction-first rule

```python
slope          = (preds[-1] - preds[0]) / (len(preds) - 1)
_breaches(v)   = v < threshold  if is_floor  else  v > threshold
time_to_breach = index of the first breaching prediction, else len+1
pred_breach    = any prediction breaches
breach_reactive= the current measurement breaches

if preds:
    return "proactive" if pred_breach else "none"     ← measurement IGNORED
if breach_reactive:
    return "reactive"
return "none"
```

The `if preds:` block is the single most consequential branch in the service. **When a prediction exists, the measurement plays no part at all** — not as a tie-breaker, not as an escalation. A VM currently breaching, whose forecast says it will recover, produces `none`.

The rationale is documented in the code and is empirical: adaptive secondary thresholds (§ `metrics_manager`) sit very close to the measured value by construction, so reactive detection made them flip on measurement noise alone. That flapping masked genuinely valid proactive detections on the metric that mattered. Trusting the model removes the noise; the cost is missing a real spike the model did not anticipate.

`reactive` therefore survives **only** as the ML-down safety net.

Note that `pred_breach` scans the **entire** horizon (`any(...)`), not the first `HORIZON_ALERT` steps. A breach predicted at step 7 counts the same as one at step 1 — `time_to_breach` records the difference but nothing acts on it.

### 3.4 `_severity` — direction-aware

```python
if is_floor:  excess = max(0, threshold - val) / threshold      # deficit below
              slope_bonus = max(0, -slope) / threshold          # falling = worse
else:         excess = max(0, val - threshold) / threshold      # overshoot above
              slope_bonus = max(0, slope) / threshold           # rising = worse
return excess + 0.3 * slope_bonus
```

Both the deficit and the slope invert with the operator. The `0.3` weight on the trend is a chosen parameter, hardcoded.

Severity is computed on the **worst predicted case** — `max(preds)` for a ceiling, `min(preds)` for a floor. It is reported in the violation record and printed, but **no threshold is applied to it**: any detected primary violation proceeds to TOPSIS regardless of severity.

### 3.5 Stage ③ — the primary gate

```python
primary_metrics    = {s["metric"] for s in slos if s["is_primary"]}
primary_violations = [v for v in violations if v["metric"] in primary_metrics]
if not primary_violations: → STAY
```

This is the boundary that gives `metrics_manager`'s two-tier architecture its operational meaning. A CPU breach with latency healthy changes nothing: CPU is a correlated co-factor, not the business objective.

The reported `breach_type` is then taken from the **primary** violation specifically, not from "the worst across all metrics". The code explains why: a secondary adaptive threshold flips to `reactive` on measurement noise, and taking the worst would have relabelled a perfectly valid proactive latency detection as reactive — misrepresenting the demonstration's headline behaviour.

### 3.6 Stage ④ — `_filter_candidates`, fail-open

```python
_satisfies_all(cand):
    for each SLO:
        preds = predictions_map[vm][metric]["predictions"]
        if not preds: return False               ← no prediction = non-compliant
        mean = weighted_mean(preds)
        if unit in ("cores","GB"): mean = _to_criterion_value(...)
        if not vm_satisfies_slo(mean, slo): return False
    return True

preferred = [c for c in all if _satisfies_all(c)]
return preferred if preferred else all_candidates     ← FAIL-OPEN
```

Two points. A candidate with **no prediction** is rejected, which during warm-up can empty the compliant set entirely — and the fail-open then returns everyone. The log line distinguishes the two cases explicitly (`SLOs pré-satisfaits` vs `fallback tous candidats`), and reading it is the only way to know whether the subsequent TOPSIS ran on a clean pool or on everything.

The fail-open exists because returning nothing would leave the service stranded on a violating VM. Better to rank imperfect options than to refuse to choose.

### 3.7 Stage ⑤ — TOPSIS, four phases

**Phase 1 — decision matrix.** One row per candidate, one column per SLO metric.

```python
raw_value = weighted_mean(preds)  if preds
            else cand[payload_key] or default_threshold      ← fallback
row.append(_to_criterion_value(metric, cand, raw_value))
```

`calculate_weighted_mean` weights the horizon **decreasingly**: weights `[n, n-1, …, 1]`, so the nearest prediction dominates. A breach at step 1 matters more than one at step 7 — the horizon is discounted, which is the sensible reading of a forecast.

**`_to_criterion_value` — the capacity conversion.**

```python
capacity = cand["total_cores"]  (or "total_ram_gb")
return capacity * (1 - raw_value / 100.0)
```

Two VMs at 50 % CPU do **not** have the same real margin if one has 2 cores and the other 8. Converting to absolute availability makes edge and cloud comparable on the quantity that actually matters. The capacity is declared by the VM itself via `/metrics` and propagated by the collector — no fixed table in the orchestrator, which is what makes this a federation-friendly design. If the VM did not report it, the raw percentage is used unconverted.

This conversion also **flips the polarity**: availability is a *benefit* criterion (more is better), whereas latency remains a *cost* criterion.

**Phase 2 — min-max normalisation, with a tie guard.**

```python
span  = col_max - col_min
scale = max(|col_max|, |col_min|, 1e-9)
if span <= 0 or span/scale < _TIE_THRESHOLD (0.01):
    → 0.5 for every candidate on this criterion
else:
    → (val - col_min) / span
```

The tie guard is not cosmetic, and the comment explains the failure it prevents. Without it, a 0.1 ms spread on ~100 ms — pure ML noise — normalises to exactly 0.0 and 1.0, indistinguishable from a real 50 ms gap. Worse, that can drive `active_score` to exactly 0.0, and since the hysteresis is multiplicative (`0.0 × 1.05 = 0.0`), **any** positive score then clears the barrier and the anti-ping-pong guard is silently neutralised. The 0.5 neutral value keeps noise out of the ranking.

**Phase 3 — weighting.** Element-wise multiplication by the SLO weights from `metrics_manager` (already normalised to sum 1).

**Phase 4 — ideal solutions, distances, score.**

```python
A⁺[j] = max(col) if benefit else min(col)
A⁻[j] = min(col) if benefit else max(col)
d±[i] = euclidean distance to A±
score = d⁻ / (d⁺ + d⁻)          ∈ [0,1], higher is better
```

The `is_benefit` list is what makes a mixed criterion set work: without it, high CPU availability would be treated as a defect.

A single candidate returns `1.0` by convention — min-max is undefined on one point.

### 3.8 Stage ⑥ — hysteresis

```python
active_score = vm_scores.get(service_vm, 0.0)
if to_vm == service_vm or topsis_score <= active_score * 1.05:
    → STAY
```

Two STAY paths with different meanings:

- **TOPSIS elected the active VM.** Despite the violation, it is still the best option — moving would make things worse. This is the behaviour the README calls "active VM as TOPSIS candidate".
- **The challenger does not beat it by 5 %.** The gain does not justify a migration.

The `.get(service_vm, 0.0)` default is load-bearing: if the active VM was filtered out of the pool (it satisfies nothing while clean VMs exist), its score is 0 and any challenger clears the margin. The system never stays on a violating VM when a compliant one is available.

Note the asymmetry with the federation layer: this dead-band is **multiplicative** (`× 1.05`) on a pool-relative score, whereas the arbiter's is **additive** (`+0.05`) on the threshold-normalised Gap Grade. Different scales, different semantics — they are not the same parameter.

### 3.9 Declared but unimplemented

Reading the code, three things are announced and not wired:

| Element | Where | Status |
|---|---|---|
| `_budget_score()` | `topsis.py:259` | Fully implemented, **never called** |
| `_BUDGET_WEIGHT`, `_RELIABILITY_WEIGHT` | `topsis.py:25-26` | Defined, never read |
| `reliability_scores` | Hub → `decision.py:35` → `topsis.select(…)` | Passed through two layers, **never read in `select`** |
| `_HIGH_UNCERTAINTY_THRESHOLD/_FACTOR` | `violation_detector.py:15-16` | Defined, never read |
| `PROACTIVE_FACTOR`, `HORIZON_ALERT` | Startup banner | Displayed, reach no logic |

The `TopsisSelector` docstring states three criteria families — SLO metrics, compliance budget, reliability. **Only the first is implemented.** Reliability is measured by the collector, transported by the Hub, received here, and discarded. This is worth knowing before describing the criteria set out loud. See L-2.

## 4. API reference

### `POST /decide`

**Request**

| Field | Type | Required | Description |
|---|---|---|---|
| `service_vm` | string | **yes** | The VM currently hosting the service |
| `current_data` | array | **yes** | Candidates with their measured values and capacities |
| `slos` | array | **yes** | The contract from `metrics_manager` |
| `predictions_map` | object | no | `{vm_id: {metric: {predictions, uncertainty}}}` |
| `cooldown_active` | bool | no | Fast path to STAY |
| `reliability_scores` | object | no | **Accepted, unused** (§3.9) |
| `mi_scores` | object | no | Display only, in the banner |
| `cycle` | int | no | Log correlation |

**Response** — see SPEC §5.2.

**Example**

```bash
curl -X POST http://localhost:8008/decide -H "Content-Type: application/json" -d '{"service_vm":"edge1","cycle":1,"slos":[{"metric":"latency","operator":"<","threshold":28.0,"unit":"ms","weight":1.0,"is_primary":true}],"current_data":[{"vm_id":"edge1","rtt_ms":31.0},{"vm_id":"edge1b","rtt_ms":18.0}],"predictions_map":{"edge1":{"latency":{"predictions":[31.0,32.0,33.0]}},"edge1b":{"latency":{"predictions":[18.0,18.5,19.0]}}}}'
```

Expected: `proactive` violation on `edge1`, TOPSIS elects `edge1b`, decision `migrate`.

### `GET /health`

```json
{"status": "healthy", "service": "decision_intelligence"}
```

Liveness only — the service has no dependency to probe.

## 5. Configuration

| Variable | Default | Status |
|---|---|---|
| `DECISION_INTELLIGENCE_PORT` | `8008` (+`PORT_OFFSET`) | Used |
| `METRICS_REGISTRY` | 3 metrics | Used — `payload_key`, `default_threshold` |
| `PROACTIVE_FACTOR` | `0.85` | **Displayed, unused** |
| `HORIZON_ALERT` | `3` | **Displayed, unused** |

Hardcoded module constants, not configurable:

| Constant | Value | Role |
|---|---|---|
| `_MIGRATION_MARGIN` | `0.05` | Anti-ping-pong hysteresis |
| `_TIE_THRESHOLD` | `0.01` | Criterion tie guard |
| `_ABSOLUTE_UNITS` | `("cores","GB")` | Absolute-unit detection — **duplicated** in `metrics_manager` and `provider_arbitration` |
| `_CAPACITY_METRICS` | cpu→cores, ram→GB | Capacity conversion mapping |
| severity slope weight | `0.3` | Trend contribution |

`_MIGRATION_MARGIN` deserves attention: it is the parameter that stops the demonstration from oscillating, and it cannot be tuned without editing the source.

## 6. Dependencies

**Internal** — `shared.config`, `shared.timing.StepProfiler`, `shared.logging_utils`.

**External** — `fastapi`, `uvicorn`. No numpy: TOPSIS is implemented in pure Python, which at 4×3 is entirely adequate and keeps the arithmetic readable next to the equations.

**Runtime** — none. Like `metrics_manager`, this service calls nobody.

## 7. Data model

Stateless. `DecisionHandler.__init__` builds one `ViolationDetector` and one `TopsisSelector`, both immutable. Nothing persists; the decision exists only in the response.

## 8. Running it standalone

```bash
python -m services.decision_intelligence.app
```

Second provider:

```bash
PORT_OFFSET=100 python -m services.decision_intelligence.app
```

No dependency to start first. The `curl` in §4 exercises the complete path.

## 9. Logging and observability

| Marker | Level | Meaning |
|---|---|---|
| `🚀 Decision Intelligence — Démarrage` | INFO | Banner (note: two of the three values shown are unused) |
| `📥 /decide — service_vm … candidats … SLOs` | INFO | Call entry |
| `🎯 Decision Intelligence — Cycle #N` | INFO | Banner listing each SLO with weight and PRIMARY/SECONDARY tier |
| `⏳ Cooldown actif` | INFO | Fast path |
| `⚠️ N violation(s) détectée(s)` | INFO | Summary with type and severity per metric |
| `✅ Aucune violation SLO` | INFO | Nominal |
| `✅ Secondary-only violation` | INFO | **The primary gate blocking a migration** |
| `🔎 Candidats TOPSIS … SLOs pré-satisfaits \| fallback` | INFO | **Tells you whether the pool was clean or fail-open** |
| `📊 Prédictions des candidats` | INFO | TOPSIS table 1 |
| `📐 Normalisation min-max` | INFO | TOPSIS table 2 |
| `⚖️ Pondération` | INFO | TOPSIS table 3 |
| `📏 Distances et score` | INFO | TOPSIS table 4 |
| `🏆 TOPSIS classement` | INFO | Full ranking on one line |
| `🟢 … STAY` | INFO | Hysteresis or "still best candidate" |
| `✅ /decide → MIGRATE` | INFO | The decision |

The four tables make each cycle's decision fully auditable: you can recompute the score by hand from what is printed. The two lines to read first are `🔎 Candidats TOPSIS` (was the pool compliant?) and `🏆 TOPSIS classement` (how close were the top two?). A ranking where the top two are within 5 % explains a STAY that would otherwise look wrong.

## 10. Testing

| File | Covers |
|---|---|
| `tests/unit/test_topsis.py` | TOPSIS ranking |
| `tests/unit/test_violation_detector.py` | Detection |
| `tests/unit/test_decision_vm_scores.py` | The `vm_scores` ranking |

The best-covered service of the stack, which is unsurprising: being pure, it needs no mocks.

Not covered: the hysteresis branch itself (§3.8) — including the `active_score = 0.0` case — and the `_TIE_THRESHOLD` guard, whose failure mode (§3.7) silently disables the anti-ping-pong.

```bash
pytest tests/unit/test_topsis.py tests/unit/test_violation_detector.py tests/unit/test_decision_vm_scores.py -v
```

## 11. Known limitations

| # | Limitation | Impact | Possible fix |
|---|---|---|---|
| **L-1** | The cooldown is handled in two places with slightly different response shapes (§3.1). | A consumer may or may not find `violated_metrics`. | Keep the `app.py` fast path only, or align the two dicts. |
| **L-2** | Reliability and the compliance budget are declared as criteria and not implemented (§3.9). | The docstring overstates the criteria set; reliability influences nothing despite being measured and transported. | Either wire them in with their weights, or remove them and correct the docstring. |
| **L-3** | `_MIGRATION_MARGIN` is a hardcoded constant (§5). | The demonstration's anti-oscillation parameter cannot be tuned without editing source. | Move it to `shared/config.py`. |
| **L-4** | `_ABSOLUTE_UNITS` is triplicated across three modules. | A divergence would resurrect the "0.5 cores → 1.0" defect. | Single definition in `shared/config.py`. |
| **L-5** | `PROACTIVE_FACTOR` and `HORIZON_ALERT` are displayed but unused (§3.9). | The banner suggests behaviour the code does not implement. | Remove from the banner, or use `HORIZON_ALERT` to bound the breach search. |
| **L-6** | A breach at horizon step 7 is treated exactly like one at step 1 (§3.3). | Migration may fire for a distant, uncertain forecast. | Weight the breach by `time_to_breach`, or bound the search by `HORIZON_ALERT`. |
| **L-7** | `severity` is computed, logged, returned — and never used as a criterion. | Rich information left unexploited. | Use it to prioritise between simultaneous violations. |
| **L-8** | Ignoring the measurement when a prediction exists (§3.3, SPEC C-3). | A real spike the model missed produces no reaction at all. | Escalate to reactive when the measured value exceeds the threshold by a wide margin. |
| **L-9** | The hysteresis is untested (§10), including the `active_score = 0.0` path. | The mechanism that stops the demo oscillating is unverified. | Three cases: active wins, challenger within margin, challenger beyond margin. |
| **L-10** | No authentication. | Any host can submit a forged payload and obtain a migration decision. | Acceptable on the demonstrator's private network. |
