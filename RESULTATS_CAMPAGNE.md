# Résultats de la campagne — état au 18 août 2026

> **Campagne 4+4 terminée.** Chiffres recalculés le 18/08/2026 sur **8 runs**
> (4 fédérés, 4 ablation). Les 8 contrôles croisés de `diagnose_gap.py`
> passent exactement. Le test de Mann-Whitney exact est **significatif**
> (p = 0,029), la p-valeur minimale atteignable à cette taille d'échantillon.
>
> **Ces valeurs remplacent celles du 17/08 (2+2).** Le papier (`paper.tex`)
> est à jour sur cette base.

---

## 1. Taux de violation — le chiffre central

Seuil de conformité : **latence < 28 ms**. Croisement de la trajectoire
réelle du PiCar avec la VM qui servait réellement à chaque instant.

| Run | Bras | Date | Violation | Échantillons | Durée |
|---|---|---|---|---|---|
| UC1 | fédéré | 13 août | 19,9 % | 315 / 1586 | 52,8 min |
| FED1 | fédéré | 17 août | 25,9 % | 422 / 1627 | 55,7 min |
| FED2 | fédéré | 18 août | 29,3 % | 502 / 1712 | 57,0 min |
| FED3 | fédéré | 18 août | 20,2 % | 656 / 3252 | 108,4 min |
| UC2 | ablation | 14 août | 53,7 % | 652 / 1215 | 40,5 min |
| ABL1 | ablation | 17 août | 52,7 % | 701 / 1331 | 44,3 min |
| ABL2 | ablation | 18 août | 56,6 % | 973 / 1720 | 57,4 min |
| ABL3 | ablation | 18 août | 56,3 % | 1067 / 1894 | 63,1 min |

| Bras | n | Moyenne | Écart-type | Étendue |
|---|---|---|---|---|
| **Fédéré** | 4 | **23,8 %** | 4,6 pt | 19,9 – 29,3 |
| **Ablation** | 4 | **54,8 %** | 1,9 pt | 52,7 – 56,6 |

**Écart : +31,0 points en faveur du fédéré.**

### Le test statistique

```
Mann-Whitney (exact, bilatéral), ablation vs fédéré :
  U = 16,0   p = 0,0286
  (p minimale atteignable avec n=4, m=4 : 0,0286)
  -> SIGNIFICATIF à 5 %
```

**U = 16 signifie que les 4 valeurs ablation dépassent les 4 valeurs
fédérées, sans aucun chevauchement** — le résultat le plus net possible à
cette taille d'échantillon.

---

## 2. Bornes de comparaison

| | Fédéré (8 VMs) | Ablation (4 VMs) |
|---|---|---|
| **Oracle** (meilleure VM à chaque instant) | **7,5 %** | **53,1 %** |
| Système mesuré | 23,8 % | 54,8 % |
| Meilleur placement statique | 81,9 % | 83,2 % |
| VM au hasard | 88,3 % | 88,3 % |
| **Gain capturé** | **78 %** (71–83 % selon le run) | **94 %** (88–97 % selon le run) |

**L'oracle de l'ablation reste à 53,1 %** : un orchestrateur parfait limité
à un provider violerait quand même le contrat plus d'une fois sur deux.
**La fédération déplace l'optimum atteignable de ~46 points** — elle ne rend
pas la décision plus intelligente, elle double la couverture physique.

---

## 3. Violations évitables ou non (moyenne des 4 runs par bras)

| | Ablation | Fédéré |
|---|---|---|
| Inévitable (aucune VM conforme) | 53,2 % | 7,6 % |
| Évitable (une VM conforme existait) | 1,7 % | 16,3 % |

**97 % des violations en ablation sont inévitables**, de façon cohérente sur
les 4 runs (0,7 à 3,6 % évitable). Le système ne se trompe presque jamais ;
il n'a rien de conforme à choisir.

---

## 4. Retard de suivi — confirmé sur 8 exécutions indépendantes

| Décalage | UC1 | FED1 | FED2 | FED3 | UC2 | ABL1 | ABL2 | ABL3 |
|---|---|---|---|---|---|---|---|---|
| 0 s | 83,8 | 76,8 | 73,4 | 83,2 | **96,9** | **94,2** | **90,6** | **94,1** |
| **12 s** | **88,6** | **81,2** | **77,7** | **88,0** | 94,0 | 93,3 | 88,4 | 91,3 |

**Les 4 runs fédérés piquent tous à 12 s. Les 4 runs ablation piquent tous à
0 s.** Reproduction exacte sur 8 exécutions indépendantes — c'est une
propriété du mécanisme (pondération d'horizon `[7,6,...,1]`), pas un artefact
d'une fenêtre de mesure.

---

## 5. Qualité des prédictions ML (8 runs, 12 classeurs)

| Métrique | n | MAE | RMSE | RMSE/moy |
|---|---|---|---|---|
| latency | 23 421 | 3,99 ms | 6,72 ms | 8,2 % |
| cpu_usage | 24 949 | 7,34 % | 9,94 % | 18,9 % |
| ram_usage | 24 477 | 6,45 % | 9,28 % | 17,4 % |

Disponibilité du GRU : **95,2 %** (72 871 / 76 572).

**La persistance naïve bat toujours le GRU sur la latence** (RMSE 4,07 ms vs
6,72 ms) — résultat négatif confirmé sur l'ensemble de la campagne.

---

## 6. Ce qui n'a PAS généralisé — et a été retiré du papier

L'écart entre FED1 (25,9 %) et UC1 (19,9 %) avait été attribué le 17/08 à une
excursion vers `cloud1` (22 cycles, non conforme par construction). **Sur les
4 runs fédérés, cette explication ne tient plus** : FED2 (29,3 %, le run le
plus mauvais) n'a **jamais** touché `cloud1`. Le papier ne revendique plus de
cause unique pour la dispersion inter-runs fédérée — c'est rapporté comme
variance non caractérisée, honnêtement.

---

## 7. Contrôle positif du mécanisme d'information mutuelle

Nouveau (18/08) : `scripts/analyse/mi_positive_control.py`. Sur données
synthétiques à autocorrélation réaliste (CPU ~0,90, latence ~0,99) :

| Scénario | p | Verdict |
|---|---|---|
| Dépendance construite et connue | **0,000** | détecté |
| Indépendant (comme le vrai testbed) | **0,444** | rien détecté, pas de faux positif |

**Confirme que l'estimateur et le test à décalage circulaire fonctionnent
correctement** — l'absence de signal sur le testbed réel reflète le
générateur de charge (CPU/RAM et latence indépendants par construction), pas
une limite de la méthode. Intégré en §VII-B du papier.

---

## 8. UC5 — traduction des intentions (inchangé depuis le 17/08)

12 phrases d'un document tiers, 10 traduites, 2 rejets hors-domaine corrects.
Voir `data_UC5_intentions/`.

## 9. Correction des seuils ancrés — mesurée (18/08)

`scripts/analyse/bench_grounded.py`, appels réels au LLM, comparaison
appariée avant/après sur les 12 intentions :

| Intention | Avant | Après |
|---|---|---|
| #03 latence minimale | 50 ms | **23,7 ms** (< 28 ms, le défaut système) |
| #17 accepte plus de latence | 200 ms | 137 ms (relâchement préservé) |
| Rejets hors-domaine | 2 | 2 (inchangé) |

Déterministe (3 tirages identiques). Intégré en §VII-A du papier, drapeau
`INTENT_GROUNDED_THRESHOLDS` désactivé par défaut.

---

## 10. Pièges rencontrés pendant la campagne

**La colonne.** `VM active (source)`, jamais `VM hôte (fédération)` — voir
`scripts/analyse/README.md`.

**L'alignement des dates.** Trois décalages de jour testés, on ne garde que
celui dans la fenêtre du run — corrigé le 17/08, toujours actif.

**Le service reste où il était.** kubectl garde la trace entre les runs ; un
ablation lancé après un fédéré qui s'est terminé chez l'autre provider laisse
le hub en STANDBY sans erreur visible. Voir `CAMPAGNE_17-08.md`. Rencontré
2 fois pendant la campagne du 18/08 (avant FED3→ABL3, et un cas où la
commande de migration a été copiée avec un placeholder littéral au lieu du
nom de VM réel — toujours vérifier `active_vm` après `/migrate`, pas
seulement le code retour).

**Réentraînement des API ML.** Contamine silencieusement un run en cours
(bascule sur `last_value_fallback`). Vérifier les 3 ports avant **et**
pendant un lancement si le comportement semble anormal.

---

## 11. Ce qui reste pour le papier

| | Quoi | Statut |
|---|---|---|
| 1 | Compiler et relire le PDF final | à faire par l'utilisateur |
| 2 | Emails des 5 auteurs (affiliations déjà remplies) | Yangui |
| 3 | Accord de citation *From Policies to Intent* | Yangui / Lahyani |
| 4 | Longueur du papier (15 pages) vs format cible | dépend de la conférence choisie par Yangui |

La campagne expérimentale est **terminée**. Tout ce qui reste est éditorial
ou dépend des encadrants.
