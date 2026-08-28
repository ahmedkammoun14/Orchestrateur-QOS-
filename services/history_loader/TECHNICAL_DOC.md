# History Loader — Technical Documentation

> **Document type:** Technical documentation (*how the service is built and how it works*).
> For *what it must do*, see [`SPEC.md`](SPEC.md).

| Field | Value |
|---|---|
| Package | `services.history_loader` |
| Entry point | `services/history_loader/app.py` |
| Framework | FastAPI + Uvicorn |
| Port | `config.HISTORY_LOADER_PORT` = `8007 + PORT_OFFSET` |
| Redis access | **Direct, read-only** |
| Lines of code | 312 (88 + 224) |

---

## 1. Role in the architecture

```text
                   ┌──────────────────────────────────────┐
                   │            Hub Core :8000            │
                   │                                      │
   SLO step ───────┤  POST /load  {service_vm}            │
                   │                                      │
   step 6 ─────────┤  POST /load × N  (asyncio.gather)    │
                   └──────────────┬───────────────────────┘
                                  │
                                  ▼
                   ┌──────────────────────────────────────┐
                   │       history_loader :8007           │
                   │                                      │
                   │   HistoryReader  (read-only)         │
                   │     LRANGE metrics:{vm}:{metric}     │
                   │     parse float → reverse            │
                   └──────────────┬───────────────────────┘
                                  │ direct connection
                                  ▼
                            Redis DB 0/1
                                  ▲
                                  │ writes only
                          database :8006  ◄── collector
```

The service sits **between Redis and the two statistical consumers** of the cycle. It is the mirror image of `database`: same backend, same key schema, same fail-fast startup, opposite direction.

Its position in the cycle is what determines the rest of the pipeline. Step 6 loads histories; step 7 immediately feeds them to `ml_predictor`. If step 6 returns short series, step 7's level-1 prediction is skipped and the cascade falls through — which is why `sizes` is the single most diagnostic field of the whole cycle.

## 2. Folder structure

```text
services/history_loader/
├── app.py             # HTTP layer: one route, validation, size default
├── history.py         # HistoryReader: LRANGE, parsing, reversal, health
├── requirements.txt
├── SPEC.md
└── TECHNICAL_DOC.md
```

The same two-layer split as every other service. `HistoryReader` mirrors `RedisClient` deliberately — same constructor signature, same injected logger, same fail-fast `PING`, same `health_check`. Reading the two side by side is the fastest way to understand the project's Redis conventions.

## 3. Internal design

### 3.1 Startup

```python
app    = FastAPI(title="History Loader", version="1.0.0")
reader = HistoryReader(logger)      # ← raises if Redis is down
```

`HistoryReader.__init__` opens the connection, issues a `PING`, logs `CRITICAL` and re-raises on failure. The exception fires at module import, so Uvicorn never binds the port.

The rationale is stronger here than for `database`. A history service that boots without Redis would answer every `/load` with empty series — and an empty series is a *legitimate* answer meaning "warm-up in progress". The Hub cannot distinguish the two. Failing at startup is the only way to keep the empty answer meaningful.

The banner prints `Mode : lecture seule`, which is the service's whole identity in one line.

> One small inconsistency: `_setup_logger()` here does **not** set `logger.propagate = False`, unlike `latency_manager` and `database`. With no root handler configured this changes nothing today, but adding one would duplicate every line of this service. See L-5.

### 3.2 `app.py` — validation and the `size` default

```python
if not vm_id or not isinstance(metrics, list) or not metrics:
    → 400

size = int(payload.get("size") or config.HISTORY_WINDOW)
```

The `or` in the size expression is doing more than it appears. It catches **three** cases at once: key absent, value `null`, and value `0`. A plain `payload.get("size", config.HISTORY_WINDOW)` would let `size=0` through and produce `LRANGE key 0 -1` — which in Redis means *the entire list*, the exact opposite of "zero points". The `or` is what prevents that inversion.

Everything else is delegated: `app.py` never touches Redis, never parses, never reverses.

### 3.3 `HistoryReader.load` — the read pipeline

```text
1. valid_metrics = [m for m in metrics if m in METRICS_REGISTRY]   ← registry guard
2. read_ts = now(UTC)                       ← ONE timestamp for the whole response
3. for each valid metric:
     a. key = "metrics:{vm_id}:{metric}"
     b. LRANGE key 0 size-1                 ← RedisError → [] for THIS metric, continue
     c. empty?  → histories[m] = [], sizes[m] = 0, continue
     d. parse each entry to float, dropping unparseable ones
     e. parsed.reverse()                    ← LIFO → chronological
     f. histories[m] = [{"value": v, "timestamp": read_ts} for v in parsed]
        sizes[m]     = len(parsed)
4. return {vm_id, histories, sizes, timestamp: now(UTC)}
```

Five points worth knowing:

**One `LRANGE` per metric, no pipeline.** Unlike `RedisClient`, which batches writes, this class issues sequential round-trips — three per call, twelve per cycle at four VMs. The reason is step 3b: a failure must be isolated *per metric*, which a pipeline's all-or-nothing `execute()` would prevent. Correctness of degradation was chosen over round-trip count. See L-1.

**The registry guard is a filter, not a validator.** An unknown metric name is dropped silently: it appears neither in `histories` nor in `sizes`. A caller that misspells `"latecy"` gets a `200` with that key simply missing — no error anywhere. Correct behaviour for the Hub, confusing when testing by hand.

**Three distinct empty-series causes, one identical output.** Redis error (3b), absent or empty key (3c), and all entries unparseable (3d) all produce `[]` with `sizes = 0`. Only the log level distinguishes them: `ERROR` for the first, `INFO` for the second, `WARNING` per entry for the third. When `sizes` is unexpectedly zero, the terminal is the only place the cause exists.

**`sizes` counts what survived parsing**, not what Redis returned. A list of 50 entries with 3 corrupted yields `sizes = 47`. This is the intended semantics — the consumer must know how many usable points it actually received.

**The reversal is the service's core transformation.** `LPUSH` stores newest-first, so `LRANGE 0 49` returns *the 50 most recent, most recent first*. `reverse()` turns that into chronological order. Get this backwards and the ML models are fed a time-reversed series — they will still produce a number, plausible-looking and wrong. There is no runtime check that could catch it; only the unit test on ordering protects this.

### 3.4 The timestamp — a structural placeholder

Every entry of every metric carries the **same** `timestamp`, computed once at step 2. Individual metric keys store bare floats; Redis holds no per-point timing at that granularity, and the docstring says so explicitly (*"Individual metric keys store raw floats only — no per-entry timestamp is available"*).

The field exists so the entry shape is uniform, not because it carries information. Two consequences:

- A consumer computing a real time delta between two points gets **zero**.
- The series is implicitly assumed to be regularly spaced at the cycle period. A collection gap produces a shorter series, not a marked hole.

Note also that `read_ts` (inside the entries) and the top-level `timestamp` are computed at two different moments — start and end of the read. The microsecond gap between them is the read duration; nothing consumes it.

### 3.5 `_parse_entry` — defensive conversion

```python
try:    return float(raw)
except (TypeError, ValueError):
        log warning; return None
```

`decode_responses=True` guarantees strings today, but the method also accepts `float`/`int` — deliberate insurance against a future storage-format change. Catching both exception types covers a `None` entry as well as a non-numeric string.

## 4. API reference

### `POST /load`

**Request**

| Field | Type | Required | Description |
|---|---|---|---|
| `vm_id` | string | **yes** | Target VM, e.g. `"edge1"` |
| `metrics` | array of string | **yes** | Non-empty. Names absent from `METRICS_REGISTRY` are silently ignored. |
| `size` | int | no | Max points per metric. Defaults to `HISTORY_WINDOW` = 50 when absent, null **or zero**. |

**Response `200`**

| Field | Type | Description |
|---|---|---|
| `vm_id` | string | Echo of the request |
| `histories` | object | `{metric: [{"value": float, "timestamp": str}]}`, **oldest first** |
| `sizes` | object | `{metric: int}` — points actually returned after parsing |
| `timestamp` | string | ISO 8601 UTC, time of the read |

**Errors** — `400` invalid payload · `500` internal error

**Example**

```bash
curl -X POST http://localhost:8007/load -H "Content-Type: application/json" -d '{"vm_id":"edge1","metrics":["latency","cpu_usage","ram_usage"],"size":50}'
```

Cross-check against Redis directly:

```bash
redis-cli -n 0 LRANGE metrics:edge1:latency 0 49
```

The `curl` output is that list **reversed** and cast to float.

### `GET /health`

```json
{"status": "healthy", "redis": "connected"}
```

Live `PING`. `"disconnected"` also emits a `⚠️ /health — Redis déconnecté` warning.

## 5. Configuration

| Variable | Default | Used for |
|---|---|---|
| `HISTORY_LOADER_PORT` | `8007` (+`PORT_OFFSET`) | Listening port |
| `REDIS_HOST` | `127.0.0.1` | Redis address |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_DB` | `0` | **Logical DB — the only provider isolation** (`1` for provider-2) |
| `HISTORY_WINDOW` | `50` | Default and effective `size` — a module constant, not an env var |
| `METRICS_REGISTRY` | 3 metrics | Set of admissible metric names |

`HISTORY_WINDOW = 50` is load-bearing beyond this service: it is the window the ML cascade's level 1 expects. Lowering it would silently push predictions to level 3 without any error appearing anywhere.

## 6. Dependencies

**Internal** — `shared.config`, `shared.redis_keys`, `shared.logging_utils`.

**External** — `fastapi`, `uvicorn`, `redis` (synchronous client).

**Runtime**

| Dependency | Nature | On failure |
|---|---|---|
| Redis | **hard at startup**, soft afterwards | Refuses to boot / empty series per metric |
| `database` service | **none, directly** | Only indirectly: without it, Redis stays empty and every series is `[]` |

The absence of a direct dependency on `database` is the point of the design: the read path survives a writer outage, it just observes a frozen history.

## 7. Data model

Read-only view of one key family:

| Key | Type | Written by | Read here |
|---|---|---|---|
| `metrics:{vm_id}:{metric}` | LIST of stringified floats, newest first, capped at 50 | `database.store_metrics` | `LRANGE 0 size-1` |

Not read by this service: `metrics:{vm_id}:history`, `slos:active`, `decisions:recent`, `llm:history`.

Manual inspection:

```bash
redis-cli -n 0 KEYS 'metrics:edge1:*'
redis-cli -n 0 LLEN metrics:edge1:latency
```

`-n 0` is provider-1, `-n 1` provider-2 — the most common way to look at the wrong dataset.

## 8. Running it standalone

```bash
python -m services.history_loader.app
```

Second provider:

```bash
REDIS_DB=1 PORT_OFFSET=100 python -m services.history_loader.app
```

Launch order: Redis → database → history_loader → Hub. The service will not boot before Redis; it *will* boot before `database`, and simply return empty series until metrics start arriving.

## 9. Logging and observability

| Marker | Level | Meaning |
|---|---|---|
| `🚀 History Loader — Démarrage` | INFO | Banner, `Mode : lecture seule` |
| `Redis connected at host:port` | INFO | `PING` succeeded, service will boot |
| `Redis initialization failed` | CRITICAL | Startup abort |
| `Load requested — vm_id=… metrics=… size=…` | INFO | One per call — 5 per cycle |
| `No history available for edge1/cpu_usage` | INFO | Key absent or empty — **normal during warm-up** |
| `LRANGE failed for …` | ERROR | Redis error on one metric; the others still served |
| `Unparseable entry for metric=…` | WARNING | One corrupted entry dropped |
| `History loaded — vm_id=… sizes={…}` | INFO | **The line that matters** |

`History loaded — sizes={'latency': 50, 'cpu_usage': 50, 'ram_usage': 50}` means the ML cascade can use level 1. Values well below 50 mean warm-up, and explain a level-3 fallback in the `ml_predictor` terminal at the same cycle. Reading these two terminals side by side is the intended diagnostic for the cascade statistics reported in the README.

## 10. Testing

There is **no** `tests/unit/test_history_loader.py` on `master`, and no test covering `HistoryReader`.

This is the most consequential coverage gap of the whole stack. The chronological reversal (§3.3) is a silent-failure transformation: reverse it and every model still returns a plausible number, computed on a time-reversed series, with no error, no warning, and no visible symptom. It is exactly the kind of defect a single three-line unit test would make impossible. See L-4.

## 11. Known limitations

| # | Limitation | Impact | Possible fix |
|---|---|---|---|
| **L-1** | Sequential `LRANGE` per metric instead of a pipeline (§3.3). | 3 round-trips per call, 12+ per cycle. | Pipeline the reads and reconstruct per-metric isolation from the results array. |
| **L-2** | Per-entry timestamps are the read time, not the measurement time (§3.4). | No consumer can compute a real time delta; gaps are invisible. | Read `metrics:{vm_id}:history`, which carries real timestamps, when true timing is needed. |
| **L-3** | Three different causes of an empty series produce an identical response (§3.3). | The caller cannot distinguish warm-up from a Redis failure. | Add a per-metric `status` field: `ok` / `empty` / `error`. |
| **L-4** | **No unit test at all**, including on the chronological reversal. | A reversal bug would be invisible and would corrupt every prediction. | Three cases: ordering, empty key, unparseable entry. |
| **L-5** | `_setup_logger()` omits `propagate = False`, unlike its sibling services (§3.1). | Duplicate log lines the day a root handler is configured. | Add the line for consistency. |
| **L-6** | Synchronous `redis-py` blocks the event loop during reads. | A Redis stall blocks the worker up to `socket_timeout` = 2 s — and the Hub calls this service N times in parallel. | Use `redis.asyncio`. |
| **L-7** | `size` is not capped and unknown metrics are silently dropped (SPEC C-5, FR-4). | A typo in a metric name yields a silent partial answer. | Return the ignored names in a `warnings` field. |
| **L-8** | No authentication. | Any host can read the full metric history of every VM. | Acceptable on the demonstrator's private network. |
