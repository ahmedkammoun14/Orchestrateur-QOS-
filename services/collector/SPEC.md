# Collector Service — Specification

> **Document type:** Specification (*what the service must do*).
> For *how it does it*, see [`TECHNICAL_DOC.md`](TECHNICAL_DOC.md).

| Field | Value |
|---|---|
| Service name | `collector` |
| Default port | `8005` (`8105` for provider-2, via `PORT_OFFSET`) |
| Component version | 3.0.0 |
| Status | Implemented |
| Position in the pipeline | Southbound acquisition — step 3 of the cycle |

---

## 1. Context

Latency reaches the orchestrator by itself: the PiCar bridge pushes it to `latency_manager`. The other two metrics do not. **CPU and RAM must be pulled**, by querying an agent deployed on each of the eight VMs, over a real network to real OpenStack machines.

That pull has a cost the pushed path does not have. Measured on the demonstrator, a round-trip to the VM agents takes **1.4–1.8 s** — against a 6.0 s cycle of which roughly 4.7 s is orchestration work. Performing that pull inside the cycle would spend a third of the budget waiting on the network, and would make the whole cycle hostage to the slowest VM.

Version 3.0.0 resolves this by **decoupling acquisition from consumption**. A background loop polls the VMs continuously at its own rhythm and fills a cache; the cycle reads that cache. The network round-trip leaves the critical path entirely, and `/collect` becomes a memory read.

The service also owns two adaptive mechanisms that only make sense at the acquisition point, because only it observes each VM's individual behaviour:

- **Adaptive timeout per VM** — a VM that answers in 80 ms and one that answers in 900 ms should not share a fixed timeout. Each converges towards its own, by EMA.
- **Reliability score per VM** — an EMA of success/failure over time, giving a continuous availability indicator rather than a binary up/down.

## 2. Objectives

| # | Objective |
|---|---|
| O-1 | Acquire CPU/RAM metrics from every VM of the provider, without ever putting a network round-trip on the cycle's critical path. |
| O-2 | Serve the cycle a coherent snapshot of all VMs in constant time. |
| O-3 | Adapt the timeout of each VM to its own observed response time. |
| O-4 | Maintain a continuous reliability score per VM, resistant to isolated incidents. |
| O-5 | Persist every reachable VM's metrics, without delaying the response to the cycle. |
| O-6 | Remain agnostic to the set of metrics: whatever the agent returns is captured, and the cycle chooses what it wants at read time. |
| O-7 | Degrade progressively — an unreachable VM must not compromise the collection of the others. |

## 3. Functional requirements

### 3.1 Background polling

| # | Requirement | Verification |
|---|---|---|
| **FR-1** | The service SHALL perform one full poll of every VM **before** accepting traffic, during startup. | Startup log |
| **FR-2** | It SHALL then poll continuously in a background task, every `COLLECTOR_POLL_INTERVAL` = 1.0 s. | Terminal observation |
| **FR-3** | All VMs of one iteration SHALL be polled **in parallel** (`asyncio.gather`), never sequentially. | Code review |
| **FR-4** | An exception in one iteration SHALL be logged and SHALL NOT stop the loop. | Fault-injection test |
| **FR-5** | The loop SHALL be launched idempotently — a second launch call while running SHALL be a no-op. | Code review |
| **FR-6** | The loop SHALL be cancelled cleanly on service shutdown. | Shutdown log |
| **FR-7** | Each poll SHALL query `GET http://{ip}:{port}/metrics` on the VM agent. | Network capture |
| **FR-8** | The service SHALL capture **every field** the agent returns, except `vm_id` and `timestamp` — no fixed metric list at acquisition time. | Code review |

### 3.2 Adaptive timeout

| # | Requirement | Verification |
|---|---|---|
| **FR-9** | Each VM SHALL start with a timeout of `COLLECTOR_TIMEOUT_BASE` = 2.0 s. | Code review |
| **FR-10** | After each **successful** poll, the timeout SHALL be updated by EMA: `α × (observed × FACTOR) + (1−α) × previous`, with `α = 0.2` and `FACTOR = 1.5`. | Unit test |
| **FR-11** | The timeout SHALL be clamped to `[COLLECTOR_MIN_TIMEOUT, COLLECTOR_MAX_TIMEOUT]` = `[0.5, 5.0]` s. | Unit test |
| **FR-12** | A **failed** poll SHALL NOT update the timeout. | Code review |

### 3.3 Reliability score

| # | Requirement | Verification |
|---|---|---|
| **FR-13** | Each VM SHALL start at a reliability of `1.0`. | Code review |
| **FR-14** | After each poll the score SHALL be updated by EMA with the same `α`: success contributes `1.0`, failure `0.0`. | Unit test |
| **FR-15** | The score SHALL be reported to three decimal places alongside every metric result. | `curl` |
| **FR-16** | The score SHALL be attached to the persistence payload sent to the database. | Network capture |

### 3.4 Serving the cycle

| # | Requirement | Verification |
|---|---|---|
| **FR-17** | The service SHALL expose `POST /collect` accepting `{active_metrics, cycle}`. | `curl` |
| **FR-18** | It SHALL reject with `400` a payload where `active_metrics` is empty/absent or `cycle` is null. | Unit test |
| **FR-19** | `/collect` SHALL perform **no network call**. It reads the cache only. | Code review |
| **FR-20** | The response SHALL contain one entry per registered VM, filtered to the requested `active_metrics`. | `curl` |
| **FR-21** | A VM with no cache entry yet SHALL yield an entry with all metrics `null` and `reachable = false`, not an omission. | Cold-start test |
| **FR-22** | Each entry SHALL also carry `total_cores`, `total_ram_gb`, `reliability`, `reachable`, `collect_ms` and `timestamp`. | `curl` |
| **FR-23** | The response SHALL include `collect_timings` (per-VM round-trip in ms) and `collect_max_ms`, feeding the Hub's timing instrumentation. | Excel export |

### 3.5 Persistence

| # | Requirement | Verification |
|---|---|---|
| **FR-24** | After responding, the service SHALL forward each **reachable** VM's metrics to the database service, as a FastAPI background task. | Network capture |
| **FR-25** | An unreachable VM SHALL NOT be persisted — no null-metric rows in storage. | Redis inspection |
| **FR-26** | Forwarding SHALL retry `POST_RETRY_COUNT` times with backoff, and its failure SHALL NOT affect the response already sent. | Database-down test |
| **FR-27** | Persistence SHALL go **directly** to the database service, not through the Hub. | Architecture — validated exception |

### 3.6 Health

| # | Requirement | Verification |
|---|---|---|
| **FR-28** | `GET /health` SHALL probe every VM's `/health` in parallel with a 1 s timeout and report `online`/`error`/`offline` per VM. | `curl` |
| **FR-29** | The health check SHALL be independent of the cache — it performs live calls. | Code review |

## 4. Non-functional requirements

| # | Requirement | Target / rationale |
|---|---|---|
| **NFR-1 — Constant-time cycle path** | `/collect` SHALL return in a time independent of the VMs' network latency. | The 1.4–1.8 s round-trip is removed from the critical path. This is the single reason version 3.0.0 exists. |
| **NFR-2 — Data freshness** | Cached data SHALL be at most `COLLECTOR_POLL_INTERVAL` + one poll duration old. | At 1 s interval against a 6 s cycle, data is always fresher than one cycle. |
| **NFR-3 — Parallel acquisition** | One poll iteration SHALL cost the duration of the **slowest** VM, not their sum. | `asyncio.gather` |
| **NFR-4 — Fault isolation** | An unreachable VM SHALL affect only its own entry. | Per-VM exception handling |
| **NFR-5 — Resilient loop** | The background loop SHALL survive any exception, indefinitely. | Broad `try/except` inside the `while True` |
| **NFR-6 — Non-blocking persistence** | Database write latency SHALL NOT be visible to the Hub. | `BackgroundTasks`, scheduled after the response |
| **NFR-7 — Extensibility** | Adding a metric SHALL require no change in this service. | Acquisition captures everything; filtering happens at read time against the cycle's `active_metrics`. |
| **NFR-8 — Provider isolation** | Each instance SHALL poll only its own provider's VMs. | `VM_REGISTRY` is derived from `PROVIDER_REGISTRY` and `PROVIDER_ID` |

## 5. Interface contract

### 5.1 Consumed — VM agents (deployed, not versioned)

`GET http://{ip}:{port}/metrics` on each VM of `VM_REGISTRY` (ports 8200–8202).

```jsonc
{ "vm_id": "edge1", "cpu_usage": 41.2, "ram_usage": 63.0,
  "total_cores": 2, "total_ram_gb": 4.0,
  "timestamp": "2026-08-11T09:14:22.031Z" }
```

`GET .../health` is used only by this service's own `/health`.

### 5.2 Consumed — inbound `POST /collect`

Caller: the Hub, once per cycle at step 3.

```jsonc
{ "active_metrics": ["cpu_usage", "ram_usage"], "cycle": 42 }
```

The Hub always sends `METRICS_REGISTRY` **minus latency** — latency arrives through `latency_manager` and is never polled here.

### 5.3 Produced — response

```jsonc
{
  "results": [
    { "vm_id": "edge1", "cpu_usage": 41.2, "ram_usage": 63.0,
      "total_cores": 2, "total_ram_gb": 4.0,
      "reliability": 0.98, "reachable": true,
      "collect_ms": 87.3, "timestamp": "2026-08-11T09:14:22.031Z" }
  ],
  "cycle": 42,
  "collect_timings": { "edge1": 87.3, "edge1b": 91.0, "edge1c": null, "cloud1": 142.7 },
  "collect_max_ms": 142.7,
  "timestamp": "2026-08-11T09:14:22.033Z"
}
```

### 5.4 Produced — `POST {DATABASE_SERVICE_URL}/store/metrics`

One call per reachable VM, in a background task.

### 5.5 Responses

| Status | Condition |
|---|---|
| `200` | Snapshot served |
| `400` | `active_metrics` empty/absent or `cycle` null |

## 6. Constraints

| # | Constraint |
|---|---|
| **C-1** | **The data is never live.** `/collect` returns what the last background poll captured, up to ~1 s old. Acceptable because CPU and RAM vary slowly relative to the cycle, but it means `collect_ms` in the response is the round-trip of the *previous* poll, not of this call. |
| **C-2** | **Direct write to the database — validated architectural exception.** The collector bypasses the Hub to persist. Documented in the README: routing this volume through the Hub would saturate it. |
| **C-3** | **Failure does not update the timeout.** Only successes move it. A VM that fails permanently keeps the timeout it had when it last succeeded — the mechanism converges on healthy behaviour, and does not drift upward on a dead VM. |
| **C-4** | **Reliability is not persisted.** It lives in memory and resets to `1.0` on every restart. History exists only in the database rows already written. |
| **C-5** | **No metric validation.** Whatever the agent returns is cached and forwarded. A buggy agent reporting `cpu_usage = 500` propagates unchecked into the ML models and the decisions. |
| **C-6** | **Two independent rhythms.** The poll interval (1 s) and the cycle period (6 s) are unrelated. Roughly six polls occur per cycle, of which only the last is read — the others exist purely to keep the EMAs converging. |
| **C-7** | **No authentication**, in either direction: the service trusts the agents, and any host may call `/collect`. |
| **C-8** | Requires the eight VM agents deployed and reachable. Those agents are **not versioned in this repository**. |

## 7. Out of scope

- Measuring latency — `latency_manager`, pushed from the PiCar.
- Storing metrics — `database`. This service only forwards.
- Reading history — `history_loader`.
- Deciding which metrics matter — `metrics_manager` (MI); the collector receives `active_metrics` and obeys.
- Detecting SLO violations — `decision_intelligence`.
- Deploying or supervising the VM agents.

## 8. Traceability against the xQoS internship objectives

| xQoS objective | Contribution of this service |
|---|---|
| O1 — Multi-provider orchestrator | Supplies the resource observation of each provider's own VMs; the `VM_REGISTRY` derivation from `PROVIDER_REGISTRY` is what keeps the two acquisition domains disjoint. |
| O2 — Intent–QoS relationship engine | None. It measures, it does not interpret. |
| O3 — Visualization & explainability | Provides `reliability` and `reachable`, the two indicators the dashboard's VM cards display, and the only continuous availability signal in the system. |
| O4 — Experimental validation | `collect_timings` / `collect_max_ms` are the direct measurement of acquisition cost, and the evidence backing the "1.4–1.8 s removed from the critical path" claim of version 3.0.0. |
