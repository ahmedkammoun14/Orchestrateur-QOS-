# Suivi — Simulation multi-VM edge

> Journal d'avancement rempli au fur et à mesure, en parallèle de la
> réalisation (comme `SUIVI_MULTI_PROVIDER_TRANSVERSAL.md`). Chaque étape n'est
> cochée qu'après **vérification indépendante**, avec la preuve associée.

Légende statut : ⬜ à faire · 🟡 en cours · ✅ vérifié · ⚠️ vérifié avec réserve

## Tableau d'avancement

| # | Étape | Statut | Preuve / note |
|---|---|---|---|
| 1 | `vm_agent_sim.py` paramétré par env | ✅ | déployé identique sur les 4 machines |
| 2 | Launchers `launch_*.sh` par machine | ✅ | edge1/edge2 (3 inst.), cloud1/cloud2 (1 inst.) — positions/ports/capacités conformes au plan |
| 3 | 8 VMs dans `shared/config.py` | ✅ | VM_REGISTRY/VM_CLUSTER_MAP/PROVIDER_REGISTRY à 8 VMs ; `validate_provider_registry()` OK ; PROVIDER_OF_VM correct (p1: edge1/1b/1c/cloud1, p2: edge2/2b/2c/cloud2) ; **184 tests verts** (test dashboard L69 corrigé : égalité stricte 4-VMs → `set(VM_REGISTRY)`) |
| 4 | Mapping k8s (`openstack_client.py`) | ✅ | déployé sur le master ✅ ; copie dépôt à jour (8 VMs dans les 3 mappings) |
| 5 | Simulateur PiCar 8 VMs | ✅ | `picar_bridge.py` + `picarx_sim.html` (Pi) : 8 VMs, ports 5001/5002/5003, positions cohérentes |
| 6 | Security groups (5002/5003, 8201/8202) | ✅ | ports déjà ouverts — 8 curl /health OK (agent 8201/8202 + ping 5002/5003), positions conformes |
| 7 | Lancement + validation TOPSIS | 🟡 | 8 VMs lancées et pinguées (pipeline OK, cycles tournent) ; seuil latence passé à **80 ms** (184 tests verts) ; carte allégée (labels compacts) ; reste : tester les 2 intentions de démo |

## Réglages de démo (étude ETUDE_PARAMETRES_DEMO_MULTI_VM.md)

- Seuil latence : **40 ms** (décision finale — voir ci-dessous). ⚠️ passer de 80 à 40.
- Positions VMs : **INCHANGÉES** — les positions déployées donnent déjà, à T=40,
  **14 migrations/tour dont 86 % inter-provider** (mesuré sur les vrais modules).
  Aucun launcher ni HTML à retoucher.
- Capacités edge 2/3/4 : gardées.
- Démo = **2 intentions** en mode enhanced :
  1. « contrôle temps réel, latence < 40 ms » → latence dominante → **edge**,
     14 migrations dont 86 % inter-provider. Cloud absent (plancher 50 ms > 40) — normal.
  2. « traitement/rendu lourd, gros besoin CPU/RAM » → cpu dominante (≥1.5 cœurs) → **cloud**.
- Carte : décocher « Frontières de zone » + « Points proactifs » pour la présentation.

### Effet du seuil sur les handoffs (positions actuelles, autonomous latence)

| Seuil | migr/tour | inter-provider | % inter |
|---|---|---|---|
| **40** | **14** | **12** | **86 %** |
| 50 | 13 | 8 | 62 % |
| 60 | 11 | 4 | 36 % |
| 80 | 3 | 0 | 0 % |

À T=40 le rayon de conformité edge (~22 cm) crée des trous de couverture par
provider → la voiture qui traverse un trou déclenche un handoff inter-provider.
À T=80 (rayon 53 cm) chaque provider couvre tout → 0 handoff (collant).

## Journal détaillé

### Étape 1 — Agent VM paramétré
- **État** : ⬜
- **Attendu** : `python3 vm_agent_sim.py` lit `VM_ID, VM_NAME, VM_TYPE, VM_IP,
  VM_X, VM_Y, PING_PORT, AGENT_PORT, D_MIN, D_MAX, LAT_B, LAT_A, TOTAL_CORES,
  TOTAL_RAM_GB, CPU_LO, CPU_HI, RAM_LO, RAM_HI` depuis l'environnement.
- **Vérification** : deux instances sur une même machine, ports 8200 et 8201,
  `/metrics` renvoie des `vm_id`, capacités et positions différents.
- **Résultat** : _(à remplir)_

### Étape 2 — Launchers
- **État** : ⬜
- **Vérification** : `launch_edge1_machine.sh` démarre edge1/edge1b/edge1c ;
  `curl 113.18:8200/health`, `:8201/health`, `:8202/health` → 200.
- **Résultat** : _(à remplir)_

### Étape 3 — Config 8 VMs
- **État** : ⬜
- **Vérification** : `pytest tests/unit/test_provider_registry.py` vert ;
  log de démarrage du hub « VMs enregistrées : 8 ».
- **Résultat** : _(à remplir)_

### Étape 4 — Mapping k8s
- **État** : ⬜
- **Vérification** : `POST /migrate {to_vm: edge1b}` → kubectl apply space_1 sur
  edge-cluster, réponse 200, pod Running sur pop1-worker-1.
- **Résultat** : _(à remplir)_

### Étape 5 — Simulateur PiCar
- **État** : ⬜
- **Vérification** : la carte affiche 8 symboles VM, 6 zones edge colorées,
  ~7 transitions par tour ; le badge VM active suit les 8 VMs.
- **Résultat** : _(à remplir)_

### Étape 6 — Security groups
- **État** : ⬜
- **Vérification** : `curl 194.199.113.18:8201/health` depuis la machine du hub.
- **Résultat** : _(à remplir)_

### Étape 7 — Validation finale
- **État** : ⬜
- **Vérification** : `GET :8000/data` renvoie 8 VMs ; prédictions dans les bandes
  (aucune signature de fallback) ; au moins un cycle où `reasoning.topsis`
  classe ≥ 2 candidats conformes.
- **Résultat** : _(à remplir)_

## Points ouverts à trancher en cours de route

- Capacités edge finales (2/3/4 par défaut) — ajuster selon la fréquence
  réelle de déclenchement de TOPSIS après l'étape 7.
- `MIGRATION_COOLDOWN_S` — réévaluer si ping-pong observé.
