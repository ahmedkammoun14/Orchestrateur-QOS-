# Suivi de réalisation — Multi-Provider Transversal

> Journal des étapes effectivement réalisées et **vérifiées**.
> Plan de référence : [PLAN_MULTI_PROVIDER_TRANSVERSAL.md](PLAN_MULTI_PROVIDER_TRANSVERSAL.md)
> Base de départ : `master` @ `aef5d75` ("Replace and Additive").
>
> Protocole : conception + prompt → exécution par LLM externe → **vérification
> indépendante** → consignation ici. Une étape n'est inscrite qu'après vérification
> réelle du code (pas sur déclaration de l'exécuteur).

---

## Tableau d'avancement

| # | Étape | Statut | Date | Vérifiée |
|---|---|---|---|---|
| 1 | Registre providers transversal (déclaratif) | ✅ **FAIT** | 2026-07-21 | ✅ oui |
| 2 | Objet `SLOIntent` formel | ✅ **FAIT** | 2026-07-21 | ✅ oui |
| 3 | Module d'arbitrage pur (`provider_arbitration.py`) | ✅ **FAIT** | 2026-07-21 | ✅ oui |
| 4 | Protocole de négociation (Cas 5) | ✅ **FAIT** | 2026-07-21 | ✅ oui |
| 5 | Microservice de passation + `/intent/relay` | ✅ **FAIT** | 2026-07-22 | ✅ oui |
| 6 | Machine à états du hub (Cas 1→5) | ✅ **FAIT** | 2026-07-22 | ✅ oui |
| 7 | Métriques groupées par provider | ⬜ à faire | — | — |
| 8 | Partition Kubernetes réelle | ⬜ à faire | — | — |
| 9 | Traçabilité + dashboard | ✅ **FAIT** | 2026-07-22 | ✅ oui |
| 10 | Validation expérimentale | ⬜ à faire | — | — |
| 11 | Doc version distribuée | ⬜ à faire | — | — |

---

## Étape 1 — Registre providers transversal ✅

**Réalisée le 2026-07-21. Vérifiée indépendamment.**

### Objectif
Poser la source de vérité déclarative provider→VMs, avec lookup inverse et
validation. **Purement déclaratif : zéro changement de comportement runtime**
(aucun service ne lit encore le registre).

### Contenu livré

| Fichier | Modification |
|---|---|
| `shared/config.py` | Ajout de `PROVIDER_REGISTRY` (provider-1 = {edge1, cloud1}, provider-2 = {edge2, cloud2}) et du dérivé `PROVIDER_OF_VM` (lookup inverse VM→provider), insérés après `VM_CLUSTER_MAP` |
| `shared/models.py` | Ajout de `validate_provider_registry()` — couverture exacte de `VM_REGISTRY`, pas de doublon, pas de VM inconnue, pas de provider vide. Import différé de `config` (anti-cycle). Non câblée au démarrage (volontaire) |
| `tests/unit/test_provider_registry.py` | **Nouveau** — 11 fonctions / 14 cas de test |

### Décision de conception actée

> **L'axe `provider` (propriété/business) est ORTHOGONAL à l'axe `cluster`/tier
> (edge vs cloud).** Un provider possède un parc mixte qui chevauche les deux
> clusters. Par conséquent :
> - `VM_CLUSTER_MAP` reste **VM→cluster** et n'est **PAS** dérivé du provider.
> - Il n'y a **pas** de `cluster_name` unique ni de `latency_model` unique par
>   provider (contrairement au modèle par-cluster de l'ancienne branche
>   `claude/admiring-ellis-a33f4d`, où provider = cluster).
> - Le modèle physique de latence appartient au **tier de la VM**, pas au provider.
>   Son rattachement sera tranché à l'étape 3/4, quand un consommateur réel en
>   aura besoin.

Cette décision est verrouillée par le test `test_provider_est_transversal_edge_et_cloud`,
qui asserte que chaque provider couvre bien `{edge-cluster, cloud-cluster}`.

### Preuves de vérification

```
$ git status --short          → seuls config.py, models.py (M) + test_provider_registry.py (??) touchés
$ pytest tests/unit/ -q       → 14 passed
$ python -c "from shared import config; print(config.PROVIDER_OF_VM)"
{'edge1': 'provider-1', 'cloud1': 'provider-1', 'edge2': 'provider-2', 'cloud2': 'provider-2'}
$ python -c "from shared.models import validate_provider_registry; validate_provider_registry(); print('OK')"
OK
```

Contrôles manuels effectués sur le diff : périmètre respecté (aucun autre fichier),
orthogonalité préservée (`VM_CLUSTER_MAP` intact), `PROVIDER_OF_VM` bien dérivé de la
source unique, import différé présent, et les 4 tests négatifs déclenchent chacun la
branche de validation visée (vérifié par construction des cas).

### Réserves connues (non bloquantes, à traiter plus tard)

1. Les 4 assertions `pytest.raises(ValueError)` sont **sans `match=`** : elles
   pourraient passer pour la mauvaise raison après un refactor de la validation.
   → À renforcer avec `match=` lors d'une passe qualité.
2. `shared/models.py` n'a **pas de newline final** (préexistant, non régressé).

### Note de contexte

Les 6 autres fichiers de `tests/unit/` (`test_topsis.py`, `test_mi_scoring.py`,
`test_llm_handler.py`, `test_redis_client.py`, `test_violation_detector.py`,
`test_adaptive_percentile.py`) sont **vides (0 octet)**. Le « la suite existante
reste verte » est donc vrai mais sans contenu : il n'existait aucune couverture
unitaire réelle avant cette étape. À considérer comme dette technique du projet.

---

## Étape 2 — Objet `SLOIntent` formel ✅

**Réalisée le 2026-07-21. Vérifiée indépendamment.**

### Objectif
Définir l'objet qui matérialise le principe anti « téléphone arabe » : l'intention
est convertie **une seule fois** en SLOs, et c'est cet objet formel qui sera relayé
d'un provider à l'autre — jamais le texte brut. **Modèle seul, aucun recâblage** :
`intent_manager`, le hub et `IntentToHubPayload` sont intacts.

### Contenu livré

| Fichier | Modification |
|---|---|
| `shared/models.py` | Ajout de `_MODES_VALIDES` et de la dataclass **`SLOIntent`** (`frozen`) : `intent_id`, `slos: Tuple[SLO, ...]`, `mode`, `created_at`, `source_text`, `service`, `attempted_providers`. Méthodes `__post_init__` (normalisation liste→tuple + validation), `has_attempted`, `with_attempt`, `to_dict`, `from_dict`. Newline finale ajoutée |
| `tests/unit/test_slo_intent.py` | **Nouveau** — 21 tests |

### Décisions de conception actées

- **`deadline_ms` supprimé** de la proposition initiale : une deadline est
  exprimable comme un SLO. L'avoir en champ de haut niveau créerait deux sources
  de vérité pour une même contrainte. **Les contraintes vivent uniquement dans `slos`.**
- **`source_text` conservé en PROVENANCE SEULE** : utile au dashboard/audit, mais
  interdiction documentée de le ré-interpréter ou d'en ré-extraire des SLOs.
- **`service` en `Optional`, prospectif** : pas encore alimenté par le pipeline.
- **`attempted_providers` + `with_attempt()` ajoutés** (hors plan initial) : trace des
  providers déjà tentés. Double rôle — **anti-boucle infinie** en version distribuée
  (P1→P2→P1→… ) et **traçabilité** pour le dashboard. `with_attempt` retourne une
  nouvelle instance et lève `ValueError` si le provider a déjà été tenté (re-tenter
  le même provider = bug de logique de relais, pas un cas nominal).
- **Immuabilité du conteneur, pas du contenu** : `frozen` + `slos` en tuple empêchent
  de réassigner un champ ou d'ajouter/retirer un SLO. Les objets `SLO` restent
  mutables (les geler casserait `metrics_handler` qui modifie `weight`/`threshold`).

### Preuves de vérification

```
$ pytest tests/unit/ -q     → 35 passed (14 registre + 21 SLOIntent)
$ smoke test round-trip     → OK
$ git diff --stat           → seuls shared/config.py et shared/models.py modifiés
```

Contrôles manuels : `FrozenInstanceError` bien levée sur réassignation ; `with_attempt`
utilise `dataclasses.replace` et laisse l'originale strictement inchangée ; round-trip
`from_dict(to_dict())` fidèle sur tous les champs y compris le détail des SLO ;
`to_dict()` renvoie des listes (JSON-sérialisable).

### ⚠️ Découverte de vérification — fuite de mutation entre intentions relayées

Test adverse **non couvert par la suite livrée** :

```
with_attempt() → l'objet SLO est PARTAGÉ avec l'intention d'origine  (identité: True)
mutation d'un SLO sur l'intention relayée → visible sur l'ORIGINALE   (fuite: True)
from_dict()    → SLOs reconstruits → isolation correcte              (isolé: True)
```

**Conséquence.** `metrics_handler` mute les SLOs (`s.weight`, `s.threshold`). Donc :

- Relais **HTTP/JSON** (`/intent/relay`) → SLOs reconstruits par `from_dict` →
  **isolation naturelle, aucun risque**.
- Relais par **appel de fonction interne** → provider-2 pourrait **corrompre
  rétroactivement** l'intention telle que vue par provider-1. C'est le mal du
  « téléphone arabe » par **mutation** au lieu de ré-interprétation.

➡️ **Ceci transforme l'arbitrage #3 (relais HTTP réel) d'un argument de
« préparation au distribué » en un argument de CORRECTNESS.**

**✅ DÉCISION PRISE (2026-07-21) — option (a) retenue :** on ne durcit pas
`with_attempt` ; l'isolation est assurée par la **sérialisation HTTP/JSON**.
L'arbitrage #3 du plan est verrouillé sur le **relais HTTP réel**.

> **INVARIANT À PROTÉGER.** Toute transmission d'un `SLOIntent` d'un contexte
> provider à un autre DOIT passer par `to_dict()`/`from_dict()`. Aucun relais par
> appel de fonction interne n'est autorisé : il partagerait les objets `SLO`
> mutables et rouvrirait la fuite de mutation. À faire respecter aux étapes 5 et 6.

### Réserve mineure reportée

Comme à l'étape 1, les assertions `pytest.raises(ValueError)` sont sans `match=`.
À renforcer lors d'une passe qualité globale.

---

## Étape 3 — Module pur d'arbitrage inter-provider ✅

**Réalisée le 2026-07-21 (+ 1 correctif). Vérifiée indépendamment.**

### Objectif
Répondre, pour un provider donné : quelles sont ses VMs **conformes** (à passer à
TOPSIS), et sinon quelle **offre de repli** proposer à la négociation. Module **PUR** :
aucun réseau, aucune I/O, testable sans le moindre mock.

### Contenu livré

| Fichier | Contenu |
|---|---|
| `hub/provider_arbitration.py` | **Nouveau.** `VMEvaluation`, `ProviderOffer`, `ProviderAssessment` (toutes `frozen`) ; `candidates_for_provider`, `evaluate_vm`, `evaluate_provider` ; helpers `_as_slo_dict`, `_applicable_slos`, `_representative_value`, `_excess` |
| `tests/unit/test_provider_arbitration.py` | **Nouveau.** 30 tests, zéro mock |

### Décisions de conception actées

- **Le module PARTITIONNE, il ne décide pas.** `compliant_vms` (→ TOPSIS, côté hub) et
  `best_effort_vm` (→ négociation) sont deux sorties distinctes. TOPSIS n'est jamais
  appelé ici.
- **Réutilisation des primitives existantes** plutôt que réécriture :
  `vm_satisfies_slo`, `TopsisSelector.calculate_weighted_mean`, `_to_criterion_value`
  importés de `services/decision_intelligence/topsis.py`. Garantit la cohérence avec
  la décision intra-provider.
- **Valeur représentative = moyenne pondérée** des prédictions (convention du pipeline),
  **pas** le pire point de l'horizon.
- **Absence de prédiction ⇒ VM non conforme** (règle reprise de `_filter_candidates`),
  mais la **valeur mesurée est acceptée en repli pour le score de violation** — sinon
  une panne ML rendrait toute négociation impossible.
- **Pas de repli sur `default_threshold`** (contrairement à `topsis.py:129`) : il faut
  pouvoir distinguer « aucune information » d'une vraie mesure, sinon `evaluable`
  serait toujours vrai et la neutralité ML down ne se déclencherait jamais.
- **Conformité par vacuité exclue** : `is_compliant = satisfies_all and evaluable`.
  Une VM sans aucun SLO applicable n'est pas un placement sûr.
- **Isolation stricte** : les candidats d'un provider sont ses seules VMs, sans
  exception pour la VM active — un orchestrateur distant ne verrait pas les VMs de
  l'autre provider.

### Correctif appliqué — biais des données manquantes

Défaut détecté **en vérification**, issu d'une erreur de spécification (formule initiale
`Σ wᵢeᵢ` sans renormalisation) :

```
Avant : P1 (2 métriques, excès 0.10 chacune) = 0.10
        P2 (1 métrique,  excès 0.10)         = 0.05   ← gagne par MANQUE de données
```

La part de poids d'un SLO écarté disparaissait du total, faisant baisser mécaniquement
le score → **biais systématique en faveur du provider le moins instrumenté**, qui aurait
fait migrer vers le mauvais provider au Cas 5.

**Correctif :** `score = Σ(wᵢ × excèsᵢ) / Σwᵢ` sur les seuls SLOs retenus — le score
devient une **moyenne pondérée**, invariante au nombre de métriques évaluées.
Après correctif : `P1 = P2 = 0.10`. Scénario de référence inchangé (0.0667 / 0.0333).

### Preuves de vérification

```
$ pytest tests/unit/ -q   → 65 passed (14 + 21 + 30)
$ grep -nE "httpx|requests|redis|aiohttp|asyncio|open\(" hub/provider_arbitration.py → vide
$ grep -n "\.select("  hub/provider_arbitration.py                                   → vide
$ smoke Cas 5 : P1 edge1 0.0667 | P2 edge2 0.0333  → scénario de référence intact
$ smoke biais : P1 = 0.1  P2 = 0.1                 → biais supprimé
```

### Correction d'une erreur de MA spécification

Mon prompt donnait l'exemple `{"vm_id":"edge1","latency":45.0}`, incompatible avec
l'usage de `payload_key` imposé par ailleurs : pour `latency`, `payload_key` vaut
**`rtt_ms`**. Vérifié dans `hub/orchestrator_core.py:559`
(`entry[meta["payload_key"]] = lc.get(m)`). L'exécuteur a tranché correctement en
suivant `meta.get("payload_key", metric)`, comme `topsis.py:127`.

### ⚠️ Limite connue reportée à l'étape 4 — asymétrie de critères

Le correctif supprime le biais lié au **nombre** de métriques, mais deux offres à `0.10`
peuvent toujours porter sur des **jeux de critères différents** (P1 = moyenne sur
{latency, cpu}, P2 = moyenne sur {latency} seule — le CPU de P2 étant simplement
inconnu). Reproduit et confirmé.

`ProviderOffer` ne transporte que `{provider_id, vm_id, violation_score}` : **rien ne
signale l'asymétrie** au provider distant.

➡️ **Exigence pour l'étape 4** : enrichir `ProviderOffer` de la liste des métriques
effectivement évaluées, et traiter explicitement le cas asymétrique dans la règle de
négociation. Détail dans le plan.

---

## Étape 4 — Protocole de négociation (Cas 5) ✅

**Réalisée le 2026-07-21 (+ 1 correctif de conception). Vérifiée indépendamment.**

### Objectif
Coder la règle qui, lorsque **aucun** provider n'a de VM conforme, décide laquelle des
deux offres de repli l'emporte — et donc où part le service.

### Contenu livré
`NegotiationDecision` (str, Enum), `NegotiationResult` (frozen) et `negotiate(...)`
ajoutés à `hub/provider_arbitration.py`, avec 24 tests.

**Les 4 branches, dans cet ordre impératif :**
1. `local.compliant_vms` non vide → `PREND_LOCAL_CONFORME` (TOPSIS départagera).
   ⚠️ Tester `compliant_vms`, **pas** `is_compliant` : par la neutralité « ML down »,
   un provider non évaluable a `is_compliant=True` sans avoir aucune VM à proposer.
2. Pas d'offre reçue → `PREND_LOCAL_MEILLEURE` ou `AUCUNE_OPTION`.
3. Rien d'exploitable localement → `CEDE_A_L_OFFRE`.
4. Comparaison des deux offres → `PREND_LOCAL_MEILLEURE` / `CEDE_A_L_OFFRE`.

### Correctif de conception — dead-band ABSOLU au lieu d'une marge relative

La marge initiale était **relative** (`challenger < tenant × 0.95`). Défaut détecté en
vérification : le score de violation étant lui-même un **excès relatif au seuil**, une
marge en pourcentage du score n'exige quasiment rien près du seuil.

Amélioration exigée du challenger (seuil latence 100 ms) :

| tenant à | son score | marge RELATIVE 5 % | dead-band ABSOLU 0.05 |
|---|---|---|---|
| 101 ms | 0.010 | **0.05 ms** | 1.00 ms |
| 150 ms | 0.500 | 2.50 ms | 5.00 ms |
| 300 ms | 2.000 | **10.00 ms** | 5.00 ms |

Simulation sur 8 cycles, deux providers oscillant entre 102 et 106 ms :
**7 migrations inter-provider avec la marge relative, 0 avec le dead-band.**

➡️ `NEGOTIATION_MARGIN` → **`NEGOTIATION_DEADBAND = 0.05`**, lu comme « il faut gagner
au moins 5 % du seuil SLO » (5 ms pour un seuil à 100 ms). Grandeur discutable avec un
opérateur, contrairement à un pourcentage de score.

### Règle du dead-band — portée exacte

| Contexte | Tenant ? | Règle |
|---|---|---|
| Déploiement initial | Non | **Comparaison stricte**, `deadband_applied = 0.0` |
| Migration en fonctionnement | Oui | **Dead-band en faveur du tenant** |

Le dead-band n'intervient **que** à la branche 4. La conformité (branche 1) l'emporte
toujours : dès qu'un provider a une VM conforme, il prend le service quel que soit
l'écart de score.

### Preuves de vérification

```
$ pytest tests/unit/ -q                                        → 92 passed
$ grep -n "NEGOTIATION_MARGIN\|margin_applied" ...             → vide (renommage complet)
$ greps de pureté et de non-appel à select()                   → vides
$ smoke anti-oscillation (8 cycles)                            → 0 migration
```

### Note — mon smoke test initial était juste, ma règle ne l'était pas

Le smoke test que j'avais fourni à l'étape 4 échouait sur sa 3ᵉ assertion, et
l'exécuteur a eu raison de **ne pas tordre le code pour le satisfaire**. Après passage
au dead-band absolu, **il passe intégralement**. L'intention exprimée (« 1 ms d'écart ne
doit pas déplacer le service ») était donc correcte dès le départ ; c'est la marge
relative qui était incapable de l'exprimer.

### Limite connue reportée
L'asymétrie de critères entre offres (jeux de métriques différents) reste non traitée —
version minimale retenue volontairement. Voir l'encadré de l'étape 4 dans le plan.

---

## Étape 5 — Microservice de passation + endpoint de réception ✅

**Réalisée le 2026-07-22. Vérifiée indépendamment.**

> **Changement d'ordre décidé avant réalisation.** Le microservice de transport passe
> AVANT la machine à états (qui devient l'étape 6). Motif : le relais P1↔P2 est
> verrouillé en HTTP/JSON ; construire la machine à états d'abord aurait imposé une
> boucle interne provisoire, jetée ensuite, **et violant l'invariant de relais
> sérialisé**. On construit le transport, puis la logique qui l'utilise.

### Contenu livré

| Fichier | Contenu |
|---|---|
| `services/provider_relay/app.py` | **Nouveau.** Microservice FastAPI **port 8010** — transport pur. `POST /handoff`, `GET /health` |
| `hub/orchestrator_core.py` | Extraction de `_build_candidates()` (comportement identique) + **nouvel endpoint `POST /intent/relay`** |
| `shared/config.py` | `PROVIDER_RELAY_PORT`, `PROVIDER_RELAY_SERVICE_URL`, **`PROVIDER_ORCHESTRATOR_URL`** (table de routage) |
| `hub/provider_arbitration.py` | `NegotiationResult.to_dict()` (le résultat traverse le réseau) |
| `README.md` | Ligne `Provider Relay | 8010` au tableau des ports |
| `tests/unit/test_hub_relay_endpoint.py` | **Nouveau** — 10 tests |
| `tests/unit/test_provider_relay.py` | **Nouveau** — 7 tests |

### Le chemin de passation

```
hub (rôle provider-1)  ──POST /handoff──►  provider_relay :8010
                                                  │ table de routage
                                                  ▼
                                          POST /intent/relay  ──►  hub (rôle provider-2)
                                                  │                évalue SES VMs, négocie
hub (rôle provider-1)  ◄───── réponse ────────────┘
```

### Décisions de conception actées

- **La topologie vit dans un seul endroit** : `PROVIDER_ORCHESTRATOR_URL`. En
  mono-processus les deux entrées pointent sur le même hub. **Passer à N orchestrateurs
  réels = changer ces URLs, et rien d'autre dans tout le projet.**
- **`provider_relay` est un transport pur** : il n'importe ni `provider_arbitration`,
  ni `shared.models`. Il manipule du JSON opaque, sauf une seule lecture —
  `attempted_providers`, pour la garde anti-boucle.
- **Garde anti-boucle (409)** : refus de relayer vers un provider déjà présent dans
  `attempted_providers`. C'est elle, et elle seule, qui rend sûr le passage à N
  orchestrateurs — sans elle, deux providers non conformes se renverraient
  indéfiniment la même intention.
- **`/intent/relay` est purement observatoire** : il lit `state.last_collected` et
  `state.snapshot_predictions`, calcule, répond. **Aucune écriture dans `state`,
  aucune migration déclenchée.** Vérifié par grep et par test comparatif avant/après.
- **`_build_candidates` extrait** de `_step8_decide` pour être partagé sans dupliquer
  la convention `payload_key` (`latency` → `rtt_ms`). Comportement identique, testé.

### Preuves de vérification

```
$ pytest tests/unit/ -q                          → 109 passed (92 + 17)
$ grep dans _step8_decide : relay|handoff|...    → aucun appel au relais
$ grep "state\.[a-z_]+ *=" dans l'endpoint       → aucune écriture
$ imports de services/provider_relay/app.py      → config + logging_utils uniquement
$ curl :8010/health                              → table de routage exposée
$ curl :8010/handoff (bout en bout, 2 serveurs)  → negotiation cohérente,
                                                   relayed_by + target_orchestrator présents,
                                                   GET /status identique avant/après
```

### Note pour l'étape 6

`/intent/relay` renvoie un champ `attempted_providers` **enrichi** du provider qui vient
de répondre. La machine à états devra utiliser **cette liste retournée** — et non
l'intention d'origine — si elle relaie plus loin. Sans importance à 2 providers,
indispensable à N.

---

## Étape 6 — Machine à états multi-provider ✅

**Réalisée le 2026-07-22. Vérifiée indépendamment.**
**Première étape à modifier le cœur du cycle d'orchestration.**

### Découverte préalable qui a simplifié la conception

`state.service_vm` est initialisé à `next(iter(config.VM_REGISTRY))`
(`orchestrator_core.py:54`) puis synchronisé avec la VM réellement active via
`openstack_client`. **Le service a donc toujours un emplacement** : il n'existe aucun
état « pas encore déployé ».

➡️ Les **Cas 1 et 2** (déploiement initial) sont **structurellement identiques** aux
**Cas 3 et 4** (migration en fonctionnement) : dans les deux cas il y a un tenant à
protéger, donc le dead-band s'applique uniformément. Les 5 cas de la spécification se
ramènent à **3 chemins de code** :

| Chemin | Situation | Action | Cas |
|---|---|---|---|
| **A** | Provider courant a ≥ 1 VM conforme | TOPSIS sur ces conformes → stay ou migration intra-provider | 1, 3 |
| **B** | Aucune chez lui, l'autre en a | Passation → l'autre choisit par TOPSIS → migration inter-provider | 2, 4 |
| **C** | Aucun des deux | Négociation sur les offres de repli | 5 |
| **D** | Rien d'exploitable | `PLACEMENT_IMPOSSIBLE` → STAY tracé | — |

### Contenu livré

| Fichier | Contenu |
|---|---|
| `shared/config.py` | `MULTI_PROVIDER_ENABLED` (défaut **`False`**) |
| `hub/orchestrator_core.py` | `_step8_decide` devient un **dispatcher** de 4 lignes ; corps d'origine déplacé tel quel dans `_decide_mono_provider` ; nouvelle `_decide_multi_provider` (machine à états) + helper commun ; `/intent/relay` étendu avec **`local_topsis`** |
| `tests/unit/test_multi_provider_flow.py` | **Nouveau** — 16 tests, aucun socket ouvert |
| `tests/unit/test_hub_relay_endpoint.py` | Mock de `_post` ajouté + 2 tests `local_topsis` |

### Complément apporté à l'étape 5 — `local_topsis`

Manque identifié en conception : `/intent/relay` renvoyait `compliant_vms` mais aucune
VM concrète. Or **provider-1 ne doit pas choisir parmi les VMs de provider-2** — c'est
au provider receveur de faire tourner TOPSIS sur ses propres conformes. L'endpoint
appelle donc désormais `decision_intelligence/decide` sur son sous-ensemble conforme et
renvoie `local_topsis: {to_vm, topsis_score, reason}`. Repli sur la première VM conforme
si `/decide` échoue. L'endpoint reste **sans effet de bord sur `state`**.

### 🔒 Non-régression — vérifiée indépendamment

```python
# Comparaison difflib entre git HEAD:_step8_decide et le _decide_mono_provider actuel
Lignes comparées : 95      IDENTIQUE : True
```
Seule différence détectée : la **bannière de section qui suit** la fonction
(`# Persistance des mesures` → `# Décision multi-provider`), hors du corps.

```
$ pytest tests/unit/ -q                                → 127 passed (109 + 18)
$ python -c "print(config.MULTI_PROVIDER_ENABLED)"     → False
$ grep -n "MULTI_PROVIDER_ENABLED" orchestrator_core.py → 1 seule occurrence (l.588)
$ hub démarre flag OFF et flag ON                       → healthy dans les deux cas
```

### Décisions actées

- **Interrupteur OFF par défaut** : le pipeline se comporte exactement comme avant.
  Repli immédiat possible le jour de la soutenance, et dispositif de mesure
  mono vs multi-provider pour l'étape 10.
- **Chemin C, `cede_a_l_offre`** : la VM de destination vient de
  `assessment.best_effort_vm` calculé **localement**, jamais de la réponse du relais.
- **Robustesse** : toute passation ratée (relais injoignable, 409 anti-boucle,
  intention invalide, provider hors registre) retombe sur **STAY**, jamais une
  exception. Le cycle ne peut pas être interrompu par la couche multi-provider.
- **`state.service_vm` hors `PROVIDER_OF_VM`** → repli automatique sur le chemin
  mono-provider avec warning.

---

## Étape 9 — Traçabilité et dashboard ✅

**Réalisée le 2026-07-22. Vérifiée indépendamment.**

### Objectif
Rendre **visibles** les 5 cas. Le système savait les exécuter depuis l'étape 6, mais une
migration inter-provider était indiscernable d'une migration ordinaire dans le journal.

### Bug d'affichage corrigé
Le dashboard mappait `breach_type` sur `'proactive'`/`'réactive'`, sinon chaîne vide.
Le chemin multi-provider émettant `breach_type = "inter_provider_negotiation"`, la phrase
affichée devenait « Violation  détectée — TOPSIS a sélectionné… » : un trou dans le
texte, et une négociation présentée comme une migration banale. Remplacé par un
dictionnaire `BREACH_FR` extensible.

### Contenu livré (un seul fichier de code : `services/observability/app.py`)
- **Badge de chemin** dans le journal : `INTRA` (A, vert), `INTER` (B, orange),
  `NÉGO` (C, violet), `IMPOSSIBLE` (D, rouge), avec infobulle explicative.
- **`provider_used`** affiché à côté du badge.
- **Phrase adaptée par chemin** — le chemin A conserve la formulation historique.
- **Compteur d'en-tête** `INTRA / INTER / NÉGO / IMPOSSIBLE`, cumulé côté backend
  (`_provider_path_counts`) et diffusé via SSE. **C'est lui qui prouvera en soutenance
  que les cinq cas se produisent réellement.**
- `tests/unit/test_observability_multi_provider.py` — 11 tests.

### Deux corrections pertinentes hors spec, apportées par l'exécuteur

1. **Filtre du journal assoupli** : `if (dec !== 'migrate' && !e.provider_path) return;`
   Sans cela, le **chemin D** (`PLACEMENT_IMPOSSIBLE`, toujours un STAY) n'apparaîtrait
   jamais dans le tableau. **Non-régression vérifiée** : pour une entrée mono-provider,
   `!e.provider_path` vaut `true`, la condition se réduit à `dec !== 'migrate'` — soit
   exactement le filtre d'origine.
2. **`decBadge` était câblé en dur sur `"MIGRATION"`** — sans risque tant que seules des
   migrations passaient le filtre, mais il aurait affiché « MIGRATION » sur une ligne D.
   Rendu conditionnel (`MAINTIEN` sinon).

### Preuves de vérification
```
$ pytest tests/unit/ -q                              → 138 passed (127 + 11)
$ git status (fichiers suivis)                       → services/observability/app.py seul
$ grep du filtre                                     → mono-provider bit-identique
$ curl des 4 chemins                                 → A:200 B:200 C:200 D:200
$ compteur après injection                           → {'A':1,'B':1,'C':1,'D':1}
$ audit SANS provider_path                           → 200, entrée sans clé parasite,
                                                       compteur inchangé
```

### Non implémenté (conforme à la consigne)
Le filtre « INTER + NÉGO » : aucun mécanisme de filtrage n'existe dans le dashboard
actuel, et la spec précisait de ne pas en créer.

---

## Étape 9+ — Dashboard de raisonnement (2026-07-22, après tests réels)

Trois travaux menés après le premier test multi-provider réel (seuil 100, flag ON,
cycles 11-49 observés). Tous vérifiés indépendamment.

### Correctif — TOPSIS jamais exécuté sur le chemin de passation
Défaut détecté en production : sur le chemin B, `/intent/relay` désignait une VM
**conforme** comme `service_vm`, si bien que `decision.py` ne détectait aucune violation
et court-circuitait TOPSIS (« repli sur la première VM conforme »). Corrigé en
transmettant la **VM active de l'émetteur** (`incumbent_vm`) de bout en bout (hub →
provider_relay → /intent/relay). `decision.py` détecte alors la violation, filtre les
candidats, et TOPSIS classe les conformes du receveur. Preuve : le nouveau test échoue
sur l'ancien code (`assert 'edge2' == 'cloud2'`), passe sur le corrigé. **157 tests.**

### Bloc `reasoning` dans l'audit
`hub/orchestrator_core.py` : ajout d'un bloc `reasoning` au payload d'audit
multi-provider (`provider_courant`, `evaluations` par VM avec score de violation,
`compliant_vms`, `negotiation`, `topsis`). Construction défensive — une erreur n'empêche
jamais l'envoi de l'audit. **Chemin mono-provider inchangé** (aucune clé ajoutée quand
`MULTI_PROVIDER_ENABLED=False`). **166 tests.**

### Classement TOPSIS exposé + panneau de raisonnement
- `decision.py` (**première modification depuis le début du projet**) : `vm_scores`
  (classement complet, déjà calculé mais jamais renvoyé) ajouté aux retours `migrate` et
  au STAY d'hystérésis. **Non-régression vérifiée** : 4 des 5 appels à `_build_stay`
  inchangés, clé conditionnelle (absente si `None`).
- `hub` : bloc `reasoning.topsis` (classement, VM retenue, score).
- `services/observability/app.py` : **suppression** de « Latence — historique &
  prédictions » (doublon du simulateur PiCar) et « Détail des SLOs actifs » (doublon de
  « Poids SLOs actifs »). **Ajout** du panneau « Raisonnement du cycle » — 5 étapes :
  SLOs actifs → violation → évaluation par VM → TOPSIS (chemin A) ou négociation
  (chemin C) → décision + raison. Muet en mono-provider. **181 tests.**

Rendu vérifié en navigateur réel — chemin A affiche le classement TOPSIS (`cloud2 0.85
← retenue`), chemin C affiche les deux offres et le dead-band. Justifie donc *pourquoi
cette VM* à chaque décision.

**Écart assumé :** l'étape « violation » du chemin C n'affiche pas PROACTIVE/REACTIVE
(donnée produite par `ViolationDetector`, jamais invoqué hors chemin A) — remplacé par
l'excès réel par métrique, seule information déterminable.

---

## ⚠️ Dette constatée — le dépôt est en retard sur la production (2026-07-21)

Comparaison entre les fichiers réellement déployés (VMs + PiCar) et les versions
versionnées :

| Fichier | État du dépôt |
|---|---|
| `infrastructure/picar_bridge.py` | ✅ à jour (seul un commentaire « port 5000 » est faux, le code utilise 5001) |
| `infrastructure/picarx_sim.html` | ❌ latence **homogène** `FML={B:5,A:150}` ; le déployé a une latence **par tier** `{edge:{B:5,A:150}, cloud:{B:30,A:210}}` |
| `infrastructure/vm_ping/*.py` | ❌ ancienne génération (Jun 22) ; le déployé (`*_fixeCarac*.py`) ajoute `SIM_PROFILES` CPU/RAM, `TOTAL_CORES`/`TOTAL_RAM_GB` et les B/A hétérogènes |

**Impact fonctionnel, pas cosmétique.** Le `bestVmAt` du dépôt sélectionne la VM la plus
proche en **distance**, alors que le déployé sélectionne la **latence minimale** en
tenant compte du tier. Avec `cloud B=30` vs `edge B=5`, un edge à 20 cm (≈37 ms) bat un
cloud à 10 cm (≈46 ms) : **le simulateur du dépôt colorie les zones de façon fausse**.

De plus, les scripts VM du dépôt ne déclarent pas la capacité
(`TOTAL_CORES`/`TOTAL_RAM_GB`), dont `topsis.py` a besoin pour convertir l'usage % en
disponibilité absolue. Le dépôt seul ne permet donc pas de reproduire la démo.

**Décision utilisateur (2026-07-21) :** le profil RAM d'`edge2` a été ramené de
`(50,90)` à `(35,60)`, identique à `edge1` — les deux providers sont donc désormais
**symétriques** sur les ressources simulées. Hypothèse à conserver pour l'interprétation
des scénarios de négociation.

---

## Statut contrat

Aucun commit, aucun push n'a été effectué sur ce travail. Les modifications sont
locales et en attente de décision.
