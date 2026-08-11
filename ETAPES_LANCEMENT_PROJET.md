# Étapes de lancement du projet

**QoS Orchestrator — PiCar-X**
Procédure complète, de l'infrastructure à la démonstration.

> Suivre les étapes **dans l'ordre**. Chaque étape a un contrôle : ne pas passer
> à la suivante tant qu'il n'est pas vert.

---

## Configuration de référence

| Paramètre | Valeur | Où |
|---|---|---|
| Seuil latence θ | **28 ms** | `shared/config.py:376` |
| Vitesse voiture | **0,25 cm/s** | curseur du simulateur PiCar |
| `SEND_INTERVAL_S` | 5 s → T_cycle 6 s | `picar_bridge_QoS1.py` |
| `AWARD_GRACE_PERIOD_S` | **90 s** | variable d'environnement, étape 7 |
| Warm-up | 3 min 30 (35 cycles × 6 s) | — |
| Durée d'un tour | 16 min | — |

---

## 1. Prérequis

### Redis

```bash
wsl redis-cli ping
```

✅ Attendu : `PONG`

### Les 8 VMs — sur les 4 machines LAAS

```bash
./launch_edge1_machine.sh      # pop1-worker-1 : edge1, edge1b, edge1c
./launch_edge2_machine.sh      # pop1-worker-2 : edge2, edge2b, edge2c
./launch_cloud1.sh             # pop2-worker-1
./launch_cloud2.sh             # pop2-worker-2
```

### Le master OpenStack

```bash
python3 openstack_client.py
```

### Le pont PiCar

```bash
cd ~/Projet_PFE/multiProvider && python3 picar_bridge_QoS1.py
```

---

## 2. Les trois APIs ML — un terminal chacune

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\Api-Model-Predict"; .\venv\Scripts\Activate.ps1; $env:MODEL_ID="delay"; uvicorn app.auto:auto_app --port 5001
```

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\Api-Model-Predict"; .\venv\Scripts\Activate.ps1; $env:MODEL_ID="cpu"; uvicorn app.auto:auto_app --port 5002
```

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\Api-Model-Predict"; .\venv\Scripts\Activate.ps1; $env:MODEL_ID="ram"; uvicorn app.auto:auto_app --port 5003
```

Les modèles sont persistés dans `models/*.keras` et rechargés automatiquement.
**Si la géométrie n'a pas changé, les étapes 3 et 4 sont inutiles** — passer
directement à l'étape 5.

---

## 3. Désactiver le réentraînement automatique

> ⚠️ **Étape critique, à faire avant tout entraînement.**
> L'API se réentraîne d'elle-même après 3 erreurs RMSE consécutives au-dessus
> de 0,8, et écrase `models/delay.keras` avec un modèle appris sur ~800 points
> au lieu de 192 000. C'est arrivé en pleine session.

```bash
curl.exe -X PUT "http://localhost:5001/update_configs?new_rmse_patience=999999&new_rmse_threshold=999999&new_trials=25"; curl.exe -X PUT "http://localhost:5002/update_configs?new_rmse_patience=999999&new_rmse_threshold=999999&new_trials=3"; curl.exe -X PUT "http://localhost:5003/update_configs?new_rmse_patience=999999&new_rmse_threshold=999999&new_trials=3"
```

✅ Attendu : `{"message":"Constants updated successfully", ...}` trois fois.

---

## 4. Entraîner les trois modèles

**Uniquement si la géométrie a changé** — positions des VMs, trajectoire,
physique `LAT_B`/`LAT_A`/`D_MIN`/`D_MAX`, ou vitesse hors de 0,20–0,30 cm/s.

### Latence — 8 à 15 min

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\Api-Model-Predict"; curl.exe -X POST "http://localhost:5001/main" -F "file=@qos_delay_demo_edge.xlsx" -F "target_columns=delay_mixed" -F "forecasting_horizon=7"
```

### CPU — quelques secondes

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\Api-Model-Predict"; curl.exe -X POST "http://localhost:5002/main" -F "file=@qos_cpu_train_mixed.xlsx" -F "target_columns=cpu_mixed" -F "forecasting_horizon=7"
```

### RAM — quelques secondes

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\Api-Model-Predict"; curl.exe -X POST "http://localhost:5003/main" -F "file=@qos_ram_train_mixed.xlsx" -F "target_columns=ram_mixed" -F "forecasting_horizon=7"
```

### Régénérer le jeu de données, si la géométrie a changé

À faire **avant** l'entraînement latence :

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\Api-Model-Predict"; python gen_delay_dataset.py
```

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\Api-Model-Predict"; python -c "import pandas as pd; d=pd.read_excel('qos_delay_demo_multi.xlsx'); e=[c for c in d.columns if c.startswith('edge')]; s=pd.concat([d[c] for c in e], ignore_index=True); pd.DataFrame({'delay_mixed': s}).to_excel('qos_delay_demo_edge.xlsx', index=False); v=s*100; print('%d lignes  min %.1f  med %.1f  max %.1f  |diff| %.2f'%(len(s),v.min(),v.median(),v.max(),v.diff().abs().mean()))"
```

✅ Attendu : **192 000 lignes**, `|diff|` autour de **1,9 ms**.

---

## 5. Vérifier les modèles

```bash
curl.exe "http://localhost:5001/hyperparameters"; curl.exe "http://localhost:5002/hyperparameters"; curl.exe "http://localhost:5003/hyperparameters"
```

✅ Attendu : `window_size` de **35** (delay), **25** (cpu), **45** (ram), `status: ready`.

❌ Si `window_size` du delay n'est pas 35, le modèle a été écrasé — reprendre
les étapes 3 et 4.

### Évaluation sur la vraie trajectoire

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\Api-Model-Predict"; python eval_delay_model.py 28
```

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\Api-Model-Predict"; python eval_delay_model.py 28 cloud
```

| Contrôle | Référence |
|---|---|
| MAE edge | **2,94 ms** |
| MAE cloud | **8,51 ms** |
| Décision correcte edge | **97,5 %** |
| Décision correcte cloud | **100 %** |
| Fausses alertes | **0** |

❌ Si la MAE edge dépasse 10 ms, ne pas lancer la démonstration : la géométrie
du générateur ne correspond plus à celle déployée.

---

## 6. Réchauffer Ollama

Le backend LAAS vLLM est **inutilisable** — incompatibilité `httpx` sur le
paramètre `proxies`, retiré en httpx 0.28. `intent_manager` bascule donc
systématiquement sur Ollama local.

```bash
curl.exe http://localhost:11434/api/tags
```

Le premier appel LLM peut expirer si le modèle n'est pas encore chargé en
mémoire. **Envoyer une intention à blanc quelques minutes avant de commencer.**

---

## 7. Les deux providers — terminaux neufs

Si des processus traînent :

```bash
Get-NetTCPConnection -LocalPort 8000,8009,8100,8109 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

### Terminal 1 — provider-1

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"; $env:AWARD_GRACE_PERIOD_S="90"; $env:PYTHONIOENCODING="utf-8"; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; .\venv\Scripts\python.exe launch_provider.py --provider provider1 2>&1 | Tee-Object -FilePath "logs\provider1_$(Get-Date -Format yyyyMMdd_HHmm).log"
```

### Terminal 2 — provider-2

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"; $env:AWARD_GRACE_PERIOD_S="90"; $env:PYTHONIOENCODING="utf-8"; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; .\venv\Scripts\python.exe launch_provider.py --provider provider2 2>&1 | Tee-Object -FilePath "logs\provider2_$(Get-Date -Format yyyyMMdd_HHmm).log"
```

### Pourquoi ces variables

| Variable | Rôle |
|---|---|
| `AWARD_GRACE_PERIOD_S=90` | kubectl met 25 à 85 s à propager une migration. À 15 s (défaut), le receveur se démet à tort et provoque une migration parasite `edge1 → edge1c` |
| `PYTHONIOENCODING` + les deux `OutputEncoding` | sans elles, `Tee-Object` force cp1252 et les threads d'affichage meurent sur les caractères de cadre |

✅ Attendu dans les bannières : seuil **28 ms**, `openstack_client opérationnel`,
un provider `ACTIF` et l'autre `STANDBY`.

---

## 8. Contrôle avant de démarrer la voiture

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\picar_local"; python check_infra.py --reel
```

✅ Attendu : tout vert. Les 8 VMs doivent afficher une latence à ±1,5 ms de la
théorie — c'est ce qui confirme que chaque VM tourne avec les coordonnées de la
carte.

---

## 9. Lancer la démonstration

Navigateur sur `http://140.93.64.105:8080/`

✅ Vérifier : vitesse **0.25**, δ **1.5 cm**, buffer proactif **10.5 cm**, les
6 zones edge colorées.

Cliquer **Démarrer**, puis **ne toucher à rien pendant 3 min 30**.

### Vérifier que le niveau 1 du modèle s'active

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"; $f=(Get-ChildItem logs\provider1_*.log | Sort-Object LastWriteTime | Select-Object -Last 1); $f.Name; "Niveau 1 : $((Get-Content $f -Encoding Unicode | Select-String 'Niveau 1').Count)"; "fallback : $((Get-Content $f -Encoding Unicode | Select-String 'fallback').Count)"
```

✅ `Niveau 1` doit augmenter, `fallback` se stabiliser.
❌ Si `fallback` monte encore après 4 minutes, les prédictions sont plates et la
démonstration ne sera pas proactive.

---

## 10. Résultats attendus sur un tour

| Indicateur | Valeur |
|---|---|
| Durée d'un tour | 16 min (240,2 cm à 0,25 cm/s) |
| Migrations par tour | **7**, une par transition de zone |
| Passations inter-provider (chemin B) | 6 |
| Migrations intra-provider (chemin A) | 1 |
| Migrations vers un cloud | **0** |
| Ping-pong | aucun |
| Migrations parasites `edge1 → edge1c` | **aucune** — sinon la grâce n'est pas à 90 s |

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"; $f=(Get-ChildItem logs\provider1_*.log | Sort-Object LastWriteTime | Select-Object -Last 1); Get-Content $f -Encoding Unicode | Select-String 'Migration kubectl réussie' | ForEach-Object { ($_ -replace '\x1b\[[0-9;]*m','') -replace '.*?(\d\d:\d\d:\d\d).*réussie : ','$1  ' -replace ' \| cluster.*','' }
```

### Cohérence des tableaux de bord

```bash
curl.exe http://localhost:8000/status; curl.exe http://localhost:8100/status
```

✅ Un seul `"role":"active"`. Les deux tableaux doivent afficher le **même**
provider actif et le **même** mode.

---

## 11. Mode enhanced — envoi d'une intention

```bash
Invoke-RestMethod -Uri "http://localhost:8002/intent" -Method Post -ContentType "application/json" -Body (@{ intention = "Je veux une latence inférieure à 25 ms" } | ConvertTo-Json) -TimeoutSec 90 | ConvertTo-Json -Depth 6
```

### Vérifier la stratégie de fusion retenue

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"; $f=(Get-ChildItem logs\provider1_*.log | Sort-Object LastWriteTime | Select-Object -Last 1); Get-Content $f -Encoding Unicode | Select-String 'Stratégie de fusion' | Select-Object -Last 3
```

Le LLM compare l'intention précédente à la nouvelle :

| Enchaînement | Stratégie |
|---|---|
| Deux usages **différents** — « upscaling 4K » puis « je regarde un direct » | `REPLACE` |
| Même usage, référence à la précédente — « réduis **encore** la latence » | `ADDITIVE` |
| Première intention | `REPLACE` |

### Revenir en mode autonome sans redémarrer

```bash
curl.exe -X POST http://localhost:8000/reset; curl.exe -X POST http://localhost:8100/reset
```

---

## Dépannage rapide

| Symptôme | Cause | Correction |
|---|---|---|
| `cycle` reste à 0 | le pont n'atteint pas le PC | vérifier `140.93.89.92` dans `picar_bridge_QoS1.py` |
| Rien ne bouge, aucun tick | page du navigateur non ouverte | le pont est l'horloge du système |
| `UnicodeEncodeError` au lancement | `Tee-Object` force cp1252 | poser les trois variables d'encodage |
| Migrations parasites `edge1 → edge1c` | grâce à 15 s | `AWARD_GRACE_PERIOD_S=90` |
| `window_size` du delay ≠ 35 | réentraînement automatique | étape 3, puis réentraîner |
| Les deux tableaux affichent un provider différent | correctif `is_active` non chargé | redémarrer les providers |
| Intention refusée / expirée | Ollama froid ou arrêté | réchauffer, puis réessayer |
| `openstack_client injoignable` | master arrêté | relancer `openstack_client.py` |

---

## Mode local — sans accès au PiCar

Le dossier `PFE_juin\picar_local\` reproduit toute l'infrastructure sur le PC :
8 VMs simulées, doublure du master sans `kubectl`, pont et simulateur.

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\picar_local"; python launch_vms_local.py
```

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\picar_local"; python fake_openstack.py
```

```bash
cd "C:\Users\ahmed\Desktop\PFE_juin\picar_local"; python picar_bridge_LOCAL.py
```

Puis, dans **chaque** terminal provider, avant le lancement :

```bash
$env:ALL_VM_REGISTRY_JSON='{"edge1":{"ip":"127.0.0.1","port":8301},"edge1b":{"ip":"127.0.0.1","port":8302},"edge1c":{"ip":"127.0.0.1","port":8303},"edge2":{"ip":"127.0.0.1","port":8304},"edge2b":{"ip":"127.0.0.1","port":8305},"edge2c":{"ip":"127.0.0.1","port":8306},"cloud1":{"ip":"127.0.0.1","port":8307},"cloud2":{"ip":"127.0.0.1","port":8308}}'; $env:OPENSTACK_MASTER_IP='localhost'; $env:PICAR_BRIDGE_URL='http://localhost:8080'
```

Navigateur sur `http://localhost:8080/`. **Aucun `kubectl` n'est exécuté** — les
migrations sont tenues en mémoire, on peut répéter autant qu'on veut.

Pour revenir au réel : ne poser **aucune** de ces trois variables. Les valeurs
par défaut pointent déjà sur l'infrastructure LAAS.
