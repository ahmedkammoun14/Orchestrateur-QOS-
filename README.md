# 🚀 QoS Orchestrator

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-Framework-green?logo=fastapi)
![Redis](https://img.shields.io/badge/Redis-Storage-red?logo=redis)
![OpenStack](https://img.shields.io/badge/Infrastructure-OpenStack-gray?logo=openstack)

Système d'orchestration de microservices pour la gestion autonome de la Qualité de Service (QoS) dans des environnements de streaming sur infrastructure cloud réelle.

## 📖 Table des matières
- [🚀 Présentation](#-présentation)
- [🎓 Contexte Académique](#-contexte-académique)
- [🏗️ Architecture Globale](#️-architecture-globale)
- [✨ Points Forts Techniques](#-points-forts-techniques)
- [🛡️ Anti-Thrashing & Cooldown](#️-anti-thrashing--cooldown)
- [🛠️ Stack Technologique](#️-stack-technologique)
- [⚙️ Configuration](#️-configuration)
- [🌐 Infrastructure Réelle](#-infrastructure-réelle)
- [📡 Agents sur les VMs OpenStack](#-agents-sur-les-vms-openstack)
- [🤖 APIs ML (Mustapha)](#-apis-ml-mustapha)
- [🚗 Lancement PiCar (Raspberry Pi)](#-lancement-picar-raspberry-pi)
- [📦 Installation](#-installation)
- [🚀 Démarrage des Services](#-démarrage-des-services)
- [🔌 API Reference](#-api-reference)
- [🧪 Tests](#-tests)
- [📂 Structure du Projet](#-structure-du-projet)
- [🗺️ Roadmap](#️-roadmap)
- [👨‍💻 Auteurs](#-auteurs)

## 🚀 Présentation
Dans les environnements distribués modernes, maintenir une performance constante est un défi. **QoS Orchestrator** résout ce problème en agissant comme un cerveau centralisé qui :
- Interprète les intentions des utilisateurs en langage naturel.
- Découvre dynamiquement les seuils critiques via l'Information Mutuelle (MI).
- Prédit les violations futures grâce à des modèles ML.
- Prend des décisions de migration optimales vers les meilleures cibles (Edge ou Cloud) en utilisant l'algorithme multicritères TOPSIS.

Le système fonctionne en deux modes : **Autonomous** (découverte automatique) et **Enhanced** (guidé par l'intention utilisateur).

## 🎓 Contexte Académique
Ce projet a été développé dans le cadre d'un **Projet de Fin d'Études (PFE)** à l'**ENIS Sfax**, en partenariat avec le laboratoire **LAAS-CNRS Toulouse**.

*   **Binôme :** Ahmed Kammoun & Mustapha
*   **Encadrement :** LAAS-CNRS / ENIS

## 🏗️ Architecture Globale
Le système suit un pattern **Hub-and-Spoke** où le `Hub` (Orchestrator Core) centralise la logique de contrôle et délègue les tâches spécifiques à des `Spokes` (microservices).

### Schéma des interactions
```text
           [ Intent Manager ] <─── User
                  │
                  ▼
[ PiCar ] ──► [ Latency Manager ] ──► [ HUB (Core) ] ──► [ Observability ]
                                         │  │
    ┌────────────────┬───────────────────┴──┴───┬───────────────────┐
    │                │                          │                   │
[ Collector ]  [ ML Predictor ]        [ Metrics Manager ]  [ Decision Intelligence ]
    │                │                          │                   │
    ▼                ▼                          ▼                   ▼
[ VM Agents ]   [ ML APIs ]             [ History Loader ]  [ OpenStack Client ]
    │                                           │                   │
    └───────────► [ Database (Redis) ] <────────┘                   └─► [ Kubectl ]
```

### ⚖️ Exceptions Architecturales Validées
Pour optimiser les performances et la latence interne, deux exceptions au modèle Hub-and-Spoke pur ont été validées :
1.  **Collector → Database** : Le collecteur écrit directement les métriques brutes en base pour éviter de saturer le Hub lors des cycles de collecte haute fréquence.
2.  **Decision Intelligence → OpenStack Client** : La logique de décision peut interroger directement l'état de l'infrastructure pour une réactivité maximale lors des migrations.

## ✨ Points Forts Techniques

*   **🔗 Pipeline QoS End-to-End :** Flux réel partant du Raspberry Pi (**PiCar**) vers le `latency_manager`, traité par le `hub`, supervisant 4 VMs OpenStack avec déclenchement de décisions automatiques.
*   **📡 RTT Applicatif Réel :** Mesure de la latence via des requêtes `HTTP GET /health` applicatives, fournissant une vision précise de l'expérience utilisateur contrairement à un simple `ICMP ping`.
*   **📊 TOPSIS 7 Étapes :** Algorithme de sélection multicritères pour le choix de la VM cible (Normalisation Min-Max, Pondération, Distances Euclidiennes aux solutions idéales A+ et A-).
*   **🧠 MI Scoring (Information Mutuelle) :** Pondération dynamique des SLOs (Service Level Objectives) en calculant la corrélation en temps réel entre les métriques système (CPU/RAM) et la latence perçue.
*   **📉 Seuils Adaptatifs :** Ajustement automatique des percentiles de calcul (**P70/P75/P85**) basés sur le coefficient de variation des métriques pour absorber la volatilité.
*   **🔮 Détection Proactive :** Anticipation des violations de SLO grâce à des prédictions ML (**LSTM/GRU/RNN**) sur un horizon de 7 cycles futurs.
*   **🤖 Cascade LLM 3 Niveaux :** Extraction d'intentions utilisateur via une cascade robuste : `Ollama (Qwen2.5)` ➔ `Regex` ➔ `Keywords`.
*   **🖥️ Dashboard Temps Réel :** Interface `matplotlib` affichant simultanément les courbes réelles, les prédictions passées (pour audit de précision), les prédictions futures et les scores MI.
*   **🧩 METRICS_REGISTRY Extensible :** Architecture "Zero-Code" pour l'ajout de nouvelles métriques via un simple dictionnaire de configuration dans `shared/config.py`.
*   **📦 Snapshot Atomique :** Le endpoint `/data` du Hub garantit une cohérence totale des données (métriques, prédictions, décisions) pour chaque cycle, évitant tout désalignement temporel.
*   **☁️ Migrations Kubectl Réelles :** Exécution de commandes de migration de pods entre clusters via `openstack_client` pilotant le master OpenStack.

## 🛡️ Anti-Thrashing & Cooldown
Le système intègre un mécanisme de cooldown post-migration (configurable via `MIGRATION_COOLDOWN_S`, défaut 60s) pour éviter les oscillations de migration. Pendant le cooldown, toute nouvelle décision de migration est bloquée même si une violation est détectée.

## 🛠️ Stack Technologique
*   **Langage :** Python 3.10+
*   **APIs :** FastAPI, Uvicorn, httpx
*   **Stockage :** Redis (via un proxy microservice)
*   **Infrastructure :** Kubectl, SSH, OpenStack
*   **Intelligence :** Ollama (LLM), Scikit-learn, TensorFlow (APIs ML)
*   **Visualisation :** Matplotlib

## ⚙️ Configuration
Le système utilise les variables d'environnement suivantes pour sa configuration :
```ini
HUB_HOST=localhost
HUB_PORT=8000
REDIS_HOST=localhost
REDIS_PORT=6379
MIGRATION_COOLDOWN_S=60
PROACTIVE_FACTOR=0.85
ML_RTT_URL=http://localhost:5001/predict
ML_CPU_URL=http://localhost:5002/predict
ML_RAM_URL=http://localhost:5003/predict
OLLAMA_URL=http://localhost:11434
INTENT_MODEL=qwen2.5:latest
OPENSTACK_MASTER_IP=194.199.113.8
OPENSTACK_SSH_USER=ubuntu
OPENSTACK_STAGE_DIR=~/stage
```

## 🌐 Infrastructure Réelle
Le système orchestre un environnement multi-cloud/edge :
*   **Master OpenStack :** `194.199.113.8` (Ubuntu)
*   **Raspberry Pi (PiCar) :** `140.93.64.105`
*   **VMs de Service :**
    *   `edge1` : `194.199.113.18`
    *   `edge2` : `194.199.113.28`
    *   `cloud1` : `194.199.113.66`
    *   `cloud2` : `194.199.113.69`
*   **Clusters Kubectl :** `edge-cluster` & `cloud-cluster`

> ⚠️ **SSH depuis Windows** : utiliser WSL pour éviter les problèmes de permissions sur la clé PEM (`chmod 400`).
>
> ⚠️ **Depuis WSL** : utiliser l'IP Windows `140.93.89.92` au lieu de `localhost` pour accéder aux services.

## 📡 Agents sur les VMs OpenStack
```bash
# SSH depuis WSL (permissions clé PEM)
chmod 400 ~/projet_PFE/admin_log_2.pem
ssh -i ~/projet_PFE/admin_log_2.pem ubuntu@194.199.113.18  # edge1
nohup python3 ~/projet_PFE/vm_agent.py &
# Répéter pour edge2 (113.28), cloud1 (113.66), cloud2 (113.69)
```

## 🤖 APIs ML (Mustapha)
```bash
# Dans le dossier Api-Model-Predict (3 terminaux séparés)
uvicorn app.auto:auto_app --port 5001 --reload  # latency
uvicorn app.auto:auto_app --port 5002 --reload  # cpu
uvicorn app.auto:auto_app --port 5003 --reload  # ram

# Entraînement des modèles (depuis WSL)
curl -X POST "http://140.93.89.92:5001/main" -F "file=@new_dataset.xlsx" -F "target_columns=node1_delay" -F "forecasting_horizon=7"
curl -X POST "http://140.93.89.92:5002/main" -F "file=@new_dataset.xlsx" -F "target_columns=node1_cpu" -F "forecasting_horizon=7"
curl -X POST "http://140.93.89.92:5003/main" -F "file=@new_dataset.xlsx" -F "target_columns=node1_ram" -F "forecasting_horizon=7"
```

## 🚗 Lancement PiCar (Raspberry Pi)
```bash
# Sur le Raspberry Pi (140.93.64.105)
HUB_URL=http://140.93.89.92:8001/rtt python3 ~/Projet_PFE/picar_client.py
```

## 📦 Installation
```bash
git clone https://github.com/ahmedkammoun14/Orchestrateur-QOS-
cd qos-orchestrator
python -m venv venv
venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

## 🚀 Démarrage des Services

### 0. Initialisation OpenStack (une seule fois)
```bash
ssh -i admin_log_2.pem ubuntu@194.199.113.8
kubectl --context=edge-cluster create ns tc
kubectl --context=cloud-cluster create ns tc
kubectl --context=edge-cluster label ns tc istio-injection=enabled --overwrite
kubectl --context=cloud-cluster label ns tc istio-injection=enabled --overwrite
kubectl --context=edge-cluster apply -f stage/tc-proxy-service-clusterip.yaml
kubectl --context=cloud-cluster apply -f stage/tc-proxy-service-clusterip.yaml
kubectl --context=edge-cluster apply -f stage/tc-proxy-deploy-edge.yaml
kubectl --context=edge-cluster apply -f stage/tc-proxy-http-nodeport.yaml
kubectl --context=edge-cluster apply -f stage/tc-stream-source-cloud.yaml
# Accès dashboard streaming : http://194.199.113.8:31555/
```

### 1. Prérequis
```bash
# Activer l'environnement virtuel
source venv/bin/activate  # ou venv\Scripts\activate sur Windows

# Lancer Redis
sudo service redis-server start

# Lancer Ollama
ollama serve
```

#### Vider Redis avant redémarrage
```bash
redis-cli FLUSHDB
```

### 2. Ordre de lancement (Terminaux séparés)
Il est crucial de respecter cet ordre pour que les health checks du Hub réussissent :

1.  **Base de données :** `python -m services.database.app` (Port 8006)
2.  **Historique :** `python -m services.history_loader.app` (Port 8007)
3.  **Collecteur :** `python -m services.collector.app` (Port 8005)
4.  **Manager de Métriques :** `python -m services.metrics_manager.app` (Port 8004)
5.  **Prédicteur ML :** `python -m services.ml_predictor.app` (Port 8003)
6.  **Intelligence de Décision :** `python -m services.decision_intelligence.app` (Port 8008)
7.  **Client OpenStack :** `python -m infrastructure.openstack_client` (Port 8024)
8.  **Manager d'Intentions :** `python -m services.intent_manager.app` (Port 8002)
9.  **Manager de Latence :** `python -m services.latency_manager.app` (Port 8001)
10. **Observabilité :** `python -m services.observability.app` (Port 8009)
11. **HUB CORE :** `python -m hub.orchestrator_core` (Port 8000)

## 📡 Ports des Services
| Service | Port | Description |
| :--- | :--- | :--- |
| **Hub Core** | 8000 | Orchestrateur central |
| **Latency Manager** | 8001 | Interface avec le PiCar |
| **Intent Manager** | 8002 | Interface LLM / Intentions |
| **ML Predictor** | 8003 | Predictions LSTM/GRU |
| **Metrics Manager** | 8004 | Calcul des seuils et MI |
| **Collector** | 8005 | Collecte sur les VMs |
| **Database** | 8006 | Proxy Redis |
| **History Loader** | 8007 | Fenêtrage des données |
| **Decision Intelligence** | 8008 | Algorithme TOPSIS |
| **Observability** | 8009 | Dashboard Graphique |
| **OpenStack Client** | 8024 | Interface Kubectl/SSH |

## 🔌 API Reference

### Hub Core (Port 8000)
- `POST /rtt` : Réception des mesures RTT depuis le PiCar.
- `POST /intent` : Soumission d'une intention utilisateur.
- `GET /data` : Snapshot complet (métriques + prédictions + décisions).
- `GET /status` : État résumé du système.

### Intent Manager (Port 8002)
- `POST /intent` : Traitement LLM de l'intention.
- `GET /health` : Vérifie Ollama + service.

### Decision Intelligence (Port 8008)
- `POST /decide` : Algorithme TOPSIS + détection violations.

### ML Predictor (Port 8003)
- `POST /predict` : Prédictions LSTM/GRU pour toutes les VMs.

### Collector (Port 8005)
- `POST /collect` : Collecte CPU/RAM sur les 4 VMs.

### OpenStack Client (Port 8024)
- `POST /migrate` : Migration kubectl réelle.
- `GET /active_vm` : VM actuellement active sur kubectl.

## 💡 Exemple d'Utilisation
Pour envoyer une intention de QoS en langage naturel :
```bash
curl -X POST http://localhost:8002/intent \
  -H "Content-Type: application/json" \
  -d '{"intention": "Je veux un flux vidéo très fluide avec une latence < 80ms"}'
```

## 🧪 Tests
```bash
# Lancer tous les tests
pytest tests/

# Tests unitaires uniquement
pytest tests/unit/

# Tests d'intégration
pytest tests/integration/
```

## 📂 Structure du Projet
```text
qos-orchestrator/
├── hub/                    # Hub Central (Orchestrator Core)
├── infrastructure/         # Clients OpenStack, PiCar et ML APIs
├── services/
│   ├── collector/          # Collecte de métriques temps réel
│   ├── database/           # Persistance Redis
│   ├── decision_intelligence/# TOPSIS et détection de violations
│   ├── history_loader/     # Gestion des fenêtres temporelles
│   ├── intent_manager/     # LLM Cascade (Ollama/Regex/Keywords)
│   ├── latency_manager/    # Gestion du RTT applicatif
│   ├── metrics_manager/    # MI Scoring et Seuils Adaptatifs
│   ├── ml_predictor/       # Orchestration des prédictions
│   └── observability/      # Dashboard Matplotlib
├── shared/                 # Configuration et modèles communs
├── tests/                  # Tests unitaires et intégration
└── requirements.txt        # Dépendances Python
```

## 🗺️ Roadmap
- ✅ Pipeline QoS end-to-end opérationnel
- ✅ Migrations kubectl réelles via OpenStack
- ✅ Dashboard temps réel avec prédictions
- ✅ LLM cascade (Ollama/Regex/Keywords)
- ⬜ Conteneurisation Docker + docker-compose
- ⬜ Support multi-utilisateurs et isolation des intents
- ⬜ Tests unitaires complets (TOPSIS, MI, violation_detector)
- ⬜ API REST publique documentée (Swagger)

