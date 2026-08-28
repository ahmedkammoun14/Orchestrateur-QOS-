# ML Predictor — Technical Documentation

> **Document type:** Technical documentation (*how the service is built and how it works*).
> For *what it must do*, see [`SPEC.md`](SPEC.md).

| Field | Value |
|---|---|
| Package | `services.ml_predictor` |
| Entry point | `services/ml_predictor/app.py` |
| Framework | FastAPI + Uvicorn (with `lifespan`) |
| Port | `config.ML_PREDICTOR_PORT` = `8003 + PORT_OFFSET` |
| State | Window sizes read at startup; nothing else |
| Lines of code | 375 (132 + 243) |

---

## 1. Role in the architecture

```text
   history_loader ──histories──► Hub ──_ml_payload()──┐
                                                       │ POST /predict  × N VMs
                                                       ▼      (parallel)
                              ┌─────────────────────────────────────────┐
                              │           ml_predictor :8003            │
                              │                                         │
                              │   3 metrics in parallel (gather)        │
                              │     ┌───────────────────────────────┐   │
                              │     │ L1  POST /predict_sequence    │   │
                              │     │     (window ≥ window_size)    │   │
                              │     │ L2  GET  /predict?input_data= │   │
                              │     │ L3  last_value × 7            │   │
                              │     └───────────────────────────────┘   │
                              │   ÷100 out · ×100 back · clamp          │
                              └──────────────┬──────────────────────────┘
                                             │
                     ┌───────────────────────┼───────────────────────┐
                     ▼                       ▼                       ▼
              ML API latency          ML API cpu             ML API ram
                  :5001                  :5002                  :5003
              (Api-Model-Predict — separate project, SHARED by both providers)

                                             │ predictions
                                             ▼
                              Hub step 8 → decision_intelligence → TOPSIS
```

The service is the **adapter between two systems that do not share conventions**: the orchestrator (milliseconds, percentages, 3 metrics, one HTTP call) and the model APIs (values ÷ 100, one API per metric, three different endpoint shapes). Everything it does is either cascading, converting, or defending.

Note the three ML APIs are **shared** by both provider stacks — they carry no `PORT_OFFSET`. Two `ml_predictor` instances query the same three models.

## 2. Folder structure

```text
services/ml_predictor/
├── app.py           # HTTP layer: lifespan, /predict, /health
├── predictor.py     # PredictorHandler: cascade, scale, clamp, parsing
├── requirements.txt
├── SPEC.md
└── TECHNICAL_DOC.md
```

## 3. Internal design

### 3.1 Startup — reading the models' own hyperparameters

```python
async def lifespan(app):
    await predictor.fetch_window_sizes()     # awaited before serving
    yield
    await predictor.close()                  # closes the shared client
```

`fetch_window_sizes` calls `GET /hyperparameters` on each API in parallel and stores the returned `window_size` per metric. If an API does not answer, that metric keeps the default `HISTORY_WINDOW` = 50 and a warning is logged.

This is a real design choice, not boilerplate: **the window size belongs to the model, not to the orchestrator.** Retraining a model with a window of 30 instead of 50 changes when level 1 becomes available, and this service picks that up automatically at the next restart.

The failure mode is worth knowing. If the API is down at startup, the default of 50 is kept — and 50 is also the maximum number of points `history_loader` can ever return. Level 1's condition `len(history) >= window_size` then requires a *completely full* history to fire. If the real model window is smaller, the service is needlessly stuck at level 2 until it is restarted with the API up. See L-1.

The single `httpx.AsyncClient` is created in `__init__` (at import) and closed in the lifespan's teardown. With N VMs × 3 metrics per cycle, per-call clients would churn sockets.

### 3.2 `handle` — three metrics in parallel

```python
results = await asyncio.gather(
    self._predict_metric("latency", payload.get("latency_history", [])),
    self._predict_metric("cpu",     payload.get("cpu_history", [])),
    self._predict_metric("ram",     payload.get("ram_history", [])),
)
```

Each `_predict_metric` returns a triple `(metric, result_or_None, api_failed)`. The `api_failed` flag is separate from the result being `None`, and that separation carries the whole `all_apis_down` logic:

```python
metrics_requested = count of metrics whose result is not None
all_apis_down     = (api_failures == metrics_requested) and metrics_requested > 0
```

| Case | result | api_failed | Counted in `metrics_requested`? |
|---|---|---|---|
| Level 1 or 2 succeeded | dict | `False` | yes |
| Level 3 fallback | dict | `True` | yes |
| Metric not requested (empty history, cpu/ram) | `None` | `False` | **no** |
| `latency` with empty history | `no_data` dict | `True` | yes |

So a cycle where only latency was requested and fell to level 3 yields `all_apis_down = true` → `502`. A cycle where latency succeeded and cpu fell to level 3 yields `false`. The metric is *"did every metric we actually asked for fail?"*, not *"is any API down?"* — the name is misleading, the semantics are correct.

### 3.3 The cascade — `_predict_metric`

```text
history empty?
    metric ≠ latency  → (None, api_failed=False)      ← not requested
    metric = latency  → ([0.0]×7, "no_data", failed)  ← requested but nothing to work with

── Level 1 ── if len(history) >= window_size
    sequence = [h["value"] / 100.0 for h in history[-window_size:]]
    POST {api}/predict_sequence {"sequence": …, "horizon": 7}
    200 + parseable list → return, model "sequence_model"
    exception → warn, fall through

── Level 2 ──
    GET {api}/predict?input_data={last_value / 100.0}
    200 + parseable → return, model "point_model"
    exception → warn, fall through

── Level 3 ──
    ERROR log
    return ([last_val] × 7, confidence 0.5, uncertainty 1.0,
            model "last_value_fallback", api_failed=True)
```

Four points that matter:

**Level 1's skip is silent, and that is deliberate.** `if len(history) >= window_size` is a plain condition, not a try/except — no warning, no error. During warm-up, every call goes straight to level 2. This is exactly what makes the warm-up period *observable* rather than alarming: the README's cascade statistics are measured *after* warm-up precisely because the pre-warm-up level-2 traffic is normal, not degraded.

**A non-200 status is not caught.** The `try` blocks catch exceptions; a `200` with an unparseable body simply falls through to the next level, and a `500` from the API falls through too — but without a warning, since no exception was raised. Only network errors and timeouts produce the `⚠️ Niveau N échoué` line. So the absence of a warning does not mean level 1 was skipped for window reasons; it may have returned garbage.

**Level 2 uses only the last value.** It sends a single point and asks for a forecast. It is a genuinely weaker prediction, not a cheaper path to the same answer — hence the 3 % share in the measured distribution: it is rarely the level that ends up producing the answer.

**Level 3 is not a prediction.** `[last_val] * 7` is a flat line. It keeps the pipeline numerically alive so TOPSIS can still rank, but it means "we do not know". The `model` field is the only way a consumer can tell.

### 3.4 The ÷100 / ×100 scale contract

This is the most consequential five lines of the service.

**Outbound**, in level 1 and level 2:

```python
sequence  = [h["value"] / 100.0 for h in history[-window_size:]]
input_val = last_val / 100.0
```

**Inbound**, in `_denormalize`:

```python
if metric in ["cpu", "ram"]:
    if max(preds) > 1.0: return preds          # already in %
    return [p * 100.0 for p in preds]
if metric == "latency":
    if max(preds) < 100.0: return [p * 100.0 for p in preds]
return preds
```

The models were trained on values divided by 100; the orchestrator speaks milliseconds and percentages. Every crossing converts.

The inbound direction is **heuristic**, inferred from magnitude rather than declared. For latency, a returned value below 100 is assumed to be in the ÷100 scale and multiplied; above 100 it is assumed to be raw milliseconds. This works for the demonstrator's operating range (5–230 ms → 0.05–2.3 in model scale) but it has a blind spot: a model genuinely returning `80.0` meaning 80 ms would be read as 8000 ms. Nothing detects it.

> **Failure mode already observed.** If the models are retrained on raw values without changing this service, every prediction comes out **100× too high** — latency predictions in the thousands of milliseconds, every VM permanently in violation, constant migrations. The symptom is unmistakable once you know it; the cause is invisible from the orchestrator's logs. This is documented in the project's memo on the latency dataset scale.

### 3.5 `_clamp` — defending against an unstable model

```python
latency  → [0, LATENCY_MAX]      (0–2000 ms)
cpu/ram  → [0, 100]
```

An ESN whose reservoir diverges can emit negative latencies or CPU above 100 %. Unclamped, those values propagate into TOPSIS's min-max normalisation — where a single absurd value rescales the entire criterion column and corrupts the ranking of *every* VM, not just the affected one. Clamping is what keeps one bad model from poisoning the whole decision.

The warning is as important as the clamp:

```
⚠️ latency — 3 prédiction(s) hors bornes [0, 2000] corrigée(s) (modèle instable ?)
```

Clamping hides the symptom from the pipeline; the warning is the only place it remains visible. If this line appears regularly, the model needs retraining — the orchestrator will keep working and keep deciding on corrected garbage.

### 3.6 `_parse_api_list` — three tolerated response shapes

```python
list          → [float(x) for x in raw]
str           → json.loads, else re.findall(r"[\d.]+", raw)
anything else → []
```

The regex fallback is a pragmatic concession to an external project whose response format is not stable. Note `[\d.]+` does **not** match a minus sign: negative values in a string response are parsed as positive. Harmless today since `_clamp` floors at 0 anyway, but it means a diverging model looks less broken than it is.

### 3.7 `_calc_uncertainty`

```python
uncertainty = |mean(confidence_high) − mean(confidence_low)| / mean(predictions)
```

A relative interval width, when the API supplies `confidence_high` / `confidence_low`. Otherwise the default is `1.0` — the maximum. Since level 3 also returns `1.0`, an uncertainty of exactly 1.0 means "either no interval was supplied, or this is a fallback" — the two are indistinguishable without reading `model`.

## 4. API reference

### `POST /predict`

**Request**

| Field | Type | Required | Description |
|---|---|---|---|
| `latency_history` | array | **yes** | `[{"value": float}, …]`, chronological |
| `cpu_history` | array | no | Omitted by the Hub when the series is empty |
| `ram_history` | array | no | idem |

**Response `200`**

| Field | Description |
|---|---|
| `predicted_latency` / `predicted_cpu` / `predicted_ram` | Prediction object, or `null` if not requested |
| `…​.predictions` | 7 values, orchestrator units (ms, %) |
| `…​.confidence` | From the model, default `0.8`; `0.5` at level 3 |
| `…​.uncertainty` | Relative interval width, default `1.0` |
| `…​.model` | **The cascade indicator** — model name, or `last_value_fallback`, or `no_data` |
| `all_apis_down` | `true` → the Hub switches to reactive detection |
| `timestamp` | ISO 8601 UTC |

`400` `latency_history` absent · `502` `all_apis_down` · `500` internal error

**Example**

```bash
curl -X POST http://localhost:8003/predict -H "Content-Type: application/json" -d '{"latency_history":[{"value":21.4},{"value":23.7},{"value":25.1}]}'
```

With only 3 points and a window of 50, this returns a level-2 or level-3 answer — the quickest way to exercise the fallback path by hand.

### `GET /health`

```json
{"service": "healthy", "latency_api": "healthy", "cpu_api": "down", "ram_api": "healthy"}
```

Probes each API's `/health` (derived from the configured `/predict` URL) in parallel, 1 s timeout. Honest: it reports the real dependencies, per API.

## 5. Configuration

| Variable | Default | Used for |
|---|---|---|
| `ML_PREDICTOR_PORT` | `8003` (+`PORT_OFFSET`) | Listening port |
| `ML_RTT_URL` | `http://localhost:5001/predict` | Latency model |
| `ML_CPU_URL` | `http://localhost:5002/predict` | CPU model |
| `ML_RAM_URL` | `http://localhost:5003/predict` | RAM model |
| `POST_TIMEOUT` | `5.0` s | Timeout of the shared client |
| `HISTORY_WINDOW` | `50` | Default window when `/hyperparameters` is unreachable |
| `LATENCY_MAX` | `2000.0` ms | Upper clamp for latency |
| `HORIZON_ALERT` | `3` | **Not used here** — the Hub uses it to pick which horizon step to act on |

The three URLs are given in their `/predict` form; `/hyperparameters`, `/predict_sequence` and `/health` are derived by `str.replace("/predict", …)`. A URL not containing `/predict` therefore breaks all three derivations silently.

The horizon **7** is not configurable: it is a literal in three places (level-1 request, level-3 fallback, response truncation).

## 6. Dependencies

**Internal** — `shared.config`, `shared.logging_utils`.

**External** — `fastapi`, `uvicorn`, `httpx`. No ML library: all inference is remote, which is what keeps this service light and the models independently deployable.

**Runtime**

| Dependency | Nature | On failure |
|---|---|---|
| ML API latency `:5001` | soft | Level 3 for latency |
| ML API cpu `:5002` | soft | Level 3 for cpu |
| ML API ram `:5003` | soft | Level 3 for ram |
| All three | — | `all_apis_down` → `502` → the Hub falls back to reactive detection |

The service never hard-fails on the models. That is the whole point of the cascade.

## 7. Data model

Stateless apart from `window_sizes`, read once at startup. Nothing is persisted: predictions live only inside the cycle's context and are never written to Redis or Excel.

## 8. Running it standalone

```bash
python -m services.ml_predictor.app
```

Second provider:

```bash
PORT_OFFSET=100 python -m services.ml_predictor.app
```

The three ML APIs come from the separate `Api-Model-Predict` project and must be started first — see `ETAPES_LANCEMENT_PROJET.md` for the training and launch order. Without them the service starts normally and answers every call at level 3.

## 9. Logging and observability

| Marker | Level | Meaning |
|---|---|---|
| `🚀 ML Predictor — Démarrage` | INFO | Banner with the three URLs and the horizon |
| `✅ Hyperparamètres chargés — latency \| window_size = 50` | INFO | Model window read successfully |
| `⚠️ Hyperparamètres indisponibles pour cpu` | WARNING | API down at startup → default window |
| `📡 /predict — requête reçue \| latency : 50 pts` | INFO | One per call — **the point count is the cascade predictor** |
| `✅ Niveau 1 (sequence) — latency \| 7 valeurs prédites` | INFO | Nominal |
| `✅ Niveau 2 (point unique) — cpu` | INFO | Degraded, or warm-up |
| `⚠️ Niveau 1 échoué pour latency : …` | WARNING | Exception only — a bad 200 falls through silently |
| `❌ Niveaux 1 et 2 épuisés — fallback last_value` | ERROR | Level 3 |
| `⚠️ latency — 3 prédiction(s) hors bornes` | WARNING | **Unstable model — investigate** |
| `🤖 Prédictions générées — APIs OK : 2/3` | INFO | Per-call summary |

This terminal is where the cascade statistics come from. Reading it against the `history_loader` terminal at the same cycle explains every level-2 fallback: `sizes` below `window_size` there ⇒ level 1 skipped here.

The two lines to react to are `hors bornes` (a model is diverging) and a persistent `Niveaux 1 et 2 épuisés` (an API is down, and every decision is being made on flat lines).

## 10. Testing

There is **no** `tests/unit/test_ml_predictor.py` on `master`.

The two functions that most warrant one are pure and trivially testable: `_denormalize` — whose heuristic has a documented blind spot (§3.4) — and `_clamp`. A scale regression here is invisible in the logs and corrupts every decision downstream, which makes it the highest-value missing test in the service.

## 11. Known limitations

| # | Limitation | Impact | Possible fix |
|---|---|---|---|
| **L-1** | Window sizes are read **once**, at startup (§3.1). | An API that comes up late leaves the service on the default 50 until restarted, needlessly stuck at level 2. | Refetch periodically, or on the first level-1 failure. |
| **L-2** | Denormalisation is heuristic (§3.4). | A latency genuinely returned as 80 ms is read as 8000 ms. | Have the APIs declare their scale in `/hyperparameters`. |
| **L-3** | Horizon 7 is hardcoded in three places (SPEC C-3). | Changing it means finding all three. | A single `PREDICTION_HORIZON` constant in `shared/config.py`. |
| **L-4** | A non-200 API response falls through **without a warning** (§3.3). | A silently broken API looks identical to a normal warm-up skip. | Log a warning on any non-200. |
| **L-5** | Level 3 is indistinguishable from a flat forecast except via `model`. | A consumer reading only `predictions` cannot tell. | Add an explicit `level: 1\|2\|3` field. |
| **L-6** | `_parse_api_list`'s regex drops minus signs (§3.6). | A diverging model looks less broken than it is. | Use `[-\d.]+`. |
| **L-7** | A model collapsed to a constant passes every check (SPEC C-6). | Predictions look valid and are worthless; only comparison against later measurements reveals it. | Track prediction variance and warn when it approaches zero. |
| **L-8** | No unit test (§10). | The scale conversion — the most consequential logic here — is unverified. | Two pure-function tests on `_denormalize` and `_clamp`. |
| **L-9** | `all_apis_down` is a misleading name (§3.2). | It means "every requested metric fell to level 3", not "the APIs are down". | Rename to `no_prediction_available`. |
