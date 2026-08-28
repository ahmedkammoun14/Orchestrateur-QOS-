# Hub Core — Technical Documentation

> **Document type:** Technical documentation (*how the component is built and how it works*).
> For *what it must do*, see [`SPEC.md`](SPEC.md).

| Field | Value |
|---|---|
| Package | `hub` |
| Entry point | `hub/orchestrator_core.py` |
| Framework | FastAPI + Uvicorn (`lifespan`) |
| Port | `config.HUB_PORT` = `8000 + PORT_OFFSET` |
| State | **In-memory, volatile** — the only stateful orchestrator |
| Lines of code | 3802 (2785 + 1017) |

---

## 1. Role in the architecture

```text
   latency_manager ──POST /rtt──►┌─────────────────────────────────┐
   intent_manager  ──POST /intent│         HUB CORE :8000          │
   peer relay ─►/evaluate /award │                                 │
                                 │  OrchestratorState (in memory)  │
                                 │   service_vm · current_slos     │
                                 │   cycle_count · is_active       │
                                 │   intent_version · cooldown     │
                                 │                                 │
                                 │  _run_flow()  — 8 stages        │
                                 │   ├ lock: no overlap            │
                                 │   ├ parallel branches A ∥ B     │
                                 │   ├ sequential: hist→pred→dec   │
                                 │   └ StepProfiler on everything  │
                                 │                                 │
                                 │  _step8_decide → 3 paths        │
                                 │   mono | multi | federated      │
                                 └──┬───────────────┬──────────────┘
                                    │               │
        ┌───────────────────────────┘               └──────────────┐
        ▼                                                          ▼
  the 8 services (per cycle)                    relay · arbiter · openstack_client
                                                     (federated path only)
```

The hub is the **only** component holding state, and the only one that *acts*. Everything else answers a question; the hub is what makes an answer consequential.

## 2. Folder structure

```text
hub/
├── orchestrator_core.py     2785 l — state, cycle, 10 routes, 3 decision paths
├── provider_arbitration.py  1017 l — Gap Grade, evaluate_vm/provider, negotiate
├── requirements.txt
├── SPEC.md
└── TECHNICAL_DOC.md
```

Two files with opposite characters. `orchestrator_core.py` is **imperative and stateful** — it sequences, mutates and acts. `provider_arbitration.py` is **pure and functional** — immutable dataclasses and functions with no I/O, testable in a REPL. The split is by nature, not by size.

## 3. `OrchestratorState` — what the system remembers

Twenty fields, grouped by what they answer:

| Group | Fields | Question answered |
|---|---|---|
| **Placement** | `service_vm`, `hosting_vm`, `is_active` | Where is the service, and am I the one running it? |
| **Contract** | `_mode`, `current_slos`, `original_intent_weights`, `intent_version`, `last_intention_text` | What must be respected, and how recent is that instruction? |
| **Rhythm** | `cycle_count`, `bootstrap_cycles`, `BOOTSTRAP_MIN`, `last_migration_ts`, `last_award_ts` | Where are we in time? |
| **Last cycle** | `last_decision`, `last_mi_scores`, `last_collected`, `last_predictions` | What just happened? |
| **Display** | `snapshot_collected`, `snapshot_predictions` | What should the dashboard show? |
| **Concurrency** | `_lock` | Is a cycle already running? |

Three of these deserve explanation.

**`service_vm` vs `hosting_vm`.** The first is *this hub's* belief; the second is kubectl's global truth, and may name a VM belonging to the **other** provider. They diverge legitimately: a `STANDBY` keeps a `service_vm` inherited from when it was active. `get_data_payload` guards against the contradiction explicitly — `is_active` is computed as `(vm_id == service_vm) and self.is_active`, so a standby never advertises `role="standby"` alongside `is_active=true`.

**`last_*` vs `snapshot_*`.** The `last_` fields are mutated during the cycle; the `snapshot_` copies are taken at a stable point and are what `/data` and the federated path read. Without the split, a dashboard polling mid-cycle would see a half-updated state.

**`original_intent_weights`** is the guard described in SPEC C-3, and its necessity is not obvious until you have seen the failure. Explained in §5.2.

## 4. `_run_flow` — the cycle

### 4.1 The lock guard

```python
if state._lock.locked():
    return
async with state._lock:
    ...
```

Two lines that define the system's temporal behaviour. A batch arriving while a cycle runs is **dropped**, not queued.

Queuing would seem safer and would be wrong. The measurement carries the vehicle's position; by the time a queued cycle ran, that position would be stale and the hub would decide on where the PiCar *was*. Dropping keeps every decision anchored to a fresh observation. It also means a cycle exceeding 6 s silently halves the effective frequency — visible only as a gap in the cycle numbering.

### 4.2 The eight stages

```python
with prof.step("total"):
  async with httpx.AsyncClient() as client:          # ONE client for the cycle

    ┌── PARALLEL ────────────────────────────────────────────────┐
    │ _slos_branch()                │ _collect_branch()          │
    │   ① slos_mi   → metrics_mgr   │   ③ collect   → collector  │
    │   ② persist_slos → database   │   ④ persist_metrics → db   │
    └────────────────────────────────────────────────────────────┘
              await asyncio.gather(A, B)     ← "slos_and_collect_parallel"

    ⑥ load_histories   → history_loader × N   (parallel internally)
    ⑦ prediction       → ml_predictor  × N   (parallel internally)
    ⑤ check_violations → local, ~0.4 ms
    [ sync_active_vm ] → openstack_client, LAZY
    ⑧ decision_total   → one of three paths

_persist_timing(prof, mode, vm_for_row)
if state.is_active: create_task(_push_state_to_picar())
```

The numbering is historical — `_step5_check_violations` runs *after* `_step7_predict`, because it needs the predictions. The code comment says so; the function names were not renumbered.

**Why A ∥ B.** Building the SLO contract and collecting CPU/RAM have no mutual dependency; only the rest of the cycle needs both. The comment quantifies the stake by reference to the calibration study (θ=60 ms, v=1.2 cm/s): *every second saved here restores a useful ML prediction on the horizon of 7*. At a 6 s cycle with a moving vehicle, latency budget converts directly into prediction quality.

**One `AsyncClient` for the whole cycle**, opened in the `async with`. This is why `_post_audit` and `_push_state_to_picar` must use **dedicated** clients: they are fire-and-forget tasks that outlive the cycle, and reusing the closed client would raise *"client has been closed"*. The docstrings say exactly that.

### 4.3 Lazy kubectl synchronisation

```python
heartbeat = state.cycle_count % ACTIVE_VM_SYNC_EVERY_N_CYCLES == 0   # 10
if violation_detected or heartbeat:
    await _sync_active_vm(client)
```

Calling `openstack_client` spawns a `kubectl` subprocess on the master — expensive. The cost is paid only when it changes something:

- **On a violation** — the hub must know whether it is ACTIVE before deciding anything.
- **Every 10 cycles** — so a `STANDBY` can discover it has just received the service after an inter-provider migration. Without this heartbeat, role discovery would be circular: you would need to be active to find out that you are.

Two refinements guard the reading:

**The award grace period.** Within `AWARD_GRACE_PERIOD_S` = 15 s of an award, a kubectl reading is ignored — the migration may not have propagated yet, and a stale answer would immediately undo the award.

**Node granularity.** kubectl resolves to the Kubernetes **node**, not the VM: `edge1`, `edge1b` and `edge1c` share one node, so kubectl always reports `edge1`. When the hub's own `service_vm` sits on the same node as kubectl's answer (`VM_NODE_GROUP`), the hub's value is the finer one and is kept; otherwise kubectl wins.

### 4.4 `_step1_slos` — the contract, and its trap

Three exits before any work:

1. **Bootstrap** (`cycle < 5`) → fixed SLOs from `METRICS_REGISTRY` primaries only.
2. **STANDBY** → no recomputation. Its `service_vm` points at a VM of the *other* provider, whose history lives in the other provider's Redis DB — `history_loader` would return zero points and MI would be computed on nothing. The comment records that this used to run anyway, in the void, freezing or overwriting the SLOs and producing two divergent contracts. Skipping also saves two HTTP calls per cycle.
3. **Otherwise** → load the active VM's history, then `/compute` (autonomous) or `/validate` (enhanced).

**The enhanced-mode filter** is the subtle part:

```python
contract = [slo for slo in state.current_slos
            if slo["metric"] in state.original_intent_weights] \
           if state.original_intent_weights else list(state.current_slos)
```

`current_slos` also contains the **secondary** SLOs `metrics_manager` added last cycle. Its step 1 forces `is_primary = True` on everything it receives — so resending them **promotes** them, cycle after cycle. The code comment records the production symptom precisely: an intention that produced a single latency SLO ended up with three primaries (latency, RAM, CPU) at drifting weights, **triggering migrations on metrics the client never asked about**.

`original_intent_weights` is captured once, when `/intent` is received, and is the exact list of the contract. Restoring the original weights each cycle also prevents cumulative dilution through repeated normalisation.

### 4.5 `_step5_check_violations` — the unit conversion

```python
preds = [to_slo_unit(metric, slo, svc_data or {}, p) for p in raw_preds]
if op in ("<", "<="):  breach = any(p > threshold for p in preds)   # cost
elif op in (">", ">="): breach = any(p < threshold for p in preds)  # benefit
```

The `to_slo_unit` call fixes a real defect, documented at length in the source. The ML **always** predicts a percentage for cpu/ram; an LLM threshold may be in cores or GB. Without conversion the hub compared `50.4` (%) with `0.6` (cores): **the breach was never detected**, so an LLM primary could not open the migration gate — while the compliance filter *did* convert and rejected the VM.

The system was therefore refusing VMs on a criterion it could not detect. Symptom: no migration ever fires in enhanced mode with a cores/GB SLO, while the candidate pool keeps shrinking. Hard to diagnose without knowing the two unit systems coexist.

The function returns `bool(violation or proactive_signals_primary)` — **primaries only**. Secondaries feed the log alone.

## 5. The three decision paths

### 5.1 Routing

```python
if not MULTI_PROVIDER_ENABLED:  → _decide_mono_provider
elif <Contract Net>:            → _decide_federated
else:                           → _decide_multi_provider
```

The flag defaults to `false` specifically so the federation extension can never alter pre-existing behaviour by accident. `_decide_multi_provider` is an intermediate stage kept alongside the federated path.

### 5.2 `_decide_mono_provider`

Builds the candidates from `last_collected`, calls `decision_intelligence POST /decide`, and applies the verdict: migration through `openstack_client`, decision stored in `database`, audit posted to `observability`.

### 5.3 `_decide_federated` — the hub as a peer

```text
① cooldown active?              → STAY
② violation_detected?  ── no ──→ STAY          ← THE GATE
③ identities: incumbent_vm, incumbent_provider, my_provider
   incumbent_provider unknown   → fall back to mono-provider
④∥⑤ local bid  ∥  broadcast     ← PARALLEL
⑥ every bid → placement_arbiter /arbitrate
   no answer                    → STAY
⑦ apply: local migration, or award to the winning peer
```

**The gate (②) is what makes the federated path affordable.** Without it, every cycle would cost N HTTP calls — broadcast plus arbitration — while the majority case is "everything is fine". The comment ties it to the θ=40 ms cycle-time calibration.

**④∥⑤ in parallel.** Building the local bid queries `decision_intelligence` and mutates no state; the broadcast sends `current_slos` to the relay. Neither needs the other's result. The source flags the one behavioural change this introduced, and accepts it: if the local bid fails, the broadcast has **already** gone out and peers will have computed a bid for nothing. Harmless — no decision is taken, the path exits in STAY exactly as before.

**⑥ is the rule that defines the architecture.** The hub submits its own bid to an **external** arbiter alongside the peers'. The docstring states it: *the hub never decides alone*. Arbiter silent → STAY, never a blind migration.

## 6. `provider_arbitration.py` — the pure module

### 6.1 Structures

Immutable dataclasses with `to_dict()` / `from_dict()`, so every one of them can cross the HTTP boundary between orchestrators:

| Structure | Content |
|---|---|
| `VMEvaluation` | One VM: values, compliance, violation score, evaluability |
| `ProviderAssessment` | A provider's partition: compliant VMs, best-effort, evaluability |
| `PlacementPlan` | The proposed VM + action |
| `GapGrade` | value, `is_compliant`, `evaluable` |
| `ProviderBid` | **The bid**: `provider_id` + `placement_plan` + `gap_grade` |
| `NegotiationResult` | Outcome of the legacy negotiation path |

### 6.2 `compute_gap_grade` — the cross-provider metric

```
G = ( max(wᵢ·δᵢ) + ρ · Σ(wᵢ·δᵢ) ) / (1 + ρ)        ρ = 0.1
```

Four properties, each deliberate and each documented in the source:

**Non-compensatory.** The `max` term decides: it is the **worst weighted criterion** that sets the grade, and a surplus elsewhere can never buy it back — unlike a weighted sum. The `ρ·Σ` term exists only to separate two VMs whose worst criterion ties, and `ρ` is kept small precisely so the additive term never regains compensatory power.

**`None`, never `0.0`, when nothing is retained.** `0.0` is a legitimate Gap Grade — exactly at threshold — and would be indistinguishable from "not evaluable".

**Mandatory weight renormalisation.** Without it a single primary SLO of weight 0.6 would return `0.6·δ` instead of `δ`. With it, the **non-regression property** holds: one retained primary, any positive weight, gives `G = δ(1+ρ)/(1+ρ) = δ` exactly. Autonomous mode — latency alone — is therefore mathematically identical to a plain `signed_excess`. That property is what allowed the federation to be added without perturbing the existing demonstration.

**The sign does not imply compliance.** In multi-SLO, a VM can violate a secondary in the worst criterion and still score `G < 0`, because the `max` picks the worst *weighted* criterion, not the worst raw one. The docstring warns in capitals: `is_compliant` (from `vm_satisfies_slo`) remains mandatory and must **never** be inferred from the sign.

### 6.3 `evaluate_provider` — and the trap it creates

```python
if not evaluables:
    return ProviderAssessment(..., is_compliant=True, evaluable=False)
```

**"ML down" neutrality.** With no evaluable VM, the provider is declared compliant. The reasoning is sound: absence of information is not a failure, and declaring the provider non-compliant would relay the intention wrongly at the first collection glitch.

But it produces a bid claiming compliance while offering nothing. This is precisely why `placement_arbiter` must test `evaluable` **before** `is_compliant` — swapping those two lines would hand the auction to a **blind provider**. Two components, one shared invariant, no mechanism enforcing it. Worth knowing when either is modified.

The function also **partitions without electing**: it returns *all* compliant VMs and lets TOPSIS choose. `min()` on the best-effort is stable, so an exact tie resolves to input order — determinism required by the tests.

## 7. API reference

### `POST /rtt` — triggers a cycle

```jsonc
{"timestamp": "…", "source": "picar_bridge", "cycle": 42,
 "measurements": [ {"vm_id": "edge1", "rtt_ms": 23.7, …} ]}
```

→ `200 {"status": "accepted", "cycle": 43}`

The returned `cycle` is the **hub's** counter, not the payload's. The two legitimately diverge (SPEC of `latency_manager`, C-5).

### `POST /intent`

```jsonc
{"intent_id": "demo-001", "intention": "…", "slos": [...],
 "intent_version": 1754902462.117,   // optional, assigned if absent
 "propagate": true}                   // false on the inbound path
```

→ `200 {"status": "applied"|"stale", "mode": "enhanced", "slos": 2}`

`"stale"` means a **later** intention is already applied and this one was ignored.

### `POST /evaluate` — the bid

Called by a peer relay. Returns `ProviderBid.to_dict()`: `provider_id`, `placement_plan.vm_id`, `gap_grade.{value,is_compliant,evaluable}`.

### `POST /award`

Makes this hub `ACTIVE` on the awarded VM, and stamps `last_award_ts` to open the grace period.

### `GET /data` — dashboard payload

Per-VM: `rtt_ms`, `cpu_usage`, `ram_usage`, `reliability`, `total_cores`, `total_ram_gb`, `is_active`, `predictions`. Plus `slos`, `mode`, `mi_scores`, `last_decision`, `cycle`, `role`, `hosting_vm`.

### `GET /status`

`mode`, `service_vm`, `role`, `hosting_vm`, `cycle`, `bootstrap_active`, `cooldown_active`, `slos_count`, `active_slos`, `last_intention`, `last_decision`.

Consumed by `intent_manager` as its RAG context — `last_intention` is what lets the LLM decide `additive` vs `replace`.

### `POST /reset` · `GET /health`

Reset rebuilds the bootstrap SLOs and returns to autonomous mode. Health is liveness only.

## 8. Configuration

| Variable | Default | Role |
|---|---|---|
| `HUB_PORT` | `8000` (+`PORT_OFFSET`) | Listening port |
| `PROVIDER_ID` | `all` | `all` = 8 VMs; `provider-N` = its 4 |
| `MULTI_PROVIDER_ENABLED` | `false` | **Gates the federated state machine** |
| `MIGRATION_COOLDOWN_S` | `5.0` s | Post-migration anti-thrashing |
| `BOOTSTRAP_MIN` | `5` | Cycles before dynamic SLOs |
| `ACTIVE_VM_SYNC_EVERY_N_CYCLES` | `10` | kubectl heartbeat |
| `AWARD_GRACE_PERIOD_S` | `15.0` s | Ignore stale kubectl after an award |
| `PICAR_BRIDGE_URL` | `http://140.93.64.105:8080` | Active-VM push target |
| `TIMING_EXCEL_*` | `data/timings_*.xlsx` | Measurement exports |

## 9. Startup

```python
async def lifespan(app):
    log primary business objectives
    for each of the 9 services: GET /health           → ✅ or ❌
    GET openstack_client/health                       → ⚠️ if down
    await _sync_active_vm(client)                     → initial role
    if not success: "⚠️ mode dégradé"
    yield
```

**Fail-soft, deliberately.** Unlike `database` and `history_loader`, which refuse to start, the hub starts anyway and logs a degraded-mode warning. The reasoning is asymmetric: a store service without a store is actively harmful, whereas an orchestrator without one service still runs a useful — if partial — cycle, and the operator can watch the missing dependency come up.

`openstack_client` is treated as a **warning**, not an error: without it migrations are not executed, but the whole decision chain still runs and can be observed. This is what makes a demonstration possible without the OpenStack master.

## 10. Logging

| Marker | Meaning |
|---|---|
| `🔄 Cycle #N \| Mode: … \| VM active: …` | Cycle banner — **the anchor for cross-terminal correlation** |
| `⏱ [Cycle #N] <step> — durée X ms` | One per profiled stage |
| `🟡 STANDBY — SLOs non recalculés` | Standby short-circuit |
| `📡 Métriques collectées` / `📚 Historiques chargés` | Stages 3 and 6 |
| `⏳ Cooldown actif` | Migration blocked |
| `⚠️ VM active absente de PROVIDER_OF_VM` | Fallback to mono-provider |
| `✅ Cycle #N terminé` | End of cycle |

The cycle number appears in this terminal, in `metrics_manager`'s and in `decision_intelligence`'s. It is the only key linking the three.

A **gap in the numbering** means a cycle was dropped by the lock (§4.1) — the cycle before it exceeded 6 s. Worth noticing: it is the one symptom of a budget overrun.

## 11. Testing

The best-covered area of the project, mostly through the federation:

| File | Covers |
|---|---|
| `test_provider_arbitration.py` | The pure module |
| `test_gap_grade.py` | The Tchebycheff formula |
| `test_provider_bid.py` | Bid construction |
| `test_federated_cycle.py` · `test_multi_provider_flow.py` | The federated path |
| `test_active_standby_role.py` · `test_ceding_provider.py` | Role transitions |
| `test_node_granularity.py` | The kubectl/VM_NODE_GROUP subtlety |
| `test_hub_relay_endpoint.py` · `test_award_message.py` | Relay integration |
| `test_federated_reasoning.py` · `test_multi_provider_reasoning.py` | The audit payload |
| `tests/integration/test_full_cycle.py` | The complete cycle |

Not covered: `_run_flow`'s own sequencing (the lock, the parallel branches), and `_step1_slos`' enhanced-mode filter — which guards against a defect that reached production once.

## 12. Known limitations

| # | Limitation | Impact | Possible fix |
|---|---|---|---|
| **L-1** | **State is volatile** (SPEC C-1). A restart in enhanced mode silently returns to autonomous. | The user's contract is lost with no warning. | Persist `current_slos` and `intent_version` to Redis; reload at startup. |
| **L-2** | **`orchestrator_core.py` is 2785 lines** (SPEC C-9). | Least modular component; three decision paths in one file. | Split: `state.py`, `flow.py`, `decisions/`, `routes.py`. |
| **L-3** | **The "ML down" neutrality invariant is shared with `placement_arbiter`** with nothing enforcing it (§6.3). | Reordering two lines there hands placements to a blind provider. | An explicit test naming the invariant, in both packages. |
| **L-4** | **Three decision paths coexist**, one of them unused (SPEC C-7). | `_decide_multi_provider` is dead on the current configuration. | Confirm and remove, or document as the fallback it is. |
| **L-5** | **A dropped cycle produces no explicit log** (§4.1). | The only symptom is a gap in the numbering. | Log at DEBUG on the `locked()` branch. |
| **L-6** | **`intent_version` uses wall-clock time** (SPEC C-8). | Valid only because both hubs share a machine. | A logical clock for a genuinely distributed deployment. |
| **L-7** | **Unit conversion is scattered.** `to_slo_unit`, `_representative_value`, `_to_criterion_value` and `TopsisSelector._to_criterion_value` all convert, in four places. | A divergence resurrects the C-4 defect. | One conversion function in `shared`. |
| **L-8** | **`_run_flow` and the enhanced filter are untested** (§11). | The lock and the anti-promotion guard — both fixes for real defects — are unverified. | Two tests: concurrent cycles, and a contract with secondaries resent. |
| **L-9** | **No authentication** (SPEC C-10). `/intent`, `/award`, `/reset` are all unauthenticated and consequential. | Any host can redefine the objective or force a role change. | Acceptable on the demonstrator's private network. |
