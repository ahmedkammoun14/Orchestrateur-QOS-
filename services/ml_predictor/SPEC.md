# ML Predictor — Specification

> **Document type:** Specification (*what the service must do*).
> For *how it does it*, see [`TECHNICAL_DOC.md`](TECHNICAL_DOC.md).

| Field | Value |
|---|---|
| Service name | `ml_predictor` |
| Default port | `8003` (`8103` for provider-2, via `PORT_OFFSET`) |
| Component version | 2.2.0 |
| Status | Implemented |
| Position in the pipeline | Step 7 of the cycle — between history loading and decision |

---

## 1. Context

An orchestrator that migrates a service **after** an SLO has been breached is always late: by the time the violation is measured, the user has already experienced it. The whole point of the project is to decide on the **future** state rather than the present one — the README calls it *ML-driven proactive detection*, and the mechanism is stated plainly: the decision is made on the prediction; the measured value is only a safety net when no prediction is available.

This has a consequence that is easy to miss. **TOPSIS does not rank VMs on their measured metrics — it ranks them on their predicted ones.** `ml_predictor` therefore does not produce a side indicator: it produces the actual input of the decision. A wrong prediction is a wrong migration.

The prediction models themselves (ESN, LSTM, GRU, RNN) live in a **separate project**, `Api-Model-Predict`, exposed as three HTTP APIs on ports 5001/5002/5003 — one per metric. They have their own training datasets (192 000 rows) and their own lifecycle, and are deliberately not versioned in this repository.

`ml_predictor` is the orchestrator's side of that boundary. Its job is not to predict; it is to **obtain a usable prediction under all circumstances**. That distinction shapes the entire design: a three-level cascade in which each level activates only when the previous one cannot, so the cycle always receives a numeric answer — never an exception, never a gap.

The service also owns the **scale contract** between two systems that do not agree on units. The orchestrator reasons in milliseconds and percentages; the models were trained on values divided by 100. Every value crossing this boundary is converted, in both directions.

## 2. Objectives

| # | Objective |
|---|---|
| O-1 | Return a prediction horizon for every requested metric, in every situation, including when the models are unavailable. |
| O-2 | Degrade the prediction *quality* progressively rather than failing, and report which level produced each answer. |
| O-3 | Own the scale conversion between the orchestrator's units and the models' training convention. |
| O-4 | Protect the downstream pipeline from physically impossible predictions produced by an unstable model. |
| O-5 | Adapt to each model's own window size, read from the model itself rather than assumed. |
| O-6 | Predict the three metrics in parallel, so the cycle cost is that of the slowest, not their sum. |
| O-7 | Signal unambiguously when no prediction at all could be obtained, so the Hub can fall back to reactive detection. |

## 3. Functional requirements

### 3.1 Startup

| # | Requirement | Verification |
|---|---|---|
| **FR-1** | At startup the service SHALL query each ML API's `/hyperparameters` endpoint to obtain its `window_size`. | Startup log |
| **FR-2** | An API that does not answer SHALL leave that metric on the default window `HISTORY_WINDOW` = 50, with a warning. | API-down startup test |
| **FR-3** | Startup SHALL succeed even if all three APIs are unreachable. | Startup test |

### 3.2 The three-level cascade

| # | Requirement | Verification |
|---|---|---|
| **FR-4** | **Level 1** SHALL be attempted only when the supplied history contains at least `window_size` points. Below that it SHALL be **skipped silently**, without an error. | Warm-up observation |
| **FR-5** | Level 1 SHALL call `POST /predict_sequence` with the last `window_size` points and `horizon = 7`. | Network capture |
| **FR-6** | **Level 2** SHALL be attempted when level 1 is skipped or fails, calling `GET /predict?input_data=X` with the **last known value** only. | Log line `Niveau 2` |
| **FR-7** | **Level 3** SHALL be the final fallback: the last known value repeated 7 times, model `last_value_fallback`. | Log line `Niveau 3` |
| **FR-8** | Each level SHALL activate **only** if the previous one produced nothing usable. | Code review |
| **FR-9** | The level that produced the answer SHALL be identifiable in the response through the `model` field. | `curl` |
| **FR-10** | A level-3 outcome SHALL count as an API failure for the `all_apis_down` computation, even though it returns values. | Unit test |

### 3.3 Scale and bounds

| # | Requirement | Verification |
|---|---|---|
| **FR-11** | Values sent to the models SHALL be divided by 100 — the convention the models were trained on. | Network capture |
| **FR-12** | Values received from the models SHALL be denormalised back to the orchestrator's units (ms, %). | Unit test |
| **FR-13** | Denormalisation SHALL be **heuristic**, detecting by magnitude whether a value is already in the target scale. | Code review |
| **FR-14** | Every prediction SHALL be clamped: `latency` to `[0, LATENCY_MAX]`, `cpu`/`ram` to `[0, 100]`. | Unit test |
| **FR-15** | A clamp correction SHALL emit a warning naming the metric and the number of corrected points — an unstable model must remain visible. | Log inspection |
| **FR-16** | The response SHALL be truncated to the first 7 values, whatever the API returned. | Code review |

### 3.4 Request handling

| # | Requirement | Verification |
|---|---|---|
| **FR-17** | The service SHALL expose `POST /predict` accepting `{latency_history, cpu_history?, ram_history?}`. | `curl` |
| **FR-18** | It SHALL reject with `400` a payload without `latency_history`. | Unit test |
| **FR-19** | The three metrics SHALL be predicted **in parallel**. | Code review |
| **FR-20** | An absent or empty `cpu_history`/`ram_history` SHALL yield `null` for that metric — not an error, and not a failure count. | Unit test |
| **FR-21** | An empty `latency_history` SHALL yield a `no_data` response of seven zeros, counted as a failure. | Unit test |
| **FR-22** | Each prediction SHALL carry `predictions`, `confidence`, `uncertainty` and `model`. | `curl` |
| **FR-23** | `all_apis_down` SHALL be true only when **every requested** metric failed. | Unit test |
| **FR-24** | `all_apis_down` SHALL produce HTTP `502`, so the Hub can switch to reactive detection. | Hub behaviour |
| **FR-25** | `GET /health` SHALL probe the three ML APIs in parallel and report each independently. | `curl` |

## 4. Non-functional requirements

| # | Requirement | Target / rationale |
|---|---|---|
| **NFR-1 — Cycle budget** | A `/predict` call SHALL fit within a 6 s cycle in which the Hub issues **N calls in parallel**, one per VM. | Per-call timeout `POST_TIMEOUT` = 5 s. |
| **NFR-2 — Parallelism** | The three metrics SHALL cost the slowest, not the sum. | `asyncio.gather` |
| **NFR-3 — Never fail silently** | The service SHALL always return numeric values, or an explicit `502`. It SHALL never return a partial structure the Hub would have to guess about. | The cycle has no branch for "prediction missing". |
| **NFR-4 — Connection reuse** | A single `httpx.AsyncClient` SHALL be reused for the service's lifetime and closed on shutdown. | N×3 calls per cycle; per-call clients would exhaust sockets. |
| **NFR-5 — Model-driven configuration** | Window sizes SHALL come from the models, not from local configuration. | Retraining with a different window must not require a code change here. |
| **NFR-6 — Robust parsing** | The service SHALL tolerate a model returning a list, a JSON string, or a loosely formatted string. | The ML APIs are an external project with an unstable response contract. |
| **NFR-7 — Observability of the cascade** | The active level SHALL be visible per metric, per call, in the terminal. | The cascade statistics reported in the README (77.5 % / 3 % / 19 %) are counted from these lines. |
| **NFR-8 — Statelessness** | Beyond the window sizes read at startup, the service SHALL hold no state. | Every call is independent. |

## 5. Interface contract

### 5.1 Consumed — inbound `POST /predict`

Caller: the Hub, at step 7, **once per candidate VM in parallel**.

```jsonc
{
  "latency_history": [ {"value": 21.4}, {"value": 23.7} ],
  "cpu_history":     [ {"value": 41.2} ],
  "ram_history":     [ {"value": 63.0} ]
}
```

The Hub builds this from `history_loader`'s output. `cpu_history`/`ram_history` are **omitted** when the corresponding series is empty; `latency_history` is always present.

### 5.2 Consumed — the ML APIs (external project)

| Metric | Default URL | Endpoints used |
|---|---|---|
| latency | `http://localhost:5001/predict` | `/hyperparameters`, `/predict_sequence`, `/predict?input_data=`, `/health` |
| cpu | `http://localhost:5002/predict` | idem |
| ram | `http://localhost:5003/predict` | idem |

The three URLs are configured as the `/predict` form; the other endpoints are derived by string substitution.

### 5.3 Produced — response

```jsonc
{
  "predicted_latency": {
    "predictions": [24.1, 25.3, 26.8, 27.2, 28.9, 29.4, 30.1],
    "confidence":  0.87,
    "uncertainty": 0.12,
    "model":       "GRU"
  },
  "predicted_cpu":  { ... },
  "predicted_ram":  null,
  "all_apis_down":  false,
  "timestamp":      "2026-08-11T09:14:22.031Z"
}
```

`predictions` is always 7 values in orchestrator units (ms, %). `null` means the metric was not requested.

### 5.4 Responses

| Status | Condition |
|---|---|
| `200` | At least one metric predicted |
| `400` | `latency_history` absent |
| `502` | `all_apis_down` — every requested metric fell to level 3 |
| `500` | Unexpected internal error |

## 6. Constraints

| # | Constraint |
|---|---|
| **C-1** | **The ÷100 scale convention is a hard contract with the external project.** The models were trained on values divided by 100. Retraining on raw values without adjusting this service produces predictions that are consistently 100× off. This has been observed in practice — see the project memo on the latency dataset scale. |
| **C-2** | **Denormalisation is heuristic, not declared.** The direction is inferred from the magnitude of the returned values (`< 100` → multiply by 100 for latency; `≤ 1.0` → ×100 for cpu/ram). A genuine latency prediction of 80 ms returned in raw ms would be misread as 8000 ms. The heuristic works because of the demonstrator's operating ranges, not because it is sound in general. |
| **C-3** | **The horizon is hardcoded to 7.** It appears in the level-1 request, in the level-3 fallback, and in the response truncation — three places, no single constant. |
| **C-4** | **The fixed 7 has no time unit.** Seven steps at a 6 s cycle is roughly 42 seconds ahead, but nothing in the code states that. `HORIZON_ALERT = 3` in the config selects which of the seven the violation detector examines. |
| **C-5** | **Level 3 returns a *constant*, not a prediction.** It repeats the last value seven times. A downstream consumer cannot distinguish it from a genuinely flat forecast except by reading the `model` field. |
| **C-6** | **A model can collapse to a constant and still look healthy.** An under-trained model returning a near-constant value passes every check here — bounds, parsing, level 1 — and produces a plausible response. Only comparing predictions against subsequent measurements reveals it. This has occurred on the latency model. |
| **C-7** | **`confidence` and `uncertainty` come from the models when supplied, otherwise from defaults** (`0.8` and `1.0`). They are not computed here and their comparability across models is not guaranteed. |
| **C-8** | Requires the `Api-Model-Predict` project running on 5001/5002/5003. Without it the service starts, but every prediction is level 3. |

## 7. Out of scope

- Training, evaluating or selecting models — the `Api-Model-Predict` project.
- Choosing which metrics to predict — the Hub sends what it has.
- Deciding whether a prediction constitutes a violation — `decision_intelligence`.
- Storing predictions — they live only in the cycle's context.
- Estimating the models' own accuracy — the service reports the confidence it is given.
- Selecting the horizon step to act on — the Hub, via `HORIZON_ALERT`.

## 8. Traceability against the xQoS internship objectives

| xQoS objective | Contribution of this service |
|---|---|
| O1 — Multi-provider orchestrator | Supplies the predicted values TOPSIS ranks on. Both providers query the **same three shared ML APIs** — the models are federation-wide, not per-provider. |
| O2 — Intent–QoS relationship engine | None directly. It predicts metric values, not their interpretation. |
| O3 — Visualization & explainability | Provides `model`, `confidence` and `uncertainty` — the fields that let the dashboard state *why* a decision was taken and *how much* to trust it. The cascade level is the single most explanatory signal of the cycle. |
| O4 — Experimental validation | The cascade level distribution (77.5 % level 1, 3 % level 2, 19 % level 3, measured over a 10-minute two-provider session after warm-up) is one of the project's headline experimental results, and it is counted from this service's logs. |
