# Metrics Manager — Technical Documentation

> **Document type:** Technical documentation (*how the service is built and how it works*).
> For *what it must do*, see [`SPEC.md`](SPEC.md).

| Field | Value |
|---|---|
| Package | `services.metrics_manager` |
| Entry point | `services/metrics_manager/app.py` |
| Framework | FastAPI + Uvicorn |
| Port | `config.METRICS_MANAGER_PORT` = `8004 + PORT_OFFSET` |
| Scientific libraries | `numpy`, `scipy.special.digamma` |
| Lines of code | 772 (173 + 599) |

---

## 1. Role in the architecture

```text
   history_loader ──series──► Hub
                                │
                                │  autonomous: POST /compute  {history, all_vals, cycle}
                                │  enhanced:   POST /validate {slos, history, cycle}
                                ▼
        ┌────────────────────────────────────────────────────────┐
        │              metrics_manager :8004                     │
        │                                                        │
        │  ① compute_mi_scores()                                 │
        │       Kozachenko-Leonenko k-NN, 5 printed steps        │
        │       skips PRIMARY metrics (weight already fixed)     │
        │                                                        │
        │  ② select_dynamic_slos()   ← autonomous                │
        │     validate_and_enrich_slos() ← enhanced              │
        │       PRIMARY   : fixed threshold,  weight 1.0 / LLM   │
        │       SECONDARY : MI > 0.15 → adaptive percentile,     │
        │                   weight = MI score                    │
        │                                                        │
        │  ③ _normalize_weights()  →  Σ = 1                      │
        └────────────────────────────┬───────────────────────────┘
                                     │ contract + mi_scores
                                     ▼
                      Hub → decision_intelligence → TOPSIS
```

The service is the **contract builder**. Everything downstream — the compliance filter, TOPSIS weighting, the violation detector, the Gap Grade — operates on what comes out of here.

## 2. Folder structure

```text
services/metrics_manager/
├── app.py               # HTTP layer: /compute, /validate, profiling
├── metrics_handler.py   # MI estimator, percentiles, the two SLO builders
├── requirements.txt
├── SPEC.md
└── TECHNICAL_DOC.md
```

`metrics_handler.py` is the largest business module of the stack (599 lines), of which roughly **half is terminal formatting**. That ratio is intentional: the printed MI derivation is the deliverable, not a debug aid.

## 3. Internal design

### 3.1 The two entry points

Both routes follow the same shape — profile, compute MI, build SLOs, return — and differ only in what is treated as primary.

| | `/compute` (autonomous) | `/validate` (enhanced) |
|---|---|---|
| Primaries | `METRICS_REGISTRY` where `is_primary_objective` | The LLM's SLOs |
| Skipped in MI | `include_primaries=False` | `skip_metrics = {LLM metrics}` |
| Builder | `select_dynamic_slos` | `validate_and_enrich_slos` |
| Primary weight | `1.0` | The LLM's weight |
| Guard | history ≥ 5 points | `slos` and `history` both non-empty |

The skip is not an optimisation. A primary's weight is fixed by definition, so its MI score would be computed and then discarded — and printing five tables of derivation for a value nobody reads would drown the useful output.

`StepProfiler` splits each call into `mi_compute` and `mi_slos`, returned in `timings` and fed into the project's Excel timing export.

### 3.2 `compute_mi_scores` — orchestration

```python
y_vals = [1 if p.get("is_violation") else 0 for p in history]

for metric, reg in self.registry.items():
    skip if primary (autonomous) or in skip_metrics (enhanced)
    x_vals   = [p[metric] for p in history if p[metric] is not None]
    synced_y = [y_vals[i] for i, p in enumerate(history) if p[metric] is not None]
    if len(x_vals) < 5 or len(set(synced_y)) < 2: → 0.0
    score = self._compute_mi(x_vals, synced_y, metric, cycle)
```

The `synced_y` construction is the subtle part. `x_vals` drops points where the metric is missing; `y_vals` must drop **the same** points, or X and Y desynchronise and the MI is computed on mismatched pairs. The list comprehension re-indexes against the original history to keep them aligned. This is the kind of bug that produces plausible, wrong numbers.

`len(set(synced_y)) < 2` is the guard for SPEC C-1: with no violation anywhere in the history, there is only one class and MI is undefined. The score is `0.0` and no secondary SLO is created.

### 3.3 `_compute_mi` — the Kozachenko-Leonenko estimator

The formula, for a 1-D continuous X and a binary Y:

```
MI(X;Y) = H(X) − H(X|Y)
H(X|Y)  = Σ_c  (n_c / n) · H(X | Y = c)
score   = clamp( MI / H(Y),  0, 1 )
```

with the differential entropy estimated from k-nearest-neighbour distances:

```
H(X) ≈ ψ(n) − ψ(k) + ln(2) + mean( ln r_k )
```

where `ψ` is the digamma function and `r_k` the distance from each point to its k-th nearest neighbour. The `ln(2)` is the 1-D unit-ball volume term.

**`k = max(3, min(5, n // 10))`** — clamped to [3, 5]. Too small and the estimate is noisy; too large and it over-smooths. At n = 50 this gives k = 5.

The five printed steps map one-to-one onto the formula:

| Step | Content |
|---|---|
| 1 | `H(X)` with the ψ terms shown, plus the max-`r_k` point (the outlier) and the min-`r_k` point (the densest cluster) |
| 2 | `H(X\|Y=1)` — the violation class, printed first |
| 3 | `H(X\|Y=0)` — the normal class |
| 4 | The weighted average `H(X\|Y)`, with the arithmetic written out |
| 5 | `MI = H(X) − H(X\|Y)`, `H(Y)`, the ratio, and the verdict |

**The small-class fallback** (`n_c ≤ k+1`) sets `H(X|Y=c) ← H(X)`. This is deliberately conservative: making the conditional entropy equal to the global one makes that class contribute **zero** information gain, so a class too small to estimate can never inflate the score. Failing towards "no correlation" is the safe direction.

### 3.4 `_knn_entropy_ex` — the numerical core

```python
dists = np.abs(vals[:, None] - vals[None, :])   # full pairwise matrix
np.fill_diagonal(dists, np.inf)                 # exclude self-distance
r_k   = np.partition(dists, k-1, axis=1)[:, k-1]
valid = r_k > 0
return digamma(n) - digamma(k) + log(2) + mean(log_r[valid]),  r_k
```

Three points:

- `np.partition` is O(n) per row rather than a full O(n log n) sort — only the k-th element is needed. Total cost O(n²) for the matrix, which at n = 50 is 2500 entries and entirely negligible.
- `fill_diagonal(inf)` excludes each point's zero distance to itself, which would otherwise always be the nearest neighbour.
- The `r_k > 0` mask handles **duplicate values**. With `k` identical points, `r_k = 0` and `ln(0) = −∞` would poison the mean. Those points are excluded from the average. Relevant in practice: a VM reporting a constant CPU produces many duplicates.

### 3.5 `_adaptive_percentile` — the threshold for undeclared metrics

```python
cv = stdev(vals) / mean(vals)
cv < CV_LOW  (0.15) → P70   "stable"
cv < CV_HIGH (0.30) → P75   "normal"
otherwise           → P85   "volatile"

idx = int(len(sorted_vals) * (p_rank / 100))
return sorted_vals[min(idx, len - 1)]
```

The intent is simple: **the noisier the signal, the more headroom the threshold gets.** A stable metric can be bounded tightly at P70 without false positives; a volatile one needs P85 or it would breach constantly on its own noise.

Two implementation details. The percentile is a plain index into the sorted array, not an interpolated quantile — at n = 50, P70 is `sorted[35]`, an actual observed value rather than a computed one. And `mean == 0` returns `0.0` before the division, guarding against a metric that is entirely zero.

### 3.6 `select_dynamic_slos` — autonomous mode

```text
for each metric in the registry:
    is_primary_objective?
        → threshold = clamp(default_threshold)   # 28 ms for latency
          weight 1.0, target = threshold × 0.9, is_primary = True
    else if MI > 0.15:
        → _build_secondary_slo(), weight = MI score, is_primary = False
    else:
        → excluded from the contract
```

A metric below the MI threshold is not merely down-weighted, it is **absent**. TOPSIS will not see it as a criterion at all.

### 3.7 `validate_and_enrich_slos` — enhanced mode

Step 1 takes the LLM's SLOs and marks them primary. Three corrections are applied, each documented in the code as the fix for an observed production defect:

**Absolute units escape the bounds clamp.**

```python
if s.unit not in _ABSOLUTE_UNITS:      # ("cores", "GB")
    s.threshold = self._clamp_to_bounds(s.metric, s.threshold)
```

The registry's bounds for `cpu_usage` are `{min: 1.0, max: 99.0}` — a **percentage** range. Clamping `>= 0.5 cores` against them turned it into `>= 1.0`, silently doubling the client's requirement. The `_ABSOLUTE_UNITS` tuple mirrors the one in `hub/provider_arbitration.py`; the two must stay in sync.

**The detection target is asymmetric.**

```python
if s.operator in (">", ">="): s.target = max(s.target, s.threshold * 1.05)
else:                         s.target = min(s.target, s.threshold * 0.95)
```

Detection must fire **before** the contract breaks. For a ceiling that means aiming lower; for a floor, higher. Applying `min()` to both — the previous behaviour — meant a `>= 0.5` floor was detected at 0.475, i.e. *after* the breach.

**The LLM's weight is used directly**, falling back to `1.0` only if absent or non-positive. This is what makes the LLM's business judgement reach TOPSIS's weighting step.

Step 2 then adds secondary SLOs for correlated metrics the LLM did not mention.

### 3.8 `_build_secondary_slo` and the capacity floor

```python
floor = self._capacity_floor(metric)      # cpu_usage → ("cores", 1.0)
                                          # ram_usage → ("GB",    1.0)
if floor:  unit, threshold = floor;  operator = ">="       # NO clamp
else:      threshold = clamp(_adaptive_percentile(vals));  # percentile path
```

`cpu_usage` and `ram_usage` bypass the percentile entirely and become **absolute availability floors**: "this VM must have at least 1 free core". This aligns the autonomous mode with the enhanced one, where the LLM already expresses CPU/RAM in cores and GB — the two modes produce contracts of the same shape.

The consequence is that the adaptive-percentile path (§3.5) is currently **only reachable for a metric that is neither primary nor in the capacity table**. With three metrics in the registry, that is no metric at all in the default configuration. The mechanism is implemented and correct, but dormant. See L-2.

### 3.9 `_normalize_weights`

```python
total = sum(s.weight for s in slos)
s.weight = round(s.weight / total, 2)
```

Merges the two weight scales — business/LLM on one side, MI scores on the other — into a single distribution summing to 1. Rounding to 2 decimals means the sum can land on 0.99 or 1.01; TOPSIS renormalises internally, so do not assert exact equality in a test.

## 4. API reference

### `POST /compute` — autonomous mode

| Field | Type | Required | Description |
|---|---|---|---|
| `history` | array | **yes** | ≥ 5 points, each with metric values and `is_violation` |
| `all_vals` | object | no | `{metric: [values]}` — **accepted but unused** (L-1) |
| `cycle` | int | no | Log correlation, default 0 |

### `POST /validate` — enhanced mode

| Field | Type | Required | Description |
|---|---|---|---|
| `slos` | array | **yes** | The LLM contract, pre-filtered by the Hub |
| `history` | array | **yes** | Same shape as above |
| `cycle` | int | no | |

**Common response** — `slos`, `active_metrics`, `mi_scores`, `timings`, `timestamp`.

**Errors** — `400` guard failure · `500` internal error

**Example**

```bash
curl -X POST http://localhost:8004/compute -H "Content-Type: application/json" -d '{"cycle":1,"history":[{"latency":23.7,"cpu_usage":41.2,"ram_usage":63.0,"is_violation":false},{"latency":31.2,"cpu_usage":78.5,"ram_usage":64.1,"is_violation":true},{"latency":22.1,"cpu_usage":40.0,"ram_usage":62.8,"is_violation":false},{"latency":33.5,"cpu_usage":82.0,"ram_usage":65.0,"is_violation":true},{"latency":24.0,"cpu_usage":42.5,"ram_usage":63.2,"is_violation":false}]}'
```

Five points with both classes present — the minimum that exercises a real MI computation.

### `GET /health`

```json
{"status": "healthy", "service": "metrics_manager"}
```

Liveness only; the service has no external dependency to probe.

## 5. Configuration

| Variable | Default | Used for |
|---|---|---|
| `METRICS_MANAGER_PORT` | `8004` (+`PORT_OFFSET`) | Listening port |
| `MI_RELATIVE_THRESHOLD` | `0.15` | **Secondary SLO admission threshold** |
| `CV_LOW` / `CV_HIGH` | `0.15` / `0.30` | Volatility regime boundaries |
| `PERCENTILE_STABLE` / `_NORMAL` / `_VOLATILE` | `70` / `75` / `85` | Percentile per regime |
| `AUTONOMOUS_CPU_FLOOR_CORES` | `1.0` | Autonomous CPU capacity floor |
| `AUTONOMOUS_RAM_FLOOR_GB` | `1.0` | Autonomous RAM capacity floor |
| `HISTORY_WINDOW` | `50` | Displayed in the banner |
| `METRICS_REGISTRY` | 3 metrics | Candidate metrics, bounds, operators |

`MI_RELATIVE_THRESHOLD` is the most consequential knob in the service: it decides which metrics enter the contract at all. Lower it and TOPSIS gains criteria; raise it and the contract collapses to the primaries.

## 6. Dependencies

**Internal** — `shared.config`, `shared.models.SLO`, `shared.timing.StepProfiler`, `shared.logging_utils`.

**External** — `fastapi`, `uvicorn`, `numpy`, `scipy` (for `digamma` only).

**Runtime** — none. The service calls nobody: everything it needs arrives in the payload. It is the only fully self-contained service of the stack, which is also why it is the easiest to test.

## 7. Data model

Stateless. `MetricsHandler.__init__` stores a reference to the registry and nothing else. No cache, no history, no persistence — every contract is rebuilt from scratch.

## 8. Running it standalone

```bash
python -m services.metrics_manager.app
```

Second provider:

```bash
PORT_OFFSET=100 python -m services.metrics_manager.app
```

No dependency to start first. The `curl` in §4 exercises the full path against a running instance.

## 9. Logging and observability

| Marker | Level | Meaning |
|---|---|---|
| `🚀 Metrics Manager — Démarrage` | INFO | Banner: MI threshold, window, registry, primary objectives, percentiles |
| `⚙️ Cycle #N \| Mode AUTONOMOUS` | INFO | `/compute` entry |
| `🔎 Cycle #N \| Mode ENHANCED` | INFO | `/validate` entry |
| `📜 Historique des cycles` | INFO | Table of the input history with the violation column |
| `🔬 MI k-NN — Cycle #N \| Métrique : X` | INFO | Per-metric banner, `n` and `k` |
| `ÉTAPE 1 … ÉTAPE 5` | INFO | The full derivation |
| `📊 Scores MI (seuil = 0.15)` | INFO | Summary table, `✅ retenu` / `❌ ignoré` |
| `✅ SLOs sélectionnés/validés` | INFO | **The final contract** |
| `⏭ X — PRIMAIRE, MI ignoré` | DEBUG | Skip explanation |
| `⚠️ historique insuffisant` | WARNING | `400` |

This is the most verbose terminal of the stack: five tables per non-primary metric per cycle. That is by design — it is the explainability artefact. In practice, watch two lines:

- `📊 Scores MI` — which metrics crossed 0.15 this cycle.
- `✅ SLOs sélectionnés` — the contract TOPSIS is about to use.

The full derivation matters when a score is surprising: step 5 shows whether the low score comes from a small `MI` or from a large `H(Y)`, which are very different problems.

## 10. Testing

`tests/unit/test_mi_scoring.py` covers the MI computation.

Not covered: `_adaptive_percentile` and its three CV regimes, `_build_secondary_slo`'s capacity-floor branch, `validate_and_enrich_slos`'s three corrections (§3.7) — each of which fixes a defect that reached production once and would return silently if regressed.

```bash
pytest tests/unit/test_mi_scoring.py -v
```

## 11. Known limitations

| # | Limitation | Impact | Possible fix |
|---|---|---|---|
| **L-1** | `all_vals` is accepted by `/compute`, passed to `select_dynamic_slos`, and **never read** — the method takes `history` and derives values itself. | Dead parameter carried across the HTTP boundary. | Remove it from the signature and the Hub's payload. |
| **L-2** | The adaptive percentile is unreachable in the default configuration (§3.8): latency is primary, cpu/ram use the capacity floor. | A documented headline feature (P70/P75/P85) does not run. | Either state it as reserved for future metrics, or restore the percentile path for cpu/ram. |
| **L-3** | MI is zero when no violation has occurred (SPEC C-1). | Secondary metrics are discovered only after things degrade. | Use a continuous target (distance to threshold) instead of a binary flag. |
| **L-4** | `_ABSOLUTE_UNITS` is duplicated between this service and `hub/provider_arbitration.py`. | Divergence would resurrect the "0.5 cores → 1.0" defect. | Move it to `shared/config.py`. |
| **L-5** | Primary and secondary weights are normalised together (SPEC C-7). | A high MI score competes directly with a business priority. | Cap the total secondary mass, e.g. at 30 %. |
| **L-6** | `_knn_entropy` (without `_ex`) is defined and never called. | Dead code. | Remove. |
| **L-7** | The percentile is an array index, not an interpolated quantile (§3.5). | At small n the effective percentile is coarse. | `numpy.percentile`. |
| **L-8** | Partial test coverage (§10). | The three §3.7 corrections, each a fix for a real defect, are unverified. | One test per correction. |
| **L-9** | No authentication. | Any host can submit a forged history and shape the contract. | Acceptable on the demonstrator's private network. |
