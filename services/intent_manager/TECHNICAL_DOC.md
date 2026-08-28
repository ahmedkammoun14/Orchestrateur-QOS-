# Intent Manager — Technical Documentation

> **Document type:** Technical documentation (*how the service is built and how it works*).
> For *what it must do*, see [`SPEC.md`](SPEC.md).

| Field | Value |
|---|---|
| Package | `services.intent_manager` |
| Entry point | `services/intent_manager/app.py` |
| Framework | FastAPI + Uvicorn |
| Port | `config.INTENT_MANAGER_PORT` = `8002 + PORT_OFFSET` |
| Persistence | Indirect — intent history in Redis, via the database service |
| Lines of code | 758 (156 + 481 + 121) |

---

## 1. Role in the architecture

```text
   Operator / Federation View :8500
              │  POST /intent  {"intention": "..."}
              ▼
   ┌──────────────────────────────────────────────┐
   │            intent_manager :8002               │
   │                                               │
   │  ① RAGContextBuilder ──GET──► Hub /status     │
   │       active_slos, last_intention, cycle      │
   │                                               │
   │  ② LLMHandler._level1_llm                     │
   │       ├─ LAAS vLLM (Qwen3-27B)   ── primary   │
   │       └─ Ollama (qwen2.5)        ── fallback  │
   │            → {"merge_strategy", "slos"}       │
   │                                               │
   │  ③ _normalize_and_validate → List[SLO]        │
   │                                               │
   │  ④ SLOMerger.merge(active, new, strategy)     │
   │       REPLACE / ADDITIVE  → Σweight = 1       │
   │                                               │
   │  ⑤ persist history ──POST──► database :8006   │
   └───────────────────┬───────────────────────────┘
                       │  POST /intent + timing
                       ▼
              Hub Core :8000
              → intent_version, mode = "enhanced"
              → propagates to the peer provider
```

The service is **not** part of the 6-second orchestration cycle. It runs once, on human action, and its only lasting effect is to replace the Hub's `current_slos`. Every subsequent cycle then decides against that new contract.

## 2. Folder structure

```text
services/intent_manager/
├── app.py             # HTTP layer: route, timing, forwarding to the Hub
├── llm_handler.py     # LLM cascade, RAG context, normalisation & validation
├── slo_merger.py      # REPLACE / ADDITIVE / REFINE, conflicts, weight normalisation
├── requirements.txt
├── SPEC.md
└── TECHNICAL_DOC.md
```

Three responsibilities, three files. `slo_merger.py` is a **pure module**: no network, no I/O, fully unit-testable without mocks — which is why it is the best-covered part of the service.

## 3. Internal design

### 3.1 Logger propagation — a project-specific detail

`setup_logger()` in `app.py` does something the other services do not: it configures **its sub-loggers too**.

```python
for name in ("LLMHandler", "SLOMerger"):
    sub = logging.getLogger(name)
    sub.setLevel(logging.DEBUG); sub.addHandler(...); sub.propagate = False
```

`LLMHandler` and `SLOMerger` acquire their logger at module import with a bare `logging.getLogger(name)` and never configure it. Without this loop their output would be silently swallowed. In practice this means: **if you import `SLOMerger` outside the FastAPI app, you get no logs** — a common surprise when writing a standalone test script.

### 3.2 The RAG context

`RAGContextBuilder.build()` performs a single `GET` on the Hub's `/status`, with a 2-second timeout, and on any failure returns:

```python
{"active_slos": [], "percentiles": {}, "history": []}
```

Note the shape mismatch: the fallback advertises `percentiles` and `history`, which `_level1_llm` never reads, and omits `last_intention` and `service_vm`, which it does read (via `.get()` with defaults). Harmless, but the fallback is not a faithful model of the real response — see L-5.

Only four fields reach the prompt:

| Field | Why the LLM needs it |
|---|---|
| `active_slos` | To compute an `additive` value relative to the current contract |
| `last_intention` | **The discriminator for `merge_strategy`** — same use case or different one |
| `service_vm` | Situational awareness |
| `cycle` | Situational awareness |

`last_intention` is the one that matters. Without the previous sentence, *"encore plus bas"* is uninterpretable, and the model would default to `replace` and wipe a contract the user meant to tighten.

### 3.3 The prompt — reasoning by service type

The system prompt (`llm_handler.py:168-262`, ~95 lines) is the intellectual core of the service. Its central instruction is **not** "extract the numbers from the sentence" but:

> *Identify the TYPE OF SERVICE that would have to be deployed to fulfil the intention, then derive its needs.*

This is what lets *"préviens-moi tout de suite si le site tombe"* — a sentence containing no metric, no number, no technical term — produce `latency < 80 ms`. The prompt supports the inference with a **reference catalogue** of five service profiles:

| Profile | CPU | RAM |
|---|---|---|
| lightweight service / API / simple monitoring | 0.3–0.5 cores | 0.2–0.5 GB |
| continuous surveillance / detection (probe, IDS) | 0.3–0.8 cores | 0.3–0.8 GB |
| classic web / backend | 0.5–1.0 cores | 0.5–1.0 GB |
| streaming / video transcoding | 1.5–3.0 cores | 1.0–2.0 GB |
| ML inference / training | 1.0–4.0 cores | 2.0–8.0 GB |

and a latency scale: critical real-time alert ≈ 50–100 ms, user comfort ≈ 100–200 ms, tolerant background task ≈ 200–500 ms, no clue at all → 100 ms.

Two instructions exist purely to correct observed model failure modes:

- **Rule 7 — "never pad to 3 SLOs by reflex."** Left to itself the model produced latency + CPU + RAM for every intention, which diluted the weights and made every VM non-compliant on criteria the user never cared about. The three worked examples in the prompt deliberately produce **1, 2 and 3** SLOs respectively.
- **The `cpu_usage` / `ram_usage` semantics.** The prompt insists at length that these are an **absolute need** of the service in cores/GB, *not* a load percentage of the host. This is the trickiest concept in the whole contract and the one the model gets wrong most often.

`merge_strategy` (rule 6) is decided by comparing `last_intention` with the current one: same use case + relative reference → `additive`; different use case → `replace`; explicit complete values → `replace`; **when in doubt → `replace`**. Defaulting to `replace` is the safe choice: it can lose a constraint the user wanted kept, but it can never accumulate stale constraints that make every placement infeasible.

### 3.4 The two-level LLM cascade

| Level | Endpoint | Model | Timeout | Notes |
|---|---|---|---|---|
| 1 — primary | `LAAS_LLM_URL` (OpenAI-compatible `/chat/completions`) | `Qwen3/Qwen--Qwen3.6-27B-FP16` | 60 s | `temperature = 0.0`; "thinking" disabled **four different ways** |
| 2 — fallback | `{OLLAMA_URL}/api/generate` | `qwen2.5:latest` | 60 s | System and user prompts concatenated — Ollama's generate API takes a single string |

The four redundant thinking flags (`chat_template_kwargs.enable_thinking`, `extra_body`, `thinking.enabled`, `enable_thinking`) target different vLLM versions and chat templates. It looks like duplication; it is defensive compatibility. A reasoning preamble would prefix the JSON with prose and, given the greedy regex in §3.5, could corrupt the parse.

There is **no level 3**. Earlier versions had regex and keyword extractors; they were removed because a syntactic fallback produced plausible-looking but semantically wrong contracts — worse than an explicit failure. Today, both levels down means `422` and the operator is told.

### 3.5 Response parsing

```python
match = re.search(r'[\[{].*[\]}]', raw_content, re.DOTALL)
```

A **greedy** capture from the first `[` or `{` to the **last** `]` or `}`, which tolerates a model wrapping its answer in prose or markdown fences. Two shapes are then accepted:

| Parsed shape | Interpretation |
|---|---|
| `{"merge_strategy": …, "slos": [...]}` | Current format |
| `[ {...}, {...} ]` | Legacy format — strategy `None`, keyword fallback will apply |

Greediness is the right trade-off here (models often add a closing sentence), but it means a response containing **two** JSON blocks would be captured as one malformed span and rejected. Acceptable at temperature 0.

### 3.6 Normalisation and validation — `_normalize_and_validate`

This is the **guarantee boundary** of the service: non-determinism upstream, valid contract downstream.

```text
for each raw SLO returned by the LLM:
  1. metric alias  → canonical name  (cpu/ram/latence/ping/rtt/delay/…)
  2. unit          → Go→GB ; Mo/MB→GB WITH value ÷ 1024
  3. threshold     → branch by metric and unit:
        latency                      → clamp [5, 2000]  ms
        cpu/ram in cores|GB          → floor 0.1        (absolute need)
        cpu/ram otherwise            → clamp [1, 99]    (percentage)
        unknown metric, no threshold → DROP the SLO
  4. target        → given, else threshold × 0.9  (ceiling "<")
                                    × 1.1  (floor  ">=")
  5. defaults      → window "5m", weight 1/n, confidence 0.8,
                     budget_remaining 100.0, violations 0, is_primary True
  6. SLO(**r)      → on exception: warn and DROP this SLO only
```

Three points deserve attention:

- **The Mo→GB conversion is not cosmetic.** Without it, a probe specified at "256 Mo" would keep an unrecognised unit, fall into the percentage branch, and be clamped to 99 — turning a 0.25 GB requirement into a nonsensical one. The comment in the code says exactly this.
- **`raw_threshold = r.get("threshold")` is deliberately defensive.** It handles both an absent key *and* a key present with `null`. `r.get("threshold", default)` would not: it returns `None` for an explicit `null`, and the model does emit nulls.
- **The asymmetric target margin** (`×0.9` vs `×1.1`) encodes a real semantic: for a ceiling you aim slightly *below* the contract, for a floor slightly *above*. A single factor would be wrong for one of the two families.

### 3.7 The merge — `SLOMerger`

**Strategy selection**, in priority order:

| Priority | Condition | Mode | Source label |
|---|---|---|---|
| 1 | `llm_strategy ∈ {replace, additive}` | that one | `LLM` |
| 2 | `allow_refine` **and** text contains *plus strict / moins strict / affine / plus de / moins de* | `REFINE` | keyword (fallback) |
| 3 | text contains *aussi / et / ajoute / en plus* | `ADDITIVE` | keyword (fallback) |
| 4 | otherwise | `REPLACE` | keyword (fallback) |

`LLMHandler` always calls with `allow_refine=False`, so **priority 2 is dead code on the live path**. It is kept because `SLOMerger` is a standalone module whose contract still supports the levels that were removed. The log line prints the source, so a terminal showing `source : mot-clé (repli)` is a direct signal that the LLM omitted `merge_strategy` — worth noticing during a demo.

**Execution:**

- `REPLACE` → `result = new_slos`. The active contract disappears.
- `ADDITIVE` → dict keyed by metric, active first, then new ones overwriting. Metrics the new intention does not mention are preserved unchanged.
- `REFINE` → multiplies active thresholds and targets by `REFINE_STRICT` (0.85) or `REFINE_RELAX` (1.15) and decays confidence by 0.9. **Mutates the active SLO objects in place** — safe today only because the objects were just rebuilt from the RAG JSON.

**Conflict detection** (`_detect_conflicts`) is advisory only: it warns on an operator mismatch or a >50 % threshold gap between two SLOs sharing a metric, and changes nothing. In practice it almost never fires on the `ADDITIVE` path, since the merge dict already collapses duplicates by metric.

**Weight normalisation** (`_normalize_weights`) divides by the sum and rounds to 2 decimals. Rounding means `Σ weight` can land on 0.99 or 1.01; TOPSIS renormalises internally, so this is harmless — but do not assert exact equality to 1.0 in a test.

### 3.8 Timing instrumentation

`app.py` measures two nested durations with `time.perf_counter()`:

- `llm_ms` — the whole of `handler.handle()`: RAG + LLM + normalisation + merge + persistence.
- `reception_ms` — the same, measured again after the `422` check.

They are taken from the same start point with almost nothing between them, so **the two values are near-identical by construction**; `reception_ms − llm_ms` is a few microseconds, not the cost of a distinct phase. The naming suggests a decomposition that the code does not actually perform — see L-1.

## 4. API reference

### `POST /intent`

**Request** — `application/json`

| Field | Type | Required | Description |
|---|---|---|---|
| `intention` | string | **yes** | Free-form natural language. Rejected if empty or whitespace-only. |
| `intent_id` | string | no | Correlation id. Defaults to `gen-HHMMSS`. |

**Responses**

| Status | Body | Meaning |
|---|---|---|
| `202` | `{"status": "accepted", "slos_count": 2}` | Contract built and delivered to the Hub |
| `400` | `{"detail": "Intention field is required and cannot be empty"}` | Empty intention |
| `422` | `{"detail": "Could not extract valid SLOs from intention"}` | Both LLM levels failed, **or** the intention is out of domain |
| `502` | `{"detail": "Hub rejected the processed intent"}` / `"Could not connect to Hub"` | Downstream failure |
| `500` | `{"detail": "Internal server error during processing"}` | Unexpected error |

**Example**

```bash
curl -X POST http://localhost:8002/intent -H "Content-Type: application/json" -d '{"intention":"je veux transcoder de la video en continu","intent_id":"demo-001"}'
```

Expected terminal sequence: `📥 Intention reçue` → `🤖 Appel LAAS vLLM` → `🔍 Seuils LLM bruts` → `🔀 Stratégie de fusion SLOs : REPLACE (source : LLM)` → `🎯 SLOs validés` → `⏱ Réception intention` → `✅ Intent transmis au Hub`.

### `GET /health`

```json
{"service": "healthy", "ollama": "healthy"}
```

Probes `{OLLAMA_URL}/api/tags` with a 2 s timeout. Note the asymmetry: **it checks the fallback, not the primary.** `"ollama": "unreachable"` with LAAS working is a fully operational service; the converse — LAAS down, Ollama up — reports `healthy` while running degraded. Do not read this endpoint as a statement about extraction quality.

## 5. Configuration

| Variable | Default | Used for |
|---|---|---|
| `INTENT_MANAGER_PORT` | `8002` (+`PORT_OFFSET`) | Listening port |
| `LAAS_LLM_URL` | `https://pfcalcul.laas.fr/vllm/v1/chat/completions` | Primary LLM |
| `LAAS_MODEL` | `Qwen3/Qwen--Qwen3.6-27B-FP16` | Primary model |
| `LAAS_LLM_PROXY` | `""` | HTTPS proxy, e.g. `https://user:pass@proxy.laas.fr:443` |
| `OLLAMA_URL` | `http://localhost:11434` | Fallback LLM |
| `INTENT_MODEL` | `qwen2.5:latest` | Fallback model |
| `HUB_INTENT_URL` | derived | Contract delivery target |
| `HUB_STATS_URL` | derived | RAG context source |
| `RAG_TIMEOUT` | `2.0` s | Context fetch timeout |
| `HISTORY_SIZE` | `100` | Intent history depth |
| `LATENCY_MIN` / `LATENCY_MAX` | `5.0` / `2000.0` ms | Latency clamp (FR-18) |
| `USAGE_MIN` / `USAGE_MAX` | `1.0` / `99.0` % | Percentage clamp (FR-20) |
| `REFINE_STRICT` / `REFINE_RELAX` | `0.85` / `1.15` | REFINE factors (dead path) |
| `POST_TIMEOUT` | `5.0` s | Hub POST timeout |

`LAAS_LLM_PROXY` is the variable that most often blocks a fresh install: without it, from outside the LAAS network, level 1 times out after 60 s on every intention and the service runs permanently on Ollama.

## 6. Dependencies

**Internal** — `shared.config`, `shared.models.SLO`, `shared.logging_utils`.

**External** — `fastapi`, `uvicorn`, `httpx`. No LLM SDK: both providers are called over plain HTTP, which is what keeps the cascade swappable.

**Runtime**

| Dependency | Nature | On failure |
|---|---|---|
| LAAS vLLM | soft | Falls back to Ollama |
| Ollama | soft (hard if LAAS is also down) | `422` |
| Hub `/status` | soft | Empty RAG context |
| Hub `/intent` | **hard** | `502` |
| Database service | soft | History not reloaded / not persisted |

## 7. Data model and storage

The service owns no Redis key directly; it delegates to the database service:

| Route | Direction | Content |
|---|---|---|
| `GET {DATABASE_SERVICE_URL}/load/llm_history?size=100` | read, once at first use | `{"history": [{intention, slos}]}` |
| `POST {DATABASE_SERVICE_URL}/store/llm_history` | write, per intention | `{intention, slos}` |

An in-memory mirror (`self.history`) is capped at `HISTORY_SIZE` with FIFO eviction. `_ensure_history_loaded` sets its flag **before** awaiting, so two concurrent first requests cannot both trigger a reload.

Note that `self.history` is written and persisted but **never read back into a prompt** — the LLM receives `last_intention` from the Hub's `/status`, not from this list. The history is an audit trail today, not an input. See L-4.

## 8. Running it standalone

```bash
python -m services.intent_manager.app
```

Second provider:

```bash
PORT_OFFSET=100 python -m services.intent_manager.app
```

To test without LAAS (forces the Ollama path immediately instead of waiting 60 s):

```bash
LAAS_LLM_URL=http://127.0.0.1:1 python -m services.intent_manager.app
```

## 9. Logging and observability

| Marker | Level | Meaning |
|---|---|---|
| `📥 Intention reçue` | INFO | Boxed banner, id + truncated text |
| `🤖 Appel LAAS vLLM` | INFO | Level 1 attempted |
| `⚠️ LAAS indisponible — fallback vers Ollama local` | WARNING | Level 1 failed |
| `🔍 Seuils LLM bruts` | INFO | **Raw** model output, before clamping — compare with the next block to see what validation changed |
| `🔀 Stratégie de fusion SLOs : X (source : Y)` | INFO | `source : LLM` = nominal; `mot-clé (repli)` = the model omitted the field |
| `⚠️ Conflit d'opérateurs` / `Écart de seuil > 50%` | WARNING | Advisory only |
| `🎯 SLOs validés` | INFO | Final contract, `détection` (target) vs `contrat` (threshold) |
| `⏱ Réception intention` | INFO | Timing block |
| `✅ Intent transmis au Hub` | INFO | Nominal outcome |
| `ℹ️ tableau vide : intention hors du domaine réseau/QoS` | INFO | Deliberate refusal, not a bug |

Reading the pair `🔍 Seuils LLM bruts` → `🎯 SLOs validés` side by side is the fastest way to diagnose an odd contract: if they match, the model is responsible; if they differ, a clamp or a unit conversion fired.

## 10. Testing

| File | Covers |
|---|---|
| `tests/unit/test_llm_handler.py` | Cascade, response parsing, normalisation |
| `tests/unit/test_intent_manager_prompt.py` | Prompt structure and instructions |
| `tests/unit/test_slo_intent.py` | SLO model and intent payload |

```bash
pytest tests/unit/test_llm_handler.py tests/unit/test_slo_merger.py -v
```

> `tests/unit/test_slo_merger.py` exists on the branch `claude/admiring-ellis-a33f4d`, **not** on `master`. On `master`, `SLOMerger` has no dedicated test file despite being the most testable module of the service — see L-6.

## 11. Known limitations

| # | Limitation | Impact | Possible fix |
|---|---|---|---|
| **L-1** | `reception_ms` and `llm_ms` measure the same span from the same origin (§3.8). | The timing export suggests a two-phase decomposition that does not exist. | Start `llm_ms` at the LLM call itself, or drop one of the two fields. |
| **L-2** | `/health` probes Ollama, i.e. the fallback, and ignores LAAS. | A service silently running degraded reports `healthy`. | Probe both and return a per-level status. |
| **L-3** | The `REFINE` branch is unreachable on the live path (`allow_refine=False` always). | Dead code carrying a mutation-in-place hazard. | Remove it, or document it as public API of the standalone module. |
| **L-4** | `self.history` is persisted and reloaded but never fed to the LLM (§7). | Cost and complexity with no effect on extraction quality. | Either inject the last N intentions into the prompt, or reduce it to an audit write. |
| **L-5** | The RAG fallback dict does not match the real `/status` shape (§3.2). | A reader may believe `percentiles`/`history` are used. | Align the fallback on the four fields actually consumed. |
| **L-6** | No `test_slo_merger.py` on `master`, although the module is pure and trivially testable. | The four strategy branches and weight normalisation are unverified. | Port the file from the unmerged branch. |
| **L-7** | No authentication on `/intent`. | Any host on the network can redefine the business objective. | Acceptable on the demonstrator's private network. |
| **L-8** | A 60 s LAAS timeout blocks the request; there is no circuit breaker. | With LAAS unreachable, **every** intention costs 60 s before falling back. | Cache the failure for N minutes and skip level 1 during that window. |
| **L-9** | No per-provider translation of the contract (SPEC §8). | xQoS objective 2 is not met on `master`. | Integrate `provider_translator.py` from the unmerged branch. |
