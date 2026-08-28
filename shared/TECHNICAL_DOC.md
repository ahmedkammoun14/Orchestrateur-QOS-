# Shared — Technical Documentation

> **Document type:** Technical documentation (*how this package is built and how it works*).
> For *what it must do*, see [`SPEC.md`](SPEC.md).

| Field | Value |
|---|---|
| Package | `shared` |
| Type | Library — no process, no port |
| Modules | 8 |
| Lines of code | 1652 |
| External dependencies | `pydantic`, `httpx`, `openpyxl` |

---

## 1. Role in the architecture

```text
                        ┌──────────────────────────┐
                        │         shared/          │
                        │                          │
   ┌────────────────────┤  config.py      414 l    │
   │   configuration    │   ports · registries     │
   │                    │                          │
   │   contracts        │  models.py      225 l    │
   │                    │  redis_keys.py    6 l    │
   │                    │                          │
   │   utilities        │  http_utils.py   34 l    │
   │                    │  logging_utils.py 33 l   │
   │                    │                          │
   │   instrumentation  │  timing.py      211 l    │
   │                    │  timing_writer.py 588 l  │
   │                    │  excel_writer.py 141 l   │
   └────────┬───────────┴──────────────────────────┘
            │  imported by
            ▼
   hub  ·  12 services  ·  tests
```

`shared` is the only package **every** other imports, and it imports none of them. The dependency graph is strictly one-directional, which is what keeps it acyclic.

## 2. Folder structure

```text
shared/
├── config.py          # 414 l — ~90 constants, 6 registries
├── models.py          # 225 l — SLO, SLOIntent, Pydantic models
├── redis_keys.py      #   6 l — the 5 key templates
├── http_utils.py      #  34 l — async_post_with_retry
├── logging_utils.py   #  33 l — C, PrettyFormatter, level SUCCESS
├── timing.py          # 211 l — StepProfiler + pipeline structure
├── timing_writer.py   # 588 l — 3-level-header Excel export
├── excel_writer.py    # 141 l — 4-sheet measurement export
├── SPEC.md
└── TECHNICAL_DOC.md
```

No `__init__.py` — an implicit namespace package (SPEC C-7).

## 3. `config.py` — the configuration layer

### 3.1 The universal pattern

```python
NAME: type = type(os.getenv("NAME", default))
```

Applied to ~90 constants. Three consequences: every value is overridable, every value is read **once at import** (no reload), and every value has a visible default in the source.

### 3.2 `PORT_OFFSET` — one variable, one whole stack

```python
PORT_OFFSET = int(os.getenv("PORT_OFFSET", 0))
HUB_PORT    = int(os.getenv("HUB_PORT", 8000)) + PORT_OFFSET
LATENCY_PORT = int(os.getenv("LATENCY_PORT", 8001)) + PORT_OFFSET
… (12 per-provider ports)
```

`PORT_OFFSET=100` shifts the entire second stack: hub 8100, relay 8110, arbiter 8111, dashboard 8109. **One variable, twelve ports.**

The exceptions matter as much as the rule. These do **not** take the offset:

| Component | Port | Why |
|---|---|---|
| `openstack_client` | 8024 | Drives a single Kubernetes cluster — shared by both providers |
| `federation_view` | 8500 | Read-only federated view — one for the whole federation |
| ML APIs | 5001-5003 | Separate project, models shared federation-wide |

Getting this wrong in either direction is a real failure mode: offsetting a shared component starts a second one that nobody calls; failing to offset a per-provider one makes two stacks fight over a port.

### 3.3 Derivation, not duplication

```python
HUB_RTT_URL = f"http://{HUB_HOST}:{HUB_PORT}/rtt"
DATABASE_SERVICE_URL = f"http://{HUB_HOST}:{DATABASE_PORT}"
PROVIDER_OF_VM = {vm: pid for pid, p in PROVIDER_REGISTRY.items() for vm in p["vms"]}
```

Nothing that can be computed is restated. Overriding `HUB_PORT` automatically corrects `HUB_RTT_URL`, `HUB_INTENT_URL`, `HUB_STATS_URL` and `CORE_URL`. This is why the two-provider launch needs so few variables.

### 3.4 `VM_REGISTRY` — the filter that isolates providers

```python
if PROVIDER_ID == "all":
    VM_REGISTRY = ALL_VM_REGISTRY                      # the 8
elif PROVIDER_ID in PROVIDER_REGISTRY:
    VM_REGISTRY = {vm: ALL_VM_REGISTRY[vm]
                   for vm in PROVIDER_REGISTRY[PROVIDER_ID]["vms"]}   # its 4
else:
    raise ValueError(f"PROVIDER_ID={PROVIDER_ID!r} inconnu …")
```

Executed **at import**, so a typo in `PROVIDER_ID` stops the process immediately rather than producing an empty registry that would silently orchestrate nothing.

This single derivation is what makes provider isolation real: the collector polls only these VMs, the hub only considers these candidates, TOPSIS only ranks within this pool.

### 3.5 `ALL_VM_REGISTRY_JSON` — the local simulation escape hatch

```python
if _registry_json:
    ALL_VM_REGISTRY = json.loads(_registry_json)
    manquantes = set(_ALL_VM_REGISTRY_DEFAUT) - set(ALL_VM_REGISTRY)
    if manquantes: raise ValueError(f"… VMs manquantes : {sorted(manquantes)}")
```

Lets the whole orchestrator run against locally simulated VMs — same ids, different ports — with no code change. The validation is the important half: a JSON missing a VM raises rather than producing a partial fleet, which would look like VMs being unreachable.

### 3.6 The two orthogonal axes

```python
VM_CLUSTER_MAP    = {"edge1": "edge-cluster",  "cloud1": "cloud-cluster", …}
PROVIDER_REGISTRY = {"provider-1": {"vms": ["edge1","edge1b","edge1c","cloud1"]}, …}
```

**Tier** (edge/cloud) and **ownership** (provider) are independent dimensions. A provider owns VMs in *both* tiers. The comment states it: *the cluster is a property of the VM, never of the provider*.

This is the transversal partition the supervisor validated, and it is what makes the federation non-trivial — a provider cannot be summarised as "the edge one".

### 3.7 `VM_NODE_GROUP` — the kubectl granularity mirror

```python
VM_NODE_GROUP = {"edge1": "pop1-worker-1", "edge1b": "pop1-worker-1",
                 "edge1c": "pop1-worker-1", …}
```

Three simulated VMs share one Kubernetes node. kubectl resolves to the **node** and always reports that node's canonical VM — it would say `edge1` while the service actually runs on `edge1c`.

This table lets the hub decide whose answer is finer: if its own `service_vm` sits on the same node as kubectl's answer, the hub's value wins; otherwise kubectl does.

The comment carries a maintenance warning: **this table must stay the exact mirror of `NODE_VM_MAP`** in `openstack_client.py`, on the master. Two files, two machines, one invariant, no automatic check.

### 3.8 `METRICS_REGISTRY` — the extension point

```python
"latency": {"payload_key": "rtt_ms", "unit": "ms", "operator": "<",
            "default_threshold": 28.0, "bounds": {"min": 5.0, "max": 2000.0},
            "always_active": True, "is_primary_objective": True}
```

Seven fields, each read by a different consumer:

| Field | Read by |
|---|---|
| `payload_key` | `decision_intelligence`, `topsis` — the field name in the VM payload |
| `unit` | `metrics_manager`, display |
| `operator` | violation direction (ceiling vs floor) |
| `default_threshold` | `metrics_manager` (primary SLO), `topsis` (fallback) |
| `bounds` | threshold and prediction clamping |
| `always_active` | latency is always collected |
| `is_primary_objective` | **the migration gate** |

Adding a metric means adding one entry: `collector` captures it automatically, `database` persists it (its loop iterates the registry), `history_loader` serves it, `metrics_manager` evaluates it by MI. This claim is verifiable by reading those four loops.

## 4. `models.py` — the data contracts

### 4.1 `SLO` — deliberately mutable

Eleven fields, and a hand-written `dict()` rather than `dataclasses.asdict()`. The reason is documented in `SLOIntent`: `metrics_manager` **mutates** `weight` and `threshold` in place, and freezing the dataclass would break existing code. The container is frozen; the contents are not (SPEC C-5).

`is_primary` carries the whole two-tier architecture: `True` means the threshold must **never** be recomputed statistically.

### 4.2 `SLOIntent` — the anti-"chinese-whispers" object

```python
@dataclass(frozen=True)
class SLOIntent:
    intent_id, slos: Tuple[SLO, ...], mode, created_at,
    source_text=None, service=None, attempted_providers: Tuple[str,...] = ()
```

The founding principle, from the docstring: **the natural-language → SLO conversion happens exactly once, at the system's entrance.** It is *this object* that is relayed between providers, never the raw text. A provider therefore never re-interprets a sentence a peer already interpreted — which is what would produce divergence between two providers' contracts.

`source_text` is kept for **provenance and display only**. The docstring is explicit: no consumer may re-interpret it or re-extract SLOs from it.

Three mechanisms enforce the immutability:

```python
object.__setattr__(self, "slos", tuple(self.slos))   # frozen → normal assignment raises
```

`__post_init__` normalises lists to tuples — accepting a list for caller convenience while storing an immutable one. `object.__setattr__` is mandatory here precisely because the dataclass is frozen.

```python
def with_attempt(self, provider_id):
    if self.has_attempted(provider_id): raise ValueError(…)
    return replace(self, attempted_providers=self.attempted_providers + (provider_id,))
```

Returns a **new** instance. And it raises rather than silently no-op'ing: re-attempting the same provider is a relay-logic bug, not a nominal case. This is the structure behind `provider_relay`'s anti-loop guard.

`to_dict()` / `from_dict()` round-trip through JSON, which is what allows the object to cross the HTTP boundary between two orchestrators without reference sharing.

### 4.3 `validate_provider_registry()` — written, not wired

Verifies that every VM is covered exactly once, none is assigned twice, none is unknown. It validates against `ALL_VM_REGISTRY`, **not** `VM_REGISTRY` — the docstring explains why: the latter is filtered by `PROVIDER_ID` and would contain only 4 VMs in a distributed deployment, making the check fail wrongly.

The docstring also states plainly that it is **not invoked at startup**. It is defined and tested (`tests/unit/test_provider_registry.py`), awaiting wiring (SPEC C-4, L-1).

The deferred import (`from shared import config` inside the function) avoids a `models ↔ config` cycle.

## 5. `redis_keys.py` — six lines that matter

```python
METRICS_KEY     = "metrics:{vm_id}:{metric}"
HISTORY_KEY     = "metrics:{vm_id}:history"
SLOS_KEY        = "slos:active"
DECISIONS_KEY   = "decisions:recent"
LLM_HISTORY_KEY = "llm:history"
```

The smallest module of the project, and one of the most consequential. `database` writes with these templates, `history_loader` reads with them. A rename in one place only would make the writer and the reader agree to disagree — the reader would find nothing, report empty series, and the ML cascade would fall to level 3 with no error anywhere.

The comment states the rule: *never hardcoded elsewhere*. It holds — grepping for `"metrics:"` finds only this file and its two consumers importing it.

## 6. `http_utils.py` — 34 lines, three decisions

```python
for attempt in 1..retry_count:
    POST url, timeout=timeout
    if status in (200, 201, 202): return True
    log warning
    if attempt < retry_count: await asyncio.sleep(backoff)
return False
```

**Linear backoff, not exponential.** With a 6 s cycle, doubling delays would push recovery past the next measurement anyway — the extra complexity buys nothing.

**No sleep after the last attempt.** This is why the worst case is `3×5 + 2×2 = 19 s` rather than 21 s.

**Returns a bool, never raises.** The caller decides what a failure means: `latency_manager` turns it into a `502`, `collector` logs it and moves on.

Used by `latency_manager` and `collector`. Notably **not** by `provider_relay`, which has no retry at all (its L-1).

## 7. `logging_utils.py` — the visual identity

```python
logging.SUCCESS = 25
logging.addLevelName(logging.SUCCESS, "SUCCESS")
```

A custom level inserted between `INFO` (20) and `WARNING` (30) — a success worth showing, without the alarm of a warning. Used as `logger.log(logging.SUCCESS, …)` in `database`.

The catch: **the level is registered when this module is imported.** Importing a service module that uses it without importing `logging_utils` first raises `AttributeError`. In practice every service imports `C` or `PrettyFormatter` anyway, so the registration always happens — but the dependency is implicit.

```python
def format(self, record):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    return f"{C.CYAN}{ts}{C.RESET}  {level}  {record.getMessage()}"
```

Note it uses `datetime.now()` rather than `record.created` — the timestamp is the *formatting* time, not the *emission* time. Identical in practice; not the same thing in principle.

## 8. `timing.py` — profiler and pipeline documentation

### 8.1 `StepProfiler`

```python
@contextmanager
def step(self, name):
    t0 = time.perf_counter(); start_iso = now()
    try:    yield
    finally:
        self.steps[name] = {"start": start_iso, "end": now(),
                            "duration_ms": round((perf_counter()-t0)*1000, 3)}
```

`perf_counter()` for the duration — a monotonic clock, immune to system clock adjustments. ISO-8601 UTC for the boundaries — human-readable and comparable across services.

The `finally` guarantees the measurement is recorded **even if the block raises**, which is what keeps the timing export complete when a cycle fails partway.

`merge()` accepts another service's timings in either shape (`{name: {...}}` or `{name: ms}`), which is how `decision_intelligence` returns its TOPSIS sub-steps to the hub and they end up in the same row of the workbook.

### 8.2 Pipeline structure as data

Four module-level constants describe the pipeline — and they are not comments, they are **read by `timing_writer.py`** to build the Excel legend:

| Constant | Content |
|---|---|
| `PARALLEL_STEPS` | The 3 steps run across the 4 VMs by `asyncio.gather` — duration = the slowest VM, **not the sum** |
| `PARALLEL_BRANCHES` | The 2 branches launched together (Metrics Manager→DB ∥ Collector→DB) |
| `SEQUENTIAL_CHAINS` | The 5 chains that cannot be parallelised — each step consumes the previous one's output |
| `PIPELINE_ORDER` | The 7 stages, the reference for Excel column order |

The distinction `PARALLEL_STEPS` vs `PARALLEL_BRANCHES` is subtle and the comments make it explicit: the first is *the same step across 4 VMs*, the second is *two different sequences of steps*. A chain can be sequential internally while belonging to a branch that runs in parallel with another — `collect → persist_metrics` is exactly that.

Making this structure a data declaration rather than prose is what allows the workbook's legend to be generated from it, so the documentation cannot drift from what is measured.

## 9. The two Excel writers

### 9.1 `excel_writer.py` — the measurement export

Four sheets (`Métriques`, `Décisions`, `SLOs`, `Intentions_LLM`), used by `database`.

Three mechanisms, already detailed in `services/database/TECHNICAL_DOC.md` §3.6: thread offloading with a lock, corruption recovery by recreation, and rotation of the oldest 20 %.

### 9.2 `timing_writer.py` — the timing export

The most elaborate module of the package (588 lines), and almost entirely presentation.

**Three merged header levels:**

```
Row 1:  │      Decision Intelligence           │   ← microservice (merged)
Row 2:  │ Sous-étapes internes │ TOPSIS calcul │   ← sub-group (merged)
Row 3:  │ détection │ filtrage │ matrice │ …   │   ← column
```

A column without a sub-group merges rows 2+3; a column standing alone merges rows 1-3.

**Two separate workbooks**, because the unit of observation differs:

| Mode | One row per | Measures |
|---|---|---|
| `AUTONOMOUS` | orchestration cycle | The full cycle duration |
| `ENHANCED` | intention | From LLM reception **to** migration execution |

**One colour per microservice** (`_SVC_DARK` / `_SVC_MED`), so a hundred columns remain readable at a glance.

**A `Légende` sheet** generated from `timing.py`'s four constants — pipeline order, parallel steps, sequential chains — so the reader knows which durations are sums and which are maxima. Without it, a reader would add up three parallel steps and get a cycle time that never existed.

## 10. Configuration reference

Grouped as the source file groups them:

| Group | Key variables |
|---|---|
| Deployment | `PROVIDER_ID`, `PORT_OFFSET` |
| Network | `HUB_HOST`, `HUB_PORT`, `CORE_URL` |
| Ports | 12 per-provider + 3 shared |
| Redis | `REDIS_HOST`, `REDIS_PORT`, **`REDIS_DB`** |
| Persistence | `HISTORY_WINDOW` 50, `DECISIONS_FIFO` 50, `HISTORY_SIZE` 100 |
| Excel | `EXCEL_PATH`, `EXCEL_MAX_MB`, `TIMING_EXCEL_*` |
| Orchestration | `COLLECTION_INTERVAL` 2 s, `MIGRATION_COOLDOWN_S` 5 s, `BOOTSTRAP_MIN` 5 |
| Arbitration | `SLO_ENFORCEMENT` hard, `ARBITER_DEADBAND` 0.05, `AWARD_TIMEOUT_S` 3 s |
| Federation | `PROVIDER_RELAY_URLS`, `MULTI_PROVIDER_ENABLED`, `FEDERATION_VIEW_*` |
| Proactive | `PROACTIVE_FACTOR` 0.85, `HORIZON_ALERT` 3 — **both unused** |
| HTTP | `POST_RETRY_COUNT` 3, `POST_RETRY_BACKOFF` 2 s, `POST_TIMEOUT` 5 s |
| Collector | 6 EMA variables |
| Metrics Manager | `CV_LOW/HIGH`, 3 percentiles, `MI_RELATIVE_THRESHOLD` 0.15 |
| Bounds | `LATENCY_MIN/MAX`, `USAGE_MIN/MAX` |
| LLM | `OLLAMA_URL`, `INTENT_MODEL`, `LAAS_*` |
| ML | `ML_RTT_URL`, `ML_CPU_URL`, `ML_RAM_URL` |

The `_TIMING_SUFFIX` derivation is worth noting:

```python
_TIMING_SUFFIX = "" if PROVIDER_ID == "all" else f"_{PROVIDER_ID.replace('-','')}"
EXCEL_PATH = f"data/qos_history{_TIMING_SUFFIX}.xlsx"
```

Two stacks therefore write `qos_history_provider1.xlsx` and `qos_history_provider2.xlsx` and never collide — a small detail that would otherwise corrupt both workbooks.

## 11. Dependencies

**External** — `pydantic` (models.py), `httpx` (http_utils.py), `openpyxl` (the two writers). `config.py`, `redis_keys.py`, `logging_utils.py` and `timing.py` use only the standard library.

**Internal** — the only intra-package edge is `timing_writer → timing`, plus `models → config` deferred inside a function. No cycle.

**Outbound** — none. `shared` imports no service, which is what keeps the graph one-directional.

## 12. Testing

| File | Covers |
|---|---|
| `tests/unit/test_provider_registry.py` | `PROVIDER_REGISTRY` and its validation |
| `tests/unit/test_slo_intent.py` | The `SLOIntent` model |

Not covered: `http_utils` (the retry policy — used by two services), `logging_utils`, `StepProfiler`, and the two Excel writers.

`http_utils` is the notable gap: 34 lines, pure logic, three behaviours worth pinning (the 2xx set, the no-sleep-on-last-attempt rule, the bool return), and it is on the critical path of every measurement that reaches the hub.

## 13. Known limitations

| # | Limitation | Impact | Possible fix |
|---|---|---|---|
| **L-1** | `validate_provider_registry()` is never called (§4.3). | An inconsistent registry is found by its symptoms, not by a check. | Call it at the end of `config.py`, or at each service's startup. |
| **L-2** | `_ABSOLUTE_UNITS = ("cores","GB")` is declared in **three** modules, none of them here (SPEC C-6). | A divergence resurrects the "0.5 cores → 1.0" defect. | Move it to `config.py`; the three sites already say they mirror each other. |
| **L-3** | `PROACTIVE_FACTOR` and `HORIZON_ALERT` are declared and unused. | The config suggests behaviour that does not exist. | Remove, or wire `HORIZON_ALERT` into the breach search. |
| **L-4** | `HISTORY_WINDOW` and `DECISIONS_FIFO` are not overridable (SPEC C-3). | `HISTORY_WINDOW` is load-bearing for the ML cascade and cannot be tuned. | `os.getenv`, with a comment on the coupling. |
| **L-5** | `VM_NODE_GROUP` must mirror `NODE_VM_MAP` on another machine, with no check (§3.7). | A silent divergence makes the hub trust the wrong VM identity. | Have `openstack_client` expose its map and compare at startup. |
| **L-6** | No `__init__.py` (SPEC C-7). | Inconsistent with two service packages that have one. | Add an empty one. |
| **L-7** | `http_utils`, `StepProfiler` and the writers are untested (§12). | The retry policy, on the critical path, is unverified. | Three tests on `async_post_with_retry`. |
| **L-8** | Configuration is read once at import (§3.1). | Changing a variable requires restarting all thirteen processes. | Acceptable — and arguably desirable — for a demonstrator. |
| **L-9** | `LAAS_LLM_PROXY` may carry credentials in an environment variable (SPEC C-8). | Visible to any process able to read the environment. | A secrets file, or an `.env` outside version control. |
