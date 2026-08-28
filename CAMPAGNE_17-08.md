# Campagne du 17 août 2026 — fiche d'exécution

> Gardez ce fichier ouvert. Une section par run, à cocher au fur et à mesure.

---

## Déjà acquis aujourd'hui — ne pas refaire

| Dossier | Contenu |
|---|---|
| `data_UC5_intentions` | ✅ UC5 : 12 intentions, 10 traduites, 2 rejets hors-domaine |
| `data_UC5_essai1_ollama_fallback` | comportement du repli Ollama (modèle LAAS en panne) |

**Correctif appliqué avant la campagne :** `LAAS_MODEL` → `Qwen3.8` ([config.py:238](shared/config.py:238)).
Le vLLM LAAS avait renommé son modèle entre le 14 et le 17 août ; l'ancien nom
renvoyait `HTTP 404` et le système basculait en silence sur Ollama.

**⚠️ `INTENT_GROUNDED_THRESHOLDS` doit rester absent ou à `false` pendant toute
la campagne.** Sinon les runs ne sont plus comparables à ceux d'août.

---

## Réglages, identiques à chaque run

**Fédéré** — les DEUX orchestrateurs :

```powershell
$env:MULTI_PROVIDER_ENABLED="true"; $env:AWARD_GRACE_PERIOD_S="90"; $env:PYTHONIOENCODING="utf-8"
.\venv\Scripts\python.exe launch_provider.py --provider provider1 2>&1 | Tee-Object -FilePath "logs\FED<N>_p1_$(Get-Date -Format yyyyMMdd_HHmm).log"
```
(idem `provider2` dans un second terminal)

**Ablation** — provider1 SEUL :

```powershell
$env:MULTI_PROVIDER_ENABLED="false"; $env:PYTHONIOENCODING="utf-8"
.\venv\Scripts\python.exe launch_provider.py --provider provider1 2>&1 | Tee-Object -FilePath "logs\ABL<N>_p1_$(Get-Date -Format yyyyMMdd_HHmm).log"
```

**Le piège :** `MULTI_PROVIDER_ENABLED` persiste dans la session PowerShell.
Posez-le sur la MÊME ligne que le lancement, à chaque fois. Un run d'ablation
lancé avec la valeur `true` héritée est un run perdu, et `verify_run.py` ne le
dira qu'à la fin.

**Le piège découvert le 17/08 après RUN 1 (fédéré) → RUN 2 (ablation) :**
kubectl garde la trace du service réel entre les runs. Si le fédéré précédent
s'est terminé avec le service sur une VM du provider-2 (ex. edge2), et qu'on
lance ensuite provider1 SEUL, le hub voit via kubectl que le service est chez
un provider absent, conclut qu'il est STANDBY ([orchestrator_core.py:384](hub/orchestrator_core.py:384))
et **ne fait plus rien** ([orchestrator_core.py:973](hub/orchestrator_core.py:973)) —
le run tourne à vide pendant toute sa durée sans qu'aucune erreur ne le signale.

**Avant CHAQUE run d'ablation qui suit un run fédéré**, vérifier puis
rapatrier si besoin :

```powershell
curl.exe -sS http://194.199.113.8:8024/active_vm
```

Si la VM affichée n'appartient pas à provider1 (`edge1`/`edge1b`/`edge1c`/`cloud1`) :

```powershell
$body = @{ from_vm = "<vm_actuelle>"; to_vm = "edge1" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://194.199.113.8:8024/migrate" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 30
Start-Sleep -Seconds 15
curl.exe -sS http://194.199.113.8:8024/active_vm   # doit confirmer edge1
```

La propagation prend ~15 s — un `Terminating` encore visible fait répondre
l'ancienne VM une première fois, ce n'est pas un échec.

---

## Déroulé d'un run — 55 min

- [ ] `data\` est vide (`Get-ChildItem data -Force`)
- [ ] Sur le Pi : `mv latences.csv latences_precedent.csv`
- [ ] Lancer le ou les orchestrateurs
- [ ] Lancer le PiCar
- [ ] **3 tours complets** — environ 50 min
- [ ] Arrêter le **PiCar**
- [ ] **Attendre 30 secondes** ← vidange de la file d'écriture Excel
- [ ] `Ctrl+C` sur les orchestrateurs
- [ ] Archiver :
      ```powershell
      mv data data_<NOM> ; mkdir data
      Copy-Item "logs\<PREFIXE>*" "data_<NOM>\"
      ```
- [ ] Récupérer la trajectoire :
      ```bash
      scp pi@140.93.64.105:~/Projet_PFE/multiProvider/latences.csv "…\data_<NOM>\"
      ```
      puis sur le Pi : `mv latences.csv latences_<NOM>.csv`
- [ ] Vérifier : `python scripts\analyse\verify_run.py data_<NOM> federe|ablation`

**Sans `latences.csv`, le run est inexploitable** — pas de trajectoire, pas de
calcul de conformité. C'est l'étape la plus facile à oublier.

---

## Nommage

| Bras | Dossiers |
|---|---|
| Fédéré | `data_FED_run1` … `data_FED_run4` |
| Ablation | `data_ABL_run1` … `data_ABL_run4` |

---

## Combien de runs

| Plan | Durée | Ce que permet le test statistique |
|---|---|---|
| 4 + 4 | 7 h 20 | Mann-Whitney peut atteindre **p = 0,029** |
| 3 + 3 | 5 h 30 | p minimale **0,10** — ne peut jamais être significatif |
| 2 + 2 | 3 h 40 | écarts-types seulement, aucun test |

La p-valeur minimale d'un Mann-Whitney bilatéral est `2 / C(n+m, n)` :
3+3 → 2/20 = 0,10 ; 4+4 → 2/70 = 0,029. **C'est 4+4 ou pas de test
significatif**, quelle que soit la netteté de l'écart observé.

**Alterner les bras** (FED, ABL, FED, ABL…) plutôt que grouper : si l'état de
l'infrastructure dérive au fil de la journée, la dérive se répartit sur les
deux conditions au lieu d'en favoriser une.

---

## Règle absolue

**Ne rien modifier dans le code entre deux runs.** Aucune exception, même une
correction évidente. Un run réalisé avec un code différent des autres n'entre
pas dans la moyenne et se jette.

Toute correction repérée pendant la campagne : notez-la, appliquez-la après.

---

## À faire après la campagne

| | Quoi | Durée |
|---|---|---|
| C | Banc hors-ligne des seuils ancrés | 4 min |
| D | Validation d'intégration, 4 phrases, drapeau ON | 15 min |
| B | Fédération sur deux machines | ~30 min |
| — | Réécriture §VI et §VII (moi) | 2 h |

---

## Fichiers liés

| Fichier | Contenu |
|---|---|
| `PLAN_FINALISATION_PAPIER.md` | plan d'ensemble du papier |
| `scripts/analyse/README.md` | les 11 scripts d'analyse et leur ordre |
| `run_uc5_intentions.ps1` | UC5, déjà exécuté |
