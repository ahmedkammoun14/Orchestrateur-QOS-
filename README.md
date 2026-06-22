# QoS Orchestrator

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-Framework-green?logo=fastapi)
![Redis](https://img.shields.io/badge/Redis-Storage-red?logo=redis)
![OpenStack](https://img.shields.io/badge/Infrastructure-OpenStack-gray?logo=openstack)
![LLM](https://img.shields.io/badge/LLM-Qwen3--27B%20%7C%20Qwen2.5-purple?logo=openai)

Système d'orchestration de microservices pour la gestion autonome de la Qualité de Service (QoS) dans des environnements de streaming sur infrastructure cloud/edge réelle (OpenStack + Kubernetes).

## Table des matières
- [Présentation](#présentation)
- [Contexte académique](#contexte-académique)
- [Architecture globale](#architecture-globale)
- [Points forts techniques](#points-forts-techniques)
- [Stack technologique](#stack-technologique)
- [Configuration](#configuration)
- [Infrastructure réelle](#infrastructure-réelle)
- [Installation](#installation)
- [Démarrage des services](#démarrage-des-services)
- [Ports des services](#ports-des-services)
- [API Reference](#api-reference)
- [Tests](#tests)
- [Structure du projet](#structure-du-projet)
- [Roadmap](#roadmap)
- [Auteurs](#auteurs)

---

## Présentation

Dans les environnements distribués modernes, maintenir une performance constante est un défi. **QoS Orchestrator** résout ce problème en agissant comme un cerveau centralisé qui :

- Interprète les intentions des utilisateurs en langage naturel via un LLM (Qwen3-27B sur LAAS-CNRS, Qwen2.5 en fallback local).
- Découvre dynamiquement les métriques critiques via l'Information Mutuelle (MI).
- Prédit les violations futures grâce à des modèles ML (LSTM/GRU/RNN) sur un horizon de 7 cycles.
- Prend des décisions de migration optimales vers la meilleure VM (Edge ou Cloud) via l'algorithme multicritères TOPSIS.

Le système fonctionne en deux modes :
- **Autonomous** — objectif métier fixe (latence < 30 ms), SLOs secondaires découverts automatiquement par MI.
- **Enhanced** — SLOs injectés par l'utilisateur via langage naturel, enrichis par MI.

---

## Contexte académique

Ce projet a été développé dans le cadre d'un **Projet de Fin d'Études (PFE)** à l'**ENIS Sfax**, en partenariat avec le laboratoire **LAAS-CNRS Toulouse**.

- **Binôme :** Ahmed Kammoun & Mustapha
- **Encadrement :** LAAS-CNRS / ENIS Sfax

---

## Architecture globale

Le système suit un pattern **Hub-and-Spoke** : le `Hub` (Orchestrator Core) centralise la logique de contrôle et délègue les tâches spécifiques à des microservices indépendants.

```text
           [ Intent Manager ] <─── User (langage naturel)
                  │
                  ▼
[ PiCar ] ──► [ Latency Manager ] ──► [ HUB (Core) ] ──► [ Observability ]
                                          │
         ┌──────────┬──────────────┬──────┴──────────────────┐
         │          │              │                          │
   [ Collector ] [ ML Predictor ] [ Metrics Manager ] [ Decision Intelligence ]
         │          │              │                          │
         ▼          ▼              ▼                          ▼
   [ VM Agents ] [ ML APIs ]  [ History Loader ]    [ OpenStack Client ]
         │                         │                          │
         └──────────► [ Database (Redis) ] <──────────────────┘
                                                              └─► [ Kubectl ]
```

### Exceptions architecturales validées

Deux exceptions au modèle Hub-and-Spoke pur ont été validées pour des raisons de performance :

1. **Collector → Database** : le collecteur écrit directement les métriques en base pour éviter de saturer le Hub lors des cycles haute fréquence.
2. **OpenStack Client** : appelé directement par le Hub pour les migrations, sans passer par un spoke intermédiaire.

---

## Points forts techniques

- **Pipeline QoS end-to-end** : flux réel du Raspberry Pi (PiCar) → `latency_manager` → `hub` → décision automatique sur 4 VMs OpenStack.
- **RTT applicatif réel** : mesure via `HTTP GET /health` sur chaque VM, plus représentatif qu'un simple ping ICMP.
- **TOPSIS 7 étapes** : sélection multicritères de la VM cible (normalisation Min-Max, pondération, distances euclidiennes aux solutions idéales A⁺ et A⁻). Critères : métriques SLO, budget de conformité, fiabilité EMA.
- **MI Scoring (Information Mutuelle)** : pondération dynamique des SLOs par corrélation temps réel entre métriques système (CPU/RAM) et violations de latence.
- **Seuils adaptatifs** : percentile automatique (P70/P75/P85) selon le coefficient de variation — absorbe la volatilité du signal sans reconfiguration manuelle.
- **Détection proactive** : anticipation des violations SLO via prédictions ML (LSTM/GRU/RNN) sur 7 cycles futurs, avec facteur de prudence ajusté à l'incertitude du modèle.
- **LLM cascade 2 niveaux** : extraction des SLOs depuis le langage naturel via LAAS vLLM (Qwen3-27B, primaire) → Ollama local (Qwen2.5, fallback). Le LLM gère seul la cohérence avec les SLOs actifs via le contexte RAG injecté dans le prompt.
- **Dashboard temps réel** : interface Matplotlib avec courbes réelles, prédictions passées (audit de précision), prédictions futures, seuil SLO, **marqueurs de migration** (ligne violette annotée à chaque événement) et **bouton Pause/Resume** pour les démos.
- **METRICS_REGISTRY extensible** : ajout d'une nouvelle métrique via un unique dictionnaire dans `shared/config.py`, sans modifier le code des services.
- **Snapshot atomique** : le endpoint `/data` du Hub garantit la cohérence totale (métriques, prédictions, SLOs, décision) pour chaque cycle.
- **Anti-thrashing** : cooldown post-migration configurable (`MIGRATION_COOLDOWN_S`, défaut 60 s) bloquant toute nouvelle migration pendant la stabilisation.
- **Fiabilité EMA** : chaque VM dispose d'un score de fiabilité mis à jour par moyenne mobile exponentielle (alpha configurable), intégré comme critère TOPSIS.

---

## Stack technologique

| Catégorie | Outils |
|-----------|--------|
| Langage | Python 3.10+ |
| APIs | FastAPI, Uvicorn, httpx |
| Stockage | Redis |
| LLM | LAAS vLLM (Qwen3-27B-FP16), Ollama (Qwen2.5) |
| ML | LSTM / GRU / RNN (APIs séparées) |
| Infrastructure | OpenStack, Kubernetes (kubectl), SSH |
| Visualisation | Matplotlib |
| Tests | Pytest |

---

## Configuration

Variables d'environnement (toutes optionnelles, valeurs par défaut dans `shared/config.py`) :

```ini
# Hub
HUB_HOST=localhost
HUB_PORT=8000

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379

# Orchestration
MIGRATION_COOLDOWN_S=60
PROACTIVE_FACTOR=0.85
BOOTSTRAP_MIN=5
HISTORY_WINDOW=50

# ML APIs
ML_RTT_URL=http://localhost:5001/predict
ML_CPU_URL=http://localhost:5002/predict
ML_RAM_URL=http://localhost:5003/predict

# LLM — LAAS (primaire)
LAAS_LLM_URL=https://pfcalcul.laas.fr/vllm/v1/chat/completions
LAAS_MODEL=Qwen3/Qwen--Qwen3.6-27B-FP16
LAAS_LLM_PROXY=

# LLM — Ollama (fallback local)
OLLAMA_URL=http://localhost:11434
INTENT_MODEL=qwen2.5:latest

# OpenStack
OPENSTACK_MASTER_IP=194.199.113.8
OPENSTACK_SSH_USER=ubuntu
OPENSTACK_STAGE_DIR=~/stage
```

---

## Infrastructure réelle

| Nœud | IP |
|------|----|
| Master OpenStack | `194.199.113.8` |
| Raspberry Pi (PiCar) | `140.93.64.105` |
| edge1 | `194.199.113.18` |
| edge2 | `194.199.113.28` |
| cloud1 | `194.199.113.66` |
| cloud2 | `194.199.113.69` |

Clusters Kubernetes : `edge-cluster` & `cloud-cluster`

> **Note WSL** : utiliser `chmod 400` sur la clé PEM depuis WSL. Remplacer `localhost` par l'IP Windows (`140.93.89.92`) pour accéder aux services depuis WSL.

### Démarrer les agents VM

```bash
chmod 400 ~/projet_PFE/admin_log_2.pem
ssh -i ~/projet_PFE/admin_log_2.pem ubuntu@194.199.113.18  # edge1
nohup python3 ~/projet_PFE/vm_agent.py &
# Répéter pour edge2 (113.28), cloud1 (113.66), cloud2 (113.69)
```

### Démarrer les APIs ML

```bash
# 3 terminaux séparés
uvicorn app.auto:auto_app --port 5001 --reload  # latency
uvicorn app.auto:auto_app --port 5002 --reload  # cpu
uvicorn app.auto:auto_app --port 5003 --reload  # ram
```

### Démarrer le PiCar

```bash
# Sur le Raspberry Pi
HUB_URL=http://140.93.89.92:8001/rtt python3 ~/Projet_PFE/picar_client.py
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

## Démarrage des services

### Prérequis

```bash
# Redis
sudo service redis-server start

# Ollama (fallback LLM local)
ollama serve

# Vider Redis avant un redémarrage propre
redis-cli FLUSHDB
```

### Ordre de lancement (respecter l'ordre — le Hub vérifie les health checks au démarrage)

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

## Ports des services

| Service | Port | Rôle |
|---------|------|------|
| Hub Core | 8000 | Orchestrateur central |
| Latency Manager | 8001 | Réception RTT depuis PiCar |
| Intent Manager | 8002 | Extraction SLOs via LLM |
| ML Predictor | 8003 | Prédictions LSTM/GRU |
| Metrics Manager | 8004 | MI scoring + seuils adaptatifs |
| Collector | 8005 | Collecte CPU/RAM sur VMs |
| Database | 8006 | Persistance Redis |
| History Loader | 8007 | Fenêtrage historique |
| Decision Intelligence | 8008 | TOPSIS + détection violations |
| Observability | 8009 | Dashboard temps réel |
| OpenStack Client | 8024 | Migrations kubectl/SSH |

---

## API Reference

### Hub Core — port 8000

| Méthode | Endpoint | Description |
|---------|----------|-------------|
| `POST` | `/rtt` | Réception mesures RTT depuis PiCar |
| `POST` | `/intent` | Injection SLOs extraits par le LLM |
| `GET` | `/data` | Snapshot complet (métriques + prédictions + décision) |
| `GET` | `/status` | État résumé de l'orchestrateur |
| `GET` | `/health` | Healthcheck |

### Intent Manager — port 8002

| Méthode | Endpoint | Description |
|---------|----------|-------------|
| `POST` | `/intent` | Traitement LLM de l'intention utilisateur |
| `GET` | `/health` | Healthcheck + état Ollama |

### Decision Intelligence — port 8008

| Méthode | Endpoint | Description |
|---------|----------|-------------|
| `POST` | `/decide` | TOPSIS + détection violations (réactive + proactive) |

### ML Predictor — port 8003

| Méthode | Endpoint | Description |
|---------|----------|-------------|
| `POST` | `/predict` | Prédictions latence/CPU/RAM pour toutes les VMs |

### OpenStack Client — port 8024

| Méthode | Endpoint | Description |
|---------|----------|-------------|
| `POST` | `/migrate` | Migration kubectl réelle entre clusters |
| `GET` | `/active_vm` | VM actuellement active sur Kubernetes |

### Exemple — envoyer une intention utilisateur

```bash
curl -X POST http://localhost:8002/intent \
  -H "Content-Type: application/json" \
  -d '{"intention": "Je veux un flux vidéo très fluide avec une latence inférieure à 80ms"}'
```

---

## Tests

```bash
# Tous les tests
pytest tests/

# Tests unitaires
pytest tests/unit/

# Tests d'intégration
pytest tests/integration/
```

---

## Structure du projet

```text
qos-orchestrator/
├── hub/
│   └── orchestrator_core.py          # Hub central — boucle de décision
├── infrastructure/
│   ├── vm_agent.py                   # Agent FastAPI sur chaque VM
│   ├── openstack_client.py           # Migrations kubectl / SSH
│   ├── picar_client.py               # Client RTT Raspberry Pi
│   └── ml_apis/                      # APIs ML (latency / cpu / ram)
├── services/
│   ├── collector/                    # Collecte métriques temps réel (EMA timeout)
│   ├── database/                     # Persistance Redis (pipeline atomique)
│   ├── decision_intelligence/        # TOPSIS + ViolationDetector
│   ├── history_loader/               # Lecture historiques Redis
│   ├── intent_manager/               # LLM (LAAS → Ollama) + SLOMerger
│   ├── latency_manager/              # Proxy RTT PiCar → Hub
│   ├── metrics_manager/              # MI scoring + percentile adaptatif
│   ├── ml_predictor/                 # Orchestration prédictions ML
│   └── observability/                # Dashboard Matplotlib temps réel
├── shared/
│   ├── config.py                     # Ports, METRICS_REGISTRY, VM_REGISTRY
│   ├── models.py                     # Modèles Pydantic (SLO, RTTMeasurement…)
│   └── redis_keys.py                 # Constantes clés Redis
├── tests/
│   ├── unit/                         # TOPSIS, MI, violation_detector, LLM handler
│   └── integration/                  # Cycle complet hub → services
└── requirements.txt
```

---

## Roadmap

- [x] Pipeline QoS end-to-end opérationnel (PiCar → Hub → migration)
- [x] Migrations kubectl réelles via OpenStack
- [x] LLM cascade LAAS vLLM (Qwen3-27B) + Ollama fallback
- [x] MI scoring + SLOs secondaires adaptatifs
- [x] Détection proactive des violations (horizon 7 cycles)
- [x] Dashboard temps réel — marqueurs de migration + pause/resume
- [x] Tests unitaires (TOPSIS, MI, violation_detector, LLM handler, redis_client)
- [ ] Conteneurisation Docker + docker-compose
- [ ] Support multi-utilisateurs et isolation des intents
- [ ] API REST publique documentée (Swagger UI enrichie)

---

## Auteurs

**Ahmed Kammoun** — [ahmed.kammoun@enis.tn](mailto:ahmed.kammoun@enis.tn) — ENIS Sfax  
**Mustapha** — ENIS Sfax  

Encadrement : **LAAS-CNRS Toulouse**
