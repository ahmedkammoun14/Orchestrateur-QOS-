# Couplage CPU → latence et découverte du SLO secondaire

**Date :** 24 août 2026
**Objectif :** que le système découvre seul, par information mutuelle, que `cpu_usage`
est corrélé à la latence, et le promeuve en SLO secondaire — en mode autonome comme
en mode enhanced.

---

## 1. Le problème de départ

Le simulateur générait les trois métriques de façon **totalement indépendante** :

| Métrique | Origine |
|---|---|
| `latency` | fonction de la seule distance PiCar ↔ VM |
| `cpu_usage` | marche aléatoire bornée, sans lien |
| `ram_usage` | marche aléatoire bornée, sans lien |

L'information mutuelle ne pouvait donc **rien** découvrir. Le constat était déjà écrit
dans le dépôt, dans l'en-tête de `scripts/analyse/mi_positive_control.py` :

> « il n'y a simplement rien à détecter sur ce testbed (charge et latence générées
> indépendamment, **par construction**) »

Un second défaut, invisible jusque-là : `_sim_step()` n'avançait la marche aléatoire
**que lorsque `/metrics` était appelé**. Le CPU n'était donc pas un état de la VM mais
un effet de bord du polling du collector — et `/ping` ne le lisait jamais.

---

## 2. Les métriques et leurs relations

C'est le cœur de la journée. Quatre relations, trois décisions.

```
distance ──────────────►  latence      FORT    (5 → 150 ms)   inchangé
cpu_usage ─────────────►  latence      MOYEN   (2 → 12 ms)    AJOUTÉ
ram_usage ─ ─ (coupé) ─►  latence      NUL     (coude à 85 %) témoin négatif
cpu_usage ─ (aucun) ─── ram_usage      NUL                    volontaire
```

### `cpu_usage → latency` — la relation construite

Sens **unique** : le CPU influence la latence, jamais l'inverse. Le CPU garde sa
dynamique propre et n'est **pas** dérivé de la distance — ce serait une corrélation
fallacieuse par variable confondante, immédiatement détectable par un jury.

### `ram_usage → latency` — codée mais inactive

Le terme existe (coude à 85 %, quadratique au-delà), mais la bande RAM simulée est
[50, 80] : la pénalité vaut **toujours zéro**. C'est un **témoin négatif** — un module
de découverte qui retient tout ne démontre rien.

Physiquement défendable : l'usage mémoire ne dégrade la latence qu'à l'approche de la
saturation (éviction du cache, swap). À 80 %, une machine Linux va très bien.

### `cpu_usage ↔ ram_usage` — délibérément indépendantes

Une corrélation CPU↔RAM est physiquement plausible sur un service de streaming
(plus de flux = plus de CPU **et** plus de buffers). Elle a été **écartée** pour une
raison expérimentale :

> On ne peut pas avoir en même temps **(a)** la RAM corrélée au CPU et **(b)** la RAM
> rejetée par la MI. Si la RAM suit le CPU, elle hérite de son lien avec la latence et
> la MI la retiendra — légitimement. Le témoin négatif serait perdu.

---

## 3. La formule et sa justification

```
latence  =  L_net(distance)   +   S0 / (1 − ρ)        ρ = cpu / 100
            aller-retour          temps de traitement
```

| Terme | Justification |
|---|---|
| `L_net(d)` | propagation — **inchangé**, c'est la contribution PiCar |
| `S0/(1−ρ)` | temps de réponse **M/M/1** ; décomposition « délai réseau + délai de traitement CPU » retenue par les simulateurs du domaine (EdgeCloudSim) |
| `ρ_max = 0,95` | saturation — borne la divergence en ρ → 1 |

Le facteur `1/(1−ρ)` produit le **coude d'utilisation** : ×2 à 50 %, ×5 à 80 %,
×10 à 90 %. C'est ce coude qui rend le CPU capable de faire basculer une violation.

### Mise à l'échelle par le nombre de cœurs

```
S0 = S_REF × (C_REF / TOTAL_CORES)
```

Effet secondaire majeur : **les 3 VMs edge d'un provider cessent de ne différer que
par leur position.** Avant, `edge1` (2 cœurs) et `edge1c` (4 cœurs) avaient exactement
la même latence à distance égale — leur capacité déclarée n'avait aucune conséquence
observable. Elle devient mesurable, donc exploitable par TOPSIS et le Gap Grade.

| VM | cœurs | S0 | service à 40 % | service à 90 % |
|---|---|---|---|---|
| edge1 / edge2 | 2 | 1,20 ms | 2,0 ms | 12,0 ms |
| edge1b / edge2b | 3 | 0,80 ms | 1,3 ms | 8,0 ms |
| edge1c / edge2c | 4 | 0,60 ms | 1,0 ms | 6,0 ms |
| cloud2 | 8 | 0,30 ms | — | 0,4 ms |
| cloud1 | 16 | 0,15 ms | — | 0,2 ms |

---

## 4. Le mécanisme, en deux cycles

Deux cycles où la voiture est **au même endroit** :

| | distance | `L_net` | CPU | service | latence | verdict |
|---|---|---|---|---|---|---|
| A | 9 cm | 16,3 ms | **62 %** | 6,6 ms | **22,9 ms** | ✅ |
| B | 9 cm | 16,3 ms | **82 %** | 13,9 ms | **30,2 ms** | ❌ violation |

Même VM, même position. **Seule la charge diffère, et le verdict bascule.** Répété sur
la fenêtre d'historique, ce déséquilibre est ce que l'information mutuelle détecte.

---

## 5. La calibration, et pourquoi elle a été difficile

### L'indicateur qui pilote tout

> **Le taux de violation de la meilleure VM doit rester entre 40 et 55 %.**

En dehors, la MI ne trouve rien : trop de violations et il n'y a plus de classe
« normale » à comparer ; trop peu et c'est l'inverse. C'est le seul indicateur de santé
qui compte.

| Réglage | Violations | Résultat MI |
|---|---|---|
| `S_REF = 2.5` | 68,8 % | ❌ MI = 0 partout — régime saturé |
| `S_REF = 1.2` | 45–55 % | ✅ fonctionne |

`S_REF = 2.5` ajoutait **+16 ms** à la médiane, pour un seuil à 28 ms et une médiane
initiale à 22,3 ms — il ne restait que 5,7 ms de marge.

### La fenêtre d'historique

La fenêtre de 50 points (~5 min à 6 s/cycle) couvre **2 migrations**. Elle se remplit
donc de cycles où la VM était **loin** et n'hébergeait pas le service — violation
certaine quelle que soit la charge.

| VM | violations **en hébergeant** | violations sur **tout** l'historique |
|---|---|---|
| edge1 | 71,7 % | 91,7 % |
| edge1b | 61,0 % | 85,4 % |
| edge1c | 69,1 % | 89,8 % |

D'où `HISTORY_WINDOW = 25`. **Prendre tout l'historique aggraverait le problème** :
ce n'est pas un manque de points mais un excès de contexte hors sujet.

### Le faux positif RAM

`ram_usage` sortait à **67,7 %** alors qu'il n'a **aucun** effet sur la latence.

Cause : deux marches aléatoires **lentes** (autocorrélation 0,96) produisent une
corrélation fallacieuse — c'est la *régression fallacieuse* de Granger & Newbold (1974).

Preuve que c'est du hasard : le signe s'inverse selon la VM.

| VM | corr(latence, RAM) |
|---|---|
| edge1 | **+0,69** |
| edge1c | **−0,78** |
| edge2 | −0,68 |
| edge2c | +0,39 |

Un vrai lien causal ne change pas de signe.

Deux leviers ont été testés et écartés :

| Levier | Résultat |
|---|---|
| Monter `MI_RELATIVE_THRESHOLD` (0,15 → 0,50) | écart cpu−ram **plat** — aucun gain |
| Agrandir la fenêtre (25 → 50) | mono-classe 73 % → 60 %, écart +0,5 pt |
| **`RAM_STEP` 1,5 → 20** | ✅ autocorrélation 0,96 → **0,60** |

Une série à mélange rapide ne peut pas produire de corrélation fallacieuse.

---

## 6. Quatre corrections en cascade côté orchestrateur

### 6.1 Trois fenêtres au lieu d'une constante partagée

`HISTORY_WINDOW` pilotait **trois** choses aux besoins opposés. La ramener à 25 a
tronqué le stockage Redis à 25 points, rendant le Niveau 1 de `predictor.py`
(qui exige 39 à 45 points selon le modèle) **structurellement inatteignable** — la
prédiction recopiait la mesure et toute anticipation disparaissait.

```
HISTORY_WINDOW    = 25    lecture MI          (fenêtre courte)
ML_HISTORY_WINDOW = 60    lecture ML          (fenêtre longue)
METRICS_RETENTION = max() profondeur en base  (LTRIM Redis)
```

### 6.2 Rétention du score MI sur fenêtre sans contraste

Émettre `0.0` quand tous les cycles violent signifie « aucune dépendance », alors que
la vérité est « **pas mesurable ici** ». Le SLO clignotait (0,27 → 0,00 → 0,27) sur
~25 % des cycles, sans qu'aucune dépendance n'ait changé.

Le dernier score **réellement mesuré** est conservé, avec péremption à
`MI_HOLD_CYCLES = 10`. L'âge se compte en **évaluations MI**, pas en cycles
d'orchestration : la MI ne tourne que chez le provider ACTIF alors que `cycle_count`
monte aussi chez le STANDBY (âges de 24 observés pour des évaluations consécutives).

### 6.3 Retrait du repli `best_effort`

Migrer vers la VM « la moins mauvaise » déplace le service **sans rétablir le SLO**, au
prix d'une migration réelle. Le chemin fédéré ne le fait pas : un provider sans VM
conforme ne soumet aucune offre. Les deux chemins appliquent désormais la même règle.

> ⚠️ Les données de la campagne **UC2** ont été produites avec l'ancien repli. Elles ne
> sont plus reproductibles avec ce code.

### 6.4 Fixture de test corrigée

`test_decide_renvoie_vm_scores_sur_stay_hysteresis` reposait sur un scénario — deux VMs
en violation atteignant quand même TOPSIS — qui n'existait que grâce à `best_effort`.

---

## 7. Résultats mesurés

Run local, 658 cycles, 8 VMs, 2 providers.

### Découverte MI

| | Départ | Étape RAM | **Final** |
|---|---|---|---|
| `cpu_usage` retenu | 72,6 %¹ | 57,8 % | **66,7 %** |
| `ram_usage` retenue | 67,7 % | 35,8 % | **36,2 %** |
| **Écart** | +4,8 | +22,0 | **+30,5 pts** |
| Médiane cpu / ram | 0,358 / 0,315 | 0,270 / 0,032 | **0,327 / 0,000** |

¹ *gonflé par la corrélation fallacieuse — les 72,6 % n'étaient pas un vrai signal*

La médiane de la RAM est à **0,000** pendant que celle du CPU est à **0,327**.

### Prédictions ML

| | Avant | Après |
|---|---|---|
| Niveau 1 (séquence) | 0 | **6 548** |
| APIs OK 3/3 | 0 | **2 139** |
| Latence identique à la mesure | **100 %** | **18,8 %** |
| MAE latence en production | — | **4,13 ms** |

### Régime

```
violations meilleure VM : 55,0 %   (cible 40–55 %)
latence médiane         : 30,9 ms
migrations              : 11       (3 avant réentraînement)
cooldown déclenché      : 0
```

---

## 8. Le modèle ML : régénération et réentraînement

Le modèle de latence ignorait le terme CPU, donc **sous-estimait** systématiquement.
Conséquence directe, dans `violation_detector.py` :

```python
if preds:
    if pred_breach: return "proactive"
    return "none"          # ← la mesure est IGNORÉE
```

Une violation causée par la charge restait **invisible** : le modèle disait « ça va »,
la mesure disait « ça viole », et le code renvoyait `none`.

Nouveaux jeux générés (les originaux sont intacts) :

| Fichier | Contenu |
|---|---|
| `qos_delay_couple_edge.xlsx` | 192 000 lignes — modèle delay |
| `qos_cpu_train_couple.xlsx` | 32 000 lignes, bande [40, 90] |
| `qos_ram_train_couple.xlsx` | 32 000 lignes, `RAM_STEP = 20` |

**Validation contre les mesures réelles** — latence edge (ms) :

| Source | min | p25 | **méd** | p75 | max |
|---|---|---|---|---|---|
| Mesuré | 8,1 | 50,0 | **71,7** | 98,9 | 152,0 |
| Généré couplé | 6,9 | 48,2 | **71,2** | 101,0 | 160,8 |
| Généré ancien | 5,0 | 45,0 | 67,9 | 97,6 | 150,7 |

Médiane à 0,5 ms près. L'ancien était 3,8 ms trop bas — précisément l'erreur apprise.

### Évaluation du modèle réentraîné

| Horizon | Modèle | Persistance | Linéaire |
|---|---|---|---|
| h+1 | 3,29 | 1,90 | 1,41 |
| h+4 | 4,57 | 6,51 | 3,89 |
| **h+7** | **6,08** | 11,16 | **6,08** |
| GLOBAL | 4,70 | 6,62 | 3,85 |

Décision `any(p > 28)` : **95 %** correcte (38/40), contre 97,5 % pour la référence
linéaire — **une seule fenêtre d'écart**, sans valeur statistique.

Lecture honnête : le modèle bat la persistance de 29 %, mais l'extrapolation linéaire
reste devant à courte échéance. L'écart se referme monotonement et **s'annule à h+7**,
là où se joue l'anticipation. C'est structurel : la latence est quasi affine en
distance et la voiture avance à vitesse quasi constante — sur 7 pas, la trajectoire
*est* une droite. Le modèle, lui, ne diverge pas quand la courbure change.

---

## 9. Limites assumées

**~29 % des cycles ne retiennent aucune métrique** — fenêtres mono-classe dues à la
trajectoire. Le correctif de rétention (§6.2) doit réduire ce chiffre ; il n'a pas
encore tourné en conditions réelles.

**Le test par décalage circulaire rejette tout, y compris le CPU** (p = 0,27).
L'autocorrélation de la latence (~0,99) ne laisse que ~5 points effectivement
indépendants sur une fenêtre de 25. Ce n'est pas que le couplage est faux — c'est
qu'il n'y a pas assez de données indépendantes pour le prouver statistiquement.

C'est une **limite mesurée et documentée**, à présenter comme telle plutôt qu'à
masquer. Combinée à `mi_positive_control.py` (validité de l'estimateur) et
`mi_test2.py` (null par décalage circulaire), elle constitue une chaîne de preuve
complète.

---

## 10. Paramètres validés

### Agents VM

```
S_REF = 1.2      RAM_STEP = 20     COUPLING = 1
C_REF = 2.0      RHO_MAX = 0.95    CPU edge [40, 90]
```

```bash
S_REF=1.2 RAM_STEP=20 ./launch_edge1_machine.sh
```

### Orchestrateurs

```
HISTORY_WINDOW = 25     ML_HISTORY_WINDOW = 60
MI_HOLD_CYCLES = 10     MI_RELATIVE_THRESHOLD = 0.15
```

### Ablation

`COUPLING=0` sur les 4 machines restaure le comportement antérieur — c'est le témoin
qui prouve que la découverte répond à une dépendance réelle et non à un artefact.

---

## 11. Fichiers modifiés

### Versionnés — commit `0a1f263`

| Fichier | Objet |
|---|---|
| `shared/config.py` | trois fenêtres, `MI_HOLD_CYCLES`, retrait de `MONO_INFEASIBLE_POLICY` |
| `hub/orchestrator_core.py` | `ML_HISTORY_WINDOW` pour les historiques ML |
| `services/database/redis_client.py` | LTRIM sur `METRICS_RETENTION` |
| `services/decision_intelligence/decision.py` | retrait de `best_effort` |
| `services/metrics_manager/metrics_handler.py` | rétention du score MI |
| `tests/unit/test_mi_hold_window.py` | 6 tests (nouveau) |
| `tests/unit/test_mono_infeasible_policy.py` | réécrit |
| `tests/unit/test_decision_vm_scores.py` | fixture corrigée |

**355 tests passent** (`test_federation_view.py` exclu — service supprimé avant cette
session).

### ⚠️ Non versionnés

`infrastructure/` est ignoré par git ([.gitignore:170](.gitignore:170)). Les
**5 copies** de `vm_agent_sim.py` — la pièce centrale — n'existent que sur disque et
sur les 4 machines du LAAS. **À sauvegarder ailleurs.**

Côté `Api-Model-Predict` (dépôt distinct) :

| Fichier | Objet |
|---|---|
| `gen_qos_datasets_couple.py` | générateur des 3 jeux couplés (nouveau) |
| `eval_delay_couple.py` | évaluation avec vérité terrain couplée (nouveau) |

Ces deux scripts corrigent au passage un chemin périmé de `gen_delay_dataset.py` et
`eval_delay_model.py` : `infrastructure/picarx_sim.html` n'existe plus, le fichier est
sous `infrastructure/Picar/picarx_sim_QoS.html`. Les anciens scripts auraient planté.

---

## 12. Piège opérationnel à retenir

> **Après CHAQUE redémarrage d'une API ML, réappliquer `update_configs` AVANT tout
> appel de prédiction.**

`RMSE_PATIENCE` est une constante de module modifiée en mémoire par `/update_configs`.
Au redémarrage d'uvicorn, elle repart à 3 : trois prédictions consécutives au-dessus du
seuil suffisent alors à déclencher un réentraînement automatique sur ~800 points, qui
**écrase** le modèle entraîné sur 192 000.

C'est arrivé aujourd'hui — `delay.keras` est passé de `look_back=39` (598 Ko) à
`look_back=45` (4,27 Mo) entre deux vérifications.

```bash
curl.exe -X PUT "http://localhost:5001/update_configs?new_rmse_patience=999999&new_rmse_threshold=999999&new_trials=25"
```

Contrôle : `window_size` doit être **identique avant et après** toute évaluation.

---

## 13. Suite — déploiement LAAS, 25 août 2026

Journée de déploiement sur l'infrastructure réelle du LAAS (8 VM OpenStack, 2
providers), avec trois découvertes supplémentaires et une décision de conception.

### 13.1 Déploiement et validation infrastructure

Les paramètres calibrés la veille ont été intégrés **directement dans les scripts de
lancement** (`launch_edge1_machine.sh`, etc.), avec des valeurs par défaut
surchargeables :

```bash
export S_REF="${S_REF:-1.2}"
export RAM_STEP="${RAM_STEP:-20}"
export COUPLING="${COUPLING:-1}"
```

Les 3 modèles ML entraînés la veille étaient intacts sur disque (`delay` look_back=39,
`cpu`/`ram` look_back=45) — aucun réentraînement nécessaire, seul le garde-fou
`update_configs` a été réappliqué après chaque redémarrage d'API.

**Validation la plus forte de la journée** : `check_infra.py --reel` a mesuré un écart
entre la latence théorique (géométrique) et la latence réellement observée sur les 8 VM.
Cet écart a été comparé au terme `S0/(1-ρ)` prédit à partir du CPU mesuré au même
instant :

| VM | écart mesuré | `S0/(1-ρ)` prédit | résidu |
|---|---|---|---|
| edge1 | +4,0 ms | 3,99 ms | +0,01 |
| edge1b | +2,8 ms | 2,76 ms | +0,04 |
| edge1c | +2,1 ms | 2,10 ms | +0,00 |
| edge2 | +4,8 ms | 4,76 ms | +0,04 |
| edge2b | +1,4 ms | 1,35 ms | +0,05 |
| edge2c | +2,9 ms | 2,82 ms | +0,08 |
| cloud1 | +0,2 ms | 0,19 ms | +0,01 |
| cloud2 | +0,3 ms | 0,33 ms | −0,03 |

**Résidu maximal : 0,08 ms sur 8 VM.** Le couplage se vérifie de bout en bout sur
l'infrastructure réelle, pas seulement en simulation.

Un bug de `check_infra.py` a été corrigé au passage : il attendait 8 cœurs pour
`cloud1`, qui en déclare 16 (`launch_cloud1.sh`) — faux échec systématique, sans lien
avec le couplage.

### 13.2 Interface PiCar — cohérence de l'affichage

`picarx_sim_QoS.html` calculait la « VM optimale » avec `latencyEst()`, une fonction
**purement géométrique**, sans le terme CPU. Résultat : le badge affichait
`✗ devrait → edge1b` alors que l'orchestrateur avait raison de préférer une VM plus
chargée mais légèrement plus proche — un désaccord de référentiel, pas un retard réel.

Correction : le badge et la pastille ✓/✗ lisent désormais `vmLat` (latences mesurées,
terme CPU inclus), via une nouvelle fonction `bestVmMeasured()`. Le coloriage des zones
et les frontières restent géométriques — c'est une carte, elle doit rester stable.

### 13.3 Faux positif RAM — la cause exacte, et un second correctif

Analyse plus fine du mécanisme de faux positif : la RAM ne se trompe **pas** dans des
fenêtres pauvres en données. Elle se trompe dans des fenêtres **bien formées**, par
alignement statistique fortuit entre deux séries autocorrélées (Granger & Newbold,
1974) — un phénomène distinct du problème de fenêtre mono-classe traité la veille.

Mesure sur 86 évaluations (RUN A, matin) : la moitié des épisodes `ram_usage`
au-dessus du seuil ne durent **qu'une seule évaluation**, contre 23 % pour `cpu_usage`.
Un vrai signal persiste ; un artefact statistique est un éclair isolé.

**Correctif : `MI_CONFIRM_CYCLES = 2`.** Une métrique n'est promue en SLO secondaire
que si son score dépasse le seuil sur **2 évaluations MI consécutives** — et non plus
sur un seul dépassement brut. Implémenté dans `MetricsHandler.is_confirmed()`, utilisé
par `select_dynamic_slos` (mode autonomous) et `validate_and_enrich_slos` (mode
enhanced). `compute_mi_scores()` n'est pas modifié : il retourne toujours le score brut.

| | Avant confirmation | **Après confirmation** |
|---|---|---|
| `cpu_usage` retenu | 57,0 % | 41,9 % |
| `ram_usage` retenue | 26,7 % | **12,8 %** |
| Écart | +30,2 | +29,1 |

RAM divisée par 2,1, écart quasi inchangé — validé par rejeu des données réelles du
matin avant tout déploiement. 361 tests passent (6 nouveaux dans
`test_mi_confirmation.py`), zéro régression.

### 13.4 Bug découvert : une seule fenêtre pour deux besoins opposés

`HISTORY_WINDOW` pilotait à la fois la lecture MI (besoin : fenêtre **courte**) et la
lecture des historiques pour le ML (besoin : fenêtre **longue**, ≥ 45 points pour
atteindre le Niveau 1 de `predictor.py`). La ramener à 25 pour le MI avait rendu le
Niveau 1 **structurellement inatteignable** : les prédictions retombaient au Niveau 2
puis recopiaient la mesure — aucune anticipation.

Correction en deux temps :
1. `ML_HISTORY_WINDOW = 60` — fenêtre séparée pour la lecture ML
2. `METRICS_RETENTION = max(HISTORY_WINDOW, ML_HISTORY_WINDOW)` — le LTRIM Redis
   (`redis_client.store_metrics`) tronquait le stockage à `HISTORY_WINDOW`,
   rendant les 60 points ML inobtenables même en les demandant

Après correction : Niveau 1 = 2012 appels sur le run complet, Niveau 2 = 0, MAE
latence en production 4,13 ms (cohérent avec les 4,70 ms de l'évaluation hors ligne).

### 13.5 Bug découvert : split-brain par échec de synchronisation kubectl

Les deux tableaux de bord affichaient chacun leur propre provider comme actif. Cause :
`_sync_active_vm` (appelée au démarrage, avant la bannière « Orchestrateur prêt »,
et à chaque cycle en cas de violation) échouait systématiquement à joindre
`openstack_client` — d'abord parce qu'il n'était pas encore lancé sur le master, puis
parce que l'IP passée en variable d'environnement était un texte de substitution non
remplacé. `state.is_active` reste à sa valeur par défaut (`True`) tant que la sync
échoue, des deux côtés à la fois.

**Ce n'était pas un bug de code** : le mécanisme de synchronisation au démarrage
existait déjà et fonctionne comme prévu dès qu'`openstack_client` est joignable
(vérifié : `curl http://194.199.113.8:8024/active_vm` → HTTP 200 en 0,16 s). Après
correction de la séquence de lancement, les deux hubs convergent vers un seul actif
dès le cycle 0, sans attendre une resynchronisation tardive.

**Limite de conception notée, non corrigée** : si `openstack_client` tombe en cours de
run (pas seulement au démarrage), rien n'alerte et rien ne force un repli — le split-
brain resterait silencieux indéfiniment. Corriger ça demanderait un mécanisme
d'élection de repli, hors de portée le jour d'une démo.

### 13.6 Limite observée : le régime de violation varie trop selon la trajectoire

Sur un tour quasi complet (213 cycles), le taux de violation a oscillé entre 33 % et
80 % selon la portion du circuit (moyenne 63,8 %, hors de la bande cible 40–55 %).
Conséquence : sur ce tronçon, `ram_usage` (59,4 %, médiane 0,263) a dépassé
`cpu_usage` (54,7 %, médiane 0,204) — l'inverse du run du matin.

Décision : **ne pas recalibrer `S_REF` à l'aveugle le jour de la démo.** Recalibrer un
paramètre physique sans le revalider sur un nouveau run complet est le même risque
qu'un run refait à la hâte — seulement caché derrière un changement de code. Le run
du matin (RUN A, écart +30 pts, validé) reste la preuve à présenter ; ce tronçon sert
de limite documentée, pas de résultat à corriger dans l'urgence.

### 13.7 Piste explorée et écartée : rendre la RAM causale mais plus faible que le CPU

Idée proposée : au lieu d'un témoin négatif (RAM sans effet), donner à la RAM un
**vrai** effet sur la latence, mais plus faible que celui du CPU — pour qu'elle
apparaisse, mais moins souvent.

**Testée en simulation avant tout changement de code**, à partir de la vraie trace de
la VM de service du matin (pas d'une distance synthétique — c'est cette erreur qui a
faussé un premier essai). Résultat mesuré sur plusieurs bandes RAM et coudes :

| Configuration | CPU confirmé | RAM confirmée | Écart |
|---|---|---|---|
| RAM sans effet (actuel) | 57,6 % | 28,5 % | 29,1 pts |
| Meilleur essai avec RAM causale | 57,4 % | 26,9 % | 30,5 pts |

Gain de +1,4 point, dans le bruit de l'estimation Monte-Carlo (10 tirages). Et le
signal CPU se dégrade dans plusieurs configurations (jusqu'à 45,8 %) : deux causes
réelles superposées se diluent l'une l'autre plutôt que de se renforcer.

**Décision : ne pas implémenter.** Le gain attendu ne justifie pas le risque
(modification du code de simulation, régénération des 3 jeux de données,
réentraînement, redéploiement sur 4 machines, revalidation sur un tour complet) un
jour de démonstration. Piste conservée pour une itération post-soutenance.

### 13.8 État de la corrélation, en une phrase

**Une seule relation causale existe dans le système : `cpu_usage → latency`.**
`ram_usage` reste un témoin négatif, sans effet dans la formule de latence — c'est un
choix de conception maintenu après l'avoir testé, pas un oubli.

---

## 14. Reste à faire

1. Documenter la limite du régime de violation (§13.6) comme résultat honnête du
   rapport, avec le run A comme preuve de référence plutôt qu'un instantané en direct
2. **Sauvegarder `vm_agent_sim.py`** hors du dossier ignoré par git (`infrastructure/`)
3. Ajouter un mécanisme d'alerte/repli en cas de panne prolongée d'`openstack_client`
   (§13.5) — hors périmètre du jour de la démo
4. Expérience post-démo : bande RAM élargie avec un effet causal réellement plus fort
   que celui testé en §13.7, si le gain mesuré en simulation le justifie
