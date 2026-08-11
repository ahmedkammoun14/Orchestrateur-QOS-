# QoS Orchestrator

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-Framework-green?logo=fastapi)
![Redis](https://img.shields.io/badge/Redis-Storage-red?logo=redis)
![OpenStack](https://img.shields.io/badge/Infrastructure-OpenStack-gray?logo=openstack)
![LLM](https://img.shields.io/badge/LLM-Qwen3--27B%20%7C%20Qwen2.5-purple?logo=openai)
![ML](https://img.shields.io/badge/ML-ESN%20%7C%20LSTM%20%7C%20GRU%20%7C%20RNN-orange)

An autonomous QoS orchestration system for microservices, enabling adaptive Quality of Service management in real cloud/edge environments (OpenStack + Kubernetes), demonstrated with a position-based PiCar-X robotic vehicle.

## Table of Contents
- [Overview](#overview)
- [What This Repository Contains — and What It Does Not](#what-this-repository-contains--and-what-it-does-not)
- [Academic Context](#academic-context)
- [Architecture](#architecture)
- [Multi-Provider Federation](#multi-provider-federation)
- [Key Technical Features](#key-technical-features)
- [PiCar-X Demo — Position-Based Latency](#picar-x-demo--position-based-latency)
- [Tech Stack](#tech-stack)
- [Configuration](#configuration)
- [Real Infrastructure](#real-infrastructure)
- [Installation](#installation)
- [Starting the Services](#starting-the-services)
- [Service Ports](#service-ports)
- [API Reference](#api-reference)
- [Tests](#tests)
- [Project Structure](#project-structure)
- [Roadmap](#roadmap)
- [Authors](#authors)

---

## Overview

In modern distributed environments, maintaining consistent performance is a challenge. **QoS Orchestrator** solves this by acting as a centralized brain that:

- Interprets user intentions in natural language via an LLM (Qwen3-27B on LAAS-CNRS, Qwen2.5 as local fallback).
- Dynamically discovers critical metrics via Mutual Information (MI k-NN, Kozachenko-Leonenko estimator).
- Predicts future violations using ML models (ESN, LSTM, GRU, RNN) via a 3-level prediction cascade over a configurable horizon.
- Makes optimal migration decisions toward the best VM (Edge or Cloud) using the multicriteria TOPSIS algorithm.
- Arbitrates between **two independent infrastructure providers** — 8 VMs, 4 per provider, each spanning both tiers — through a **Contract Net Protocol** negotiation over HTTP (see [Multi-Provider Federation](#multi-provider-federation)).

Each provider runs its **own complete orchestrator stack** (12 microservices), the second one offset by `PORT_OFFSET=100`. Neither is a master: whichever hosts the service is `ACTIVE`, the other is `STANDBY`, and the role follows the service across migrations.

The system operates in two modes:
- **Autonomous** — fixed business objective (latency < 28 ms, `shared/config.py` `METRICS_REGISTRY["latency"]["default_threshold"]`), secondary SLOs discovered automatically by MI.
- **Enhanced** — SLOs injected by the user via natural language (LLM also assigns each SLO's **weight** and decides the **merge_strategy** — REPLACE or ADDITIVE — against active SLOs), enriched by MI-driven secondary SLOs.

> **Start here:** [`ETAPES_LANCEMENT_PROJET.md`](ETAPES_LANCEMENT_PROJET.md) is the operational runbook — the exact order in which to train the ML models, start the infrastructure, launch the two providers, and run the demo, with the troubleshooting table for the failures we actually hit.

---

## What This Repository Contains — and What It Does Not

This repository holds the **orchestrator** and nothing else: the twelve microservices, the hub, the shared configuration, and the test suite. That is the contribution.

Everything that runs *outside* the orchestrator is deliberately **not versioned here**:

| Component | Where it actually lives | Why it is not in the repo |
|---|---|---|
| Per-VM agents (`/ping`, `/metrics`) | the 8 OpenStack VMs | one deployed copy per VM, edited in place; a versioned copy diverges silently from what runs |
| PiCar bridge + HTML simulator | Raspberry Pi, `~/Projet_PFE/multiProvider/` | same reason — the PiCar is the source of truth |
| `openstack_client.py` (kubectl) | the OpenStack master `194.199.113.8` | holds cluster-specific `NODE_VM_MAP` and YAML paths |
| ML prediction APIs (`:5001/:5002/:5003`) | separate `Api-Model-Predict` project | own training datasets (192 000 rows), own lifecycle |
| Training datasets, logs, measurement exports | local disk | volume, and they are session artefacts, not source |

The consequence is explicit: **cloning this repository reproduces the orchestrator, not the demo.** Reproducing the demo requires the physical infrastructure described in [Real Infrastructure](#real-infrastructure) and the deployment steps in [`ETAPES_LANCEMENT_PROJET.md`](ETAPES_LANCEMENT_PROJET.md).

---

## Academic Context

This project was developed as a **Final Year Project (PFE)** at **ENIS Sfax**, in partnership with the **LAAS-CNRS laboratory in Toulouse**.

- **Team:** Ahmed Kammoun & Mustapha
- **Supervision:** LAAS-CNRS / ENIS Sfax

---

## Architecture

The system follows a **Hub-and-Spoke** pattern: the `Hub` (Orchestrator Core) centralizes the control logic and delegates specific tasks to independent microservices.

```text
           [ Intent Manager ] <─── User (natural language)
                  │
                  ▼
[ PiCar-X ] ──► [ Latency Manager ] ──► [ HUB (Core) ] ──► [ Observability ]
                                              │  ▲
                                              │  ├── [ Placement Arbiter ]  (:8011)
                                              │  │      elects the winning bid
                                              │  └── [ Provider Relay ] ◄── peer orchestrator
                                              │        (:8010, Contract Net)
         ┌──────────┬──────────────┬──────────┴──────────────────┐
         │          │              │                              │
   [ Collector ] [ ML Predictor ] [ Metrics Manager ] [ Decision Intelligence ]
         │          │              │                              │
         ▼          ▼              ▼                              ▼
   [ VM Agents ] [ ML APIs ]  [ History Loader ]      [ OpenStack Client ]
         │                         │                              │
         └──────────► [ Database (Redis) ] <────────────────────--┘
                                                                 └─► [ Kubectl ]
```

### Validated Architectural Exceptions

Two exceptions to the pure Hub-and-Spoke model have been validated for performance reasons:

1. **Collector → Database**: the collector writes metrics directly to the database to avoid saturating the Hub during high-frequency cycles.
2. **OpenStack Client**: called directly by the Hub for migrations, without going through an intermediate spoke.

---

## Multi-Provider Federation

The orchestrator arbitrates between **two independent infrastructure providers**. The partition is **transversal**: each provider owns VMs in *every* tier, so provider identity and edge/cloud tier are orthogonal dimensions.

| Provider | Edge VMs | Cloud VM | Kubernetes PoP label |
|---|---|---|---|
| `provider-1` | edge1, edge1b, edge1c | cloud1 | `space_1` |
| `provider-2` | edge2, edge2b, edge2c | cloud2 | `space_2` |

Declared in `shared/config.py` → `PROVIDER_REGISTRY` (single source of truth; `PROVIDER_OF_VM` is derived from it).

### Two processes, one federation

Each provider runs a **complete, independent stack** of 12 microservices. `PORT_OFFSET` shifts the second one by +100 (hub 8000 → 8100, relay 8010 → 8110, …), and `REDIS_DB` isolates their state (DB 0 / DB 1). Two components are **shared**, not duplicated: the OpenStack client (`:8024`, it drives a single Kubernetes cluster) and the read-only Federation View (`:8500`).

There is no master. The provider hosting the service is `ACTIVE` and drives the decision cycle; the other is `STANDBY` and only answers bids. The role **follows the service** — an inter-provider migration swaps the two.

> **kubectl granularity.** The 8 VMs are simulated on 4 Kubernetes nodes: `edge1`, `edge1b` and `edge1c` share one physical node. `kubectl` therefore resolves to the *node* and always reports the canonical VM of that node. When our own `service_vm` sits on the same node as kubectl's answer, our value is the finer one and is kept; otherwise kubectl wins. See the placement-group table in `shared/config.py` and `NODE_VM_MAP` in `openstack_client.py`.

### Why TOPSIS scores cannot be compared across providers

TOPSIS normalises each criterion with **min-max over its own candidate pool**. A score of 0.87 therefore means "best *within this pool*" — it carries no absolute meaning. Comparing a score computed on `{edge1, cloud1}` against one computed on `{edge2, cloud2}` is mathematically unsound.

Each provider consequently runs TOPSIS **only on its own compliant VMs**, never on a mixed pool — `candidates_for_provider()` enforces the isolation.

### The Gap Grade — the only valid inter-provider metric

Cross-provider comparison uses an **augmented Tchebycheff scalarisation** (Steuer & Choo, 1983), computed over the **primary SLOs only**:

```
δᵢ = (vᵢ − τᵢ) / τᵢ        cost criteria    (lower is better)
δᵢ = (τᵢ − vᵢ) / τᵢ        benefit criteria
δᵢ floored at −1

G = ( max(wᵢ·δᵢ) + ρ · Σ(wᵢ·δᵢ) ) / (1 + ρ)         ρ = GAP_GRADE_RHO = 0.1
```

Weights are renormalised so `Σwᵢ = 1`. A negative `G` means every primary SLO is met with margin; a positive `G` quantifies the worst relative breach. The `max` term makes the metric **pessimistic** — a provider cannot hide one bad metric behind several good ones — while the small `ρ·Σ` term breaks ties between candidates sharing the same worst criterion.

Unlike a TOPSIS closeness, `G` is measured against **absolute thresholds** rather than a candidate pool, which is precisely what makes it comparable across providers.

> **Two scores, opposite polarities.** TOPSIS closeness ∈ [0,1], **higher is better**, meaningful only *within* one provider's pool. Gap Grade ∈ [−1,+∞), **lower is better**, meaningful *across* providers. The two are never compared against one another.

### Contract Net Protocol

Federated placement follows the **Contract Net Protocol** (Smith, *IEEE Trans. Computers*, 1980; later FIPA-standardised). The provider that detects the violation becomes the **initiator** — this is a role, not a fixed coordinator: any provider can play it.

1. The initiator detects a violation on a **primary** SLO (the gate — secondaries never trigger a migration).
2. It builds its own bid locally **and** broadcasts the SLO contract to its peers — **in parallel** (`asyncio.gather`), since the two are independent.
3. Each peer evaluates *its own* VMs, runs TOPSIS on its compliant pool, and returns a single bid: champion VM + Gap Grade.
4. The initiator forwards every bid to **its own** Placement Arbiter (`:8011`).
5. The arbiter elects the winner, dead-band included, and returns the decision.
6. The initiator applies it — local migration, or **award** to the winning peer, which then becomes `ACTIVE`.

`SLO_ENFORCEMENT = "hard"`: a non-compliant placement is **never** elected. If no VM anywhere satisfies the primaries, the outcome is `INFEASIBLE` and the service stays where it is.

The **dead-band** (`ARBITER_DEADBAND = 0.05`) protects the incumbent: a challenger must beat it by more than 0.05. The band is **absolute, not relative** — the Gap Grade is already normalised by the threshold, so 0.05 reads directly as *"must win by more than 5 % of the threshold"* (1.4 ms on a 28 ms SLO). It is an engineering parameter, not a measured one.

Note the deliberate asymmetry: the dead-band guards **provider** changes; a VM change *within* the winning provider is guarded only by the temporal `MIGRATION_COOLDOWN_S`.

### Shared SLO contract

A client does not know which provider hosts the service, so an intention may legitimately land on a `STANDBY`. Two mechanisms keep the contract consistent across the federation:

- **Propagation** — the hub receiving an intention forwards it to its peers (`/intent/propagate` → `/inbound/intent`), with an anti-loop guard on the inbound path.
- **Adoption** — the broadcast carries the initiator's *complete* contract: primaries **and** secondaries, each with its `weight` and its `is_primary` flag. A `STANDBY` records it as its own current contract instead of using it once and discarding it — so the TOPSIS compliance filter keeps working identically on both sides.

A `STANDBY` no longer recomputes its SLOs by MI: only the `ACTIVE` provider hosts the service, hence only it holds an exploitable history. The direct `/intent` path and the award path are both guarded by `intent_version` — a late intention can never overwrite a more recent one.

### Transport

`services/provider_relay/` (port 8010 / 8110) is a **pure transport gateway** — it carries messages between orchestrators and holds no decision logic whatsoever. Each outbound route has an `/inbound/…` counterpart on the peer, and the inbound side carries the anti-loop guard.

| Outbound | Inbound on the peer | Carries |
|---|---|---|
| `POST /broadcast` | `POST /inbound/evaluate` | SLO contract → returns the peer's bid |
| `POST /award` | `POST /inbound/award` | placement decision → the peer becomes `ACTIVE` |
| `POST /intent/propagate` | `POST /inbound/intent` | user intention + `intent_version` |
| `POST /handoff` | `POST /inbound` | legacy direct handoff (pre-Contract-Net) |

HTTP/JSON serialisation also provides intent isolation: one provider cannot mutate the other's SLO objects by reference.

`ORCHESTRATOR_URL_PROVIDER_*` and `RELAY_URL_PROVIDER_*` are the only places in the codebase that know the topology. Scaling to N real orchestrators means changing those URLs — and nothing else.

### Feature flag

`MULTI_PROVIDER_ENABLED` (default **`false`**) gates the entire state machine. When off, the cycle behaves exactly as before the extension. `launch_provider.py` sets it to `true`.

---

## Key Technical Features

- **End-to-end QoS pipeline**: real flow from the PiCar-X (Raspberry Pi) → `latency_manager` → `hub` → automatic decision across 8 OpenStack VMs split over two providers.
- **Position-based simulated latency**: the closer the vehicle, the lower the latency. The deployed model is **clamped per tier** — see [PiCar-X Demo](#picar-x-demo--position-based-latency) — bounding latency between a floor `B` and a ceiling `A` distinct for edge and cloud.
- **7-step TOPSIS**: multicriteria VM selection (Min-Max normalization, weighting, Euclidean distances to ideal solutions A⁺ and A⁻). Criteria: SLO metrics (latency, CPU, RAM). Uses **ML predictions** as input values — not raw measurements — to anticipate future state.
- **Active VM as TOPSIS candidate**: the currently active VM is always included in the decision pool. If TOPSIS selects it despite a violation → STAY (it remains the best option). This prevents unnecessary migrations when the current VM is still the least-bad choice.
- **Transversal multi-provider arbitration**: two providers, each spanning both tiers, negotiating over HTTP with an absolute dead-band. TOPSIS stays confined to a single provider's compliant VMs; the normalised violation score is the only cross-provider comparison. See [Multi-Provider Federation](#multi-provider-federation).
- **Compliance as a strict gate**: a VM satisfying *all* SLOs always outranks a non-compliant one, however small the latter's violation. The violation score only breaks ties *among non-compliant VMs* — it is never weighed against a compliant one.
- **MI k-NN (Kozachenko-Leonenko)**: continuous Mutual Information estimator — replaces the old 2×2 contingency table. No discretization, detects non-linear dependencies, robust from ~15 points per class. Formula: `MI(X;Y) = H(X) − H(X|Y)`, normalized by `H(Y)` → score in [0, 1].
- **3-level ML prediction cascade**: Level 1 — `POST /predict_sequence` (full window, horizon 7); Level 2 — `GET /predict?input_data=X` (single point); Level 3 — `last_value_fallback`. Each level activates only if the previous fails. Level 1 is **skipped silently** while the history is shorter than the model's `window_size`, which is what makes the warm-up period observable. Measured over a 10-minute two-provider session, *after* warm-up: **77.5 % Level 1**, 3 % Level 2, 19 % Level 3 — the residual falls concern latency only and trace back to contention on the single-worker latency API, not to a logic fault.
- **ESN (Echo State Network)**: reservoir computing model with a recursive multi-step strategy for horizon > 1. Trained via `/main` endpoint on historical datasets. Fixed for correct 2D input shape (flatten before recursive loop).
- **YAML_PER_VM mapping**: each VM maps to the correct Kubernetes YAML file matching its PoP label (`space_1` → edge1/cloud1, `space_2` → edge2/cloud2). Ensures pods land on the correct physical node after migration.
- **5-step MI visualization**: the `metrics_manager` terminal displays a detailed step-by-step k-NN pipeline (H(X), H(X|Y=1), H(X|Y=0), weighted average, final score) with ASCII tables for each metric at every cycle.
- **Cycle traceability**: every cycle number is passed from the Hub to both `metrics_manager` and `decision_intelligence`. Both terminals display `[Cycle #N]` headers so MI scores and TOPSIS decisions from the same cycle are visibly linked.
- **Adaptive thresholds**: automatic percentile (P70/P75/P85) based on coefficient of variation — absorbs signal volatility without manual reconfiguration.
- **ML-driven proactive detection**: for every metric (primary and secondary), the decision is made on the **prediction** — `"proactive"` if a predicted horizon value breaches the threshold, `"none"` otherwise (ignores transient measured spikes). `"reactive"` (raw measured value) is only used as a safety net when no ML prediction is available (ML API down).
- **LLM-driven SLO weight & merge strategy**: in Enhanced mode, the LLM assigns each extracted SLO's `weight` (used directly, renormalized, in the TOPSIS weighting phase — not a fixed 1.0) and decides whether new SLOs should `REPLACE` or be `ADDITIVE` to the active ones, based on the intent's meaning (keyword detection is only a fallback for non-LLM levels).
- **Observability dashboard**: real-time SSE dashboard at `http://localhost:8009` — VM cards with metric bars and predictions, latency history chart, SLO weight chart, and full audit log with cycle number, breach type, TOPSIS score, and migration trace.
- **Audit trail**: every decision is posted to the observability service with full context (cycle, breach_type, SLOs, MI scores, TOPSIS score) and broadcast to all SSE subscribers.
- **2-level LLM cascade**: SLO extraction from natural language via LAAS vLLM (Qwen3-27B, primary) → local Ollama (Qwen2.5, fallback).
- **Live PiCar simulator**: HTML canvas at `http://<picar-ip>:8080/` with vehicle trajectory, per-VM latency display, active VM badge, and cyan highlight.
- **Extensible METRICS_REGISTRY**: add a new metric via a single dictionary in `shared/config.py`, without modifying service code.
- **Anti-thrashing**: configurable post-migration cooldown (`MIGRATION_COOLDOWN_S`, default 5 s for demo) blocking any new migration during stabilization.

---

## PiCar-X Demo — Position-Based Latency

The demo replaces traditional network RTT measurement with **distance-based simulated latency**. As the PiCar-X vehicle moves along its track, latency to each VM is computed from the Euclidean distance on a 2D map.

**Deployed model (clamped, per tier).** Latency is a linear interpolation between a floor `B` (vehicle on top of the VM) and a ceiling `A` (vehicle beyond `D_MAX`), saturating at both ends:

```
L(d) = ((clamp(d, D_MIN, D_MAX) − D_MIN) / (D_MAX − D_MIN)) × (A − B) + B

D_MIN = 3   D_MAX = 80
edge  : B = 5   A = 150      →   5–150 ms
cloud : B = 50  A = 230      →  50–230 ms
```

Saturation is what makes the demo controllable: each tier has a *guaranteed* range, so the edge tier is structurally favoured (its floor is 10× lower) without ever letting a distant VM report an unbounded latency.

The distance at which a VM still meets a latency SLO of threshold `T` — its **conformity radius** — follows directly:

```
D_slo(T) = D_MIN + (T − B) / (A − B) × (D_MAX − D_MIN)      valid iff  B < T < A
```

Inverting the relation gives the **conformity radius** used to size the demo. At the operating threshold `θ = 28 ms`, an edge VM stays compliant within **19.8 cm** and a cloud VM is compliant nowhere (`θ < B_cloud = 50`) — which is precisely what forces the service onto the edge tier and makes the federation necessary rather than decorative.

### VM Positions on the Map

The 8 VMs were repositioned by k-means over the recorded trajectory so that every provider covers a distinct arc of the lap. Coordinates are in centimetres, in the simulator's frame:

| VM | x | y | Tier | Provider |
|---|---|---|---|---|
| edge1 | +3 | −9 | Edge | provider-1 |
| edge1b | +34 | +19 | Edge | provider-1 |
| edge1c | −6 | +51 | Edge | provider-1 |
| cloud1 | −4 | +34 | Cloud | provider-1 |
| edge2 | +31 | −8 | Edge | provider-2 |
| edge2b | +4 | +23 | Edge | provider-2 |
| edge2c | −23 | +30 | Edge | provider-2 |
| cloud2 | +18 | +4 | Cloud | provider-2 |

At `θ = 28 ms`, provider-1 alone covers **45.6 %** of the lap and provider-2 **47.1 %**; together they reach **92.7 %**. Neither provider can hold the service for a full lap on its own — the inter-provider handover is a physical necessity of the layout, not a staged event.

### Components — deployed, not versioned

**`picar_bridge_QoS1.py`** — Flask server on the Raspberry Pi (port 8080):

| Route | Method | Description |
|---|---|---|
| `/` | GET | Serves the HTML simulator |
| `/Trajectoire.jpg` | GET | Serves the track image |
| `/tick` | POST | Receives `{x, y}`, pings the 8 VMs in parallel, forwards RTT to **both** latency managers |
| `/active-vm-push` | POST | Receives the active hub's state at the end of each cycle |
| `/vm-status` | GET | Returns the precise active VM — push, then hub polling, then last known value |

The active-VM resolution is three-tiered on purpose. The push is preferred because the hub emits it at the exact moment its cycle closes, so `service_vm`, `cycle` and `last_decision` are mutually consistent; polling is the fallback when pushes are lost (they are fire-and-forget, never retried). The former kubectl fallback was **removed**: `openstack_client` resolves to the Kubernetes *node* and would report `edge1` while the service actually ran on `edge1c`.

**Per-VM agent** — one process per simulated VM:

| Port | Description |
|---|---|
| 5001–5003 | `POST /ping {x,y}` → computes distance, sleeps `latency_ms/1000`, returns latency |
| 8200–8202 | `GET /metrics` → CPU/RAM via psutil |

Three VMs share one physical host (`edge1`, `edge1b`, `edge1c` on `194.199.113.18`), hence three port pairs per host.

**`picarx_sim_QoS.html`** — visual trajectory simulator: animated car on the recorded track, real-time latency next to each VM, active-VM badge with cyan halo, and the active provider tile.

### Demo Flow

```
Browser (PC)
    │  GET  http://<picar>:8080/               → HTML simulator
    │  POST /tick {x,y}      every 2 s         → real-time latencies
    │  GET  /vm-status       every 2 s         → active VM badge
    ▼
picar_bridge_QoS1.py (PiCar :8080)
    │  POST /ping {x,y} × 8 VMs in parallel    → simulated latency
    │  POST :8001/rtt   (provider-1 VMs)       ┐ partitioned by provider,
    │  POST :8101/rtt   (provider-2 VMs)       ┘ throttled to one send per 5 s
    ▲  POST /active-vm-push  from the ACTIVE hub, end of each cycle
    │
    └── Hub provider-1 :8000        Hub provider-2 :8100
```

The effective cycle is **6.0 s** — `SEND_INTERVAL_S = 5 s` quantised by the browser's 2 s tick — of which ~4.7 s is orchestration work. Every timing parameter in the demo is dimensioned against that figure.

### Starting the Demo

See [`ETAPES_LANCEMENT_PROJET.md`](ETAPES_LANCEMENT_PROJET.md) for the full ordered runbook. In outline:

```bash
# On each OpenStack VM
ssh -i ~/projet_PFE/admin_log_2.pem ubuntu@194.199.113.18   # edge1 / edge1b / edge1c
./launch_edge1_machine.sh

# On the Raspberry Pi
cd ~/Projet_PFE/multiProvider && python3 picar_bridge_QoS1.py
```

---

## Tech Stack

| Category | Tools |
|---|---|
| Language | Python 3.10+ |
| APIs | FastAPI, Uvicorn, Flask, httpx |
| Storage | Redis |
| LLM | LAAS vLLM (Qwen3-27B-FP16), Ollama (Qwen2.5) |
| ML Models | ESN, LSTM, GRU, RNN (external FastAPI APIs on :5001/:5002/:5003) |
| ML Libraries | NumPy, scikit-learn, TensorFlow/Keras, scipy |
| Infrastructure | OpenStack, Kubernetes (kubectl), SSH |
| Demo | Raspberry Pi (PiCar-X), Flask bridge, HTML5 Canvas |
| Tests | Pytest |

---

## Configuration

Environment variables (all optional, defaults in `shared/config.py`):

```ini
# Hub
HUB_HOST=localhost
HUB_PORT=8000

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379

# Orchestration
MIGRATION_COOLDOWN_S=5        # 5s for demo, 60s recommended for production
PROACTIVE_FACTOR=0.85
BOOTSTRAP_MIN=5
HISTORY_WINDOW=50             # must be >= each ML model's window_size (see note below)

# Multi-provider federation
MULTI_PROVIDER_ENABLED=false  # OFF by default; launch_provider.py sets it to true
PROVIDER_ID=provider-1        # identity of THIS stack — "all" disables the partition
PORT_OFFSET=0                 # 100 for the provider-2 stack (hub 8100, relay 8110…)
REDIS_DB=0                    # 1 for provider-2 — state isolation between stacks
PROVIDER_RELAY_PORT=8010
PLACEMENT_ARBITER_PORT=8011
FEDERATION_VIEW_PORT=8500
ARBITER_DEADBAND=0.05         # ABSOLUTE band on the Gap Grade, not a relative margin
SLO_ENFORCEMENT=hard          # "hard": never elect a non-compliant placement
AWARD_GRACE_PERIOD_S=90       # see note below — MUST exceed kubectl propagation time
ORCHESTRATOR_URL_PROVIDER_1=http://localhost:8000
ORCHESTRATOR_URL_PROVIDER_2=http://localhost:8100
RELAY_URL_PROVIDER_1=http://localhost:8010
RELAY_URL_PROVIDER_2=http://localhost:8110

# ML APIs
ML_RTT_URL=http://localhost:5001/predict
ML_CPU_URL=http://localhost:5002/predict
ML_RAM_URL=http://localhost:5003/predict

# LLM — LAAS (primary)
LAAS_LLM_URL=https://pfcalcul.laas.fr/vllm/v1/chat/completions
LAAS_MODEL=Qwen3/Qwen--Qwen3.6-27B-FP16
LAAS_LLM_PROXY=

# LLM — Ollama (local fallback)
OLLAMA_URL=http://localhost:11434
INTENT_MODEL=qwen2.5:latest

# OpenStack
OPENSTACK_MASTER_IP=194.199.113.8
OPENSTACK_SSH_USER=ubuntu
OPENSTACK_STAGE_DIR=~/stage

# Infrastructure endpoints
PICAR_BRIDGE_URL=http://140.93.64.105:8080
ALL_VM_REGISTRY_JSON=            # optional JSON override of the 8 VM endpoints;
                                 # rejected at startup if any VM is missing
```

> **`AWARD_GRACE_PERIOD_S` is the parameter most likely to break the demo.** After an award, the receiving hub must not re-evaluate its role until kubectl has actually propagated the pod. Propagation was measured at **25–85 s**, against a default grace of 15 s. Too short, and the receiver demotes itself, later re-adopts the node's canonical VM, and then "migrates" to where the service already is — the parasitic `edge1 → edge1c` transitions. Set it to **90** for the demo:
>
> ```powershell
> $env:AWARD_GRACE_PERIOD_S="90"
> ```

> **Guard the trained latency model.** `check_and_retrain_model()` retrains automatically after 3 consecutive RMSE above `RMSE_THRESHOLD`, on the ~800 rows of the current session — silently overwriting the model trained on 192 000 rows. Before a demo, disable it:
>
> ```bash
> curl "http://localhost:5001/update_configs?new_rmse_patience=999999&new_rmse_threshold=999999"
> ```

---

## Real Infrastructure

Eight simulated VMs over **four physical hosts**, plus the master, the PiCar and the orchestrator PC:

| Role | Host | Simulated VMs | Ping ports | Agent ports |
|---|---|---|---|---|
| Edge host 1 | `194.199.113.18` | edge1, edge1b, edge1c | 5001–5003 | 8200–8202 |
| Edge host 2 | `194.199.113.28` | edge2, edge2b, edge2c | 5001–5003 | 8200–8202 |
| Cloud 1 | `194.199.113.66` | cloud1 | 5001 | 8200 |
| Cloud 2 | `194.199.113.69` | cloud2 | 5001 | 8200 |
| OpenStack master | `194.199.113.8` | — kubectl + `openstack_client :8024` | | |
| Raspberry Pi (PiCar-X) | `140.93.64.105` | — bridge `:8080` | | |
| Orchestrator PC | `140.93.89.92` | — both provider stacks | | |

Kubernetes clusters: `edge-cluster` & `cloud-cluster`.

Cloud VMs are provisioned at **16 cores / 16 GB**. The earlier 8 GB sizing left available RAM straddling a 6 GB SLO threshold, which produced compliance flapping between consecutive cycles.

> **kubectl granularity.** Three simulated VMs share one Kubernetes node, so kubectl can only ever name the node's canonical VM. Anything that needs the *precise* VM must ask the hub, never the master — this is why the PiCar bridge no longer falls back on `openstack_client`.

> **WSL note**: use `chmod 400` on the PEM key from WSL. Replace `localhost` with the Windows IP (`140.93.89.92`) to access services from WSL.

---

## Installation

```bash
git clone https://github.com/ahmedkammoun14/Orchestrateur-QOS-
cd qos-orchestrator
python -m venv venv
source venv/bin/activate        # Linux/macOS
# venv\Scripts\activate         # Windows
pip install -r requirements.txt
```

---

## Starting the Services

### Prerequisites

```bash
# Redis
sudo service redis-server start

# Ollama (local LLM fallback)
ollama serve
```

### Recommended — one command per provider

`launch_provider.py` starts a **complete stack** (11 spokes + hub) in a single window, prefixing each line with its service name. It sets `PROVIDER_ID`, `PORT_OFFSET`, `REDIS_DB` and `MULTI_PROVIDER_ENABLED=true`, and **purges that provider's Redis database before starting**.

```powershell
# UTF-8 is required, otherwise the log-capture threads die on the first accented character
$env:PYTHONIOENCODING="utf-8"; [Console]::OutputEncoding=[Text.Encoding]::UTF8

python launch_provider.py --provider provider1    # hub 8000, relay 8010, arbiter 8011, Redis DB 0
python launch_provider.py --provider provider2    # hub 8100, relay 8110, arbiter 8111, Redis DB 1
```

Then, once, the shared component:

```bash
python -m services.federation_view.app        # :8500 — read-only federated view
```

`openstack_client` (`:8024`) is also shared, but it is **not started from this repository** — it runs on the OpenStack master, where kubectl and the cluster YAMLs live. See [`ETAPES_LANCEMENT_PROJET.md`](ETAPES_LANCEMENT_PROJET.md).

> **Why the Redis purge matters.** Redis runs outside the stack and survives its shutdown. The metric lists keep the last `HISTORY_WINDOW` points (LTRIM), including the previous session's. On restart, the prediction window would hold yesterday's data, then a *mix* of two sessions — an artificial step in the middle of the window that the GRU has never seen in training, sending predictions far off. Purging removes that failure mode entirely; measured effect on the out-of-bounds clamp rate: **7.6 % → 2.3 %**. Use `--keep-redis` to opt out (not recommended).

> **Warm-up.** Level 1 of the prediction cascade is skipped until the history reaches the model's `window_size`. At `look_back = 45` and a ~6 s cycle, that is **≈ 4 min 30** during which predictions come from `last_value_fallback` and proactive detection is inactive. Do not measure anything before that point.

### Manual launch order (respect the order — the Hub checks health at startup)

```bash
python -m services.database.app              # 1. Port 8006
python -m services.history_loader.app        # 2. Port 8007
python -m services.collector.app             # 3. Port 8005
python -m services.metrics_manager.app       # 4. Port 8004
python -m services.ml_predictor.app          # 5. Port 8003
python -m services.decision_intelligence.app # 6. Port 8008
python -m services.intent_manager.app        # 7. Port 8002
python -m services.latency_manager.app       # 8. Port 8001
python -m services.observability.app         # 9. Port 8009
python -m services.placement_arbiter.app     # 10. Port 8011 (multi-provider only)
python -m services.provider_relay.app        # 11. Port 8010 (multi-provider only)
python -m hub.orchestrator_core              # 12. Port 8000
```

Add `PORT_OFFSET=100` to every command to obtain the provider-2 stack. `openstack_client` (`:8024`) must already be running **on the master** before the hub starts — the hub health-checks it.

> **After retraining any ML model, restart `ml_predictor`.** It reads each model's `window_size` **once, at startup** (`services/ml_predictor/app.py`). Retraining can change that value; the predictor would then send a wrongly-sized sequence, the API would answer `400`, and the cascade would fall through to `last_value_fallback` — silently, and permanently. The tell-tale sign is a prediction *exactly equal to the measurement and identical across all 7 horizons*: that is the fallback, not a well-fitted model.

---

## Service Ports

| Service | Port | Role |
|---|---|---|
| Hub Core | 8000 | Central orchestrator |
| Latency Manager | 8001 | RTT reception from PiCar |
| Intent Manager | 8002 | SLO extraction via LLM |
| ML Predictor | 8003 | Prediction orchestrator (3-level cascade) |
| Metrics Manager | 8004 | MI scoring + adaptive thresholds |
| Collector | 8005 | CPU/RAM collection from VMs |
| Database | 8006 | Redis persistence |
| History Loader | 8007 | Historical windowing |
| Decision Intelligence | 8008 | TOPSIS + violation detection |
| Observability | 8009 | Real-time dashboard + reasoning panel |
| Provider Relay | 8010 | Inter-orchestrator transport (Contract Net) |
| Placement Arbiter | 8011 | Elects the winning bid — `POST /arbitrate` |

Every port above belongs to **one provider stack** and is shifted by `PORT_OFFSET` for the second one: provider-2's hub is 8100, its relay 8110, its arbiter 8111, its dashboard 8109.

The following are **shared** by both stacks and started once:

| Service | Port | Role |
|---|---|---|
| OpenStack Client | 8024 | kubectl migrations (on master 194.199.113.8) |
| Federation View | 8500 | Read-only federated view + operator controls (`/api/intent`, `/api/reset`) |
| ML API — Latency | 5001 | GRU model for latency prediction |
| ML API — CPU | 5002 | GRU model for cpu_usage prediction |
| ML API — RAM | 5003 | GRU model for ram_usage prediction |

---

## API Reference

### Hub Core — port 8000

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/rtt` | Receive RTT measurements from PiCar |
| `POST` | `/intent` | Inject SLOs extracted by the LLM (internal use) |
| `GET` | `/data` | Full snapshot (metrics + predictions + decision) |
| `GET` | `/status` | Orchestrator summary state |
| `GET` | `/health` | Health check |
| `POST` | `/reset` | Reset to autonomous mode |

### Intent Manager — port 8002

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/intent` | LLM processing of user intention → forwards SLOs to hub |
| `GET` | `/health` | Health check + Ollama status |

> **Important**: send intent to `:8002/intent` (not `:8000/intent`) with the field `intention` (not `intent`). The LLM analyzes the text, extracts SLOs, and automatically forwards them to the hub.

### Decision Intelligence — port 8008

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/decide` | TOPSIS + violation detection (reactive + proactive) |

### Example — send a user intention

```powershell
# PowerShell — UTF-8 encoding required for French characters
$bodyObj = @{
    intent_id = "test-enhanced-001"
    intention = "Je regarde mon robot en direct depuis l appli et ca rame vraiment. L image saute, elle gele pendant des secondes et des fois je perds completement le flux. En plus l appli plante de temps en temps sans raison. Je veux juste regarder le robot bouger sans probleme."
}
$bytes = [System.Text.Encoding]::UTF8.GetBytes(($bodyObj | ConvertTo-Json -Depth 3 -Compress))

Invoke-RestMethod -Uri "http://localhost:8002/intent" `
                  -Method POST `
                  -ContentType "application/json; charset=utf-8" `
                  -Body $bytes
```

```bash
# bash
curl -X POST http://140.93.89.92:8002/intent \
  -H "Content-Type: application/json" \
  -d '{"intention": "I want a very smooth video stream with low latency"}'
```

### Example — check current state

```bash
curl http://140.93.89.92:8000/status
# Returns: mode, service_vm, cycle, cooldown_active, active_slos, last_decision
```

---

## Tests

```bash
# All tests
pytest tests/

# Unit tests
pytest tests/unit/

# Integration tests
pytest tests/integration/
```

**354 tests**, across 29 unit modules.

Federation coverage: `test_provider_registry.py` (registry consistency), `test_provider_arbitration.py` (Gap Grade, dead-band), `test_gap_grade.py` (augmented Tchebycheff), `test_provider_bid.py` (bid construction), `test_relay_broadcast.py` and `test_provider_relay.py` (transport + anti-loop guard), `test_placement_arbiter.py` (election), `test_award_message.py` and `test_ceding_provider.py` (handover), `test_active_standby_role.py` (role follows the service), `test_federated_cycle.py` (end-to-end cycle), `test_multi_provider_flow.py` (paths A/B/C/D), `test_federated_reasoning.py` and `test_multi_provider_reasoning.py` (audit block), `test_node_granularity.py` (kubectl node vs simulated VM), `test_federation_view.py`, `test_slo_intent.py` (immutable intent).

No test opens a socket: `_post`, `_post_audit` and `_execute_kubectl_migration` are mocked. Coroutines are driven through a local `_run()` helper, as `pytest-asyncio` is deliberately not a dependency.

---

## Project Structure

```text
qos-orchestrator/
├── hub/
│   ├── orchestrator_core.py          # Central hub — decision loop
│   └── provider_arbitration.py       # Pure module — per-provider evaluation,
│                                     # violation score, negotiate() + dead-band
├── services/
│   ├── collector/                    # Real-time metrics collection (EMA timeout)
│   ├── database/                     # Redis persistence (atomic pipeline)
│   ├── decision_intelligence/        # TOPSIS + ViolationDetector
│   │   ├── decision.py               # Decision handler (TOPSIS + active VM fix)
│   │   ├── topsis.py                 # 7-step TOPSIS with min-max normalisation
│   │   └── violation_detector.py     # Reactive / proactive breach classification
│   ├── history_loader/               # Redis history reading
│   ├── intent_manager/               # LLM (LAAS → Ollama) + SLOMerger
│   ├── latency_manager/              # RTT proxy PiCar → Hub
│   ├── metrics_manager/              # MI k-NN scoring + adaptive percentile
│   ├── ml_predictor/                 # 3-level prediction cascade orchestrator
│   ├── observability/                # Real-time SSE dashboard + reasoning panel
│   ├── placement_arbiter/            # Bid election (:8011) — POST /arbitrate
│   ├── federation_view/              # Read-only federated view (:8500, shared)
│   │                                 # + operator controls: /api/intent, /api/reset
│   └── provider_relay/               # Inter-orchestrator transport (:8010)
│                                     # broadcast / award / intent — no decision logic
├── shared/
│   ├── config.py                     # Ports, METRICS_REGISTRY, SLO bounds
│   ├── models.py                     # Pydantic models (SLO, RTTMeasurement…)
│   └── redis_keys.py                 # Redis key constants
├── scripts/                          # Timing export & comparison utilities
├── tests/
│   ├── unit/                         # TOPSIS, MI, violation_detector, LLM handler
│   └── integration/                  # Full hub → services cycle
├── launch_provider.py                # Launcher — one full stack per provider,
│                                     # single window, automatic Redis purge
├── start_provider.ps1                # PowerShell wrapper around launch_provider.py
├── start_relay.ps1                   # Relay-only launcher (debug)
├── start_all_multi.ps1               # Legacy Windows launcher (single-process mode)
├── ETAPES_LANCEMENT_PROJET.md        # Operational runbook — training, startup, demo (FR)
├── README.md
└── requirements.txt
```

Deployed elsewhere and intentionally absent from this tree: the per-VM agents, the PiCar bridge and simulator, `openstack_client.py`, and the three ML prediction APIs. See [What This Repository Contains](#what-this-repository-contains--and-what-it-does-not).

---

## Roadmap

- [x] End-to-end QoS pipeline (PiCar → Hub → migration)
- [x] Real kubectl migrations via OpenStack
- [x] LLM cascade LAAS vLLM (Qwen3-27B) + Ollama fallback
- [x] MI k-NN (Kozachenko-Leonenko) continuous estimator — replaces 2×2 table
- [x] 5-step MI visualization with ASCII tables in metrics_manager terminal
- [x] Cycle traceability — same cycle number visible in metrics_manager AND decision_intelligence
- [x] Adaptive secondary SLOs driven by MI scores
- [x] ML-driven proactive violation detection (labeled "proactive" when predictions guide the decision)
- [x] SSE observability dashboard with audit trail (http://localhost:8009)
- [x] PiCar-X demo with position-based latency simulation
- [x] Live HTML PiCar simulator with active VM indicator
- [x] Unit tests (TOPSIS, MI, violation_detector, LLM handler)
- [x] Active VM included in TOPSIS pool — STAY when current VM is still best candidate
- [x] YAML_PER_VM mapping — correct Kubernetes nodeSelector per VM (space_1/space_2 PoP labels)
- [x] ESN recursive strategy fixed — flatten guarantees 2D input shape in multi-step prediction
- [x] 3-level ML prediction cascade (sequence → point → last_value_fallback)
- [x] Transversal multi-provider partition — each provider spans both tiers (`PROVIDER_REGISTRY`)
- [x] Normalised violation score as the only valid inter-provider comparison metric
- [x] HTTP negotiation protocol with absolute dead-band — `negotiate()` (legacy handoff), then `arbitrate()` (`ARBITER_DEADBAND`)
- [x] Provider Relay transport gateway with anti-loop guard (:8010)
- [x] `MULTI_PROVIDER_ENABLED` feature flag — non-regression safety, OFF by default
- [x] Reasoning panel in the dashboard — the 5 decision steps, per-VM justification
- [x] Distributed deployment — one full orchestrator stack per provider (`PORT_OFFSET`, separate Redis DB)
- [x] Contract Net Protocol — broadcast → bids → arbitration → award, initiator as a role
- [x] Gap Grade — augmented Tchebycheff scalarisation (Steuer & Choo 1983) as the inter-provider metric
- [x] Placement Arbiter as a dedicated microservice (`:8011`)
- [x] Federated view with operator controls (`:8500`)
- [x] Shared SLO contract — propagation to peers + adoption by the `STANDBY`, versioned by `intent_version`
- [x] Bid and broadcast issued in parallel (`asyncio.gather`)
- [x] 8 simulated VMs over 4 Kubernetes nodes, with kubectl-granularity handling
- [x] Automatic Redis purge at stack startup — removes cross-session prediction drift
- [x] Latency model retrained on the deployed geometry — MAE **50.79 ms → 2.94 ms**, 97.5 % decision accuracy, 0 false alarms. The original model had been trained on a distribution centred at 118 ms while the active VM operates at 5–31 ms, which is what made migrations appear late
- [x] Precise active-VM reporting on the PiCar dashboard — hub push preferred, kubectl fallback removed (it could only name the canonical VM of a node)
- [ ] Version guard on the broadcast contract adoption (the `/intent` and award paths already have it)
- [ ] Single shared history load for MI and prediction — removes a redundant call and the read/write race on the metric keys. Measured as cycle-time neutral (3 034 ms vs 3 051 ms); deliberately deferred rather than touch the core loop late in the project
- [ ] Version the deployed per-VM and PiCar scripts in a dedicated infrastructure repository
- [ ] Calibrate `MI_RELATIVE_THRESHOLD` by permutation test (currently a fixed 0.15)
- [ ] Retrain the CPU and RAM models on the deployed geometry, as was done for latency
- [ ] Experimental validation — negotiation overhead measurement
- [ ] Docker + docker-compose containerization
- [ ] Multi-user support and intent isolation
- [ ] Public REST API documentation (enriched Swagger UI)

---

## Authors

**Ahmed Kammoun** — [ahmed.kammoun@enis.tn](mailto:ahmed.kammoun@enis.tn) — ENIS Sfax  
**Mustapha** — ENIS Sfax

Supervision: **LAAS-CNRS Toulouse**
