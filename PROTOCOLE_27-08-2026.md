# Protocole — dernière journée au LAAS (27/08/2026)

> **Contrainte :** dernier accès à l'infrastructure. Après cette journée, plus
> aucune donnée physique ne pourra être collectée. Le plan est donc conçu pour
> ne **jamais** finir la journée les mains vides : la réplication (phase 2) est
> garantie quelle que soit l'issue de l'expérience (phase 1).

## Adresses

| Rôle | Adresse | Ce qui y tourne |
|---|---|---|
| Machine edge 1 | `194.199.113.18` | agents `edge1`, `edge1b`, `edge1c` |
| Machine edge 2 | `194.199.113.28` | agents `edge2`, `edge2b`, `edge2c` |
| cloud 1 | `194.199.113.66` | agent `cloud1` |
| cloud 2 | `194.199.113.69` | agent `cloud2` |
| Master | `194.199.113.8` | `openstack_client` (port 8024) |
| Raspberry Pi | `140.93.64.105` | pont PiCar + `latences.csv` |

## Planning

| Créneau | Phase | Durée |
|---|---|---|
| Matin | Phase 0 — préparation | 20 min |
| Matin | Phase 1 — run S1 (test `CPU_STEP`) | 40 min |
| Matin | **Point de décision** | 10 min |
| Reste | Phase 2 — 4 runs de réplication | ~4 h |

---

## Pourquoi ce plan

Le test de significativité MI (décalage circulaire) échoue sur **tous** les
runs couplés existants : 2/27 tests CPU sous 5 %, contre 2/27 pour la RAM
utilisée comme témoin négatif — le test ne discrimine pas.

La cause est mesurée, pas supposée : l'autocorrélation du CPU vaut **0,93 à
0,99** sur tous les runs, celle de la RAM **0,57 à 0,62**. La RAM mixe vite
depuis que son `RAM_STEP` a été porté à 20 ; le CPU est resté à `CPU_STEP=3`.
Deux séries qui dérivent lentement ensemble ne laissent qu'environ 1 % de
points effectivement indépendants — d'où l'absence de puissance.

**Allonger les runs ne résoudrait rien** : `LAAS_D` compte 1 400 points par VM,
cinq fois plus que les autres, et donne p = 0,41.

Valeur retenue, obtenue par simulation de la marche du simulateur :

| `CPU_STEP` | autocorrélation |
|---|---|
| 3 (actuel) | 0,992 |
| 20 | 0,832 |
| **33** | **0,674** ← même régime que la RAM |

---

## Phase 0 — préparation (20 min)

### 0.1 Deux protections contre la perte de données

Ces deux points ont coûté `qos_history` **entièrement** sur RUN_G et **à
moitié** sur RUN_H. Les runs A, C et D, eux, sont intacts.

1. **Poser `EXCEL_PATH` explicitement** dans les commandes de lancement.
   C'est ce que faisaient les runs A/C/D, tous exploitables ; les runs G/H ne
   le faisaient pas.
2. **Attendre 20 secondes après l'arrêt du PiCar** avant le `Ctrl+C` des
   orchestrateurs. `shared/excel_writer.py` écrit sans fichier temporaire : un
   arrêt pendant une sauvegarde tronque le fichier (`BadZipFile`).

### 0.2 Vérifier les APIs de prédiction

```bash
curl.exe "http://localhost:5001/hyperparameters"; curl.exe "http://localhost:5002/hyperparameters"; curl.exe "http://localhost:5003/hyperparameters"
```

Attendu : `window_size` = **39** (delay), **45** (cpu), **45** (ram). Toute
autre valeur signale un réentraînement involontaire — dans ce cas, réappliquer
le garde-fou :

```bash
curl.exe -X PUT "http://localhost:5001/update_configs?new_rmse_patience=999999&new_rmse_threshold=999999&new_trials=25"
```

### 0.3 Vérifier que le master répond

```bash
curl.exe "http://194.199.113.8:8024/active_vm"
```

Attendu : HTTP 200. Sans cela, les deux orchestrateurs se croiront actifs
(split-brain).

---

## Phase 1 — run S1 : le test de `CPU_STEP` (40 min)

Run **court** (25-30 min) : il ne sert qu'à répondre à une question binaire.

### 1.1 Relancer les agents VM avec `CPU_STEP=33`

Sur **chacune** des 4 machines, arrêter les agents en cours (`Ctrl+C`) puis :

```bash
CPU_STEP=33 ./launch_edge1_machine.sh
```

```bash
CPU_STEP=33 ./launch_edge2_machine.sh
```

```bash
CPU_STEP=33 ./launch_cloud1.sh
```

```bash
CPU_STEP=33 ./launch_cloud2.sh
```

Chaque script affiche sa ligne `Couplage : S_REF=1.2 RAM_STEP=20 COUPLING=1`.
`CPU_STEP` n'y figure pas — c'est normal, il est lu directement par
`vm_agent_sim.py`.

### 1.2 Sur le Pi — repartir d'un fichier propre

Arrêter le pont, **puis** :

```bash
mv latences.csv latences_avant_S1.csv
```

L'ordre compte : le pont garde le fichier ouvert en mode ajout, renommer sans
l'arrêter ne sert à rien.

### 1.3 Créer le dossier et lancer les orchestrateurs

```powershell
mkdir data_27-08_S1_cpustep
```

**Fenêtre 1 — provider 1 :**

```powershell
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"; $env:OPENSTACK_MASTER_IP="194.199.113.8"; $env:AWARD_GRACE_PERIOD_S="90"; $env:HISTORY_WINDOW="25"; $env:ML_HISTORY_WINDOW="60"; $env:EXCEL_PATH="data_27-08_S1_cpustep\qos_history_provider1.xlsx"; $env:TIMING_EXCEL_AUTONOMOUS_PATH="data_27-08_S1_cpustep\timings_autonomous_provider1.xlsx"; $env:PYTHONIOENCODING="utf-8"; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; .\venv\Scripts\python.exe launch_provider.py --provider provider1 2>&1 | Tee-Object -FilePath "data_27-08_S1_cpustep\provider1.log"
```

**Fenêtre 2 — provider 2 :**

```powershell
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"; $env:OPENSTACK_MASTER_IP="194.199.113.8"; $env:AWARD_GRACE_PERIOD_S="90"; $env:HISTORY_WINDOW="25"; $env:ML_HISTORY_WINDOW="60"; $env:EXCEL_PATH="data_27-08_S1_cpustep\qos_history_provider2.xlsx"; $env:TIMING_EXCEL_AUTONOMOUS_PATH="data_27-08_S1_cpustep\timings_autonomous_provider2.xlsx"; $env:PYTHONIOENCODING="utf-8"; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; .\venv\Scripts\python.exe launch_provider.py --provider provider2 2>&1 | Tee-Object -FilePath "data_27-08_S1_cpustep\provider2.log"
```

### 1.4 Contrôles avant de lancer la voiture

- `http://localhost:8009` et `http://localhost:8109` : **un seul** des deux doit
  être ACTIF, l'autre STANDBY, et les deux doivent désigner le **même** provider
  actif. Sinon : split-brain, arrêter et vérifier le point 0.3.
- Aucune erreur d'encodage dans les fenêtres.
- Après 2-3 min : les valeurs « Prédit » doivent **varier**. Si
  `all_apis_down : True` persiste au-delà de 3 min, voir le point 0.2.

### 1.5 Lancer le pont PiCar et laisser tourner 25-30 min

### 1.6 Arrêt — dans cet ordre

1. Arrêter le PiCar
2. **Attendre 20 secondes** (cf. 0.1)
3. `Ctrl+C` sur les deux fenêtres

```powershell
scp pi@140.93.64.105:~/Projet_PFE/multiProvider/latences.csv "data_27-08_S1_cpustep"
```

Sur le Pi, archiver pour le run suivant :

```bash
mv latences.csv latences_S1.csv
```

### 1.7 Analyse immédiate

```bash
./venv/Scripts/python.exe scripts/analyse/verify_run.py data_27-08_S1_cpustep federe
```

```bash
./venv/Scripts/python.exe scripts/analyse/mi_test2.py data_27-08_S1_cpustep/qos_history_provider1.xlsx
```

```bash
./venv/Scripts/python.exe scripts/analyse/mi_test2.py data_27-08_S1_cpustep/qos_history_provider2.xlsx
```

---

## Point de décision

Lire la colonne `ac_cpu` puis les colonnes `p`.

| Observation | Décision |
|---|---|
| `ac_cpu` ≈ 0,65-0,75 **et** au moins un `p_cpu` < 0,05 **et** tous les `p_ram` ≥ 0,05 | **Retenir `CPU_STEP=33`.** Phase 2 dans cette configuration. |
| `ac_cpu` encore > 0,90 | Le changement n'a pas pris — vérifier que les 4 machines ont bien été relancées, refaire 1.1. |
| `ac_cpu` ≈ 0,7 mais aucun `p_cpu` significatif | Le test n'a toujours pas de puissance. **Revenir à `CPU_STEP=3`** et faire la phase 2 sur la configuration actuelle. |
| Un `p_ram` devient significatif | **Alerte.** Le témoin négatif ne doit jamais sortir. Revenir à `CPU_STEP=3`. |

Vérifier aussi le taux de violation (`verify_run.py`) : il doit rester dans la
bande **40-55 %** pour la meilleure VM. En dehors, la découverte MI ne fonctionne
plus — c'est la règle de calibration déjà établie.

> **En cas de doute, revenir à `CPU_STEP=3`.** La configuration actuelle est
> connue et fonctionne ; quatre runs de réplication dessus valent mieux qu'une
> configuration incertaine.

---

## Phase 2 — 4 runs de réplication (~4 h)

Quatre runs de **45-50 min**, protocole identique, dans la configuration
retenue au point de décision.

> **Les agents VM ne sont PAS relancés entre les runs.** Ils gardent la
> configuration retenue au point de décision (`CPU_STEP=33` ou `CPU_STEP=3`)
> pendant les quatre runs. Ne les toucher que si le point de décision impose un
> retour à `CPU_STEP=3` — dans ce cas, refaire l'étape 1.1 avec `CPU_STEP=3`
> **avant** de commencer R1.

Chaque run suit exactement les mêmes 7 étapes. Elles sont écrites en entier
pour les quatre : rien à substituer, tout est copiable tel quel.

---

### RUN R1

**a. Créer le dossier**

```powershell
mkdir data_27-08_R1
```

**b. Sur le Pi** — arrêter le pont PiCar, **puis** :

```bash
mv latences.csv latences_avant_R1.csv
```

**c. Orchestrateur provider 1** (fenêtre 1) :

```powershell
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"; $env:OPENSTACK_MASTER_IP="194.199.113.8"; $env:AWARD_GRACE_PERIOD_S="90"; $env:HISTORY_WINDOW="25"; $env:ML_HISTORY_WINDOW="60"; $env:EXCEL_PATH="data_27-08_R1\qos_history_provider1.xlsx"; $env:TIMING_EXCEL_AUTONOMOUS_PATH="data_27-08_R1\timings_autonomous_provider1.xlsx"; $env:PYTHONIOENCODING="utf-8"; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; .\venv\Scripts\python.exe launch_provider.py --provider provider1 2>&1 | Tee-Object -FilePath "data_27-08_R1\provider1.log"
```

**Orchestrateur provider 2** (fenêtre 2) :

```powershell
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"; $env:OPENSTACK_MASTER_IP="194.199.113.8"; $env:AWARD_GRACE_PERIOD_S="90"; $env:HISTORY_WINDOW="25"; $env:ML_HISTORY_WINDOW="60"; $env:EXCEL_PATH="data_27-08_R1\qos_history_provider2.xlsx"; $env:TIMING_EXCEL_AUTONOMOUS_PATH="data_27-08_R1\timings_autonomous_provider2.xlsx"; $env:PYTHONIOENCODING="utf-8"; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; .\venv\Scripts\python.exe launch_provider.py --provider provider2 2>&1 | Tee-Object -FilePath "data_27-08_R1\provider2.log"
```

**d. Contrôles** — `localhost:8009` et `localhost:8109` : un seul ACTIF, les
deux d'accord sur lequel. Prédictions qui varient après 2-3 min.

**e. Lancer le PiCar**, laisser tourner **45-50 min** (viser le temps, pas le
nombre de cycles).

**f. Arrêt** — PiCar, puis **attendre 20 s**, puis `Ctrl+C` sur les deux
fenêtres. Ensuite :

```powershell
scp pi@140.93.64.105:~/Projet_PFE/multiProvider/latences.csv "data_27-08_R1"
```

Sur le Pi :

```bash
mv latences.csv latences_R1.csv
```

**g. Vérifier avant de passer à R2**

```bash
./venv/Scripts/python.exe scripts/analyse/verify_run.py data_27-08_R1 federe
```

---

### RUN R2

**a. Créer le dossier**

```powershell
mkdir data_27-08_R2
```

**b. Sur le Pi** — arrêter le pont PiCar, **puis** :

```bash
mv latences.csv latences_avant_R2.csv
```

**c. Orchestrateur provider 1** (fenêtre 1) :

```powershell
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"; $env:OPENSTACK_MASTER_IP="194.199.113.8"; $env:AWARD_GRACE_PERIOD_S="90"; $env:HISTORY_WINDOW="25"; $env:ML_HISTORY_WINDOW="60"; $env:EXCEL_PATH="data_27-08_R2\qos_history_provider1.xlsx"; $env:TIMING_EXCEL_AUTONOMOUS_PATH="data_27-08_R2\timings_autonomous_provider1.xlsx"; $env:PYTHONIOENCODING="utf-8"; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; .\venv\Scripts\python.exe launch_provider.py --provider provider1 2>&1 | Tee-Object -FilePath "data_27-08_R2\provider1.log"
```

**Orchestrateur provider 2** (fenêtre 2) :

```powershell
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"; $env:OPENSTACK_MASTER_IP="194.199.113.8"; $env:AWARD_GRACE_PERIOD_S="90"; $env:HISTORY_WINDOW="25"; $env:ML_HISTORY_WINDOW="60"; $env:EXCEL_PATH="data_27-08_R2\qos_history_provider2.xlsx"; $env:TIMING_EXCEL_AUTONOMOUS_PATH="data_27-08_R2\timings_autonomous_provider2.xlsx"; $env:PYTHONIOENCODING="utf-8"; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; .\venv\Scripts\python.exe launch_provider.py --provider provider2 2>&1 | Tee-Object -FilePath "data_27-08_R2\provider2.log"
```

**d. Contrôles** — dashboards cohérents, prédictions qui varient.

**e. Lancer le PiCar**, 45-50 min.

**f. Arrêt** — PiCar, **20 s**, `Ctrl+C` × 2. Puis :

```powershell
scp pi@140.93.64.105:~/Projet_PFE/multiProvider/latences.csv "data_27-08_R2"
```

Sur le Pi :

```bash
mv latences.csv latences_R2.csv
```

**g. Vérifier avant de passer à R3**

```bash
./venv/Scripts/python.exe scripts/analyse/verify_run.py data_27-08_R2 federe
```

---

### RUN R3

**a. Créer le dossier**

```powershell
mkdir data_27-08_R3
```

**b. Sur le Pi** — arrêter le pont PiCar, **puis** :

```bash
mv latences.csv latences_avant_R3.csv
```

**c. Orchestrateur provider 1** (fenêtre 1) :

```powershell
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"; $env:OPENSTACK_MASTER_IP="194.199.113.8"; $env:AWARD_GRACE_PERIOD_S="90"; $env:HISTORY_WINDOW="25"; $env:ML_HISTORY_WINDOW="60"; $env:EXCEL_PATH="data_27-08_R3\qos_history_provider1.xlsx"; $env:TIMING_EXCEL_AUTONOMOUS_PATH="data_27-08_R3\timings_autonomous_provider1.xlsx"; $env:PYTHONIOENCODING="utf-8"; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; .\venv\Scripts\python.exe launch_provider.py --provider provider1 2>&1 | Tee-Object -FilePath "data_27-08_R3\provider1.log"
```

**Orchestrateur provider 2** (fenêtre 2) :

```powershell
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"; $env:OPENSTACK_MASTER_IP="194.199.113.8"; $env:AWARD_GRACE_PERIOD_S="90"; $env:HISTORY_WINDOW="25"; $env:ML_HISTORY_WINDOW="60"; $env:EXCEL_PATH="data_27-08_R3\qos_history_provider2.xlsx"; $env:TIMING_EXCEL_AUTONOMOUS_PATH="data_27-08_R3\timings_autonomous_provider2.xlsx"; $env:PYTHONIOENCODING="utf-8"; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; .\venv\Scripts\python.exe launch_provider.py --provider provider2 2>&1 | Tee-Object -FilePath "data_27-08_R3\provider2.log"
```

**d. Contrôles** — dashboards cohérents, prédictions qui varient.

**e. Lancer le PiCar**, 45-50 min.

**f. Arrêt** — PiCar, **20 s**, `Ctrl+C` × 2. Puis :

```powershell
scp pi@140.93.64.105:~/Projet_PFE/multiProvider/latences.csv "data_27-08_R3"
```

Sur le Pi :

```bash
mv latences.csv latences_R3.csv
```

**g. Vérifier avant de passer à R4**

```bash
./venv/Scripts/python.exe scripts/analyse/verify_run.py data_27-08_R3 federe
```

---

### RUN R4

**a. Créer le dossier**

```powershell
mkdir data_27-08_R4
```

**b. Sur le Pi** — arrêter le pont PiCar, **puis** :

```bash
mv latences.csv latences_avant_R4.csv
```

**c. Orchestrateur provider 1** (fenêtre 1) :

```powershell
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"; $env:OPENSTACK_MASTER_IP="194.199.113.8"; $env:AWARD_GRACE_PERIOD_S="90"; $env:HISTORY_WINDOW="25"; $env:ML_HISTORY_WINDOW="60"; $env:EXCEL_PATH="data_27-08_R4\qos_history_provider1.xlsx"; $env:TIMING_EXCEL_AUTONOMOUS_PATH="data_27-08_R4\timings_autonomous_provider1.xlsx"; $env:PYTHONIOENCODING="utf-8"; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; .\venv\Scripts\python.exe launch_provider.py --provider provider1 2>&1 | Tee-Object -FilePath "data_27-08_R4\provider1.log"
```

**Orchestrateur provider 2** (fenêtre 2) :

```powershell
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"; $env:OPENSTACK_MASTER_IP="194.199.113.8"; $env:AWARD_GRACE_PERIOD_S="90"; $env:HISTORY_WINDOW="25"; $env:ML_HISTORY_WINDOW="60"; $env:EXCEL_PATH="data_27-08_R4\qos_history_provider2.xlsx"; $env:TIMING_EXCEL_AUTONOMOUS_PATH="data_27-08_R4\timings_autonomous_provider2.xlsx"; $env:PYTHONIOENCODING="utf-8"; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; .\venv\Scripts\python.exe launch_provider.py --provider provider2 2>&1 | Tee-Object -FilePath "data_27-08_R4\provider2.log"
```

**d. Contrôles** — dashboards cohérents, prédictions qui varient.

**e. Lancer le PiCar**, 45-50 min.

**f. Arrêt** — PiCar, **20 s**, `Ctrl+C` × 2. Puis :

```powershell
scp pi@140.93.64.105:~/Projet_PFE/multiProvider/latences.csv "data_27-08_R4"
```

Sur le Pi :

```bash
mv latences.csv latences_R4.csv
```

**g. Vérifier**

```bash
./venv/Scripts/python.exe scripts/analyse/verify_run.py data_27-08_R4 federe
```

---

> **Ne jamais enchaîner sur le run suivant sans avoir validé le précédent.**
> `verify_run.py` prend 30 secondes. Un run raté détecté tout de suite peut
> être refait ; détecté le soir, il est perdu définitivement.

**Les 5 fichiers attendus dans chaque dossier :**

```
qos_history_provider1.xlsx      <- indispensable pour le test MI
qos_history_provider2.xlsx      <- indispensable pour le test MI
timings_autonomous_provider1.xlsx
timings_autonomous_provider2.xlsx
latences.csv
```

Si un `qos_history` manque ou est corrompu, c'est la protection 0.1 qui n'a pas
été appliquée.

---

## Fin de journée — contrôle global

**Test de significativité MI sur les 8 historiques** (4 runs × 2 providers) :

```bash
./venv/Scripts/python.exe scripts/analyse/mi_test2.py data_27-08_R1/qos_history_provider1.xlsx
```

```bash
./venv/Scripts/python.exe scripts/analyse/mi_test2.py data_27-08_R1/qos_history_provider2.xlsx
```

```bash
./venv/Scripts/python.exe scripts/analyse/mi_test2.py data_27-08_R2/qos_history_provider1.xlsx
```

```bash
./venv/Scripts/python.exe scripts/analyse/mi_test2.py data_27-08_R2/qos_history_provider2.xlsx
```

```bash
./venv/Scripts/python.exe scripts/analyse/mi_test2.py data_27-08_R3/qos_history_provider1.xlsx
```

```bash
./venv/Scripts/python.exe scripts/analyse/mi_test2.py data_27-08_R3/qos_history_provider2.xlsx
```

```bash
./venv/Scripts/python.exe scripts/analyse/mi_test2.py data_27-08_R4/qos_history_provider1.xlsx
```

```bash
./venv/Scripts/python.exe scripts/analyse/mi_test2.py data_27-08_R4/qos_history_provider2.xlsx
```

Ce qu'on lit : le nombre de `p_cpu` sous 5 % **face au** nombre de `p_ram` sous
5 %. C'est l'écart entre les deux qui compte, pas la valeur absolue — la RAM
n'étant couplée à rien, elle donne la mesure du bruit.

**Vérification finale avant de quitter le LAAS.** Les 5 dossiers
(`data_27-08_S1_cpustep`, `data_27-08_R1` à `R4`) doivent contenir chacun leurs
5 fichiers :

```powershell
Get-ChildItem data_27-08_S1_cpustep, data_27-08_R1, data_27-08_R2, data_27-08_R3, data_27-08_R4 | Select-Object Directory, Name, Length
```

C'est la dernière occasion de refaire un run.

---

## Sauvegardes à ne pas oublier

`infrastructure/` est ignoré par git : les 5 exemplaires de `vm_agent_sim.py`
n'existent que sur disque et sur les 4 machines du LAAS. **Les copier hors du
LAAS avant de partir** — c'est la pièce centrale du couplage, et elle n'est
sauvegardée nulle part.

---

## Ce qu'on ne fait pas, et pourquoi

- **Pas de campagne 4+4 refaite.** Les modèles de prédiction ont été entraînés
  sans le couplage ; relancer la campagne sans régénérer les jeux de données et
  réentraîner produirait des résultats moins bons, non représentatifs. La
  campagne existante reste le résultat central du rapport.
- **Pas de runs d'ablation.** La comparaison fédéré/ablation est déjà établie
  et significative. Ajouter des runs sans hypothèse à tester alourdit sans rien
  démontrer.
- **Pas de runs allongés pour « avoir plus de points ».** Démontré ci-dessus :
  `LAAS_D` avec 1 400 points par VM donne p = 0,41. Le problème est
  l'autocorrélation, pas la taille de l'échantillon.
