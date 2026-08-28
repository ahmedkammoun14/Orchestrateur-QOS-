# QoS Orchestrator — System Specification

> **Document type:** Specification (*what the system must do*).
> For *how it is built*, see [`TECHNICAL_DOC.md`](TECHNICAL_DOC.md).
> For *how to run it*, see [`ETAPES_LANCEMENT_PROJET.md`](ETAPES_LANCEMENT_PROJET.md).
> Per-component specifications live in each folder as `SPEC.md`.

| Field | Value |
|---|---|
| System | QoS Orchestrator — xQoS prototype |
| Context | Final Year Project (PFE), ENIS Sfax × LAAS-CNRS Toulouse |
| Team | Ahmed Kammoun, Mustapha |
| Status | Feature-complete; measurement and analysis phase |
| Scale | 13 processes per provider stack, 2 stacks, 8 VMs |

---

## 1. Context and problem statement

Modern distributed systems — autonomous vehicles, Industry 5.0, real-time monitoring — run on a **cloud continuum** where several actors collaborate: cloud providers, edge and fog providers, IoT verticals, private infrastructures. Each has its own capabilities and constraints, and end-to-end quality of service depends on their coordination.

Three problems follow, and this system addresses them in order.

**Expressing the objective.** An operator does not want to specify a placement; they want to state a goal — *"alert me immediately if the site goes down"*. Turning that sentence into numeric, weighted, comparable QoS constraints is a translation problem, not a configuration one.

**Deciding before the breach.** An orchestrator that migrates *after* an SLO is violated is always late: by the time the violation is measured, the user has already experienced it. Deciding on a **prediction** rather than a measurement is what makes orchestration proactive — and it is why an ML chain sits in the middle of the decision cycle.

**Choosing across providers that cannot be compared directly.** Each provider ranks its own VMs, but a ranking is relative to the pool it was computed on. Comparing two providers requires a quantity measured against **absolute** thresholds, and a negotiation protocol that no single actor controls.

The system is demonstrated on a **PiCar-X** robotic vehicle whose position drives the latency to each VM, over eight real OpenStack VMs split across two providers.

## 2. Objectives

| # | Objective |
|---|---|
| **O-1** | Accept a business objective in natural language and turn it into a quantified, weighted SLO contract. |
| **O-2** | Discover, from observation alone, which secondary metrics correlate with violations, and give them thresholds nobody declared. |
| **O-3** | Predict the future state of every candidate VM, and decide on the prediction rather than the measurement. |
| **O-4** | Select the best VM by a multi-criteria method that handles conflicting objectives and heterogeneous capacities. |
| **O-5** | Arbitrate between two independent providers through a negotiation protocol, with no master. |
| **O-6** | Execute the placement decision on the real infrastructure. |
| **O-7** | Make every decision explainable — which contract, which prediction, which ranking, which margin. |
| **O-8** | Measure the cost of every pipeline stage, for experimental validation. |

## 3. Functional requirements

Component-level requirements live in each `SPEC.md`. What follows are the **system-level** requirements — those no single component satisfies alone.

### 3.1 Operating modes

| # | Requirement | Verification |
|---|---|---|
| **FR-1** | The system SHALL support an **autonomous** mode: a fixed business objective from `METRICS_REGISTRY` (latency < 28 ms), enriched by MI-discovered secondary SLOs. | Autonomous run |
| **FR-2** | It SHALL support an **enhanced** mode: SLOs extracted from a natural-language intention by an LLM, enriched the same way. | Enhanced run |
| **FR-3** | Mode switching SHALL be triggered by receiving an intention, and reversible via the hub's `/reset`. | Dashboard button |
| **FR-4** | In both modes the decision pipeline SHALL be **identical** — only the origin of the primary SLOs differs. | Code review |

### 3.2 The orchestration cycle

| # | Requirement | Verification |
|---|---|---|
| **FR-5** | A cycle SHALL be triggered by the arrival of a latency measurement batch, not by a timer. | `hub POST /rtt` |
| **FR-6** | Each cycle SHALL run: build the SLO contract, collect metrics, load histories, predict, detect violations, decide, act. | Terminal trace |
| **FR-7** | Independent stages SHALL run in parallel — SLO/MI and metric collection have no mutual dependency. | `asyncio.gather` |
| **FR-8** | A cycle SHALL never overlap with the previous one; a batch arriving mid-cycle SHALL be dropped. | Lock |
| **FR-9** | Every cycle SHALL be identified by a number, traceable across every service's terminal. | Cross-terminal check |

### 3.3 End-to-end decision chain

| # | Requirement | Verification |
|---|---|---|
| **FR-10** | A migration SHALL be triggered **only** by a violation on a **primary** objective. | `decision_intelligence` |
| **FR-11** | The decision SHALL be based on ML predictions; the measured value SHALL serve only as a fallback when no prediction exists. | Violation detector |
| **FR-12** | The currently active VM SHALL be a candidate in its own succession. | TOPSIS pool |
| **FR-13** | A placement violating an SLO SHALL never be elected while a compliant one exists (`SLO_ENFORCEMENT = hard`). | Compliance filter |
| **FR-14** | Two anti-oscillation guards SHALL be active: a temporal cooldown and a score margin. | Cooldown + hysteresis |
| **FR-15** | An elected migration SHALL be executed on the real infrastructure via `kubectl`. | `openstack_client` |

### 3.4 Federation

| # | Requirement | Verification |
|---|---|---|
| **FR-16** | Each provider SHALL run a **complete, independent** stack of 12 microservices. | Two-provider launch |
| **FR-17** | The provider hosting the service SHALL be `ACTIVE`, the other `STANDBY`; the role SHALL follow the service across migrations. | Role test |
| **FR-18** | There SHALL be **no master**: any provider may initiate a negotiation. | Contract Net |
| **FR-19** | TOPSIS SHALL run only on a **single provider's** compliant VMs, never on a mixed pool. | `candidates_for_provider()` |
| **FR-20** | Cross-provider comparison SHALL use only the **Gap Grade**, measured against absolute thresholds. | `placement_arbiter` |
| **FR-21** | Inter-orchestrator exchanges SHALL pass exclusively through the relays; a hub SHALL never be reachable by a peer. | `provider_relay` |
| **FR-22** | A challenger SHALL displace the incumbent provider only beyond an absolute dead-band. | Arbiter |
| **FR-23** | An intention received by a `STANDBY` SHALL be propagated to the federation, with an anti-loop guard. | `/intent/propagate` |
| **FR-24** | A `STANDBY` SHALL **adopt** the initiator's complete contract rather than recomputing its own. | Adoption test |
| **FR-25** | If no VM anywhere satisfies the primaries, the outcome SHALL be `INFEASIBLE` and the service SHALL not move. | Arbiter path C |

### 3.5 Explainability

| # | Requirement | Verification |
|---|---|---|
| **FR-26** | Every decision SHALL be accompanied by: the cycle, the breach type, the active SLOs with weights and tiers, the TOPSIS score, and a textual reason. | Audit payload |
| **FR-27** | The MI computation SHALL be traceable step by step in the terminal. | `metrics_manager` |
| **FR-28** | The four TOPSIS phases SHALL be printed as tables, allowing the score to be recomputed by hand. | `decision_intelligence` |
| **FR-29** | A real-time dashboard SHALL expose the state and the decision history. | `:8009` |
| **FR-30** | A read-only federated view SHALL aggregate both providers. | `:8500` |
| **FR-31** | The ML cascade level SHALL be identifiable per prediction. | `model` field |

### 3.6 Measurement

| # | Requirement | Verification |
|---|---|---|
| **FR-32** | Every pipeline stage SHALL be profiled and exported to Excel. | `data/timings_*.xlsx` |
| **FR-33** | Two separate exports SHALL exist: per cycle (autonomous) and per intention (enhanced). | File inspection |
| **FR-34** | The export SHALL document which durations are sums and which are maxima. | Légende sheet |
| **FR-35** | Metrics, SLOs, decisions and intentions SHALL be exported for offline analysis. | `data/qos_history*.xlsx` |

## 4. Non-functional requirements

| # | Requirement | Target / rationale |
|---|---|---|
| **NFR-1 — Cycle period** | The effective cycle SHALL be **6.0 s**, of which ~4.7 s is orchestration work. | `SEND_INTERVAL_S` = 5 s quantised by the browser's 2 s tick. Every timing parameter is dimensioned against this figure. |
| **NFR-2 — Critical path** | Network round-trips to the VMs SHALL be kept **off** the cycle's critical path. | The collector's background polling removes 1.4–1.8 s. |
| **NFR-3 — Progressive degradation** | No single component's failure SHALL stop the cycle. | ML down → reactive detection; peer down → single-provider federation; Redis down → the reader still serves. |
| **NFR-4 — Fail-fast where it matters** | A component that cannot function SHALL refuse to start rather than run degraded. | `database` and `history_loader` verify Redis at construction. |
| **NFR-5 — Provider isolation** | Two stacks on one host SHALL never interfere. | `PORT_OFFSET` (ports) + `REDIS_DB` (data) + per-provider Excel suffix. |
| **NFR-6 — Topological confinement** | Scaling to N orchestrators on N machines SHALL require **configuration only**. | `PROVIDER_RELAY_URLS` and `CORE_URL` are the only declarations of topology. |
| **NFR-7 — Terminal observability** | Every service SHALL be readable live in its terminal, colourised, with no external tooling. | Demonstration constraint: an evaluator watches the terminals. |
| **NFR-8 — Extensibility** | Adding a QoS metric SHALL require editing a single dictionary. | `METRICS_REGISTRY` |
| **NFR-9 — Reproducibility** | The LLM SHALL run at temperature 0; the decision SHALL be deterministic given its payload. | Enhanced mode and `decision_intelligence` |
| **NFR-10 — Statelessness** | Every service except the collector and the hub SHALL be stateless. | Any of them is restartable mid-session. |

## 5. System interfaces

### 5.1 Inbound — what enters the system

| Interface | Source | Frequency |
|---|---|---|
| `POST :8001/rtt` | PiCar bridge (Raspberry Pi) | Every 5 s, per provider |
| `POST :8002/intent` | Operator, or Federation View | On demand |
| `GET :8200/metrics` | Polled on each VM | 1 s, background |
| `POST :8009/reset` | Dashboard button | On demand |

### 5.2 Outbound — what the system produces

| Interface | Target | Content |
|---|---|---|
| `kubectl` via `:8024` | OpenStack cluster | Actual pod migration |
| `POST :8080/active-vm-push` | PiCar bridge | Active VM at end of cycle |
| `data/*.xlsx` | Local disk | Measurements and timings |
| Redis DB 0/1 | Local | Metrics, SLOs, decisions, intent history |

### 5.3 Between orchestrators

Exclusively through the relays (`:8010` ↔ `:8110`), four outbound routes with four inbound counterparts. See [`services/provider_relay/SPEC.md`](services/provider_relay/SPEC.md).

## 6. Constraints

| # | Constraint |
|---|---|
| **C-1** | **The repository holds the orchestrator, not the demonstrator.** Deliberately excluded: the per-VM agents (deployed on the 8 VMs), the PiCar bridge and HTML simulator (on the Raspberry Pi), `openstack_client.py` (on the master `194.199.113.8`), and the ML APIs (separate `Api-Model-Predict` project). **Cloning reproduces the orchestrator, not the demo.** |
| **C-2** | **Three metrics only.** `latency`, `cpu_usage`, `ram_usage`. The other dimensions of the internship offer — reliability, energy, cost, freshness — cannot be expressed. `reliability` is measured by the collector but influences no decision. |
| **C-3** | **The LLM is a hard dependency in enhanced mode.** No regex or keyword fallback remains: a syntactic fallback produced plausible but semantically wrong contracts, which is worse than an explicit failure. Both LLM levels down means enhanced mode is unavailable. |
| **C-4** | **The models' scale convention is a hard contract.** They were trained on values divided by 100. Retraining on raw values without adjusting `ml_predictor` yields predictions 100× too high — every VM permanently in violation. Already observed. |
| **C-5** | **MI needs violations to exist.** With no violation in the history, every MI score is 0 and no secondary SLO is created. The contract is richer under stress than at rest — inherent to a supervised correlation measure. |
| **C-6** | **Two providers, mono-process by default.** `PROVIDER_RELAY_URLS` points both at the same relay, so the current deployment never exercises a real network hop between two relays. |
| **C-7** | **`MULTI_PROVIDER_ENABLED` defaults to `false`.** With it off the cycle behaves exactly as before the federation extension. `launch_provider.py` sets it to `true`. |
| **C-8** | **No authentication anywhere.** Any host on the private network can inject metrics, redefine the business objective, submit forged bids, or trigger `/reset`. Acceptable for the demonstrator; blocking in production. |
| **C-9** | **Engineering parameters, not measured ones.** `ARBITER_DEADBAND` 0.05, `_MIGRATION_MARGIN` 0.05, `MI_RELATIVE_THRESHOLD` 0.15, the CV thresholds, `GAP_GRADE_RHO` 0.1, horizon 7 — all chosen, none derived. |
| **C-10** | **Python 3.10+, FastAPI, Redis.** Runs on the orchestrator host; requires the eight VM agents reachable and, for full function, the ML APIs. |

## 7. Out of scope

- **Provider discovery** — explicitly out of scope in the internship offer; intents are broadcast across a known federation.
- **Scaling, prioritisation, resource allocation** — the offer lists them among the functional capabilities; only **deployment** and **migration** are implemented.
- **Per-provider QoS interpretation** — see §8.
- Training or evaluating the ML models — the `Api-Model-Predict` project.
- Provisioning the VMs or the Kubernetes cluster.
- Backup, restore, or high availability of the orchestrator itself.

## 8. Coverage of the xQoS internship offer

The system is the first operational prototype of the xQoS orchestrator. Coverage against the offer's four objectives, stated honestly:

| Objective | Coverage | Detail |
|---|---|---|
| **O1 — Multi-provider intent-aware orchestrator** | **Largely covered** | Parsing and validating intents ✅ · broadcasting to candidate providers ✅ · aggregating responses and selecting a feasible strategy ✅ (Contract Net + arbiter) · verifying end-to-end QoS ✅ (`hard` enforcement). **Gaps:** intents are broadcast *identically*, with no per-provider adaptation; `scaling`, `prioritization` and `resource allocation` are not implemented. |
| **O2 — Intent–QoS relationship engine** | **Partially covered — the main gap** | The semantic mapping *intention → QoS metrics* is real: the LLM infers the service type and derives its needs, rather than matching keywords. **But** both providers share one `METRICS_REGISTRY` and one threshold: there is no heterogeneous vocabulary, no qualitative concept resolution, no feasibility status, no counter-proposal. The offer's canonical example — *"low latency" = <20 ms for one provider, <40 ms for another* — is not modelled. A `provider_translator.py` implementing exactly this exists on the unmerged branch `claude/admiring-ellis-a33f4d`. |
| **O3 — Visualization & explainability** | **Covered, with two reservations** | Received intent ✅ · decomposition into QoS metrics ✅ · decisions taken ✅ · reasoning trace ✅ (MI steps, TOPSIS tables, textual reasons, path counters). **Reservations:** per-provider interpretations cannot be shown (blocked by O2), and the dashboards are server-rendered HTML rather than React/Vue as the offer's skill list suggests. |
| **O4 — Experimental validation** | **Partially covered** | *Safety-critical vehicular coordination* ✅ — and rigorously: the geometry is dimensioned so that no provider can hold the service for a full lap (45.6 % / 47.1 % / 92.7 % combined), making the handover a physical necessity rather than a staged event. End-to-end QoS satisfaction ✅. **Gaps:** Industry 5.0 and healthcare scenarios absent; scalability tested at 2 providers only; consistency of provider interpretations not measurable without O2; no user study on explanation clarity. |

**Summary.** The three objectives that concern *orchestrating*, *deciding* and *explaining* are met. The objective that concerns *interpreting differently per provider* — which the offer calls central to explainability — is the one substantive gap, and a partial implementation of it already exists on a branch that was never merged.

## 9. Component index

| Component | Port | Specification |
|---|---|---|
| Hub Core | 8000 | [`hub/SPEC.md`](hub/SPEC.md) |
| Latency Manager | 8001 | [`services/latency_manager/SPEC.md`](services/latency_manager/SPEC.md) |
| Intent Manager | 8002 | [`services/intent_manager/SPEC.md`](services/intent_manager/SPEC.md) |
| ML Predictor | 8003 | [`services/ml_predictor/SPEC.md`](services/ml_predictor/SPEC.md) |
| Metrics Manager | 8004 | [`services/metrics_manager/SPEC.md`](services/metrics_manager/SPEC.md) |
| Collector | 8005 | [`services/collector/SPEC.md`](services/collector/SPEC.md) |
| Database | 8006 | [`services/database/SPEC.md`](services/database/SPEC.md) |
| History Loader | 8007 | [`services/history_loader/SPEC.md`](services/history_loader/SPEC.md) |
| Decision Intelligence | 8008 | [`services/decision_intelligence/SPEC.md`](services/decision_intelligence/SPEC.md) |
| Observability | 8009 | [`services/observability/SPEC.md`](services/observability/SPEC.md) |
| Provider Relay | 8010 | [`services/provider_relay/SPEC.md`](services/provider_relay/SPEC.md) |
| Placement Arbiter | 8011 | [`services/placement_arbiter/SPEC.md`](services/placement_arbiter/SPEC.md) |
| Federation View | 8500 | *(not yet documented — shared)* |
| Shared library | — | [`shared/SPEC.md`](shared/SPEC.md) |
