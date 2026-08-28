# QoS Orchestrator — System Technical Documentation

> **Document type:** Technical documentation (*how the system is built and how it works*).
> For *what it must do*, see [`SPEC.md`](SPEC.md).
> For *the exact launch order*, see [`ETAPES_LANCEMENT_PROJET.md`](ETAPES_LANCEMENT_PROJET.md).
> For the narrative overview and the demo description, see [`README.md`](README.md).
> Per-component documentation lives in each folder as `TECHNICAL_DOC.md`.

| Field | Value |
|---|---|
| Language | Python 3.10+ |
| Framework | FastAPI + Uvicorn (13 processes per stack) |
| Storage | Redis (hot) + Excel `.xlsx` (cold) |
| Infrastructure | OpenStack + Kubernetes, 8 VMs |
| Total | ~9 000 lines across the orchestrator |

---

## 1. Architecture

### 1.1 Hub-and-Spoke, twice

```text
                     User (natural language)
                              │
                              ▼
                     ┌────────────────┐
                     │ Intent Manager │ :8002  ── LLM (LAAS → Ollama)
                     └────────┬───────┘
                              ▼
  PiCar ──► ┌────────────────┐   ┌─────────────────────────┐   ┌───────────────┐
  :8080     │ Latency Mgr    │──►│      HUB CORE  :8000    │──►│ Observability │ :8009
            │ :8001          │   │      _run_flow()        │   └───────────────┘
            └────────────────┘   └────┬───────────▲────────┘
                                      │           │
                                      │  ┌────────┴──────────┐
                                      │  │ Placement Arbiter │ :8011
                                      │  └───────────────────┘
                                      │  ┌───────────────────┐
                                      │  │ Provider Relay    │ :8010 ◄──► peer
                                      │  └───────────────────┘
         ┌──────────┬─────────────┬───┴──────────┬──────────────────┐
         ▼          ▼             ▼              ▼                  ▼
   ┌──────────┐┌──────────┐┌────────────┐┌──────────────┐   ┌──────────────┐
   │Collector ││ML Predict││Metrics Mgr ││Decision Intel│   │History Loader│
   │  :8005   ││  :8003   ││   :8004    ││    :8008     │   │    :8007     │
   └────┬─────┘└────┬─────┘└─────┬──────┘└──────┬───────┘   └──────┬───────┘
        │           │            │              │                  │
     VM agents   ML APIs         │        OpenStack Client         │
     :8200-8202  :5001-5003      │            :8024                │
        │                        │              │                  │
        └────────────► ┌─────────▼──────────────▼──────────────────┘
                       │  Database :8006  ──►  Redis DB 0/1
                       └────────────────────────────────────────────
```

**Two validated exceptions** to the pure Hub-and-Spoke pattern, both for performance:

1. **`collector → database`** writes directly. Routing one write per VM per cycle through the hub would saturate it.
2. **`history_loader → Redis`** reads directly. The metric series are read on the hottest path of the cycle; an HTTP hop would add latency for nothing, and reads need none of the atomicity guarantees writes do.

The rule the project actually enforces is **single *writer***, not single accessor. It holds: `database` is the only Redis writer.

### 1.2 Two providers, one federation

Each provider runs a **complete, independent stack** of 12 microservices. `PORT_OFFSET=100` shifts the second one; `REDIS_DB` isolates its state.

| | provider-1 | provider-2 |
|---|---|---|
| Hub | 8000 | 8100 |
| Services | 8001–8011 | 8101–8111 |
| Redis DB | 0 | 1 |
| VMs | edge1, edge1b, edge1c, cloud1 | edge2, edge2b, edge2c, cloud2 |
| Excel | `*_provider1.xlsx` | `*_provider2.xlsx` |

**Shared, not duplicated:** `openstack_client` (`:8024`, one Kubernetes cluster), `federation_view` (`:8500`), the three ML APIs (`:5001-5003`).

The partition is **transversal**: provider identity and edge/cloud tier are orthogonal. A provider owns VMs in both tiers, so it cannot be summarised as "the edge one".

## 2. The orchestration cycle

### 2.1 `_run_flow` — the 8 stages

Triggered by `POST /rtt` on the hub, guarded by a lock: a batch arriving mid-cycle is **dropped**, not queued.

```text
with prof.step("total"):

    ┌── PARALLEL (asyncio.gather) ─────────────────────────────┐
    │ Branch A                    │ Branch B                   │
    │  ① slos_mi                  │  ③ collect                 │
    │     metrics_manager         │     collector (cache read) │
    │     MI + SLO contract       │  ④ persist_metrics         │
    │  ② persist_slos → database  │     → database             │
    └──────────────────────────────────────────────────────────┘
                          ▼  (both results needed from here on)
    ⑥ load_histories    history_loader × N VMs   ← parallel
    ⑦ prediction        ml_predictor  × N VMs    ← parallel
    ⑤ check_violations  hub, ~0.4 ms
    ⑧ decide            decision_intelligence  →  mono | multi | federated
       ↓ if migrate
       store_decision → database
       migration      → openstack_client (kubectl)
       audit          → observability
```

Two levels of parallelism, deliberately distinguished in `shared/timing.py`:

- **Parallel branches** — two *different sequences* launched together. A and B have no mutual dependency; only the rest of the cycle needs both.
- **Parallel steps** — the *same step* across the 4 VMs. Duration = the slowest VM, **not the sum**.

Everything after the parallel block is strictly sequential, by data dependency: history → prediction → decision → action.

### 2.2 The three decision paths

`_step8_decide` routes to one of three implementations:

| Path | Condition | Behaviour |
|---|---|---|
| `_decide_mono_provider` | `MULTI_PROVIDER_ENABLED = false` | Original code, unchanged. TOPSIS on the local pool. |
| `_decide_multi_provider` | flag on, no Contract Net | Per-provider evaluation, handoff if needed |
| `_decide_federated` | flag on, Contract Net | Broadcast → bids → arbiter → local migration or award |

The flag defaults to `false` precisely so the federation extension can never alter the pre-existing behaviour by accident.

### 2.3 Timing budget

The effective cycle is **6.0 s** — `SEND_INTERVAL_S = 5 s` quantised by the browser's 2 s tick — of which ~4.7 s is orchestration. Every timing parameter is dimensioned against that figure.

The single largest optimisation in the system is the collector's background polling: it removes the 1.4–1.8 s VM round-trip from the critical path by turning `/collect` into a cache read.

## 3. The four decision mechanisms

These are the system's substance. Each lives in one place and is documented there in detail.

### 3.1 LLM extraction — [`intent_manager`](services/intent_manager/TECHNICAL_DOC.md)

Two-level cascade: LAAS vLLM (Qwen3-27B) → Ollama (Qwen2.5), temperature 0.

The prompt's central instruction is **not** "extract the numbers" but *"identify the type of service that would fulfil this intention, then derive its needs"*. That is what lets a sentence with no metric, no number and no technical term produce `latency < 80 ms`. The LLM also assigns each SLO's **weight** and decides the **merge strategy** (REPLACE / ADDITIVE) against the active contract.

`_normalize_and_validate` is the guarantee boundary: non-determinism upstream, a schema-valid, numerically bounded contract downstream.

### 3.2 MI discovery — [`metrics_manager`](services/metrics_manager/TECHNICAL_DOC.md)

Continuous Mutual Information between each metric and the binary violation signal, estimated by **Kozachenko-Leonenko k-NN** — no discretisation, non-linear dependencies detected, usable from ~15 points per class.

```
MI(X;Y) = H(X) − H(X|Y),  normalised by H(Y) → [0,1]
H(X) ≈ ψ(n) − ψ(k) + ln(2) + mean(ln r_k)
```

Above `MI_RELATIVE_THRESHOLD = 0.15`, the metric becomes a **secondary** SLO, weighted by its own MI score. Primary thresholds are never recomputed.

The five estimation steps are printed as tables. That trace *is* the statistical explainability layer.

### 3.3 TOPSIS — [`decision_intelligence`](services/decision_intelligence/TECHNICAL_DOC.md)

Four phases: decision matrix (on **predicted** values), min-max normalisation, weighting, ideal solutions and relative closeness.

Two properties specific to this implementation:

- **Capacity conversion.** `cpu_usage`/`ram_usage` become `capacity × (1 − usage/100)` — absolute availability — and flip to **benefit** criteria. Two VMs at 50 % CPU do not have the same real margin if one has 2 cores and the other 8. Capacity is declared by the VM itself, not tabulated in the orchestrator.
- **Tie guard.** A criterion whose relative spread is under 1 % is neutralised to 0.5 for every candidate. Without it, 0.1 ms of ML noise on ~100 ms normalises to exactly 0.0/1.0 — and an active score of exactly 0.0 silently disables the multiplicative anti-ping-pong hysteresis.

### 3.4 Gap Grade and Contract Net — [`hub/provider_arbitration.py`](hub/provider_arbitration.py) + [`placement_arbiter`](services/placement_arbiter/TECHNICAL_DOC.md)

**Why TOPSIS cannot cross providers.** It normalises min-max over its own pool, so 0.87 means "best *here*" — the best of any pool scores ≈ 1.0 regardless of absolute quality. Each provider therefore runs TOPSIS only on its own compliant VMs.

**The Gap Grade** is the only valid inter-provider metric: an augmented Tchebycheff scalarisation (Steuer & Choo, 1983) over the **primary** SLOs only.

```
δᵢ = (vᵢ − τᵢ)/τᵢ   (cost)      δᵢ = (τᵢ − vᵢ)/τᵢ   (benefit)     δᵢ floored at −1
G  = ( max(wᵢ·δᵢ) + ρ·Σ(wᵢ·δᵢ) ) / (1 + ρ)         ρ = 0.1
```

The `max` term makes it **pessimistic** — a provider cannot hide one bad metric behind several good ones — while the small `ρ·Σ` breaks ties. Measured against **absolute** thresholds, which is exactly what makes it comparable.

> **Two scores, opposite polarities.** TOPSIS closeness ∈ [0,1], higher is better, meaningful only *within* a pool. Gap Grade ∈ [−1,+∞), **lower is better**, meaningful *across* providers. They are never compared to one another.

**Contract Net** (Smith, 1980): the provider detecting the violation becomes initiator — a role, not a fixed coordinator. It builds its own bid **and** broadcasts in parallel, forwards every bid to its own arbiter, and applies the verdict — local migration, or award to the winning peer, which becomes `ACTIVE`.

## 4. Repository structure

```text
qos-orchestrator/
├── SPEC.md                    ← this system's requirements
├── TECHNICAL_DOC.md           ← this file
├── README.md                  ← narrative overview + demo
├── ETAPES_LANCEMENT_PROJET.md ← the operational runbook
│
├── hub/
│   ├── orchestrator_core.py       2785 l — _run_flow, the 8 stages, 10 routes
│   └── provider_arbitration.py    1017 l — Gap Grade, evaluate_provider, state machine
│
├── services/                  ← 12 microservices, each with SPEC + TECHNICAL_DOC
│   ├── latency_manager/       ·  intent_manager/     ·  ml_predictor/
│   ├── metrics_manager/       ·  collector/          ·  database/
│   ├── history_loader/        ·  decision_intelligence/
│   ├── observability/         ·  provider_relay/     ·  placement_arbiter/
│   └── federation_view/       ← shared, :8500
│
├── shared/                    ← the contract layer (SPEC + TECHNICAL_DOC)
│   ├── config.py  models.py  redis_keys.py  http_utils.py
│   ├── logging_utils.py  timing.py  timing_writer.py  excel_writer.py
│
├── infrastructure/            ← Picar/ and VMS/ (reference copies, deployed elsewhere)
├── tests/                     ← unit/ + integration/
├── scripts/                   ← PDF and comparison exports
├── data/                      ← Excel outputs (gitignored)
├── launch_provider.py         ← one command per provider
└── start_provider.ps1 / start_relay.ps1 / start_all_multi.ps1
```

## 5. Cross-cutting conventions

Recognising these makes any service readable in minutes.

### 5.1 The two-file split

```
app.py            → HTTP layer: FastAPI, routes, status codes
<name>_handler.py → domain layer: no FastAPI import, reusable behind another transport
```

Four services depart from it, each for a reason: `provider_relay` (pure transport — no domain layer to separate), `placement_arbiter` (`arbiter.py` is a pure module), `decision_intelligence` (three domain modules), `observability` (81 % of the file is the embedded HTML page).

### 5.2 Logging

Every service configures its own non-propagating logger with `PrettyFormatter`, guarded by `if not logger.handlers` so Uvicorn's reload cannot stack duplicates. A custom `SUCCESS` level sits at 25, between `INFO` and `WARNING`.

Two variants: `intent_manager` and `ml_predictor` also configure their sub-loggers explicitly; `decision_intelligence` uses hierarchical propagation instead.

### 5.3 Startup: fail-fast or fail-soft

| Behaviour | Services | Rationale |
|---|---|---|
| **Fail-fast** — refuses to start | `database`, `history_loader` | A store service without a store is actively harmful: it would accept writes into the void, or report "no history" when there is history |
| **Fail-soft** — starts anyway | all others | A proxy without its downstream is temporarily useless, not harmful |

### 5.4 Registry-driven, never hardcoded

Four loops iterate `METRICS_REGISTRY` rather than a payload or a literal list: `database.store_metrics`, `history_loader.load`, `metrics_manager.compute_mi_scores`, `topsis.select`. That is what makes "add a metric via one dictionary" verifiable rather than aspirational.

### 5.5 Timing instrumentation

`StepProfiler` is threaded through the hub, `metrics_manager` and `decision_intelligence`. Sub-service timings are returned in a `timings` key and `merge()`d into the hub's profiler, so a single Excel row carries the whole cycle including its nested phases.

## 6. Installation and launch

### 6.1 Prerequisites

```bash
python -m venv venv && source venv/Scripts/activate   # Git Bash on Windows
pip install -r requirements.txt
```

Also required: Redis, the 8 VM agents, the ML APIs (`Api-Model-Predict`), and — for enhanced mode — network access to LAAS or a local Ollama.

### 6.2 Recommended — one command per provider

```bash
python launch_provider.py provider-1
```

```bash
python launch_provider.py provider-2
```

Sets `PROVIDER_ID`, `PORT_OFFSET`, `REDIS_DB` and `MULTI_PROVIDER_ENABLED=true`, and starts the 12 services in dependency order.

### 6.3 Manual order

Redis → database → collector → the stateless services → **hub last** (it health-checks its dependencies at startup).

The full ordered runbook, including ML model training and the troubleshooting table for the failures actually encountered, is in [`ETAPES_LANCEMENT_PROJET.md`](ETAPES_LANCEMENT_PROJET.md).

### 6.4 Verification

```bash
curl -s http://localhost:8010/health
```

The relay's `/health` prints the federation routing table — two identical peer URLs means mono-process, two different ones means distributed. It is the fastest confirmation that the federation is wired as intended.

## 7. Port map

| Service | P1 | P2 | Shared |
|---|---|---|---|
| Hub Core | 8000 | 8100 | |
| Latency Manager | 8001 | 8101 | |
| Intent Manager | 8002 | 8102 | |
| ML Predictor | 8003 | 8103 | |
| Metrics Manager | 8004 | 8104 | |
| Collector | 8005 | 8105 | |
| Database | 8006 | 8106 | |
| History Loader | 8007 | 8107 | |
| Decision Intelligence | 8008 | 8108 | |
| Observability | 8009 | 8109 | |
| Provider Relay | 8010 | 8110 | |
| Placement Arbiter | 8011 | 8111 | |
| OpenStack Client | | | **8024** |
| Federation View | | | **8500** |
| ML APIs (latency/cpu/ram) | | | **5001-5003** |
| VM agents (`/metrics`) | | | 8200-8202 |
| PiCar bridge | | | 8080 |

## 8. Reading the terminals

Twelve terminals per provider. Four carry most of the diagnostic value:

| Terminal | Line to watch | What it tells you |
|---|---|---|
| `collector` | `✅ edge1 — cpu_usage=41.2 \| fiabilité : 0.98` | Infrastructure health. Reliability below 0.9 means recent failures even if the VM answers now. |
| `history_loader` | `History loaded — sizes={'latency': 50, …}` | Whether the ML cascade can use level 1. Values well below 50 mean warm-up. |
| `ml_predictor` | `✅ Niveau 1 (sequence)` / `❌ Niveaux 1 et 2 épuisés` | Which cascade level produced the decision's input. |
| `decision_intelligence` | `🔎 Candidats TOPSIS … \| 🏆 TOPSIS classement` | Whether the pool was compliant, and how close the top two were. |

In federated mode, two more:

| Terminal | Line | Meaning |
|---|---|---|
| `provider_relay` | `📬 N bid(s), M erreur(s)` | `0 bid, 1 erreur` every cycle = federation nominally on, effectively degraded to one provider |
| `placement_arbiter` | `⚖️ chemin B \| gagnant : …` | Recurring `C` = contract unsatisfiable federation-wide; recurring `D` = ML chain down |

Cross-terminal correlation is by **cycle number**, printed by the hub, `metrics_manager` and `decision_intelligence`.

## 9. Data and exports

### 9.1 Redis — hot path

| Key | Type | Bound | Writer | Reader |
|---|---|---|---|---|
| `metrics:{vm}:{metric}` | LIST | 50 | database | history_loader |
| `metrics:{vm}:history` | LIST | 50 | database | offline |
| `slos:active` | STRING | 1 | database | offline |
| `decisions:recent` | LIST | 50 | database | offline |
| `llm:history` | LIST | 100 | database | intent_manager |

No TTL anywhere — retention is by count (`LTRIM`), not by time. `redis-cli TTL` returning `-1` on every key is correct, not a misconfiguration.

### 9.2 Excel — cold path

| File | Content |
|---|---|
| `data/qos_history*.xlsx` | Métriques · Décisions · SLOs · Intentions_LLM |
| `data/timings_autonomous*.xlsx` | One row per cycle, 3-level headers |
| `data/timings_enhanced*.xlsx` | One row per intention |

The **Légende** sheet of the timing workbooks is generated from `shared/timing.py`'s pipeline constants, so the reader knows which durations are sums and which are maxima. Without it, adding three parallel steps yields a cycle time that never existed.

## 10. Testing

```bash
pytest tests/ -v
```

| Area | Coverage |
|---|---|
| **Well covered** | Federation (7 files), TOPSIS, violation detector, MI scoring, relay (4 files), arbiter, observability (3 files) |
| **Not covered** | `latency_manager`, `history_loader`, `collector`, `ml_predictor`, `metrics_manager` (partial), `http_utils`, `StepProfiler`, the Excel writers |

Two gaps stand out because their failure mode is *silent*:

- **`history_loader`'s chronological reversal.** Reverse it and every model still returns a plausible number, computed on a time-reversed series, with no error anywhere. A three-line test makes it impossible.
- **`ml_predictor`'s `_denormalize`.** A scale regression produces predictions 100× off, invisible in the orchestrator's logs. Pure function, trivially testable.

## 11. System-level limitations

Component-level limitations are in each `TECHNICAL_DOC.md` §Known limitations. These are the ones no single component owns.

| # | Limitation | Impact | Direction |
|---|---|---|---|
| **L-1** | **No per-provider QoS interpretation** (SPEC §8, O2). | The offer's central objective is unmet on `master`. | Rebase `provider_translator.py` and `provider_feasibility.py` from the unmerged branch `claude/admiring-ellis-a33f4d` onto the Contract Net architecture. Highest-value work available. |
| **L-2** | **`reliability` is measured, transported, and never used.** The collector computes it, the hub forwards it, `topsis.select` receives it and ignores it — while its docstring lists it as a criterion. | The declared criteria set overstates the implemented one. | Wire it in with a weight, or remove it and correct the docstring. |
| **L-3** | **`_ABSOLUTE_UNITS` is triplicated** across `metrics_manager`, `decision_intelligence` and `provider_arbitration`, none of them `shared`. | A divergence resurrects the "0.5 cores → 1.0" defect. | Single declaration in `shared/config.py`. |
| **L-4** | **`validate_provider_registry()` is written, tested, never called.** | An inconsistent registry is found by symptoms, not by a check. | One line at the end of `config.py`. |
| **L-5** | **No authentication anywhere** (SPEC C-8). `POST /reset` on the dashboard is destructive and unauthenticated. | Any host can redefine or wipe the business objective. | A confirmation dialog at minimum. |
| **L-6** | **Mono-process federation by default** (SPEC C-6). | The real network path between two relays is never exercised. | A two-machine test before claiming distributed operation. |
| **L-7** | **Three metrics only** (SPEC C-2). | Reliability, energy, cost and freshness cannot be expressed. | `reliability` is already collected — the cheapest dimension to promote. |
| **L-8** | **Scaling, prioritisation, resource allocation not implemented** (SPEC §7). | Three of the five functional capabilities the offer lists. | Out of scope for the PFE; worth stating explicitly rather than leaving implicit. |

## 12. Component documentation index

| Component | Specification | Technical documentation |
|---|---|---|
| Latency Manager | [SPEC](services/latency_manager/SPEC.md) | [DOC](services/latency_manager/TECHNICAL_DOC.md) |
| Intent Manager | [SPEC](services/intent_manager/SPEC.md) | [DOC](services/intent_manager/TECHNICAL_DOC.md) |
| ML Predictor | [SPEC](services/ml_predictor/SPEC.md) | [DOC](services/ml_predictor/TECHNICAL_DOC.md) |
| Metrics Manager | [SPEC](services/metrics_manager/SPEC.md) | [DOC](services/metrics_manager/TECHNICAL_DOC.md) |
| Collector | [SPEC](services/collector/SPEC.md) | [DOC](services/collector/TECHNICAL_DOC.md) |
| Database | [SPEC](services/database/SPEC.md) | [DOC](services/database/TECHNICAL_DOC.md) |
| History Loader | [SPEC](services/history_loader/SPEC.md) | [DOC](services/history_loader/TECHNICAL_DOC.md) |
| Decision Intelligence | [SPEC](services/decision_intelligence/SPEC.md) | [DOC](services/decision_intelligence/TECHNICAL_DOC.md) |
| Observability | [SPEC](services/observability/SPEC.md) | [DOC](services/observability/TECHNICAL_DOC.md) |
| Provider Relay | [SPEC](services/provider_relay/SPEC.md) | [DOC](services/provider_relay/TECHNICAL_DOC.md) |
| Placement Arbiter | [SPEC](services/placement_arbiter/SPEC.md) | [DOC](services/placement_arbiter/TECHNICAL_DOC.md) |
| Shared library | [SPEC](shared/SPEC.md) | [DOC](shared/TECHNICAL_DOC.md) |
| Hub Core | [SPEC](hub/SPEC.md) | [DOC](hub/TECHNICAL_DOC.md) |
| **Federation View** | *pending* | *pending* |
