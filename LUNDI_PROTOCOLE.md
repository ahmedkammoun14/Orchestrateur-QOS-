# LUNDI AU LAAS — protocole détaillé, use case par use case

> **Objectif de la journée :** produire les données qui font passer l'axe
> évaluation du papier de **5/10 à ~8/10**, seul point faible restant.
>
> **Deux règles absolues, valables toute la journée :**
> 1. **Ne modifier AUCUNE ligne de code.** Ni les poids de l'horizon, ni le
>    cooldown, ni la marge. Sinon les runs ne sont plus comparables entre eux
>    ni avec `data_UC1_federe` / `data_UC2_ablation`.
> 2. **Ne rien décider après coup.** Les critères de lecture des résultats
>    sont fixés ci-dessous, avant les runs.

---

# AVANT DE PARTIR — une décision à écrire

## La phrase hors-domaine (UC5, phrase 4)

`Please back up my data every hour` ne correspond à aucune métrique du
registre (`latency`, `cpu_usage`, `ram_usage`).

**Décidez maintenant comment vous rapporterez le résultat, quel qu'il soit :**

| Résultat | Interprétation, fixée d'avance |
|---|---|
| Le LLM **rejette** ou produit un contrat vide | Garde-fou fonctionnel — résultat positif |
| Le LLM **invente un contrat plausible** | **Défaillance du garde-fou — résultat publiable aussi** |

Les deux cas se rapportent. Choisir après le run reviendrait à sélectionner
ce qui arrange, et cela se voit.

---

# 9h00 — CONTRÔLES PRÉALABLES (30 min)

## Objectif
Éviter de découvrir après trois heures de runs qu'une VM ne répondait pas —
c'est arrivé le 14 août avec `194.199.113.28`.

## Actions

```powershell
# 1. data/ doit être vide
ls data

# 2. Les 8 agents VM
curl.exe -sS http://194.199.113.18:8200/health   # edge1
curl.exe -sS http://194.199.113.18:8201/health   # edge1b
curl.exe -sS http://194.199.113.18:8202/health   # edge1c
curl.exe -sS http://194.199.113.28:8200/health   # edge2   <-- VIGILANCE
curl.exe -sS http://194.199.113.28:8201/health   # edge2b  <-- VIGILANCE
curl.exe -sS http://194.199.113.28:8202/health   # edge2c  <-- VIGILANCE
curl.exe -sS http://194.199.113.66:8200/health   # cloud1
curl.exe -sS http://194.199.113.69:8200/health   # cloud2

# 3. Le master (migrations kubectl réelles)
curl.exe -sS http://194.199.113.8:8024/health

# 4. Les 3 APIs ML
curl.exe -sS http://localhost:5001/health
curl.exe -sS http://localhost:5002/health
curl.exe -sS http://localhost:5003/health
```

Sur le Raspberry Pi :
```bash
mv latences.csv latences_avant_campagne.csv
```

## Critère de passage
**Les 8 agents répondent.** Si `194.199.113.28` ne répond pas, relancer
`./launch_edge2_machine.sh` sur cette machine **avant** de continuer.

---

# 9h30 — UC5 : SIX INTENTIONS VARIÉES (30 min)

## Objectif scientifique
La contribution 3 (traduction d'intention par LLM) repose aujourd'hui sur
**deux classes de phrases**. Les trois évaluateurs soulèvent la même
objection : à cette échelle, une heuristique « calcul → cloud, latence →
edge » ferait aussi bien.

**Ce que cet UC démontre :** le LLM traite des formulations qu'aucune règle
simple ne couvre — ambiguës, contradictoires, hors-domaine.

## Configuration
Fédéré, **deux orchestrateurs**, `MULTI_PROVIDER_ENABLED="true"`.

## Déroulé
1. Lancer les deux orchestrateurs
2. **5 minutes en autonome** (l'historique doit se remplir pour le MI)
3. Envoyer les six phrases, **espacées de 2 minutes**, toutes sur le port 8002

```powershell
try {
    Invoke-RestMethod -Uri "http://localhost:8002/intent" -Method Post `
      -ContentType "application/json" `
      -Body (@{ intention = "<LA PHRASE>" } | ConvertTo-Json) `
      -TimeoutSec 90 | ConvertTo-Json -Depth 6
} catch { "ERREUR : $($_.Exception.Message)"; $_.ErrorDetails.Message }
```

## Les six phrases, dans l'ordre

| # | Phrase | Ce qu'elle teste | Attendu |
|---|---|---|---|
| 1 | `I need a lot of memory for in-memory caching, latency is not critical` | Mémoire seule, sans calcul | contrat RAM dominant |
| 2 | `I just want it to feel smooth for my users` | **Ambiguë** — aucun terme technique | contrat cohérent malgré l'absence d'indice |
| 3 | `Make it as fast as possible but keep the cost low` | **Contradictoire** | arbitrage visible dans les poids |
| 4 | `Please back up my data every hour` | **Hors-domaine** | rejet (voir décision écrite d'avance) |
| 5 | `This is a real-time video call, delay is unacceptable` | Latence reformulée | même placement que l'ancienne phrase latence |
| 6 | `Run heavy batch analytics overnight, response time does not matter` | Calcul reformulé + négation latence | même placement que l'ancienne phrase calcul |

## Ce qu'on relève dans le terminal
Après chaque intention :
```
📋 SLOs mis à jour — N SLO(s) actif(s) | primaires : [...] | secondaires : [...]
```
Notez le contrat produit pour chacune des six.

⚠️ **Signal d'alarme** : si `cpu_usage >= 1` apparaît en **primaire**, ou une
latence à un seuil autre que celui annoncé par le LLM, arrêtez et signalez —
c'est la signature du bug de course corrigé le 14 août.

## Archivage
```powershell
mv data data_UC5_intentions ; mkdir data
Copy-Item "logs\*UC5*.log" "data_UC5_intentions\"
```
Puis `scp` de `latences.csv`, et sur le Pi `mv latences.csv latences_UC5.csv`.

## Critère de réussite
Les six intentions ont produit un contrat, et les contrats sont notés.
**Même si un contrat est mauvais, l'UC est réussi** — c'est un résultat.

---

# 10h15 — CAMPAGNE : 4 FÉDÉRÉS + 4 ABLATIONS (7 h 20)

## Objectif scientifique
Aujourd'hui, une seule exécution par condition. Les intervalles de confiance
actuels bornent la variabilité **à l'intérieur** d'un run, jamais **entre**
runs. C'est la critique n°1 des trois évaluateurs.

**Pourquoi 4 et pas 3 :** avec 3 contre 3, le test de Mann-Whitney ne peut
structurellement pas descendre sous p = 0,10 (20 arrangements possibles,
minimum 2/20). Avec **4 contre 4** : 70 arrangements, minimum **2/70 =
0,029**. C'est le plus petit effort qui rend la significativité atteignable.

⚠️ Ce minimum exige une **séparation parfaite** — les 4 fédérés tous en
dessous des 4 ablations. Vos données rendent cela probable (30 points
séparent le pire fédéré du meilleur ablation observés), mais **ne le
promettez à personne avant d'avoir les chiffres**.

## Configuration des deux conditions

| | Fédéré | Ablation |
|---|---|---|
| Variable | `MULTI_PROVIDER_ENABLED="true"` | `="false"` |
| Orchestrateurs | provider1 **et** provider2 | **provider1 seul** |
| Cibles disponibles | 8 | 4 |

**C'est la seule différence.** Même trajectoire, même seuil, même code.

## Durée d'un run : 55 minutes

| Phase | Durée |
|---|---|
| Démarrage orchestrateurs + vérification | 5 min |
| Fenêtre autonome avant lancement du véhicule | 3 min |
| **Roulage : 3 passages au même point** | **~40 min** |
| Arrêt PiCar + attente 30 s + arrêt orchestrateurs | 2 min |
| Archivage, copie logs, scp, renommage Pi | 5 min |

⚠️ **Trois passages, pas trois tours.** Trois passages au même point donnent
**deux tours complets exploitables** (fenêtre de 32 min) — c'est exactement
la base des chiffres actuels. Viser trois tours complets exigerait un
quatrième passage, 16 min de plus par run, pour un gain nul : la variance
qui manque est **entre** les runs, pas à l'intérieur d'un run.

## Les 3 minutes autonomes sont un plancher, pas une marge

`launch_provider.py` **purge automatiquement** la base Redis du provider à
chaque démarrage (`_flush_redis`). C'est une bonne nouvelle pour la
campagne : chacun des huit runs repart d'un état froid identique, et c'est
précisément la condition qui rend la variance entre runs interprétable.

**Ne jamais utiliser `--keep-redis` pendant la campagne.**

Conséquence directe : après purge, la fenêtre de prédiction de la latence
exige **18 points**, soit environ **108 secondes** à 6 s par cycle. Les
3 minutes prévues avant de lancer le véhicule couvrent tout juste ce besoin.

⚠️ Si vous prenez du retard dans la journée, ce sera la première chose que
vous serez tenté de rogner. **Ne le faites pas** — vous démarreriez le
roulage avec un prédicteur froid, et le run ne serait plus comparable aux
autres.

> À reprendre dans le paragraphe de reproductibilité du papier : chaque
> exécution démarre d'un état Redis purgé, suivi d'une fenêtre de préchauffage
> qui remplit la fenêtre de prédiction avant tout mouvement du véhicule.

## Planning — alterner les conditions

| Heure | Run | Dossier |
|---|---|---|
| 10h15 | fédéré 1 | `data_UC1_federe_run1` |
| 11h10 | ablation 1 | `data_UC2_ablation_run1` |
| 12h05 | fédéré 2 | `data_UC1_federe_run2` |
| 13h00 | ablation 2 | `data_UC2_ablation_run2` |
| **13h55** | **pause 30 min** | |
| 14h25 | fédéré 3 | `data_UC1_federe_run3` |
| 15h20 | ablation 3 | `data_UC2_ablation_run3` |
| 16h15 | fédéré 4 | `data_UC1_federe_run4` |
| 17h10 | ablation 4 | `data_UC2_ablation_run4` |
| **18h05** | **fin** | |

**Pourquoi alterner :** si une dérive survient en cours de journée — charge
réseau, VM qui ralentit, API ML qui fatigue — elle affecte les deux groupes
également au lieu d'en biaiser un seul. Et si vous devez vous arrêter en
route, vous vous arrêtez toujours sur un nombre égal de runs par condition.

## Checklist entre chaque run — à cocher

```
[ ] 1. Arrêter le PiCar
[ ] 2. ATTENDRE 30 SECONDES (vidange de la file d'écriture Excel)
[ ] 3. Couper les orchestrateurs (Ctrl+C dans chaque terminal)
[ ] 4. Vérifier que qos_history_provider1.xlsx s'ouvre
[ ] 5. mv data data_UCx_..._runN ; mkdir data
[ ] 6. Copier les logs du run dans le dossier
[ ] 7. scp latences.csv du Pi vers le dossier
[ ] 8. Sur le Pi : mv latences.csv latences_runN.csv
[ ] 9. Noter l'heure de début et de fin dans un fichier notes
[ ] 10. Au run SUIVANT : poser MULTI_PROVIDER_ENABLED sur la même ligne que
        le lancement, puis contrôler la négociation à la 2e minute
```

**L'étape 2 n'est pas optionnelle.** Sans elle, la fin du run part avec les
tâches d'écriture en attente — c'est ce qui a amputé la fin d'UC1 le 13 août.

## Commandes de lancement

**Fédéré — terminal 1**
```powershell
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"
$env:MULTI_PROVIDER_ENABLED="true"; $env:AWARD_GRACE_PERIOD_S="90"
$env:PYTHONIOENCODING="utf-8"
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$OutputEncoding=[System.Text.Encoding]::UTF8
.\venv\Scripts\python.exe launch_provider.py --provider provider1 2>&1 | Tee-Object -FilePath "logs\FED_run1_p1_$(Get-Date -Format yyyyMMdd_HHmm).log"
```

**Fédéré — terminal 2** : idem avec `--provider provider2` et `p2` dans le
nom du log.

**Ablation — UN SEUL terminal**
```powershell
cd "C:\Users\ahmed\Desktop\PFE_juin\uc1(02-06-2026)\qos-orchestrator"
$env:MULTI_PROVIDER_ENABLED="false"; $env:AWARD_GRACE_PERIOD_S="90"
$env:PYTHONIOENCODING="utf-8"
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$OutputEncoding=[System.Text.Encoding]::UTF8
.\venv\Scripts\python.exe launch_provider.py --provider provider1 2>&1 | Tee-Object -FilePath "logs\ABL_run1_$(Get-Date -Format yyyyMMdd_HHmm).log"
```

## ⚠️ LE BRAS EXPÉRIMENTAL EST INVISIBLE — lire avant le premier run

`MULTI_PROVIDER_ENABLED` est la **seule** variable qui distingue les deux
bras. Elle n'est affichée nulle part : la bannière du lanceur imprime le
provider, les ports et la base Redis, pas le drapeau ; aucun `/health` ne
l'expose. Elle n'est lue qu'à un seul endroit, `hub/orchestrator_core.py:992`.

**Le sens de la panne est piégeux.** `launch_provider.py:54` pose `"true"`
par défaut quand la variable est absente, alors que `shared/config.py:367`
pose `"false"`. C'est le lanceur qui gagne. Donc :

| Oubli | Conséquence |
|---|---|
| Variable absente pendant une phase d'ablation | Run **fédéré** étiqueté ablation |
| Session PowerShell qui garde `"false"`, relance du fédéré avec ↑ sur la seule dernière ligne | Deux orchestrateurs **sans négociation** — ni un bras ni l'autre, run à jeter |

**Posez toujours la variable sur la même ligne que le lancement.** Ne
rappelez jamais uniquement la ligne `python` avec ↑.

## Vérification au démarrage de chaque run

### ❌ Ce qui NE marche PAS

```powershell
curl.exe -sS http://localhost:8010/health   # ne prouve RIEN sur le bras
```
`peer_relays` est posé **inconditionnellement** dans `_env_for()`
(`launch_provider.py:56`) : cette commande affiche exactement la même chose
en fédéré et en ablation. Elle vérifie le câblage des relais, pas le bras.

### ✅ Le contrôle qui marche — à la 2ᵉ minute, pas à la 55ᵉ

Regardez le terminal du hub **deux minutes après le démarrage** :

| Bras | Ce qui doit apparaître | Ce qui ne doit JAMAIS apparaître |
|---|---|---|
| Fédéré | diffusions et offres (négociation) | — |
| Ablation | — | toute trace de négociation |

C'est le même critère d'intégrité que celui déjà appliqué dans
`data_UC2_ablation/notes.txt` (« Négociations : 0 »), simplement vérifié
**tout de suite**. Deux minutes perdues valent mieux que cinquante-cinq.

**Ablation, en plus :** dans le terminal du hub, le rôle doit être **`ACTIF`**
(affiché en vert, `orchestrator_core.py:456`), pas `STANDBY`. Si `STANDBY`,
le service est hébergé chez provider-2 — il faut d'abord le ramener côté
provider-1.

## Critère de réussite
**Huit dossiers**, chacun avec 3 passages détectables, les 6 fichiers Excel,
les logs et `latences.csv`.

---

# SI LA JOURNÉE DÉRAPE

| Temps réellement disponible | Faire |
|---|---|
| 1 h | UC5 seul |
| 3 h | UC5 + 2 fédérés + 2 ablations |
| 5 h | UC5 + 3+3 |
| **7 h** | **UC5 + 4+4** ⭐ objectif |

**En dessous de 3 h, ne lancez pas la campagne.** Deux runs par condition
n'apportent pas assez pour justifier le temps. Faites UC5 et reportez.

**Toujours s'arrêter sur un nombre égal par condition.** C'est ce que
garantit l'alternance.

---

# CE QUI EST REPORTÉ — et pourquoi

## §3 — Fédération sur deux machines

**Reporté à un autre après-midi.** Une heure suffit, sans PiCar, sans
trajectoire propre, sans journée entière. Son gain — supprimer une phrase de
la section « menaces à la validité » — est réel mais bien inférieur à celui
de passer de 3+3 à 4+4.

## Les améliorations du code

`AMELIORATIONS_A_FAIRE.md` liste trois pistes, dont les poids de l'horizon
`[7,6,5,4,3,2,1]` identifiés comme cause du retard de 12 s.

**Ne rien appliquer lundi.** Si le temps le permet **après** les huit runs,
un neuvième run fédéré avec le correctif transformerait une limitation en
optimisation démontrée — mais seulement après avoir sécurisé la base.

---

# UN POINT À TRANSMETTRE POUR LA RÉDACTION

**La trajectoire est déterministe.** La latence est une fonction exacte de la
position, et le PiCar suit le même parcours programmé à chaque run. La
variance entre runs ne viendra donc **pas** de l'environnement, mais de la
marche aléatoire CPU/RAM, des prédictions ML et du décalage de phase entre
position et cycle de décision.

C'est ce qui explique la stabilité d'UC2 entre ses deux tours — 55,5 % et
55,8 %.

**Phrase à intégrer en Section VI ou VII :**

> *La réplication mesure la variance du comportement de l'orchestrateur —
> décisions, cadence, prédictions — et non celle de l'environnement, la
> trajectoire étant déterministe par construction.*

Sans elle, un relecteur demandera pourquoi répliquer une expérience dont
l'entrée est déterministe.

**Ne pas varier les positions de départ** pour ajouter de la variance : cela
casserait la comparabilité entre conditions, et à n=4 le test apparié
(Wilcoxon, minimum p = 0,125) serait moins puissant que le test non apparié
visé (Mann-Whitney, 0,029).

---

# EN FIN DE JOURNÉE

Transmettre la liste des dossiers créés avec leurs heures de début et de fin,
et les six contrats produits par UC5.

Le traitement qui suit :
1. Adapter les scripts pour boucler sur les 8 dossiers
2. Recalculer tous les chiffres
3. Réécrire §VI avec moyennes ± écarts-types
4. Test de Mann-Whitney sur 4+4
5. Supprimer la menace « une seule exécution » de §VII
6. Réduire ou supprimer « Two intent classes only » grâce à UC5
7. Régénérer les figures 4 et 5

---

# RÉCAPITULATIF

| UC | Objectif | Durée | Priorité |
|---|---|---|---|
| Contrôles | Éviter un run perdu sur une VM muette | 30 min | obligatoire |
| **UC5** | **Sauver la contribution 3** | **30 min** | ⭐⭐ absolue |
| **Campagne 4+4** | **Débloquer l'axe évaluation** | **7 h 20** | ⭐ décisive |
| §3 deux machines | Supprimer « co-localisé » | 1 h | reportée |

**Note visée : 6,5 → ~7,8.**
Plafond structurel ~8,5, limité par la QoS émulée et le banc à 4 machines.
