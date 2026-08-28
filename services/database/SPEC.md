# Database Service — Specification

> **Document type:** Specification (*what the service must do*).
> For *how it does it*, see [`TECHNICAL_DOC.md`](TECHNICAL_DOC.md).

| Field | Value |
|---|---|
| Service name | `database` |
| Default port | `8006` (`8106` for provider-2, via `PORT_OFFSET`) |
| Component version | 2.2.0 |
| Status | Implemented |
| Position in the pipeline | Southbound persistence — terminal sink of the cycle |

---

## 1. Context

The orchestration cycle produces three kinds of durable facts: **what was measured** (per-VM metrics), **what was required** (the active SLO contract), and **what was decided** (migrations and their justification). A fourth, the **intent history**, records what the user asked for over time.

These facts serve three distinct consumers with incompatible requirements:

| Consumer | Needs | Access pattern |
|---|---|---|
| `ml_predictor`, `metrics_manager`, `history_loader` | The last 50 points of a metric, in milliseconds | Hot, every cycle, must be fast |
| The evaluator writing the PFE report | Full traces, sortable, plottable | Cold, offline, after the run |
| `intent_manager`, on restart | The intentions issued before the crash | Once, at startup |

A single storage technology cannot serve all three well. The `database` service therefore maintains **two backends in parallel**: Redis for the hot path (bounded lists, sub-millisecond reads) and an Excel workbook for the cold path (human-readable, directly usable in the report).

The service exists so that no other component has to know either backend. It is the **only writer** of Redis in the whole project, and the only producer of the measurement workbook. Concentrating writes in one place is what makes the key schema (`shared/redis_keys.py`) enforceable rather than merely conventional.

> **Note on the architectural rule.** The rule is *sole writer*, not *sole accessor*. `history_loader` opens its own Redis connection to **read** the metric time-series, deliberately bypassing an HTTP hop on the hottest path of the cycle. This is a validated exception, symmetric to the `collector → database` direct-write exception documented in the README.

## 2. Objectives

| # | Objective |
|---|---|
| O-1 | Provide a single HTTP persistence API, so that no service embeds a Redis client for writing. |
| O-2 | Guarantee that a multi-key write is atomic — a reader never observes a half-written snapshot. |
| O-3 | Bound memory growth without ever expiring data that a reader might still need. |
| O-4 | Mirror everything to a human-readable workbook, for offline analysis and the PFE report. |
| O-5 | Ensure that the slow backend (Excel) never delays the fast one (Redis). |
| O-6 | Fail fast at startup if Redis is unavailable, rather than accepting writes that go nowhere. |
| O-7 | Keep both provider stacks strictly isolated in the same Redis instance. |

## 3. Functional requirements

### 3.1 Metrics persistence

| # | Requirement | Verification |
|---|---|---|
| **FR-1** | The service SHALL expose `POST /store/metrics` accepting `{vm_id, metrics, timestamp?, reliability?}`. | `curl` |
| **FR-2** | It SHALL reject with `400` any payload where `vm_id` is absent or `metrics` is not a dict. | Unit test |
| **FR-3** | For each metric declared in `METRICS_REGISTRY` **and** present with a non-null value, it SHALL append the value to a dedicated per-VM time-series list. | `redis-cli LRANGE` |
| **FR-4** | A metric absent from `METRICS_REGISTRY` SHALL be silently ignored in the time-series, while remaining visible in the full snapshot. | Code review |
| **FR-5** | It SHALL also append a complete JSON snapshot (all metrics + reliability + timestamp) to a per-VM history list. | `redis-cli LRANGE` |
| **FR-6** | Every list SHALL be trimmed to `HISTORY_WINDOW` = 50 entries, newest first. | `redis-cli LLEN` |
| **FR-7** | `timestamp` SHALL default to the current UTC time when the caller omits it. | Code review |

### 3.2 SLO persistence

| # | Requirement | Verification |
|---|---|---|
| **FR-8** | The service SHALL expose `POST /store/slos` accepting `{slos, timestamp?}`. | `curl` |
| **FR-9** | It SHALL reject with `400` a payload where `slos` is absent. An **empty list is valid** and overwrites the contract. | Unit test |
| **FR-10** | It SHALL store the SLO set as a single JSON value under one key, **overwriting** the previous contract. Only the current contract is kept in Redis. | `redis-cli GET` |

### 3.3 Decision persistence

| # | Requirement | Verification |
|---|---|---|
| **FR-11** | The service SHALL expose `POST /store/decision` accepting at least `{decision, from_vm, to_vm}`. | `curl` |
| **FR-12** | It SHALL reject with `400` a payload missing any of those three fields. | Unit test |
| **FR-13** | It SHALL append the **entire** payload — not a projection of it — to a capped FIFO of `DECISIONS_FIFO` = 50 entries. | `redis-cli LRANGE` |

### 3.4 Intent history

| # | Requirement | Verification |
|---|---|---|
| **FR-14** | The service SHALL expose `POST /store/llm_history` accepting `{intention, slos}`, rejecting an empty `intention` with `400`. | `curl` |
| **FR-15** | It SHALL cap the history at `HISTORY_SIZE` = 100 entries. | `redis-cli LLEN` |
| **FR-16** | It SHALL expose `GET /load/llm_history?size=N` returning the entries in **chronological order (oldest first)**, reversing Redis's newest-first storage. | `curl` |
| **FR-17** | A read failure SHALL return an empty list, never an error — the caller must degrade gracefully. | Redis-down test |

### 3.5 Excel mirroring

| # | Requirement | Verification |
|---|---|---|
| **FR-18** | Every successful write SHALL be mirrored to the workbook at `EXCEL_PATH`, on the sheet matching its kind (Métriques, SLOs, Décisions, Intentions_LLM). | Open the file |
| **FR-19** | Excel writing SHALL be **fire-and-forget**: scheduled as a background task, never awaited, and its failure SHALL NOT affect the HTTP response. | Code review |
| **FR-20** | The workbook SHALL be trimmed when it exceeds `EXCEL_MAX_MB` = 200 MB. | Long-run test |

### 3.6 Health

| # | Requirement | Verification |
|---|---|---|
| **FR-21** | `GET /health` SHALL report both service liveness and real Redis reachability (via `PING`). | `curl` |
| **FR-22** | The health check SHALL never raise, whatever the state of Redis. | Redis-down test |

## 4. Non-functional requirements

| # | Requirement | Target / rationale |
|---|---|---|
| **NFR-1 — Atomicity** | Multi-command writes SHALL be issued through a Redis pipeline, so that `LPUSH` and `LTRIM` are never observed apart. | A reader must never see a list of 51 entries, nor an entry pushed without its trim. |
| **NFR-2 — Fail-fast startup** | The service SHALL verify Redis with a `PING` at construction and **refuse to start** if it fails. | A database service that accepts writes into the void is worse than one that is visibly down. |
| **NFR-3 — Hot-path latency** | A `/store/*` call SHALL return in a time compatible with a 6 s cycle in which the collector writes once per VM. | Redis pipeline: sub-millisecond. Excel: seconds — hence NFR-4. |
| **NFR-4 — Backend decoupling** | The latency of the Excel backend SHALL NOT be visible to the caller. | `asyncio.create_task`, never awaited. |
| **NFR-5 — Bounded memory** | Redis memory SHALL be bounded by construction, not by expiry. | `LTRIM` on every list. **No TTL is set** — see C-1. |
| **NFR-6 — Provider isolation** | Two provider stacks sharing one Redis instance SHALL never see each other's data. | `REDIS_DB` = 0 / 1. Key names are identical across providers; only the logical DB separates them. |
| **NFR-7 — Socket timeout** | The Redis client SHALL use a 2 s socket timeout, so a hung Redis cannot block a worker indefinitely. | `socket_timeout=2.0` |
| **NFR-8 — Key centralisation** | No key name SHALL be hardcoded outside `shared/redis_keys.py`. | Code review |

## 5. Interface contract

### 5.1 Consumed — callers

| Caller | Route | Frequency |
|---|---|---|
| `collector` | `POST /store/metrics` | Once per VM per cycle — the highest-volume writer |
| Hub / `metrics_manager` | `POST /store/slos` | Once per cycle when the contract changes |
| Hub | `POST /store/decision` | Once per cycle |
| `intent_manager` | `POST /store/llm_history` | Once per user intention |
| `intent_manager` | `GET /load/llm_history` | Once, at first use after startup |

### 5.2 Produced — Redis key schema

All names come from `shared/redis_keys.py`; none is hardcoded elsewhere.

| Key | Type | Bound | Written by | Read by |
|---|---|---|---|---|
| `metrics:{vm_id}:{metric}` | LIST | 50 (`HISTORY_WINDOW`) | `store_metrics` | `history_loader`, **directly** |
| `metrics:{vm_id}:history` | LIST of JSON | 50 | `store_metrics` | offline inspection |
| `slos:active` | STRING (JSON) | 1 (overwritten) | `store_slos` | offline inspection |
| `decisions:recent` | LIST of JSON | 50 (`DECISIONS_FIFO`) | `store_decision` | offline inspection |
| `llm:history` | LIST of JSON | 100 (`HISTORY_SIZE`) | `store_llm_history` | `GET /load/llm_history` |

### 5.3 Produced — Excel workbook

`EXCEL_PATH` (default `data/qos_history.xlsx`), four sheets: **Métriques**, **Décisions**, **SLOs**, **Intentions_LLM**.

### 5.4 Responses

| Status | Condition |
|---|---|
| `200` | Written — `{"status": "metrics_stored"}` and equivalents |
| `400` | Mandatory field missing or of the wrong type |
| `500` | Redis write failure — `{"detail": "Redis write failure"}` |

## 6. Constraints

| # | Constraint |
|---|---|
| **C-1** | **No TTL, by design.** Earlier versions expired metric keys; the code comment states the current rule explicitly — retention is bounded by `LTRIM`, not by time. A VM that stops reporting keeps its last 50 points indefinitely, which is what lets the ML cascade warm up again after an agent restart. The trade-off is that stale data is indistinguishable from fresh data by key inspection alone; the per-entry `timestamp` is the only freshness signal. |
| **C-2** | **`slos:active` keeps no history.** Each write overwrites. The evolution of the contract over time exists **only** in the Excel workbook. Losing the workbook loses that history. |
| **C-3** | **Redis is a hard dependency at startup, soft afterwards.** The constructor raises if `PING` fails, so the service will not boot without Redis. Once running, a Redis outage produces `500`s but does not stop the process. |
| **C-4** | **Excel is not transactional.** A crash mid-write can leave the workbook inconsistent with Redis. Redis is authoritative; the workbook is an export. |
| **C-5** | **Provider isolation relies solely on `REDIS_DB`.** Key names are identical for both providers. Launching provider-2 without setting `REDIS_DB=1` silently merges the two datasets, and the corruption is invisible until the metrics look impossible. |
| **C-6** | **Writes are not authenticated.** Any host on the network can inject metrics that will feed the ML models and the decisions. |
| **C-7** | Requires a reachable Redis (default `127.0.0.1:6379`) and a writable `data/` directory. |

## 7. Out of scope

- Collecting metrics — `collector`.
- Reading metric time-series for the cycle — `history_loader`, which accesses Redis directly.
- Any business logic, aggregation, statistics or filtering. The `RedisClient` docstring states it: *pure storage*.
- Cross-provider replication — each provider persists only its own data.
- Backup, restore, migration of the Redis dataset.
- Serving the dashboard — `observability` keeps its own in-memory state.

## 8. Traceability against the xQoS internship objectives

| xQoS objective | Contribution of this service |
|---|---|
| O1 — Multi-provider orchestrator | Enables two full stacks on one machine through logical DB separation; carries no orchestration logic itself. |
| O2 — Intent–QoS relationship engine | None directly. Would be the natural place to persist per-provider interpretations if that engine were integrated. |
| O3 — Visualization & explainability | The `decisions:recent` FIFO and the Décisions sheet are the durable audit trail behind the dashboard's log — the dashboard's own state is in memory and dies with the process. |
| O4 — Experimental validation | **Central.** The Excel workbook is the primary data source for the report's measurements: metric traces, SLO evolution, decision history. |
