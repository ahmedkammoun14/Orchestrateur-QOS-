# Plan — Multi-Provider Transversal avec négociation inter-provider

> **Statut :** document de planification. Aucun commit / push.
> **Base de départ verrouillée :** `master` @ `aef5d75` ("Replace and Additive").
> **Ancienne branche `claude/admiring-ellis-a33f4d` : ABANDONNÉE.** Aucun de ses
> composants n'est réutilisé — la logique multi-provider ci-dessous est entièrement
> nouvelle. La branche est conservée en archive locale uniquement.
> **Dernière mise à jour :** 2026-07-21.

---

## 1. Objectif

Orchestration multi-provider **transversale** avec **un seul orchestrateur** qui
change de rôle (contexte provider), communiquant **toujours par requêtes HTTP** —
ce qui prépare sans réécriture la version à deux orchestrateurs réels.

- **Provider 1 = { edge1, cloud1 }**
- **Provider 2 = { edge2, cloud2 }**

Chaque provider possède son parc mixte edge+cloud et **ne voit pas** les VMs de l'autre.

---

## 2. Les 5 cas de fonctionnement (spécification de référence)

Le fonctionnement est **symétrique** : ce qui est décrit pour P1→P2 vaut pour P2→P1
si le service démarre chez provider-2.

> ### 🔑 Règle transversale — quand TOPSIS intervient (et quand il n'intervient pas)
>
> **TOPSIS ne sert QU'À départager des VMs qui respectent DÉJÀ les SLOs, à
> l'intérieur d'un MÊME provider.** Il s'applique donc sur le **sous-ensemble
> conforme** des VMs d'un provider, jamais sur son parc entier.
>
> - **≥ 1 VM conforme** chez un provider → TOPSIS sur ce sous-ensemble → décision.
>   Le score de violation ne sert pas.
> - **0 VM conforme** → le provider passe la main. Si *aucun* provider n'a de VM
>   conforme → **négociation sur le score de violation** (Cas 5). TOPSIS
>   n'intervient alors pas du tout.

### Cas 1 — Déploiement initial sur le provider courant
L'utilisateur envoie son intention. L'orchestrateur, en rôle provider courant,
évalue **ses seules VMs** et identifie celles qui respectent les SLOs.
Si au moins une VM est conforme → **TOPSIS sur les VMs conformes** →
**déploiement** sur la gagnante.

### Cas 2 — Déploiement sur l'autre provider
Si **aucune** VM du provider courant ne satisfait les SLOs → l'orchestrateur
**change d'état** et devient l'orchestrateur de l'autre provider. Il analyse
**uniquement** les VMs de ce second provider. Si au moins une VM est conforme →
**TOPSIS sur les VMs conformes** → **déploiement** sur la gagnante.

### Cas 3 — Violation après déploiement : migration intra-provider
Une violation de SLO est détectée sur la VM active. L'orchestrateur cherche
**d'abord chez le provider courant**. Si une VM respecte les SLOs **et** possède un
**meilleur score TOPSIS** → **migration intra-provider**.

### Cas 4 — Migration inter-provider
Si aucune VM du provider courant ne satisfait les SLOs → changement d'état vers
l'autre provider → recherche d'une VM respectant les SLOs → TOPSIS →
**migration inter-provider**.

### Cas 5 — Aucun provider ne satisfait les SLOs : négociation ⭐
1. Le provider courant sélectionne **sa meilleure VM** au sens du **minimum de
   violation** des SLOs (ex. latence 32 ms).
2. Cette **offre** est transmise à l'autre provider **dans la requête HTTP**.
3. Le second provider cherche : une VM respectant les SLOs, ou à défaut **sa
   meilleure VM**.
4. Il **compare** sa meilleure VM à l'offre reçue, et tranche :

| Situation chez le second provider | Décision |
|---|---|
| Il a une VM **respectant** les SLOs | Le service **migre** vers cette VM |
| Pas de VM conforme, mais **sa meilleure est meilleure que l'offre** (ex. 31 < 32) | Le service **migre** vers sa VM |
| Sa meilleure est **moins bonne que l'offre** | Il **répond** au premier que l'offre gagne → le service **reste / migre** vers la VM du premier provider |

**Résumé**

| Cas | Décision |
|---|---|
| 1 | Déploiement sur le provider courant |
| 2 | Déploiement sur l'autre provider |
| 3 | Migration intra-provider |
| 4 | Migration inter-provider |
| 5 | Meilleure VM globale (minimum de violation) — par négociation |

---

## 3. ⚠️ Point technique fondateur : TOPSIS n'est PAS comparable entre providers

TOPSIS applique une **normalisation min-max à l'intérieur de son pool de candidats**.
Un score obtenu sur les VMs de P1 et un score obtenu sur celles de P2 sont calculés
**contre des solutions idéales A⁺/A⁻ différentes** : les comparer n'a aucun sens
mathématique. Le Cas 5 ne peut donc PAS être arbitré sur des scores TOPSIS.

### Grandeur de comparaison retenue : le **score de violation**

Absolu, sans dimension, relatif aux **seuils SLO** (qui sont globaux et partagés par
les deux providers) — donc **comparable entre providers** :

```
Pour chaque SLO i, de seuil t_i et de valeur prédite p_i :

  opérateur "<" ou "<=" :   excès_i = max(0, (p_i − t_i) / t_i)
  opérateur ">" ou ">=" :   excès_i = max(0, (t_i − p_i) / t_i)

  Score de violation(VM) = Σ ( poids_i × excès_i )
```

- `= 0` ⟺ **tous** les SLOs sont respectés (VM conforme).
- `> 0` → ampleur de la violation. **Plus bas = meilleur.**

*Vérification sur l'exemple de la spécification* (seuil latence 30 ms) :
P1 → (32−30)/30 = 0,067 ; P2 → (31−30)/30 = 0,033 → **P2 gagne**, conforme à
l'intuition « 31 ms < 32 ms ».

### Répartition des rôles — règle à ne jamais enfreindre

| Besoin | Outil | Périmètre |
|---|---|---|
| Décider si une VM est **conforme** | Score de violation `== 0` | par VM |
| Décider si un provider est **conforme** | ≥ 1 VM conforme | par provider |
| Départager les VMs **conformes** d'un provider | **TOPSIS** (pool homogène → valide) | **sous-ensemble conforme uniquement** |
| Comparer **entre** providers quand personne n'est conforme (Cas 5) | **Score de violation** (jamais TOPSIS) | offre scalaire inter-provider |

> ⚠️ TOPSIS ne tourne **jamais** sur des VMs non conformes, et **jamais** sur un pool
> mélangeant deux providers.

### Conventions EXISTANTES du code à respecter impérativement

Relevé par lecture de `services/decision_intelligence/` (2026-07-21). Le module
d'arbitrage doit s'y aligner, **pas inventer sa propre convention**.

| Sujet | Convention en place | Où |
|---|---|---|
| « VM qui vérifie les SLOs » | **Déjà implémenté** par `DecisionHandler._filter_candidates` : la VM doit satisfaire **tous** les SLOs | `decision.py` |
| Valeur représentative d'une prédiction | **`TopsisSelector.calculate_weighted_mean(preds)`** — poids `n, n−1, …, 1` (les points proches pèsent plus). **Pas** `max()` | `topsis.py` |
| Absence de prédiction | La VM est **NON conforme** (`if not preds: return False`) | `decision.py` |
| Personne n'est conforme | Repli `preferred if preferred else all_candidates` → tout le monde redevient candidat | `decision.py` |
| SLO en `cores`/`GB` | Convertir % → disponibilité absolue via `TopsisSelector._to_criterion_value` **avant** comparaison | `decision.py`, `violation_detector.py` |
| Test de satisfaction | **`vm_satisfies_slo(mean, slo)`** — gère `<`, `<=`, `>`, `>=`. **À réutiliser** | `topsis.py` |
| Déclenchement de migration | **Gate** : seule une violation de métrique **PRIMAIRE** déclenche ; secondaire seule → STAY | `decision.py` |
| Anti-ping-pong | `_MIGRATION_MARGIN = 0.05` : migrer seulement si `score > actif × 1.05` | `decision.py` |
| Égalité TOPSIS | `_TIE_THRESHOLD = 0.01` : écart négligeable → 0.5 partout (anti-bruit) | `topsis.py` |
| Candidat unique | TOPSIS renvoie score **1.0** d'office | `topsis.py` |

> **⚠️ Deux notions de « violation » coexistent volontairement — ne pas les confondre :**
> - `ViolationDetector` (« la VM active a-t-elle un problème ? ») → **pessimiste**,
>   `any(pred dépasse le seuil)` sur tout l'horizon.
> - `_filter_candidates` (« cette VM est-elle une bonne cible ? ») → sur la
>   **moyenne pondérée** des prédictions.
>
> La conformité d'un provider (Cas 1-5) relève de la **seconde**.

> **Dette technique repérée :** `_budget_score`, `_BUDGET_WEIGHT` et
> `_RELIABILITY_WEIGHT` sont définis dans `topsis.py` mais **jamais utilisés** par
> `select()`, alors que la docstring annonce ces critères. À nettoyer hors de ce plan.

---

## 4. Arbitrages verrouillés

| # | Arbitrage | Décision | Statut |
|---|---|---|---|
| 1 | Base de départ | `master` @ `aef5d75`. Ancienne branche abandonnée, aucun composant réutilisé | ✅ Verrouillé |
| 2 | Critère de conformité d'un provider | Un provider est **conforme** si ≥ 1 de ses VMs a un score de violation nul, évalué sur les **prédictions**. Si les prédictions manquent (ML down) → verdict **neutre = conforme** : on ne relaie/négocie jamais sur données incomplètes | ✅ Verrouillé |
| 3 | Nature du relais P1↔P2 | **HTTP/JSON systématique.** Double justification : (a) exerce le chemin distribué dès le mono-processus ; (b) **correctness** — `from_dict` reconstruit les `SLO` et **isole** les instances, là où un appel interne partagerait des `SLO` mutables (`metrics_handler` réécrit `weight`/`threshold`) → corruption rétroactive de l'intention entre providers | ✅ Verrouillé |
| 4 | Comparaison inter-provider | **Score de violation** (§3), jamais le score TOPSIS | ✅ Verrouillé |
| 5 | Découpage du code | **(a) Logique d'arbitrage** = module **pur** `hub/provider_arbitration.py` (pas un service : c'est du calcul, pas du transport). **(b) Transport / passation de provider** = **microservice dédié** (passerelle de fédération) qui émet l'ordre de changement de provider. **(c) Réception** = endpoint sur le **hub**. Ainsi, passer à N orchestrateurs réels = faire pointer la passerelle vers une adresse distante, sans réécriture | ✅ Verrouillé |

> **INVARIANT — relais sérialisé obligatoire.** Toute transmission d'un `SLOIntent`
> ou d'une offre entre contextes providers DOIT passer par `to_dict()`/`from_dict()`
> (donc HTTP/JSON). Aucun passage par appel de fonction interne.

> ### 🔒 Exigence de non-régression — `MULTI_PROVIDER_ENABLED`
>
> Objectif imposé : **à la fin de toutes les étapes, le pipeline doit fonctionner
> parfaitement.** L'étape 6 étant la seule à modifier le cœur du cycle
> (`_step8_decide`), elle doit être protégée par un interrupteur :
>
> ```python
> MULTI_PROVIDER_ENABLED: bool = os.getenv("MULTI_PROVIDER_ENABLED", "false").lower() == "true"
> ```
>
> - **OFF (défaut)** → `_step8_decide` se comporte **exactement** comme aujourd'hui,
>   au bit près. La démo PiCar reste fonctionnelle quoi qu'il arrive.
> - **ON** → machine à états multi-provider active.
>
> Cela garantit un repli immédiat en cas de problème le jour de la soutenance, et
> permet de **comparer expérimentalement** les deux modes (étape 10) — mono-provider
> contre multi-provider — sur les mêmes trajectoires. Le flag n'est pas une béquille :
> c'est le dispositif de mesure du gain apporté par l'extension.
>
> **Conséquence pour l'ordre des étapes :** le transport (étape 5) est construit AVANT
> la logique qui l'utilise (étape 6). Construire la machine à états d'abord obligerait
> à écrire une boucle interne provisoire, jetée ensuite — et qui violerait l'invariant
> de relais sérialisé ci-dessus.

---

> ### ✅ La partition transversale existe déjà dans l'infrastructure (constaté 2026-07-21)
>
> `openstack_client.py` (déployé sur le master) associe déjà les VMs par **label PoP** :
>
> ```python
> "edge1":  "tc-stream-source-cloud1.yaml",   # PoP: space_1
> "cloud1": "tc-stream-source-cloud1.yaml",   # PoP: space_1
> "edge2":  "tc-stream-source-cloud.yaml",    # PoP: space_2
> "cloud2": "tc-stream-source-cloud.yaml",    # PoP: space_2
> ```
>
> **`space_1 = {edge1, cloud1}` et `space_2 = {edge2, cloud2}` — c'est exactement notre
> partition transversale.** L'adressage d'une VM se fait par la combinaison
> `(contexte kubectl, label PoP)` :
>
> | Contexte | PoP | Node | VM | Provider |
> |---|---|---|---|---|
> | edge-cluster | space_1 | pop1-worker-1 | edge1 | provider-1 |
> | cloud-cluster | space_1 | pop2-worker-1 | cloud1 | provider-1 |
> | edge-cluster | space_2 | pop1-worker-2 | edge2 | provider-2 |
> | cloud-cluster | space_2 | pop2-worker-2 | cloud2 | provider-2 |
>
> Conséquence : **aucun nouveau label Kubernetes à poser**, `openstack_client` reste
> inchangé. L'étape 8 se réduit à documenter la correspondance provider ↔ space.
>
> **Confirmation de l'orthogonalité (étape 1).** Les profils de latence des scripts VM
> et du simulateur HTML sont indexés par **type** — `{edge: {B:5, A:150},
> cloud: {B:30, A:210}}` — et **non par provider**. Le modèle physique appartient donc
> bien au **tier**, jamais au provider : avoir exclu `latency_model` du
> `PROVIDER_REGISTRY` transversal était la bonne décision (le `latency_model` par
> provider de l'ancienne branche aurait été faux ici).

## 5. Ancrage dans le code actuel (`master`)

Pipeline du hub (`hub/orchestrator_core.py`, `_run_flow`) :

`_step1_slos → _step2_persist_slos → _step3_collect → _step4_persist_metrics →
_step5_check_violations → _step6_load_histories → _step7_predict → _step8_decide`

- **`_step8_decide`** construit la liste de candidats `current_data` (toutes les VMs)
  puis appelle `POST /decide`. **C'est ici que s'insère la logique multi-provider** :
  le pool devient provider-scopé, et la boucle Cas 1→5 encapsule l'appel.
- Migration effective : `_execute_kubectl_migration(client, from_vm, to_vm)`.
- **`topsis.py` n'a pas besoin d'être modifié** : il opère déjà sur une liste de
  candidats arbitraire.

---

## 6. Plan des étapes

| # | Étape | Objectif | Fichiers | Livrable / critère d'acceptation | Risque | Charge | Statut |
|---|---|---|---|---|---|---|---|
| 1 | Registre providers transversal | `PROVIDER_REGISTRY` + `PROVIDER_OF_VM` + validation. Déclaratif, zéro runtime | `shared/config.py`, `shared/models.py`, tests | Registre validé, orthogonalité provider ⊥ cluster testée | Faible | 0.5 j | ✅ **FAIT** |
| 2 | Objet `SLOIntent` formel | Intention convertie une seule fois, immuable, sérialisable, avec trace `attempted_providers` | `shared/models.py`, tests | Round-trip JSON fidèle, immuabilité, anti-boucle | Faible | 1 j | ✅ **FAIT** |
| 3 | **Module d'arbitrage (pur)** | Cadrage des candidats par provider + **score de violation** + partition **conformes / non conformes** + **offre de repli** (VM de violation minimale). **Ne calcule PAS TOPSIS** : il fournit le sous-ensemble conforme que le hub passera à TOPSIS. **Aucun réseau** → testable sans mock | **nouveau** `hub/provider_arbitration.py`, tests | Score exact ; sous-ensemble conforme correct ; offre de repli correcte ; ML-down neutre | **Moyen-fort** | 2 j | ⬜ |
| 4 | **Protocole de négociation (pur)** | Règles de décision du Cas 5 : comparer offre reçue vs offre locale → `MIGRE_CHEZ_MOI` / `GARDE_CHEZ_TOI`. **Doit traiter l'asymétrie de critères** (voir encadré ci-dessous) | `hub/provider_arbitration.py`, tests | Les 3 branches du tableau Cas 5 testées, symétrie P1↔P2, comparaison asymétrique détectée | **Moyen-fort** | 1.5 j | ⬜ |
| 5 | **Microservice de passation** (passerelle de fédération) — **port 8010** | Service dédié qui **transporte** `SLOIntent` + offre vers l'orchestrateur d'un autre provider. En mono-processus il reboucle sur le hub ; en distribué il pointera vers une adresse distante. **Réception** sur un **nouvel endpoint** `/intent/relay` du hub, qui évalue le provider ciblé et négocie en réutilisant les fonctions pures des étapes 3-4 | **nouveau** `services/provider_relay/`, `hub/orchestrator_core.py` (route additive), `shared/config.py` | Relais testable au `curl` de bout en bout, **sans que le cycle d'orchestration l'appelle** | Moyen | 1.5 j | ⬜ |
| 6 | Machine à états du hub | Enchaînement Cas 1→5 dans `_step8_decide` : provider courant → (TOPSIS si conformes) → sinon passation via le relais → sinon négociation. Distinguer déploiement initial (Cas 1-2) et violation runtime (Cas 3-4). **Protégé par `MULTI_PROVIDER_ENABLED`** | `hub/orchestrator_core.py` | Chaque cas produit la bonne décision ; STAY géré ; **flag OFF = comportement actuel bit pour bit** | **Fort** | 2 j | ⬜ |
| 7 | ~~Métriques groupées par provider~~ | **ABANDONNÉE (2026-07-22)** — voir encadré ci-dessous. Le filtrage par provider est déjà assuré par `candidates_for_provider()`, et scoper le collector casserait la simulation mono-orchestrateur | — | — | — | 0 j | ❌ |
| 8 | Alignement partition Kubernetes | **La partition transversale existe DÉJÀ** en production sous forme de labels PoP `space_1` / `space_2` (voir encadré). Il ne reste qu'à faire correspondre `provider-1 ↔ space_1` et `provider-2 ↔ space_2` dans `PROVIDER_REGISTRY`. **Aucun `kubectl label` à poser, `openstack_client` non modifié** | `shared/config.py` | Correspondance provider↔space documentée et testée | Faible | 0.25 j | ⬜ |
| 9 | Traçabilité + dashboard | Par cycle : provider tenté, offres échangées, score de violation, décision. Format `Cycle #N` conservé | `services/observability/app.py` | Chronologie de négociation visible | Faible | 1.5 j | ⬜ |
| 10 | Validation expérimentale | Saturer un provider → observer Cas 4 puis Cas 5 → mesurer le surcoût de décision | `shared/timing*.py`, scénario | Preuve des 5 cas + courbe de surcoût | Faible | 1.5 j | ⬜ |
| 11 | Doc version distribuée | `/intent/relay` = point d'extension vers 2 orchestrateurs réels | docs | Section rédigée | Faible | 0.5 j | ⬜ |

**Charge restante estimée : ~11 jours-homme.**

> ### ⚠️ Exigence pour l'étape 4 — asymétrie de critères entre offres
>
> Le score de violation est une **moyenne pondérée normalisée** (`Σ wᵢeᵢ / Σ wᵢ`), donc
> invariante au **nombre** de métriques évaluées — un provider moins instrumenté ne peut
> plus gagner par manque de données (biais corrigé à l'étape 3).
>
> **Mais il reste un biais inter-SLO :** deux offres à `0.10` peuvent porter sur des
> jeux de critères différents (P1 = moyenne sur {latency, cpu}, P2 = moyenne sur
> {latency} seule). Elles sont commensurables en échelle, mais ne mesurent pas la même
> chose : le CPU de P2 est simplement **inconnu**. Reproduit et confirmé le 2026-07-21.
>
> Or `ProviderOffer` ne transporte aujourd'hui que
> `{provider_id, vm_id, violation_score}` — **rien ne signale l'asymétrie** au provider
> distant, qui conclurait à une égalité trompeuse.
>
> **Décision pour l'étape 4 :** enrichir `ProviderOffer` avec la **liste des métriques
> effectivement évaluées**, et faire traiter explicitement le cas asymétrique par la
> règle de négociation (p. ex. à score égal, privilégier l'offre reposant sur le jeu
> de critères le plus complet). L'alternative — exiger un jeu de métriques identique
> des deux côtés — est écartée : trop fragile en conditions réelles, où la
> disponibilité du ML varie par VM.

> ### 🔎 Nature de l'isolation entre providers — logique, pas physique
>
> **Constaté le 2026-07-22.** Le `collector` itère sur `config.VM_REGISTRY` et collecte
> **les 4 VMs à chaque cycle**. Le hub lui transmet `active_metrics` (quelles métriques),
> jamais quelles VMs. `state.last_collected` contient donc toujours le parc complet.
>
> **L'isolation est appliquée à l'arbitrage**, par `candidates_for_provider()`, qui
> restreint strictement le pool aux VMs déclarées du provider et lève sur un provider
> inconnu. Quand l'orchestrateur endosse le rôle provider-1, il **voit** les 4 VMs en
> mémoire mais n'en **utilise** que 2. Verrouillé par test
> (`test_candidats_exclut_vm_autre_provider_meme_si_active`).
>
> **Ce n'est pas un défaut, c'est une contrainte de la simulation mono-orchestrateur :**
> un seul processus jouant les deux rôles doit nécessairement collecter les deux parcs.
> En déploiement distribué, l'orchestrateur de provider-1 ne pourrait pas joindre les
> VMs de provider-2 — la donnée n'existerait pas pour lui, et l'isolation deviendrait
> physique **sans changement de code** (seule `PROVIDER_ORCHESTRATOR_URL` évolue).
>
> ➡️ **L'étape 7 est donc abandonnée** : elle prévoyait de regrouper les métriques par
> provider, ce que `candidates_for_provider()` fait déjà. Rendre le `collector`
> provider-scopé **casserait** la simulation, puisque le hub a besoin des 4 VMs
> précisément parce qu'il joue les deux rôles.
>
> **Réponse à préparer pour la soutenance** — la question viendra : *« provider-1
> voit-il vraiment les VMs de provider-2 ? »*

### Chemin critique
```
#3 ──► #4 ──► #5 ──► #6 ──► #9 ──► #10 ──► #11
                      │
        #7 ‖ #8 (parallélisables dès maintenant)
```
Le risque est concentré sur **#3/#4** (justesse du score de violation et des règles
de négociation) et **#5** (machine à états couvrant les 5 cas).

---

## 6 bis. Paramétrage de la démo — atteignabilité des 5 cas

Analyse des **398 points** de la trajectoire réelle (`DATA.path` du simulateur), en
calculant pour chaque position la meilleure latence atteignable par chaque provider,
avec la physique déployée (edge B=5/A=150, cloud B=30/A=210, D∈[3,80]).

| Seuil latence | P1 seul conforme | P2 seul | Les deux | **Aucun → Cas 5** |
|---|---|---|---|---|
| 120 ms | 16 % | 19 % | 63 % | **0 %** |
| **100 ms** (`default_threshold` actuel) | 26 % | 28 % | 44 % | **0 %** |
| 80 ms | 44 % | 40 % | 14 % | **0 %** |
| **60 ms** ← recommandé | 40 % | 31 % | 0 % | **28 %** |
| 40 ms | 19 % | 19 % | 0 % | 61 % |

> ### ⚠️ Avec le seuil actuel de 100 ms, le Cas 5 ne se produit JAMAIS
>
> Il n'existe aucun point de la piste où les deux providers échouent simultanément.
> Toute la machinerie de négociation (étapes 3-4) resterait **morte** pendant la démo.
>
> **Recommandation : seuil latence à 60 ms pour la démo multi-provider.** Répartition
> obtenue : 71 % du parcours avec un seul provider conforme (Cas 2 et 4), 28 % sans
> aucun conforme (Cas 5), et les Cas 1/3 chaque fois que le service tourne déjà sur le
> provider conforme. **Les cinq cas deviennent tous observables sur un seul tour.**
>
> Modifiable soit via `METRICS_REGISTRY["latency"]["default_threshold"]` (mode
> autonomous), soit par une intention en langage naturel (mode enhanced).

**Symétrie confirmée :** les deux providers couvrent exactement la même plage de
latence sur la piste (5 – 146 ms). Combiné aux profils de ressources identiques
(edge1 ≡ edge2, cloud1 ≡ cloud2 depuis la correction du 2026-07-21), cela signifie que
**seule la géométrie différencie les providers**. Toute migration inter-provider
observée est donc causée par la position du véhicule, jamais par une asymétrie
artificielle de ressources — plan d'expérience propre et défendable.

---

## 7. Notes de recherche (à valoriser dans le mémoire)

- **Non-comparabilité de TOPSIS entre pools** (§3) : c'est un vrai résultat
  méthodologique. Beaucoup de travaux comparent naïvement des scores TOPSIS issus
  de pools différents ; ce plan l'évite explicitement par le score de violation.
- **Négociation avec contre-offre** (Cas 5) : le système ne se contente pas d'un
  repli en cascade, il réalise une **sélection globalement optimale** (minimum de
  violation) tout en préservant l'isolation entre providers — aucun provider ne
  voit les VMs de l'autre, seulement une **offre scalaire**. C'est une propriété
  intéressante : l'optimalité est atteinte sans divulgation d'information.
- **Biais d'ordre** : le provider courant est toujours interrogé en premier. Effet
  d'hystérésis souhaitable (évite le flapping), à mentionner explicitement.
- **Coût du protocole** : pire cas = 2 évaluations + 1 aller-retour HTTP de
  négociation. À mesurer (#10).

---

## 8. Statut contrat

Aucune étape n'est implémentée par l'auteur de ce document ; l'exécution est confiée
à un LLM tiers, puis **vérifiée indépendamment** avant consignation dans
[SUIVI_MULTI_PROVIDER_TRANSVERSAL.md](SUIVI_MULTI_PROVIDER_TRANSVERSAL.md).
Aucun commit, aucun push.
