# Latency Manager — Technical Documentation

> **Document type:** Technical documentation (*how the service is built and how it works*).
> For *what it must do*, see [`SPEC.md`](SPEC.md).

| Field | Value |
|---|---|
| Package | `services.latency_manager` |
| Entry point | `services/latency_manager/app.py` |
| Framework | FastAPI + Uvicorn |
| Port | `config.LATENCY_PORT` = `8001 + PORT_OFFSET` |
| Persistence | None |
| Lines of code | 187 (83 + 104) |

---

## 1. Role in the architecture

```text
 Raspberry Pi                      Orchestrator host
┌──────────────────┐
│ picar_bridge     │   POST /rtt (provider-1 subset)
│   :8080          │ ──────────────────────────────► ┌──────────────────┐
│                  │                                  │ latency_manager  │
│  pings 8 VM      │   POST /rtt (provider-2 subset)  │   :8001          │
│  agents, splits  │ ──────────────────────────┐      └────────┬─────────┘
│  by provider     │                           │               │ POST /rtt
└──────────────────┘                           │               ▼
                                               │      ┌──────────────────┐
                                               │      │  Hub Core :8000  │
                                               │      │  cycle_count++   │
                                               │      │  _run_flow(...)  │
                                               │      └──────────────────┘
                                               │
                                               └────► latency_manager :8101
                                                      → Hub Core :8100
```

The service is a **stateless proxy**. It sits on the northbound edge of the orchestrator and is the only component that talks to the measurement source. Every orchestration cycle begins with a POST that lands here.

Note the two independent chains: each provider stack has its own Latency Manager and its own Hub, and the bridge feeds both in parallel. Neither instance is aware of the other.

## 2. Folder structure

```text
services/latency_manager/
├── app.py              # HTTP layer: FastAPI app, routes, status-code mapping
├── latency_handler.py  # Business layer: validation, logging, retry forwarding
├── requirements.txt    # Empty — see §10, limitation L-4
├── SPEC.md
└── TECHNICAL_DOC.md
```

The two-file split is the deliberate pattern of the whole project: **`app.py` knows HTTP, `*_handler.py` knows the domain**. `LatencyHandler` contains no FastAPI import and could be reused behind a different transport (gRPC, message queue) with no modification.

## 3. Internal design

### 3.1 Startup sequence

`app.py` executes at import time, before Uvicorn serves any request:

1. `_setup_app_logger()` builds the `LatencyManager` logger — level `DEBUG`, a single `StreamHandler` with `PrettyFormatter`, and `propagate = False` so records never reach the root logger and get printed twice.
2. A boxed startup banner prints the effective configuration: Hub RTT URL, retry count, retry backoff. **This is the fastest way to confirm which provider stack a terminal belongs to** — provider-2 shows `:8100`.
3. The `FastAPI` app is instantiated (`title="Latency Manager"`, `version="1.1.0"`).
4. A single module-level `LatencyHandler` is created and reused for every request. It is safe to share because it is immutable after construction.
5. A readiness line prints the listening port.

The guard `if not logger.handlers` matters in practice: without it, Uvicorn's `reload` mode would stack a new handler on every reload and each log line would be duplicated.

### 3.2 Request flow — `POST /rtt`

```text
receive_rtt(payload)                        app.py:53
  └─► handler.handle(payload)               latency_handler.py:38
        ├─ 1. _validate(payload)            latency_handler.py:75
        │     └─ measurements present, is a list, non-empty?   → False if not
        ├─ 2. summary log  (cycle, count, reachable count)
        ├─ 3. per-VM DEBUG log
        └─ 4. _forward_with_retry(payload)  latency_handler.py:84
              └─► async_post_with_retry(...)  shared/http_utils.py:7
                    └─ up to POST_RETRY_COUNT attempts
  └─► map the boolean result to an HTTP status code
```

`handle()` is wrapped in a broad `try/except` that logs `type(e).__name__` and the message, then returns `False`. This satisfies FR-11: no exception ever escapes to Uvicorn, and the service stays up.

### 3.3 Boolean-to-status-code disambiguation — the one subtle point

`handle()` returns a single `bool`, which collapses two very different failures: *the client sent garbage* (client's fault, `400`) and *the Hub is unreachable* (our fault, `502`). `app.py` recovers the distinction by **re-running the validation check** on the failure path:

```python
if not success:
    measurements = payload.get("measurements")
    if not measurements or not isinstance(measurements, list) or len(measurements) == 0:
        response.status_code = 400          # invalid input
        ...
    response.status_code = 502              # forwarding failed
```

This duplicates the predicate of `_validate()`. It works, and it keeps `LatencyHandler` free of any HTTP concept — but the two copies must be kept in sync by hand. See limitation L-1.

A consequence worth knowing when debugging: an **unexpected internal exception** on a payload that *does* contain measurements is reported as `502`, even though nothing was wrong with the Hub. The log line (`❌ Erreur interne…`) is what disambiguates.

### 3.4 Retry policy — `shared/http_utils.py`

```python
for attempt in 1..retry_count:
    POST url, json=payload, timeout=timeout
    if status in (200, 201, 202): return True
    log warning
    if attempt < retry_count: await asyncio.sleep(backoff)
return False
```

Three properties of this policy:

- **Linear, not exponential.** The delay is constant (`backoff`), not doubled. With a 6 s cycle, exponential backoff would push a recovery past the next measurement anyway, so the extra complexity buys nothing.
- **No sleep after the last attempt** (`if attempt < retry_count`) — this is why the worst case is `3×timeout + 2×backoff = 19 s` and not 21 s.
- **A non-2xx response is retried like a network error.** A permanent `422` from the Hub therefore costs the full budget before failing. Acceptable here because the Hub's only rejection path is a Pydantic validation error, which this service's own validation makes very unlikely.

Each attempt opens its own `httpx.AsyncClient` (the `async with` is inside the helper, outside the loop) — so the connection pool is per-call, not per-service. Negligible at one call per 6 s.

## 4. API reference

### `POST /rtt`

Receives a batch of latency measurements and forwards it to the Hub.

**Request body** — `application/json`

| Field | Type | Required | Description |
|---|---|---|---|
| `timestamp` | string | no¹ | ISO 8601 UTC, set by the bridge |
| `source` | string | no¹ | Origin tag, `"picar_bridge"` in the demo |
| `cycle` | int | no¹ | Bridge send counter — informational only (SPEC C-5) |
| `measurements` | array | **yes** | Non-empty list; the only field validated here |
| `measurements[].vm_id` | string | no¹ | e.g. `"edge1"` |
| `measurements[].ip` | string | no¹ | VM agent address |
| `measurements[].rtt_ms` | float | no¹ | Distance-derived latency in ms |
| `measurements[].raw_ms` | float | no¹ | Unfiltered value; equal to `rtt_ms` in the demo |
| `measurements[].reachable` | bool | no¹ | Defaults to `True` when absent, in the logs |
| `measurements[].timestamp` | string | no¹ | Per-measurement ISO 8601 |

¹ *Not required **by this service**, but required by the Hub's `LatencyPayload` model. Omitting one produces a `422` at the Hub, which this service surfaces as a `502` after exhausting its retries.*

**Responses**

| Status | Body | Meaning |
|---|---|---|
| `202` | `{"status": "forwarded", "cycle": 42}` | Accepted and delivered to the Hub |
| `400` | `{"error": "Invalid payload: measurements is missing or empty"}` | Client-side error |
| `502` | `{"error": "Failed to forward measurements to Hub"}` | Hub unreachable, or internal error |

**Example**

```bash
curl -X POST http://localhost:8001/rtt -H "Content-Type: application/json" -d '{"timestamp":"2026-08-11T09:14:22Z","source":"manual","cycle":1,"measurements":[{"vm_id":"edge1","ip":"194.199.113.18","rtt_ms":23.7,"raw_ms":23.7,"reachable":true,"timestamp":"2026-08-11T09:14:22Z"}]}'
```

### `GET /health`

Liveness probe. Always `200` while the process is up — it does **not** check Hub reachability, so a healthy response says nothing about the downstream chain.

```json
{"status": "healthy", "service": "latency_manager"}
```

Used by the Hub's startup health check and by the launch scripts to gate the boot order.

## 5. Configuration

All values come from `shared/config.py` and are overridable by environment variable.

| Variable | Default | Effective value | Used for |
|---|---|---|---|
| `LATENCY_PORT` | `8001` | `8001 + PORT_OFFSET` | Listening port |
| `PORT_OFFSET` | `0` | `100` for provider-2 | Whole-stack port shift |
| `HUB_HOST` | `localhost` | — | Hub address |
| `HUB_PORT` | `8000` | `8000 + PORT_OFFSET` | Hub port |
| `HUB_RTT_URL` | derived | `http://{HUB_HOST}:{HUB_PORT}/rtt` | Forwarding target |
| `POST_RETRY_COUNT` | `3` | — | Maximum attempts |
| `POST_RETRY_BACKOFF` | `2.0` | seconds | Constant inter-attempt delay |
| `POST_TIMEOUT` | `5.0` | seconds | Per-attempt HTTP timeout |

`HUB_RTT_URL` is **derived**, not independent: overriding `HUB_PORT` is enough, and overriding both inconsistently is a common source of "why is provider-2 feeding provider-1's hub".

## 6. Dependencies

**Internal**

| Module | Used for |
|---|---|
| `shared.config` | All configuration constants |
| `shared.http_utils.async_post_with_retry` | The entire retry policy |
| `shared.logging_utils` (`C`, `PrettyFormatter`) | ANSI colours and log formatting |

**External** — `fastapi`, `uvicorn`, `httpx`. Declared in the root `requirements.txt`, not in the service's own (empty) one.

**Runtime** — requires the Hub to be reachable to do anything useful, but **starts fine without it** and must be started *before* the Hub in the launch order, since the Hub does not depend on it at boot.

## 7. Data model and storage

None. The service writes nothing to Redis, no Excel export, no file. Its only externally visible trace is stdout.

This is the reason it carries **no timing instrumentation** while most other services do: it has no step boundaries worth measuring, and its cost is dominated by the Hub's response time, which the Hub measures itself.

## 8. Running it standalone

```bash
python -m services.latency_manager.app
```

From the project root, with the virtualenv active. For the second provider:

```bash
PORT_OFFSET=100 python -m services.latency_manager.app
```

In normal operation both instances are started by `launch_provider.py` / `start_provider.ps1`. See `ETAPES_LANCEMENT_PROJET.md` for the full ordered runbook.

Smoke test with the Hub down: the `curl` in §4 should return `502` after roughly 19 seconds, with three warning lines and one `🔴 CRITICAL` line in the terminal.

## 9. Logging and observability

Two loggers, both at `DEBUG`, both non-propagating:

| Logger | Emitted from | Content |
|---|---|---|
| `LatencyManager` | `app.py` | Startup banner, readiness line |
| `LatencyHandler` | `latency_handler.py` | Per-batch summary, per-VM detail, retry warnings, outcome |

Reading the terminal:

| Marker | Level | Meaning |
|---|---|---|
| `📡 Mesures RTT reçues — cycle #N` | INFO | Batch accepted, with VM count and reachable count |
| `🔍 VM edge1  RTT : 23.7 ms  [OK]` | DEBUG | One line per VM |
| `⚠️ Payload invalide` | WARNING | FR-2 rejection → `400` |
| `POST … — tentative 2/3` | WARNING | One failed attempt, retry pending |
| `✅ Mesures transmises au Hub` | INFO | Nominal outcome |
| `🔴 Toutes les tentatives ont échoué` | CRITICAL | Batch lost — one cycle skipped |

The reachable count in the summary line is the fastest health signal of the whole demo: if it drops below the expected 4, a VM agent is down, and every downstream anomaly that cycle should be read in that light.

## 10. Known limitations

| # | Limitation | Impact | Possible fix |
|---|---|---|---|
| **L-1** | The validation predicate is duplicated between `_validate()` and `app.py`'s failure branch (§3.3). | Silent divergence if one is changed alone. | Return an enum or raise a typed exception from `handle()` instead of a `bool`. |
| **L-2** | An internal exception is reported as `502`, blaming the Hub for a local fault. | Misleading during debugging. | Same fix as L-1. |
| **L-3** | No unit test covers this service. `tests/unit/` contains no `test_latency_manager.py`. | The three status-code paths are only verified manually. | Three `TestClient` cases: valid, empty `measurements`, Hub mocked as failing. |
| **L-4** | `requirements.txt` is empty (0 bytes) while every sibling service declares its own. | The service is not independently installable. | Declare `fastapi`, `uvicorn`, `httpx`. |
| **L-5** | The retry budget (19 s) exceeds the cycle period (6 s) — SPEC C-4. | Overlapping in-flight requests when the Hub is down. | Cap the total budget below one cycle, e.g. 2 attempts at 2 s timeout. |
| **L-6** | Payload validation is shallow: a batch of measurements missing `vm_id` passes here and is rejected by the Hub. | A client-side error costs the full retry budget and is reported as `502`. | Validate against `LatencyPayload` here, which would move the `422` to a `400` returned immediately. |
| **L-7** | No authentication and no rate limiting on `/rtt`. | Any host on the network can inject arbitrary latency values and drive the orchestrator's decisions. | Acceptable on the demonstrator's private network; would be blocking in production. |
