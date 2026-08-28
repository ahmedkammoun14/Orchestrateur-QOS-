# RUN 9 — Correction des poids de l'horizon de prédiction

> **Objectif scientifique.** Transformer la limite « le système suit l'optimum
> avec 12 s de retard » (rapport, section 5.4.3) en **optimisation démontrée**.
>
> **Critère de réussite, fixé AVANT le run** (ne rien décider après coup) :
> le pic de concordance de `diagnose_gap.py` doit se déplacer de **12 s vers
> 6 s ou 0 s**. Si le pic reste à 12 s, le résultat est **négatif** et se
> rapporte tel quel : l'hypothèse des poids d'horizon serait alors écartée,
> ce qui est aussi une information utile pour le rapport.
>
> **Garde-fou.** Si le nombre de migrations explose (> 15 / 100 cycles, contre
> 7,0 en référence fédérée), c'est de l'oscillation : le rapporter, ne pas
> réajuster la marge d'hystérésis dans la foulée — on ne saurait plus quelle
> modification a produit quel effet.

Durée totale : **~1 h 05** (5 min patch + 55 min run + 5 min analyse).

---

## Valeurs de référence à battre (campagne fédérée, 4 runs)

| Indicateur | Référence fédérée | Source |
|---|---|---|
| Pic de concordance | **12 s** (4 runs sur 4) | `diagnose_gap.py` |
| Taux de violation | 23,8 % (étendue 19,9 – 29,3) | `violation_rate.py` |
| Violations évitables | 16,3 % | `diagnose_gap.py` |
| Migrations / 100 cycles | 7,0 (étendue 6,5 – 7,3) | tableau 5.10 du rapport |

---

## ÉTAPE 0 — Contrôles préalables (10 min)

Les 8 agents VM doivent répondre. Un run lancé avec une VM muette est perdu.

```powershell
curl.exe -sS http://194.199.113.18:8200/health   # edge1
curl.exe -sS http://194.199.113.18:8201/health   # edge1b
curl.exe -sS http://194.199.113.18:8202/health   # edge1c
curl.exe -sS http://194.199.113.28:8200/health   # edge2   <-- VIGILANCE
curl.exe -sS http://194.199.113.28:8201/health   # edge2b  <-- VIGILANCE
curl.exe -sS http://194.199.113.28:8202/health   # edge2c  <-- VIGILANCE
curl.exe -sS http://194.199.113.66:8200/health   # cloud1
curl.exe -sS http://194.199.113.69:8200/health   # cloud2
```

Si `194.199.113.28` ne répond pas : `./launch_edge2_machine.sh` sur cette
machine **avant** de continuer.

Le master (migrations kubectl réelles) :

```powershell
curl.exe -sS http://194.199.113.8:8024/health
```

⚠️ **Les 3 APIs ML n'exposent PAS `/health`** — un `404 {"detail":"Not Found"}`
signifie que l'API répond, donc qu'elle tourne. Ne pas conclure à une panne.
Le contrôle utile est celui de l'effondrement du modèle, ci-dessous.

**Contrôle d'effondrement du modèle de latence.** L'entraînement ne fait que
**3 essais Optuna**. Si le tirage retient un `learning_rate` élevé (≳ 0,05), le
GRU s'effondre et prédit une **constante** : les 8 VM affichent alors la même
latence prédite, aucune migration n'a lieu, et le run est perdu.

```powershell
@(0.10, 0.50, 0.95) | ForEach-Object { $r = Invoke-RestMethod "http://localhost:5001/predict?input_data=$_"; "input $_  ->  $($r | ConvertTo-Json -Compress)" }
```

Les trois sorties doivent être **nettement différentes**. Si elles sont
identiques, relancer un entraînement puis retester. Contrôler aussi le
`learning_rate` du meilleur essai dans les logs de l'API : < 0,01 = bon signe,
> 0,05 = relancer sans même tester.

La non-monotonie des sorties n'est pas un défaut : cet endpoint ne fournit
qu'un seul point à un modèle entraîné sur des séquences de 45, c'est le niveau
dégradé de la cascade. Le chemin nominal est `/predict_sequence`.

⚠️ **Ne redémarrer aucune API ML pendant le run.** Un redémarrage relance un
entraînement et contamine l'exécution en cours — c'est ce qui a invalidé les
runs archivés sous `data_FED1_CONTAMINE_reentrainement` et
`data_ABL2_CONTAMINE_reentrainement`.

**Si les modèles ont été réentraînés avant ce run**, le noter : le run 9
combine alors deux changements, les poids d'horizon (voulu) et des modèles
réentraînés (subi). Ce n'est pas rédhibitoire — même architecture, mêmes
fenêtres — mais cela doit être mentionné au moment de rapporter le résultat.

Le dossier `data\` doit être vide :

```powershell
Get-ChildItem data -Force
```

Sur le Raspberry Pi :

```bash
mv latences.csv latences_avant_run9.csv
```

---

## ÉTAPE 1 — Appliquer le patch (5 min)

**Fichier** : `services/decision_intelligence/topsis.py`, lignes 251-257.

Remplacer :

```python
    def calculate_weighted_mean(self, preds: List[float]) -> float:
        if not preds:
            return 0.0
        n:       int       = len(preds)
        weights: List[int] = list(range(n, 0, -1))
        total:   int       = sum(weights)
        return sum(w * p for w, p in zip(weights, preds)) / total
```

par :

```python
    def calculate_weighted_mean(self, preds: List[float]) -> float:
        """
        Moyenne ponderee sur l'horizon de prediction.

        Les poids privilegient les pas 2-3 plutot que le pas immediat : le
        retard mesure du systeme sur l'optimum est de ~12 s, soit 2 cycles.
        Ponderer le present le plus fort (ancien schema [7,6,5,4,3,2,1])
        faisait reagir le systeme APRES la sortie de zone de couverture.

        Les pas lointains (t+5 a t+7) restent faiblement ponderes : la
        precision du modele s'y degrade.
        """
        if not preds:
            return 0.0
        n = len(preds)
        default = [3, 5, 5, 4, 3, 2, 1]          # profil centre sur t+2/t+3
        weights = (default[:n] if n <= len(default)
                   else default + [1] * (n - len(default)))
        total = sum(weights)
        return sum(w * p for w, p in zip(weights, preds)) / total
```

**Vérifier le patch avant de lancer** — l'ancien profil met 25 % sur le pas 1,
le nouveau met 25 % sur les pas 2-3 :

```powershell
.\venv\Scripts\python.exe -c "d=[3,5,5,4,3,2,1]; t=sum(d); print('total',t); print([round(100*w/t,1) for w in d])"
```

Sortie attendue : `total 23` puis `[13.0, 21.7, 21.7, 17.4, 13.0, 8.7, 4.3]`.
Le maximum doit être sur les pas **2 et 3**, pas sur le pas 1.

**Ne rien modifier d'autre.** Ni le cooldown, ni la marge d'hystérésis, ni la
période du cycle. Un run avec deux modifications simultanées est ininterprétable.

---

## ÉTAPE 2 — Lancer le run (55 min)

Configuration **fédérée**, identique aux 4 runs de référence.

⚠️ **`MULTI_PROVIDER_ENABLED` doit être posé sur la MÊME LIGNE que le
lancement.** La variable persiste dans la session PowerShell, et le lanceur la
force à `true` par défaut si elle est absente. Ne jamais rappeler seulement la
ligne `python` avec la flèche haut.

**Terminal 1 — provider1**

```powershell
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"
$env:MULTI_PROVIDER_ENABLED="true"; $env:AWARD_GRACE_PERIOD_S="90"
$env:PYTHONIOENCODING="utf-8"
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$OutputEncoding=[System.Text.Encoding]::UTF8
.\venv\Scripts\python.exe launch_provider.py --provider provider1 2>&1 | Tee-Object -FilePath "logs\RUN9_p1_$(Get-Date -Format yyyyMMdd_HHmm).log"
```

**Terminal 2 — provider2** : identique avec `--provider provider2` et `p2` dans
le nom du log.

**Contrôle à la 2ᵉ minute, pas à la 55ᵉ.** Dans le terminal du hub, des
diffusions et des offres (négociation) doivent apparaître. Si aucune trace de
négociation : le run n'est pas fédéré, arrêter et relancer.

Puis :

1. **Attendre 5 minutes** avant de lancer le PiCar.
   La fenêtre des modèles est de **45 points pour la latence et la mémoire**
   (4 min 30 à 6 s/cycle), 25 pour le CPU. Les 3 minutes du protocole d'août
   étaient insuffisantes — voir rapport, section 5.2.1. Cinq minutes couvrent
   les trois modèles.
2. Lancer le PiCar.
3. **3 passages au même point** — environ 40 min de roulage.
4. Arrêter le **PiCar** en premier.
5. **Attendre 30 secondes** — vidange de la file d'écriture Excel. Sans cette
   attente, la fin du run est amputée.
6. `Ctrl+C` sur les deux orchestrateurs.

---

## ÉTAPE 3 — Archiver (5 min)

```powershell
mv data data_RUN9_horizon ; mkdir data
Copy-Item "logs\RUN9*" "data_RUN9_horizon\"
```

Récupérer la trajectoire depuis le Pi — **sans elle le run est inexploitable** :

```bash
scp pi@140.93.64.105:~/Projet_PFE/multiProvider/latences.csv "C:/Users/ahmed/Desktop/PFE_juin/uc1(02-06-2026)/qos-orchestrator/data_RUN9_horizon/"
```

Puis sur le Pi : `mv latences.csv latences_run9.csv`

Vérifier l'intégrité du run :

```powershell
.\venv\Scripts\python.exe scripts\analyse\verify_run.py data_RUN9_horizon federe
```

---

## ÉTAPE 4 — Analyser (5 min)

**4.1 — Isoler la trajectoire**

```powershell
.\venv\Scripts\python.exe scripts\analyse\isolate_session.py data_RUN9_horizon/latences.csv
```

**4.2 — Taux de violation**

```powershell
.\venv\Scripts\python.exe scripts\analyse\violation_rate.py
```

Ajouter d'abord le run dans la liste `RUNS` en tête de
`scripts/analyse/violation_rate.py` :

```python
    ("RUN9 (horizon corrige)", "federe", "data_RUN9_horizon",
     ["timings_autonomous_provider1.xlsx", "timings_autonomous_provider2.xlsx"]),
```

Noter les deux nombres affichés (violations / échantillons) : ils servent de
valeurs de contrôle à l'étape suivante.

**4.3 — Le diagnostic décisif : où est le pic ?**

Ajouter en bas de `scripts/analyse/diagnose_gap.py`, en reprenant les deux
nombres de l'étape 4.2 :

```python
run("RUN9 (horizon corrige) -- FEDERE (8 VMs)",
    "data_RUN9_horizon/latences_session.csv",
    ["data_RUN9_horizon/timings_autonomous_provider1.xlsx",
     "data_RUN9_horizon/timings_autonomous_provider2.xlsx"],
    VMS, <violations>, <echantillons>)
```

```powershell
.\venv\Scripts\python.exe scripts\analyse\diagnose_gap.py
```

⚠️ Le script s'arrête sur `DIVERGENCE` si les valeurs de contrôle ne
correspondent pas à celles de `violation_rate.py`. C'est voulu : les deux
calculs doivent concorder exactement.

**Lire le bloc « Le systeme suit-il l'optimum avec du retard ? »** :

| Pic observé | Verdict |
|---|---|
| **0 s ou 6 s** | ✅ Hypothèse confirmée — le retard était bien causé par la pondération |
| **12 s** (inchangé) | ❌ Hypothèse écartée — la cause est ailleurs, à rapporter tel quel |
| **18 s ou plus** | ⚠️ Aggravation — vérifier que le patch a bien été appliqué |

Relever aussi les **violations évitables** (référence : 16,3 %). Une baisse
confirmerait l'effet.

**4.4 — Taux de migration (garde-fou contre l'oscillation)**

```powershell
.\venv\Scripts\python.exe -c "import openpyxl; ws=openpyxl.load_workbook('data_RUN9_horizon/timings_autonomous_provider1.xlsx',read_only=True).worksheets[0]; d=[str(r[4]) for r in ws.iter_rows(min_row=4,values_only=True) if r[0] is not None]; m=sum(1 for x in d if x=='migrate'); print(f'p1: {m} migrations / {len(d)} cycles = {100*m/len(d):.1f} pour 100')"
```

Idem pour `provider2`, puis additionner les migrations et diviser par le nombre
de cycles de provider1 (méthode du tableau 5.10).

Référence fédérée : **7,0 pour 100 cycles**. Au-delà de ~15, c'est de
l'oscillation : le rapporter comme effet secondaire du patch.

---

## ÉTAPE 5 — Reporter dans le rapport

Selon le résultat :

**Si le pic s'est déplacé** → section 5.4.3, remplacer le dernier paragraphe
(« Cette correction n'a pas été appliquée pendant la campagne… ») par le
résultat mesuré. Retirer le `%% TODO — demo LAAS` correspondant dans 5.6.2. La
limite devient une contribution.

**Si le pic n'a pas bougé** → conserver le paragraphe actuel en précisant que
l'hypothèse a été testée et écartée. C'est un résultat négatif publiable, et il
renforce l'honnêteté du chapitre.

Dans les deux cas, mentionner que ce run est **hors campagne** : il utilise un
code différent des huit autres et n'entre donc pas dans les moyennes ni dans le
test de Mann-Whitney.

---

## Après le run — revenir à l'état de la campagne

Le patch modifie le comportement du système. Si d'autres runs comparables aux
huit de la campagne devaient être relancés ensuite, il faudrait **annuler le
patch** au préalable, sans quoi ils ne seraient plus comparables.

```powershell
git diff services/decision_intelligence/topsis.py    # verifier ce qui a change
git checkout services/decision_intelligence/topsis.py  # annuler le patch
```
