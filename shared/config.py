import json
import os
from typing import Dict, Any

# ── Déploiement distribué (multi-provider, multi-stack) ────────
# PROVIDER_ID : quelle partition CE PROCESSUS orchestre. "all" (défaut) =
# comportement mono-processus actuel, inchangé. "provider-1"/"provider-2" =
# ne collecte/orchestre QUE les VMs de ce provider — permet de faire tourner
# deux stacks orchestrateur complètes en parallèle sur le même PC.
PROVIDER_ID: str = os.getenv("PROVIDER_ID", "all")

# PORT_OFFSET : décale les ports PAR-PROVIDER pour que les deux stacks
# coexistent sur localhost sans collision. Les ports PARTAGÉS (relais
# inter-provider, client OpenStack/kubectl) ne sont PAS décalés — un seul
# exemplaire de ces deux services suffit pour les deux stacks.
PORT_OFFSET: int = int(os.getenv("PORT_OFFSET", 0))

# ── Network ───────────────────────────────────────────────────
HUB_HOST: str = os.getenv("HUB_HOST", "localhost")
HUB_PORT: int = int(os.getenv("HUB_PORT", 8000)) + PORT_OFFSET

# ── Hub endpoints ─────────────────────────────────────────────
HUB_RTT_URL:    str = f"http://{HUB_HOST}:{HUB_PORT}/rtt"
HUB_INTENT_URL: str = f"http://{HUB_HOST}:{HUB_PORT}/intent"
HUB_STATS_URL:  str = f"http://{HUB_HOST}:{HUB_PORT}/status"
CORE_URL:       str = f"http://{HUB_HOST}:{HUB_PORT}"

# ── Ports services ────────────────────────────────────────────
# PORT_OFFSET s'applique à tous les ports PAR-PROVIDER ci-dessous, y compris
# le relais (chaque provider a désormais SON relais). Seul le client
# OpenStack/kubectl reste mutualisé et non décalé.
LATENCY_PORT                 = int(os.getenv("LATENCY_PORT",                 8001)) + PORT_OFFSET
LATENCY_MANAGER_PORT         = LATENCY_PORT
INTENT_MANAGER_PORT          = int(os.getenv("INTENT_MANAGER_PORT",          8002)) + PORT_OFFSET
ML_PREDICTOR_PORT            = int(os.getenv("ML_PREDICTOR_PORT",            8003)) + PORT_OFFSET
METRICS_MANAGER_PORT         = int(os.getenv("METRICS_MANAGER_PORT",         8004)) + PORT_OFFSET
COLLECTOR_PORT               = int(os.getenv("COLLECTOR_PORT",               8005)) + PORT_OFFSET
DATABASE_PORT                = int(os.getenv("DATABASE_PORT",                8006)) + PORT_OFFSET
HISTORY_LOADER_PORT          = int(os.getenv("HISTORY_LOADER_PORT",          8007)) + PORT_OFFSET
DECISION_INTELLIGENCE_PORT   = int(os.getenv("DECISION_INTELLIGENCE_PORT",   8008)) + PORT_OFFSET
OBSERVABILITY_PORT           = int(os.getenv("OBSERVABILITY_PORT",           8009)) + PORT_OFFSET
PLACEMENT_ARBITER_PORT       = int(os.getenv("PLACEMENT_ARBITER_PORT",       8011)) + PORT_OFFSET
OPENSTACK_CLIENT_PORT        = int(os.getenv("OPENSTACK_CLIENT_PORT",        8024))

# Relais : port PAR-PROVIDER (décalé), mais _RELAY_BASE_PORT sert aussi de
# valeur par défaut à PROVIDER_RELAY_URLS ci-dessous (mono-processus : les
# deux providers pointent sur l'unique relais de base).
_RELAY_BASE_PORT = 8010
PROVIDER_RELAY_PORT = int(os.getenv("PROVIDER_RELAY_PORT", _RELAY_BASE_PORT)) + PORT_OFFSET

# ── URLs services ────────────────────────────────────────────
DATABASE_SERVICE_URL              = f"http://{HUB_HOST}:{DATABASE_PORT}"
COLLECTOR_SERVICE_URL             = f"http://{HUB_HOST}:{COLLECTOR_PORT}"
HISTORY_LOADER_SERVICE_URL        = f"http://{HUB_HOST}:{HISTORY_LOADER_PORT}"
ML_PREDICTOR_SERVICE_URL          = f"http://{HUB_HOST}:{ML_PREDICTOR_PORT}"
METRICS_MANAGER_SERVICE_URL       = f"http://{HUB_HOST}:{METRICS_MANAGER_PORT}"
DECISION_INTELLIGENCE_SERVICE_URL = f"http://{HUB_HOST}:{DECISION_INTELLIGENCE_PORT}"
OPENSTACK_CLIENT_SERVICE_URL      = f"http://{HUB_HOST}:{OPENSTACK_CLIENT_PORT}"
PROVIDER_RELAY_SERVICE_URL        = f"http://{HUB_HOST}:{PROVIDER_RELAY_PORT}"
PLACEMENT_ARBITER_SERVICE_URL     = f"http://{HUB_HOST}:{PLACEMENT_ARBITER_PORT}"

# ── Redis ─────────────────────────────────────────────────────
REDIS_HOST: str = os.getenv("REDIS_HOST",  "127.0.0.1")
REDIS_PORT: int = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB:   int = int(os.getenv("REDIS_DB",   0))

# ── Persistence ───────────────────────────────────────────────
# Fenêtre lue par le calcul MI. Ramenée de 50 à 25 : à 6 s/cycle, 50 points
# couvrent 2 migrations, donc majoritairement des cycles où la VM était LOIN et
# n'hébergeait pas le service — violation certaine quelle que soit la charge, et
# MI saturée à 0. 25 points restent dans l'épisode d'hébergement courant.
HISTORY_WINDOW:   int = int(os.getenv("HISTORY_WINDOW", 25))

# Fenêtre lue par les PRÉDICTIONS ML — distincte de celle du MI, car les deux
# ont des besoins OPPOSÉS :
#   • MI  : fenêtre COURTE (25). Plus long couvre plusieurs migrations, donc
#           des cycles où la VM était loin : violation certaine, MI saturée à 0.
#   • ML  : fenêtre LONGUE. predictor.py n'atteint le Niveau 1 (predict_sequence)
#           que si len(history) >= window_size du modèle — 39 pour delay, 45
#           pour cpu et ram. En dessous, il retombe au Niveau 2 (point unique)
#           puis au repli, qui recopie la mesure : la prédiction devient
#           l'observation et toute anticipation disparaît.
# Les deux ont partagé HISTORY_WINDOW jusqu'au 24/08/2026 ; le ramener à 25
# pour le MI a rendu le Niveau 1 inatteignable. Garder cette valeur >= au plus
# grand window_size des trois modèles (curl :5001..5003/hyperparameters).
ML_HISTORY_WINDOW: int = int(os.getenv("ML_HISTORY_WINDOW", 60))

# Profondeur CONSERVÉE dans Redis (LTRIM de redis_client.store_metrics).
# Doit couvrir le plus gourmand des deux lecteurs, sinon la base tronque et
# aucune fenêtre plus longue n'est servable : ramener HISTORY_WINDOW à 25 avait
# tronqué le stockage à 25 points, rendant les 60 du ML impossibles à obtenir
# quoi qu'on demande au history_loader.
METRICS_RETENTION: int = max(HISTORY_WINDOW, ML_HISTORY_WINDOW)
DECISIONS_FIFO:   int = 50
HISTORY_SIZE:     int = int(os.getenv("HISTORY_SIZE", 100))

# ── Suffixe par provider (classeurs Excel) ────────────────────
# Chaque stack écrit dans SON propre classeur. Sans ce suffixe, les deux
# providers ouvrent et réécrivent le MÊME fichier en boucle : openpyxl le
# relit corrompu (« Bad magic number for file header »), le recrée, et des
# lignes de mesure sont perdues à chaque collision.
_TIMING_SUFFIX: str = "" if PROVIDER_ID == "all" else f"_{PROVIDER_ID.replace('-', '')}"

# ── Excel export ──────────────────────────────────────────────
EXCEL_PATH:   str = os.getenv("EXCEL_PATH",   f"data/qos_history{_TIMING_SUFFIX}.xlsx")
EXCEL_MAX_MB: int = int(os.getenv("EXCEL_MAX_MB", 200))

# ── Profilage / mesures de performance ────────────────────────
# Un fichier par mode (autonomous → par cycle, enhanced → par intention).
TIMING_EXCEL_AUTONOMOUS_PATH: str = os.getenv(
    "TIMING_EXCEL_AUTONOMOUS_PATH", f"data/timings_autonomous{_TIMING_SUFFIX}.xlsx"
)
TIMING_EXCEL_ENHANCED_PATH:   str = os.getenv(
    "TIMING_EXCEL_ENHANCED_PATH", f"data/timings_enhanced{_TIMING_SUFFIX}.xlsx"
)
TIMING_EXCEL_MAX_MB:          int = int(os.getenv("TIMING_EXCEL_MAX_MB", 100))

# ── Orchestration ─────────────────────────────────────────────
COLLECTION_INTERVAL:  float = float(os.getenv("COLLECTION_INTERVAL",  2.0))
MIGRATION_COOLDOWN_S: float = float(os.getenv("MIGRATION_COOLDOWN_S", 5.0))
BOOTSTRAP_MIN:        int   = int(os.getenv("BOOTSTRAP_MIN",          5))
RAG_TIMEOUT:          float = float(os.getenv("RAG_TIMEOUT",          2.0))

# Fréquence du « battement de cœur » de synchronisation kubectl.
# La synchronisation a lieu si une violation primaire est détectée
# (on doit alors savoir si l'on est ACTIF) OU tous les N cycles, ce qui
# permet à un orchestrateur STANDBY de découvrir qu'il vient de recevoir
# le service après une migration inter-provider.
ACTIVE_VM_SYNC_EVERY_N_CYCLES: int = max(1, int(
    os.getenv("ACTIVE_VM_SYNC_EVERY_N_CYCLES", 10)
))

# Fenêtre de grâce après un /award reçu : le temps que kubectl propage
# effectivement la migration (delete + apply sur le cluster cible) avant
# que _sync_active_vm ne recommence à faire autorité sur le rôle actif.
# Sans elle, un sync qui tombe juste après l'award peut lire une VM
# active encore périmée côté kubectl et démettre à tort le provider qui
# vient d'être promu.
AWARD_GRACE_PERIOD_S: float = float(os.getenv("AWARD_GRACE_PERIOD_S", 15.0))

# ── Arbitrage de placement (services/placement_arbiter) ────────
# Sévérité du contrat SLO appliquée par l'arbitre.
#   "hard" : aucun placement non conforme n'est jamais élu ; si aucun
#            provider n'est conforme → STAY + alerte d'infaisabilité.
#   "soft" : le meilleur best-effort peut être élu (dégradation gracieuse).
SLO_ENFORCEMENT: str = os.getenv("SLO_ENFORCEMENT", "hard").lower()

# Chemin MONO-PROVIDER, aucune VM locale ne satisfait les SLOs → STAY.
#
# Le repli « best_effort » (élire la moins mauvaise VM) a été RETIRÉ le
# 24/08/2026 : migrer vers une VM qui viole elle aussi le contrat déplace le
# service sans le rétablir, et le chemin fédéré ne le fait pas — un provider
# sans VM conforme ne soumet AUCUNE offre (_build_local_bid, cas B) et
# l'arbitre renvoie STAY + alerte INFAISABLE. Les deux chemins appliquent
# désormais la même règle de dernier recours.
#
# ⚠️ Les données de la campagne UC2 ont été produites avec l'ancien repli.
# Elles ne sont plus reproductibles avec ce code — les rejouer suppose de
# restaurer la politique retirée.

# Écart de Gap Grade minimal pour qu'un challenger arrache le service au
# provider en place. ABSOLU (et non relatif) : le Gap Grade étant déjà un
# écart relatif au seuil, une marge en pourcentage n'exigerait presque rien
# près du seuil et beaucoup trop loin de lui. 0.05 sur un seuil de 40 ms
# = « il faut gagner plus de 2 ms ».
#
# NOTE : hub/provider_arbitration.py possède déjà NEGOTIATION_DEADBAND (0.05)
# pour l'ANCIEN chemin 2-way — les deux coexistent jusqu'au lot 6, qui
# retirera l'ancien chemin. Ne pas les confondre ni les fusionner ici.
ARBITER_DEADBAND: float = float(os.getenv("ARBITER_DEADBAND", 0.05))

# Délai d'attente du message d'attribution (award, lot 7). Court par
# construction : l'award est une OPTIMISATION du suivi de placement, jamais
# une dependance dure. Son echec fait retomber le gagnant sur la decouverte
# par kubectl, c'est-a-dire le comportement anterieur au lot 7.
AWARD_TIMEOUT_S: float = float(os.getenv("AWARD_TIMEOUT_S", 3.0))

# ── Vue de fédération (lot 8a) ──────────────────────────────────
# Vue de fédération — service UNIQUE, lancé une seule fois pour l'ensemble
# des providers. Son port ne prend donc PAS PORT_OFFSET, contrairement aux
# services de stack (même logique qu'openstack_client).
FEDERATION_VIEW_PORT: int = int(os.getenv("FEDERATION_VIEW_PORT", 8500))

# Cibles interrogées par la vue de fédération. Ajouter un provider N+1 =
# ajouter UNE entrée ici, rien d'autre dans tout le service.
FEDERATION_VIEW_TARGETS: Dict[str, Dict[str, str]] = {
    "provider-1": {
        "hub":            os.getenv("FV_HUB_P1", "http://localhost:8000"),
        "observability":  os.getenv("FV_OBS_P1", "http://localhost:8009"),
        "intent_manager": os.getenv("FV_IM_P1",  "http://localhost:8002"),
    },
    "provider-2": {
        "hub":            os.getenv("FV_HUB_P2", "http://localhost:8100"),
        "observability":  os.getenv("FV_OBS_P2", "http://localhost:8109"),
        "intent_manager": os.getenv("FV_IM_P2",  "http://localhost:8102"),
    },
}

# ── Décision proactive ────────────────────────────────────────
PROACTIVE_FACTOR: float = float(os.getenv("PROACTIVE_FACTOR", 0.85))
HORIZON_ALERT:    int   = int(os.getenv("HORIZON_ALERT",       3))

# ── HTTP retry ────────────────────────────────────────────────
POST_RETRY_COUNT:   int   = int(os.getenv("POST_RETRY_COUNT",    3))
POST_RETRY_BACKOFF: float = float(os.getenv("POST_RETRY_BACKOFF", 2.0))
POST_TIMEOUT:       float = float(os.getenv("POST_TIMEOUT",       5.0))

# ── Collector EMA ─────────────────────────────────────────────
COLLECTOR_TIMEOUT_BASE:      float = float(os.getenv("COLLECTOR_TIMEOUT_BASE",      2.0))
COLLECTOR_MIN_TIMEOUT:       float = float(os.getenv("COLLECTOR_MIN_TIMEOUT",       0.5))
COLLECTOR_MAX_TIMEOUT:       float = float(os.getenv("COLLECTOR_MAX_TIMEOUT",       5.0))
COLLECTOR_TIMEOUT_FACTOR:    float = float(os.getenv("COLLECTOR_TIMEOUT_FACTOR",    1.5))
COLLECTOR_RELIABILITY_ALPHA: float = float(os.getenv("COLLECTOR_RELIABILITY_ALPHA", 0.2))
# Intervalle du sondage de fond des VMs (découplé du cycle d'orchestration —
# voir services/collector/collector.py). /collect lit le cache au lieu
# d'attendre le round-trip réseau vers les 4 VMs à chaque cycle.
COLLECTOR_POLL_INTERVAL:     float = float(os.getenv("COLLECTOR_POLL_INTERVAL",     1.0))

# ── Metrics Manager ───────────────────────────────────────────
CV_LOW:               float = float(os.getenv("CV_LOW",              0.15))
CV_HIGH:              float = float(os.getenv("CV_HIGH",             0.30))
PERCENTILE_STABLE:    float = float(os.getenv("PERCENTILE_STABLE",   70.0))
PERCENTILE_NORMAL:    float = float(os.getenv("PERCENTILE_NORMAL",   75.0))
PERCENTILE_VOLATILE:  float = float(os.getenv("PERCENTILE_VOLATILE", 85.0))
MI_RELATIVE_THRESHOLD: float = float(os.getenv("MI_RELATIVE_THRESHOLD", 0.15))

# Nombre de cycles pendant lesquels un score MI reste valable quand la fenêtre
# d'historique ne permet plus de le recalculer (aucun contraste : tous les
# cycles en violation, ou aucun — mesuré sur 25 % des cycles). Au-delà,
# l'information est trop vieille et le score retombe à 0. À 6 s/cycle, 10
# cycles ≈ 1 minute. Voir MetricsHandler.compute_mi_scores.
MI_HOLD_CYCLES: int = int(os.getenv("MI_HOLD_CYCLES", 10))

# Planchers de disponibilité absolue pour les SLOs secondaires cpu/ram en
# mode AUTONOMOUS (percentile adaptatif désactivé pour ces deux métriques,
# cf. metrics_handler._capacity_floor). Cohérent avec la convention déjà
# utilisée par le LLM en mode ENHANCED (operator ">=", unit "cores"/"GB").
AUTONOMOUS_CPU_FLOOR_CORES: float = float(os.getenv("AUTONOMOUS_CPU_FLOOR_CORES", 1.0))
AUTONOMOUS_RAM_FLOOR_GB:    float = float(os.getenv("AUTONOMOUS_RAM_FLOOR_GB", 1.0))

# ── SLO merger ────────────────────────────────────────────────
REFINE_STRICT: float = float(os.getenv("REFINE_STRICT", 0.85))
REFINE_RELAX:  float = float(os.getenv("REFINE_RELAX",  1.15))

# ── Latency / Usage bounds ────────────────────────────────────
LATENCY_MIN: float = float(os.getenv("LATENCY_MIN", 5.0))
LATENCY_MAX: float = float(os.getenv("LATENCY_MAX", 2000.0))
USAGE_MIN:   float = float(os.getenv("USAGE_MIN",   1.0))
USAGE_MAX:   float = float(os.getenv("USAGE_MAX",   99.0))

# ── Ollama / LLM (local fallback) ────────────────────────────
OLLAMA_URL:   str = os.getenv("OLLAMA_URL",   "http://localhost:11434")
INTENT_MODEL: str = os.getenv("INTENT_MODEL", "qwen2.5:latest")

# ── LAAS vLLM (primary) ───────────────────────────────────────
LAAS_LLM_URL:   str = os.getenv("LAAS_LLM_URL",   "https://pfcalcul.laas.fr/vllm/v1/chat/completions")

LAAS_MODEL:     str = os.getenv("LAAS_MODEL",      "Qwen3/Qwen--Qwen3.8-27B-FP16")
LAAS_LLM_PROXY: str = os.getenv("LAAS_LLM_PROXY", "")  # e.g. https://user:pass@proxy.laas.fr:443

# ── Seuils de latence ancrés sur les mesures (17/08/2026) ─────
# OFF par défaut : le prompt reste MOT POUR MOT celui des campagnes d'août,
# donc aucun run n'est invalidé par ce code.
#
# Problème corrigé quand ON : la règle 2 du prompt impose au LLM la bande
# « temps réel ≈ 50-100 ms », un a priori issu du web et sans rapport avec
# ce banc (latence edge 5-150 ms, rayon de conformité 28 ms). Conséquence
# mesurée : « reduce latency as much as possible » produit `latency < 50`,
# soit PLUS PERMISSIF que le défaut autonome de 28 ms — l'intention relâche
# le contrat au lieu de le durcir. Que la valeur vienne d'un a priori et non
# d'un raisonnement est établi : LAAS Qwen3.6-27B (14/08) et Ollama
# qwen2.5-7B (17/08) ont répondu 50.0/45.0 au dixième près.
#
# ON : les percentiles RÉELLEMENT MESURÉS sur le parc sont injectés dans le
# prompt, et le LLM place son seuil dedans. Il garde la sémantique (quelle
# métrique, quel sens, quelle importance) ; le banc fournit l'échelle.
# Volontairement, aucun verrou de non-régression n'est ajouté : il écraserait
# les relâchements LÉGITIMES (« encrypt even if it increases latency » doit
# pouvoir monter à 200 ms).
INTENT_GROUNDED_THRESHOLDS: bool = os.getenv(
    "INTENT_GROUNDED_THRESHOLDS", "false"
).lower() == "true"

# Nombre de points par VM lus pour établir la distribution. En dessous de
# GROUNDED_MIN_SAMPLES points au total, l'ancrage est abandonné et le prompt
# d'origine est utilisé — un percentile sur 4 points ne veut rien dire.
GROUNDED_HISTORY_SIZE: int = int(os.getenv("GROUNDED_HISTORY_SIZE", 50))
GROUNDED_MIN_SAMPLES:  int = int(os.getenv("GROUNDED_MIN_SAMPLES",  40))

# ── ML APIs ───────────────────────────────────────────────────
ML_RTT_URL: str = os.getenv("ML_RTT_URL", "http://localhost:5001/predict")
ML_CPU_URL: str = os.getenv("ML_CPU_URL", "http://localhost:5002/predict")
ML_RAM_URL: str = os.getenv("ML_RAM_URL", "http://localhost:5003/predict")

# ── VMs OpenStack ─────────────────────────────────────────────
# Source globale, inchangée quel que soit PROVIDER_ID — décrit le parc
# complet des 8 VMs. VM_REGISTRY (dérivé plus bas, après PROVIDER_REGISTRY)
# est ce que CE processus collecte/orchestre réellement.
_ALL_VM_REGISTRY_DEFAUT: Dict[str, Any] = {
    "edge1":  {"ip": "194.199.113.18", "port": 8200},
    "edge1b": {"ip": "194.199.113.18", "port": 8201},
    "edge1c": {"ip": "194.199.113.18", "port": 8202},
    "edge2":  {"ip": "194.199.113.28", "port": 8200},
    "edge2b": {"ip": "194.199.113.28", "port": 8201},
    "edge2c": {"ip": "194.199.113.28", "port": 8202},
    "cloud1": {"ip": "194.199.113.66", "port": 8200},
    "cloud2": {"ip": "194.199.113.69", "port": 8200},
}

# Surcharge par environnement — permet de pointer l'orchestrateur sur des
# VMs simulees en local (meme parc, memes ids, ports differents) sans
# toucher au code. Variable absente => parc reel, comportement inchange.
_registry_json = os.getenv("ALL_VM_REGISTRY_JSON", "").strip()
if _registry_json:
    ALL_VM_REGISTRY: Dict[str, Any] = json.loads(_registry_json)
    manquantes = set(_ALL_VM_REGISTRY_DEFAUT) - set(ALL_VM_REGISTRY)
    if manquantes:
        raise ValueError(
            f"ALL_VM_REGISTRY_JSON incomplet — VMs manquantes : "
            f"{sorted(manquantes)}"
        )
else:
    ALL_VM_REGISTRY = _ALL_VM_REGISTRY_DEFAUT

VM_CLUSTER_MAP: Dict[str, str] = {
    "edge1":  "edge-cluster",
    "edge1b": "edge-cluster",
    "edge1c": "edge-cluster",
    "edge2":  "edge-cluster",
    "edge2b": "edge-cluster",
    "edge2c": "edge-cluster",
    "cloud1": "cloud-cluster",
    "cloud2": "cloud-cluster",
}

# ── Providers (partition transversale) ────────────────────────
# Axe PROPRIÉTÉ (business), ORTHOGONAL à l'axe CLUSTER/TIER physique
# (VM_CLUSTER_MAP ci-dessus). Un provider possède son propre parc mixte
# edge+cloud et ignore les VMs des autres. Le cluster reste une propriété
# de la VM, jamais du provider.
#
# Purement DÉCLARATIF : aucun service ne lit encore ce registre à ce stade
# → zéro changement de comportement runtime.
PROVIDER_REGISTRY: Dict[str, Any] = {
    "provider-1": {"vms": ["edge1", "edge1b", "edge1c", "cloud1"]},
    "provider-2": {"vms": ["edge2", "edge2b", "edge2c", "cloud2"]},
}

# Dérivé (source unique = PROVIDER_REGISTRY) : lookup inverse VM → provider.
PROVIDER_OF_VM: Dict[str, str] = {
    vm_id: provider_id
    for provider_id, profile in PROVIDER_REGISTRY.items()
    for vm_id in profile["vms"]
}

# Groupe de placement RÉEL (node Kubernetes) de chaque VM. Les VMs
# simulées d'une même machine physique partagent le MÊME node : kubectl
# ne peut pas les distinguer et renvoie toujours la VM canonique du node
# (voir NODE_VM_MAP dans openstack_client.py, côté master).
#
# Sert à savoir QUAND la réponse de kubectl est moins précise que notre
# propre suivi : si notre service_vm est sur le MÊME node que la VM
# renvoyée par kubectl, notre valeur est la plus fine et doit être
# conservée. Sinon, kubectl fait autorité.
#
# ⚠️ Cette table DOIT rester le miroir exact de NODE_VM_MAP côté master.
VM_NODE_GROUP: Dict[str, str] = {
    "edge1":  "pop1-worker-1", "edge1b": "pop1-worker-1", "edge1c": "pop1-worker-1",
    "edge2":  "pop1-worker-2", "edge2b": "pop1-worker-2", "edge2c": "pop1-worker-2",
    "cloud1": "pop2-worker-1",
    "cloud2": "pop2-worker-2",
}

# VM_REGISTRY : parc effectivement collecté/orchestré par CE processus.
# "all" (défaut) → tout le parc (ALL_VM_REGISTRY), comportement historique
# inchangé. Sinon → uniquement les VMs du provider ciblé, PROVIDER_REGISTRY
# faisant foi sur l'appartenance (permet à deux stacks orchestrateur de
# coexister, chacune ne voyant que son provider).
if PROVIDER_ID == "all":
    VM_REGISTRY: Dict[str, Any] = ALL_VM_REGISTRY
elif PROVIDER_ID in PROVIDER_REGISTRY:
    VM_REGISTRY = {
        vm_id: ALL_VM_REGISTRY[vm_id]
        for vm_id in PROVIDER_REGISTRY[PROVIDER_ID]["vms"]
    }
else:
    raise ValueError(
        f"PROVIDER_ID={PROVIDER_ID!r} inconnu — attendu 'all' ou une clé de "
        f"PROVIDER_REGISTRY ({sorted(PROVIDER_REGISTRY.keys())})"
    )

# ── Routage inter-orchestrateurs (passerelle de fédération) ───
# Adresse de l'orchestrateur responsable de chaque provider. En mono-processus,
# les deux rôles sont joués par le MÊME hub : les deux entrées pointent donc
# sur lui. Passer à N orchestrateurs réels = changer ces URLs, et RIEN d'autre
# dans tout le projet — c'est le seul endroit qui connaît la topologie.
PROVIDER_ORCHESTRATOR_URL: Dict[str, str] = {
    provider_id: os.getenv(
        f"ORCHESTRATOR_URL_{provider_id.upper().replace('-', '_')}",
        f"http://{HUB_HOST}:{HUB_PORT}",
    )
    for provider_id in PROVIDER_REGISTRY
}

# Adresse du RELAIS de chaque provider (relais → relais, jamais relais → hub
# d'un pair) : le hub d'un provider n'est joignable que par SON propre
# relais, en localhost. Défaut sûr pour le mono-processus : les deux
# entrées pointent sur l'unique relais de base (:8010) — le distribué
# surcharge via RELAY_URL_PROVIDER_1/2 (une adresse par provider réel).
PROVIDER_RELAY_URLS: Dict[str, str] = {
    "provider-1": os.getenv("RELAY_URL_PROVIDER_1", f"http://{HUB_HOST}:{_RELAY_BASE_PORT}"),
    "provider-2": os.getenv("RELAY_URL_PROVIDER_2", f"http://{HUB_HOST}:{_RELAY_BASE_PORT}"),
}

# Interrupteur de la machine à états multi-provider dans _step8_decide (hub).
# OFF (défaut) : le cycle se comporte EXACTEMENT comme avant cette extension
# (chemin _decide_mono_provider, code d'origine inchangé). ON : bascule vers
# _decide_multi_provider (évaluation par provider, passation inter-provider
# si besoin). Défaut à False pour ne jamais changer le comportement en
# production tant que la fonctionnalité n'est pas explicitement activée.
MULTI_PROVIDER_ENABLED: bool = os.getenv("MULTI_PROVIDER_ENABLED", "false").lower() == "true"

# Profil de pondération de l'horizon dans calculate_weighted_mean (TOPSIS).
# Vide (défaut) = poids décroissants [n, n-1, …, 1], comportement d'origine :
# la valeur agrégée est dominée par le pas le plus proche.
#
# Format : entiers séparés par des virgules, du pas le PLUS PROCHE au plus
# LOINTAIN. Ex. "1,2,3,4,5,6,7" inverse le profil et porte l'horizon pondéré
# de 18 s à 30 s.
#
# Mesuré le 27/08/2026 par rejeu hors ligne de l'extrapolation linéaire sur
# les 112 débuts de violation de la campagne LAAS (UC1, FED1-3, 2 providers),
# retard de fraîcheur de 13 s inclus dans le calcul de l'avance :
#   [7,6,5,4,3,2,1] (actuel)  → 75,9 % détectés à l'avance, avance méd. 11,0 s
#   [1,2,3,4,5,6,7] (inversé) → 96,4 % détectés à l'avance, avance méd. 22,7 s
# Fausses alarmes nulles dans les deux cas.
#
# ⚠️ Ce gain suppose un prédicteur capable de voir venir un franchissement.
# Le GRU seul ne le fait PAS (rappel 0 % à tous les horizons) : changer ces
# poids sans l'extrapolation linéaire ne donne rien — c'est précisément ce
# qu'a montré RUN9, qui n'avait modifié que la pondération.
TOPSIS_HORIZON_WEIGHTS: str = os.getenv("TOPSIS_HORIZON_WEIGHTS", "").strip()

# Extrapolation linéaire proposée EN PLUS du GRU dans ml_predictor : à chaque
# appel les deux prédisent, on garde celui dont l'erreur récente à t+1 est la
# plus faible. Le GRU reste calculé et mesuré dans les deux cas.
#
# Défaut FALSE : les exécutions de réplication doivent tourner sur le MÊME
# prédicteur que la campagne de référence (UC1, FED1-3, UC2, ABL1-3). Laisser
# cette option active par défaut rendrait toute nouvelle exécution
# incomparable aux précédentes — sans aucun signe visible.
#
# Mesuré le 27/08/2026 sur les 112 débuts de violation de la campagne LAAS :
# le GRU ne détecte JAMAIS un franchissement à venir (rappel 0 % de t+1 à
# t+7) ; l'extrapolation linéaire sur les 3 derniers points le détecte à
# 100 % jusqu'à t+6 (94,7 % à t+7), avec 0,0–0,2 % de fausses alarmes.
ML_LINEAR_EXTRAPOLATION: bool = os.getenv(
    "ML_LINEAR_EXTRAPOLATION", "false"
).strip().lower() == "true"

# Capacité physique (total_cores, total_ram_gb) : plus fixée ici. Chaque VM
# la déclare elle-même via son propre /metrics (logique fédération/service
# mesh — chaque provider annonce sa capacité). Le collector la propage et
# TopsisSelector la lit directement sur le candidat. Voir vm_ping scripts.

# ── OpenStack SSH ─────────────────────────────────────────────
OPENSTACK_MASTER_IP: str = os.getenv("OPENSTACK_MASTER_IP", "194.199.113.8")
OPENSTACK_SSH_USER:  str = os.getenv("OPENSTACK_SSH_USER",  "ubuntu")
OPENSTACK_SSH_KEY:   str = os.getenv("OPENSTACK_SSH_KEY",   "admin_log_2.pem")
OPENSTACK_STAGE_DIR: str = os.getenv("OPENSTACK_STAGE_DIR", "~/stage")

# ── Push d'état vers le bridge PiCar ──────────────────────────
# Le hub ACTIF pousse sa VM de service au bridge PiCar à la fin de chaque
# cycle (fire-and-forget, jamais bloquant — même contrat que _post_audit
# vers observability). Sans ce push, le bridge doit interroger les deux
# hubs en polling et retombe sur la VM CANONIQUE de kubectl (edge2 au lieu
# de edge2b) dès qu'un hub tarde à répondre. Mettre à "" pour désactiver.
PICAR_BRIDGE_URL: str = os.getenv("PICAR_BRIDGE_URL", "http://140.93.64.105:8080")

# ─────────────────────────────────────────────────────────────
# Metrics Registry — architecture primaire/secondaire
#
# Sémantique des champs :
#   • default_threshold      : seuil métier FIXE pour les SLOs primaires.
#                              Utilisé UNIQUEMENT pour les métriques marquées
#                              is_primary_objective=True. Pour les autres,
#                              le seuil est calculé dynamiquement par
#                              percentile adaptatif quand MI détecte
#                              une corrélation.
#   • is_primary_objective   : True  = objectif métier fixe non négociable
#                                      (ex: latency en mode Autonomous).
#                              False = candidate pour SLO secondaire
#                                      adaptatif via Information Mutuelle.
#   • always_active          : True  = métrique toujours collectée par
#                                      le collector, indépendamment de MI.
#   • bounds                 : bornes physiques min/max appliquées par
#                              clamp à TOUT seuil (fixe ou adaptatif).
# ─────────────────────────────────────────────────────────────

METRICS_REGISTRY: Dict[str, Any] = {
    "latency": {
        "payload_key":          "rtt_ms",
        "unit":                 "ms",
        "operator":             "<",
        "default_threshold":    28.0,
        "bounds":               {"min": 5.0, "max": 2000.0},
        "always_active":        True,
        "is_primary_objective": True,
    },
    "cpu_usage": {
        "payload_key":          "cpu_usage",
        "unit":                 "%",
        "operator":             "<",
        "default_threshold":    80.0,
        "bounds":               {"min": 1.0, "max": 99.0},
        "always_active":        False,
        "is_primary_objective": False,
    },
    "ram_usage": {
        "payload_key":          "ram_usage",
        "unit":                 "%",
        "operator":             "<",
        "default_threshold":    80.0,
        "bounds":               {"min": 1.0, "max": 99.0},
        "always_active":        False,
        "is_primary_objective": False,
    },
}
