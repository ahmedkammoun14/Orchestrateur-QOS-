# Observability — Specification

> **Document type:** Specification (*what the service must do*).
> For *how it does it*, see [`TECHNICAL_DOC.md`](TECHNICAL_DOC.md).

| Field | Value |
|---|---|
| Service name | `observability` |
| Default port | `8009` (`8109` for provider-2, via `PORT_OFFSET`) |
| Component version | 2.0.0 |
| Status | Implemented |
| Position in the architecture | Lateral — observes, never participates in the cycle |

---

## 1. Context

Every service of the stack already explains itself in its own terminal: `metrics_manager` prints the five MI steps, `decision_intelligence` prints the four TOPSIS tables, `ml_predictor` prints the cascade level. Together they form a complete, auditable trace of every decision — spread across ten terminal windows nobody can watch simultaneously.

That is the gap this service fills. The xQoS internship offer asks for a dashboard exposing *the received intent, its decomposition into QoS metrics, the decisions taken, and the reasoning trace*. `observability` is the answer for one provider.

It is designed around a strict constraint: **it must never affect the cycle it observes.** An observer that slows down, blocks, or perturbs the observed system is worse than no observer at all during a live demonstration. Three properties follow from that:

- It **pulls** metrics from the hub rather than being pushed them, so a dead dashboard costs the hub nothing.
- Audit events are pushed by the hub **fire-and-forget**, never awaited.
- The service holds no authority: it decides nothing, persists nothing durable, and its only write path is a proxy to the hub's own `/reset`.

The delivery mechanism is **Server-Sent Events** rather than WebSocket or polling. The traffic is strictly one-directional — server to browser — which is exactly what SSE is for; it reconnects automatically, and it needs no library.

## 2. Objectives

| # | Objective |
|---|---|
| O-1 | Present, in one browser page, the live state of every VM and the decision history. |
| O-2 | Make each decision explainable: cycle, breach type, active SLOs with weights, TOPSIS score, migration trace. |
| O-3 | Push updates in real time, without the browser polling. |
| O-4 | Let a client connecting mid-session immediately see the full history. |
| O-5 | Count the multi-provider paths cumulatively, to evidence that all specified cases actually occur. |
| O-6 | Never disturb the observed cycle. |
| O-7 | Offer the operator one control — returning the hub to autonomous mode. |

## 3. Functional requirements

### 3.1 Audit ingestion

| # | Requirement | Verification |
|---|---|---|
| **FR-1** | The service SHALL expose `POST /audit`, receiving one event per decision from the hub. | Network capture |
| **FR-2** | It SHALL stamp `received_at` **only if absent**, without altering any other field. | Code review |
| **FR-3** | The stored entry SHALL be, apart from `received_at`, **identical** to what the hub sent. | Code review |
| **FR-4** | Events SHALL be kept in a FIFO capped at **500** entries. | Long-run test |
| **FR-5** | Each event SHALL be broadcast immediately to every SSE subscriber. | Browser test |
| **FR-6** | A `provider_path` among `A`/`B`/`C`/`D` SHALL increment its counter. | `curl /audit/log` |
| **FR-7** | A mono-provider event — no `provider_path` key — SHALL increment **no** counter. | Non-regression test |
| **FR-8** | `GET /audit/log` SHALL return the full history, its count, and the path counters. | `curl` |

### 3.2 SSE streaming

| # | Requirement | Verification |
|---|---|---|
| **FR-9** | `GET /stream` SHALL open a `text/event-stream` connection. | Browser test |
| **FR-10** | On connection it SHALL immediately send a `snapshot` event carrying the whole audit log and the current counters. | Mid-session connection test |
| **FR-11** | It SHALL then push `audit` and `metrics` events as they occur. | Browser test |
| **FR-12** | After 30 s of silence it SHALL emit a `ping` to keep the connection alive. | Idle test |
| **FR-13** | A disconnected subscriber SHALL be removed from the list. | Code review |
| **FR-14** | Response headers SHALL include `Cache-Control: no-cache` and `X-Accel-Buffering: no`. | Header inspection |

### 3.3 Metrics polling

| # | Requirement | Verification |
|---|---|---|
| **FR-15** | A background task SHALL poll the hub's `GET /data` every **2 s**. | Network capture |
| **FR-16** | Each successful response SHALL be broadcast as a `metrics` event. | Browser test |
| **FR-17** | A hub failure SHALL be **silently ignored**; the loop SHALL continue. | Hub-down test |
| **FR-18** | The poll SHALL be capped by a 3 s timeout. | Code review |

### 3.4 Dashboard

| # | Requirement | Verification |
|---|---|---|
| **FR-19** | `GET /` SHALL serve a self-contained HTML page. | Browser |
| **FR-20** | It SHALL display one card per VM with metric bars and predictions. | Browser |
| **FR-21** | It SHALL display a latency history chart and an SLO weight chart. | Browser |
| **FR-22** | It SHALL display the full audit log with cycle, breach type, TOPSIS score and migration trace. | Browser |
| **FR-23** | It SHALL display a reasoning panel listing the active SLOs. | Browser |
| **FR-24** | Backend reasons, emitted in English, SHALL be translated for display without losing the original. | Browser |
| **FR-25** | It SHALL display the cumulative `A`/`B`/`C`/`D` path counters. | Browser |

### 3.5 Control

| # | Requirement | Verification |
|---|---|---|
| **FR-26** | `POST /reset` SHALL relay to the hub's `POST /reset` and return its response unchanged. | `curl` |
| **FR-27** | An unreachable or erroring hub SHALL yield `502` with an explicit message — **never** an unhandled exception. | Hub-down test |
| **FR-28** | The service SHALL hold no reset logic of its own. | Code review |

### 3.6 Health

| # | Requirement | Verification |
|---|---|---|
| **FR-29** | `GET /health` SHALL report service liveness. | `curl` |

## 4. Non-functional requirements

| # | Requirement | Target / rationale |
|---|---|---|
| **NFR-1 — Non-intrusiveness** | The service SHALL never block, slow or perturb the cycle. | Metrics are pulled; audit is pushed fire-and-forget by the hub. |
| **NFR-2 — Bounded memory** | History SHALL be bounded by construction. | `deque(maxlen=500)` — automatic eviction, no growth. |
| **NFR-3 — Total resilience** | No failure of the hub SHALL degrade the service beyond stale data. | Silent `except` in the poll loop; `502` on the reset proxy. |
| **NFR-4 — Mid-session hydration** | A client connecting at any moment SHALL see the complete state immediately. | Snapshot on connect. |
| **NFR-5 — Zero external dependency at runtime** | The page SHALL need no build step and no local asset server. | Single embedded HTML string; Chart.js from a CDN. |
| **NFR-6 — Multi-subscriber** | Several browsers SHALL be able to watch simultaneously. | One `asyncio.Queue` per subscriber. |
| **NFR-7 — Provider isolation** | Each provider SHALL have its own dashboard on its own port. | `PORT_OFFSET`; the federated view is a separate service. |

## 5. Interface contract

### 5.1 Consumed — `POST /audit` (from the hub)

Emitted at three points of `hub/orchestrator_core.py`, always fire-and-forget.

```jsonc
{
  "cycle": 42, "decision": "migrate", "breach_type": "proactive",
  "from_vm": "edge1", "to_vm": "edge1b",
  "reason": "proactive violation on latency — TOPSIS selected 'edge1b'",
  "topsis_score": 0.8712,
  "slos": [ {"metric": "latency", "weight": 0.62, "is_primary": true} ],
  "mi_scores": {"cpu_usage": 0.41},
  "vm_scores": {"edge1": 0.31, "edge1b": 0.8712},
  "provider_path": "A", "provider_used": "provider-1"
}
```

The payload shape is set by the hub. This service reads only `provider_path` (for the counter) and stores the rest opaquely — which is what keeps it decoupled from the hub's evolving audit schema.

### 5.2 Consumed — `GET {CORE_URL}/data` (polled)

The hub's `state.get_data_payload()`: per-VM metrics, predictions, active VM, cycle.

### 5.3 Produced — SSE events on `/stream`

| `type` | When | Content |
|---|---|---|
| `snapshot` | On connection | `log` (full history) + `provider_path_counts` |
| `audit` | On each decision | `data` (the event) + updated counters |
| `metrics` | Every 2 s | The hub's `/data` payload |
| `ping` | After 30 s idle | Keep-alive |

### 5.4 Produced — `GET /audit/log`

```jsonc
{ "count": 137, "log": [ … ],
  "provider_path_counts": {"A": 92, "B": 11, "C": 3, "D": 0} }
```

Consumed by `federation_view`, which aggregates both providers' logs.

### 5.5 Responses

| Route | `200` | `502` |
|---|---|---|
| `/audit` | `{"status": "ok"}` | — |
| `/audit/log` | history | — |
| `/reset` | the hub's response | hub unreachable or erroring |
| `/` | HTML | — |

## 6. Constraints

| # | Constraint |
|---|---|
| **C-1** | **Nothing is persisted.** The audit log lives in memory and dies with the process. The durable trail is `decisions:recent` in Redis, written by `database`. A dashboard restart loses the session's history. |
| **C-2** | **Capped at 500 events.** Beyond that the oldest are evicted. At one decision per 6 s cycle, that is roughly 50 minutes of session. |
| **C-3** | **The dashboard is one 945-line HTML string** embedded in `app.py` (lines 196–1140). No template engine, no build step, no separate asset. Deliberate — the page must be servable with zero infrastructure — but it makes the front-end hard to edit and impossible to lint. |
| **C-4** | **Chart.js is loaded from a CDN.** Without internet access the charts do not render, though the rest of the page still works. |
| **C-5** | **The 2 s poll is unrelated to the 6 s cycle.** Roughly three polls per cycle, most returning unchanged data. Chosen for display fluidity, not efficiency. |
| **C-6** | **One dashboard per provider.** Watching the federation means opening `:8009` and `:8109`, or using `federation_view` on `:8500`. |
| **C-7** | **The path counters reset on restart.** They are cumulative *since startup*, not since the beginning of the experiment. |
| **C-8** | **No authentication.** Anyone on the network can read the full decision history and **trigger `/reset`**, which returns the hub to autonomous mode and discards the user's intent contract. This is the one place where a read-only service exposes a genuinely destructive action. |

## 7. Out of scope

- Deciding anything — the service observes.
- Persisting durably — `database`.
- Aggregating both providers — `federation_view` (`:8500`).
- Computing metrics, predictions, MI or TOPSIS — it displays what it receives.
- Sending an intention — `federation_view`'s `/api/intent`, or `intent_manager` directly.
- Alerting, thresholding, notifying.

## 8. Traceability against the xQoS internship objectives

| xQoS objective | Contribution of this service |
|---|---|
| O1 — Multi-provider orchestrator | Observes one provider. The `A`/`B`/`C`/`D` counters expose the federated state machine's behaviour. |
| O2 — Intent–QoS relationship engine | Displays the **decomposition of the intent into QoS metrics** — the SLO list with weights and primary/secondary tiers, which is the visible half of O2. What it cannot display is the per-provider interpretation, since that engine is not on `master`. |
| O3 — Visualization & explainability | **This service *is* objective 3 for a single provider.** It delivers: the decomposition into metrics, the decisions taken, and the reasoning trace. Two gaps against the offer: no per-provider interpretations (blocked by O2), and it is server-rendered HTML rather than React/Vue as the offer's skill list suggests. |
| O4 — Experimental validation | The path counters are a directly reportable result — evidence that all five specified cases occur in a real session. `/audit/log` is the machine-readable export for offline analysis. |
