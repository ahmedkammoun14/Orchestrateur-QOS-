# Provider Relay — Specification

> **Document type:** Specification (*what the service must do*).
> For *how it does it*, see [`TECHNICAL_DOC.md`](TECHNICAL_DOC.md).

| Field | Value |
|---|---|
| Service name | `provider_relay` |
| Default port | `8010` (`8110` for provider-2, via `PORT_OFFSET`) |
| Component version | 1.0.0 |
| Status | Implemented |
| Position in the architecture | Federation gateway — the only path between two orchestrators |

---

## 1. Context

Once the orchestrator became multi-provider, a new problem appeared that no existing service solved: **two independent orchestrators must talk to each other**, and they must do so without either of them knowing the other's internal structure.

The naive approach — hub P1 calls hub P2 directly — was rejected for a specific reason. It would put the peer's address inside the hub, spread across every call site that needs it. Scaling from two orchestrators to N would then mean touching decision logic, not configuration. The Contract Net protocol would become entangled with the network topology.

`provider_relay` is the answer: a **pure transport gateway**. It carries messages between orchestrators and holds no decision logic whatsoever. Its own docstring states the rule in three negations — *it computes nothing, decides nothing, and knows neither the SLOs nor the VMs*.

The routing rule that makes this work is strict and non-obvious:

> **A hub is never reachable by a peer. It is reachable only by its own relay, over localhost.**

Every inter-provider exchange therefore follows the same four-hop shape:

```
hub P1  →  relay P1  →  relay P2  →  hub P2
                                     (localhost)
```

The benefit is concentration. `PROVIDER_RELAY_URLS` and `CORE_URL` in `shared/config.py` are **the only two places in the entire codebase that know the topology**. Moving to N real orchestrators on N machines means editing those tables, and nothing else — no service code changes.

A second, less visible benefit: HTTP/JSON serialisation provides **intent isolation**. One provider cannot mutate the other's SLO objects by reference, because nothing crosses the boundary except bytes.

## 2. Objectives

| # | Objective |
|---|---|
| O-1 | Provide the only communication path between two orchestrators, so that topology is confined to configuration. |
| O-2 | Transport payloads without ever interpreting their substance. |
| O-3 | Support the four exchange patterns the Contract Net needs: unicast handoff, N-ary broadcast with aggregation, award notification, intent propagation. |
| O-4 | Make infinite loops structurally impossible between non-compliant providers. |
| O-5 | Degrade gracefully — one unreachable peer must never paralyse the others. |
| O-6 | Guarantee that a peer's failure never blocks the initiator's own cycle. |
| O-7 | Remain functionally identical whether the federation is one process or N machines. |

## 3. Functional requirements

### 3.1 Routing invariant

| # | Requirement | Verification |
|---|---|---|
| **FR-1** | Every outbound route SHALL target a **peer relay**, never a peer hub. | Code review |
| **FR-2** | Every `/inbound/*` route SHALL deliver to the **local hub** at `CORE_URL`, always localhost. | Code review |
| **FR-3** | Each outbound route SHALL have exactly one `/inbound/*` counterpart on the peer. | Route table |
| **FR-4** | The service SHALL never deserialise a payload into domain objects. It SHALL NOT import `hub/provider_arbitration.py`. | Import review |

### 3.2 `POST /handoff` — unicast

| # | Requirement | Verification |
|---|---|---|
| **FR-5** | The service SHALL route a handoff to `target_provider`'s relay, on its `/inbound`. | Network capture |
| **FR-6** | An unknown `target_provider` SHALL yield `400`. | Unit test |
| **FR-7** | `target_provider == from_provider` SHALL yield `409` — it is a caller bug. | Unit test |
| **FR-8** | **Anti-loop guard:** a `target_provider` already listed in `slo_intent.attempted_providers` SHALL yield `409`. | Unit test |
| **FR-9** | An unreachable target relay SHALL yield `502`. | Peer-down test |
| **FR-10** | The peer's response SHALL be returned as-is, augmented with `relayed_by` and `target_orchestrator` for traceability. | `curl` |
| **FR-11** | A `≥ 400` from the peer SHALL be propagated with its original status code. | Unit test |

### 3.3 `POST /broadcast` — N-ary scatter-gather

| # | Requirement | Verification |
|---|---|---|
| **FR-12** | The service SHALL broadcast the SLO contract to **all** peers **in parallel** and aggregate their bids. | Code review |
| **FR-13** | The initiator SHALL exclude itself from its own broadcast — it evaluates its own VMs locally. | Code review |
| **FR-14** | A missing `slos` or `from_provider` SHALL yield `400`. These are the **only** rejections. | Unit test |
| **FR-15** | A single-member federation SHALL return empty `bids` and `errors` — a valid configuration, not an error. | Unit test |
| **FR-16** | An unreachable, timing-out or erroring peer SHALL feed `errors` **without** failing the call. HTTP `200` regardless. | Peer-down test |
| **FR-17** | A malformed peer response (non-JSON-object) SHALL feed `errors`, not `bids`. | Unit test |
| **FR-18** | Aggregation SHALL be deterministic: peer order is frozen before dispatch and reused when zipping results. | Code review |

### 3.4 `POST /award` — best-effort notification

| # | Requirement | Verification |
|---|---|---|
| **FR-19** | The service SHALL relay the award to the winning provider's relay, on `/inbound/award`. | Network capture |
| **FR-20** | It SHALL use the **short** timeout `AWARD_TIMEOUT_S` = 3 s, not `POST_TIMEOUT`. | Code review |
| **FR-21** | **Every** failure — unknown provider, timeout, network error — SHALL return HTTP `200` with `{"delivered": false, "error": …}`. Never a 4xx/5xx. | Fault-injection test |
| **FR-22** | Success SHALL return `{"delivered": true}`. | `curl` |

### 3.5 `POST /intent/propagate` — notification broadcast

| # | Requirement | Verification |
|---|---|---|
| **FR-23** | The service SHALL propagate an intention to all peers in parallel, on `/inbound/intent`. | Network capture |
| **FR-24** | A missing `from_provider` SHALL yield `400`. | Unit test |
| **FR-25** | It SHALL return `delivered` (provider ids) and `errors` — **no response aggregation**, unlike `/broadcast`. | `curl` |
| **FR-26** | `from_provider` SHALL be stripped from the relayed body. | Code review |

### 3.6 Inbound routes

| # | Requirement | Verification |
|---|---|---|
| **FR-27** | `/inbound` → local hub `/intent/relay`. | Network capture |
| **FR-28** | `/inbound/evaluate` → local hub `/evaluate`. | Network capture |
| **FR-29** | `/inbound/award` → local hub `/award`. | Network capture |
| **FR-30** | `/inbound/intent` → local hub `/intent`, **forcing `propagate: false`**. | Code review |
| **FR-31** | An unreachable local hub SHALL yield `502`. | Hub-down test |
| **FR-32** | Every `/inbound/*` route SHALL be a **terminal point**: it SHALL never re-broadcast nor call another relay. | Code review |

### 3.7 Health

| # | Requirement | Verification |
|---|---|---|
| **FR-33** | `GET /health` SHALL return the peer routing table and the local hub URL. | `curl` |

## 4. Non-functional requirements

| # | Requirement | Target / rationale |
|---|---|---|
| **NFR-1 — Purity of transport** | The service SHALL hold no business state and no domain knowledge. | This is what allows the Contract Net to evolve without touching the network layer. |
| **NFR-2 — Parallelism** | A broadcast SHALL cost the **slowest** peer, not their sum. | With 3 peers at 800 ms: ~800 ms parallel vs ~2400 ms sequential. Sequential dispatch would make cycle time grow linearly with federation size. |
| **NFR-3 — Graceful degradation** | On the broadcast paths, one peer's failure SHALL never fail the whole call. | Otherwise a single dead provider paralyses the entire federation. |
| **NFR-4 — Bounded latency** | Every outbound call SHALL be capped by a timeout. | `POST_TIMEOUT` = 5 s, except award at `AWARD_TIMEOUT_S` = 3 s. |
| **NFR-5 — Loop impossibility** | Two mechanisms SHALL prevent infinite loops: the `attempted_providers` guard on `/handoff`, and the terminal nature of every `/inbound/*` route. | Loop safety must not depend on the payload's content. |
| **NFR-6 — Topological transparency** | Behaviour SHALL be identical in mono-process and distributed deployments. | In mono-process, both entries of `PROVIDER_RELAY_URLS` point at the same relay and the handoff loops back on itself. |
| **NFR-7 — Traceability** | Relayed responses SHALL carry the relay's identity and the target URL. | Debugging a four-hop chain requires knowing which hop answered. |
| **NFR-8 — Statelessness** | No state between calls. | Any relay is restartable at any time. |

## 5. Interface contract

### 5.1 Route table

| Outbound (from the local hub) | Inbound on the peer | Carries | Aggregates? |
|---|---|---|---|
| `POST /broadcast` | `POST /inbound/evaluate` | SLO contract → peer bids | **Yes** — `bids` + `errors` |
| `POST /award` | `POST /inbound/award` | Placement decision | No — `delivered` bool |
| `POST /intent/propagate` | `POST /inbound/intent` | Intention + `intent_version` | No — `delivered` list |
| `POST /handoff` | `POST /inbound` | Legacy direct handoff (pre-Contract-Net) | No |

### 5.2 Message flow

```text
                    ┌── relay P1 ──┐                ┌── relay P2 ──┐
   hub P1 ─────────►│  /broadcast  │───────────────►│ /inbound/eval│─────────► hub P2
   (localhost)      │              │◄───────bid─────│              │◄──bid──── (localhost)
                    └──────────────┘                └──────────────┘
```

### 5.3 `POST /broadcast` — request and response

```jsonc
// request
{ "slos": [ … ], "intent_id": "demo-001",
  "incumbent_vm": "edge1", "from_provider": "provider-1" }

// response — ALWAYS 200 unless the payload is invalid
{ "bids":   [ { "provider_id": "provider-2", "vm_id": "edge2b",
                "gap_grade": -0.12 } ],
  "errors": [ { "provider_id": "provider-3", "error": "ConnectTimeout" } ],
  "relayed_by": "provider_relay",
  "timestamp":  "2026-08-11T09:14:22.031Z" }
```

The bid's internal shape (`gap_grade`, `vm_id`, …) is **opaque** to this service — it is produced by the peer hub and consumed by the initiator's arbiter.

### 5.4 Responses

| Route | `200` | `400` | `409` | `502` |
|---|---|---|---|---|
| `/handoff` | relayed | unknown target | self-handoff, or anti-loop | peer unreachable |
| `/broadcast` | **always** | `slos`/`from_provider` missing | — | — |
| `/award` | **always**, even on failure | — | — | — |
| `/intent/propagate` | always | `from_provider` missing | — | — |
| `/inbound/*` | delivered | — | — | local hub unreachable |

## 6. Constraints

| # | Constraint |
|---|---|
| **C-1** | **Mono-process by default.** `PROVIDER_RELAY_URLS` points both providers at the same relay (`:8010`). A handoff therefore loops back onto the same process — `/handoff` → its own `/inbound` → local hub. Functionally correct, but it means the current deployment never exercises a real network hop between two relays. |
| **C-2** | **Loop safety rests on two different mechanisms.** `/handoff` uses the `attempted_providers` guard, which depends on the *caller* populating that field. The `/inbound/*` routes are safe by construction, because they have nobody to forward to. The second guarantee is stronger than the first. |
| **C-3** | **`/award` never reports a hard failure.** Every error becomes `200 {"delivered": false}`. This is deliberate — the award is an optimisation, and the peer falls back to kubectl discovery if it does not arrive — but it means a permanently broken award path is invisible to the caller. |
| **C-4** | **`/broadcast` and `/intent/propagate` duplicate the same dispatch pattern.** Same self-exclusion, same `asyncio.gather`, same error aggregation; they differ only in whether responses are collected. |
| **C-5** | **No authentication and no message signing.** Any host on the network can inject a forged bid, a forged award, or a forged intention into the federation. |
| **C-6** | **No retry.** Unlike `latency_manager` and `collector`, the relay does not use `async_post_with_retry`. A transient network glitch loses the message for that cycle. |
| **C-7** | **`MULTI_PROVIDER_ENABLED` gates the whole state machine.** Default `false`. With it off, the hub never calls this service — the relay runs but sees no traffic. `launch_provider.py` sets it to `true`. |
| **C-8** | **Timeouts are not aligned with the cycle.** `POST_TIMEOUT` = 5 s against a 6 s cycle: a slow peer consumes most of the budget. |

## 7. Out of scope

- Computing a bid — the peer hub, via `/evaluate`.
- Electing a winner — `placement_arbiter`.
- Comparing providers (Gap Grade) — `hub/provider_arbitration.py`.
- Deciding whether to broadcast — the Hub.
- Knowing what an SLO, a VM or a provider *is* — all payloads are opaque JSON.
- Persisting or replaying messages.
- Discovering peers — the routing table is static configuration; provider discovery is explicitly out of scope in the internship offer too.

## 8. Traceability against the xQoS internship objectives

| xQoS objective | Contribution of this service |
|---|---|
| O1 — Multi-provider orchestrator | **Its entire reason for existing.** It implements the transport of the Contract Net Protocol and is what makes "send adapted intents to candidate service providers" and "aggregate responses" operational. Its confinement of topology to two config tables is the property that makes scaling to N orchestrators a configuration change. |
| O2 — Intent–QoS relationship engine | None — and deliberately so. The relay must stay ignorant of what it carries; a per-provider translation layer would sit at the hub, not here. |
| O3 — Visualization & explainability | Provides `relayed_by`, `target_orchestrator` and the `bids`/`errors` split — the raw material for showing *which providers answered and which did not*. |
| O4 — Experimental validation | The `errors` list is the direct measurement of federation robustness under peer failure, and the parallel-vs-sequential argument (NFR-2) is a quantified scalability result. |
