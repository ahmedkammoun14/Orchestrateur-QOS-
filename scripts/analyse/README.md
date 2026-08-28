# Scripts d'analyse — évaluation du papier

Tous se lancent depuis la **racine du dépôt**, pas depuis ce dossier :

```bash
python scripts/analyse/violation_rate.py
```

Ils lisent les archives `data_UC*/` et n'écrivent rien, sauf
`isolate_session.py` et `gen_figures.py`.

---

## ⚠️ Le piège à connaître

Dans les fichiers `timings_autonomous_*.xlsx`, deux colonnes désignent une VM :

| Colonne | Source | À utiliser ? |
|---|---|---|
| `VM active (source)` | `state.service_vm` — suivi fin | ✅ **oui** |
| `VM hôte (fédération)` | `state.hosting_vm` — vient de kubectl | ❌ non |

kubectl résout au **nœud**, pas à la VM : `edge1`, `edge1b` et `edge1c`
partagent `pop1-worker-1`, donc il renvoie toujours la canonique `edge1`.
En UC2, `VM hôte (fédération)` vaut `edge1` sur **100 %** des cycles alors
que le service circulait sur quatre VMs différentes.

**Utiliser la mauvaise colonne donne 82,3 % de violation au lieu de 53,7 %.**

---

## Les scripts

| Script | Rôle | Sortie |
|---|---|---|
| `isolate_session.py` | Isole la dernière session dans `latences.csv` (append-only, plusieurs jours mélangés, pas de colonne de date). Segmente par continuité temporelle en partant de la fin. | écrit `latences_session.csv` |
| `violation_rate.py` | Taux de violation, en croisant la trajectoire réelle et la VM réellement hôte. **Le chiffre central du papier.** | console |
| `oracle_bound.py` | Bornes : oracle, placement statique par VM, VM au hasard, pire cas. | console |
| `diagnose_gap.py` | Violations évitables vs inévitables, et mesure du retard sur l'optimum par corrélation décalée. Contient un **contrôle croisé** qui refuse de tourner si le taux ne correspond pas à `violation_rate.py`. | console |
| `mae_rmse.py` | MAE/RMSE des prédictions, séparés par niveau de cascade (GRU / point\_model / persistance). Aligne correctement prédiction du cycle N et mesure du cycle suivant. | console |
| `mi_test2.py` | Test de significativité du MI avec un nul **préservant l'autocorrélation** (décalage circulaire). Une permutation naïve serait anti-conservatrice ici. | console |
| `floor_freq.py` | Fréquence à laquelle le plancher du Gap Grade est atteint. | console |
| `gen_figures.py` | Génère `paper/fig4_trajectory.tex` et `paper/fig5_latency.tex`. | écrit 2 `.tex` |
| `checkrefs.py` | Labels dupliqués et références orphelines dans le `.tex`. | console |
| `checkcite.py` | Cohérence citations ↔ entrées bibliographiques. | console |

---

## ⚠️ Le second piège — l'alignement des dates

`latences.csv` ne porte qu'une **heure** (`HH:MM:SS`), sans date, en heure
locale du Pi (UTC+2) ; les fichiers de temps sont en UTC avec date complète.

Jusqu'au 17/08/2026, les scripts ajoutaient un jour dès qu'un échantillon
tombait plus de 10 min avant le début de la chronologie, pour gérer un
passage de minuit. Quand le **PiCar démarre avant l'orchestrateur** — 21 min
d'avance sur le run `data_ABL_run1` — cette règle projetait ces échantillons
au lendemain, où tous les événements leur étaient antérieurs : ils héritaient
donc de la **dernière VM du run**. ~350 échantillons faussement attribués.

Règle appliquée depuis : on essaie les trois décalages (−1, 0, +1 jour) et on
ne retient que celui qui tombe **dans la fenêtre** couverte par la
chronologie. Aucun candidat valable → l'échantillon est écarté, jamais deviné.

`violation_rate.py`, `oracle_bound.py` et `diagnose_gap.py` partagent
désormais cette logique. Les quatre contrôles croisés de `diagnose_gap.py`
doivent passer exactement ; s'ils divergent, c'est que l'alignement a changé.

---

## Ordre après une nouvelle campagne

```bash
# 1. isoler la trajectoire de CHAQUE run
python scripts/analyse/isolate_session.py data_UC1_federe/latences.csv
python scripts/analyse/isolate_session.py data_FED_run1/latences.csv
python scripts/analyse/isolate_session.py data_UC2_ablation/latences.csv
python scripts/analyse/isolate_session.py data_ABL_run1/latences.csv

# 2. le résultat principal
python scripts/analyse/violation_rate.py

# 3. les bornes et le diagnostic
python scripts/analyse/oracle_bound.py
python scripts/analyse/diagnose_gap.py

# 4. précision ML et test MI
python scripts/analyse/mae_rmse.py
python scripts/analyse/mi_test2.py data_UC1_federe/qos_history_provider1.xlsx 28

# 5. régénérer les figures
python scripts/analyse/gen_figures.py

# 6. vérifier le papier
python scripts/analyse/checkrefs.py paper/paper.tex
python scripts/analyse/checkcite.py paper/paper.tex paper/references.bib
```

⚠️ Les runs sont **listés en tête de fichier** dans `violation_rate.py`
(`RUNS`), `oracle_bound.py` (`RUNS`), `diagnose_gap.py` (appels `run(...)` en
bas), `mae_rmse.py` (`SOURCES`) et `gen_figures.py`. Ajouter un run =
ajouter une ligne dans chacun.

**Chaîne de dépendance à respecter** : `violation_rate.py` produit les
pourcentages ; `oracle_bound.py` les **recopie** dans sa table `RUNS` et
`diagnose_gap.py` les **recopie** comme valeurs de contrôle. Les trois
doivent être mis à jour ensemble, sinon `diagnose_gap.py` s'arrête de
lui-même sur `DIVERGENCE`.

Valeurs de contrôle au 17/08/2026 (4 runs) :

| Run | Violations / échantillons | Taux |
|---|---|---|
| `data_UC1_federe` | 315 / 1586 | 19,9 % |
| `data_FED_run1` | 422 / 1627 | 25,9 % |
| `data_UC2_ablation` | 652 / 1215 | 53,7 % |
| `data_ABL_run1` | 701 / 1331 | 52,7 % |

Résultats consolidés : [`RESULTATS_CAMPAGNE.md`](../../RESULTATS_CAMPAGNE.md).

## Dépendances

`openpyxl`, `numpy`, `scikit-learn` (pour `mi_test2.py` uniquement).
