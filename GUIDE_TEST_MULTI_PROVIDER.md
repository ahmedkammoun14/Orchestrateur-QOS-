# Guide de test — Multi-provider transversal

> À utiliser sur l'infrastructure réelle (4 VMs OpenStack + PiCar).
> Objectif : vérifier que chaque cas se produit **et** se voit.
> Plan : [PLAN_MULTI_PROVIDER_TRANSVERSAL.md](PLAN_MULTI_PROVIDER_TRANSVERSAL.md)

---

## 0. Préparation

### Lancer
```powershell
.\start_all_multi.ps1                 # phase 1 : multi-provider OFF
.\start_all_multi.ps1 -MultiProvider  # phases 2 à 4 : ON
```
Les logs sont capturés dans `logs\run_<horodatage>\`.

### Vérifier avant de commencer

> ⚠️ **Sous Windows PowerShell, `curl` est un alias vers `Invoke-WebRequest`**, qui
> tente d'analyser la réponse avec le moteur d'Internet Explorer et déclenche un
> avertissement de sécurité. Utiliser **`Invoke-RestMethod`** (désérialise le JSON
> directement), ou le vrai binaire **`curl.exe`**.

```powershell
Invoke-RestMethod http://localhost:8010/health     # relais : doit lister les 2 providers
Invoke-RestMethod http://localhost:8000/health     # hub
start http://localhost:8009                        # dashboard
```

Vérifier que le mode multi-provider est bien actif, **dans la fenêtre du hub avant de
le lancer** (la variable est lue à l'import de `config.py`) :
```powershell
$env:MULTI_PROVIDER_ENABLED = "true"
python -c "from shared import config; print('multi-provider =', config.MULTI_PROVIDER_ENABLED)"
```

### Côté infrastructure
Les 4 scripts VM (`*_ping_fixeCarac*.py`) et `picar_bridge.py` doivent tourner.

### Changer le seuil (phases 3 et 4)
`shared/config.py` ligne 208 : `"default_threshold": 100.0` → `60.0`. **Redémarrer le hub.**

---

## 1. Rappel du pipeline complet

### Mode AUTONOMOUS
```
PiCar ──POST /rtt──► latency_manager ──► hub
   │
   ▼ hub._run_flow, à chaque cycle :
   step1_slos        cycles < 5  → bootstrap, SLO primaire seul (latence < seuil)
                     cycles ≥ 5  → metrics_manager.select_dynamic_slos()
                                   · latence  = PRIMAIRE, seuil FIXE du registre
                                   · cpu/ram  = SECONDAIRE si MI > 0.15, seuil percentile
   step2_persist_slos
   step3_collect      collector interroge les 4 VMs (/metrics :8200)
   step4_persist      Redis + Excel
   step5_check        violations sur la VM active (log seulement)
   step6_histories    50 points par VM
   step7_predict      ml_predictor, horizon 7, cascade 3 niveaux
   step8_decide  ◄── AIGUILLAGE ICI
```

### Mode ENHANCED
```
Toi ──POST /intent──► intent_manager
   │  LLM (LAAS Qwen3 → Ollama) extrait : SLOs + poids + merge_strategy
   │  SLOMerger applique REPLACE ou ADDITIVE
   ▼
  hub /intent  → mode = "enhanced", current_slos remplacés
   │
   ▼ cycles suivants :
   step1_slos → metrics_manager.validate_and_enrich_slos()
                · SLOs du LLM = PRIMAIRES, seuil ET poids du LLM conservés
                · métriques non couvertes + MI > 0.15 → SECONDAIRES adaptatifs
   … puis identique à autonomous
```

### `step8_decide` — l'aiguillage
```
MULTI_PROVIDER_ENABLED = False → _decide_mono_provider   (code d'origine, 4 VMs)
MULTI_PROVIDER_ENABLED = True  → _decide_multi_provider  (machine à états)
                                   │
                    provider courant = PROVIDER_OF_VM[service_vm]
                    evaluate_provider(courant, slos, 4 VMs, prédictions)
                                   │
        ┌──────────────────────────┴──────────────────────────┐
   compliant_vms non vide                          compliant_vms vide
        │                                                     │
   CHEMIN A                                    POST relais :8010 /handoff
   TOPSIS sur CES VMs                                         │
   → STAY ou migration intra                    hub /intent/relay (rôle autre provider)
                                                evaluate + negotiate + local_topsis
                                                              │
                                    ┌─────────────┬───────────┴────────┬──────────────┐
                          prend_local_conforme  prend_local_meilleure  cede_a_l_offre  aucune_option
                              CHEMIN B              CHEMIN C            CHEMIN C        CHEMIN D
                          migration inter      migration inter      on garde/migre     STAY
                                                                     chez nous      IMPOSSIBLE
```

---

## 2. Positions de piste qui déclenchent chaque cas

Calculées sur les 398 points réels de `DATA.path`, avec la physique déployée
(edge B=5/A=150, cloud B=30/A=210).

### Seuil 100 ms
| Situation | Position (x, y) cm | Latence P1 | Latence P2 |
|---|---|---|---|
| Les deux providers conformes | (−1.7, 1.8) | 73.8 ms | 63.1 ms |
| **P1 seul** conforme | (−3.0, 55.0) | 32.7 ms | 101.1 ms |
| **P2 seul** conforme | (12.4, −17.6) | 100.9 ms | 35.4 ms |
| Aucun conforme | **INEXISTANT** | — | — |

### Seuil 60 ms
| Situation | Position (x, y) cm | Latence P1 | Latence P2 |
|---|---|---|---|
| Les deux conformes | **INEXISTANT** | — | — |
| **P1 seul** conforme | (6.8, 32.3) | 59.9 ms | 90.1 ms |
| **P2 seul** conforme | (−1.9, −9.7) | 65.3 ms | 59.5 ms |
| **Aucun conforme** | (−1.7, 1.8) | 73.8 ms | 63.1 ms |

> Rappel : `provider-1 = {edge1, cloud1}` (colonne x=−20),
> `provider-2 = {edge2, cloud2}` (colonne x=+30).

---

## 3. Les 4 phases de test

### PHASE 1 — seuil 100, flag OFF · non-régression
**But :** prouver qu'on n'a rien cassé.

| À vérifier | Attendu |
|---|---|
| `hub.log` | Cycles normaux, aucune mention de « chemin » ni de provider |
| Dashboard | Compteur `INTRA/INTER/NÉGO/IMPOSSIBLE` **à zéro partout** |
| Journal d'audit | Identique à d'habitude, uniquement des lignes MIGRATION |
| `provider_relay.log` | Aucune requête reçue |

🔴 **Si quelque chose diffère de ton comportement habituel, on s'arrête et on corrige.**

---

### PHASE 2 — seuil 100, flag ON · chemins A et B
**But :** voir la machine à états fonctionner, sans négociation.

Fais un tour de piste complet à vitesse lente (0.5 cm/s).

| À vérifier | Attendu |
|---|---|
| `hub.log` | Lignes `chemin A` et `chemin B`, avec le provider utilisé |
| Dashboard | Badges **INTRA** (vert) et **INTER** (orange) |
| Compteur | INTRA > 0, INTER > 0, **NÉGO = 0**, IMPOSSIBLE = 0 |
| `provider_relay.log` | Passations sur chemin B uniquement |
| `decision_intelligence.log` | TOPSIS sur **2 VMs max** (jamais 4) |

🔴 **Si NÉGO apparaît à 100 ms → bug.** Aucune position de ta piste ne rend les deux
providers non conformes à ce seuil.

🔴 **Si TOPSIS reçoit 4 candidats → bug** : le filtrage par provider n'est pas appliqué.

---

### PHASE 3 — seuil 60, flag ON · le Cas 5
**But :** déclencher la négociation. C'est le test décisif.

Change le seuil à 60 dans `config.py`, redémarre le hub, refais un tour.

| À vérifier | Attendu |
|---|---|
| Dashboard | Badge **NÉGO** (violet) apparaît, ~28 % des cycles |
| Compteur | NÉGO > 0 |
| `provider_relay.log` | Passations avec un champ `offer` non nul |
| `hub.log` | `breach_type = inter_provider_negotiation` |
| Journal d'audit | Phrase « Aucun provider conforme — négociation remportée par … » |

**À observer de près :** quand la voiture est près de (−1.7, 1.8), P1 propose ~73.8 ms
et P2 ~63.1 ms → **P2 doit gagner** la négociation. Vérifie que le service part bien
chez provider-2.

---

### PHASE 4 — intention hors domaine
**But :** tester la robustesse sémantique.

```powershell
$body = @{ intent_id = "sec-001"
           intention = "Alert me if my connection is being intercepted or downgraded (e.g. HTTPS stripping)" }
$bytes = [System.Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Compress))
Invoke-RestMethod -Uri "http://localhost:8002/intent" -Method POST `
                  -ContentType "application/json; charset=utf-8" -Body $bytes
```

C'est une intention de **sécurité**, or `METRICS_REGISTRY` ne connaît que
`latency`, `cpu_usage`, `ram_usage`. Deux issues, **toutes deux correctes** :

| Issue | `intent_manager.log` | Conséquence attendue |
|---|---|---|
| **A** — refus propre | `ℹ️ tableau vide : intention hors du domaine réseau/QoS` | Les SLOs actifs **restent inchangés**, le système continue |
| **B** — interprétation | `✅ N SLO(s) extraits \| stratégie : replace` | Service d'inspection : CPU (analyse du trafic) + latence (alerte rapide) |

🔴 **Bug si :** les SLOs actifs sont effacés sans être remplacés, ou si le hub plante,
ou si le LLM invente une métrique absente du registre.

Si tu obtiens l'issue B, **vérifie que les seuils produits sont plausibles** — c'est là
qu'une hallucination se verrait.

---

## 4. Checklist récapitulative

| # | Test | Attendu | ✅ / ❌ |
|---|---|---|---|
| 1 | Phase 1 — comportement identique à avant | Compteur à zéro | |
| 2 | Phase 2 — chemin A observé | Badge INTRA | |
| 3 | Phase 2 — chemin B observé | Badge INTER + passation dans le relais | |
| 4 | Phase 2 — NÉGO absent | Compteur NÉGO = 0 | |
| 5 | Phase 2 — TOPSIS sur ≤ 2 VMs | Log decision_intelligence | |
| 6 | Phase 3 — chemin C observé | Badge NÉGO | |
| 7 | Phase 3 — le bon provider gagne | P2 à (−1.7, 1.8) | |
| 8 | Phase 4 — intention hors domaine gérée | SLOs préservés ou remplacés proprement | |
| 9 | Aucun plantage sur tout le parcours | | |
| 10 | Relais injoignable → STAY, pas de crash | Couper `provider_relay`, observer | |

Le test **10** vaut le coup : arrête `provider_relay` en pleine démo. Le hub doit
continuer à tourner et faire STAY, sans exception. C'est le filet de robustesse.

### Injecter manuellement les 4 chemins dans le dashboard
Utile pour vérifier l'affichage sans attendre que la voiture passe au bon endroit :
```powershell
foreach ($p in 'A','B','C','D') {
    $body = @{
        decision      = "migrate"; from_vm = "edge1"; to_vm = "edge2"
        reason        = "test chemin $p"; topsis_score = 0.8
        breach_type   = "inter_provider_negotiation"; cycle = 1; mode = "autonomous"
        provider_path = $p; provider_used = "provider-2"
    } | ConvertTo-Json -Compress
    Invoke-RestMethod -Uri "http://localhost:8009/audit" -Method POST `
                      -ContentType "application/json" -Body $body | Out-Null
    Write-Host "  chemin $p injecte"
}
```
Le compteur du dashboard doit alors afficher `INTRA 1  INTER 1  NEGO 1  IMPOSSIBLE 1`.

---

## 5. Que collecter pour l'analyse

À la fin de chaque phase, conserve le dossier `logs\run_<horodatage>\` complet, plus :

```powershell
Invoke-RestMethod http://localhost:8009/audit/log |
    ConvertTo-Json -Depth 8 |
    Out-File -Encoding utf8 "audit_phase1.json"
```

Commandes utiles pendant l'observation :
```powershell
Invoke-RestMethod http://localhost:8000/status     # mode, VM active, cycle, derniere decision
Invoke-RestMethod http://localhost:8010/health     # table de routage du relais
```

Les fichiers qui portent l'information :

| Fichier | Ce qu'on y lit |
|---|---|
| `hub.log` | Chemin emprunté, provider retenu, décision, migration |
| `provider_relay.log` | Passations, garde anti-boucle, erreurs de transport |
| `decision_intelligence.log` | Détail TOPSIS, taille du pool de candidats |
| `intent_manager.log` | Extraction LLM, stratégie de fusion |
| `metrics_manager.log` | Scores MI, seuils adaptatifs |
| `audit_phase<N>.json` | Journal complet avec `provider_path` et `provider_used` |

---

## 6. Limites connues à mentionner dans le mémoire

- **Isolation logique, pas physique** : le `collector` collecte les 4 VMs à chaque
  cycle ; c'est `candidates_for_provider()` qui restreint le pool. Inévitable en
  mono-orchestrateur, puisqu'un seul processus joue les deux rôles. En distribué,
  l'isolation devient physique sans changement de code.
- **Instantané partagé** : les deux rôles lisent le même `state.last_collected`, donc
  évaluent sur des données rigoureusement simultanées. En distribué, chaque
  orchestrateur aurait le sien, pris à un instant légèrement différent — la simulation
  est donc *plus* cohérente que la réalité, jamais moins.
- **Asymétrie de critères non traitée** : deux offres peuvent porter sur des jeux de
  métriques différents si des prédictions manquent de façon inégale. Documenté,
  volontairement non traité (version minimale).
