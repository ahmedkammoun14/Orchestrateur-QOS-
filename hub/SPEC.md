# Hub Core — Specification

> **Document type:** Specification (*what the component must do*).
> For *how it does it*, see [`TECHNICAL_DOC.md`](TECHNICAL_DOC.md).

| Field | Value |
|---|---|
| Component name | `hub` — Orchestrator Core |
| Default port | `8000` (`8100` for provider-2, via `PORT_OFFSET`) |
| Component version | 2.1.0 |
| Status | Implemented |
| Position in the architecture | Centre — the only stateful orchestrating component |
| Size | 3802 lines (`orchestrator_core.py` 2785 + `provider_arbitration.py` 1017) |

---

## 1. Context

Twelve microservices each answer one question well. None of them knows what a cycle is, which VM currently hosts the service, whether a migration just happened, or what the user asked for three minutes ago. Each is stateless by design, and that design is only viable because **something else holds the state and sequences the work**.

That something is the hub. It is the single point in the system where three responsibilities meet, and they meet here precisely because they cannot be separated:

**Sequencing.** The eight stages of a cycle have real data dependencies — you cannot predict without a history, decide without a prediction, or act without a decision. Two of them are independent and must run in parallel to fit the 6 s budget. Only a component seeing the whole chain can arrange that.

**State.** `service_vm`, `current_slos`, `cycle_count`, `intent_version`, the migration cooldown, the ACTIVE/STANDBY role — these are facts about the *system*, not about any one service. A service holding them would make itself impossible to restart.

**Authority.** The hub is the only component that *acts*: it triggers migrations, applies arbitration verdicts, and switches operating mode. Every other service returns an answer; the hub is what makes an answer consequential.

In the federation it plays a fourth role that is deliberately **not** a coordinator's: it is a **peer**. It builds its own bid, broadcasts to the others, and submits every bid — including its own — to an external arbiter. The docstring of the federated path states the rule plainly: *the hub never decides alone*. If the arbiter does not answer, the default is STAY, never a blind migration.

## 2. Objectives

| # | Objective |
|---|---|
| O-1 | Sequence the orchestration cycle, parallelising what is independent. |
| O-2 | Hold the system state that no stateless service can hold. |
| O-3 | Execute the placement decision on the real infrastructure. |
| O-4 | Receive user intentions, version them, and propagate them to the federation. |
| O-5 | Participate in the Contract Net as a peer — never as a coordinator. |
| O-6 | Track the ACTIVE/STANDBY role and keep it consistent with reality. |
| O-7 | Guarantee the cycle never overlaps with itself. |
| O-8 | Profile every stage and persist the measurements. |
| O-9 | Degrade progressively: no single service's failure may stop the cycle. |

## 3. Functional requirements

### 3.1 Cycle sequencing

| # | Requirement | Verification |
|---|---|---|
| **FR-1** | A cycle SHALL be triggered by `POST /rtt`, not by a timer. | Network capture |
| **FR-2** | The cycle counter SHALL be incremented **on reception**, before the cycle runs. | Code review |
| **FR-3** | A batch arriving while a cycle is running SHALL be **dropped**, not queued. | Lock test |
| **FR-4** | `POST /rtt` SHALL return immediately; the cycle SHALL run as a background task. | Response time |
| **FR-5** | The SLO/MI branch and the collection branch SHALL run **in parallel** — they have no mutual dependency. | `asyncio.gather` |
| **FR-6** | Everything after the parallel block SHALL be strictly sequential: histories → predictions → violations → decision → action. | Code review |
| **FR-7** | The eight stages SHALL be individually profiled. | Excel export |
| **FR-8** | A service's failure SHALL yield an empty result for that stage, never an exception propagating out of the cycle. | Service-down test |

### 3.2 SLO contract management

| # | Requirement | Verification |
|---|---|---|
| **FR-9** | During bootstrap (`cycle < BOOTSTRAP_MIN` = 5), the hub SHALL use fixed SLOs built from `METRICS_REGISTRY` primaries only. | Startup log |
| **FR-10** | In autonomous mode it SHALL call `metrics_manager POST /compute`; in enhanced mode `POST /validate`. | Network capture |
| **FR-11** | In enhanced mode it SHALL send **only the original LLM contract**, filtered by `original_intent_weights` — never the full `current_slos`. | Code review — see C-3 |
| **FR-12** | It SHALL restore the LLM's original weights on each cycle, to prevent cumulative dilution. | Weight trace |
| **FR-13** | A `STANDBY` provider SHALL **not** recompute its SLOs; it SHALL keep the contract received by broadcast. | Standby log |

### 3.3 Intentions

| # | Requirement | Verification |
|---|---|---|
| **FR-14** | `POST /intent` SHALL replace `current_slos`, switch to `enhanced` mode, and record the raw text. | `GET /status` |
| **FR-15** | Each intention SHALL carry an `intent_version` (epoch), assigned by the **first** hub receiving it. | Code review |
| **FR-16** | An intention older than the applied version SHALL be **ignored**, returning `stale`. | Late-propagation test |
| **FR-17** | The receiving hub SHALL propagate the intention to its peers, unless `propagate: false`. | `/intent/propagate` |
| **FR-18** | It SHALL capture the primary SLOs' original weights into `original_intent_weights`. | Code review |
| **FR-19** | `POST /reset` SHALL rebuild the bootstrap SLOs and return to autonomous mode. | Dashboard button |

### 3.4 Violation detection and decision routing

| # | Requirement | Verification |
|---|---|---|
| **FR-20** | The hub SHALL compute a coherence signal on the active VM before deciding: current violation + ML proactive signal. | Terminal |
| **FR-21** | Predictions SHALL be **converted to the SLO's unit** before comparison — the ML always predicts a percentage, while an LLM threshold may be in cores/GB. | Unit test — see C-4 |
| **FR-22** | The return value SHALL consider **primary** SLOs only; secondaries feed the log alone. | Code review |
| **FR-23** | Routing SHALL choose among three paths: mono-provider, multi-provider, federated, according to `MULTI_PROVIDER_ENABLED`. | Code review |
| **FR-24** | With the flag off, behaviour SHALL be **strictly identical** to the pre-federation code. | Non-regression |

### 3.5 Federation — as a peer

| # | Requirement | Verification |
|---|---|---|
| **FR-25** | The federated path SHALL activate **only** on a detected primary violation. | Gate test |
| **FR-26** | The local bid and the peer broadcast SHALL be issued **in parallel** — they are independent. | `asyncio.gather` |
| **FR-27** | Every bid, including the hub's own, SHALL be submitted to the **external** arbiter. | Network capture |
| **FR-28** | If the arbiter does not answer, the decision SHALL be **STAY** — never a blind migration. | Arbiter-down test |
| **FR-29** | A verdict awarding a peer SHALL be relayed as an `award`, best-effort. | `/award` |
| **FR-30** | `POST /evaluate` SHALL return this provider's bid: champion VM + Gap Grade + compliance. | `curl` |
| **FR-31** | `POST /award` SHALL make this hub `ACTIVE` and adopt the awarded VM. | Role test |
| **FR-32** | An active VM absent from `PROVIDER_OF_VM` SHALL fall back to the mono-provider path rather than crash. | Fault-injection |

### 3.6 Gap Grade — `provider_arbitration.py`

| # | Requirement | Verification |
|---|---|---|
| **FR-33** | The Gap Grade SHALL aggregate the signed deviations of the **primary** SLOs by an augmented Tchebycheff function. | Unit test |
| **FR-34** | Aggregation SHALL be **non-compensatory**: the `max` term decides, and `ρ = 0.1` only breaks ties. | Unit test |
| **FR-35** | It SHALL return `None` — never `0.0` — when no SLO is retained. `0.0` is a legitimate grade. | Unit test |
| **FR-36** | Weights SHALL be renormalised so that a single retained primary SLO yields **exactly** `δ`, whatever its weight. | Unit test — non-regression property |
| **FR-37** | The **sign** of the Gap Grade SHALL NOT be used to infer compliance; `is_compliant` remains mandatory. | Code review — see C-5 |
| **FR-38** | A provider with **no evaluable VM** SHALL return `is_compliant = True` and `evaluable = False` — absence of information is not a failure. | Unit test — see C-6 |

### 3.7 Role and infrastructure

| # | Requirement | Verification |
|---|---|---|
| **FR-39** | The hub SHALL query kubectl **lazily**: on a detected violation, or every `ACTIVE_VM_SYNC_EVERY_N_CYCLES` = 10 cycles. | Network capture |
| **FR-40** | It SHALL ignore a kubectl reading still stale within `AWARD_GRACE_PERIOD_S` = 15 s of an award. | Code review |
| **FR-41** | When its own `service_vm` sits on the same Kubernetes node as kubectl's answer, **its own** value SHALL be kept — it is finer. | `VM_NODE_GROUP` |
| **FR-42** | An elected migration SHALL be executed through `openstack_client`. | kubectl trace |
| **FR-43** | The migration cooldown SHALL block any new migration for `MIGRATION_COOLDOWN_S` = 5 s. | Cooldown test |
| **FR-44** | Only the `ACTIVE` hub SHALL push its state to the PiCar bridge, fire-and-forget. | Code review |

### 3.8 Observability and measurement

| # | Requirement | Verification |
|---|---|---|
| **FR-45** | Every decision SHALL be posted to `observability`, fire-and-forget, with a dedicated HTTP client. | Code review |
| **FR-46** | `GET /data` SHALL expose the dashboard payload; `GET /status` the operational state. | `curl` |
| **FR-47** | Cycle timings SHALL be persisted to the **autonomous** workbook (per cycle) or the **enhanced** one (per intention). | Excel |
| **FR-48** | At startup the hub SHALL health-check every dependency and start in **degraded mode** rather than abort. | Startup log |

## 4. Non-functional requirements

| # | Requirement | Target / rationale |
|---|---|---|
| **NFR-1 — Cycle budget** | A full cycle SHALL fit within 6.0 s, of which ~4.7 s is orchestration work. | Every parallelisation decision is dimensioned against this. |
| **NFR-2 — No overlap** | Two cycles SHALL never run concurrently. | `asyncio.Lock`; a batch arriving mid-cycle is dropped, since a stale measurement is worse than a missed one. |
| **NFR-3 — Progressive degradation** | Every downstream call SHALL tolerate failure and continue with an empty result. | `_post` returns `None` rather than raising. |
| **NFR-4 — Fail-soft startup** | The hub SHALL start even with dependencies down, logging a degraded-mode warning. | Unlike `database`, the hub degrades usefully. |
| **NFR-5 — Sobriety of the federated path** | The federation SHALL cost nothing when everything is fine. | The violation gate avoids N HTTP calls per cycle in the majority case. |
| **NFR-6 — Total ordering of intentions** | Two hubs SHALL never apply intentions in conflicting orders. | `intent_version` epoch; both hubs on one machine, so clocks are directly comparable. |
| **NFR-7 — Lazy kubectl** | A costly subprocess on the master SHALL be paid only when it changes something. | Violation or 10-cycle heartbeat. |
| **NFR-8 — Fire-and-forget for the non-essential** | Audit and PiCar push SHALL never slow or interrupt a cycle. | Dedicated clients, `create_task`. |
| **NFR-9 — Purity of the arbitration module** | `provider_arbitration.py` SHALL be pure functions and immutable dataclasses. | Testable without mocks. |

## 5. Interface contract

### 5.1 Routes

| Route | Caller | Role |
|---|---|---|
| `POST /rtt` | `latency_manager` | **Triggers a cycle** |
| `POST /intent` | `intent_manager`, or a peer relay | Applies an intention |
| `POST /intent/relay` | Peer relay | Legacy handoff (pre-Contract-Net) |
| `POST /evaluate` | Peer relay | **Returns this provider's bid** |
| `POST /award` | Peer relay | Becomes `ACTIVE` on the awarded VM |
| `GET /data` | `observability` | Dashboard payload |
| `GET /status` | `intent_manager` (RAG), `federation_view` | Operational state |
| `POST /reset` | `observability` | Returns to autonomous mode |
| `GET /health` | Launch scripts | Liveness |

### 5.2 Outbound calls per cycle

| Target | Route | When |
|---|---|---|
| `metrics_manager` | `/compute` or `/validate` | Each cycle, if ACTIVE |
| `collector` | `/collect` | Each cycle |
| `database` | `/store/slos`, `/store/decision` | Each cycle / on migration |
| `history_loader` | `/load` × N VMs | Each cycle, parallel |
| `ml_predictor` | `/predict` × N VMs | Each cycle, parallel |
| `decision_intelligence` | `/decide` | Each cycle |
| `provider_relay` | `/broadcast`, `/award`, `/intent/propagate` | Federated path only |
| `placement_arbiter` | `/arbitrate` | Federated path only |
| `openstack_client` | migration | On migration |
| `observability` | `/audit` | Each decision, fire-and-forget |
| PiCar bridge | `/active-vm-push` | End of cycle, if ACTIVE |

### 5.3 State exposed by `GET /status`

`mode`, `service_vm`, `role`, `hosting_vm`, `cycle`, `bootstrap_active`, `cooldown_active`, `slos_count`, `active_slos`, `last_intention`, `last_decision`, `timestamp`.

## 6. Constraints

| # | Constraint |
|---|---|
| **C-1** | **The hub is the only stateful orchestrating component.** Its restart loses `service_vm`, `current_slos`, the cycle counter and the cooldown. The role is rediscovered by kubectl; the contract is not — a restart in enhanced mode silently returns to autonomous. |
| **C-2** | **Cycles are dropped, not queued.** If a cycle exceeds the 6 s period, the next batch is discarded. Deliberate: a stale measurement would make the hub decide on an obsolete position of the vehicle. |
| **C-3** | **Enhanced mode requires filtering the contract before resending it.** `current_slos` also contains the secondaries added by `metrics_manager` last cycle; its step 1 forces `is_primary = True` on everything it receives. Resending them **promotes** them, cycle after cycle. Observed in production: an intention producing one latency SLO ended with three primaries at drifting weights, triggering migrations on metrics the client never asked about. `original_intent_weights` is the guard. |
| **C-4** | **Two coexisting unit systems.** The ML **always** predicts a percentage for cpu/ram, while an LLM threshold may be in cores/GB. Without conversion the hub compared `50.4` (%) with `0.6` (cores): the breach was **never** detected, and an LLM primary could not open the gate — while the compliance filter *did* convert and rejected the VM. The system was refusing VMs on a criterion it could not detect. |
| **C-5** | **The Gap Grade's sign does not imply compliance.** In multi-SLO, a VM can violate a secondary in the worst criterion yet stay `G < 0` thanks to a wide margin on a more heavily weighted one — the `max` term picks the worst *weighted*, not the worst raw. `is_compliant` is mandatory and must never be inferred from the sign. |
| **C-6** | **"ML down" neutrality creates a trap downstream.** A provider with no evaluable VM returns `is_compliant = True` — absence of information is not a failure. But that bid claims compliance while offering nothing, which is why `placement_arbiter` must test `evaluable` **before** `is_compliant`. Two components, one shared invariant. |
| **C-7** | **`MULTI_PROVIDER_ENABLED` defaults to `false`.** Three decision paths coexist; only one runs. `_decide_multi_provider` is an intermediate stage kept alongside `_decide_federated`. |
| **C-8** | **`intent_version` relies on comparable clocks.** Both hubs run on the same machine, so `time.time()` is directly comparable. A genuinely distributed deployment would need a logical clock. |
| **C-9** | **`orchestrator_core.py` is 2785 lines in one file.** Three decision paths, ten routes, the state, the eight stages, the reasoning builder and the timing persistence. It is the least modular component of the project. |
| **C-10** | **No authentication.** `POST /intent`, `POST /award` and `POST /reset` are all unauthenticated and all consequential. |

## 7. Out of scope

- Measuring, predicting, scoring — the twelve services.
- Executing kubectl — `openstack_client`, on the master.
- Electing a winner among bids — `placement_arbiter`.
- Transporting messages between orchestrators — `provider_relay`.
- Extracting SLOs from language — `intent_manager`.
- Persisting metrics — `database`, written directly by the collector.
- Rendering the dashboard — `observability`.

## 8. Traceability against the xQoS internship objectives

| xQoS objective | Contribution of this component |
|---|---|
| O1 — Multi-provider intent-aware orchestrator | **The core of it.** Receives and validates intents, sequences the pipeline, broadcasts to candidate providers, aggregates their responses, verifies end-to-end QoS through `hard` enforcement, and executes the placement. The Gap Grade — the only valid cross-provider comparison — is defined here. |
| O2 — Intent–QoS relationship engine | Receives the interpreted contract and applies it. It is where a per-provider translation layer would plug in — `provider_feasibility.py` on the unmerged branch is designed as a hub-side step (`_step7b`). |
| O3 — Visualization & explainability | Builds and emits the audit payload the dashboard renders: cycle, breach type, contract with weights and tiers, TOPSIS score, full VM ranking, federated path. `_build_reasoning` exists solely for this. |
| O4 — Experimental validation | Owns the timing instrumentation: `StepProfiler` threaded through the cycle, sub-service timings merged, two Excel workbooks produced. Every measurement in the report comes through here. |
