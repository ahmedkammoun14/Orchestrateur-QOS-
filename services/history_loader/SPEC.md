# History Loader — Specification

> **Document type:** Specification (*what the service must do*).
> For *how it does it*, see [`TECHNICAL_DOC.md`](TECHNICAL_DOC.md).

| Field | Value |
|---|---|
| Service name | `history_loader` |
| Default port | `8007` (`8107` for provider-2, via `PORT_OFFSET`) |
| Component version | 1.0.0 |
| Status | Implemented |
| Position in the pipeline | Read path — step 6 of the cycle, immediately before ML prediction |

---

## 1. Context

Two components of the decision cycle need not a *current value* but a *time-series*:

- **`ml_predictor`** — its level-1 prediction (`/predict_sequence`) consumes a full window of past points. Below the model's `window_size` it silently falls back to a lower level, which is exactly what makes the warm-up period observable in the demonstrator.
- **`metrics_manager`** — Mutual Information estimation and adaptive percentile thresholds are statistical operations over a sample, meaningless on a single point.

Those series are stored in Redis by the `database` service, one list per `(vm_id, metric)` pair, capped at 50 entries. But they are stored in the **storage layer's** form: newest-first (`LPUSH` order), as raw strings, with no envelope. Neither consumer wants that shape — both need oldest-first, typed floats, with a documented structure and an explicit answer when the history simply does not exist yet.

`history_loader` is the **read counterpart** of the `database` service. It owns exactly one question: *"give me the last N points of these metrics for this VM, in the form the rest of the pipeline expects."*

Its cost profile explains its existence as a separate service. At step 6 of each cycle the Hub queries it **once per VM in parallel** (`asyncio.gather`), for every metric of the registry — 4 VMs × 3 metrics = 12 Redis lists per cycle, every 6 seconds, plus one extra call at the SLO step. Concentrating that traffic in a dedicated read-only process keeps it off the writer's path and makes the read pattern measurable on its own.

> **Architectural exception, stated plainly.** This service opens its **own** Redis connection instead of going through `database`. The project's rule is *single **writer***, not single accessor. Reading needs none of the atomicity guarantees writing does, and inserting an HTTP hop between two Redis reads on the hottest path of the cycle would add latency for nothing. This is a validated exception, symmetric to the `collector → database` direct-write exception documented in the README.

## 2. Objectives

| # | Objective |
|---|---|
| O-1 | Serve per-VM metric time-series to the Hub, in a single call per VM covering all requested metrics. |
| O-2 | Convert Redis's storage form (newest-first raw strings) into the pipeline's consumption form (oldest-first typed floats). |
| O-3 | Return an explicit, well-formed *empty* answer when no history exists, never an error. |
| O-4 | Guarantee read-only access — no code path of this service may write to Redis. |
| O-5 | Tolerate a corrupted individual entry without losing the surrounding series. |
| O-6 | Keep the read path independent of the write path, so a `database` outage does not blind the cycle. |

## 3. Functional requirements

| # | Requirement | Verification |
|---|---|---|
| **FR-1** | The service SHALL expose `POST /load` accepting `{vm_id, metrics, size?}`. | `curl` |
| **FR-2** | It SHALL reject with `400` a payload where `vm_id` is absent, or `metrics` is not a list, or `metrics` is empty. | Unit test |
| **FR-3** | `size` SHALL default to `HISTORY_WINDOW` = 50 when absent, null, or zero. | Code review |
| **FR-4** | Only metric names present in **both** the request and `METRICS_REGISTRY` SHALL be processed. Unknown names SHALL be silently ignored — not rejected. | Unit test |
| **FR-5** | For each valid metric, the service SHALL read at most `size` entries from `metrics:{vm_id}:{metric}`. | `redis-cli` comparison |
| **FR-6** | Entries SHALL be returned in **chronological order (oldest first)**, reversing Redis's newest-first storage. | Unit test |
| **FR-7** | Each entry SHALL be converted to a `float`; an entry that cannot be converted SHALL be dropped with a warning, without affecting the others. | Unit test |
| **FR-8** | A metric whose key is absent or whose list is empty SHALL yield `histories[metric] = []` and `sizes[metric] = 0`. It SHALL NOT produce a `404`. | Unit test |
| **FR-9** | A Redis failure on one metric SHALL yield an empty series **for that metric only**; the other metrics of the same request SHALL still be served. | Fault-injection test |
| **FR-10** | The response SHALL contain `vm_id`, `histories`, `sizes` and a read `timestamp`. | `curl` |
| **FR-11** | `sizes[metric]` SHALL report the number of entries **actually returned** after parsing — not the number read from Redis. | Unit test |
| **FR-12** | The service SHALL expose `GET /health` reporting service liveness and real Redis reachability. | `curl` |
| **FR-13** | The service SHALL refuse to start if Redis does not answer `PING` at construction. | Redis-down startup test |
| **FR-14** | The service SHALL NOT issue any Redis write command, in any code path. | Code review |

## 4. Non-functional requirements

| # | Requirement | Target / rationale |
|---|---|---|
| **NFR-1 — Read latency** | A `/load` call SHALL complete in a time compatible with 5 sequential-then-parallel calls per 6 s cycle. | One `LRANGE` per metric, 50 entries each: sub-millisecond in Redis, dominated by HTTP overhead. |
| **NFR-2 — Fail-fast startup** | Redis SHALL be verified by `PING` at construction, and the service SHALL abort if it fails. | Same rationale as `database`: a history service that answers "no history" because Redis is down is worse than one that is visibly absent — the Hub would read the empty answer as a legitimate warm-up state. |
| **NFR-3 — Per-metric fault isolation** | A failure affecting one metric SHALL NOT propagate to the others. | Guarantees the cycle degrades progressively rather than all at once. |
| **NFR-4 — Read-only by construction** | The class SHALL expose no write method at all, so read-only is a structural property, not a convention. | `HistoryReader` has exactly `load`, `_parse_entry`, `health_check`. |
| **NFR-5 — Socket timeout** | The Redis client SHALL use a 2 s socket timeout. | A hung Redis must not hold the worker indefinitely. |
| **NFR-6 — Registry-driven** | The set of admissible metrics SHALL derive from `METRICS_REGISTRY`; no metric name may be hardcoded. | Adding a metric to the registry must be sufficient. |
| **NFR-7 — Provider isolation** | Two provider stacks sharing one Redis instance SHALL never read each other's series. | `REDIS_DB` = 0 / 1, as for `database`. |
| **NFR-8 — Statelessness** | The service SHALL hold no cache and no state between calls. | Every response reflects Redis at the instant of the read; a stale cache would defeat the whole point of a 6 s cycle. |

## 5. Interface contract

### 5.1 Consumed — inbound `POST /load`

Caller: the Hub, at two distinct points of the cycle.

| Call site | Scope | Purpose |
|---|---|---|
| SLO step (`_step…slos_load_hist`) | one call, `vm_id = state.service_vm` | Feeds MI scoring and adaptive thresholds in `metrics_manager` |
| Step 6 (`_step6_load_histories`) | **N parallel calls**, one per candidate VM | Feeds the ML prediction cascade at step 7 |

```jsonc
{
  "vm_id":   "edge1",
  "metrics": ["latency", "cpu_usage", "ram_usage"],
  "size":    50
}
```

The Hub always sends `list(METRICS_REGISTRY.keys())` and `HISTORY_WINDOW` — it never requests a subset. FR-4's filtering therefore never fires in production; it is a guard for direct callers.

> **Standby short-circuit.** When multi-provider mode is on and this provider is `STANDBY`, the Hub **skips** the SLO-step call entirely. A standby's `service_vm` points at a VM of the *other* provider, whose series live in the other provider's Redis DB — the read would return zero points and MI would be computed on nothing. Skipping also saves two HTTP calls per cycle.

### 5.2 Consumed — Redis (direct, read-only)

| Key | Command | Notes |
|---|---|---|
| `metrics:{vm_id}:{metric}` | `LRANGE key 0 size-1` | Written by `database.store_metrics`; capped at 50 by `LTRIM` |
| — | `PING` | Startup check and `/health` |

`metrics:{vm_id}:history` (the full JSON snapshots) is **not** read by this service.

### 5.3 Produced — response

```jsonc
{
  "vm_id": "edge1",
  "histories": {
    "latency":   [ {"value": 21.4, "timestamp": "2026-08-11T09:14:22.031Z"},
                   {"value": 23.7, "timestamp": "2026-08-11T09:14:22.031Z"} ],
    "cpu_usage": [],
    "ram_usage": [ {"value": 63.0, "timestamp": "2026-08-11T09:14:22.031Z"} ]
  },
  "sizes": { "latency": 2, "cpu_usage": 0, "ram_usage": 1 },
  "timestamp": "2026-08-11T09:14:22.033Z"
}
```

`cpu_usage: []` with `sizes.cpu_usage = 0` is a **valid, expected** answer during warm-up or when the VM agent does not report that metric.

### 5.4 Responses

| Status | Condition | Body |
|---|---|---|
| `200` | Read performed — possibly with empty series | see above |
| `400` | `vm_id` absent, or `metrics` not a non-empty list | `{"detail": "vm_id (str) and metrics (non-empty list) are mandatory"}` |
| `500` | Unexpected internal error | `{"detail": "Internal history reader error"}` |

## 6. Constraints

| # | Constraint |
|---|---|
| **C-1** | **The per-entry timestamp is not real.** Individual metric keys store bare floats — Redis holds no timestamp at that granularity. The `timestamp` field of every entry is the **time of the read**, identical across all entries and all metrics of one response. It exists for structural uniformity, not for temporal information. Any consumer needing genuine per-point timing must read `metrics:{vm_id}:history` instead. |
| **C-2** | **Points are assumed regularly spaced.** Since C-1 removes real timing, the ML models treat the series as a uniform sequence at the cycle period. A collection gap (VM unreachable for several cycles) is invisible: the series simply has fewer points, with no hole marked. |
| **C-3** | **Read-only, hard.** No write path exists. Populating history is exclusively `database`'s job. |
| **C-4** | **No cross-provider read.** The `REDIS_DB` binding is fixed at startup, so a provider can only see its own series. This is what makes the standby short-circuit necessary rather than optional. |
| **C-5** | **`size` is not validated upward.** A caller may request more than the 50 entries `LTRIM` keeps; Redis simply returns what exists. No error, no warning. |
| **C-6** | **No authentication.** Any host on the network can read the full metric history of every VM. |
| **C-7** | Requires a reachable Redis. Depends on `database` having written first — but only through Redis, never directly. |

## 7. Out of scope

- Writing, trimming or expiring history — `database`.
- Computing statistics over the series (mean, percentile, MI) — `metrics_manager`.
- Predicting future values — `ml_predictor`.
- Reading the JSON snapshot list, the active SLOs, the decision FIFO or the LLM history — respectively out of scope, and `database` for the last one.
- Caching, aggregating or resampling — the service returns raw points.
- Detecting collection gaps — see C-2.

## 8. Traceability against the xQoS internship objectives

| xQoS objective | Contribution of this service |
|---|---|
| O1 — Multi-provider orchestrator | Supplies the observation window that makes prediction and MI possible. Its per-provider isolation is what allows two independent stacks to reason on disjoint histories. |
| O2 — Intent–QoS relationship engine | None. It transports values, never interpretations. |
| O3 — Visualization & explainability | Indirect: `sizes` is the observable that explains *why* the ML cascade falls to level 3 during warm-up — a series shorter than the model's `window_size`. |
| O4 — Experimental validation | Its `sizes` values are the direct measurement of history availability per VM, and therefore of the warm-up duration reported in the README's cascade statistics (77.5 % level 1 after warm-up). |
