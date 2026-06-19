import os
from typing import Dict, Any

# ── Network ───────────────────────────────────────────────────
HUB_HOST: str = os.getenv("HUB_HOST", "localhost")
HUB_PORT: int = int(os.getenv("HUB_PORT", 8000))

# ── Hub endpoints ─────────────────────────────────────────────
HUB_RTT_URL:    str = f"http://{HUB_HOST}:{HUB_PORT}/rtt"
HUB_INTENT_URL: str = f"http://{HUB_HOST}:{HUB_PORT}/intent"
HUB_STATS_URL:  str = f"http://{HUB_HOST}:{HUB_PORT}/status"
CORE_URL:       str = f"http://{HUB_HOST}:{HUB_PORT}"

# ── Ports services ────────────────────────────────────────────
LATENCY_PORT                 = int(os.getenv("LATENCY_PORT",                 8001))
LATENCY_MANAGER_PORT         = LATENCY_PORT
INTENT_MANAGER_PORT          = int(os.getenv("INTENT_MANAGER_PORT",          8002))
ML_PREDICTOR_PORT            = int(os.getenv("ML_PREDICTOR_PORT",            8003))
METRICS_MANAGER_PORT         = int(os.getenv("METRICS_MANAGER_PORT",         8004))
COLLECTOR_PORT               = int(os.getenv("COLLECTOR_PORT",               8005))
DATABASE_PORT                = int(os.getenv("DATABASE_PORT",                8006))
HISTORY_LOADER_PORT          = int(os.getenv("HISTORY_LOADER_PORT",          8007))
DECISION_INTELLIGENCE_PORT   = int(os.getenv("DECISION_INTELLIGENCE_PORT",   8008))
OBSERVABILITY_PORT           = int(os.getenv("OBSERVABILITY_PORT",           8009))
OPENSTACK_CLIENT_PORT        = int(os.getenv("OPENSTACK_CLIENT_PORT",        8024))

# ── URLs services ────────────────────────────────────────────
DATABASE_SERVICE_URL              = f"http://{HUB_HOST}:{DATABASE_PORT}"
COLLECTOR_SERVICE_URL             = f"http://{HUB_HOST}:{COLLECTOR_PORT}"
HISTORY_LOADER_SERVICE_URL        = f"http://{HUB_HOST}:{HISTORY_LOADER_PORT}"
ML_PREDICTOR_SERVICE_URL          = f"http://{HUB_HOST}:{ML_PREDICTOR_PORT}"
METRICS_MANAGER_SERVICE_URL       = f"http://{HUB_HOST}:{METRICS_MANAGER_PORT}"
DECISION_INTELLIGENCE_SERVICE_URL = f"http://{HUB_HOST}:{DECISION_INTELLIGENCE_PORT}"
OPENSTACK_CLIENT_SERVICE_URL      = f"http://{HUB_HOST}:{OPENSTACK_CLIENT_PORT}"

# ── Redis ─────────────────────────────────────────────────────
REDIS_HOST: str = os.getenv("REDIS_HOST",  "127.0.0.1")
REDIS_PORT: int = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB:   int = int(os.getenv("REDIS_DB",   0))

# ── Persistence ───────────────────────────────────────────────
METRICS_TTL:    int = 300
HISTORY_WINDOW: int = 50
DECISIONS_FIFO: int = 50
HISTORY_SIZE:   int = int(os.getenv("HISTORY_SIZE", 10))

# ── Orchestration ─────────────────────────────────────────────
COLLECTION_INTERVAL:  float = float(os.getenv("COLLECTION_INTERVAL",  5.0))
MIGRATION_COOLDOWN_S: float = float(os.getenv("MIGRATION_COOLDOWN_S", 60.0))
BOOTSTRAP_MIN:        int   = int(os.getenv("BOOTSTRAP_MIN",          5))
RAG_TIMEOUT:          float = float(os.getenv("RAG_TIMEOUT",          2.0))

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

# ── Metrics Manager ───────────────────────────────────────────
CV_LOW:               float = float(os.getenv("CV_LOW",              0.15))
CV_HIGH:              float = float(os.getenv("CV_HIGH",             0.30))
PERCENTILE_STABLE:    float = float(os.getenv("PERCENTILE_STABLE",   70.0))
PERCENTILE_NORMAL:    float = float(os.getenv("PERCENTILE_NORMAL",   75.0))
PERCENTILE_VOLATILE:  float = float(os.getenv("PERCENTILE_VOLATILE", 85.0))
MI_RELATIVE_THRESHOLD: float = float(os.getenv("MI_RELATIVE_THRESHOLD", 0.30))

# ── SLO merger ────────────────────────────────────────────────
REFINE_STRICT: float = float(os.getenv("REFINE_STRICT", 0.85))
REFINE_RELAX:  float = float(os.getenv("REFINE_RELAX",  1.15))

# ── Latency / Usage bounds ────────────────────────────────────
LATENCY_MIN: float = float(os.getenv("LATENCY_MIN", 5.0))
LATENCY_MAX: float = float(os.getenv("LATENCY_MAX", 2000.0))
USAGE_MIN:   float = float(os.getenv("USAGE_MIN",   1.0))
USAGE_MAX:   float = float(os.getenv("USAGE_MAX",   99.0))

# ── Ollama / LLM ──────────────────────────────────────────────
OLLAMA_URL:   str = os.getenv("OLLAMA_URL",   "http://localhost:11434")
INTENT_MODEL: str = os.getenv("INTENT_MODEL", "qwen2.5:latest")

# ── ML APIs ───────────────────────────────────────────────────
ML_RTT_URL: str = os.getenv("ML_RTT_URL", "http://localhost:5001/predict")
ML_CPU_URL: str = os.getenv("ML_CPU_URL", "http://localhost:5002/predict")
ML_RAM_URL: str = os.getenv("ML_RAM_URL", "http://localhost:5003/predict")

# ── VMs OpenStack ─────────────────────────────────────────────
VM_REGISTRY: Dict[str, Any] = {
    "edge1":  {"ip": "194.199.113.18", "port": 8200},
    "edge2":  {"ip": "194.199.113.28", "port": 8200},
    "cloud1": {"ip": "194.199.113.66", "port": 8200},
    "cloud2": {"ip": "194.199.113.69", "port": 8200},
}

VM_CLUSTER_MAP: Dict[str, str] = {
    "edge1":  "edge-cluster",
    "edge2":  "edge-cluster",
    "cloud1": "cloud-cluster",
    "cloud2": "cloud-cluster",
}

# ── OpenStack SSH ─────────────────────────────────────────────
OPENSTACK_MASTER_IP: str = os.getenv("OPENSTACK_MASTER_IP", "194.199.113.8")
OPENSTACK_SSH_USER:  str = os.getenv("OPENSTACK_SSH_USER",  "ubuntu")
OPENSTACK_SSH_KEY:   str = os.getenv("OPENSTACK_SSH_KEY",   "admin_log_2.pem")
OPENSTACK_STAGE_DIR: str = os.getenv("OPENSTACK_STAGE_DIR", "~/stage")

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
        "default_threshold":    30.0,
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