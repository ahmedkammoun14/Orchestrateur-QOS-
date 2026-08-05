# Plan — Arbitrage fédéré N-providers (broadcast + Gap Grade + arbitre)

> **Contrat de travail** : discussion, plan et fichiers de suivi. **Aucun commit,
> aucun push, aucune branche** tant que non explicitement autorisé. Le code
> orchestrateur passe par une **prompt d'exécuteur** ; l'infrastructure est
> livrée dans la conversation. Chaque lot est **vérifié indépendamment** (lecture
> du code réel) avant de passer au suivant.

**Contexte** : fait suite à `PLAN_DISTRIBUE_MULTI_PROVIDER.md` (phase 1 ✅ validée :
2 orchestrateurs debout et isolés). Ce document couvre la **phase 2** — la
coordination et l'arbitrage inter-provider — refondue après la réunion
encadrant du 21/07/2026.

---

## 1. Objectif

Remplacer la passation **séquentielle 2-way** (`negotiate()` receveur-centrique)
par un **arbitrage fédéré N-way initiateur-centrique**, extensible à un nombre
arbitraire de providers **sans modification de la logique métier**.

Modèle d'interaction retenu : **Contract Net Protocol** (Smith, 1980)
— *announce* (broadcast des SLOs) → *bid* (Placement Plan + Gap Grade) →
*award* (arbitre) → *execute* (kubectl).

---

## 2. Le problème central résolu

### 2.1 Pourquoi les scores TOPSIS ne traversent JAMAIS

TOPSIS normalise en **min-max sur son propre pool** et dérive ses solutions
idéales de la matrice elle-même :

```python
norm[i][j] = (matrix[i][j] - col_min) / span        # topsis.py:306
a_plus  = max/min sur range(n_vm)                    # topsis.py:183-191
```

**Conséquence** : le meilleur du pool obtient toujours ≈ 1.0, le pire ≈ 0.0 —
**quelles que soient les valeurs absolues**. C'est le phénomène de **rank
reversal**, propriété structurelle de la méthode.

> **Preuve reproductible** : un pool P2 = {32 ms, 34 ms, 33 ms} donne
> `edge2b` (34 ms, **conforme**) → score **0.000**. Un provider P3 avec une VM
> unique à 34.9 ms obtient **1.000** (retour anticipé `topsis.py:90-95`).
> Comparer ces scores classerait la moins bonne VM en tête.

### 2.2 La monnaie d'échange : le Gap Grade

Normalisé par le **seuil SLO** (global, partagé, issu de l'intention), donc
**absolu et comparable entre providers**. Formellement : une **scalarisation par
point de référence** (Weighted Goal Programming, Charnes & Cooper 1961 ;
achievement scalarizing function, Wierzbicki).

### 2.3 Propriété d'extensibilité (démontrable)

> **Proposition.** Soit `P = {p1 … pN}` l'ensemble des participants.
> Alors `∀ p ∈ P`, `G_p` est indépendant de `P \ {p}`.
>
> **Preuve.** `G_p` ne dépend que de `(SLOs, seuils, poids)` — diffusés
> identiquement à tous — et des mesures de `p` seul. Aucun terme n'indexe un
> autre provider. ∎
>
> **Corollaire.** Ajouter `p(N+1)` ne modifie aucun `G_pi`, `i ≤ N`.
> L'arbitre passe de N à N+1 comparaisons, **sans changer une ligne de logique**.

**C'est la réponse formelle à la contrainte principale de l'encadrant.**

---

## 3. Décisions verrouillées (13)

| # | Décision | Détail |
|---|---|---|
| **Q1** | Gap Grade **signé** | retrait du `max(0, …)` : les conformes obtiennent une valeur **négative** (leur marge), sinon tous à 0.000 et aucun départage possible |
| **Q2** | **SLOs primaires uniquement** | c'est LA GATE appliquée à la comparaison. cpu/ram secondaires ne pèsent que sur TOPSIS |
| **Q3** | **L'arbitre ne normalise RIEN** | toute normalisation à la SOURCE, contre l'INTENTION — jamais contre les pairs. Sinon le dead-band devient participant-dépendant et R3 tombe |
| **Q4** | `coverage` transmis, **audit seulement** | en mode `hard`, tout bid conforme a par construction une couverture complète (règle « pas de prédiction ⇒ non conforme ») |
| **Q5** | Politique **lexicographique** | pas d'agrégation pondérée : elle autoriserait des compromis interdits par les règles verrouillées |
| **Q6** | **Étape 5 (ACTIF/STANDBY) en premier** | fondation : sans elle, split-brain (2 migrateurs kubectl concurrents) |
| **Q7** | **Borne `δ ≥ −1`**, côté marge uniquement | rétablit la symétrie : les critères de coût sont déjà bornés à −1 par la physique (`v ≥ 0`), pas les critères de bénéfice. Le côté violation reste **non borné** |
| **Q8** | **`is_compliant` obligatoire** dans le bid | en multi-SLO, `G < 0` **n'implique pas** la conformité (compensation). Jamais déduit du signe |
| **Q9** | **Tchebycheff augmenté** (ρ ≈ 0.1) | agrégation **non compensatoire** : un excédent cpu ne rachète jamais une violation de latence |
| **Q10** | **`SLO_ENFORCEMENT = "hard"`** | implémenté comme **refus de sélection** chez l'arbitre, **pas** comme suppression du calcul best-effort (qui alimente l'alerte) |
| **Q11** | Dashboard : **C → INFAISABLE**, **D → SANS DONNÉES** | clés internes `A/B/C/D` **inchangées** — seuls les libellés changent |
| **Q12** | Alerte d'infaisabilité : **dashboard seul** | pas de boucle retour LLM → préserve l'indépendance LLM ⊥ MI |
| **Q13** | **`placement_arbiter`** = nouveau microservice | 8011 / 8111, **un par orchestrateur**, sollicité uniquement quand SON orchestrateur est initiateur |

---

## 4. Formules

### 4.1 Écart normalisé, signé et borné

```
critère de coût   (<, <=) :   δ = (v − τ) / τ
critère bénéfice  (>, >=) :   δ = (τ − v) / τ

borne :  δ = max(−1, δ)          ← côté MARGE uniquement
                                    (le côté violation reste non borné)
```

**Lecture** : `δ < 0` = marge · `δ > 0` = violation · **plus bas = meilleur**.

**Justification de la borne** : `v ≥ 0` implique `δ ≥ −1` pour un critère de
coût — la borne est **automatique**. Pour un critère de bénéfice, la marge est
**non bornée** (12.8 cœurs contre un seuil de 2.5 → `δ = −4.12`). Borner à −1
donne aux critères de bénéfice la borne que les critères de coût possèdent déjà,
et rend aux poids du LLM leur sens.

### 4.2 Gap Grade — Tchebycheff augmenté

```
        max_i( w_i · δ_i )  +  ρ · Σ_i w_i · δ_i
G  =   ───────────────────────────────────────────        ρ ≈ 0.1
                      1 + ρ
```

sur les **SLOs primaires** uniquement.

| Terme | Rôle |
|---|---|
| `max(w·δ)` | **décide** — seul le pire critère compte ⇒ **non compensatoire** |
| `ρ · Σ(w·δ)` | **départage** — restaure la discrimination sans rendre le pouvoir compensatoire |
| `/(1+ρ)` | **normalise l'échelle** — voir non-régression ci-dessous |

### 4.3 Non-régression du mode autonome (démonstration)

Avec **un seul** SLO primaire (`w = 1`) :

```
G = [ 1·δ + ρ·1·δ ] / (1+ρ) = δ(1+ρ)/(1+ρ) = δ
```

> **Le Tchebycheff dégénère EXACTEMENT en `δ` en mode autonome.**
> Aucun changement de comportement sur le chemin latence-seule. Il n'entre en
> jeu qu'en enhanced, quand le LLM déclare ≥ 2 primaires.

### 4.4 Interprétation métier

**Tchebycheff = maximiser la marge minimale = maximiser la durée de vie du
placement.**

Sur un robot mobile, la marge **est** le compte à rebours avant la prochaine
violation :

| VM | Marge latence | Pente `dL/dd` | Distance avant violation |
|---|---|---|---|
| `cloud1` | 5 ms | 180/77 = 2.34 ms/u | **≈ 2.1 unités** |
| `edge2` | 33 ms | 145/77 = 1.88 ms/u | **≈ 17.5 unités** |

> Maximiser la marge minimale = **minimiser le nombre de migrations**.
> C'est la politique anti-ping-pong appliquée au **choix du placement**, en
> complément du dead-band.

---

## 5. Politique de l'arbitre (lexicographique)

```
⓪ FILTRE      evaluable == true   ET   is_compliant == true      (mode hard)
②  CLASSER    min(gap_grade)          ← jamais un score TOPSIS
③  DEAD-BAND  challenger < tenant − 0.05     (absolu, pas relatif)
④  ÉGALITÉ    ordre du registre            (déterminisme des tests)
```

- Le palier ① (conformité) devient un **filtre** en mode `hard`.
- Dead-band **= 0** s'il n'y a pas de tenant (déploiement initial).
- `0.05 × τ` : pour `τ = 40 ms`, le challenger doit gagner **> 2 ms**.

**Interdits absolus pour l'arbitre** : lire un `topsis_score`, normaliser les
bids entre eux, recalculer un Gap Grade.

---

## 6. Contrat du bid

```json
{
  "provider_id": "provider-2",
  "intent_id": "intent-2026-07-29-001",

  "placement_plan": {
    "vm_id":        "edge2",
    "action":       "migrate",
    "topsis_score": 1.0,
    "vm_scores":    { "edge2": 1.0, "edge2c": 0.4695, "edge2b": 0.0 },
    "reason":       "TOPSIS sur 3 VMs conformes de provider-2"
  },

  "gap_grade": {
    "value":        -0.0616,
    "is_compliant": true,
    "evaluable":    true,
    "coverage":     ["latency", "cpu_usage"],
    "detail":       { "latency": -0.0857, "cpu_usage": -0.2800 }
  },

  "timestamp": "2026-07-29T14:32:07Z"
}
```

| Champ | Lu par l'arbitre ? |
|---|---|
| `gap_grade.evaluable` | ✅ palier ⓪ |
| `gap_grade.is_compliant` | ✅ palier ⓪ |
| `gap_grade.value` | ✅ palier ② |
| `placement_plan.vm_id` | ✅ pour exécuter |
| `topsis_score`, `vm_scores`, `coverage`, `detail` | ❌ **audit / dashboard uniquement** |

---

## 7. Carte des services

**Légende** : ✅ existe · 🔧 à modifier · 🆕 à créer

| Port | Service | Rôle | État |
|---|---|---|---|
| 8000 | `hub` | orchestration · **calcule SON Gap Grade** (module pur) | 🔧 |
| 8001 | `latency_manager` | — | ✅ |
| 8002 | `intent_manager` | LLM → SLOs | ✅ |
| 8003 | `ml_predictor` | 7 horizons | ✅ |
| 8004 | `metrics_manager` | MI | ✅ |
| 8005 | `collector` | — | ✅ |
| 8006 | `database` | — | ✅ |
| 8007 | `history_loader` | — | ✅ |
| **8008** | `decision_intelligence` | **TOPSIS — INTRA-provider** | ✅ |
| 8009 | `observability` | dashboard · **alerte INFAISABLE** | 🔧 |
| **8010** | `provider_relay` | **transport pur** + `/broadcast` | 🔧 |
| **8011** | **`placement_arbiter`** | **arbitrage — INTER-provider** | 🆕 |
| 8024 | `openstack_client` | kubectl — **partagé, sans offset** | ✅ |

*provider-2 : `PORT_OFFSET=100` → 8100 … 8111.*

### Flux

```
hub → relais /broadcast → N bids bruts → hub → arbitre /arbitrate → verdict → hub → kubectl
```

### Les trois responsabilités, séparées

| Service | Fait | Ne fait **jamais** |
|---|---|---|
| **8010 relais** | transporte, diffuse, agrège | ne calcule rien, ne décide rien |
| **8011 arbitre** | filtre, classe, dead-band | ne calcule aucun score, ne normalise rien |
| **8000 hub** | évalue **ses** VMs, calcule **son** Gap Grade, exécute | ne juge jamais les VMs d'un autre provider |

> **Les deux échelles de décision sont visibles dans le déploiement** :
> `8008` = TOPSIS intra · `8011` = arbitrage inter. C'est ce qui justifie le
> microservice séparé — bien plus que « l'encadrant l'a demandé ».

---

## 8. Les 6 lots

| Lot | Contenu | Risque | Vérification indépendante |
|:---:|---|:---:|---|
| **1** | **ÉTAPE 5 — ACTIF/STANDBY**. `_sync_active_vm()` en **sync paresseux** (violation détectée **OU** tous les N cycles ; aujourd'hui au boot seul, `orchestrator_core.py:1418`) · gate en tête de `_step8_decide` · `role` + `hosting_vm` dans `/status` · conditionné par `PROVIDER_ID != "all"` | 🟢 faible | `/status` des 2 hubs : **un seul** `role: "active"` · le standby n'appelle `decision_intelligence` **que pour servir un bid**, jamais depuis `_step8_decide` · **jamais** `openstack_client/migrate` · 186 tests verts |
| **2** | **Gap Grade v2** — **nouvelle fonction pure** : `δ` signé, borné à −1, Tchebycheff augmenté. ⚠️ **Ajoutée à côté de l'ancienne, NON branchée** | 🟢 **nul** | tests unitaires seuls · **zéro changement de comportement runtime** · cas 65 ms : `edge2` (−0.140) bat `cloud1` (−0.083) |
| **3** | **Bid unifié** — structures `PlacementPlan` + `GapGrade` · endpoint hub `/evaluate` produisant un bid complet | 🟡 moyen | `curl /evaluate` → JSON conforme au §6, avec `is_compliant`, `evaluable`, `coverage` |
| **4** | **`/broadcast`** scatter-gather N-aire sur `provider_relay` | 🟡 moyen | 1 appel → N bids agrégés · pair éteint = **absent** des bids, pas d'erreur · relais toujours **sans état** |
| **5** | **`placement_arbiter`** (8011 / 8111) + filtre `SLO_ENFORCEMENT = "hard"` | 🟠 élevé | jeu de bids → verdict attendu · **rejeu des 22 cas du §9** |
| **6** | **Câblage final** — hub utilise `/broadcast` puis `/arbitrate` · retrait de l'ancien chemin 2-way (`negotiate`, `/handoff`, `/intent/relay`) · reclassement A/B/C/D a posteriori · alerte dashboard | 🔴 élevé | démo PiCar end-to-end · **un seul hôte à tout instant** |

### Pourquoi le lot 2 n'est pas branché

Remplacer directement `_excess` modifierait le comportement du `negotiate()`
2-way **encore en production**, avant que la nouvelle politique existe. En
ajoutant la fonction **à côté**, le risque runtime est **nul** et elle est
**100 % validable par tests unitaires** pendant que le lot 1 est validé sur
l'infrastructure réelle. Le lot 5 la branche, le lot 6 retire l'ancienne.

---

## 9. Cas de test de référence

### Mode autonome (1 SLO primaire : latence < 40 ms, w = 1.0)

| # | Situation | Gate | Décision | Chemin | kubectl |
|---|---|:---:|---|:---:|:---:|
| A0 | Bootstrap (cycle < 5) | — | pas de décision | — | ❌ |
| A1 | Aucune violation primaire | 🔒 | **STAY** | — | ❌ |
| A2 | Je suis **STANDBY** | — | observe, répond aux bids | — | ❌ |
| A3 | Cooldown actif | 🔒 | **STAY** | — | ❌ |
| A4 | Violation · conformes chez moi · je gagne | 🔓 | migration interne | **A** | ✅ |
| A5 | Violation · pair meilleur de **> 2 ms** | 🔓 | migration inter-provider | **B** | ✅ |
| A6 | Violation · pair meilleur de **< 2 ms** | 🔓 | dead-band bloque → interne | **A** | ✅ |
| A7 | Violation · 0 conforme chez moi · pair conforme | 🔓 | migration inter-provider | **B** | ✅ |
| A8 | Violation · **personne conforme** | 🔓 | **STAY + ALERTE** | **C** | ❌ |
| A9 | Violation · **ML muet partout** | 🔓 | **STAY + ALERTE** | **D** | ❌ |
| A10 | Violation · pair injoignable | 🔓 | décide sur les bids reçus | A/B/C | selon |

### Mode enhanced (N SLOs primaires, seuils et poids du LLM)

| # | Situation | Gate | Décision | Chemin | kubectl |
|---|---|:---:|---|:---:|:---:|
| E1 | Intention reçue · VM active toujours conforme | 🔒 | **STAY** | — | ❌ |
| E2 | Je suis **STANDBY** | — | observe, répond aux bids | — | ❌ |
| E3 | Cooldown actif | 🔒 | **STAY** | — | ❌ |
| E4 | Seuil LLM plus strict → VM active non conforme | 🔓 | tour ouvert | A/B/C | selon |
| E5 | Conformes chez moi · je gagne | 🔓 | migration interne | **A** | ✅ |
| E6 | Un pair conforme gagne | 🔓 | migration inter-provider | **B** | ✅ |
| **E7** | ⭐ **Deux providers conformes → Tchebycheff tranche** | 🔓 | le plus **robuste** gagne | A ou B | ✅ |
| E8 | Pair meilleur mais sous le dead-band | 🔓 | dead-band bloque → interne | **A** | ✅ |
| E9 | **Personne conforme** aux SLOs du LLM | 🔓 | **STAY + ALERTE** | **C** | ❌ |
| E10 | SLO LLM sur métrique non instrumentée | 🔓 | ignoré ; si **tous** ignorés → alerte | **D** | ❌ |
| E11 | **Déploiement initial** (aucune VM active) | 🔓 | **dead-band = 0**, comparaison stricte | DEPLOY | ✅ |
| E12 | REPLACE vs ADDITIVE | — | change l'ensemble de SLOs, pas la logique | — | — |

### Cas E7 de référence (à figer en test unitaire)

`latence < 65 ms` (w 0.6) · `cpu dispo ≥ 2.5 cœurs` (w 0.4)

| Champion | Latence | CPU | δ_lat | δ_cpu (borné) | **G (Tchebycheff)** |
|---|---|---|---|---|---|
| `cloud1` (P1) | 60 ms | 12.8 | −0.077 | **−1.000** | **−0.083** |
| `edge2` (P2) | 32 ms | 3.2 | −0.508 | −0.280 | **−0.140** 🏆 |

Comparaison des trois méthodes sur ce jeu :

| Méthode | Gagnant | Verdict |
|---|---|---|
| Somme pondérée sans borne | `cloud1` (−1.694) | ❌ le cpu rachète la latence |
| Somme pondérée + borne | `cloud1` (−0.446) | ⚠️ écart 0.029 < dead-band → indécidable |
| **Tchebycheff + borne** | **`edge2`** | ✅ marge minimale 28 % contre 7.7 % |

---

## 10. Propriétés invariantes (à ne jamais violer)

| # | Invariant | Pourquoi |
|---|---|---|
| **1** | **Initiateur = l'orchestrateur ACTIF** (autonome) ou **celui qui reçoit l'intention** (enhanced) | évite le split-brain — lot 1 |
| **2** | **TOPSIS reste DANS le provider · seul le Gap Grade traverse** | scores TOPSIS incomparables (min-max du pool) |
| **3** | **Tout est calculé à la SOURCE · l'arbitre ne fait que classer** | le Gap Grade détruit l'information privée `v` ; le calculer ailleurs = divulgation + recentralisation |
| **4** | **Le pair évalue contre les SLOs REÇUS, jamais les siens** | sans SLOs identiques, les Gap Grades ne sont pas comparables. ✅ **déjà respecté** (`orchestrator_core.py:1540`) |
| **5** | **LA GATE** : seule une violation **primaire** ouvre un tour | cpu/ram ne déclenchent jamais de migration |
| **6** | **LLM (`intent_manager`) ⊥ MI (`metrics_manager`)** | aucune relation, jamais |

---

## 11. Non-régression

- `PROVIDER_ID = "all"` + `MULTI_PROVIDER_ENABLED = false` ⇒ comportement
  mono-processus **inchangé**.
- Mode autonome : le Tchebycheff **dégénère en `δ`** (§4.3) ⇒ aucun changement.
- Baseline : **186 tests** doivent rester verts à chaque lot.
- `picarx_sim_QoS.html` : **jamais modifié**.

---

## 12. Points ouverts (hors périmètre, pour le mémoire)

1. **Coût comme second objectif** — nativement commensurable entre providers
   (la monnaie est une unité partagée). L'arbitre deviendrait bi-objectif
   `(G, coût)`. Non instrumenté aujourd'hui (`topsis.py:71-72`).
2. **Boucle de retour utilisateur** sur infaisabilité — « voulez-vous relâcher
   la contrainte ? » via le LLM. Écarté (Q12), gardé en perspective.
3. **`SLO_ENFORCEMENT = "soft"`** — le best-effort est calculé et transmis ;
   seule la sélection le refuse. Réactivable en une ligne.
4. **Registre dynamique des orchestrateurs** (`/register`) — aujourd'hui
   statique et configurable par variable d'env, ce qui suffit à la contrainte
   « ajouter un provider sans modifier la logique métier ».
5. **Docstring périmée de `TopsisSelector`** — annonce « budget de conformité,
   fiabilité » comme critères, alors que `reliability_scores` est reçu et
   **jamais utilisé**, et `_BUDGET_WEIGHT` / `_RELIABILITY_WEIGHT` sont des
   constantes mortes. Correctif cosmétique à traiter **séparément**.

---

## 13. Références

- Hwang & Yoon (1981) — TOPSIS.
- Rank reversal en TOPSIS — phénomène largement documenté en MCDM
  (chercher « rank reversal TOPSIS », García-Cascales & Lamata). *À vérifier
  avant citation.*
- Charnes & Cooper (1961) — Goal Programming, déviations pondérées normalisées.
- Wierzbicki (années 1980) — méthode du point de référence, *achievement
  scalarizing functions*.
- Steuer & Choo (1983) — *augmented weighted Tchebycheff*.
- Smith (1980) — *The Contract Net Protocol*, IEEE Trans. Computers.
