# Latency Manager — Specification

> **Document type:** Specification (*what the service must do*).
> For *how it does it*, see [`TECHNICAL_DOC.md`](TECHNICAL_DOC.md).

| Field | Value |
|---|---|
| Service name | `latency_manager` |
| Default port | `8001` (`8101` for provider-2, via `PORT_OFFSET`) |
| Component version | 1.1.0 |
| Status | Implemented |
| Position in the pipeline | Northbound ingress — first orchestrator component touched by a measurement |

---

## 1. Context

The QoS Orchestrator drives its whole decision cycle from one signal: the **latency observed between the client and each candidate VM**. In the PiCar-X demonstrator this signal does not come from a network RTT probe but from a **position-based simulated latency**: the PiCar bridge, running on the Raspberry Pi, pings the 8 VM agents, each of which derives a latency value from the Euclidean distance between the vehicle and the VM.

That measurement is produced **outside** the orchestrator, on hardware that is not versioned in this repository. The orchestrator therefore needs a well-defined ingress point that:

- decouples the Hub from the physical measurement source,
- absorbs the unreliability of the link between the Raspberry Pi and the orchestrator host,
- guarantees that a malformed or empty batch never reaches the decision cycle.

`latency_manager` is that ingress point. It is deliberately the **thinnest** service of the stack: it owns no decision logic, no state, and no persistence. Its value is isolation, not computation.

The measurement source is also the **clock of the whole system**: the Hub starts a new orchestration cycle each time it receives a batch. A batch lost here is a cycle not run — which is why the reliability requirements below are stricter than the service's apparent simplicity would suggest.

## 2. Objectives

| # | Objective |
|---|---|
| O-1 | Expose a single, stable HTTP entry point for latency measurements, independent of the measurement technology used upstream. |
| O-2 | Shield the Hub from malformed input by rejecting invalid batches at the boundary. |
| O-3 | Make transmission to the Hub resilient to transient network failures. |
| O-4 | Report to the caller, through HTTP status codes, whether a batch was accepted, rejected, or lost — so the upstream source can act on it. |
| O-5 | Provide human-readable, per-VM operational tracing of every batch received. |
| O-6 | Remain provider-agnostic: the same code serves both provider stacks, differentiated only by configuration. |

## 3. Functional requirements

| # | Requirement | Verification |
|---|---|---|
| **FR-1** | The service SHALL expose `POST /rtt`, accepting a JSON batch of latency measurements. | `curl` against a running instance |
| **FR-2** | The service SHALL reject a batch whose `measurements` field is absent, is not a list, or is an empty list, and SHALL respond `400 Bad Request`. | Unit test (missing) |
| **FR-3** | The service SHALL forward every valid batch to the Hub at `HUB_RTT_URL`, **unchanged** — no field is added, removed, reordered or reinterpreted. | Code review + capture |
| **FR-4** | On successful forwarding the service SHALL respond `202 Accepted`, echoing the `cycle` value carried by the incoming payload. | `curl` |
| **FR-5** | If forwarding fails after all retry attempts, the service SHALL respond `502 Bad Gateway`. The batch is **not** buffered and is definitively lost. | Hub-down test |
| **FR-6** | The service SHALL retry a failed forwarding attempt `POST_RETRY_COUNT` times with a linear backoff of `POST_RETRY_BACKOFF` seconds. | Config + `shared/http_utils.py` |
| **FR-7** | The service SHALL treat HTTP `200`, `201` and `202` from the Hub as success; any other status is a failed attempt and triggers a retry. | `shared/http_utils.py` |
| **FR-8** | The service SHALL log, for each received batch: the cycle number, the number of measurements, and the number of reachable VMs. | Terminal output |
| **FR-9** | The service SHALL log, at `DEBUG` level, one line per VM containing `vm_id`, RTT in ms, and reachability status. | Terminal output |
| **FR-10** | The service SHALL expose `GET /health` returning `{"status": "healthy", "service": "latency_manager"}`. | Startup health check by the Hub |
| **FR-11** | An unexpected internal exception SHALL NOT crash the service; it SHALL be logged and reported as a failure to the caller. | Code review |
| **FR-12** | The service SHALL bind to the port given by `LATENCY_PORT`, which already includes `PORT_OFFSET`, so that two provider stacks can run side by side on the same host. | Two-provider launch |

## 4. Non-functional requirements

| # | Requirement | Target / rationale |
|---|---|---|
| **NFR-1 — Statelessness** | The service SHALL hold no state between two requests: no buffer, no cache, no database, no measurement history. | Any instance can be restarted at any time with no recovery procedure and no data loss beyond the in-flight batch. |
| **NFR-2 — Latency budget** | End-to-end processing of a batch, excluding the Hub's own response time, SHALL stay well below the demonstrator's cycle period. | The effective cycle is **6.0 s**; the nominal path here is a validation plus one HTTP POST. |
| **NFR-3 — Worst-case bound** | Total blocking time in the degraded case SHALL be bounded and predictable. | With the defaults (3 attempts, 5 s timeout, 2 s backoff) the ceiling is `3×5 + 2×2 = 19 s`. **This exceeds the 6 s cycle** — see §6, C-4. |
| **NFR-4 — Non-blocking I/O** | All outbound calls SHALL be asynchronous, so a slow Hub never blocks the reception of a concurrent batch. | `async`/`await` + `httpx.AsyncClient` |
| **NFR-5 — Fault isolation** | A Hub failure SHALL degrade this service to an error response, never to a crash or to a corrupted forward. | Availability of the ingress point is what lets the operator distinguish "measurement source down" from "orchestrator down". |
| **NFR-6 — Observability** | Operational logs SHALL be readable directly in the terminal, colourised, without any external tooling. | Demonstrator constraint: an evaluator watches the terminals live. |
| **NFR-7 — Deployability** | The service SHALL be launchable by a single command, with no dependency other than the shared stack. | See `ETAPES_LANCEMENT_PROJET.md` |
| **NFR-8 — Configurability** | Every operational parameter (Hub URL, port, retries, backoff, timeout) SHALL be overridable by environment variable, with no code change. | `shared/config.py` |

## 5. Interface contract

### 5.1 Consumed — inbound `POST /rtt`

Producer: `picar_bridge_QoS1.py` / `picar_bridge_QoS2.py` (Raspberry Pi, port 8080), which **partitions measurements by provider** and sends each provider's subset to that provider's own Latency Manager. A given instance therefore normally receives **4 measurements**, not 8.

```jsonc
{
  "timestamp": "2026-08-11T09:14:22.031Z",   // ISO 8601, UTC
  "source":    "picar_bridge",
  "cycle":     42,                            // send counter of the bridge
  "measurements": [
    {
      "vm_id":     "edge1",
      "ip":        "194.199.113.18",
      "rtt_ms":    23.7,                      // distance-derived latency
      "raw_ms":    23.7,
      "reachable": true,
      "timestamp": "2026-08-11T09:14:22.031Z"
    }
  ]
}
```

Only `measurements` is contractually enforced by this service (FR-2). All other fields are carried through and validated downstream by the Hub's `LatencyPayload` Pydantic model.

### 5.2 Produced — outbound `POST {HUB_RTT_URL}`

Consumer: Hub Core, `POST /rtt` (port 8000 / 8100). The body is **byte-for-byte the received payload**. The Hub increments its own cycle counter and launches `_run_flow` asynchronously.

### 5.3 Responses returned to the caller

| Status | Condition | Body |
|---|---|---|
| `202 Accepted` | Batch valid and forwarded | `{"status": "forwarded", "cycle": <cycle>}` |
| `400 Bad Request` | `measurements` missing or empty | `{"error": "Invalid payload: measurements is missing or empty"}` |
| `502 Bad Gateway` | Forwarding failed after all retries | `{"error": "Failed to forward measurements to Hub"}` |

## 6. Constraints

| # | Constraint |
|---|---|
| **C-1** | **No persistence.** A batch lost after retry exhaustion is lost for good. This is a deliberate choice: a stale measurement is worse than no measurement, because the Hub would decide on an obsolete position of the vehicle. |
| **C-2** | **No transformation.** The service must never normalise, filter, average or enrich a measurement. All semantics belong to the Hub and to `decision_intelligence`; adding logic here would split the decision across two components. |
| **C-3** | **No knowledge of the topology.** The service does not know which VMs exist, nor which provider it serves. Provider partitioning is done upstream by the bridge; the port separation is done by configuration. |
| **C-4** | **Retry budget exceeds the cycle period.** The worst case (19 s, NFR-3) is longer than the 6 s cycle. Since the bridge sends fire-and-forget with a 5 s client timeout, a fully degraded Hub produces overlapping in-flight requests. Acceptable in the demonstrator — the Hub being down means there is no cycle to be late for — but it is a known dimensioning weakness. |
| **C-5** | **The `cycle` field is not authoritative.** It is the bridge's send counter. The Hub ignores it and uses its own counter. The two can legitimately diverge (e.g. after a Hub restart), and no component must rely on their equality. |
| **C-6** | **Single measured metric.** Only latency transits through this service. CPU and RAM follow a completely different path (`collector` → `database`). |
| **C-7** | Python ≥ 3.10, FastAPI, `httpx`. Runs on the orchestrator host, not on a VM. |

## 7. Out of scope

The following are explicitly **not** requirements of this service:

- Measuring latency itself — done by the VM agents (deployed, not versioned).
- Detecting SLO violations — `decision_intelligence`.
- Storing measurements — `database`.
- Predicting future latency — `ml_predictor`.
- Authenticating or rate-limiting the measurement source — the demonstrator runs on a trusted private network.
- Buffering or replaying lost batches — see C-1.

## 8. Traceability against the xQoS internship objectives

| xQoS objective | Contribution of this service |
|---|---|
| O1 — Multi-provider intent-aware orchestrator | Provides the per-provider measurement ingress; the `PORT_OFFSET` mechanism is what makes two independent stacks possible on one host. |
| O2 — Intent–QoS relationship engine | None. The service carries raw values, never interpretations. |
| O3 — Visualization & explainability | Indirect: its per-VM logs are the ground truth against which the dashboard's latency chart can be checked. |
| O4 — Experimental validation | Its `202`/`400`/`502` responses are the measurement point for input-chain reliability in the demonstrator. |
