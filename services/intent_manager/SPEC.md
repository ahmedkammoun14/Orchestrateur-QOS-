# Intent Manager — Specification

> **Document type:** Specification (*what the service must do*).
> For *how it does it*, see [`TECHNICAL_DOC.md`](TECHNICAL_DOC.md).

| Field | Value |
|---|---|
| Service name | `intent_manager` |
| Default port | `8002` (`8102` for provider-2, via `PORT_OFFSET`) |
| Component version | 2.0.0 |
| Status | Implemented |
| Position in the pipeline | Northbound ingress — the intent-driven entry point of the orchestrator |

---

## 1. Context

The orchestrator has two operating modes. In **autonomous** mode it pursues a fixed business objective declared in `shared/config.py` (latency < 28 ms). In **enhanced** mode the objective comes from a **human, expressed in natural language** — *"préviens-moi tout de suite si le site tombe"*, *"je veux transcoder de la vidéo en continu"*.

Nothing downstream of this service can consume a sentence. TOPSIS needs numeric criteria with weights, the violation detector needs thresholds with comparison operators, and the compliance filter needs units it recognises. **`intent_manager` is the translation boundary between human language and the numeric contract that drives the whole decision cycle.**

This is the single most delicate service of the stack, for three reasons:

1. **It is the only non-deterministic component.** Everything else in the pipeline is reproducible; an LLM is not. A specification that assumes deterministic output would be false.
2. **It sets the weights.** The `weight` field it produces is used directly by TOPSIS in its weighting step. A misjudged weight silently changes every migration decision that follows.
3. **It decides how a new intention combines with the previous one.** Answering *"does this new sentence replace the current contract or add to it?"* requires understanding meaning, not keywords — this is the `merge_strategy` decision.

The service is also where the xQoS notion of *intent* is realised concretely: a user states a **goal**, never a placement, and the orchestrator derives the QoS requirements itself.

## 2. Objectives

| # | Objective |
|---|---|
| O-1 | Accept a free-form natural-language intention over HTTP and turn it into a validated, typed list of SLOs. |
| O-2 | Infer the *type of service* the intention implies, and derive its resource needs from that — not from keywords in the sentence. |
| O-3 | Assign each SLO a weight reflecting its business importance within the intention. |
| O-4 | Decide how the new SLOs combine with the currently active contract (REPLACE or ADDITIVE). |
| O-5 | Guarantee that whatever the LLM returns, what leaves the service is structurally valid and numerically bounded. |
| O-6 | Survive the unavailability of the primary LLM without losing the capability. |
| O-7 | Preserve the history of past intentions across restarts, so the reasoning has continuity. |
| O-8 | Refuse, explicitly and without guessing, an intention that no deployable network service can fulfil. |

## 3. Functional requirements

### 3.1 Ingestion

| # | Requirement | Verification |
|---|---|---|
| **FR-1** | The service SHALL expose `POST /intent` accepting `{"intention": "<text>", "intent_id": "<optional>"}`. | `curl` |
| **FR-2** | The service SHALL reject an absent, empty or whitespace-only `intention` with `400 Bad Request`. | Unit test |
| **FR-3** | The service SHALL generate an `intent_id` of the form `gen-HHMMSS` when the caller does not provide one. | Code review |
| **FR-4** | The service SHALL expose `GET /health`, reporting both its own liveness and Ollama reachability. | `curl` |

### 3.2 Context enrichment (RAG)

| # | Requirement | Verification |
|---|---|---|
| **FR-5** | Before calling the LLM, the service SHALL fetch the live system state from the Hub's `GET /status`. | Network capture |
| **FR-6** | The context passed to the LLM SHALL include: the currently active SLOs, the **previous** intention, the current `service_vm`, and the cycle number. | Prompt inspection |
| **FR-7** | If the Hub is unreachable within `RAG_TIMEOUT`, the service SHALL proceed with an empty context rather than fail. | Hub-down test |
| **FR-8** | On first use, the service SHALL reload the last `HISTORY_SIZE` intentions from Redis via the database service. | Restart test |
| **FR-9** | Each processed intention SHALL be persisted to Redis; a persistence failure SHALL be logged but SHALL NOT fail the request. | Redis-down test |

### 3.3 LLM extraction

| # | Requirement | Verification |
|---|---|---|
| **FR-10** | The service SHALL call the LAAS vLLM endpoint (Qwen3-27B) as the primary extractor, at temperature 0.0. | Log line `🤖 Appel LAAS vLLM` |
| **FR-11** | If LAAS fails, times out, or returns unparseable content, the service SHALL fall back to a local Ollama model. | Log line `⚠️ LAAS indisponible` |
| **FR-12** | If **both** levels fail, the service SHALL return `422 Unprocessable Entity`. No SLO is fabricated. | Both-down test |
| **FR-13** | The LLM SHALL be constrained to the three metrics of `METRICS_REGISTRY`: `latency`, `cpu_usage`, `ram_usage`. No other metric name is admissible. | Prompt + normalisation |
| **FR-14** | The service SHALL accept both the current response format `{"merge_strategy": …, "slos": [...]}` and the legacy bare-array format. | `test_llm_handler.py` |
| **FR-15** | An empty `slos` array SHALL be interpreted as *"no deployable service matches this intention"* and SHALL yield `422`, not a default contract. | Off-domain intention test |

### 3.4 Normalisation and validation

| # | Requirement | Verification |
|---|---|---|
| **FR-16** | Metric aliases (`cpu`, `ram`, `mémoire`, `latence`, `ping`, `rtt`, `delay`, …) SHALL be normalised to canonical names. | Unit test |
| **FR-17** | Units SHALL be normalised: `Go` → `GB`; `Mo`/`MB` → `GB` **with division of the value by 1024**. | Unit test |
| **FR-18** | A `latency` threshold SHALL be clamped to `[LATENCY_MIN, LATENCY_MAX]` = `[5, 2000]` ms. | Unit test |
| **FR-19** | A `cpu_usage`/`ram_usage` threshold expressed in `cores`/`GB` SHALL be floored at `0.1` and NOT clamped to a percentage range — it is an absolute resource need, not a load ratio. | Unit test |
| **FR-20** | The same metrics expressed as a percentage SHALL be clamped to `[USAGE_MIN, USAGE_MAX]` = `[1, 99]`. | Unit test |
| **FR-21** | A missing `target` SHALL be derived from the threshold: `×0.9` for a ceiling (`<`), `×1.1` for a floor (`>=`). | Unit test |
| **FR-22** | Missing `window`, `weight`, `confidence`, `budget_remaining`, `violations` SHALL receive documented defaults. | Code review |
| **FR-23** | An SLO that fails `SLO(**r)` construction SHALL be dropped with a warning, without aborting the others. | Unit test |
| **FR-24** | Every SLO produced by this service SHALL be marked `is_primary = True`. | Code review |

### 3.5 Merging with the active contract

| # | Requirement | Verification |
|---|---|---|
| **FR-25** | The merge strategy SHALL be taken from the LLM's `merge_strategy` field when it is `replace` or `additive`. | `test_slo_merger.py` |
| **FR-26** | Keyword detection on the raw text SHALL be used **only** as a fallback when the LLM supplied no usable strategy. | Code review |
| **FR-27** | `REPLACE` SHALL discard the active SLOs entirely. | Unit test |
| **FR-28** | `ADDITIVE` SHALL keep active SLOs and overwrite, per metric, those the new intention redefines. | Unit test |
| **FR-29** | `REFINE` SHALL be disabled on the LLM path (`allow_refine=False`), the LLM already handling coherence via the RAG context. | Code review |
| **FR-30** | The service SHALL warn when the merged set contains an operator conflict on one metric, or a threshold gap above 50 %. | Log inspection |
| **FR-31** | Final weights SHALL be normalised so that `Σ weight = 1`. | Unit test |

### 3.6 Delivery to the Hub

| # | Requirement | Verification |
|---|---|---|
| **FR-32** | The service SHALL POST the final contract to `HUB_INTENT_URL`, carrying `intent_id`, `intention`, the serialised SLOs, a timestamp, and a `timing` block. | Network capture |
| **FR-33** | The `timing` block SHALL report `reception_ms`, `llm_ms` and `started_at`, feeding the project's timing instrumentation. | Excel export |
| **FR-34** | A Hub rejection or connection failure SHALL yield `502 Bad Gateway`. | Hub-down test |
| **FR-35** | On success the service SHALL return `202 Accepted` with the number of SLOs produced. | `curl` |

## 4. Non-functional requirements

| # | Requirement | Target / rationale |
|---|---|---|
| **NFR-1 — Bounded latency** | LLM calls SHALL be capped at 60 s per level; RAG at `RAG_TIMEOUT` = 2 s; the Hub POST at `POST_TIMEOUT` = 5 s. | The worst case (LAAS timeout + Ollama timeout) is ~127 s. This is **out of band with the 6 s cycle** — see C-3. |
| **NFR-2 — Graceful degradation** | Every optional dependency (Hub `/status`, Redis history) SHALL degrade to a documented default, never to a failure. | Only the LLM is a hard dependency, because without it there is nothing to translate. |
| **NFR-3 — Determinism of the boundary** | Whatever the LLM's non-determinism, the output SHALL always satisfy the `SLO` schema and the numeric bounds. | Non-determinism is confined upstream of `_normalize_and_validate`; downstream services can assume a valid contract. |
| **NFR-4 — Reproducibility of extraction** | The LLM SHALL be called at `temperature = 0.0` with "thinking" disabled. | Maximises repeatability of a demonstration and eliminates reasoning preamble that would break JSON parsing. |
| **NFR-5 — Explainability** | Each stage SHALL be traced in the terminal: raw LLM thresholds, chosen strategy and its source, final validated SLOs. | This trace is the raw material of the explainability dashboard (xQoS objective 3). |
| **NFR-6 — Continuity** | Intent history SHALL survive a service restart. | An orchestrator that forgets the previous intention cannot judge `additive` vs `replace`. |
| **NFR-7 — Provider agnosticism** | The same code SHALL serve both providers, differentiated only by `PORT_OFFSET` and by the Hub URL. | Two instances run simultaneously. |
| **NFR-8 — Cost of a call** | One user intention SHALL cost at most two LLM calls. | No retry loop on top of the two-level cascade. |

## 5. Interface contract

### 5.1 Consumed — inbound `POST /intent`

Callers: the operator (via `curl`), or the Federation View's `POST /api/intent` (port 8500), which relays to the **chosen** provider's `intent_manager`.

```jsonc
{
  "intention": "je veux transcoder de la vidéo en continu",
  "intent_id": "demo-001"          // optional
}
```

### 5.2 Consumed — `GET {HUB_STATS_URL}` (RAG context)

Fields actually used: `active_slos`, `last_intention`, `service_vm`, `cycle`.

### 5.3 Consumed — `GET/POST {DATABASE_SERVICE_URL}/…/llm_history`

Reload and persistence of the intent history. Both are best-effort.

### 5.4 Produced — outbound `POST {HUB_INTENT_URL}`

```jsonc
{
  "intent_id":  "demo-001",
  "intention":  "je veux transcoder de la vidéo en continu",
  "slos": [
    { "metric": "cpu_usage", "operator": ">=", "threshold": 2.0, "unit": "cores",
      "weight": 0.6, "target": 2.2, "window": "5m",
      "budget_remaining": 100.0, "violations": 0, "confidence": 0.8,
      "is_primary": true }
  ],
  "timestamp": "2026-08-11T09:14:22.031Z",
  "timing": { "reception_ms": 1842.117, "llm_ms": 1839.402,
              "started_at": "2026-08-11T09:14:20.189Z" }
}
```

The Hub stamps this contract with an `intent_version`, switches to `enhanced` mode, and propagates it to its peers.

### 5.5 Responses returned to the caller

| Status | Condition | Body |
|---|---|---|
| `202 Accepted` | Intention translated and delivered | `{"status": "accepted", "slos_count": 2}` |
| `400 Bad Request` | Empty intention | `{"detail": "Intention field is required and cannot be empty"}` |
| `422 Unprocessable Entity` | No usable SLO could be extracted | `{"detail": "Could not extract valid SLOs from intention"}` |
| `502 Bad Gateway` | Hub rejected or unreachable | `{"detail": "Hub rejected the processed intent"}` |
| `500 Internal Server Error` | Unexpected internal error | `{"detail": "Internal server error during processing"}` |

## 6. Constraints

| # | Constraint |
|---|---|
| **C-1** | **The LLM is a hard dependency.** Unlike earlier versions, there is no regex or keyword extraction level. If both LAAS and Ollama are down, the enhanced mode is unavailable — the orchestrator falls back to autonomous mode, it does not guess. |
| **C-2** | **Three metrics only.** The vocabulary is closed by `METRICS_REGISTRY`. The offer's other dimensions (reliability, energy, cost, freshness) cannot be expressed today. |
| **C-3** | **Out of the cycle's timing budget.** An LLM call takes seconds; the cycle is 6 s. This is acceptable *because intent handling is asynchronous with respect to the cycle* — it happens once, on human action, not on every tick. But it means an intention is never applied within the cycle in which it was submitted. |
| **C-4** | **Mixed-language prompt.** The system prompt is written in French while the codebase and models are English. This is deliberate — the demonstrator's intentions are stated in French and the LLM performs better when prompt and input share a language — but it makes the prompt harder to reuse. |
| **C-5** | **Two semantic levels of threshold.** `cpu_usage`/`ram_usage` can mean either an *absolute need* (cores, GB) or a *load ratio* (%), with different validation branches. The unit is the only discriminator; an LLM emitting a bare number with no unit lands in the percentage branch. |
| **C-6** | **No authentication on `/intent`.** Any host on the network can redefine the orchestrator's business objective. |
| **C-7** | Python ≥ 3.10, FastAPI, `httpx`. Requires network access to LAAS (through a proxy if configured) or a local Ollama. |

## 7. Out of scope

- Discovering secondary SLOs — that is the `metrics_manager`'s MI analysis, applied after this service.
- Computing adaptive percentile thresholds — `metrics_manager`. Thresholds produced here are fixed business values and must never be recomputed statistically (hence `is_primary = True`).
- Detecting SLO violations — `decision_intelligence`.
- Propagating the intention to the peer provider — the **Hub** does that, on `/intent/propagate`.
- Versioning intentions to reject stale ones — the Hub, via `intent_version`.
- Translating the intention into provider-*specific* thresholds — **not implemented in `master`**; see §8.

## 8. Traceability against the xQoS internship objectives

| xQoS objective | Contribution of this service | Gap |
|---|---|---|
| O1 — Multi-provider intent-aware orchestrator | Full coverage of "parse and validate the intent structure". The intention is broadcast to the federation by the Hub afterwards. | The intent sent to every provider is **identical**; there is no per-provider adaptation. |
| O2 — Intent–QoS relationship engine | Partial: the mapping *intention → QoS metrics* is implemented, and it is genuinely semantic (service-type inference, not keyword matching). | **The core of O2 is missing in `master`**: no per-provider vocabulary, no qualitative concept resolution (`low_latency`), no feasibility status, no counter-proposal. A `provider_translator.py` implementing exactly this exists on the unmerged branch `claude/admiring-ellis-a33f4d`. |
| O3 — Visualization & explainability | Emits the reasoning trace the dashboard consumes: raw LLM thresholds, merge strategy and its source, final contract. | Per-provider interpretations cannot be displayed, since they are not produced. |
| O4 — Experimental validation | `llm_ms` and `reception_ms` feed the timing measurement campaign. | "Consistency of provider interpretations" is not measurable without O2. |
