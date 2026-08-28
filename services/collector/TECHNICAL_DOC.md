# Collector Service — Technical Documentation

> **Document type:** Technical documentation (*how the service is built and how it works*).
> For *what it must do*, see [`SPEC.md`](SPEC.md).

| Field | Value |
|---|---|
| Package | `services.collector` |
| Entry point | `services/collector/app.py` |
| Framework | FastAPI + Uvicorn (with `lifespan`) |
| Port | `config.COLLECTOR_PORT` = `8005 + PORT_OFFSET` |
| State | In-memory cache + two per-VM EMA tables |
| Lines of code | 374 (128 + 246) |

---

## 1. Role in the architecture

```text
                        ┌───────────────────────────────────────┐
                        │         collector :8005               │
                        │                                       │
   VM agents            │   ┌─────────────────────────────┐     │
   :8200/8201/8202      │   │  background loop  (1 s)     │     │
        ▲               │   │  poll_once() → gather × N   │     │
        └───GET /metrics┼───┤  updates: timeout, EMA      │     │
            (parallel)  │   │  fills:   self.cache        │     │
                        │   └──────────────┬──────────────┘     │
                        │                  ▼                    │
   Hub ──POST /collect──┼──►  handle() reads the cache          │
        ◄──results──────┼──   NO network call                   │
                        │                  │                    │
                        │                  ▼ BackgroundTasks    │
                        └──────────────────┼────────────────────┘
                                           ▼
                                 database :8006  /store/metrics
                                 (direct — validated exception)
```

Two rhythms, deliberately unsynchronised. The **left half** runs at 1 s and touches the network. The **right half** runs at the cycle's 6 s and touches only memory. The cache is the seam between them, and it is what removes 1.4–1.8 s from the cycle's critical path.

## 2. Folder structure

```text
services/collector/
├── app.py          # HTTP layer: lifespan, /collect, /health
├── collector.py    # CollectorHandler: loop, polling, EMAs, cache, forwarding
├── requirements.txt
├── SPEC.md
└── TECHNICAL_DOC.md
```

`CollectorHandler` is the only stateful business object of the whole stack: it holds `cache`, `vm_timeouts` and `vm_reliability`, all in memory, all lost on restart.

## 3. Internal design

### 3.1 Lifespan — the ordering that matters

```python
@asynccontextmanager
async def lifespan(app):
    await handler.poll_once()              # ① blocking, awaited
    handler.launch_background_polling()    # ② fire-and-forget task
    yield                                  # ③ service accepts traffic
    await handler.shutdown()               # ④ cancel the task
```

Step ① is **awaited before the service is ready**, so the very first `/collect` already finds a populated cache. Without it, the first cycle would receive the cold-start entries of FR-21 (`reachable = false`, all metrics `null`) — the Hub would treat every VM as unreachable and the first decision would be made blind.

Uvicorn only binds the port after `yield`, so the startup poll delays readiness by one full poll duration. That is intentional and is why the launch runbook tolerates the collector taking a couple of seconds longer than its siblings.

Note that `handler = CollectorHandler()` is constructed at **module import**, before the event loop exists. The constructor therefore creates no task and touches no network — it only initialises dictionaries. Moving any `async` work into it would break at import time.

### 3.2 The background loop

```python
async def _background_loop(self):
    while True:
        try:
            await self.poll_once()
        except Exception as e:
            logger.error(...)              # swallow, never break
        await asyncio.sleep(COLLECTOR_POLL_INTERVAL)
```

Two properties:

- **The loop is unkillable by an exception.** Any failure is logged and the next iteration proceeds. Since this loop is the only thing keeping the cache fresh, letting it die would silently freeze all metrics at their last values — with `/collect` still returning `200` and stale data. The broad catch is the right call here.
- **The interval is a `sleep` *after* the work, not a fixed period.** Effective cadence is `poll duration + 1 s`, so with 150 ms polls the real interval is ~1.15 s. It drifts with VM latency rather than skipping iterations, which is the safer behaviour.

`launch_background_polling` is idempotent (`if self._poll_task is None or self._poll_task.done()`), so a second call cannot spawn a duplicate loop writing to the same cache.

### 3.3 `poll_once` — parallel acquisition

```python
async with httpx.AsyncClient() as client:
    results = await asyncio.gather(*[self._poll_vm(client, vid, info) ...])
for r in results:
    self.cache[r["vm_id"]] = r
```

One client shared across all VMs of an iteration, so connections are pooled. Cost is the **slowest** VM, not the sum.

The cache update happens **after** `gather` completes, in a synchronous loop with no `await` inside. That makes it effectively atomic against the event loop: a concurrent `/collect` sees either the whole previous iteration or the whole new one, never a half-updated mix. This is an important property and it depends on the absence of `await` in that loop — easy to break inadvertently.

### 3.4 `_poll_vm` — acquisition, and the extensibility mechanism

```python
metrics = {k: v for k, v in data.items() if k not in ("vm_id", "timestamp")}
```

This one line is what NFR-7 rests on. The collector does **not** know the list of metrics. It captures everything the agent returns, minus the two envelope fields, and stores it all. Filtering happens later, at read time, against the cycle's `active_metrics`.

The consequence is that adding a metric requires touching neither this service nor the agent contract: declare it in `METRICS_REGISTRY`, teach the agent to return it, and it flows through untouched. It also means `total_cores` and `total_ram_gb` — which are *capacities*, not metrics — arrive by the same path and are re-exposed explicitly in `handle()`.

On a non-200 response or any exception, `_handle_vm_failure` degrades the reliability, logs a warning, and returns an entry with **no metric keys at all** — only `vm_id`, `reliability`, `reachable`, `collect_ms`, `timestamp`. This matters in §3.6.

### 3.5 The two EMAs

Both use the same `α = COLLECTOR_RELIABILITY_ALPHA = 0.2`, meaning a new observation weighs 20 % against 80 % of accumulated history.

**Adaptive timeout** — updated on success only:

```
timeout ← clamp( α × (observed × 1.5) + (1−α) × timeout ,  0.5 s , 5.0 s )
```

The `×1.5` factor is the safety margin: the timeout converges towards 1.5× the observed response time, not towards it. A VM answering steadily at 100 ms converges to a 150 ms timeout, an order of magnitude tighter than the 2 s base — so a genuine failure is detected in 150 ms instead of 2 s.

Not updating on failure (FR-12) is the subtle part. If failures fed the EMA with the timeout value itself, a flapping VM would ratchet its timeout upward towards the 5 s ceiling, making it progressively slower to declare dead. Ignoring failures keeps the timeout anchored to observed *healthy* behaviour.

**Reliability** — updated on every poll:

```
reliability ← α × (1.0 if success else 0.0) + (1−α) × reliability
```

Convergence at α = 0.2 and one poll per second: a VM that dies falls from 1.0 to ~0.33 in 5 s and ~0.11 in 10 s; recovering, it returns above 0.9 in ~11 s. The asymmetry between "clearly degraded" and "confirmed healthy again" is what makes the score usable as a stability indicator rather than a liveness flag.

### 3.6 `handle` — the cache read

```python
for vm_id in self.vm_registry:              # iterate the REGISTRY, not the cache
    cached = self.cache.get(vm_id)
    if cached is None: → cold-start entry (all None, reachable False)
    filtered = {m: cached.get(m) for m in active_metrics}
```

Iterating `vm_registry` rather than `cache` guarantees the response always has exactly N entries, in registry order, whatever the cache contains. The Hub can index positionally without checking for gaps.

`cached.get(m)` returning `None` is the normal outcome for an unreachable VM: `_handle_vm_failure` produced an entry with no metric keys (§3.4), so every requested metric resolves to `None` while `reachable` is `False`. The two signals are consistent by construction rather than by an explicit branch.

`collect_ms` in the response is the round-trip of the **last background poll**, not of this call — `/collect` makes no network call at all. Same for `timestamp`, which comes from the agent's own clock when it supplies one.

### 3.7 Persistence — `_forward_to_database`

Scheduled by `app.py` through FastAPI's `BackgroundTasks`, which runs it **after the response has been sent**. The Hub therefore never waits on the database.

```python
if not vm_id or not vm_result.get("reachable"): continue     # skip unreachable

metrics = {k: v for k, v in vm_result.items()
           if k not in ("vm_id", "reliability", "reachable", "timestamp")}
```

Two consequences of that dict comprehension:

- `collect_ms` is **not** excluded, so it is persisted as if it were a metric. It survives only because `database.store_metrics` iterates `METRICS_REGISTRY` and ignores anything not declared there — the two services' filters happen to compose correctly. Fragile, but currently correct. See L-3.
- Unreachable VMs are skipped entirely, so storage never contains null-metric rows. A gap in a series means "VM was down", which is the semantics `history_loader` and the ML cascade expect.

The forward uses `async_post_with_retry` — the same helper as `latency_manager`, 3 attempts with 2 s linear backoff. Calls are **sequential** across VMs inside a single `AsyncClient`, so a fully unreachable database costs `N × 19 s` in the background. Invisible to the Hub, but it means background tasks can outlive several cycles and pile up. See L-2.

### 3.8 `/health` — live, not cached

Unlike `/collect`, the health endpoint performs real parallel calls to every VM's `/health` with a 1 s timeout. It deliberately ignores the cache, so it reports current reachability rather than last-known state. Useful during the launch runbook, when the cache may not exist yet.

## 4. API reference

### `POST /collect`

**Request**

| Field | Type | Required | Description |
|---|---|---|---|
| `active_metrics` | array of string | **yes** | Non-empty. The Hub sends `METRICS_REGISTRY` minus `latency`. |
| `cycle` | int | **yes** | Cycle number, used for log correlation only |

**Response `200`**

| Field | Description |
|---|---|
| `results[]` | One entry per registered VM, in registry order |
| `results[].vm_id` | VM identifier |
| `results[].<metric>` | One key per requested metric; `null` if unreachable |
| `results[].total_cores` / `total_ram_gb` | Capacities reported by the agent |
| `results[].reliability` | EMA score, 3 decimals |
| `results[].reachable` | Boolean |
| `results[].collect_ms` | Round-trip of the **last background poll** |
| `results[].timestamp` | Agent's timestamp when available |
| `collect_timings` | `{vm_id: ms}` — feeds the Hub's profiler |
| `collect_max_ms` | Slowest VM of the last poll |

`400` — `{"detail": "active_metrics and cycle are required"}`

**Example**

```bash
curl -X POST http://localhost:8005/collect -H "Content-Type: application/json" -d '{"active_metrics":["cpu_usage","ram_usage"],"cycle":1}'
```

### `GET /health`

```json
{"service": "healthy", "vms": {"edge1": "online", "edge1b": "online", "cloud1": "offline"}}
```

`online` (HTTP 200) · `error` (any other status) · `offline` (unreachable).

## 5. Configuration

| Variable | Default | Used for |
|---|---|---|
| `COLLECTOR_PORT` | `8005` (+`PORT_OFFSET`) | Listening port |
| `COLLECTOR_POLL_INTERVAL` | `1.0` s | Background loop interval |
| `COLLECTOR_TIMEOUT_BASE` | `2.0` s | Initial per-VM timeout |
| `COLLECTOR_MIN_TIMEOUT` | `0.5` s | Lower clamp |
| `COLLECTOR_MAX_TIMEOUT` | `5.0` s | Upper clamp |
| `COLLECTOR_TIMEOUT_FACTOR` | `1.5` | Safety margin over observed time |
| `COLLECTOR_RELIABILITY_ALPHA` | `0.2` | **Shared** by both EMAs |
| `POST_RETRY_COUNT` / `POST_RETRY_BACKOFF` | `3` / `2.0` s | Database forwarding retry |
| `DATABASE_SERVICE_URL` | derived | Persistence target |
| `VM_REGISTRY` | derived from `PROVIDER_REGISTRY` | The VMs this instance polls |

One `α` drives both the timeout and the reliability EMAs. Tuning one necessarily retunes the other — a real coupling, worth knowing before touching it.

`VM_REGISTRY` is derived at import from `PROVIDER_ID`: `provider-1` sees `edge1/1b/1c + cloud1`, `provider-2` sees the other four, `all` sees the eight.

## 6. Dependencies

**Internal** — `shared.config`, `shared.http_utils.async_post_with_retry`, `shared.logging_utils`.

**External** — `fastapi`, `uvicorn`, `httpx`.

**Runtime**

| Dependency | Nature | On failure |
|---|---|---|
| VM agents | soft | `reachable = false`, reliability decays |
| Database service | soft | Metrics lost after retries; the cycle is unaffected |
| Hub | none | The collector does not call the Hub |

The service starts and runs fine with **every** VM down. It will report all of them unreachable, which is exactly what the Hub needs to know.

## 7. State model

Three in-memory structures, none persisted:

| Structure | Content | Lost on restart |
|---|---|---|
| `cache` | `{vm_id: last poll result}` | Yes — rebuilt by the startup poll |
| `vm_timeouts` | `{vm_id: adaptive timeout}` | Yes — resets to 2.0 s, reconverges in a few seconds |
| `vm_reliability` | `{vm_id: EMA score}` | Yes — **resets to 1.0**, so a VM down for an hour looks perfectly reliable right after a collector restart |

The reliability reset is the one with real consequences: after a restart, the score is optimistic and takes ~10 s of polling to reflect reality.

## 8. Running it standalone

```bash
python -m services.collector.app
```

Second provider:

```bash
PROVIDER_ID=provider-2 PORT_OFFSET=100 python -m services.collector.app
```

Launch order: VM agents → Redis → database → collector. Starting it before the agents is harmless — it will report everything unreachable and recover on its own once they come up.

## 9. Logging and observability

| Marker | Level | Meaning |
|---|---|---|
| `📡 Collector Service — Démarrage` | INFO | Import-time banner |
| `🔧 Sondage initial des VMs` | INFO | Startup poll in progress |
| `✅ Collector Service prêt` | INFO | Full config: VM count, base timeout, α, interval |
| `🔁 Sondage de fond démarré` | INFO | Background loop live |
| `✅ edge1 — cpu_usage=41.2 … \| fiabilité : 0.98 \| temps : 87.3 ms` | INFO | **One line per VM per second** |
| `⚠️ edge1 — injoignable \| raison : ConnectTimeout` | WARNING | Failed poll, with the exception class name |
| `📡 Collecte (cache) — cycle #42` | INFO | One line per `/collect` — the cycle marker |
| `❌ Échec persistance Database` | ERROR | Metrics lost after all retries |
| `🛑 Arrêt du sondage de fond` | INFO | Clean shutdown |

This is by far the **noisiest terminal of the stack**: N VMs × 1 line/second. The `📡 Collecte (cache)` line is the only cycle-rate marker in it, and the natural anchor when correlating with the other terminals.

The `fiabilité` value is the fastest read on infrastructure health: 1.0 means a perfect recent history, anything below 0.9 means recent failures even if the VM is answering right now.

## 10. Testing

There is **no** `tests/unit/test_collector.py` on `master`.

The two EMAs are pure functions of `(previous, observation)` and would be trivial to test — including the clamps and the "no update on failure" rule, which is the least obvious behaviour in the service and the easiest to break during a refactor.

## 11. Known limitations

| # | Limitation | Impact | Possible fix |
|---|---|---|---|
| **L-1** | Reliability and timeouts reset on restart (§7). | After a restart a dead VM shows `reliability = 1.0` for ~10 s. | Persist the EMA tables to Redis. |
| **L-2** | Database forwarding is sequential per VM, with a 19 s worst case each (§3.7). | With the database down, background tasks pile up across cycles. | `asyncio.gather` the forwards, and cap the total retry budget. |
| **L-3** | `collect_ms` is included in the persisted `metrics` dict (§3.7). | Survives only because `database` filters on `METRICS_REGISTRY`. | Add `collect_ms` to the exclusion list explicitly. |
| **L-4** | No validation of the values returned by the agents (SPEC C-5). | A buggy agent propagates absurd values into the ML models and decisions. | Clamp against `METRICS_REGISTRY[...]["bounds"]`, which already exists. |
| **L-5** | No cache freshness check in `handle()`. | If the background loop dies silently despite §3.2, `/collect` keeps serving frozen data with `200`. | Add a cache age check and mark entries as stale beyond N seconds. |
| **L-6** | A single `α` governs both EMAs (§5). | The two behaviours cannot be tuned independently. | Split into `TIMEOUT_ALPHA` and `RELIABILITY_ALPHA`. |
| **L-7** | No unit test (§10). | The clamps and the no-update-on-failure rule are unverified. | Two pure-function tests on the EMAs. |
| **L-8** | No authentication in either direction. | Any host can call `/collect`; a spoofed agent can inject metrics. | Acceptable on the demonstrator's private network. |
