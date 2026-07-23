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
- [Academic Context](#academic-context)
- [Architecture](#architecture)
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

The system operates in two modes:
- **Autonomous** — fixed business objective (latency < 100 ms, `shared/config.py` `METRICS_REGISTRY["latency"]["default_threshold"]`), secondary SLOs discovered automatically by MI.
- **Enhanced** — SLOs injected by the user via natural language (LLM also assigns each SLO's **weight** and decides the **merge_strategy** — REPLACE or ADDITIVE — against active SLOs), enriched by MI-driven secondary SLOs.

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
                                              │
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

## Key Technical Features

- **End-to-end QoS pipeline**: real flow from the PiCar-X (Raspberry Pi) → `latency_manager` → `hub` → automatic decision across 4 OpenStack VMs.
- **Position-based simulated latency**: `latency_ms = VM_BASE_MS + VM_K_MS_PER_CM × distance_cm(car, VM)` (`infrastructure/vm_agent.py`) — the closer the vehicle, the lower the latency. Default coefficient `K` is **20 ms/cm for edge VMs, 40 ms/cm for cloud VMs** (edge is structurally favored).
- **7-step TOPSIS**: multicriteria VM selection (Min-Max normalization, weighting, Euclidean distances to ideal solutions A⁺ and A⁻). Criteria: SLO metrics (latency, CPU, RAM). Uses **ML predictions** as input values — not raw measurements — to anticipate future state.
- **Active VM as TOPSIS candidate**: the currently active VM is always included in the decision pool. If TOPSIS selects it despite a violation → STAY (it remains the best option). This prevents unnecessary migrations when the current VM is still the least-bad choice.
- **MI k-NN (Kozachenko-Leonenko)**: continuous Mutual Information estimator — replaces the old 2×2 contingency table. No discretization, detects non-linear dependencies, robust from ~15 points per class. Formula: `MI(X;Y) = H(X) − H(X|Y)`, normalized by `H(Y)` → score in [0, 1].
- **3-level ML prediction cascade**: Level 1 — `POST /predict_sequence` (full window, horizon 7); Level 2 — `GET /predict?input_data=X` (single point); Level 3 — `last_value_fallback`. Each level activates only if the previous fails.
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

The demo replaces traditional network RTT measurement with **distance-based simulated latency**. As the PiCar-X vehicle moves along its track, latency to each VM is computed from the Euclidean distance on a 2D map:

```
latency_ms = VM_BASE_MS + K × distance_cm(car_position, vm_position)
K = 20 ms/cm (edge VMs)  |  K = 40 ms/cm (cloud VMs)
```

### VM Positions on the Map

| VM | x (cm) | y (cm) | Type |
|---|---|---|---|
| edge1 | -20 | +50 | Edge (blue square) |
| edge2 | +30 | -10 | Edge (blue square) |
| cloud1 | -20 | -10 | Cloud (orange diamond) |
| cloud2 | +30 | +50 | Cloud (orange diamond) |

### Components

**`infrastructure/picar_bridge.py`** — Flask server on the PiCar (port 8080):

| Route | Method | Description |
|---|---|---|
| `/` | GET | Serves the HTML simulator |
| `/Trajectoire.jpg` | GET | Serves the track image |
| `/tick` | POST | Receives `{x, y}`, pings 4 VMs in parallel, sends RTT to hub |
| `/vm-status` | GET | Proxies `GET :8000/status` from hub → returns `service_vm` |

**`infrastructure/vm_ping/`** — one script per VM (edge1/edge2/cloud1/cloud2):

| Port | Description |
|---|---|
| 5001 | `POST /ping {x,y}` → computes distance, sleeps `latency_ms/1000`, returns latency |
| 8200 | `GET /metrics` → CPU/RAM via psutil |

**`infrastructure/picarx_sim.html`** — visual trajectory simulator:
- Animated car on a looped track
- Real-time latency displayed next to each VM
- **"Service active on" badge** — shows the VM hosting the service (polls `/vm-status` every 3s)
- **Cyan highlight** — active VM glows with a halo and `★ [ACTIVE]` label on the canvas

Access: `http://140.93.64.105:8080/`

### Demo Flow

```
Browser (PC)
    │  GET http://140.93.64.105:8080/          → HTML simulator
    │  POST /tick {x,y}  every 2s              → real-time latencies
    │  GET /vm-status    every 3s              → active VM badge
    ▼
picar_bridge.py (PiCar :8080)
    │  POST /ping {x,y} × 4 VMs in parallel   → simulated latency
    │  POST /rtt {measurements}                → triggers hub cycle
    │  GET  http://140.93.89.92:8000/status   → current service_vm
    ▼
VMs (port 5001) + Hub (port 8000)
```

### Starting the Demo

```bash
# On the PiCar (pi@140.93.64.105)
cd ~/Projet_PFE/trajectoire
python picar_bridge.py

# On each VM (e.g. edge1)
ssh -i ~/projet_PFE/admin_log_2.pem ubuntu@194.199.113.18
cd ~/projet_PFE/trajectoire
python edge1_ping.py
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
HISTORY_WINDOW=50

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
```

---

## Real Infrastructure

| Node | IP |
|---|---|
| OpenStack Master | `194.199.113.8` |
| Raspberry Pi (PiCar-X) | `140.93.64.105` |
| edge1 | `194.199.113.18` |
| edge2 | `194.199.113.28` |
| cloud1 | `194.199.113.66` |
| cloud2 | `194.199.113.69` |

Kubernetes clusters: `edge-cluster` & `cloud-cluster`

> **WSL note**: use `chmod 400` on the PEM key from WSL. Replace `localhost` with the Windows IP (`140.93.89.92`) to access services from WSL.

### Start VM agents

```bash
chmod 400 ~/projet_PFE/admin_log_2.pem
ssh -i ~/projet_PFE/admin_log_2.pem ubuntu@194.199.113.18  # edge1
cd ~/projet_PFE/trajectoire
python edge1_ping.py
# Repeat for edge2 (113.28), cloud1 (113.66), cloud2 (113.69)
```

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

# Flush Redis before a clean restart
redis-cli FLUSHDB
```

### Launch Order (respect the order — the Hub checks health at startup)

```bash
python -m services.database.app              # 1. Port 8006
python -m services.history_loader.app        # 2. Port 8007
python -m services.collector.app             # 3. Port 8005
python -m services.metrics_manager.app       # 4. Port 8004
python -m services.ml_predictor.app          # 5. Port 8003
python -m services.decision_intelligence.app # 6. Port 8008
python -m infrastructure.openstack_client    # 7. Port 8024
python -m services.intent_manager.app        # 8. Port 8002
python -m services.latency_manager.app       # 9. Port 8001
python -m services.observability.app         # 10. Port 8009
python -m hub.orchestrator_core              # 11. Port 8000
```

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
| Observability | 8009 | Real-time dashboard |
| OpenStack Client | 8024 | kubectl migrations (on master 194.199.113.8) |
| Provider Relay | 8010 | Inter-provider handoff gateway (federation) |
| ML API — Latency | 5001 | ESN/LSTM model for latency prediction |
| ML API — CPU | 5002 | ESN/LSTM model for cpu_usage prediction |
| ML API — RAM | 5003 | ESN/LSTM model for ram_usage prediction |

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

---

## Project Structure

```text
qos-orchestrator/
├── hub/
│   └── orchestrator_core.py          # Central hub — decision loop
├── infrastructure/
│   ├── ml_apis/                      # External ML prediction APIs
│   │   ├── ml_api_rtt.py             # ESN/LSTM latency model — port 5001
│   │   ├── ml_api_cpu.py             # ESN/LSTM CPU model    — port 5002
│   │   └── ml_api_ram.py             # ESN/LSTM RAM model    — port 5003
│   ├── picar_bridge.py               # PiCar Flask bridge (port 8080)
│   ├── picarx_sim.html               # HTML trajectory simulator
│   ├── Trajectoire.jpg               # Track image
│   └── vm_ping/                      # Per-VM ping + metrics scripts
│       ├── edge1_ping.py             # ping :5001 + agent :8200
│       ├── edge2_ping.py
│       ├── cloud1_ping.py
│       └── cloud2_ping.py
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
│   └── observability/                # Real-time SSE dashboard
├── shared/
│   ├── config.py                     # Ports, METRICS_REGISTRY, SLO bounds
│   ├── models.py                     # Pydantic models (SLO, RTTMeasurement…)
│   └── redis_keys.py                 # Redis key constants
├── tests/
│   ├── unit/                         # TOPSIS, MI, violation_detector, LLM handler
│   └── integration/                  # Full hub → services cycle
├── openstack_client.py               # kubectl migrations — deployed on master :8024
│                                     # YAML_PER_VM: space_1→edge1/cloud1, space_2→edge2/cloud2
├── DOCUMENTATION.md                  # Full technical documentation (FR)
└── requirements.txt
```

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
- [ ] Docker + docker-compose containerization
- [ ] Multi-user support and intent isolation
- [ ] Public REST API documentation (enriched Swagger UI)

---

## Authors

**Ahmed Kammoun** — [ahmed.kammoun@enis.tn](mailto:ahmed.kammoun@enis.tn) — ENIS Sfax  
**Mustapha** — ENIS Sfax

Supervision: **LAAS-CNRS Toulouse**
