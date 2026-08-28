# Améliorations identifiées — à appliquer APRÈS la campagne

> ⚠️ **Ne rien modifier avant d'avoir fini d'écrire le papier.**
> UC1 et UC2 (les deux runs de référence sur l'infrastructure réelle du
> LAAS) ont été produits avec les valeurs actuelles. Changer un seul de ces
> paramètres invalide la comparaison et oblige à refaire les deux runs.

---

## Le problème mesuré

En mode **fédéré** (UC1, 8 VMs), le système laisse passer **12,2 % du temps
en violation évitable** — c'est-à-dire des instants où une VM conforme
existait mais n'a pas été choisie.

En mode **mono-provider** (UC2, 4 VMs), ce chiffre tombe à **0,7 %** : le
système y est quasi optimal.

**Diagnostic mesuré** — corrélation entre la VM choisie et la meilleure VM
à différents instants passés :

```
VM choisie = meilleure VM d'il y a  0 s : 83,8 %
VM choisie = meilleure VM d'il y a  6 s : 86,7 %
VM choisie = meilleure VM d'il y a 12 s : 88,6 %   <-- maximum
VM choisie = meilleure VM d'il y a 18 s : 87,3 %
VM choisie = meilleure VM d'il y a 30 s : 80,7 %
```

**Le système est en retard d'environ 12 secondes sur l'optimum** — soit
2 cycles de 6 s. Le retard n'apparaît qu'en fédéré : avec 4 VMs seulement,
la meilleure VM change trop rarement pour qu'un retard soit visible
(pic à 0 s, 96,9 %).

---

## AMÉLIORATION 1 — Rééquilibrer les poids de l'horizon de prédiction

**Priorité : haute. Effort : très faible. C'est la cause directe du retard.**

### Ce qui se passe aujourd'hui

Le modèle ML renvoie **7 pas de prédiction** (`horizon: 7`, défini dans
`services/ml_predictor/predictor.py:124`). La décision les utilise tous,
mais avec des poids **décroissants** :

**Fichier** : `services/decision_intelligence/topsis.py`, lignes 251-257

```python
def calculate_weighted_mean(self, preds: List[float]) -> float:
    if not preds:
        return 0.0
    n:       int       = len(preds)
    weights: List[int] = list(range(n, 0, -1))   # [7, 6, 5, 4, 3, 2, 1]
    total:   int       = sum(weights)            # 28
    return sum(w * p for w, p in zip(weights, preds)) / total
```

Répartition réelle du poids :

| Pas | t+1 | t+2 | t+3 | t+4 | t+5 | t+6 | t+7 |
|-----|-----|-----|-----|-----|-----|-----|-----|
| Poids | 7 | 6 | 5 | 4 | 3 | 2 | 1 |
| Part | **25,0 %** | 21,4 % | 17,9 % | 14,3 % | 10,7 % | 7,1 % | 3,6 % |

Le pas immédiat pèse un quart du total. La moyenne est donc **tirée vers le
présent**, ce qui fait que le système réagit à la sortie de zone au lieu de
l'anticiper.

### La modification

Déplacer le maximum de poids vers les pas 2-3, qui correspondent à
l'horizon utile pour anticiper (12 s ≈ 2 cycles) :

```python
def calculate_weighted_mean(self, preds: List[float]) -> float:
    """
    Moyenne pondérée sur l'horizon de prédiction.

    Les poids privilégient les pas 2-3 plutôt que le pas immédiat : le
    retard mesuré du système sur l'optimum est de ~12 s, soit 2 cycles.
    Pondérer le présent le plus fort (ancien schéma [7,6,5,4,3,2,1])
    faisait réagir le système APRÈS la sortie de zone de couverture.

    Les pas lointains (t+5 à t+7) restent faiblement pondérés : la
    précision du modèle s'y dégrade.
    """
    if not preds:
        return 0.0
    n = len(preds)
    default = [3, 5, 5, 4, 3, 2, 1]          # profil centré sur t+2/t+3
    weights = (default[:n] if n <= len(default)
               else default + [1] * (n - len(default)))
    total = sum(weights)
    return sum(w * p for w, p in zip(weights, preds)) / total
```

### Comment vérifier que ça marche

1. Relancer un run fédéré (3 tours, même trajectoire qu'UC1)
2. Recalculer le taux de violation avec `violation_rate.py`
3. **Attendu** : violation < 19,8 %, et le pic de corrélation du diagnostic
   doit se déplacer de 12 s vers 6 s ou 0 s
4. **Surveiller** : le nombre de migrations ne doit pas exploser (UC1 = 21).
   Si on passe à 60+, la marge d'hystérésis compense mal — voir amélioration 3.

### Risque

Les prédictions à t+2/t+3 sont moins fiables que t+1. Avec un MAE latence
de 3,42 ms, l'erreur reste faible devant un seuil de 28 ms, donc le risque
est limité — mais il faut vérifier que le taux de migration reste stable.

---

## AMÉLIORATION 2 — Réduire la période du cycle (6 s → 3 s)

**Priorité : moyenne. Effort : faible EN APPARENCE. Coût : double la charge.**

> ### ⚠️ PRÉALABLE OBLIGATOIRE — étude de dimensionnement
>
> **Ne pas traiter cette modification comme un simple changement de nombre.**
> `SEND_INTERVAL_S` n'est pas un paramètre isolé : il est couplé à toute la
> chaîne de simulation et de décision. Le changer sans revoir les autres
> casse la cohérence du banc.
>
> **Les grandeurs couplées, à réétudier ensemble :**
>
> | Grandeur | Où | Lien avec la période du cycle |
> |---|---|---|
> | `SEND_INTERVAL_S` | bridge, ligne 49 | fixe la période effective |
> | Tick du simulateur HTML | `picarx_sim_QoS.html` | quantifie la période (pas de 2 s → 5 s devient 6 s) |
> | Vitesse du véhicule | simulateur (0,25 cm/s) | distance parcourue par cycle : δ = v × T_cycle |
> | `D_MIN`, `D_MAX`, `LAT_A`, `LAT_B` | `vm_agent_sim.py` 40-43 | convertissent la distance en latence |
> | Rayon de conformité | dérivé | ~15 cm pour un seuil de 28 ms côté edge |
> | `MIGRATION_COOLDOWN_S` | `config.py` 95 | exprimé en secondes, donc en cycles si T change |
> | `HISTORY_WINDOW` (50 points) | `config.py` 68 | 50 cycles = 300 s aujourd'hui, 150 s si T=3 s |
> | Horizon ML (7 pas) | `predictor.py` 124 | 7 pas = 42 s aujourd'hui, 21 s si T=3 s |
> | `HORIZON_ALERT` (3) | `config.py` 163 | idem |
>
> **Les conséquences en cascade, souvent oubliées :**
>
> 1. **L'horizon de prédiction rétrécit en temps réel.** 7 pas × 3 s = 21 s
>    d'anticipation au lieu de 42 s. Or c'est précisément l'horizon qui sert
>    à anticiper la sortie de zone (amélioration 1). Réduire T sans
>    augmenter le nombre de pas *réduit* la capacité d'anticipation.
> 2. **La fenêtre d'historique rétrécit aussi.** 50 points = 150 s au lieu de
>    300 s. Le calcul du MI, déjà fragile (voir plus bas), disposerait de
>    moitié moins de temps couvert.
> 3. **Le cooldown change de sens.** 5 s bloque ~1 cycle à T=6 s, mais ~2
>    cycles à T=3 s. Il faudrait le réexprimer en cycles, pas en secondes.
> 4. **Le rapport δ/rayon change.** À 0,25 cm/s, le véhicule parcourt 1,5 cm
>    par cycle à T=6 s, 0,75 cm à T=3 s. Le rayon de conformité étant
>    ~15 cm, on passe de 10 à 20 cycles pour traverser une zone. C'est le
>    vrai gain — mais il ne sert que si l'horizon suit.
>
> **Étude à mener avant toute modification :**
>
> - Exprimer tous les paramètres temporels **en nombre de cycles**, pas en
>   secondes, puis vérifier lesquels doivent rester constants en secondes
>   (physique du véhicule) et lesquels en cycles (logique de décision).
> - Vérifier dans `timings_autonomous_*.xlsx` (colonne `TOTAL cycle (ms)`)
>   que le cycle tient sous la nouvelle période, **percentile 95 compris** —
>   pas seulement la médiane. Un cycle qui déborde fait abandonner le lot.
> - Recalculer le rayon de conformité et le nombre de cycles par zone pour
>   la nouvelle période.
> - Décider si l'horizon ML passe de 7 à 14 pas pour conserver la même
>   anticipation en secondes.
>
> Tant que cette étude n'est pas faite, **appliquer l'amélioration 1 seule** :
> elle ne touche à aucune grandeur temporelle et ne demande aucun
> redimensionnement.

### Ce qui se passe aujourd'hui

Le cycle n'est pas piloté par l'orchestrateur : il est déclenché par
l'arrivée d'un lot de mesures venant du **bridge PiCar**, qui limite ses
envois.

**Fichier** : `infrastructure/Picar/picar_bridge_QoS2.py`, ligne 49

```python
SEND_INTERVAL_S = 5.0     # aligné sur la durée d'un cycle d'orchestration
```

Utilisé ligne 165 :

```python
if now - _last_send_ts < SEND_INTERVAL_S:
    return   # lot ignoré, pas d'envoi au latency_manager
```

Les 5 s deviennent ~6 s en pratique, car le simulateur HTML envoie ses
positions par pas de 2 s : le premier tick après 5 s tombe à 6 s.

### La modification

```python
SEND_INTERVAL_S = 2.5     # cycle visé ~3 s
```

⚠️ **Ce fichier tourne sur le Raspberry Pi**, pas sur le PC. Il faut le
modifier là-bas et redémarrer le bridge. Ou surcharger par variable
d'environnement si vous ajoutez ce support (le fichier accepte déjà
`VMS_JSON` et `ORCH_HOST` sur ce principe).

### Vérifier d'abord que le cycle tient en 3 s

Le cycle actuel dure ~4,7 s de travail effectif dans les 6 s disponibles.
**Passer à 3 s ne marchera que si le cycle tient sous 3 s.** À vérifier
dans `timings_autonomous_*.xlsx`, colonne `TOTAL cycle (ms)` :

- Si le total médian dépasse 3000 ms → **ne pas faire cette modification**,
  les lots seraient abandonnés en masse (exclusion mutuelle) et on perdrait
  plus de mesures qu'on n'en gagnerait.
- Levier possible avant : le collector fait déjà du sondage en arrière-plan
  (1,4-1,8 s retirés du chemin critique). Regarder ce qui reste dominant.

### Effet attendu

Retard divisé par 2 (12 s → 6 s), donc une partie des 12,2 points récupérée.
Coût : deux fois plus de cycles, donc deux fois plus d'appels ML, d'écritures
Excel et de charge CPU.

---

## AMÉLIORATION 3 — Réduire le cooldown de migration

**Priorité : moyenne. Effort : très faible. Risque : oscillation.**

### Ce qui se passe aujourd'hui

Deux garde-fous empêchent les migrations trop rapprochées.

**a) Cooldown temporel** — `shared/config.py`, ligne 95 :

```python
MIGRATION_COOLDOWN_S: float = float(os.getenv("MIGRATION_COOLDOWN_S", 5.0))
```

Après une migration, aucune autre n'est possible pendant 5 s. Avec un cycle
de 6 s, cela bloque en pratique **au moins un cycle complet**.

**b) Marge d'hystérésis** — `services/decision_intelligence/decision.py`,
ligne 16 :

```python
_MIGRATION_MARGIN: float = 0.05
```

Utilisée ligne 204 :

```python
if to_vm == service_vm or topsis_score <= active_score * (1.0 + _MIGRATION_MARGIN):
    # → STAY
```

Le candidat doit battre la VM active de **plus de 5 %** en score TOPSIS
pour déclencher une migration.

### La modification

**Étape 1 — le cooldown**, sans toucher au code (variable d'environnement) :

```powershell
$env:MIGRATION_COOLDOWN_S="2.0"
```

**Étape 2 — seulement si l'étape 1 ne suffit pas** : abaisser la marge

```python
_MIGRATION_MARGIN: float = 0.03   # au lieu de 0.05
```

### Ordre à respecter

Faire l'**amélioration 1 d'abord, seule**, et mesurer. Si le taux de
migration reste proche de 21, alors essayer le cooldown. **Ne jamais
changer les deux en même temps** : en cas d'oscillation, on ne saurait pas
laquelle des deux en est la cause.

### Signal d'alarme

Si le nombre de migrations par tour dépasse ~40, c'est de l'oscillation :
le système fait des allers-retours entre deux VMs proches. Revenir en
arrière immédiatement.

---

## Ce qui n'est PAS à corriger — et pourquoi

### Le plancher du Gap Grade (`DELTA_FLOOR = -1.0`)

`hub/provider_arbitration.py:106`. Il rend indiscernables deux VMs qui
dépassent toutes deux le double du seuil demandé (voir le cas cloud1 vs
cloud2 documenté en Section VII du papier). Une saturation plus douce
(`tanh`) ou un départage secondaire sur la capacité brute corrigerait le
problème.

**Mais** : ce plancher protège contre une métrique qui écraserait toutes
les autres. Le modifier demande de revalider tout l'arbitrage. À traiter
comme un travail de fond, pas comme un réglage.

### Les SLO secondaires du MI

Le test de significativité (nul par décalage circulaire, préservant
l'autocorrélation) montre que 4 tests sur 6 sont **indistinguables du
bruit** sur ce banc : la latence est pilotée par la géométrie du
déplacement, le CPU et la RAM sont générés indépendamment.

**Le mécanisme n'est pas en cause** — c'est le banc qui n'offre rien à
découvrir. Le corriger demanderait une charge de travail où la contention
des ressources cause réellement la dégradation. À laisser en travaux
futurs.

---

## Récapitulatif

| # | Modification | Fichier | Ligne | Effort | Gain attendu |
|---|---|---|---|---|---|
| 1 | Poids de l'horizon | `services/decision_intelligence/topsis.py` | 251-257 | 10 min | **Le plus fort** |
| 2 | Période du cycle | `infrastructure/Picar/picar_bridge_QoS2.py` (sur le Pi) | 49 | 5 min | Moyen, coûteux |
| 3 | Cooldown | variable d'env. `MIGRATION_COOLDOWN_S` | — | 1 min | Faible à moyen |
| 3b | Marge hystérésis | `services/decision_intelligence/decision.py` | 16 | 1 min | Faible |

**Ordre recommandé** : 1 seule → mesurer → 3 si besoin → mesurer → 2 en
dernier (le plus coûteux).

**Protocole de mesure après chaque changement** : un run fédéré de 3 tours,
même trajectoire qu'UC1, puis `violation_rate.py` et `diagnose_gap.py`.
Comparer aux valeurs de référence : **violation 19,8 %, 21 migrations,
pic de corrélation à 12 s**.
