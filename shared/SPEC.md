# Shared — Specification

> **Document type:** Specification (*what this package must do*).
> For *how it does it*, see [`TECHNICAL_DOC.md`](TECHNICAL_DOC.md).

| Field | Value |
|---|---|
| Package name | `shared` |
| Type | Library — **no process, no port, no HTTP** |
| Status | Implemented |
| Consumers | The 12 microservices + the Hub |
| Lines of code | 1652 across 8 modules |

---

## 1. Context

Twelve microservices and a hub run as independent processes. They agree on nothing by construction: each has its own memory, its own logger, its own view of the world. Yet they must agree on a great deal — which port to call, what an SLO looks like, which Redis key holds the latency series, how to retry a failed POST, what a log line looks like.

There are only two ways to obtain that agreement. Either each service declares its own copy — and they drift, silently, until a demonstration fails for a reason nobody can locate — or the agreement is declared **once**, in a place all of them import.

`shared` is that place. It is not a service: it has no port, no process, no lifecycle. It is the **contract layer** every service compiles against.

Its correctness requirement is unusual and worth stating plainly: **a defect here does not break one service, it breaks the system consistently.** A wrong port in `config.py` silently reroutes a whole stack. A changed Redis key makes the writer and the reader agree to disagree. A modified `SLO` field breaks four services at once. That asymmetry is why this package is small, declarative, and deliberately dull.

The package covers five distinct concerns:

| Concern | Module(s) | Why it must be shared |
|---|---|---|
| **Configuration** | `config.py` | Ports, URLs, thresholds — a divergence reroutes or misjudges |
| **Data contracts** | `models.py`, `redis_keys.py` | The shape of what crosses a boundary |
| **Cross-cutting utilities** | `http_utils.py`, `logging_utils.py` | Behaviour that must be identical everywhere |
| **Instrumentation** | `timing.py`, `timing_writer.py` | The measurement campaign's vocabulary and export |
| **Export** | `excel_writer.py` | The offline analysis format |

## 2. Objectives

| # | Objective |
|---|---|
| O-1 | Declare every configuration value once, overridable by environment variable, with no code change. |
| O-2 | Make two full orchestrator stacks coexist on one host, differentiated only by configuration. |
| O-3 | Define the data structures that cross service boundaries, in one place. |
| O-4 | Centralise every Redis key name, so writer and reader cannot drift. |
| O-5 | Provide identical retry and logging behaviour across all services. |
| O-6 | Provide the vocabulary and the export format of the timing measurement campaign. |
| O-7 | Confine knowledge of the topology to as few declarations as possible. |

## 3. Functional requirements

### 3.1 `config.py` — configuration

| # | Requirement | Verification |
|---|---|---|
| **FR-1** | Every operational parameter SHALL be overridable by environment variable, with a documented default. | Code review |
| **FR-2** | `PORT_OFFSET` SHALL shift **every per-provider port** at once, so a second stack needs one variable. | Two-provider launch |
| **FR-3** | Ports of **shared** components (`openstack_client` 8024, `federation_view` 8500, ML APIs 5001-5003) SHALL **not** take the offset. | Code review |
| **FR-4** | `PROVIDER_REGISTRY` SHALL be the single source of truth for provider membership; `PROVIDER_OF_VM` and `VM_REGISTRY` SHALL be **derived** from it. | Code review |
| **FR-5** | `VM_REGISTRY` SHALL be filtered by `PROVIDER_ID`: `all` → the 8 VMs, `provider-N` → its 4. | Startup log |
| **FR-6** | An unknown `PROVIDER_ID` SHALL raise `ValueError` at import — the process must not start. | Startup test |
| **FR-7** | `METRICS_REGISTRY` SHALL declare, per metric: `payload_key`, `unit`, `operator`, `default_threshold`, `bounds`, `always_active`, `is_primary_objective`. | Code review |
| **FR-8** | Adding a metric SHALL require editing **only** this dictionary. | Extension test |
| **FR-9** | `ALL_VM_REGISTRY` SHALL be overridable by `ALL_VM_REGISTRY_JSON`, for a fully local simulated deployment. | Local test |
| **FR-10** | That override SHALL be validated: a VM missing from the JSON SHALL raise `ValueError`. | Unit test |
| **FR-11** | Excel paths SHALL carry a per-provider suffix, so two stacks never write the same workbook. | File inspection |
| **FR-12** | `PROVIDER_RELAY_URLS` and `CORE_URL` SHALL be the **only** declarations of the federation topology. | Grep review |

### 3.2 `models.py` — data contracts

| # | Requirement | Verification |
|---|---|---|
| **FR-13** | `SLO` SHALL carry the eleven fields of the contract, including `is_primary`, and expose `dict()`. | Code review |
| **FR-14** | Pydantic models (`LatencyPayload`, `RTTMeasurementModel`, `IntentToHubPayload`) SHALL validate the HTTP payloads that need it. | FastAPI |
| **FR-15** | `SLOIntent` SHALL be **immutable**: `frozen=True`, `slos` and `attempted_providers` stored as tuples. | Unit test |
| **FR-16** | `SLOIntent.__post_init__` SHALL reject: an empty `intent_id`, an empty SLO list, an invalid `mode`, a duplicate in `attempted_providers`. | Unit test |
| **FR-17** | `with_attempt()` SHALL return a **new** instance, never mutate, and SHALL raise if the provider was already attempted. | Unit test |
| **FR-18** | `to_dict()` / `from_dict()` SHALL round-trip an `SLOIntent` through JSON. | Unit test |
| **FR-19** | `validate_provider_registry()` SHALL verify that every VM is covered exactly once, with no duplicate and no unknown. | Unit test |

### 3.3 `redis_keys.py` — key schema

| # | Requirement | Verification |
|---|---|---|
| **FR-20** | The five key templates SHALL be declared here and **nowhere else**. | Grep review |
| **FR-21** | No service SHALL hardcode a Redis key name. | Grep review |

### 3.4 `http_utils.py` — retry

| # | Requirement | Verification |
|---|---|---|
| **FR-22** | `async_post_with_retry` SHALL retry `retry_count` times with a **linear** backoff. | Unit test |
| **FR-23** | It SHALL treat `200`, `201`, `202` as success, and everything else as a retryable failure. | Unit test |
| **FR-24** | It SHALL **not** sleep after the last attempt. | Unit test |
| **FR-25** | It SHALL return a boolean and never raise. | Unit test |

### 3.5 `logging_utils.py` — formatting

| # | Requirement | Verification |
|---|---|---|
| **FR-26** | `C` SHALL expose the ANSI colour codes used across all services. | Terminal |
| **FR-27** | A custom `SUCCESS` level SHALL be registered at severity 25, between `INFO` (20) and `WARNING` (30). | Code review |
| **FR-28** | `PrettyFormatter` SHALL render `HH:MM:SS  [LEVEL]  message`, colourised, in UTC. | Terminal |

### 3.6 `timing.py` — profiling

| # | Requirement | Verification |
|---|---|---|
| **FR-29** | `StepProfiler.step(name)` SHALL be a context manager recording start, end and duration in ms. | Unit test |
| **FR-30** | Durations SHALL use `time.perf_counter()` — a monotonic clock — and timestamps ISO-8601 UTC. | Code review |
| **FR-31** | The `finally` SHALL guarantee the measurement is recorded even if the block raises. | Unit test |
| **FR-32** | `record()` SHALL accept a duration measured elsewhere. | Code review |
| **FR-33** | `merge()` SHALL accept another service's timings, in either `{name: {...}}` or `{name: ms}` form. | Unit test |
| **FR-34** | The module SHALL **document** the pipeline structure: parallel steps, parallel branches, sequential chains, service order. | Code review |

### 3.7 `timing_writer.py` — timing export

| # | Requirement | Verification |
|---|---|---|
| **FR-35** | Two separate workbooks SHALL be produced: **autonomous** (one row per cycle) and **enhanced** (one row per intention). | File inspection |
| **FR-36** | Headers SHALL be on **three merged levels**: microservice / sub-group / column. | File inspection |
| **FR-37** | Each microservice SHALL have its own header colour. | File inspection |
| **FR-38** | A **Légende** sheet SHALL document the pipeline order, the parallel steps and the sequential chains. | File inspection |
| **FR-39** | A **SUM** column SHALL total the cycle. | File inspection |

### 3.8 `excel_writer.py` — measurement export

| # | Requirement | Verification |
|---|---|---|
| **FR-40** | Four sheets SHALL be maintained: `Métriques`, `Décisions`, `SLOs`, `Intentions_LLM`. | File inspection |
| **FR-41** | Disk writes SHALL be offloaded to a thread and guarded by a lock. | Code review |
| **FR-42** | A corrupted workbook SHALL be detected and recreated rather than crashing the caller. | Fault-injection test |
| **FR-43** | Past `max_bytes`, the oldest 20 % of rows SHALL be deleted from every sheet. | Long-run test |

## 4. Non-functional requirements

| # | Requirement | Target / rationale |
|---|---|---|
| **NFR-1 — No side effects at import** | Importing any module SHALL open no socket, no file, no connection. | A service must be able to import `shared` without provoking anything. |
| **NFR-2 — Fail-fast on inconsistency** | A configuration error SHALL raise at import, not at first use. | An unknown `PROVIDER_ID` must stop the process, not surface three minutes into a demonstration. |
| **NFR-3 — Derivation over duplication** | Every value derivable from another SHALL be derived, never restated. | `PROVIDER_OF_VM`, `VM_REGISTRY`, the service URLs, `HUB_RTT_URL`. |
| **NFR-4 — Environment override** | Every parameter SHALL be settable without touching code. | Two stacks, distributed deployment, and experimental sweeps all rely on this. |
| **NFR-5 — Zero circular imports** | `shared` SHALL never import a service; `models.py` SHALL defer its `config` import. | Import test |
| **NFR-6 — Thread safety of the writers** | Excel writers SHALL be safe under concurrent calls. | `threading.Lock` |
| **NFR-7 — Documentation as code** | The pipeline structure SHALL be a declared data structure, not prose. | `PARALLEL_STEPS`, `SEQUENTIAL_CHAINS`, `PIPELINE_ORDER` are read by the Excel exporter. |

## 5. Interface contract

### 5.1 Module map

| Module | Lines | Exposes |
|---|---|---|
| `config.py` | 414 | ~90 constants, 6 registries |
| `models.py` | 225 | `SLO`, `RTTMeasurement`, `SLOIntent`, 3 Pydantic models, 1 validator |
| `redis_keys.py` | 6 | 5 key templates |
| `http_utils.py` | 34 | `async_post_with_retry` |
| `logging_utils.py` | 33 | `C`, `PrettyFormatter`, level `SUCCESS` |
| `timing.py` | 211 | `StepProfiler` + 4 pipeline-structure constants |
| `timing_writer.py` | 588 | `TimingWriter` (autonomous / enhanced) |
| `excel_writer.py` | 141 | `ExcelWriter` (4 sheets) |

### 5.2 The six registries of `config.py`

| Registry | Role | Derived from |
|---|---|---|
| `ALL_VM_REGISTRY` | The 8 VMs and their agent endpoints | Defaults, or `ALL_VM_REGISTRY_JSON` |
| `PROVIDER_REGISTRY` | **Source of truth** — provider → VMs | Declared |
| `PROVIDER_OF_VM` | Reverse lookup | `PROVIDER_REGISTRY` |
| `VM_REGISTRY` | What **this** process orchestrates | `PROVIDER_ID` + `PROVIDER_REGISTRY` |
| `METRICS_REGISTRY` | The 3 metrics and their semantics | Declared |
| `VM_CLUSTER_MAP` / `VM_NODE_GROUP` | Physical tier / Kubernetes node | Declared |

`VM_NODE_GROUP` deserves attention: it is the **mirror of `NODE_VM_MAP`** held on the OpenStack master. Three simulated VMs share one Kubernetes node, so kubectl cannot distinguish them and always reports the node's canonical VM. This table is what lets the hub know when its own tracking is finer than kubectl's answer.

### 5.3 The five Redis keys

| Template | Type | Bound | Writer | Reader |
|---|---|---|---|---|
| `metrics:{vm_id}:{metric}` | LIST | 50 | `database` | `history_loader` |
| `metrics:{vm_id}:history` | LIST | 50 | `database` | offline |
| `slos:active` | STRING | 1 | `database` | offline |
| `decisions:recent` | LIST | 50 | `database` | offline |
| `llm:history` | LIST | 100 | `database` | `intent_manager` |

## 6. Constraints

| # | Constraint |
|---|---|
| **C-1** | **A change here propagates to thirteen processes.** There is no local blast radius. A key rename, a field addition, a port change affects everything at once. |
| **C-2** | **Provider isolation rests on two variables.** `PORT_OFFSET` (ports) and `REDIS_DB` (data). Launching provider-2 without both silently merges the two stacks, and the corruption is invisible until the numbers look impossible. |
| **C-3** | **Some values are not overridable.** `HISTORY_WINDOW = 50`, `DECISIONS_FIFO = 50`, `GAP_GRADE_RHO = 0.1`, `_MIGRATION_MARGIN = 0.05` are plain constants. `HISTORY_WINDOW` in particular is load-bearing: it is the window the ML cascade's level 1 expects, and lowering it silently pushes predictions to level 3. |
| **C-4** | **`validate_provider_registry()` is defined and never called.** Written, documented, tested — but not wired at startup. An inconsistent registry is therefore detected by its symptoms, not by a check. |
| **C-5** | **The `SLO` dataclass is mutable, deliberately.** `SLOIntent` freezes the *container*, not its contents: `metrics_manager` mutates `weight` and `threshold` in place. The docstring states this as a known and accepted limitation. |
| **C-6** | **`_ABSOLUTE_UNITS` is not here.** The tuple `("cores", "GB")` is redeclared in three modules — `metrics_manager`, `decision_intelligence`, `provider_arbitration`. Each carries a comment saying it mirrors the others. This is precisely the kind of value `shared` exists for, and it escaped. |
| **C-7** | **`shared/` has no `__init__.py`.** It works as an implicit namespace package under Python 3.3+, but it is inconsistent with `services/provider_relay/` and `services/placement_arbiter/`, which have one. |
| **C-8** | **No secret management.** `LAAS_LLM_PROXY` can contain credentials (`https://user:pass@proxy…`) and is read as a plain environment variable, echoed in no log but held in the process environment. |

## 7. Out of scope

- Any business logic — `shared` declares, it does not decide.
- Any I/O at import time — except `ExcelWriter`, which creates its workbook on construction.
- Any inter-service call — `http_utils` provides the *mechanism*, never the destination.
- Runtime validation of the registries — see C-4.
- Secret storage, rotation or encryption.
- Runtime configuration reload — every value is read once, at import.

## 8. Traceability against the xQoS internship objectives

| xQoS objective | Contribution of this package |
|---|---|
| O1 — Multi-provider orchestrator | **Structural.** `PORT_OFFSET`, `PROVIDER_REGISTRY`, `PROVIDER_RELAY_URLS` and `REDIS_DB` are what make two independent stacks possible on one host, and what would make N stacks on N machines a configuration change. |
| O2 — Intent–QoS relationship engine | `METRICS_REGISTRY` is the **QoS vocabulary** — but a *single* one, shared by the whole federation. The offer asks for heterogeneous per-provider vocabularies; the natural home for them would be a `vocabulary` field inside `PROVIDER_REGISTRY`, which is exactly what the unmerged branch's `provider_translator.py` assumes. |
| O3 — Visualization & explainability | `logging_utils` gives every service the same readable trace; `timing.py`'s pipeline constants are the structural documentation the Excel legend renders. |
| O4 — Experimental validation | **Central.** `timing.py` + `timing_writer.py` *are* the measurement campaign: the vocabulary, the profiler, and the export format that produces the report's figures. |
