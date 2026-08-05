# Plan — Simulation multi-VM edge (8 VMs sur 4 machines)

> **Contrat de travail** : discussion et fichiers de suivi uniquement. Aucun
> commit, aucun push, aucune branche tant que l'utilisateur ne l'autorise pas
> explicitement. Le code infrastructure est livré dans la conversation ; le
> code orchestrateur passe par une prompt d'exécuteur.

## Objectif

Enrichir la démo sans matériel supplémentaire : simuler **3 VMs edge par
provider** (au lieu d'une) en lançant plusieurs instances de l'agent VM sur la
**même machine physique**, à des **ports différents**, avec des **positions** et
**capacités** distinctes. Objectif métier : que **TOPSIS s'exécute réellement et
en continu** (≥ 2 VMs conformes simultanément dans un même provider), ce que la
topologie à 4 VMs ne permettait quasiment jamais.

Passage de **4 → 8 VMs** :
- provider-1 = { edge1, edge1b, edge1c, cloud1 }
- provider-2 = { edge2, edge2b, edge2c, cloud2 }

## Placement verrouillé

Positions dérivées par k-means sur la trajectoire (6 arcs edge équilibrés,
14–19 % de piste chacun), providers alternés dans l'ordre de parcours pour
maximiser les transitions inter-provider. Clouds repositionnés pour devenir
**éligibles** (couverture piste < 100 ms : cloud1 9,5 %→52,5 %, cloud2 0 %→47,7 %).

| VM | Position (cm) | Provider | Machine / node k8s | ping | agent | cœurs/Go |
|---|---|---|---|---|---|---|
| edge1  | (3, −9)    | provider-1 | 113.18 / pop1-worker-1 | 5001 | 8200 | 2 |
| edge1b | (34, 19)   | provider-1 | 113.18 / pop1-worker-1 | 5002 | 8201 | 3 |
| edge1c | (−6, 51)   | provider-1 | 113.18 / pop1-worker-1 | 5003 | 8202 | 4 |
| edge2  | (31, −8)   | provider-2 | 113.28 / pop1-worker-2 | 5001 | 8200 | 2 |
| edge2b | (4, 23)    | provider-2 | 113.28 / pop1-worker-2 | 5002 | 8201 | 3 |
| edge2c | (−23, 30)  | provider-2 | 113.28 / pop1-worker-2 | 5003 | 8202 | 4 |
| cloud1 | (−4, 34)   | provider-1 | 113.66 / pop2-worker-1 | 5001 | 8200 | 8 |
| cloud2 | (18, 4)    | provider-2 | 113.69 / pop2-worker-2 | 5001 | 8200 | 8 |

Comportement dynamique validé (simulation sur la trajectoire réelle) :
~14 transitions de zone sur le tracé (2 tours), **4,6 VMs edge conformes en
moyenne**, **2,2 conformes par provider** en permanence → TOPSIS départage en continu.

## Décisions de conception (verrouillées)

1. **Un seul script agent paramétré par variables d'environnement**
   (`vm_agent_sim.py`), lancé N fois par machine. Remplace les 4 scripts en dur
   `*_ping_fixeCarac*.py`. Prérequis de tout le reste.

2. **Mapping Kubernetes** : les 3 edges d'un provider partagent le **même node
   physique** (pop1-worker-1 pour provider-1, pop1-worker-2 pour provider-2),
   donc le **même YAML** (space_1 / space_2). Conséquences :
   - migration edge1 ↔ edge1b ↔ edge1c = **no-op physique** (pod inchangé), mais
     vrai changement d'état d'orchestration ;
   - migrations edge↔cloud et inter-provider = **physiquement réelles** ;
   - `NODE_VM_MAP` inverse (node → VM) devient ambigu → `_get_active_vm()`
     retombe sur la VM canonique du node. Impact au **démarrage seulement**
     (le hub suit ensuite son propre `state.service_vm`). Limite assumée.

3. **Capacités variées 2/3/4 cœurs** par instance edge → donne à TOPSIS de quoi
   discriminer entre edges conformes. Tunables par env, à ajuster après mesure.

4. **Contrainte ML (non négociable)** : un seul modèle par métrique sert les
   8 VMs. On fait varier **positions** (latence reste dans [5,150] edge /
   [50,230] cloud, bornée par le clamp) et **capacités** (conversion runtime,
   sans effet sur le % prédit). On NE touche PAS aux **bandes d'usage**
   (edge cpu/ram 50–80 %, cloud cpu 8–25 %, ram 15–30 %) sous peine de devoir
   régénérer les datasets. Voir [[ml-latency-dataset-scale]].

## Couleurs dashboard / simulateur (8 VMs)

Provider-1 en tons froids + cloud orange ; provider-2 en violets/roses + cloud vert.

| VM | Couleur | | VM | Couleur |
|---|---|---|---|---|
| edge1  | `#3b82f6` bleu | | edge2  | `#a855f7` violet |
| edge1b | `#0ea5e9` ciel | | edge2b | `#d946ef` fuchsia |
| edge1c | `#06b6d4` cyan | | edge2c | `#ec4899` rose |
| cloud1 | `#f97316` orange | | cloud2 | `#22c55e` vert |

## Étapes

| # | Étape | Où | Livrable | Vérification |
|---|---|---|---|---|
| 1 | Agent VM paramétré par env | infra (conversation) | `vm_agent_sim.py` | 2 instances même machine, ports différents, positions/capacités différentes |
| 2 | Launchers par machine | infra (conversation) | `launch_*.sh` | 3 instances edge démarrent, /health OK sur 8200/8201/8202 |
| 3 | Déclarer 8 VMs (orchestrateur) | dépôt (prompt) | `shared/config.py` | `test_provider_registry` passe, hub annonce 8 VMs |
| 4 | Mapping k8s des VMs simulées | infra (conversation) | `openstack_client.py` | migration vers edge1b exécute kubectl, réussit |
| 5 | Simulateur PiCar 8 VMs | infra (conversation) | `picarx_sim.html`, `picar_bridge.py` | carte : 6 zones edge + 2 cloud, ~7 transitions/tour |
| 6 | Security groups OpenStack | infra (manuel) | ouverture 5002/5003, 8201/8202 | `curl` hub → 113.18:8201/health |
| 7 | Lancer et valider | tout | — | `/data` = 8 VMs, prédictions en bande, **TOPSIS départage 2–3 candidats/cycle** |

## Contraintes de non-régression

- `MULTI_PROVIDER_ENABLED=false` par défaut : comportement mono-provider
  inchangé si le flag reste off.
- Aucune modification des bandes d'usage cpu/ram → pas de réentraînement ML.
- Après tout réentraînement ML : **redémarrer ml_predictor** (voir
  [[ml-latency-dataset-scale]]).

## Risques identifiés

- **Cooldown** : avec ~7 transitions/tour et `MIGRATION_COOLDOWN_S=5`, migrations
  rapprochées. À réévaluer après mesure, pas avant.
- **Ports** : collisions possibles si un ancien `*_ping_fixeCarac*.py` tourne
  encore sur 5001/8200. Tuer les anciens process avant de lancer les launchers.
- **Dérive au démarrage** : `_get_active_vm()` peut resynchroniser sur la VM
  canonique d'un node partagé. Cosmétique, démarrage seulement.
