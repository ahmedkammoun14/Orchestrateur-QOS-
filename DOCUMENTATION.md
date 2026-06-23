# QoS Orchestrator — Documentation Technique Détaillée

**Projet** : Orchestration QoS adaptative pour services distribués sur infrastructure OpenStack/Kubernetes  
**Auteur** : Ahmed Kammoun — ahmed.kammoun@enis.tn  
**Date** : Juin 2026  
**Version** : 2.4.0

---

## Table des matières

1. [Vue d'ensemble](#1-vue-densemble)
2. [Architecture Hub-and-Spoke](#2-architecture-hub-and-spoke)
3. [Carte des services](#3-carte-des-services)
4. [Flux d'orchestration — Les 8 étapes](#4-flux-dorchestration--les-8-étapes)
5. [Modes de fonctionnement](#5-modes-de-fonctionnement)
6. [Calcul des SLOs — Metrics Manager](#6-calcul-des-slos--metrics-manager)
7. [Décision TOPSIS](#7-décision-topsis)
8. [Détection de violations — Proactivité ML](#8-détection-de-violations--proactivité-ml)
9. [Traçabilité des cycles](#9-traçabilité-des-cycles)
10. [Observabilité — Dashboard SSE](#10-observabilité--dashboard-sse)
11. [Module Intent Manager (mode ENHANCED)](#11-module-intent-manager-mode-enhanced)
12. [Infrastructure OpenStack / Kubernetes](#12-infrastructure-openstack--kubernetes)
13. [Persistance — Redis & Excel](#13-persistance--redis--excel)
14. [Configuration](#14-configuration)
15. [Démarrage du système](#15-démarrage-du-système)
16. [Démo PiCar-X — Latence basée sur la position](#16-démo-picar-x--latence-basée-sur-la-position)
17. [Résultats de validation](#17-résultats-de-validation)

---

## 1. Vue d'ensemble

Le QoS Orchestrator est un système d'orchestration autonome qui surveille en continu la qualité de service (QoS) d'un service applicatif déployé sur un cluster Kubernetes/OpenStack et décide automatiquement de migrer le service vers la VM candidate optimale lorsqu'une violation de SLO est détectée.

### Principes clés

- **Proactivité ML** : le type de violation est `"proactive"` dès que les prédictions ML confirment ou anticipent le breach — même si la valeur courante est déjà en violation. Le label `"réactif"` n'apparaît que si aucune prédiction n'est disponible.
- **MI k-NN différentiel** : l'Information Mutuelle est calculée par l'estimateur continu de Kozachenko-Leonenko (k plus proches voisins) — pas de discrétisation, détecte les dépendances non-linéaires.
- **Traçabilité des cycles** : le numéro de cycle est propagé du Hub vers `metrics_manager` ET `decision_intelligence`. Les deux terminaux affichent `[Cycle #N]` pour montrer comment les scores MI d'un cycle deviennent les poids TOPSIS du même cycle.
- **Adaptativité** : les seuils secondaires sont calculés dynamiquement par percentile adaptatif (P70/P75/P85 selon la volatilité du signal).
- **Observabilité SSE** : dashboard temps réel avec Server-Sent Events (SSE) sur port 8009 — métriques VM, prédictions, graphiques, audit log complet.
- **Interaction naturelle** : en mode ENHANCED, un LLM extrait les SLOs directement depuis une intention en langage naturel.

---

## 2. Architecture Hub-and-Spoke

```
┌─────────────────────────────────────────────────────────────────────┐
│                         QoS Orchestrator                            │
│                                                                     │
│   ┌──────────────┐     ┌─────────────────────────────────────┐     │
│   │ Latency Mgr  │────▶│                                     │     │
│   │  :8001       │ RTT │           HUB / Core                │     │
│   └──────────────┘     │         :8000                       │     │
│                        │   (orchestrator_core.py)            │     │
│   ┌──────────────┐     │                                     │     │
│   │ Intent Mgr   │────▶│  • État global (OrchestratorState) │     │
│   │  :8002       │INTENT│  • 8 étapes de cycle              │     │
│   └──────────────┘     │  • Cooldown anti-thrashing (60s)   │     │
│                        │  • Bootstrap (5 cycles)             │     │
│                        └──────────────┬──────────────────────┘     │
│                                       │ appels HTTP                 │
│              ┌────────────────────────┼──────────────────┐         │
│              ▼                        ▼                   ▼         │
│   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐ │
│   │   Collector      │  │  ML Predictor    │  │ Metrics Manager  │ │
│   │   :8005          │  │  :8003           │  │  :8004           │ │
│   │ (CPU, RAM/VMs)   │  │ (ARIMA/ETS/Lin.) │  │ (MI, SLOs)       │ │
│   └──────────────────┘  └──────────────────┘  └──────────────────┘ │
│              │                        │                   │         │
│              ▼                        ▼                   ▼         │
│   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐ │
│   │   Database       │  │ History Loader   │  │Decision Intel.   │ │
│   │   :8006          │  │  :8007           │  │  :8008           │ │
│   │ (Redis)          │  │ (Redis → hist.)  │  │ (TOPSIS)         │ │
│   └──────────────────┘  └──────────────────┘  └──────────────────┘ │
│                                                                     │
│   ┌──────────────────┐  ┌──────────────────┐                       │
│   │  Observability   │  │OpenStack Client  │                       │
│   │  :8009           │  │  :8024           │                       │
│   │ (dashboard)      │  │ (kubectl/migrate) │                      │
│   └──────────────────┘  └──────────────────┘                       │
└─────────────────────────────────────────────────────────────────────┘
```

Le Hub est le seul coordinateur. Les services spokes n'ont aucune connaissance les uns des autres — ils exposent tous une API REST (`/health`, plus endpoints métier).

---

## 3. Carte des services

| Service | Port | Fichier principal | Rôle |
|---|---|---|---|
| **Hub / Core** | 8000 | `hub/orchestrator_core.py` | Coordinateur central, boucle de cycle |
| **Latency Manager** | 8001 | `services/latency_manager/latency_handler.py` | Mesure RTT vers toutes les VMs (TCP ping) |
| **Intent Manager** | 8002 | `services/intent_manager/llm_handler.py` | Extraction SLOs depuis intention LLM |
| **ML Predictor** | 8003 | `services/ml_predictor/app.py` | Prédictions ARIMA/ETS/Linéaire pour latence, CPU, RAM |
| **Metrics Manager** | 8004 | `services/metrics_manager/metrics_handler.py` | Calcul MI, seuils adaptatifs, sélection SLOs |
| **Collector** | 8005 | `services/collector/collector.py` | Collecte CPU/RAM via API agents sur chaque VM |
| **Database** | 8006 | `services/database/redis_client.py` | Persistance Redis (métriques, SLOs, décisions) |
| **History Loader** | 8007 | `services/history_loader/app.py` | Chargement historiques depuis Redis |
| **Decision Intelligence** | 8008 | `services/decision_intelligence/topsis.py` | Sélection VM optimale par TOPSIS |
| **Observability** | 8009 | `services/observability/visualizer.py` | Dashboard temps réel |
| **OpenStack Client** | 8024 | `shared/openstack_client.py` | Exécution migrations kubectl sur le master |

### VMs OpenStack disponibles

| VM | IP | Cluster |
|---|---|---|
| edge1 | 194.199.113.18 | edge-cluster |
| edge2 | 194.199.113.28 | edge-cluster |
| cloud1 | 194.199.113.66 | cloud-cluster |
| cloud2 | 194.199.113.69 | cloud-cluster |

---

## 4. Flux d'orchestration — Les 8 étapes

Chaque cycle est déclenché par la réception d'une mesure RTT sur `POST /rtt`. Les 8 étapes sont exécutées séquentiellement dans `_run_flow()` via un `asyncio.Lock` (un seul cycle actif à la fois).

```
RTT reçu → _run_flow()
│
├─ Étape 1 : _step1_slos()
│   ├─ Si bootstrap (< 5 cycles) → SLOs primaires fixes uniquement
│   └─ Sinon → appel metrics_manager (/compute ou /validate selon le mode)
│
├─ Étape 2 : _step2_persist_slos()
│   └─ Stocke les SLOs courants dans Redis
│
├─ Étape 3 : _step3_collect()
│   └─ Collecte CPU/RAM de toutes les VMs via Collector
│
├─ Étape 4 : _step4_persist_metrics()
│   └─ Fusionne RTT + métriques collectées → persiste dans Redis (parallèle)
│
├─ Étape 5 : _step5_check_violations()
│   └─ Vérifie si la VM active viole un SLO primaire (latency, ou SLOs ENHANCED)
│
├─ Étape 6 : _step6_load_histories()
│   └─ Charge les 50 derniers points de chaque VM en parallèle
│
├─ Étape 7 : _step7_predict()
│   └─ Génère les prédictions ML (latence, CPU, RAM) pour toutes les VMs en parallèle
│
└─ Étape 8 : _step8_decide()
    ├─ Si cooldown actif → MAINTIEN immédiat
    └─ Sinon → appel decision_intelligence /decide → TOPSIS
        ├─ MIGRATION → execute kubectl, maj service_vm, reset cooldown
        └─ MAINTIEN → log, aucune action
```

### Variables d'état globales (`OrchestratorState`)

| Attribut | Type | Description |
|---|---|---|
| `_mode` | `str` | `"autonomous"` ou `"enhanced"` |
| `service_vm` | `str` | VM actuellement active (héberge le service) |
| `current_slos` | `List[Dict]` | SLOs actifs du cycle courant |
| `original_intent_weights` | `Dict[str, float]` | Poids originaux LLM (évite dilution cumulative) |
| `cycle_count` | `int` | Numéro de cycle (incrémenté à chaque RTT reçu) |
| `last_migration_ts` | `float` | Timestamp monotonic de la dernière migration |
| `bootstrap_cycles` | `int` | Compteur de cycles bootstrap |
| `last_mi_scores` | `Dict[str, float]` | Derniers scores MI calculés |
| `last_predictions` | `Dict` | Dernières prédictions ML par VM |

---

## 5. Modes de fonctionnement

### Mode AUTONOMOUS (défaut)

Le système génère ses propres SLOs sans intervention humaine.

**Séquence :**
1. **Bootstrap (cycles 0–4)** : SLO unique `latency < 30ms` (is_primary=True, poids=1.0)
2. **Post-bootstrap** : le Metrics Manager calcule l'Information Mutuelle entre latence et chaque métrique secondaire
3. Les métriques dont le score MI > `MI_RELATIVE_THRESHOLD` (1e-8) deviennent des SLOs secondaires avec seuil adaptatif (percentile P70/P75/P85)

**SLOs typiques observés :**
- Primaire : `latency < 30ms` (weight=1.0)
- Secondaire : `cpu_usage < seuil_adaptatif` (si MI > 1e-8)
- Secondaire : `ram_usage < seuil_adaptatif` (si MI > 1e-8 et RAM variable)

### Mode ENHANCED

Activé par réception d'une intention utilisateur en langage naturel.

**Séquence :**
1. L'utilisateur envoie une intention à `POST :8002/intent`
2. Le LLM (LAAS vLLM Qwen3-27B → Ollama fallback) extrait les SLOs avec seuils et poids
3. L'Intent Manager envoie les SLOs au Hub via `POST :8000/intent`
4. Le Hub passe en mode `"enhanced"` ; les SLOs LLM deviennent primaires (is_primary=True)
5. À chaque cycle, `/validate` du Metrics Manager valide et enrichit ces SLOs

**Différence avec AUTONOMOUS :**
- Tous les SLOs sont `is_primary=True` → poids répartis selon la préférence utilisateur
- Le TOPSIS prend en compte latence + CPU + RAM avec les poids définis par le LLM (ex: 0.5/0.25/0.25)
- Les poids originaux sont mémorisés dans `original_intent_weights` pour éviter la dilution cycle après cycle

**Exemple d'intention ENHANCED :**
```
"Je regarde un stream vidéo en ce moment et j'ai besoin que ça soit fluide"
→ LLM extrait :
  • latency < 30ms   (weight=0.50) — is_primary=True
  • cpu_usage < 80%  (weight=0.25) — is_primary=True
  • ram_usage < 80%  (weight=0.25) — is_primary=True
```

---

## 6. Calcul des SLOs — Metrics Manager

Fichier : `services/metrics_manager/metrics_handler.py`

### 6.1 Information Mutuelle — Estimateur k-NN de Kozachenko-Leonenko

L'Information Mutuelle (MI) mesure la dépendance statistique entre la métrique continue X et le signal binaire de violation Y. L'estimateur différentiel de Kozachenko-Leonenko est utilisé — il opère directement sur les valeurs continues sans discrétisation.

**Formule :**
```
MI(X ; Y) = H(X) − H(X|Y)

H(X)    ≈ ψ(N) − ψ(k) + ln(2) + mean(ln r_k)
H(X|Y)  = Σ_c  P(c) · H(X | Y=c)

Normalisation : Score = MI / H(Y)  ∈ [0, 1]
```

Où `r_k` est la distance au k-ième plus proche voisin (norme L∞ en 1D), `ψ` est la fonction digamma, et `k = max(3, min(5, n // 10))`.

**Pourquoi k-NN plutôt que la table de contingence 2×2 ?**

| Critère | Table 2×2 (ancienne) | k-NN Kozachenko-Leonenko |
|---|---|---|
| Discrétisation | Médiane (perte info) | Aucune |
| Dépendances non-linéaires | Aveugle (U-shape → MI=0) | Détectées |
| Robustesse | Fragile (seuil médiane) | Robuste ≥15 pts/classe |
| Exemple U-shape | MI = 0.000 | MI = 0.85 |

**Sélection :** une métrique est retenue si `Score > MI_RELATIVE_THRESHOLD` (= 0.15).

**Résultats observés en production (Cycle #41) :**
| Métrique | Score MI | Décision |
|---|---|---|
| latency | 0.880 | — (SLO primaire fixe) |
| cpu_usage | 0.354 | ✅ Retenu → SLO secondaire (seuil adaptatif 11.2 %) |
| ram_usage | 0.000 | ❌ Écarté (MI négative clampée à 0) |

### 6.2 Visualisation 5 étapes (logs terminal)

À chaque cycle, le terminal `metrics_manager` affiche le pipeline complet pour chaque métrique :

```
══════════════════════════════════════════════════════════════
  🔬  MI k-NN — Cycle #41 | Métrique : cpu_usage
  n = 50 points   k = 5 voisins (plus proches)
══════════════════════════════════════════════════════════════

  ÉTAPE 1 — H(X) : Entropie différentielle globale
  ┌──────────────────────────┬────────────┬──────┬─────────┐
  │ Rôle                     │ Valeur (%) │ r_5  │ ln(r_k) │
  ├──────────────────────────┼────────────┼──────┼─────────┤
  │ max r_k  ← outlier       │ 91.3       │ 36.8 │ 3.605   │
  │ min r_k  ← cluster dense │ 72.1       │  2.1 │ 0.742   │
  └──────────────────────────┴────────────┴──────┴─────────┘
  H(X) = ψ(50) − ψ(5) + ln(2) + mean(ln r_5)
       = 3.902 − 1.506 + 0.693 + (0.086) = 3.592 nats

  ÉTAPE 2 — H(X|Y=1) : Classe violation (n=15)
  ÉTAPE 3 — H(X|Y=0) : Classe normale (n=35)
  ÉTAPE 4 — H(X|Y) : Entropie conditionnelle pondérée
  ÉTAPE 5 — Score final : MI = 0.458 nats  Score = 0.750  ✅ retenu
══════════════════════════════════════════════════════════════
```

### 6.3 Seuil adaptatif (percentile)

Pour chaque métrique secondaire retenue, le seuil est calculé dynamiquement :

```
CV = écart-type / moyenne

Si CV < 0.15 (signal stable)   → P70 (seuil serré)
Si CV < 0.30 (signal normal)   → P75 (seuil normal)
Si CV ≥ 0.30 (signal volatile) → P85 (seuil relâché)
```

Le seuil est ensuite clampé dans les bornes physiques du registre (`bounds.min`, `bounds.max`).

### 6.4 Logs ASCII en production

Le Metrics Manager affiche à chaque cycle :

1. **Bannière cycle** : `Metrics Manager — Cycle #N | Mode AUTONOMOUS/ENHANCED`
2. **Historique** : toutes les valeurs latence/CPU/RAM + flag `is_violation` (tableau)
3. **Pipeline k-NN 5 étapes** : pour chaque métrique (H(X), H(X|Y=c), MI, score)
4. **Scores MI récapitulatif** : métrique → score → décision (> 0.15 ou non)
5. **SLOs sélectionnés** : métrique, type, seuil, opérateur, poids normalisé

---

## 7. Décision TOPSIS

Fichier : `services/decision_intelligence/topsis.py`

TOPSIS (Technique for Order of Preference by Similarity to Ideal Solution) — méthode d'aide multicritère à la décision.

### 7 étapes du calcul

**Étape 1 — Matrice de décision**
Chaque VM candidate est une alternative, chaque SLO actif est un critère.
La valeur utilisée est la **prédiction ML** (pas la mesure instantanée) pour anticiper l'évolution.

```
         latency_pred   cpu_pred   ram_pred
edge1  │    51.4 ms   │  20.5 %  │  43.6 %  │
edge2  │    28.2 ms   │  22.0 %  │  41.0 %  │
cloud1 │    26.7 ms   │  13.6 %  │  38.1 %  │
cloud2 │    27.4 ms   │  12.9 %  │  40.6 %  │
```

**Étape 2 — Normalisation Min-Max**
```
x_norm = (x - min) / (max - min)   [pour critère "minimiser"]
```

**Étape 3 — Matrice pondérée**
Chaque valeur normalisée × poids du SLO correspondant.

**Étape 4 — Solution idéale positive (A+) et négative (A-)**
- A+ : meilleure valeur possible par critère (0 pour "minimiser")
- A- : pire valeur possible par critère (1 pour "minimiser")

**Étape 5 — Distance euclidienne**
- `d+` : distance à A+
- `d-` : distance à A-

**Étape 6 — Score de proximité**
```
score = d- / (d+ + d-)    ∈ [0, 1]
```
Score → 1 : VM proche de l'idéal. Score → 0 : VM proche du pire.

**Étape 7 — Classement**
La VM avec le score maximal est sélectionnée si elle n'est pas la VM active courante.

### Logs ASCII en production

Le Decision Intelligence affiche 4 tables à chaque appel `/decide` :

1. **Prédictions candidats** : latence / CPU / RAM prédits par VM
2. **Normalisation Min-Max** : valeurs normalisées [0,1] par critère
3. **Pondération** : valeurs × poids par critère
4. **Distances & Score** : d+, d-, score TOPSIS final + classement

**Exemple de résultat observé :**
```
edge1  : score=0.0000 (VM active violante)
edge2  : score=0.9858 ← SÉLECTIONNÉE
cloud1 : score=0.0142
cloud2 : score=0.0639
→ MIGRATION edge1 → edge2
```

### Garde-fous

- **VM active incluse comme candidate** : la VM courante est désormais incluse dans le pool TOPSIS. Si TOPSIS la sélectionne malgré la violation → décision STAY (meilleure option disponible).
- Fast path cooldown : si cooldown actif → STAY immédiat sans appel TOPSIS
- Colonnes **budget** et **fiabilité** supprimées du TOPSIS — leurs valeurs étaient systématiquement identiques pour toutes les VMs (0.000) et n'apportaient aucune discrimination. Seules les métriques SLO (latency, cpu_usage, ram_usage) constituent la matrice de décision.

---

## 8. Détection de violations — Proactivité ML

Fichier : `services/decision_intelligence/violation_detector.py`

### Logique de classification

La détection analyse chaque SLO actif sur la VM de service :

```python
# Condition ML : prédictions confirment ou anticipent le breach
pred_breach       = any(pred > threshold * proactive_factor  for pred in preds)
time_breach_imm.  = time_to_breach <= HORIZON_ALERT  or  estimated_steps <= HORIZON_ALERT
ml_driven         = pred_breach  or  time_breach_imminent

# Classification
if ml_driven or trend_rising:
    → "proactive"   # ML guide la décision (même si valeur courante déjà en breach)
elif breach_reactive:
    → "reactive"    # Aucune prédiction disponible — réaction à la mesure brute
else:
    → "none"        # Pas de violation
```

### Sémantique

| Label | Signification |
|---|---|
| `proactive` | Les prédictions ML ont guidé la décision (anticipation ou confirmation ML) |
| `reactive` | Aucune prédiction disponible — décision basée sur la mesure courante uniquement |

> **Pourquoi "proactive" même quand la valeur courante dépasse déjà le seuil ?**
> Parce que si les prédictions ML confirment la violation, c'est le **modèle d'apprentissage** qui pilote la décision. Le système est proactif par nature — il utilise l'intelligence prédictive plutôt que la mesure brute.

### Paramètres

| Paramètre | Valeur | Description |
|---|---|---|
| `PROACTIVE_FACTOR` | 0.85 | Seuil d'alerte = 85% du seuil SLO |
| `HORIZON_ALERT` | 3 | Nombre de pas de prédiction pour alerte imminente |
| `_MIN_SLOPE` | 5% du seuil/pas | Pente minimale pour tendance montante |
| `_PROACTIVE_MIN_SEVERITY` | 0.05 | Seuil minimum de sévérité proactive (filtre bruit) |

### Sévérité

```python
sev = excess + 0.3 * slope_bonus
    où excess      = max(0, predicted_peak - threshold) / threshold
       slope_bonus = max(0, slope) / threshold
```

La sévérité est calculée sur le **pic prédit** pour les violations proactives, sur la **valeur courante** pour les réactives.

---

## 9. Traçabilité des cycles

### Principe

Le numéro de cycle est propagé par le Hub à chaque appel de service. Cela permet au jury de voir que les scores MI calculés dans `metrics_manager` au Cycle #N sont exactement les poids utilisés par `decision_intelligence` au même Cycle #N.

### Flux de propagation

```
Hub (cycle_count = N)
  ├─ POST metrics_manager /compute  {"cycle": N, ...}
  │      → bannière "Metrics Manager — Cycle #N"
  │      → pipeline MI 5 étapes avec "MI k-NN — Cycle #N | Métrique : cpu_usage"
  │      → retourne mi_scores (stockés dans state.last_mi_scores)
  │
  └─ POST decision_intelligence /decide  {"cycle": N, "mi_scores": {...}, ...}
         → bannière "Decision Intelligence — Cycle #N"
         → SLOs affichés avec MI en annotation :
              • latency     seuil=300.0ms  poids=0.74  [PRIMAIRE]
              • cpu_usage   seuil=11.20%   poids=0.26  [SECONDAIRE MI=0.354]
```

### Ce que voit le jury

**Terminal `metrics_manager`** :
```
══════════════════════════════════════════════════════════════
  🔬  MI k-NN — Cycle #41 | Métrique : cpu_usage
  Score = 0.354  ✅ retenu
══════════════════════════════════════════════════════════════
```

**Terminal `decision_intelligence`** (même cycle) :
```
══════════════════════════════════════════════════════════════
  🎯  Decision Intelligence — Cycle #41
  VM active : edge2
  SLOs issus du Cycle #41 (Metrics Manager) :
    • latency     seuil=300.0ms  poids=0.74  [PRIMAIRE]
    • cpu_usage   seuil=11.20%   poids=0.26  [SECONDAIRE MI=0.354]
══════════════════════════════════════════════════════════════
```

Le score MI `0.354` du Cycle #41 devient exactement le poids `0.26` (normalisé) dans le TOPSIS du même cycle.

---

## 10. Observabilité — Dashboard SSE

Fichier : `services/observability/app.py` — port 8009

### Architecture

```
GET  /         → Dashboard HTML auto-contenu (Chart.js + SSE)
GET  /stream   → Server-Sent Events (SSE) — une connexion persistante par client
POST /audit    → Réception des décisions du Hub → broadcast SSE → deque(maxlen=500)
GET  /audit/log → Historique complet des décisions (JSON)
```

Un thread de fond (`_poll_hub`) interroge `GET :8000/data` toutes les 2 secondes et diffuse les snapshots aux clients SSE connectés.

### Contenu du dashboard

| Bloc | Description |
|---|---|
| Cartes VM | Métriques en temps réel (barres de progression) + prédictions ML |
| Latence historique | Graphique Chart.js multi-VM avec seuil SLO |
| Poids SLOs actifs | Graphique en barres des poids TOPSIS (latency / cpu_usage / ram_usage) |
| Détail SLOs | Tableau des SLOs actifs avec seuils et types (PRIMAIRE/SECONDAIRE) |
| Audit log | Tableau des décisions : heure, cycle, décision, type, VM source, VM cible, TOPSIS score, métriques, raison |

### Payload audit (POST /audit)

```json
{
  "cycle":        41,
  "decision":     "migrate",
  "breach_type":  "proactive",
  "from_vm":      "edge2",
  "to_vm":        "cloud1",
  "topsis_score": 0.9858,
  "metrics":      ["latency", "cpu_usage"],
  "slos_active":  [...],
  "mi_scores":    {"latency": 0.88, "cpu_usage": 0.354},
  "reason":       "proactive violation on latency..."
}
```

---

## 11. Module Intent Manager (mode ENHANCED)

Fichier : `services/intent_manager/llm_handler.py`

### Cascade LLM

```
1. LAAS vLLM (primaire)
   URL  : https://pfcalcul.laas.fr/vllm/v1/chat/completions
   Modèle : Qwen3/Qwen--Qwen3.6-27B-FP16
   Timeout : 10s

2. Ollama (fallback local)
   URL  : http://localhost:11434
   Modèle : qwen2.5:latest

3. Regex + keywords (fallback ultime)
   Extraction par patterns : "latency < Xms", "cpu < X%", etc.
```

### Format de sortie attendu du LLM

```json
[
  {"metric": "latency",   "threshold": 30,  "operator": "<", "weight": 0.5},
  {"metric": "cpu_usage", "threshold": 80,  "operator": "<", "weight": 0.25},
  {"metric": "ram_usage", "threshold": 80,  "operator": "<", "weight": 0.25}
]
```

### API

```
POST :8002/intent
Body: {"intention": "Je regarde un stream vidéo...", "intent_id": "opt."}

→ LLM extrait les SLOs
→ Forward à POST :8000/intent
→ Hub passe en mode enhanced
→ Réponse : {"status": "accepted", "slos_count": 3}
```

### SLO Merger (`slo_merger.py`)

Fusionne les SLOs LLM avec les contraintes du registre métier :
- Clamp du seuil dans `bounds` (min/max physiques)
- Normalisation des poids (somme = 1.0)
- Vérification de l'opérateur (cohérence avec le registre)

---

## 12. Infrastructure OpenStack / Kubernetes

### OpenStack Client (`:8024`)

Déployé sur le master Kubernetes (`194.199.113.8`). Expose deux endpoints :

```
GET  /active_vm  → retourne la VM active courante (kubectl get pods)
POST /migrate    → body: {"from_vm": "edge1", "to_vm": "cloud2"}
                 → exécute kubectl scale/rollout sur le bon cluster
                 → retourne {"status": "ok", "cluster": "cloud-cluster"}
```

### Clusters

| Cluster | VMs | Namespace Kubernetes |
|---|---|---|
| edge-cluster | edge1, edge2 | `default` |
| cloud-cluster | cloud1, cloud2 | `default` |

### VM Agent

Chaque VM expose un agent HTTP sur le port 8200 (`vm_agent.py`). Le Collector interroge `/metrics` pour récupérer CPU et RAM en temps réel.

---

## 13. Persistance — Redis & Excel

### Redis (`services/database/redis_client.py`)

Clés Redis (voir `shared/redis_keys.py`) :

| Clé | Structure | Contenu |
|---|---|---|
| `metrics:{vm_id}:{metric}` | LPUSH/LTRIM liste | Historique des valeurs (100 points max) |
| `slos:current` | SET JSON | SLOs actifs courants |
| `decisions:history` | LPUSH/LTRIM liste | 50 dernières décisions |
| `metrics:all_vms:snapshot` | SET JSON | Snapshot dernière collecte |

Pas de TTL/EXPIRE — la rotation est gérée par LTRIM.

### Excel (`shared/excel_writer.py`)

Fichier : `data/qos_history.xlsx`

| Feuille | Colonnes |
|---|---|
| Métriques | timestamp, vm_id, latency, cpu_usage, ram_usage, reliability |
| Décisions | timestamp, decision, from_vm, to_vm, reason, cycle |
| SLOs | timestamp, metric, threshold, operator, weight, is_primary |
| Intentions_LLM | timestamp, intention, nb_slos |

**Mécanismes de protection :**
- `threading.Lock` : sérialise toutes les écritures (évite les race conditions entre threads asyncio)
- `_load_workbook_safe()` : détecte les fichiers corrompus et recrée le workbook automatiquement
- Rotation automatique : si taille > `EXCEL_MAX_MB` (200 MB), supprime 20% des lignes les plus anciennes

---

## 14. Configuration

Fichier : `shared/config.py`

### Paramètres clés

| Paramètre | Valeur par défaut | Description |
|---|---|---|
| `COLLECTION_INTERVAL` | 5.0s | Intervalle entre deux cycles |
| `MIGRATION_COOLDOWN_S` | 5.0s | Anti-thrashing entre deux migrations (réduit pour la démo PiCar) |
| `BOOTSTRAP_MIN` | 5 | Cycles avant activation des SLOs adaptatifs |
| `MI_RELATIVE_THRESHOLD` | 0.15 | Seuil MI pour sélection métriques secondaires (relevé de 1e-8 pour filtrer le bruit) |
| `PROACTIVE_FACTOR` | 0.85 | Facteur de déclenchement proactif (85% du seuil) |
| `HORIZON_ALERT` | 3 | Nombre de pas de prédiction pour alerte proactive |
| `HISTORY_WINDOW` | 50 | Points d'historique chargés pour chaque cycle |
| `PERCENTILE_STABLE` | 70.0 | Percentile seuil adaptatif (signal stable, CV<0.15) |
| `PERCENTILE_NORMAL` | 75.0 | Percentile seuil adaptatif (signal normal, CV<0.30) |
| `PERCENTILE_VOLATILE` | 85.0 | Percentile seuil adaptatif (signal volatile) |

### Registre des métriques (`METRICS_REGISTRY`)

```python
METRICS_REGISTRY = {
    "latency": {
        "default_threshold":    300.0,  # ms (augmenté pour la démo PiCar — latence min ~65ms)
        "operator":             "<",
        "bounds":               {"min": 5.0, "max": 2000.0},
        "is_primary_objective": True,   # SLO fixe en mode AUTONOMOUS
        "always_active":        True,
    },
    "cpu_usage": {
        "default_threshold":    80.0,   # %
        "operator":             "<",
        "bounds":               {"min": 1.0, "max": 99.0},
        "is_primary_objective": False,  # SLO secondaire adaptatif
        "always_active":        False,
    },
    "ram_usage": {
        "default_threshold":    80.0,   # %
        "operator":             "<",
        "bounds":               {"min": 1.0, "max": 99.0},
        "is_primary_objective": False,
        "always_active":        False,
    },
}
```

### Variables d'environnement supportées

Toute valeur de `config.py` peut être surchargée par variable d'environnement du même nom.

Exemples :
```
MI_RELATIVE_THRESHOLD=0.1
MIGRATION_COOLDOWN_S=30
BOOTSTRAP_MIN=3
LAAS_LLM_PROXY=https://user:pass@proxy.laas.fr:443
```

---

## 15. Démarrage du système

### Ordre de démarrage recommandé

Les services doivent être démarrés depuis la racine du projet (`qos-orchestrator/`).

```powershell
# 1. Redis (si non démarré)
redis-server redis.conf

# 2. Services spokes (ordre libre)
uvicorn services.database.app:app              --port 8006
uvicorn services.collector.app:app             --port 8005
uvicorn services.history_loader.app:app        --port 8007
uvicorn services.ml_predictor.app:app          --port 8003
uvicorn services.metrics_manager.app:app       --port 8004
uvicorn services.decision_intelligence.app:app --port 8008
uvicorn services.intent_manager.app:app        --port 8002
uvicorn services.observability.app:app         --port 8009

# 3. OpenStack Client (sur master 194.199.113.8:8024)
# (démarré indépendamment sur le nœud master)

# 4. Hub (en dernier)
uvicorn hub.orchestrator_core:app --port 8000

# 5. Latency Manager (déclenche les cycles)
uvicorn services.latency_manager.app:app --port 8001
```

### Capture des logs

Les logs applicatifs sont émis sur stderr (StreamHandler). Pour les capturer :

```powershell
Start-Process python -ArgumentList "-m", "uvicorn", "hub.orchestrator_core:app", "--port", "8000" `
    -WorkingDirectory "C:\chemin\qos-orchestrator" `
    -RedirectStandardError "logs\hub.log" `
    -WindowStyle Hidden
```

### Vérification de santé

```bash
curl http://localhost:8000/health    # Hub
curl http://localhost:8000/status    # État courant (mode, VM active, SLOs, cycle)
curl http://localhost:8004/health    # Metrics Manager
```

### Envoi d'une intention (mode ENHANCED)

L'intention doit être envoyée à **l'intent_manager** (`:8002`) avec le champ `intention` (et non au hub directement) :

```powershell
# PowerShell
$body = @{
    intention = "I need to deploy a real-time video streaming service for autonomous vehicles. The application requires very fast response times to avoid any control delay, must handle intensive video processing workloads, and will run continuously without interruption."
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://140.93.89.92:8002/intent" `
                  -Method Post `
                  -ContentType "application/json" `
                  -Body $body
```

```bash
# bash/curl
curl -X POST http://140.93.89.92:8002/intent \
  -H "Content-Type: application/json" \
  -d '{"intention": "Je regarde un stream vidéo en ce moment et j'\''ai besoin que ce soit fluide"}'
```

> **Important** : le champ s'appelle `intention` (pas `intent`). Le LLM (LAAS Qwen3-27B → Ollama fallback) analyse le texte, extrait les SLOs, puis les transmet automatiquement au hub via `POST :8000/intent`.

### Reset vers le mode AUTONOMOUS

```bash
curl -X POST http://localhost:8000/reset
```

---

## 16. Démo PiCar-X — Latence basée sur la position

### Principe

Pour la démonstration, la latence vers chaque VM n'est plus mesurée par TCP ping réseau, mais **calculée à partir de la distance euclidienne** entre la voiture PiCar-X et la VM sur une carte 2D. Cela crée un comportement démonstratif visible et intuitif : plus la voiture s'approche d'une VM, plus la latence simulée est faible.

```
latency_ms = BASE_MS + K_MS_PER_CM × distance_cm
           = 0 + 20.0 × distance(voiture, VM)
```

### Composants ajoutés

#### `infrastructure/picar_bridge.py` (sur le PiCar, port 8080)

Serveur Flask unique qui remplace le client picar original :

| Route | Méthode | Description |
|---|---|---|
| `/` | GET | Sert le simulateur HTML |
| `/Trajectoire.jpg` | GET | Sert l'image de la trajectoire |
| `/tick` | POST | Reçoit `{x, y}`, pinge les 4 VMs, transmet RTT au hub |
| `/vm-status` | GET | Proxifie `GET :8000/status` du hub → retourne `service_vm` |
| `/health` | GET | Santé du bridge |

Le `/tick` pinge les VMs en parallèle via `ThreadPoolExecutor`. Chaque ping VM dort `latency_ms / 1000` secondes côté VM, simulant le délai réseau.

#### `infrastructure/vm_ping/` (sur chaque VM)

Un script Python unique par VM fusionne deux serveurs Flask via `threading` :

```
edge1_ping.py   →  IP 194.199.113.18  pos=(-20, 50)
edge2_ping.py   →  IP 194.199.113.28  pos=( 30,-10)
cloud1_ping.py  →  IP 194.199.113.66  pos=(-20,-10)
cloud2_ping.py  →  IP 194.199.113.69  pos=( 30, 50)
```

| Port | App | Endpoints |
|---|---|---|
| 5001 | `ping_app` | `POST /ping {x,y}` → calcule distance, sleep, retourne `latency_ms` |
| 8200 | `agent_app` | `GET /metrics` → CPU/RAM via psutil |

#### `infrastructure/picarx_sim.html` (servi par le bridge)

Simulateur visuel de la trajectoire PiCar-X avec :
- Carte 2D avec positions des VMs (carrés = edge, losanges = cloud)
- Voiture animée sur la trajectoire bouclée
- Latences affichées en temps réel sur chaque VM
- **Badge "Service actif sur"** : indique la VM hébergeant le service (polling `/vm-status` toutes les 3s)
- **Highlight cyan** : la VM active est mise en évidence sur le canvas avec halo lumineux et préfixe `★ [ACTIF]`

Accès : `http://140.93.64.105:8080/`

### Flux complet de la démo

```
Navigateur (PC)
    │  GET http://140.93.64.105:8080/        → simulateur HTML
    │  POST /tick {x,y}  toutes les 2s       → latences en temps réel
    │  GET /vm-status    toutes les 3s        → badge VM active
    ▼
picar_bridge.py (PiCar :8080)
    │  POST /ping {x,y} × 4 VMs en parallèle → latence simulée
    │  POST /rtt {measurements}               → déclenche cycle hub
    │  GET  http://140.93.89.92:8000/status   → service_vm courant
    ▼
VMs (port 5001) + Hub (port 8000)
```

### Positions des VMs sur la carte

| VM | x (cm) | y (cm) | Type |
|---|---|---|---|
| edge1 | -20 | +50 | Edge (carré bleu) |
| edge2 | +30 | -10 | Edge (carré bleu) |
| cloud1 | -20 | -10 | Cloud (losange orange) |
| cloud2 | +30 | +50 | Cloud (losange orange) |

L'origine (0,0) est le point de départ de la voiture sur la trajectoire.

### Seuil SLO adapté à la démo

La latence minimale atteignable (voiture à 8cm de la VM la plus proche) est ~65ms. Le seuil SLO a donc été relevé à **300ms** pour que le système puisse alterner entre STAY (voiture proche) et MIGRATION (voiture loin), rendant le comportement visible.

### Démarrage côté PiCar

```bash
# Sur le PiCar (pi@140.93.64.105)
cd ~/Projet_PFE/trajectoire
python picar_bridge.py
# → http://140.93.64.105:8080/
```

### Démarrage côté VMs

```bash
# Sur chaque VM (ex: edge1 → ubuntu@194.199.113.18)
ssh -i ~/projet_PFE/admin_log_2.pem ubuntu@194.199.113.18
cd ~/projet_PFE/trajectoire
python edge1_ping.py
# → ping :5001  agent :8200
```

---

## 17. Résultats de validation

Tests effectués le 22 juin 2026 sur infrastructure réelle (OpenStack ENIS).

### Mode AUTONOMOUS — cycles de référence

| Cycle | VM active | RTT mesuré | Violation | Décision | TOPSIS |
|---|---|---|---|---|---|
| 3 (bootstrap) | cloud2 | 17.5 ms | Non | MAINTIEN | — |
| 4 (bootstrap) | cloud2 | 36.7 ms | Oui (latency) | **MIGRATION → edge1** | 1.000 |
| 5 (post-bootstrap) | edge1 | 16.0 ms | Non | MAINTIEN (cooldown) | — |
| 6 | edge1 | 33.6 ms | Oui | MAINTIEN (cooldown 50s) | — |

**SLOs AUTONOMOUS post-bootstrap (Cycle #41 observé) :**
- Primaire : `latency < 300ms` (weight=0.74) — seuil métier fixe
- Secondaire : `cpu_usage < 11.2%` (MI=0.354 > 0.15, weight=0.26) — seuil adaptatif P85
- Écarté : `ram_usage` (MI=0.000 — distribution homogène entre classes violation/normale)

### Mode ENHANCED — cycles avec intention streaming

Intention : *"Je regarde un stream vidéo en ce moment, j'ai besoin que ce soit fluide"*

LLM extrait → 3 SLOs primaires : `latency<300ms (0.5)` + `cpu<80% (0.25)` + `ram<80% (0.25)`

| Cycle | VM active | SLOs actifs | Violation | Décision |
|---|---|---|---|---|
| 14 | edge1 | 3 primaires (LLM) | Oui (latency) | MAINTIEN (cooldown) |
| 15 | edge1 | 3 primaires (LLM) | Oui (latency) | MAINTIEN (cooldown) |
| 17 | edge1 | 3 primaires (LLM) | Oui (latency) | **MIGRATION → cloud2** |

### Comparaison AUTONOMOUS vs ENHANCED

| Aspect | AUTONOMOUS | ENHANCED |
|---|---|---|
| Source des SLOs | MI k-NN automatique | LLM (intention naturelle) |
| SLOs primaires | latency seul | latency + cpu + ram |
| SLOs secondaires | cpu_usage si MI > 0.15 | aucun (tout est primaire) |
| Poids décision TOPSIS | latency = 74%, cpu = 26% | latency=50%, cpu=25%, ram=25% |
| Flexibilité | Zéro configuration | Expression en langage naturel |
| Réactivité | Immédiate (dès post-bootstrap) | Dès réception de l'intention |

### Observations techniques

1. **MI k-NN vs table 2×2** : l'estimateur de Kozachenko-Leonenko détecte les dépendances non-linéaires (U-shape : MI_table=0.000, MI_kNN=0.85). Le seuil relevé à 0.15 filtre le bruit statistique et ne retient que les métriques réellement corrélées.

2. **Proactivité ML** : dès que les prédictions ML confirment une violation (`pred_breach=True` ou `imminent=True`), la décision est `"proactive"`. Le label `"reactive"` n'apparaît qu'en l'absence de prédictions.

3. **Traçabilité cycle** : le numéro de cycle est propagé du Hub aux deux terminaux (`metrics_manager` et `decision_intelligence`). Le jury peut voir que le score MI 0.354 calculé au Cycle #41 devient le poids 0.26 dans le TOPSIS du même cycle.

4. **Dashboard SSE** : toutes les décisions sont enregistrées dans un audit trail avec cycle, type breach, SLOs, scores MI et score TOPSIS, diffusé en temps réel via SSE sur `http://localhost:8009`.

5. **Anti-thrashing cooldown 5s** : réduit de 60s à 5s pour la démo PiCar afin que le système réagisse rapidement. En production, 60s est recommandé pour éviter les migrations en cascade.

6. **Poids originaux préservés** : le mécanisme `original_intent_weights` empêche la dilution progressive des poids LLM cycle après cycle quand le Metrics Manager enrichit avec des SLOs secondaires.
