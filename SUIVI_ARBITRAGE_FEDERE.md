# Suivi — Arbitrage fédéré N-providers

> Journal d'avancement rempli au fil de la réalisation. Un lot n'est coché
> qu'après **vérification indépendante** (lecture du code réel, jamais sur la
> foi du rapport de l'exécuteur), avec la preuve associée.
>
> Plan de référence : `PLAN_ARBITRAGE_FEDERE.md`
> Phase précédente : `SUIVI_DISTRIBUE_MULTI_PROVIDER.md` (phase 1 ✅)

**Légende** : ⬜ à faire · 🟡 en cours · ✅ vérifié · ⚠️ vérifié avec réserve · ❌ échec

---

## État de départ (constaté dans le code, 30/07/2026)

| Constat | Emplacement |
|---|---|
| `_get_active_vm()` interroge **les deux clusters** via `NODE_VM_MAP` / `VM_CLUSTER_MAP` → vérité **globale** | `infrastructure/openstack_client.py:84` |
| `_sync_active_vm()` existe et met à jour `state.service_vm` depuis kubectl… | `hub/orchestrator_core.py:253` |
| …**mais n'est appelé qu'au démarrage** (`lifespan`) — jamais par cycle ⇒ après un handoff exécuté par l'autre provider, `service_vm` reste **périmé** | `hub/orchestrator_core.py:1418` |
| `_decide_multi_provider` est **déjà distribution-ready** : la passation part en **HTTP via le relais** | `hub/orchestrator_core.py:892` |
| `/intent/relay` est **déjà purement observatoire** — ne modifie aucun champ de `state`, ne migre jamais | `hub/orchestrator_core.py:1489` |
| ⚠️ **Verrou 2-providers** : `next((p for p in PROVIDER_REGISTRY if p != current_provider), None)` — « l'autre », au singulier | `hub/orchestrator_core.py:993` |
| `PROVIDER_RELAY_URLS` est **déjà** un dict `{provider_id → url}` ⇒ **déjà N-ready** pour le broadcast | `shared/config.py:227-228` |
| Le pair évalue déjà contre les **SLOs reçus** (`intent.slos`), pas les siens ⇒ **invariant 4 déjà respecté** | `hub/orchestrator_core.py:1540` |
| ⚠️ Un provider sans VM évaluable renvoie `is_compliant = True` (neutralité ML-down) ⇒ **en broadcast, un provider aveugle gagnerait**. L'arbitre DOIT filtrer sur `evaluable` d'abord | `hub/provider_arbitration.py:416-425` |
| ⚠️ Docstring périmée de `TopsisSelector` : `reliability_scores` reçu mais **jamais utilisé** ; `_BUDGET_WEIGHT` / `_RELIABILITY_WEIGHT` sont des **constantes mortes** | `services/decision_intelligence/topsis.py:70-72, 25-26` |

**Baseline tests** : 186 verts.

---

## Tableau d'avancement

| Lot | Contenu | Statut | Preuve / note |
|:---:|---|:---:|---|
| **1** | ÉTAPE 5 — ACTIF/STANDBY (sync paresseux + gate + `/status`) | ⚠️ | **code vérifié conforme** (7/7 points, relecture indépendante 30/07/2026) · **201 tests** (186 + 15) · ⚠️ réserve mineure : modulo par zéro si `ACTIVE_VM_SYNC_EVERY_N_CYCLES=0` · **validation terrain sur les 2 stacks encore à faire** |
| **2** | Gap Grade v2 (signé · borné · Tchebycheff) — **non branché** | ✅ | **8/8 points vérifiés** (relecture indépendante 30/07/2026) · `signed_excess` + `compute_gap_grade` dans `provider_arbitration.py:310-403` · normalisation des poids présente `:400` · **non-branchement confirmé par grep** (une seule occurrence, dans son propre fichier) · **224 tests** (201 + 23) |
| **3** | Bid unifié `PlacementPlan` + `GapGrade` + hub `/evaluate` | ✅ | **3/3 points critiques vérifiés** (relecture indépendante 30/07/2026) · `slo_values_for_vm` utilise bien `_representative_value` (`:583`) → conversion d'unités correcte · CAS B **recalcule** par Gap Grade, n'utilise **jamais** `assessment.best_effort_vm` (`orchestrator_core.py:1846-1855`) · `test_gap_grade.py` **non modifié** (23 tests) → refactorisation `_retained_primary_slos` neutre · **241 tests** (224 + 17) · **additivité confirmée** : `/evaluate` n'est appelé nulle part en production |
| **4** | `/broadcast` scatter-gather N-aire sur `provider_relay` | ✅ | **3/3 points critiques vérifiés** (relecture indépendante 30/07/2026) · **parallélisme réel** : `asyncio.gather(return_exceptions=True)` dans **un seul** `AsyncClient` (`app.py:295-297`) · **dégradation gracieuse** : pair injoignable → `errors` + HTTP **200** (`:303-305`, `:333`) · **transport pur** : aucun import de `provider_arbitration`, `slos` jamais désérialisé · ordre déterministe via `zip(target_items, results)` (`:302`) · `/inbound/evaluate` → `CORE_URL/evaluate` uniquement (`:355`), point **terminal** (aucune rediffusion ⇒ boucle impossible par construction) · **256 tests** (241 + 15) · **additivité confirmée** |
| **5** | `placement_arbiter` (8011/8111) + filtre `hard` | ✅ | **3/3 points critiques vérifiés** (relecture indépendante 30/07/2026) · `evaluable` testé **AVANT** `is_compliant` (`arbiter.py:108` puis `:114`) → le provider aveugle est écarté en premier · `topsis_score`/`vm_scores` **jamais lus** (une seule occurrence, en docstring `:20`) · dead-band **non appliqué** si le tenant n'a pas de bid retenu (`:208-209`) · `DEPLOY` sans tenant (`:207`) · distinction C/D via `any_evaluable` (`:174`) · **277 tests** (256 + 21) · **additivité confirmée** |
| **6a** | Câblage du cycle (`/broadcast` + `/arbitrate`) | ✅ | **3/3 points critiques vérifiés** (relecture indépendante 31/07/2026) · `MULTI_PROVIDER_ENABLED=false` → `_decide_mono_provider` **inchangé** (`:647-649`) · arbitre indisponible **ou réponse invalide** → **STAY**, `return` avant tout kubectl (`:1417-1429`) · **aucun broadcast sans violation** (`:1330-1336`, avant `:1373`) · cooldown en premier sans réseau (`:1313`) · relais injoignable → poursuite avec le seul bid local (`:1384`) · bid local **en tête** (`:1400`) · `topsis_score: None` (`:1443`) · **291 tests** (277 + 14) |
| **1b** | Granularité kubectl (`VM_NODE_GROUP`) + LA GATE sur primaires + clamp modulo | ✅ | **4/4 points vérifiés** (relecture indépendante 31/07/2026) · règle à **3 conditions** dont `mon_node != node_hote` (`:313`) · branches d'erreur **inertes** (`:325-338`) · logs de `_step5_check_violations` **inchangés** (`:558-570`) · **aucun test existant modifié** · **308 tests** (291 + 17) |
| **6b** | Dashboard (libellés C/D + alerte) + retrait du code mort + défauts cosmétiques | ⬜ | — |

---

## Journal détaillé

### Lot 1 — ÉTAPE 5 : ACTIF / STANDBY

- **Statut** : 🟡 prompt rédigée le 30/07/2026, en attente d'exécution
- **Périmètre** :
  1. `shared/config.py` : constante `ACTIVE_VM_SYNC_EVERY_N_CYCLES` (défaut 10).
  2. `state.hosting_vm` (vérité globale kubectl) + `state.is_active` (init `True`).
  3. `_sync_active_vm` corrigée : capte la vérité **globale** via `ALL_VM_REGISTRY`
     (aujourd'hui elle ignore une VM active appartenant à l'autre provider et
     logue à tort « aucun pod actif »). Sur erreur / timeout : **conserver** la
     dernière valeur connue (une panne transitoire ne fait jamais basculer un rôle).
  4. `_step5_check_violations` renvoie un **booléen** (corps et logs inchangés).
  5. **Sync paresseux** dans `_run_flow`, entre `check_violations` et
     `decision_total` : appelé si **violation détectée** OU **tous les N cycles**.
     Chronométré via `prof.step("sync_active_vm")`.
  6. Gate en tête de `_step8_decide` : `not state.is_active` → `stay` + `return`.
  7. `/status` : `role: "active" | "standby"` + `hosting_vm`.
  8. Tout conditionné par `config.PROVIDER_ID != "all"`.

- **Pourquoi paresseux + battement de cœur** :
  `openstack_client` exécute `kubectl` en sous-processus (≈ 200 ms – 1 s). Sans
  violation, la décision est **STAY** de toute façon : payer ce coût ne change
  rien. Mais un sync **purement** paresseux se bloque après la première
  migration inter-provider — la découverte du rôle dépendrait de la détection
  d'une violation, qui dépend elle-même de connaître son rôle (circulaire).
  Le battement tous les N cycles casse cette circularité. Coût amorti ≈ 10 %.

- **⚠️ Ce qui doit CONTINUER en STANDBY (les 2 orchestrateurs travaillent en parallèle)** :
  collecte des 4 VMs · historiques · prédictions ML · MI · dashboard ·
  **réponse à `/intent/relay`, y compris l'appel à `decision_intelligence` pour
  son propre TOPSIS**. Le seul interdit absolu du standby est **kubectl**.

- **Vérification prévue** :
  - `GET /status` sur les 2 hubs → **un seul** `role: "active"` ; `hosting_vm` identique des deux côtés.
  - Logs du standby : **zéro** `decide_call` **depuis son cycle**, **zéro** migration kubectl.
  - Après une migration inter-provider : les rôles **s'inversent** en ≤ N cycles.
  - `PROVIDER_ID="all"` → `role` toujours `active`, comportement bit-identique.
  - `prof.step("sync_active_vm")` visible dans l'Excel de timings → mesurer le coût réel de kubectl et réajuster `N` si besoin.
  - 186 tests verts + 7 nouveaux.

- **Résultat — code ✅ / terrain ⬜ (relecture indépendante du 30/07/2026)** :

  | # | Vérification | Constat |
  |:--:|---|:--:|
  | 1 | `return` du standby **avant** `_build_candidates` | ✅ `orchestrator_core.py:632` puis `:634` |
  | 2 | `_sync_active_vm` utilise **`ALL_VM_REGISTRY`** (vérité globale) | ✅ `:275` — bug d'origine corrigé |
  | 3 | Erreur/timeout kubectl → `is_active` et `service_vm` **inchangés** | ✅ `:291-304`, uniquement des `logger.warning` |
  | 4 | `_step5_check_violations` : corps et logs intacts, un seul `return` | ✅ `:525` |
  | 5 | Étapes 1→7 et `/intent/relay` **non touchés** | ✅ `gather` `:1396-1401` identique |
  | 6 | `prof.step("sync_active_vm")` présent | ✅ `:1418` |
  | 7 | Tests | **201** (186 + 15) — chiffre cohérent, non rejoué |

  **⚠️ Réserve mineure** : `state.cycle_count % config.ACTIVE_VM_SYNC_EVERY_N_CYCLES`
  (`:1415`) lève une `ZeroDivisionError` si la variable d'env vaut `0`.
  À durcir (`max(1, …)`) — non bloquant, à traiter dans un lot ultérieur.

  **✅ Validation terrain — synchronisation au DÉMARRAGE (30/07/2026)** :

  ```
  :8000  →  role "standby" · hosting_vm "edge2" · service_vm "edge1"
  :8100  →  role "active"  · hosting_vm "edge2" · service_vm "edge2"
  ```

  | Attendu | Constat |
  |---|:--:|
  | **un seul** `role: "active"` | ✅ |
  | `hosting_vm` **identique** des deux côtés (`edge2`) | ✅ |
  | Le standby **ne modifie pas** son `service_vm` (reste `edge1`) | ✅ |
  | L'actif **met à jour** son `service_vm` (`edge2`) | ✅ |

  Preuve que la vérité globale (`ALL_VM_REGISTRY`) et la branche
  `if state.is_active:` (`orchestrator_core.py:282`) fonctionnent : P1 **voit**
  que l'hôte est `edge2` sans pour autant écraser son propre `service_vm`.

  **⬜ Reste à prouver — comportement en RÉGIME (bridge PiCar actif)** :
  relevés faits à `cycle: 0`, la boucle n'avait pas encore tourné.
  1. **Coût réel de kubectl** → colonne `sync_active_vm` de l'Excel (arbitrera `N`).
  2. Gate STANDBY effectif → log `🟡 STANDBY — service hébergé par edge2` chez P1.
  3. **Inversion des rôles** après une migration inter-provider, en ≤ N cycles.

> **Effet de bord connu et accepté** : en standby, `state.service_vm` pointe une
> VM hors du `VM_REGISTRY` local ⇒ `_step1_slos` charge un historique vide et
> `_step5_check_violations` logue « SLOs respectés » de façon trompeuse.
> **Cosmétique, sans risque fonctionnel.** À traiter séparément si gênant.

---

### Lot 2 — Gap Grade v2 (non branché)

- **Statut** : ⬜
- **Périmètre** : nouvelle fonction **pure**, ajoutée **à côté** de l'existante,
  **sans modifier `_excess` ni `evaluate_vm`** :
  - `δ` signé (retrait du `max(0, …)`)
  - borne `δ = max(−1, δ)` — **côté marge uniquement**
  - `G = [max(w·δ) + ρ·Σ(w·δ)] / (1+ρ)`, `ρ = 0.1`
  - **SLOs primaires uniquement**
- **Attendu** : **zéro changement de comportement runtime** — la fonction n'est
  appelée par personne. Le `negotiate()` 2-way encore en production reste
  strictement intact.
- **Vérification prévue** (tests unitaires) :

  | Test | Attendu |
  |---|---|
  | 1 seul SLO primaire (`w=1`) | `G == δ` **exactement** (non-régression autonome) |
  | Cas E7 (65 ms) | `edge2` = **−0.140** bat `cloud1` = **−0.083** |
  | Borne | `cloud1` δ_cpu = −4.12 → **−1.000** |
  | Non-compensation | VM violant la latence avec énorme surplus cpu ⇒ `G > 0` |
  | Comparaison des méthodes | somme pondérée élit `cloud1` ; Tchebycheff élit `edge2` |
  | `is_compliant` ≠ `signe(G)` | une VM non conforme peut avoir `G < 0` en multi-SLO |
  | Aucune VM évaluable | pas de crash, `evaluable = false` |

- **Résultat** : _(à remplir)_

---

### Lot 3 — Bid unifié

- **Statut** : ⬜
- **Périmètre** : structures `PlacementPlan` et `GapGrade` (sérialisables JSON)
  + endpoint hub `/evaluate` produisant un bid complet à partir des SLOs reçus.

- **⚠️ PIÈGE D'INTÉGRATION À NE PAS RATER** :
  `compute_gap_grade` attend des valeurs **DÉJÀ CONVERTIES**. Un SLO
  `cpu_usage >= 2.5 cores` porte sur une **disponibilité absolue**, pas sur un
  pourcentage. Le lot 3 DOIT réutiliser `_representative_value`
  (`provider_arbitration.py:221`), qui applique déjà `_to_criterion_value`
  quand `slo["unit"]` vaut `cores`/`GB`. Passer `cpu_usage = 20` (le %) au lieu
  de `3.2` (les cœurs) produirait un Gap Grade **faux et silencieux**.

- **Rappel** : le Gap Grade se calcule sur le **champion élu par TOPSIS**,
  pas sur toutes les VMs.
- **Attendu** : contrat du §6 du plan respecté à la lettre, y compris
  `is_compliant`, `evaluable`, `coverage`.
- **Vérification prévue** :
  - `curl -X POST /evaluate` avec un jeu de SLOs → JSON conforme.
  - Provider sans conforme → `is_compliant: false` mais **bid quand même émis**
    (best-effort calculé, pour l'alerte — décision Q10).
  - Provider aveugle → `evaluable: false`.
- **Résultat** : _(à remplir)_

---

### Lot 4 — `/broadcast` sur `provider_relay`

- **Statut** : ⬜
- **Périmètre** : endpoint `/broadcast` — scatter-gather vers **tous** les pairs
  de `PROVIDER_RELAY_URLS`, en parallèle, avec agrégation des réponses.
- **Contrainte absolue** : le relais reste **sans état** et **ne calcule rien**
  (cf. sa propre docstring `services/provider_relay/app.py:4-6`).
- **Attendu** :
  - 1 appel entrant → N appels sortants → N bids agrégés.
  - Pair injoignable ⇒ **simplement absent** de la liste, **pas** d'erreur 502
    globale (repli gracieux).
  - Timeout HTTP borné.
- **Vérification prévue** :
  - Avec 2 orchestrateurs debout → 2 bids.
  - Avec P2 éteint → 1 bid, aucune exception, le cycle continue.
  - `/health` du relais toujours cohérent (`peer_relays`, `local_hub`).
- **Résultat** : _(à remplir)_

---

### Lot 5 — `placement_arbiter` (8011 / 8111)

- **Statut** : ⬜
- **Périmètre** : nouveau microservice, **un par orchestrateur**, endpoint
  `/arbitrate`. Politique lexicographique du §5 du plan +
  `SLO_ENFORCEMENT = "hard"` implémenté en **refus de sélection**.
- **Interdits** : lire `topsis_score`, normaliser les bids entre eux,
  recalculer un Gap Grade.
- **Attendu** : `PORT_OFFSET` appliqué (8011 / 8111) ; ajouté à
  `launch_provider.py`.
- **Vérification prévue** :
  - Rejeu des **22 cas** du §9 du plan (A0→A10, E1→E12) sous forme de jeux de
    bids en entrée → verdict attendu.
  - **Test d'extensibilité** : ajouter un 3ᵉ bid ⇒ les Gap Grades des 2 premiers
    sont **inchangés au chiffre près** (preuve numérique de la proposition R3).
  - Dead-band : challenger à −0.100 contre tenant à −0.075 ⇒ **refusé**.
  - Mode `hard` : aucun bid `is_compliant: false` ne peut être élu.
- **Résultat** : _(à remplir)_

---

### ⚠️ Dette à traiter au lot 6b (héritée du 6a)

Ma prompt du 6a était **contradictoire** : elle demandait à la fois de rerouter
le dispatch de `_step8_decide` vers `_decide_federated` ET de garder verte une
suite dont 23 tests (`test_multi_provider_flow.py`, `test_multi_provider_reasoning.py`)
testaient précisément **cet ancien dispatch**. L'exécuteur a signalé la
contradiction et ajouté dans ces deux fichiers un helper appelant
`_decide_multi_provider` **directement** — correctif minimal, aucune assertion
touchée, couverture d'origine restaurée.

**⚠️ Conséquence pour le 6b** : ces 23 tests couvrent désormais du **code mort**.
Quand le 6b supprimera `_decide_multi_provider`, **ces tests devront être
supprimés en même temps** — sinon ils testeront du vide.

### Lot 6 — Câblage final

- **Statut** : ⬜
- **Périmètre** :
  1. `_step8_decide` appelle `/broadcast` puis `/arbitrate`.
  2. Retrait de l'ancien chemin 2-way : `negotiate()`, `/handoff`,
     `/intent/relay`, et le verrou `next((p for p in PROVIDER_REGISTRY …))`
     (`orchestrator_core.py:993`).
  3. Reclassement **A/B/C/D a posteriori** à partir du verdict de l'arbitre
     (clés internes inchangées).
  4. Libellés dashboard : **C → INFAISABLE**, **D → SANS DONNÉES**.
  5. Alerte d'infaisabilité sur le dashboard (contenu : exigence, meilleure
     offre non actionnable, écart, providers évalués).
- **Attendu** : démo PiCar end-to-end, **un seul hôte à tout instant**.
- **Vérification prévue** :
  - Les 4 compteurs du dashboard s'incrémentent conformément aux cas observés.
  - Aucun `topsis_score` ne franchit la frontière décisionnelle.
  - Un tour ouvert se termine **toujours** par une migration (A/B) ou une
    alerte (C/D) — **jamais** par « on reste sur la VM active ».
  - 186 tests verts (adaptés).
- **Résultat** : _(à remplir)_

---

## Décisions verrouillées (rappel)

| # | Décision |
|---|---|
| Q1 | Gap Grade **signé** |
| Q2 | **Primaires uniquement** |
| Q3 | L'arbitre **ne normalise rien** — calcul **à la source** |
| Q4 | `coverage` transmis, **audit seulement** |
| Q5 | Politique **lexicographique** |
| Q6 | **Lot 1 (ACTIF/STANDBY) en premier** |
| Q7 | Borne **δ ≥ −1**, côté marge uniquement |
| Q8 | **`is_compliant`** obligatoire (jamais déduit du signe de `G`) |
| Q9 | **Tchebycheff augmenté** ρ ≈ 0.1, calculé **par chaque provider** |
| Q10 | **`SLO_ENFORCEMENT = "hard"`** — refus de sélection, pas suppression du calcul |
| Q11 | **C → INFAISABLE** · **D → SANS DONNÉES** (clés internes inchangées) |
| Q12 | Alerte : **dashboard seul**, pas de retour LLM |
| Q13 | **`placement_arbiter`** = nouveau microservice, 8011/8111, un par orchestrateur |

---

## Validation terrain de la chaîne 4→5 (31/07/2026)

Deux stacks lancées (`launch_provider.py`), test manuel par `Invoke-RestMethod`.

**Santé des nouveaux services** :
```
:8011 et :8111 → {"status":"healthy","service":"placement_arbiter",
                  "enforcement":"hard","deadband":0.05}
```

**`/broadcast` depuis P1** (`from_provider: provider-1`) :
```json
{ "bids": [],
  "errors": [ { "provider_id": "provider-2", "error": "HTTP 503",
                "detail": "…last_collected vide…" } ],
  "relayed_by": "provider_relay" }
```

| Ce qui est PROUVÉ | ✓ |
|---|:--:|
| Routage complet relais P1 → relais P2 (:8110) → hub P2 (:8100/evaluate) | ✅ |
| **Dégradation gracieuse** : pair en erreur → **HTTP 200** quand même | ✅ |
| Attribution correcte de l'erreur au bon provider (`zip(target_items, results)`) | ✅ |
| Arbitre : `bids: []` → path **D**, `decision: "stay"`, alerte `SANS_DONNEES`, `deadband_applied: 0.0` | ✅ |
| La chaîne broadcast → pair → `/evaluate` → arbitre → verdict est **bouclée** | ✅ |

### Second test, hub P2 réchauffé (1 cycle manuel) — chaîne 2→5 complète

**`/broadcast` → bid réel de provider-2** :
```json
{ "provider_id": "provider-2",
  "placement_plan": { "vm_id": "edge2", "action": "stay",
                      "topsis_score": null, "vm_scores": {},
                      "reason": "aucune VM conforme — meilleure offre par Gap Grade : edge2 (0.3121)" },
  "gap_grade": { "value": 0.3121477301631655, "is_compliant": false,
                 "evaluable": true, "coverage": ["latency"],
                 "detail": { "latency": 0.31214773016316555 } } }
```

> ⭐ **PREUVE EMPIRIQUE DE LA NON-RÉGRESSION** : `gap_grade.value` et
> `detail.latency` sont **identiques**. Avec un seul SLO primaire, `G = δ`
> exactement — le Tchebycheff est **totalement transparent en mode autonome**,
> vérifié cette fois sur données réelles et prédictions ML réelles.
>
> Décodage : `δ = 0.31214773`, `τ = 40 ms` ⇒ latence représentative
> `v = 40 × 1.31214773 = 52.49 ms` → violation → `is_compliant: false`. Cohérent.

**`/arbitrate` → verdict** :
```json
{ "decision": "stay", "path": "C", "deadband_applied": 0.0,
  "considered": [ { "provider_id": "provider-2", "retained": false,
                    "why": "non conforme (mode hard)" } ],
  "alert": { "kind": "INFAISABLE",
             "best_effort": { "provider_id": "provider-2", "vm_id": "edge2",
                              "gap_grade": 0.3121477301631655 } } }
```

| Ce qui est PROUVÉ en plus | ✓ |
|---|:--:|
| **Lot 2** — `G = δ` sur données réelles (non-régression autonome) | ✅ |
| **Lot 3** — bid complet · **CAS B** : best-effort par Gap Grade, TOPSIS non appelé | ✅ |
| **Lot 3** — `assessment.best_effort_vm` bien ignoré (la `reason` cite le Gap Grade) | ✅ |
| **Lot 5** — chemin **C**, `decision: "stay"`, alerte **INFAISABLE** | ✅ |
| **Lot 5** — `SLO_ENFORCEMENT="hard"` : best-effort **calculé, annoncé, refusé** (Q10) | ✅ |
| **Lot 5** — filtre : `evaluable` passé puis blocage sur `is_compliant` (`why`) | ✅ |
| **Lot 1** — `/status` P2 : `role: active`, `hosting_vm: edge2` | ✅ |

**⬜ Reste à prouver** : les chemins **A** et **B**, et le **dead-band** avec de
vraies valeurs — cela exige **deux bids conformes simultanés**, donc le bridge
PiCar avec le robot assez proche pour que la latence prédite passe sous 40 ms.

**🟠 Défaut cosmétique relevé** : le champ `detail` est imbriqué **trois fois**
(`{"detail":{"detail":{"detail":"…"}}}`) — chaque étage (hub → `/inbound/evaluate`
→ `/broadcast`) réencapsule le précédent. Sans impact fonctionnel, mais illisible
au dashboard. **À aplatir au lot 6.**

---

## Livrables annexes

| Livrable | Statut | Note |
|---|:---:|---|
| Diagramme **PIPELINE — MODE AUTONOME** | 🟡 | logique validée ; 2 retouches texte restantes (boîte `BROADCAST` en double, `PROVIDER N ...` dupliqué) |
| Diagramme **PIPELINE — MODE ENHANCED** | 🟡 | logique validée ; 2 retouches texte restantes (`BROACCAST` → `BROADCAST`, `poidd` → `poids`) |
| Validation encadrant des 2 pipelines | ⬜ | prérequis avant de lancer le lot 1 |

---

## ⚠️ Dette identifiée au lot 1b — à traiter au lot 6b

### Incohérence d'opérateur entre chemin réactif et chemin proactif

```python
_is_violation            (RÉACTIF)  →  op = meta["operator"]        # METRICS_REGISTRY
_step5_check_violations  (PROACTIF) →  op = slo.get("operator")     # le SLO
```

Sur une **même métrique au même cycle**, les deux chemins peuvent rendre des
verdicts **opposés** dès que le SLO déclare un opérateur différent de celui du
registre — ce que le LLM peut faire en mode enhanced (`cpu_usage >= 2.5 cores`
alors que le registre porte `<` pour `cpu_usage`).

### Comparaison sans conversion d'unité (les DEUX chemins)

Les prédictions ML sont en **pourcentage** ; un SLO en unité absolue porte un
seuil en **cœurs**/**Go**. Comparer `65` à `1.0` n'a aucun sens.

| Chemin | Opérateur | Unités |
|---|:--:|:--:|
| Réactif (`_is_violation`) | ❌ registre au lieu du SLO | ❌ % vs cœurs |
| Proactif (corrigé au lot 1b) | ✅ | ❌ % vs cœurs |

**Sans effet aujourd'hui** : en mode autonome le seul primaire est `latency`,
en ms des deux côtés, opérateur `<` identique dans le registre et dans le SLO.
**À corriger avant toute démo enhanced comportant un primaire en `cores`/`GB`.**

**Correctif attendu** : faire passer les deux chemins par
`_representative_value` / `_to_criterion_value`, qui appliquent déjà la
conversion (voir le piège documenté au lot 3).

---

## Points à trancher plus tard

- Coût comme **second objectif** de l'arbitre (bi-objectif `(G, coût)`).
- Boucle de retour utilisateur sur infaisabilité (via LLM) — écartée en Q12.
- Registre **dynamique** des orchestrateurs (`/register`) — statique suffit.
- Correctif cosmétique de la **docstring périmée** de `TopsisSelector`
  (à traiter **hors** de cette refonte).
- **Docstring inexacte de `compute_gap_grade`** (`provider_arbitration.py:350-355`) :
  elle explique le risque de signe par « une VM peut violer un SLO **secondaire** »,
  alors que les secondaires ne sont **jamais** pris en compte (filtre `is_primary`).
  La vraie cause est le terme d'augmentation `ρ·Σ` : une violation minuscule
  (max ≈ +0.0006) peut être renversée par de larges marges (Σ ≈ −0.36), d'où
  `G < 0` sur une VM non conforme. Sans effet sur le comportement, mais à
  corriger — ce texte sera lu en soutenance.
- **Robustesse** : `state.cycle_count % config.ACTIVE_VM_SYNC_EVERY_N_CYCLES`
  (`orchestrator_core.py:1415`) lève une `ZeroDivisionError` si la variable
  d'env vaut `0`. Durcir avec `max(1, …)`.
- Effet de bord cosmétique du mode standby (historique vide, log trompeur).
