# Database Service — Technical Documentation

> **Document type:** Technical documentation (*how the service is built and how it works*).
> For *what it must do*, see [`SPEC.md`](SPEC.md).

| Field | Value |
|---|---|
| Package | `services.database` |
| Entry point | `services/database/app.py` |
| Framework | FastAPI + Uvicorn |
| Port | `config.DATABASE_PORT` = `8006 + PORT_OFFSET` |
| Backends | Redis (hot) + Excel `.xlsx` (cold) |
| Lines of code | 438 (186 + 252), plus `shared/excel_writer.py` |

---

## 1. Role in the architecture

```text
   collector ──POST /store/metrics──┐
   Hub       ──POST /store/slos─────┤
   Hub       ──POST /store/decision─┤        ┌─────────────────────┐
   intent_mgr──POST /store/llm_hist─┼───────►│  database  :8006    │
   intent_mgr──GET  /load/llm_hist ─┘        │                     │
                                             │  RedisClient  ──────┼──► Redis DB 0/1
                                             │   (sync, pipeline)  │    5 key families
                                             │                     │
                                             │  ExcelWriter  ──────┼──► data/qos_history.xlsx
                                             │   (async → thread)  │    4 sheets
                                             └─────────────────────┘
                                                       ▲
   history_loader ───── reads Redis DIRECTLY ──────────┘
                        (bypasses this service)
```

Two backends, one API. The caller does not know either of them exists.

Note the dashed path at the bottom: **`history_loader` reads Redis without going through this service.** That is deliberate. The metric time-series are read on every cycle, for every VM, for every metric; an HTTP round-trip per read would add latency to the hottest path for no benefit, since reading needs none of the atomicity guarantees that writing does. The invariant the project actually enforces is *single **writer***, and it holds.

## 2. Folder structure

```text
services/database/
├── app.py             # HTTP layer: 6 routes, validation, task scheduling
├── redis_client.py    # Redis backend: pipelines, key formatting, health
├── requirements.txt
├── SPEC.md
└── TECHNICAL_DOC.md

shared/
├── redis_keys.py      # The 5 key templates — single source of truth
└── excel_writer.py    # Excel backend, shared with the timing instrumentation
```

`ExcelWriter` lives in `shared/` rather than here because the timing instrumentation uses the same class for its own workbooks. `RedisClient` is local because nothing else may write.

## 3. Internal design

### 3.1 Startup and fail-fast

```python
app          = FastAPI(...)
redis_client = RedisClient(logger)          # ← raises if Redis is down
excel        = ExcelWriter(config.EXCEL_PATH, config.EXCEL_MAX_MB * 1024 * 1024)
```

`RedisClient.__init__` opens the connection and issues a `PING`. On failure it logs `CRITICAL` and **re-raises**, which happens at module import — so Uvicorn never binds the port and the launch script sees the service fail immediately.

This is the opposite choice from `latency_manager`, which starts happily without its downstream. The reasoning is that a latency proxy without a Hub is a *temporarily useless* service, whereas a database service without a database is an *actively harmful* one: it would return `500` on every write while the collector kept feeding it, and the operator would only discover the data loss later.

The logger is **injected** into `RedisClient` rather than acquired by module name. This is the only service that does so, and it is why `RedisClient` needs no logging setup of its own — a small design point worth preserving if the class is reused.

### 3.2 The two-backend write pattern

Every `/store/*` route follows the same four steps:

```python
1. validate            → 400 on failure
2. redis_client.store_xxx(...)          # SYNCHRONOUS, blocking, awaited implicitly
3. asyncio.create_task(excel.write_xxx(...))   # FIRE-AND-FORGET
4. return {"status": "..."}
```

Step 2 is a **synchronous** call inside an `async def`. The `redis-py` client used here is the blocking one, so it occupies the event loop for the duration of the pipeline. At sub-millisecond latencies and one write per VM per 6 s cycle this is invisible, but it is a real property of the code: a Redis stall would block the whole worker, bounded only by `socket_timeout=2.0`.

Step 3 is the important one. `asyncio.create_task` schedules the Excel write and **immediately moves on** — it is never awaited. Consequences:

- The HTTP response returns at Redis speed, not Excel speed.
- An Excel failure is invisible to the caller; it surfaces only as a `⚠️ ExcelWriter` warning in the terminal.
- The task holds no strong reference outside the loop's own scheduling, so on shutdown pending Excel writes can be dropped silently.

Redis is authoritative. The workbook is an export that is *usually* complete.

### 3.3 `store_metrics` — the registry-driven write

This is the most-called method of the service. One call produces **2n + 2 Redis commands** for n metrics, all in one pipeline:

```text
for each metric in METRICS_REGISTRY:          ← iterates the REGISTRY, not the payload
    if metrics.get(metric) is not None:
        LPUSH  metrics:{vm_id}:{metric}  <value>
        LTRIM  metrics:{vm_id}:{metric}  0  49

LPUSH  metrics:{vm_id}:history  <full JSON snapshot>
LTRIM  metrics:{vm_id}:history  0  49

pipe.execute()                                 ← single round-trip, atomic
```

Two design points:

**The loop iterates `METRICS_REGISTRY`, not the incoming payload.** A metric the collector sends but that the registry does not declare is silently absent from the time-series. It still appears in the JSON snapshot, so nothing is lost — but no ML model will ever see it. This is the mechanism behind the README's claim that a new metric is added "via a single dictionary in `shared/config.py`": declare it in the registry and this loop starts persisting it, with no change here.

**The `is not None` test, not a truthiness test.** A CPU usage of `0.0` is a legitimate measurement; `if val:` would discard it. This is easy to break when refactoring.

The docstring in `redis_client.py:83-85` still advertises an `EXPIRE` command, but the implementation replaced it with a code comment stating the opposite (*"Pas de EXPIRE : les données sont conservées indéfiniment"*). Trust the code, not the docstring — see L-1.

### 3.4 The other three writes

| Method | Redis structure | Semantics |
|---|---|---|
| `store_slos` | `SET slos:active <JSON>` | **Overwrite.** No history, no pipeline (single command, already atomic). |
| `store_decision` | `LPUSH` + `LTRIM` on `decisions:recent`, cap 50 | Appends the **whole payload**, whatever fields it carries. The service imposes only that `decision`, `from_vm`, `to_vm` be present; everything else the Hub adds (cycle, reason, TOPSIS score, MI scores) rides along untouched. That is what makes the audit trail rich without coupling this service to the Hub's schema. |
| `store_llm_history` | `LPUSH` + `LTRIM` on `llm:history`, cap 100 | Same pattern. Note it uses `HISTORY_SIZE` (100), while metrics use `HISTORY_WINDOW` (50) — two different constants, easy to confuse. |

### 3.5 `load_llm_history` — the only read, and the only reversal

```python
raw_list = self._r.lrange(LLM_HISTORY_KEY, 0, size - 1)
return [json.loads(r) for r in reversed(raw_list)]
```

`LPUSH` stores newest-first, so `LRANGE 0 N-1` returns the N *most recent* entries in *reverse chronological* order. The `reversed()` restores chronological order (oldest first), which is what a conversational history must be.

Note the subtlety: with `size=10` and 100 stored entries, you get **the 10 most recent, in chronological order** — not the 10 oldest. That is the intended behaviour, but the reversal makes it easy to misread.

Unlike every write method, this one **swallows its exception and returns `[]`**. A caller that cannot read history should start with an empty one, not fail. The asymmetry is deliberate: losing a write is data loss, losing a read is a degraded but functional startup.

### 3.6 The Excel backend — `shared/excel_writer.py`

| Sheet | Columns |
|---|---|
| `Métriques` | timestamp, vm_id, latency, cpu_usage, ram_usage, reliability |
| `Décisions` | timestamp, decision, from_vm, to_vm, reason, cycle |
| `SLOs` | timestamp, metric, threshold, operator, weight, is_primary |
| `Intentions_LLM` | timestamp, intention, nb_slos |

Three mechanisms matter:

- **Thread offloading.** `openpyxl` is fully synchronous and rewrites the entire workbook on every save. The writes are pushed to a thread so the event loop is not blocked, and guarded by a `threading.Lock` so two concurrent writes cannot corrupt the file.
- **Corruption recovery.** `_load_workbook_safe` catches any load failure, **recreates the workbook from scratch**, and continues. This keeps the service alive after a crash mid-save — at the cost of silently destroying all prior rows. The `⚠️ fichier corrompu détecté, recréation` warning is the only trace.
- **Rotation.** When the file exceeds `EXCEL_MAX_MB`, `_maybe_trim` deletes the oldest 20 % of rows from **every** sheet. Sheets are trimmed proportionally, not by age across the workbook, so a high-volume sheet and a low-volume one lose the same *fraction*, not the same *time span*.

Rewriting the whole workbook on every append means cost grows with file size. At demo scale (a few thousand rows) this is fine; over a multi-hour run each metric write becomes progressively more expensive. Since the write is fire-and-forget, the symptom is not slow responses but a growing backlog of scheduled tasks.

## 4. API reference

### `POST /store/metrics`

| Field | Type | Required |
|---|---|---|
| `vm_id` | string | **yes** |
| `metrics` | object | **yes** — `{"latency": 23.7, "cpu_usage": 41.2, "ram_usage": 63.0}` |
| `timestamp` | string | no — defaults to now (UTC) |
| `reliability` | float | no — EMA computed by the collector |

`200 {"status": "metrics_stored"}` · `400` invalid payload · `500` Redis write failure

### `POST /store/slos`

| Field | Type | Required |
|---|---|---|
| `slos` | array | **yes** — may be empty, which clears the contract |
| `timestamp` | string | no |

`200 {"status": "slos_stored"}` · `400` `slos` absent · `500`

### `POST /store/decision`

| Field | Type | Required |
|---|---|---|
| `decision` | string | **yes** — `MIGRATE`, `STAY`, … |
| `from_vm` / `to_vm` | string | **yes** |
| *any other field* | any | no — stored as-is |

`200 {"status": "decision_stored"}` · `400` · `500`

### `POST /store/llm_history`

| Field | Type | Required |
|---|---|---|
| `intention` | string | **yes**, non-empty |
| `slos` | array | no |

`200 {"status": "llm_history_stored"}` · `400` · `500`

### `GET /load/llm_history?size=N`

`size` defaults to **10**. Returns `{"history": [...], "count": N}`, oldest first.

> `intent_manager` calls this with `size=HISTORY_SIZE` (100). Calling it without `size` silently gives you 10 — a real trap when testing by hand.

### `GET /health`

```json
{"status": "healthy", "redis": "connected"}
```

`redis` is `"connected"` or `"disconnected"`, based on a live `PING`. Unlike `intent_manager`'s health check, this one is honest: it probes the dependency that actually matters.

**Examples**

```bash
curl -X POST http://localhost:8006/store/metrics -H "Content-Type: application/json" -d '{"vm_id":"edge1","metrics":{"latency":23.7,"cpu_usage":41.2,"ram_usage":63.0},"reliability":0.98}'
```

```bash
curl "http://localhost:8006/load/llm_history?size=100"
```

## 5. Configuration

| Variable | Default | Used for |
|---|---|---|
| `DATABASE_PORT` | `8006` (+`PORT_OFFSET`) | Listening port |
| `REDIS_HOST` | `127.0.0.1` | Redis address |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_DB` | `0` | **Logical DB — the only provider isolation** (`1` for provider-2) |
| `HISTORY_WINDOW` | `50` | Cap on metric lists — **not** an env var, hardcoded |
| `DECISIONS_FIFO` | `50` | Cap on the decision FIFO — hardcoded |
| `HISTORY_SIZE` | `100` | Cap on the LLM history |
| `EXCEL_PATH` | `data/qos_history{suffix}.xlsx` | Workbook path |
| `EXCEL_MAX_MB` | `200` | Rotation threshold |

`HISTORY_WINDOW` and `DECISIONS_FIFO` are plain module constants, not `os.getenv` lookups — changing them requires editing `shared/config.py`. `HISTORY_WINDOW = 50` is not arbitrary: it is the window the ML cascade's level 1 expects, so lowering it would silently push predictions to level 3.

## 6. Dependencies

**Internal** — `shared.config`, `shared.redis_keys`, `shared.excel_writer`, `shared.logging_utils`.

**External** — `fastapi`, `uvicorn`, `redis` (synchronous client), `openpyxl`.

**Runtime**

| Dependency | Nature | On failure |
|---|---|---|
| Redis | **hard at startup**, soft afterwards | Refuses to boot / `500` per write |
| `data/` directory | soft | Created automatically by `ExcelWriter` |

## 7. Data model

See SPEC §5.2 for the key table. Two properties worth internalising:

- **Newest-first everywhere.** Every list uses `LPUSH`, so index 0 is the most recent entry. Any consumer reading with `LRANGE` must reverse if it wants chronological order.
- **No TTL anywhere.** Retention is by count, not by time. `redis-cli TTL <key>` returns `-1` on every key of this project, and that is correct, not a misconfiguration.

Inspecting the state by hand:

```bash
redis-cli -n 0 KEYS 'metrics:*'
redis-cli -n 0 LRANGE metrics:edge1:latency 0 9
redis-cli -n 0 GET slos:active
redis-cli -n 1 LLEN decisions:recent
```

`-n 0` is provider-1, `-n 1` is provider-2. Forgetting the flag is the most common way to read the wrong provider's data.

## 8. Running it standalone

```bash
python -m services.database.app
```

Redis must be up first:

```bash
redis-server redis.conf
```

Second provider:

```bash
REDIS_DB=1 PORT_OFFSET=100 python -m services.database.app
```

Order matters in the launch runbook: Redis → database → collector, since the collector writes on its first cycle.

## 9. Logging and observability

| Marker | Level | Meaning |
|---|---|---|
| `🚀 Database Service — Démarrage` | INFO | Banner with port and Redis address |
| `Redis connected at host:port` | INFO | `PING` succeeded — the service will boot |
| `Redis initialization failed` | CRITICAL | Startup abort |
| `📥 Réception métriques pour edge1` | INFO | One line per metric write — the highest-volume log of the whole stack |
| `✅ Métriques persistées avec succès` | SUCCESS | Custom level defined in `shared/logging_utils` |
| `❌ Erreur interne /store/…` | ERROR | Redis write failure → `500` |
| `⚠️ ExcelWriter — échec écriture` | WARNING | Excel failed; **Redis succeeded and the caller saw `200`** |
| `⚠️ fichier corrompu détecté, recréation` | WARNING | The workbook was just wiped and recreated |
| `✂️ ExcelWriter — rotation effectuée` | INFO | 20 % of the oldest rows deleted |

Note `logger.log(logging.SUCCESS, …)`: a custom level added by `shared/logging_utils`. Importing `RedisClient` outside this app will not have it registered.

At one line per VM per cycle, `📥 Réception métriques` dominates this terminal. It is also the quickest liveness check of the collector: if the lines stop, the collector died, not the database.

## 10. Testing

`tests/unit/test_redis_client.py` covers the client. There is no test for `app.py`'s routes or for `ExcelWriter`.

```bash
pytest tests/unit/test_redis_client.py -v
```

## 11. Known limitations

| # | Limitation | Impact | Possible fix |
|---|---|---|---|
| **L-1** | `store_metrics`' docstring documents an `EXPIRE` the code no longer issues (§3.3). | A reader concludes keys expire; they never do. | Update the docstring to describe the `LTRIM`-only policy. |
| **L-2** | Excel writes are fire-and-forget and unreferenced. | A silent Excel failure yields `200`; pending tasks are dropped on shutdown. | Keep task references and log a summary on shutdown. |
| **L-3** | `_load_workbook_safe` recreates a corrupted workbook, **destroying all history**. | An entire measurement session can vanish with one warning line. | Rename the corrupted file to `.bak` before recreating. |
| **L-4** | `openpyxl` rewrites the whole file per append. | Cost grows with size; a long run accumulates a backlog of scheduled writes. | Batch writes, or switch the cold path to CSV append. |
| **L-5** | Provider isolation relies only on `REDIS_DB` (SPEC C-5). | Launching provider-2 without `REDIS_DB=1` silently merges datasets. | Prefix keys with `PROVIDER_ID`, making the mistake impossible. |
| **L-6** | The synchronous `redis-py` client blocks the event loop during a write. | A Redis stall blocks the worker for up to `socket_timeout` = 2 s. | Use `redis.asyncio`. |
| **L-7** | `slos:active` keeps no history in Redis (SPEC C-2). | Contract evolution lives only in the workbook. | Add a capped `slos:history` list. |
| **L-8** | No test for the routes or for `ExcelWriter`. | Validation branches and rotation are unverified. | `TestClient` cases per route; a rotation test on a small `max_bytes`. |
| **L-9** | No authentication on the write routes. | Any host can inject metrics that drive ML predictions and decisions. | Acceptable on the demonstrator's private network. |
