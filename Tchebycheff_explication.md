# Gap Grade et Tchebycheff — explication chiffrée

> **Objet** : distinguer clairement `compute_gap_grade` (la **fonction**) de la
> fonction de **Tchebycheff** (la **méthode d'agrégation** qu'elle utilise), et
> démontrer par les chiffres pourquoi cette méthode a été retenue.
>
> Code de référence : `hub/provider_arbitration.py:310-403`
> Plan associé : `PLAN_ARBITRAGE_FEDERE.md` §4 (décisions Q1, Q2, Q7, Q9)

---

## 1. La distinction en une image

```
compute_gap_grade()  ─── NOTRE fonction, en 5 étapes
│
├─ ① filtrer les SLOs PRIMAIRES              ← notre règle métier (Q2)
├─ ② calculer les écarts signés δ = (v−τ)/τ  ← notre normalisation (Q1)
├─ ③ appliquer le plancher δ ≥ −1            ← notre correctif (Q7)
├─ ④ normaliser les poids (Σw = 1)           ← contrainte technique
│
└─ ⑤ AGRÉGER  →  ★ TCHEBYCHEFF ★             ← méthode de la littérature (Q9)
```

**4 étapes sur 5 sont propres au projet. Une seule est « Tchebycheff ».**

Dans le code, cela se traduit par **une seule ligne** :

```python
# hub/provider_arbitration.py
def compute_gap_grade(slos, values, rho=GAP_GRADE_RHO):
    # lignes 369-387  → ① filtrage des primaires
    # ligne  389      → ② écarts signés + plancher
    # lignes 390-400  → ④ normalisation des poids
    # ligne  402      → préparation des termes wᵢ·δᵢ

    return (max(terms) + rho * sum(terms)) / (1 + rho)   # ← ★ CECI est Tchebycheff
```

---

## 2. Racine mathématique — pourquoi ce nom

Tchebycheff vient de la **norme de Tchebychev** (norme L∞), c'est-à-dire le
**maximum**. Le choix de la norme détermine entièrement le comportement :

| Norme | Formule | Nom courant | Comportement |
|---|---|---|---|
| **L¹** | `Σ \|xᵢ\|` | somme pondérée | ⚠️ **compensatoire** — un bon critère rachète un mauvais |
| **L∞** | `max \|xᵢ\|` | Tchebychev | ✅ **non compensatoire** — le pire critère décide |

« **Augmenté** » (*augmented weighted Tchebycheff*, Steuer & Choo, 1983) désigne
l'ajout du petit terme `ρ·Σ`, qui sert **uniquement à départager** deux
alternatives dont le pire critère est identique — sans redonner de pouvoir
compensatoire.

---

## 3. Le décor de l'exemple

**Intention** (mode enhanced) — le LLM produit 3 SLOs :

| Métrique | Opérateur | Seuil | Poids | `is_primary` |
|---|---|---|---|:---:|
| `latency` | `<` | **35 ms** | **0.6** | ✅ |
| `cpu_usage` | `>=` | **2.5 cœurs** | **0.4** | ✅ |
| `ram_usage` | `>=` | 1.5 Go | 0.25 | ❌ *(secondaire, MI)* |

**Deux champions à comparer** — les deux **violent** la latence :

| VM | Provider | Latence | CPU dispo | RAM dispo |
|---|---|---|---|---|
| `edge1b` | P1 | **38 ms** ❌ | 3.8 cœurs | 2.0 Go |
| `cloud1` | P1 | **50 ms** ❌❌ | **12.8 cœurs** | 24 Go |

---

## 4. Étapes ① à ④ — la partie « compute_gap_grade »

> Ces 4 étapes sont **identiques quelle que soit la méthode d'agrégation**.

### ① Filtrage des primaires (règle Q2)

```
latency    ✅ retenu
cpu_usage  ✅ retenu
ram_usage  ❌ ÉCARTÉ  →  secondaire, il ne pèse jamais sur le Gap Grade
```

### ② Écarts signés `δ = (v − τ)/τ`

| | `edge1b` | `cloud1` |
|---|---|---|
| `δ_lat` | `(38−35)/35` = **+0.0857** | `(50−35)/35` = **+0.4286** |
| `δ_cpu` | `(2.5−3.8)/2.5` = **−0.5200** | `(2.5−12.8)/2.5` = **−4.1200** |

### ③ Plancher `δ ≥ −1` (règle Q7)

| | `edge1b` | `cloud1` |
|---|---|---|
| `δ_cpu` | −0.52 → **−0.52** *(inchangé)* | −4.12 → **−1.00** ⬅️ **le plancher agit** |

### ④ Normalisation des poids

```
Σw = 0.6 + 0.4 = 1.0     →     w_lat = 0.6   ·   w_cpu = 0.4
```

### Les termes pondérés `wᵢ · δᵢ`

| | `w_lat · δ_lat` | `w_cpu · δ_cpu` |
|---|---|---|
| **`edge1b`** | `0.6 × (+0.0857)` = **+0.0514** | `0.4 × (−0.52)` = **−0.2080** |
| **`cloud1`** | `0.6 × (+0.4286)` = **+0.2571** | `0.4 × (−1.00)` = **−0.4000** |

> Tout ce qui précède est propre au projet. Ce qui suit est le **choix de méthode**.

---

## 5. Étape ⑤ — l'agrégation : trois variantes comparées

### Variante A — Somme pondérée, **sans** plancher *(état d'origine)*

```
edge1b :  +0.0514 + 0.4×(−0.52)   =  +0.0514 − 0.2080  =  −0.1566
cloud1 :  +0.2571 + 0.4×(−4.12)   =  +0.2571 − 1.6480  =  −1.3909
```

| VM | Résultat | |
|---|---|---|
| `edge1b` | −0.1566 | |
| **`cloud1`** | **−1.3909** | 🏆 **gagne** |

**Deux défauts majeurs :**
- `cloud1` (50 ms) est déclarée **8× meilleure** qu'`edge1b` (38 ms) — absurde.
- Les deux scores sont **négatifs** → elles paraissent **conformes** alors
  qu'elles violent toutes les deux.

### Variante B — Somme pondérée, **avec** plancher

```
edge1b :  +0.0514 − 0.2080  =  −0.1566
cloud1 :  +0.2571 − 0.4000  =  −0.1429
```

| VM | Résultat | |
|---|---|---|
| **`edge1b`** | **−0.1566** | 🏆 gagne |
| `cloud1` | −0.1429 | |

**Bonne réponse, mais deux réserves :**
- Écart = **0.0137**, soit **4× plus petit que le dead-band (0.05)** →
  l'arbitre serait **incapable de trancher**.
- Les deux scores restent **négatifs** → toujours le faux signal « conforme ».

### Variante C — **Tchebycheff augmenté** avec plancher *(retenue)*

```
edge1b :  max(+0.0514, −0.2080) = +0.0514
          Σ = +0.0514 − 0.2080  = −0.1566
          G = (+0.0514 + 0.1×(−0.1566)) / 1.1
            = (+0.0514 − 0.0157) / 1.1  =  +0.0325

cloud1 :  max(+0.2571, −0.4000) = +0.2571
          Σ = +0.2571 − 0.4000  = −0.1429
          G = (+0.2571 + 0.1×(−0.1429)) / 1.1
            = (+0.2571 − 0.0143) / 1.1  =  +0.2208
```

| VM | **G** | |
|---|---|---|
| **`edge1b`** | **+0.0325** | 🏆 **gagne** |
| `cloud1` | **+0.2208** | |

**Deux propriétés obtenues :**
- Écart **0.1883** — **4× plus grand que le dead-band** → décision **nette**.
- Les deux `G` sont **positifs** → signal correct : **ces deux VMs violent**.

---

### ⚠️ Précision essentielle — ce classement ne déclenche PAS de migration

Dans cet exemple, **les deux VMs violent le SLO de latence** (38 ms et 50 ms
contre un seuil de 35 ms). C'est d'ailleurs ce que disent les résultats :
**les deux `G` sont POSITIFS** (`+0.0325` et `+0.2208`), et en Tchebycheff
`G > 0` signifie **violation**.

Or le projet est configuré en **`SLO_ENFORCEMENT = "hard"`** (décision Q10) :

```
⓪ FILTRE de l'arbitre :  is_compliant == true
   edge1b  →  is_compliant = false  →  ❌ ÉCARTÉ
   cloud1  →  is_compliant = false  →  ❌ ÉCARTÉ
   →  aucun bid ne survit au filtre
```

**Verdict réel du système : STAY + ALERTE — chemin C (INFAISABLE).**

Le classement `edge1b` > `cloud1` sert malgré tout à **deux** choses :

| Usage | Détail |
|---|---|
| **① Contenu de l'alerte** | permet d'annoncer *« meilleure offre : `edge1b` — 38 ms »* au lieu de citer `cloud1` à 50 ms. C'est la décision **Q10** : on **calcule** le best-effort, on **refuse** de l'élire |
| **② Mode `soft`** *(si activé un jour)* | `edge1b` serait alors réellement choisie pour la migration |

> Cet exemple démontre le **comportement de la formule**, pas le verdict final
> du système. Pour un cas où le classement mène à une **vraie migration**,
> voir §6 bis.

---

## 6. Le même calcul, mais avec des VMs CONFORMES

Mêmes ingrédients, seuil de latence porté à **65 ms** (cas E7 du plan) :

| VM | Latence | Seuil | Conforme ? | CPU dispo |
|---|---|---|:---:|---|
| `cloud1` | 60 ms | < 65 | ✅ **OUI** | 12.8 cœurs |
| `edge2` | 32 ms | < 65 | ✅ **OUI** | 3.2 cœurs |

```
cloud1 :  δ_lat = (60−65)/65 = −0.0769   ·   δ_cpu = −4.12 → plancher −1.00
          termes = [ −0.0462 , −0.4000 ]
          max = −0.0462  ·  Σ = −0.4462
          G = (−0.0462 + 0.1×(−0.4462)) / 1.1  =  −0.0826

edge2  :  δ_lat = (32−65)/65 = −0.5077   ·   δ_cpu = −0.28
          termes = [ −0.3046 , −0.1120 ]
          max = −0.1120  ·  Σ = −0.4166
          G = (−0.1120 + 0.1×(−0.4166)) / 1.1  =  −0.1397
```

| VM | **G** | `is_compliant` | |
|---|---|:---:|---|
| **`edge2`** | **−0.1397** | ✅ | 🏆 **ÉLU → migration réelle** |
| `cloud1` | −0.0826 | ✅ | |

**Interprétation** : `edge2` gagne parce que son **maillon le plus faible** (le
CPU, 28 % de marge) est **3,6× plus solide** que celui de `cloud1` (la latence,
7,7 % de marge). Sur un robot mobile, cette marge est le **temps avant la
prochaine violation** : `cloud1` casserait après ≈ 2 unités de déplacement,
`edge2` après ≈ 17,5 — **8× plus longtemps**.

### La règle à retenir

| Signe de `G` | Signification | En mode `hard` |
|:---:|---|---|
| **`G < 0`** | conforme, avec marge | ✅ **éligible** → migration possible |
| **`G > 0`** | **viole** le contrat | ❌ **écarté** → sert uniquement à l'alerte |

---

## 7. Bilan comparatif

| | Étapes ①-④ | **Étape ⑤** | Gagnant | Écart | Signe correct |
|---|:---:|---|---|---|:---:|
| **A** Somme sans plancher | identiques | `Σ(wᵢδᵢ)` | ❌ `cloud1` | — | ❌ |
| **B** Somme avec plancher | identiques | `Σ(wᵢδᵢ)` | ✅ `edge1b` | **0.014** ⚠️ | ❌ |
| **C** **Tchebycheff** | identiques | `(max + ρΣ)/(1+ρ)` | ✅ **`edge1b`** | **0.188** ✅ | ✅ |

---

## 8. Ce que l'exemple démontre

### 8.1 La différence est localisée dans une seule ligne

```
compute_gap_grade  =  ①filtrer + ②signer + ③borner + ④pondérer  +  ⑤AGRÉGER
                      └────────── identique dans A, B et C ──────┘    └──┬──┘
                                                                        │
                                                    seul endroit où
                                                    Tchebycheff existe
```

### 8.2 Le `max()` efface littéralement le CPU excédentaire

```
cloud1 :  max( +0.2571 , −0.4000 )  =  +0.2571
                          └────┬───┘
              ses 12.8 cœurs n'apparaissent JAMAIS dans le résultat.
              Ils ne peuvent structurellement rien racheter.
```

Alors que la somme les fait entrer directement dans le total (`−0.4` ou `−1.648`).

### 8.3 Chaque ingrédient a son rôle — aucun n'est superflu

| Ingrédient | Sans lui |
|---|---|
| **Plancher** (③) | `cloud1` gagne avec `−1.3909` — **désastre** |
| **Tchebycheff** (⑤) | écart de **0.014**, sous le dead-band → **arbitrage impossible** |
| **Les deux** | écart de **0.188** + signe correct → **décision nette et lisible** |

---

## 9. Réponse synthétique

| | Ce que c'est | Dans l'exemple |
|---|---|---|
| **`compute_gap_grade`** | la **fonction complète** : filtre, signe, borne, pondère, **puis agrège** | tout le parcours ① → ⑤, qui produit `+0.0325` |
| **Tchebycheff** | la **méthode d'agrégation** utilisée à l'étape ⑤ | uniquement `(max + ρΣ)/(1+ρ)` — la variante **C** |

> Changer Tchebycheff en somme pondérée revient à passer de la variante **C** à
> la variante **B**. Les étapes ①-④ ne bougent pas, le bid ne bouge pas,
> l'arbitre ne bouge pas. **Une seule ligne de code — et un résultat qui bascule.**

---

## 10. Cas particulier : le mode autonome

Avec **un seul** SLO primaire (latence, `w = 1.0` après normalisation) :

$$G = \frac{\delta + \rho\delta}{1+\rho} = \frac{\delta(1+\rho)}{1+\rho} = \delta$$

**Le Tchebycheff est totalement transparent en mode autonome** : `G` vaut
exactement `δ`. C'est la preuve de non-régression du comportement historique —
`max()` d'une liste à un seul élément vaut cet élément, et le terme
d'augmentation est annulé par la division.

Exemples :

| Champion | Latence (τ = 40 ms) | Calcul | **G** | Lecture |
|---|---|---|---|---|
| `edge1b` | 22 ms | `(22−40)/40` | **−0.45** | conforme, 45 % de marge |
| `edge1` | 46 ms | `(46−40)/40` | **+0.15** | viole de 15 % |
| `cloud1` | 95 ms | `(95−40)/40` | **+1.375** | viole de 137 % |

---

## 11. Formulation pour la soutenance

> *« Le **Gap Grade** est la grandeur d'arbitrage inter-provider de notre
> architecture : une déviation normalisée par les seuils SLO, donc absolue et
> comparable entre providers. Son **agrégation** repose sur une fonction de
> **Tchebycheff augmentée**, choisie pour sa propriété **non compensatoire** :
> dans une infrastructure hétérogène, une somme pondérée permettrait à
> l'excédent de ressources d'un cloud de racheter sa violation de latence. »*

**Gap Grade = le QUOI** *(la grandeur qu'on transporte)*
**Tchebycheff = le COMMENT** *(la manière de la calculer)*

---

## Références

- Hwang & Yoon (1981) — TOPSIS.
- Charnes & Cooper (1961) — Goal Programming, déviations pondérées normalisées.
- Wierzbicki (années 1980) — méthode du point de référence, *achievement
  scalarizing functions*.
- Steuer & Choo (1983) — *augmented weighted Tchebycheff*.

*Références à vérifier avant citation dans le mémoire.*
