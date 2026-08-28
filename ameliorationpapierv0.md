# Plan d'amélioration — `paper_v0`

Évaluation externe reçue : **7,5/10** (structure 8,5 · contenu 8 · évaluation 6,5).
Mon avis : la note est juste, peut-être un demi-point généreuse — deux
faiblesses touchent les **contributions revendiquées**, pas seulement
l'expérimentation, et l'évaluation externe ne les a pas relevées.

| Axe | Actuel | Cible réaliste | Levier principal |
|---|---|---|---|
| Structure | 8,5 | **9,5** | alléger §V, nettoyer les marqueurs |
| Contenu | 8 | **9** | reformuler C4, traiter le plancher du Gap Grade |
| Évaluation | 6,5 | **8,5** | **répéter chaque condition** |
| **Global** | **7,5** | **≈ 8,8** | |

---

# PRIORITÉ 1 — Évaluation : 6,5 → 8,5

> C'est ici que se joue la catégorie de l'article. Les deux autres axes sont
> déjà bons ; celui-ci plafonne l'ensemble.

## 1.1 Répéter chaque condition — LE point décisif

**Problème.** Une seule exécution par condition. Les résultats sont des
proportions sur une fenêtre unique, pas des moyennes avec variance. Aucun
reviewer de rang A n'accepte ça, et le papier l'admet lui-même (§VII.E).

**Action.**
- **3 runs indépendants** de la condition fédérée (UC1)
- **3 runs indépendants** de la condition ablation (UC2)
- Même trajectoire, mêmes paramètres, redémarrage complet entre chaque

**Coût.** ~40 min par run × 6 = **4 h de manipulation**, plus le temps de
recalcul (quelques minutes, les scripts existent déjà).

**Ce que ça change dans le papier.**
- Table `tab:violation` : remplacer chaque valeur par **moyenne ± écart-type**
- §VI.B : remplacer « une baisse de 33,9 points » par l'écart moyen et son
  intervalle
- §VII.E : **supprimer** le paragraphe « One run per condition » — la menace
  disparaît
- Ajouter un test statistique valide (Mann-Whitney sur les 3+3 runs, plutôt
  qu'un test de proportion sur des échantillons autocorrélés)

**Gain estimé : +1,5 à +2 points sur l'axe évaluation.** C'est le meilleur
rapport effort/résultat de tout ce document.

## 1.2 Fédération distribuée sur deux machines

**Problème.** Les deux relais tournent sur le même hôte. Les temps de relais
rapportés sont des temps de boucle locale, et le mot « distribué » n'est pas
démontré.

**Action.** Lancer provider-1 sur une machine, provider-2 sur une autre, en
ne changeant que `PROVIDER_RELAY_URLS`. Un seul run suffit pour lever la
critique : il ne s'agit pas de refaire l'évaluation, mais de **prouver que le
chemin réseau fonctionne**.

**Coût.** ~1 h au LAAS.

**Ce que ça change.** §VII.E : « co-located, never exercised » devient
« exercised on two hosts, relay latency measured at X ms ». Une menace à la
validité supprimée, et un chiffre de plus.

## 1.3 Comparaison à un système existant

**Problème non relevé par l'évaluation externe.** Vous vous comparez à un
oracle, à un placement statique et à votre propre ablation. Méthodologiquement
propre — mais un reviewer demandera : *« et par rapport à Sfaxi et al., à
QONNECT, ou à une approche par apprentissage par renforcement ? »*

**Action réaliste.** Ne pas réimplémenter un système tiers (trop coûteux).
Ajouter plutôt une **baseline réactive** : même code, mais décision sur la
valeur *mesurée* au lieu de la valeur *prédite*. C'est la comparaison qui
valide directement l'argument « proactif » de l'Introduction, et elle ne
demande qu'un paramètre.

**Coût.** ~40 min de run + une ligne de configuration.

**Gain.** Répond à la question « à quoi sert la prédiction ? », actuellement
sans réponse expérimentale dans le papier.

---

# PRIORITÉ 2 — Contenu : 8 → 9

## 2.1 Reformuler la contribution C4 — correction la plus urgente

**Problème.** L'Introduction annonce la découverte auto-supervisée d'objectifs
secondaires comme **contribution acquise**, et la §VII.B démontre qu'elle ne
produit aucun signal sur ce banc. Un reviewer écrira : *« les auteurs
revendiquent un mécanisme qu'ils montrent eux-mêmes inopérant »*.

**Action.** Réécrire C4 dans l'Introduction pour annoncer ce qui est
réellement démontré : un mécanisme **proposé, implémenté et testé**, dont
l'évaluation sur ce banc est négative pour une raison identifiée. Le passage
de « nous apportons X » à « nous proposons X et montrons dans quelles
conditions il ne s'applique pas » supprime entièrement la critique.

**Coût : 15 minutes.** C'est la modification la plus rentable du document.

## 2.2 Traiter le plancher du Gap Grade

**Problème non relevé par l'évaluation externe.** Le plancher `DELTA_FLOOR =
-1.0` rend indiscernables deux cibles qui dépassent toutes deux le double de
leur seuil, et le tenant l'emporte alors par défaut (§VII.A). Ce n'est pas une
limite périphérique : **c'est un défaut dans le mécanisme central** que
l'article revendique (C1/C2).

**Deux options.**

- **Option courte (papier actuel)** : renforcer §VII.A en quantifiant la
  fréquence du cas sur vos données — combien de cycles ont vu deux Gap Grades
  égaux ? Si c'est rare, la limite se relativise d'elle-même.
- **Option longue (version suivante)** : remplacer le plancher dur par une
  saturation douce, ou ajouter un départage secondaire sur la capacité
  résiduelle brute. Invalide les runs → à faire avec la campagne de répétition.

**Recommandation :** option courte maintenant, option longue en même temps que
la priorité 1.1.

## 2.3 Renforcer la provenance des intentions

**Problème.** 2 intentions sur infrastructure réelle, 6 en local — et les
trois répétitions qui portent la mesure de reproductibilité sont **toutes
locales**. La contribution C3 est moins étayée que le texte ne le suggère.

**Action.** Lors de la campagne de la priorité 1.1, **refaire les 6 intentions
sur l'infrastructure réelle**. Coût marginal : ~25 min, puisque vous serez
déjà en train de lancer des runs.

**Gain.** La §VI.E cesse de reposer sur un déploiement local, et la §VII.E
perd sa cinquième menace.

## 2.4 Compléter les références

**Problème relevé par l'évaluation externe.** Les mentions `VENUE TO VERIFY`
et `JOURNAL TO VERIFY` apparaissent dans le PDF compilé. Dans un brouillon
partagé, ça donne une impression d'inachevé.

**Action.** Vérifier les 13 entrées marquées `% CHECK` dans
`paper/references.bib`. Quatre demandent une information que vous seul pouvez
obtenir :
- `sfaxi2025aidriven` — venue, année, pages
- `rosmaninho2024` — venue et année
- `metsch2023` — journal, volume, pages
- `sfcmultidomain2023` — auteurs

Pour les trois papiers du groupe de Sfaxi, demandez directement à Yangui ou
Lahyani.

**À faire aussi :** obtenir leur accord pour citer *From Policies to Intent*,
actuellement référencé comme `@unpublished`.

---

# PRIORITÉ 3 — Structure : 8,5 → 9,5

## 3.1 Alléger la Section V

**Problème relevé par l'évaluation externe.** La section fait ~2,5 pages et
sa densité fatigue. Elle contient 7 équations et 4 tables.

**Action.** Deux candidats à la compression, sans perte d'argument :
- Les quatre phases de TOPSIS (§V.D) peuvent être condensées : les
  équations (4) et (5) sont standard et pourraient tenir en une phrase avec
  renvoi à Hwang & Yoon.
- Le paragraphe « What a wrong score can and cannot do » (§V.C) fait doublon
  partiel avec §VII.B — garder l'un des deux, renvoyer depuis l'autre.

**Gain :** ~0,5 page libérée, qui servira aux barres d'erreur de la
priorité 1.1.

## 3.2 Nettoyer les marqueurs de brouillon

Avant toute diffusion, y compris à vos encadrants :

```latex
% Supprimer ces deux lignes du préambule
\newcommand{\todo}[1]{...}
\newcommand{\refcheck}[1]{...}
```

Il reste **4 marqueurs `\todo`** — tous des affiliations et emails.
Ils doivent disparaître avant la première relecture externe.

## 3.3 Figures manquantes

Le papier a 3 figures TikZ (architecture, cycle, deux échelles). Deux
manquent et renforceraient l'évaluation :

- **Trajectoire du véhicule** avec les zones de couverture des 8 cibles —
  rend visible d'un coup d'œil pourquoi 19,8 % de violation est bon. Les
  données existent (`latences_session.csv`, colonnes `x_cm`, `y_cm`).
- **Latence au cours du tour**, fédéré vs ablation vs statique, avec la ligne
  du seuil à 28 ms. C'est la figure qui *montre* le résultat au lieu de le
  tabuler.

**Coût :** je peux les générer en TikZ à partir des données existantes.

---

# Outillage à conserver

Les scripts d'analyse écrits pendant cette session vivent dans un dossier
temporaire et **seront perdus**. Les déplacer dans le dépôt, par exemple sous
`scripts/analyse/` :

| Script | Rôle |
|---|---|
| `isolate_session.py` | Isole une session dans `latences.csv` (append-only, plusieurs jours) |
| `violation_rate.py` | Taux de violation en croisant trajectoire et VM hôte |
| `oracle_bound.py` | Bornes oracle / statique / VM au hasard |
| `diagnose_gap.py` | Violations évitables vs inévitables, mesure du retard |
| `mae_rmse.py` | MAE/RMSE par niveau de cascade ML |
| `mi_test2.py` | Test de significativité MI (nul par décalage circulaire) |

Sans eux, la campagne de répétition demanderait de tout réécrire.

⚠️ **Rappel critique** dans `violation_rate.py` : utiliser la colonne
`VM active (source)`, **jamais** `VM hôte (fédération)`. Cette dernière vient
de kubectl, qui résout au nœud et renvoie la VM canonique — elle vaut `edge1`
sur 100 % des cycles en UC2. L'erreur donne 82,3 % au lieu de 53,7 %.

---

# Ordre d'exécution recommandé

| # | Action | Coût | Gain |
|---|---|---|---|
| 1 | Reformuler C4 dans l'Introduction | 15 min | **Élevé** |
| 2 | Compléter les références + accord de citation | 1 h | Moyen |
| 3 | Nettoyer les marqueurs de brouillon | 5 min | Moyen |
| 4 | **Campagne de répétition (3+3 runs) + 6 intentions réelles** | **5 h** | **Décisif** |
| 5 | Fédération sur deux machines | 1 h | Élevé |
| 6 | Baseline réactive | 40 min | Élevé |
| 7 | Recalculer et réécrire §VI avec moyennes ± écarts-types | 2 h | Décisif |
| 8 | Alléger §V, ajouter les 2 figures | 2 h | Moyen |

**Les points 1 à 3 se font aujourd'hui, sans relancer quoi que ce soit.**
Les points 4 à 7 forment une seule journée de travail et font passer
l'article de « acceptable en workshop » à « défendable en conférence ».

---

# Ce qu'il ne faut PAS changer

- **La Section VII.** L'honnêteté sur les résultats négatifs est le point le
  plus remarqué par l'évaluation externe. Ne pas la diluer.
- **Le résultat négatif du MI.** Le reformuler dans l'Introduction (2.1),
  jamais le retirer de l'évaluation.
- **La comparaison à l'oracle.** C'est ce qui transforme « 19,8 % de
  violation » en « 83,6 % de l'amélioration atteignable ». Peu d'articles du
  domaine le font.
