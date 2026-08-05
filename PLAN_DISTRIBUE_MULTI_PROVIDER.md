# Plan — Passage au multi-provider DISTRIBUÉ (N orchestrateurs)

> **Contrat de travail** : discussion, plan et fichiers de suivi. Aucun commit,
> aucun push, aucune branche tant que non explicitement autorisé. Le code
> orchestrateur passe par une prompt d'exécuteur ; l'infrastructure est livrée
> dans la conversation.

## Objectif

Transformer l'orchestrateur mono-processus (2 rôles logiques) en **déploiement
distribué réel** : **un processus orchestrateur par provider**, chacun ne gérant
QUE ses propres VMs, tous communiquant **en HTTP** via le microservice
**`provider_relay`** qui joue le rôle de **hub de communication** (routage des
handoffs + diffusion des intentions) — extensible à **N orchestrateurs**.

On **conserve** :
- la **partition transversale à 8 VMs** (provider-1 = edge1/edge1b/edge1c/cloud1 ;
  provider-2 = edge2/edge2b/edge2c/cloud2) ;
- la **comparaison inter-provider actuelle** (score de violation, dead-band
  absolu, conformité prioritaire) — sa généralisation N-way sera conçue plus tard ;
- cpu/ram en **disponibilité** (aucun impact).

## Architecture cible

```
                    ┌───────────────────────┐
   Intention  ─────►│   provider_relay      │  (HTTP, SANS ÉTAT)
                    │  :8010                 │
                    │  • broadcast intention │
                    │  • routage handoff     │
                    │  • anti-boucle (409)   │
                    └───────┬───────┬────────┘
                            │       │   (HTTP, /intent/relay + /intent/broadcast)
              ┌─────────────┘       └─────────────┐
              ▼                                     ▼
   ┌────────────────────┐               ┌────────────────────┐
   │ Orchestrateur P1   │               │ Orchestrateur P2   │
   │ PROVIDER_ID=prov-1 │◄── kubectl ──►│ PROVIDER_ID=prov-2 │
   │ VMs: e1,e1b,e1c,c1 │  (qui héberge)│ VMs: e2,e2b,e2c,c2 │
   └────────────────────┘               └────────────────────┘
   ACTIF si héberge le service ; sinon STANDBY (répond aux requêtes du relais)
```

- **PiCar / bridge** : chaque VM envoie sa latence à **l'orchestrateur
  propriétaire** (partition des mesures par provider), OU au relais qui fanne.
- **Un seul orchestrateur ACTIF** à la fois (celui qui héberge le service, selon
  kubectl). Les autres sont en **standby**.

## RÉVISION ARCHITECTURE — relais PAR orchestrateur (pas de relais partagé)

> Décision utilisateur : **chaque orchestrateur a son PROPRE relais**, il n'y a
> **aucun relais partagé**. Les relais communiquent de **pair à pair** (relais ↔
> relais). C'est le vrai modèle de fédération (chaque provider expose sa propre
> passerelle, personne ne dépend d'un composant central = pas de SPOF).

```
   ORCHESTRATEUR P1                          ORCHESTRATEUR P2
   hub :8000 + 9 microservices 80xx          hub :8100 + 9 microservices 81xx
   + provider_relay :8010          ◄─HTTP─►  + provider_relay :8110
   (unité autonome, 11 processus)            (unité autonome, 11 processus)
```

Conséquences vs version précédente :
- `PROVIDER_RELAY_PORT` **reçoit l'offset** (P1 8010, P2 8110) — revient sur la
  décision initiale « relais non décalé ».
- Chaque hub parle à **son** relais (localhost) — automatique via
  `PROVIDER_RELAY_SERVICE_URL`.
- Nouvelle table `PROVIDER_RELAY_URLS` : chaque relais connaît les relais des pairs.
- Le relais est lancé **dans chaque stack** (`start_provider.ps1`) —
  `start_relay.ps1` devient inutile.
- **Partagé restant** : uniquement `openstack_client` (:8024, master) + API ML.

## Décisions de conception (verrouillées)

1. **Relais PAR orchestrateur, SANS ÉTAT** : chaque orchestrateur a son relais ;
   pur routage HTTP + garde anti-boucle + diffusion d'intention ; aucun état
   métier. Communication relais ↔ relais (pair à pair). Topologie dans
   `PROVIDER_RELAY_URLS` (relais des pairs) et `PROVIDER_ORCHESTRATOR_URL`.
2. **Modèle actif/standby** : l'orchestrateur qui **héberge** le service décide
   et migre ; les standbys **répondent seulement** aux requêtes du relais
   (évaluent leurs VMs, renvoient offre/TOPSIS). Empêche le split-brain.
3. **kubectl = source de vérité** de « qui héberge » : chaque orchestrateur
   vérifie à chaque cycle s'il possède la VM active (via `_get_active_vm`). Le
   rôle actif **suit** le service.
4. **`PROVIDER_ID`** (variable d'env) filtre `VM_REGISTRY` sur les VMs du
   provider. Valeur `"all"` (défaut) = comportement mono-processus actuel
   (non-régression).
5. **Communication 100 % HTTP** via le relais (handoff + broadcast). Pas de bus
   de messages, pas de point-à-point direct entre orchestrateurs.
6. **Partition et comparaison inter-provider INCHANGÉES** pour l'instant.

## Défis de recherche (à traiter avec soin)

| Défi | Traitement retenu |
|---|---|
| **Singleton du service** (un seul hôte) | kubectl = vérité ; l'actif est celui qui héberge. Handoff idempotent (garde anti-boucle 409). |
| **Propagation de l'intention** | Le relais **diffuse** l'intention à TOUS les orchestrateurs → mêmes SLOs partout (prérequis de comparabilité des scores). |
| **Qui décide ?** | Seul l'actif ; les standbys répondent aux requêtes. |
| **Panne d'un orchestrateur** | Le relais saute le pair injoignable (repli séquentiel) ; timeout HTTP borné. |
| **Cycles indépendants** | Chaque orchestrateur a son propre `cycle_count`, son bootstrap, son cooldown — plus d'état global partagé. |
| **Routage des mesures PiCar** | Le bridge envoie à chaque orchestrateur uniquement les latences de SES VMs (partition des `measurements`). |

## Coexistence de 2 stacks sur UN SEUL PC (contrainte de départ)

Deux stacks complètes sur la même machine → trois isolations pilotées par
`PROVIDER_ID` :

1. **`PORT_OFFSET`** : provider-1 (ou "all") → offset 0 (ports actuels) ;
   provider-2 → offset +100 (hub 8100, latency 8101, … observability 8109).
   Les **URLs internes (`_URLS`) DOIVENT utiliser l'offset** (le hub de P2 appelle
   le collector de P2, pas celui de P1).
2. **`REDIS_DB`** : P1 → DB 0, P2 → DB 1. États séparés (sinon corruption mutuelle).
3. **Filtrage `VM_REGISTRY`** : chaque orchestrateur ne sonde que ses 4 VMs.

**Partagé (une seule instance, PAS d'offset)** : `provider_relay` (:8010),
`openstack_client` (:8024, master), API ML (5001-5003), serveur Redis (DB
séparées).

## Phasage

- **PHASE 1 — les 2 orchestrateurs debout** (ce qu'on fait d'abord) : chaque
  stack collecte/prédit/décide sur ses 4 VMs, isolée (ports + Redis DB).
  **Migrations non coordonnées encore** — on observe, on ne lâche pas 2
  migrateurs concurrents sur le même pod.
- **PHASE 2 — coordination** (à discuter ensuite) : « qui héberge / qui décide »
  (kubectl = vérité, actif/standby), puis handoff inter-provider via relais.

## Étapes

| # | Étape | Phase | Où | Vérification |
|---|---|---|---|---|
| 1 | `PROVIDER_ID` : filtrage VMs + `PORT_OFFSET` + `REDIS_DB` + URLs internes | 1 | dépôt (prompt) | import config P1→4 VMs/DB0/ports 80xx ; P2→4 VMs/DB1/ports 81xx ; "all"→8 VMs, inchangé |
| 2 | Scripts de lancement des 2 stacks + relais partagé | 1 | infra (conversation) | 2 hubs (:8000/:8100), relais :8010, chacun sonde ses 4 VMs |
| 3 | Bridge : partition des mesures par provider | 1 | infra (conversation) | chaque orchestrateur reçoit ses 4 latences |
| 4 | Validation phase 1 : 2 stacks isolées, métriques OK, pas de collision Redis | 1 | tout | `/status` des 2 hubs cohérents, DB Redis distinctes |
| — | **DISCUSSION : qui héberge / qui décide** | 2 | — | (design coordination) |
| 5 | Actif décide / standby répond (kubectl = vérité) | 2 | dépôt (prompt) | standby ne migre pas |
| 6 | Relais : broadcast d'intention à tous | 2 | dépôt (prompt) | intention → 2 orchestrateurs |
| 7 | Handoff via relais (2-providers → N) + validation end-to-end | 2 | tout | handoff réel, un seul hôte à la fois |

## Non-régression

- `PROVIDER_ID="all"` + `MULTI_PROVIDER_ENABLED=false` = comportement mono-processus
  actuel, bit-identique. Aucun test cassé (baseline 184).
- Le mode distribué est **opt-in** (variables d'env), jamais le défaut.

## Questions ouvertes (à discuter le moment venu)

1. **Comparaison inter-provider N-way** : passer de `negotiate` 2-way à une
   collecte d'offres de N pairs + repli séquentiel. Principe conservé
   (score de violation, dead-band, conformité prioritaire), structure à concevoir.
2. **Gestion fine du split-brain** pendant la fenêtre de handoff (les deux se
   croient actifs 1 cycle) : idempotence + cooldown suffisent-ils, ou faut-il un
   verrou léger via le relais ?
3. **Réplication du relais** (haute dispo) : hors périmètre initial, mais le
   design stateless la rend possible.
